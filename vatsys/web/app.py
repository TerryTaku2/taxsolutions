"""Web interface (server-rendered pages)."""

import csv
import io
import mimetypes
import re
import secrets
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .. import audit, config
from ..checks import run_checks
from ..classify import validate_rule_pattern
from ..compute import LINE_LABELS, compute_return
from ..db import Database
from ..demo import EMAIL as DEMO_EMAIL
from ..export import return_to_csv, return_to_xlsx
from ..ingest import (
    FIELD_LABELS,
    IngestError,
    UploadOptions,
    classifier_for,
    import_file,
    load_table_preview,
    locked_ranges,
    reprocess_upload,
)
from ..ingest.parse import parse_amount, parse_date
from ..models import (
    CURRENCIES,
    DIRECTIONS,
    FILING_FREQUENCIES,
    INPUT_GENERAL,
    INPUT_TYPES,
    PURCHASE,
    VAT_CATEGORIES,
    AuditLog,
    Business,
    ClassificationRule,
    ExchangeRate,
    ReferenceItem,
    Transaction,
    Upload,
    User,
    VatRate,
    VatReturn,
)
from ..money import fmt
from ..periods import Period, parse_period, period_for, periods_covering
from ..questions import apply_answer, pending_groups
from ..reference import import_items, template_xlsx
from ..registration import rolling_turnover
from ..reminders import deadline_for
from ..security import hash_password, verify_password
from ..summary import CURRENCY_NAMES, business_summaries, summarise

HERE = Path(__file__).parent
# Not every system's MIME table knows the web app manifest, and browsers expect this type for it.
mimetypes.add_type("application/manifest+json", ".webmanifest")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["money"] = fmt
templates.env.filters["pct"] = lambda v: "" if v is None else f"{Decimal(v).normalize():f}%"
templates.env.filters["d"] = lambda v: "" if v is None else v.strftime("%d %b %Y")
templates.env.globals.update(VAT_CATEGORIES=VAT_CATEGORIES, INPUT_TYPES=INPUT_TYPES, DIRECTIONS=DIRECTIONS,
                             CURRENCIES=CURRENCIES, FILING_FREQUENCIES=FILING_FREQUENCIES,
                             FIELD_LABELS=FIELD_LABELS, LINE_LABELS=LINE_LABELS, CURRENCY_NAMES=CURRENCY_NAMES)

SOURCE_TYPES = {"ledger": "Sales or purchases list (Excel/CSV)", "bank": "Bank statement",
                "mobile_money": "Mobile money export (EcoCash, OneMoney, InnBucks)"}


PAGE_SIZE = 500


MIN_PASSWORD_LENGTH = 10


class LoginRequired(Exception):
    pass


class PasswordChangeRequired(Exception):
    pass


def create_app(db: Database | None = None, storage_dir: Path | None = None) -> FastAPI:
    db = db or Database()
    db.create_all()
    storage_dir = storage_dir or config.DATA_DIR

    app = FastAPI(title="VAT Computation System", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, same_site="lax",
                       https_only=False, max_age=60 * 60 * 12)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.db = db

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(PasswordChangeRequired)
    async def _password_change_required(request: Request, exc: PasswordChangeRequired):
        return RedirectResponse("/account/password", status_code=303)

    # ---- dependencies --------------------------------------------------

    def get_db():
        with db.session() as s:
            yield s

    async def csrf_protect(request: Request):
        if request.method == "POST":
            form = await request.form(max_fields=PAGE_SIZE * 6 + 50)
            token = request.session.get("csrf")
            if not token or not secrets.compare_digest(str(form.get("csrf", "")), token):
                raise HTTPException(400, "Form expired. Go back, reload the page and try again.")

    def signed_in_user(request: Request, s: Session = Depends(get_db)) -> User:
        uid = request.session.get("uid")
        user = s.get(User, uid) if uid else None
        if not user:
            raise LoginRequired()
        return user

    def current_user(user: User = Depends(signed_in_user)) -> User:
        # Accounts given a temporary password by an admin must choose their own first.
        if user.must_change_password:
            raise PasswordChangeRequired()
        return user

    def get_business(bid: int, user: User = Depends(current_user), s: Session = Depends(get_db)) -> Business:
        business = s.get(Business, bid)
        if not business or (business.owner_id != user.id and not user.is_admin):
            raise HTTPException(404, "Business not found")
        return business

    def require_admin(user: User = Depends(current_user)) -> User:
        if not user.is_admin:
            raise HTTPException(403, "Only administrators can change system settings")
        return user

    # ---- helpers -------------------------------------------------------

    def flash(request: Request, message: str, level: str = "info"):
        request.session.setdefault("flash", []).append([level, message])

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(32)
        ctx.setdefault("user", None)
        messages = request.session.pop("flash", [])
        # A sign-in is reported to the visitor counter once, on the first page after it.
        count_event = request.session.pop("count_event", None) if config.GOATCOUNTER_URL else None
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], "messages": messages,
                                                          "goatcounter": config.GOATCOUNTER_URL,
                                                          "count_event": count_event, **ctx},
                                          status_code=status_code)

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def ensure_unlocked(s: Session, business: Business, d: date):
        if any(start <= d <= end for start, end in locked_ranges(s, business.id)):
            raise HTTPException(409, f"The return for the period containing {d:%d %b %Y} is finalised. "
                                     "Reopen it before changing its transactions.")

    def get_period(business: Business, key: str) -> Period:
        try:
            return parse_period(key, business.filing_frequency)
        except ValueError:
            raise HTTPException(404, "Unknown tax period")

    # ---- auth ----------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, s: Session = Depends(get_db)):
        if s.scalar(select(func.count(User.id))) == 0:
            return redirect("/register")
        return render(request, "login.html")

    @app.post("/login", dependencies=[Depends(csrf_protect)])
    def login(request: Request, email: str = Form(...), password: str = Form(...), s: Session = Depends(get_db)):
        user = s.scalar(select(User).where(User.email == email.strip().lower()))
        if not user or not verify_password(password, user.password_hash):
            flash(request, "Incorrect email or password.", "error")
            return redirect("/login")
        request.session.clear()
        request.session["uid"] = user.id
        request.session["count_event"] = "demo-sign-in" if user.email == DEMO_EMAIL else "sign-in"
        return redirect("/")

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request):
        return render(request, "register.html")

    @app.post("/register", dependencies=[Depends(csrf_protect)])
    def register(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
                 s: Session = Depends(get_db)):
        email = email.strip().lower()
        if len(password) < MIN_PASSWORD_LENGTH:
            flash(request, f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.", "error")
            return redirect("/register")
        if s.scalar(select(User).where(User.email == email)):
            flash(request, "An account with that email already exists.", "error")
            return redirect("/register")
        first = s.scalar(select(func.count(User.id))) == 0
        user = User(email=email, name=name.strip(), password_hash=hash_password(password), is_admin=first)
        s.add(user)
        s.flush()
        audit.log(s, "register", user_id=user.id, entity="user", entity_id=user.id)
        s.commit()
        request.session.clear()
        request.session["uid"] = user.id
        request.session["count_event"] = "register"
        return redirect("/")

    @app.post("/logout", dependencies=[Depends(csrf_protect)])
    def logout(request: Request):
        request.session.clear()
        return redirect("/login")

    @app.get("/account/password", response_class=HTMLResponse)
    def password_page(request: Request, user: User = Depends(signed_in_user)):
        return render(request, "password.html", user=user)

    @app.post("/account/password", dependencies=[Depends(csrf_protect)])
    def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...),
                        confirm_password: str = Form(...), user: User = Depends(signed_in_user),
                        s: Session = Depends(get_db)):
        error = None
        if not verify_password(current_password, user.password_hash):
            error = "Your current password is incorrect."
        elif len(new_password) < MIN_PASSWORD_LENGTH:
            error = f"Use a password of at least {MIN_PASSWORD_LENGTH} characters."
        elif new_password != confirm_password:
            error = "The new passwords don't match."
        elif new_password == current_password:
            error = "Choose a password different from the current one."
        if error:
            flash(request, error, "error")
            return redirect("/account/password")
        user = s.merge(user)
        user.password_hash, user.must_change_password = hash_password(new_password), False
        audit.log(s, "change_password", user_id=user.id, entity="user", entity_id=user.id)
        s.commit()
        flash(request, "Password changed.", "success")
        return redirect("/")

    # ---- businesses ----------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, user: User = Depends(current_user), s: Session = Depends(get_db)):
        q = select(Business).order_by(Business.name)
        if not user.is_admin:
            q = q.where(Business.owner_id == user.id)
        today = date.today()
        rows = [(b, deadline_for(s, b, today)) for b in s.scalars(q)]
        for b, d in rows:
            b.due_summary = summarise(s, b, d.period, today)
        if user.is_admin:
            owners = {u.id: u for u in s.scalars(select(User).where(User.id.in_({b.owner_id for b, _ in rows})))}
            for b, _ in rows:
                b.owner_label = owners[b.owner_id].email if b.owner_id in owners else ""
        return render(request, "home.html", user=user, rows=rows)

    @app.get("/businesses/new", response_class=HTMLResponse)
    def new_business_page(request: Request, user: User = Depends(current_user)):
        return render(request, "business_form.html", user=user, business=None)

    def _assign_owner(s: Session, admin: User, business: Business, owner_email: str, owner_name: str,
                      temp_password: str) -> tuple[User | None, str | None]:
        """Admin-only: give the business to the account with `owner_email`, creating it if needed.

        A temporary password creates the account, or resets an existing owner's password.
        Either way the owner must choose their own password when they next sign in.
        Returns (owner, error).
        """
        owner_email = owner_email.strip().lower()
        if not owner_email:
            return None, None
        if temp_password and len(temp_password) < MIN_PASSWORD_LENGTH:
            return None, f"The temporary password must be at least {MIN_PASSWORD_LENGTH} characters."
        owner = s.scalar(select(User).where(User.email == owner_email))
        if owner is None:
            if not temp_password:
                return None, f"No account exists for {owner_email}. Enter a temporary password to create one."
            owner = User(email=owner_email, name=owner_name.strip() or owner_email, is_admin=False,
                         password_hash=hash_password(temp_password), must_change_password=True)
            s.add(owner)
            s.flush()
            audit.log(s, "create_user", user_id=admin.id, entity="user", entity_id=owner.id, email=owner_email,
                      business_id=business.id)
        elif temp_password:
            if owner.id == admin.id:
                return None, "Change your own password from your account page."
            owner.password_hash, owner.must_change_password = hash_password(temp_password), True
            audit.log(s, "reset_password", user_id=admin.id, entity="user", entity_id=owner.id,
                      business_id=business.id)
        if business.owner_id != owner.id:
            previous = business.owner_id
            business.owner_id = owner.id
            audit.log(s, "assign_owner", user_id=admin.id, business_id=business.id, entity="business",
                      entity_id=business.id, owner=owner_email, previous_owner_id=previous)
        return owner, None

    def _business_fields(b: Business, name, bp_number, vat_number, sector, filing_frequency, reporting_currency,
                         contact_email):
        if filing_frequency not in FILING_FREQUENCIES or reporting_currency not in CURRENCIES:
            raise HTTPException(400, "Invalid filing frequency or currency")
        b.name, b.bp_number, b.vat_number = name.strip(), bp_number.strip() or None, vat_number.strip() or None
        b.sector, b.filing_frequency, b.reporting_currency = sector.strip() or None, filing_frequency, reporting_currency
        b.contact_email = contact_email.strip() or None

    @app.post("/businesses/new", dependencies=[Depends(csrf_protect)])
    def create_business(request: Request, name: str = Form(...), bp_number: str = Form(""),
                        vat_number: str = Form(""), sector: str = Form(""), filing_frequency: str = Form("monthly"),
                        reporting_currency: str = Form("USD"), contact_email: str = Form(""),
                        owner_email: str = Form(""), owner_name: str = Form(""), temp_password: str = Form(""),
                        user: User = Depends(current_user), s: Session = Depends(get_db)):
        b = Business(owner_id=user.id)
        _business_fields(b, name, bp_number, vat_number, sector, filing_frequency, reporting_currency, contact_email)
        s.add(b)
        s.flush()
        audit.log(s, "create_business", user_id=user.id, business_id=b.id, entity="business", entity_id=b.id)
        owner = None
        if user.is_admin:
            owner, error = _assign_owner(s, user, b, owner_email, owner_name, temp_password)
            if error:
                s.rollback()
                flash(request, error, "error")
                return redirect("/businesses/new")
        s.commit()
        msg = f"{b.name} created."
        if owner and owner.id != user.id:
            msg += f" {owner.email} can now sign in and see it"
            msg += " using the temporary password you set." if temp_password else "."
        flash(request, msg + " Upload its transactions to get started.", "success")
        return redirect(f"/b/{b.id}")

    @app.get("/b/{bid}/settings", response_class=HTMLResponse)
    def business_settings(request: Request, business: Business = Depends(get_business),
                          user: User = Depends(current_user), s: Session = Depends(get_db)):
        return render(request, "business_form.html", user=user, business=business,
                      owner=s.get(User, business.owner_id))

    @app.post("/b/{bid}/settings", dependencies=[Depends(csrf_protect)])
    def update_business(request: Request, name: str = Form(...), bp_number: str = Form(""),
                        vat_number: str = Form(""), sector: str = Form(""), filing_frequency: str = Form("monthly"),
                        reporting_currency: str = Form("USD"), contact_email: str = Form(""),
                        owner_email: str = Form(""), owner_name: str = Form(""), temp_password: str = Form(""),
                        business: Business = Depends(get_business), user: User = Depends(current_user),
                        s: Session = Depends(get_db)):
        business = s.merge(business)
        if user.is_admin:
            _, error = _assign_owner(s, user, business, owner_email, owner_name, temp_password)
            if error:
                s.rollback()
                flash(request, error, "error")
                return redirect(f"/b/{business.id}/settings")
        if (filing_frequency != business.filing_frequency and
                s.scalar(select(VatReturn.id).where(VatReturn.business_id == business.id))):
            flash(request, "Filing frequency can't change once a return has been finalised.", "error")
            return redirect(f"/b/{business.id}/settings")
        _business_fields(business, name, bp_number, vat_number, sector, filing_frequency, reporting_currency,
                         contact_email)
        audit.log(s, "update_business", user_id=user.id, business_id=business.id, entity="business",
                  entity_id=business.id)
        s.commit()
        flash(request, "Business details saved.", "success")
        return redirect(f"/b/{business.id}")

    @app.get("/b/{bid}", response_class=HTMLResponse)
    def dashboard(request: Request, business: Business = Depends(get_business), user: User = Depends(current_user),
                  s: Session = Depends(get_db)):
        txns = list(s.scalars(select(Transaction).where(Transaction.business_id == business.id,
                                                        Transaction.excluded.is_(False))))
        finalised = {r.period_start: r for r in s.scalars(select(VatReturn).where(
            VatReturn.business_id == business.id))}
        deadline = deadline_for(s, business, date.today())
        periods = []
        if txns:
            first, last = min(t.txn_date for t in txns), max(t.txn_date for t in txns)
            for p in reversed(periods_covering(first, last, business.filing_frequency)):
                in_p = [t for t in txns if p.start <= t.txn_date <= p.end]
                review = sum(1 for t in in_p if not t.reviewed and t.confidence < config.REVIEW_CONFIDENCE_THRESHOLD)
                periods.append({"period": p, "count": len(in_p), "review": review,
                                "sales": sum(1 for t in in_p if t.direction != PURCHASE),
                                "returned": finalised.get(p.start)})
        if not any(row["period"] == deadline.period for row in periods):
            periods.insert(0, {"period": deadline.period, "count": 0, "review": 0, "sales": 0,
                               "returned": finalised.get(deadline.period.start)})
        uploads = list(s.scalars(select(Upload).where(Upload.business_id == business.id)
                                 .order_by(Upload.created_at.desc()).limit(5)))
        registration = None if business.vat_number else rolling_turnover(s, business, date.today())
        summaries = business_summaries(s, business, date.today())
        # Earlier returns past their due date that were never filed
        overdue = [row["period"] for row in periods if row["count"] and not row["returned"]
                   and row["period"].due_date < date.today()]
        return render(request, "dashboard.html", user=user, business=business, deadline=deadline,
                      periods=periods, uploads=uploads, registration=registration, summaries=summaries,
                      overdue=overdue)

    # ---- uploads -------------------------------------------------------

    @app.get("/b/{bid}/uploads", response_class=HTMLResponse)
    def uploads_page(request: Request, business: Business = Depends(get_business), user: User = Depends(current_user),
                     s: Session = Depends(get_db)):
        uploads = list(s.scalars(select(Upload).where(Upload.business_id == business.id)
                                 .order_by(Upload.created_at.desc())))
        return render(request, "uploads.html", user=user, business=business, uploads=uploads,
                      source_types=SOURCE_TYPES)

    @app.post("/b/{bid}/uploads", dependencies=[Depends(csrf_protect)])
    async def upload_file(request: Request, file: UploadFile, source_type: str = Form("ledger"),
                          default_direction: str = Form("auto"), default_currency: str = Form("USD"),
                          amounts_include_vat: str = Form("yes"), business: Business = Depends(get_business),
                          user: User = Depends(current_user), s: Session = Depends(get_db)):
        content = await file.read()
        if len(content) > config.MAX_UPLOAD_BYTES:
            flash(request, "That file is too large.", "error")
            return redirect(f"/b/{business.id}/uploads")
        if source_type not in SOURCE_TYPES or default_direction not in ("auto", "sale", "purchase") \
                or default_currency not in CURRENCIES:
            raise HTTPException(400, "Invalid upload options")
        options = UploadOptions(source_type, default_direction, default_currency, amounts_include_vat == "yes")
        try:
            upload = import_file(s, s.merge(business), file.filename or "upload.csv", content, options,
                                 storage_dir=storage_dir, user_id=user.id)
        except IngestError as e:
            s.rollback()
            flash(request, str(e), "error")
            return redirect(f"/b/{business.id}/uploads")
        s.commit()
        if not upload.mapping:
            flash(request, "The columns in this file weren't recognised. Choose the heading row and columns "
                           "below, then re-import.", "warning")
            return redirect(f"/b/{business.id}/uploads/{upload.id}")
        msg = f"Imported {upload.rows_imported} transaction(s) from {upload.filename}."
        if upload.rows_failed:
            msg += f" {upload.rows_failed} row(s) could not be read; see below."
        flash(request, msg, "success" if not upload.rows_failed else "warning")
        return redirect(f"/b/{business.id}/uploads/{upload.id}")

    def _get_upload(s: Session, business: Business, uid: int) -> Upload:
        upload = s.get(Upload, uid)
        if not upload or upload.business_id != business.id:
            raise HTTPException(404, "Upload not found")
        return upload

    @app.get("/b/{bid}/uploads/{uid}", response_class=HTMLResponse)
    def upload_detail(request: Request, uid: int, business: Business = Depends(get_business),
                      user: User = Depends(current_user), s: Session = Depends(get_db)):
        upload = _get_upload(s, business, uid)
        try:
            preview, header_row = load_table_preview(upload)
        except (IngestError, OSError):
            preview, header_row = [], upload.header_row or 0
        counts = Counter((t.direction, t.vat_category) for t in s.scalars(
            select(Transaction).where(Transaction.upload_id == upload.id)))
        return render(request, "upload_detail.html", user=user, business=business, upload=upload,
                      preview=preview, header_row=header_row, counts=counts, source_types=SOURCE_TYPES)

    @app.post("/b/{bid}/uploads/{uid}/reprocess", dependencies=[Depends(csrf_protect)])
    async def reprocess(request: Request, uid: int, business: Business = Depends(get_business),
                        user: User = Depends(current_user), s: Session = Depends(get_db)):
        upload = _get_upload(s, business, uid)
        form = await request.form()
        mapping = {}
        for fld in FIELD_LABELS:
            v = form.get(f"map_{fld}", "")
            if v != "":
                mapping[fld] = int(v)
        try:
            header_row = int(form.get("header_row", "1")) - 1
            if header_row != upload.header_row:
                mapping = {}  # a new heading row means the columns must be detected again
            options = UploadOptions(form.get("source_type", upload.source_type),
                                    form.get("default_direction", upload.default_direction),
                                    form.get("default_currency", upload.default_currency),
                                    form.get("amounts_include_vat", "yes") == "yes")
            reprocess_upload(s, s.merge(business), upload, options, header_row, mapping, user_id=user.id)
        except (IngestError, ValueError) as e:
            # Failures happen before any transaction is replaced; keep the new heading row so the
            # mapping form shows that row's column names.
            s.commit()
            flash(request, str(e), "error")
            return redirect(f"/b/{business.id}/uploads/{uid}")
        s.commit()
        flash(request, f"Re-imported: {upload.rows_imported} transaction(s), {upload.rows_failed} unreadable row(s). "
                       "Earlier manual classifications for this file were replaced.", "success")
        return redirect(f"/b/{business.id}/uploads/{uid}")

    @app.post("/b/{bid}/uploads/{uid}/delete", dependencies=[Depends(csrf_protect)])
    def delete_upload(request: Request, uid: int, business: Business = Depends(get_business),
                      user: User = Depends(current_user), s: Session = Depends(get_db)):
        upload = _get_upload(s, business, uid)
        txns = list(s.scalars(select(Transaction).where(Transaction.upload_id == upload.id)))
        locked = locked_ranges(s, business.id)
        if any(a <= t.txn_date <= b for t in txns for a, b in locked):
            flash(request, "Rows from this upload are in a finalised return. Reopen it first.", "error")
            return redirect(f"/b/{business.id}/uploads/{uid}")
        for t in txns:
            s.delete(t)
        s.delete(upload)
        audit.log(s, "delete_upload", user_id=user.id, business_id=business.id, entity="upload", entity_id=uid,
                  filename=upload.filename, transactions=len(txns))
        s.commit()
        try:
            if not s.scalar(select(Upload.id).where(Upload.stored_path == upload.stored_path)):
                Path(upload.stored_path).unlink(missing_ok=True)
        except OSError:
            pass
        flash(request, f"Deleted {upload.filename} and its {len(txns)} transaction(s).", "success")
        return redirect(f"/b/{business.id}/uploads")

    # ---- transactions --------------------------------------------------

    @app.get("/b/{bid}/transactions", response_class=HTMLResponse)
    def transactions_page(request: Request, period: str = "", direction: str = "", category: str = "",
                          status: str = "", q: str = "", ids: str = "", upload: str = "", page: int = 1,
                          business: Business = Depends(get_business), user: User = Depends(current_user),
                          s: Session = Depends(get_db)):
        stmt = select(Transaction).where(Transaction.business_id == business.id)
        p = get_period(business, period) if period else None
        if p:
            stmt = stmt.where(Transaction.txn_date >= p.start, Transaction.txn_date <= p.end)
        if direction in DIRECTIONS:
            stmt = stmt.where(Transaction.direction == direction)
        if category in VAT_CATEGORIES:
            stmt = stmt.where(Transaction.vat_category == category)
        if upload.isdigit():
            stmt = stmt.where(Transaction.upload_id == int(upload))
        if status == "review":
            stmt = stmt.where(Transaction.reviewed.is_(False),
                              Transaction.confidence < config.REVIEW_CONFIDENCE_THRESHOLD)
        elif status == "excluded":
            stmt = stmt.where(Transaction.excluded.is_(True))
        if q:
            like = f"%{q}%"
            stmt = stmt.where(Transaction.description.ilike(like) | Transaction.counterparty.ilike(like)
                              | Transaction.invoice_number.ilike(like))
        id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
        if id_list:
            stmt = stmt.where(Transaction.id.in_(id_list))
        page = max(page, 1)
        txns = list(s.scalars(stmt.order_by(Transaction.txn_date, Transaction.id)
                              .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)))
        more = len(txns) > PAGE_SIZE
        txns = txns[:PAGE_SIZE]
        locked = locked_ranges(s, business.id)
        locked_ids = {t.id for t in txns if any(a <= t.txn_date <= b for a, b in locked)}
        filters = {"period": period, "direction": direction, "category": category, "status": status, "q": q,
                   "ids": ids, "upload": upload}
        query_without_page = urlencode({k: v for k, v in filters.items() if v})
        return render(request, "transactions.html", user=user, business=business, txns=txns, filters=filters,
                      page=page, more=more, query_without_page=query_without_page, period_obj=p, locked_ids=locked_ids, threshold=config.REVIEW_CONFIDENCE_THRESHOLD)

    @app.post("/b/{bid}/transactions/save", dependencies=[Depends(csrf_protect)])
    async def save_transactions(request: Request, business: Business = Depends(get_business),
                                user: User = Depends(current_user), s: Session = Depends(get_db)):
        form = await request.form()
        ids = [int(i) for i in form.getlist("row_id")]
        bulk_category = form.get("bulk_category", "")
        bulk_input = form.get("bulk_input_type", "")
        selected = {int(i) for i in form.getlist("selected")}
        mark_reviewed = form.get("action") == "save_review"
        changed = 0
        locked = locked_ranges(s, business.id)
        for t in s.scalars(select(Transaction).where(Transaction.business_id == business.id,
                                                     Transaction.id.in_(ids))):
            new_cat = form.get(f"cat_{t.id}", t.vat_category)
            new_input = form.get(f"input_{t.id}", t.input_type)
            new_excluded = form.get(f"excl_{t.id}") == "1"
            if t.id in selected:
                new_cat = bulk_category or new_cat
                new_input = bulk_input or new_input
            if new_cat not in VAT_CATEGORIES or new_input not in INPUT_TYPES:
                continue
            classification_changed = (new_cat, new_input) != (t.vat_category, t.input_type)
            review_now = mark_reviewed and (t.id in selected or not selected) and not t.reviewed
            if not (classification_changed or new_excluded != t.excluded or review_now):
                continue
            if any(a <= t.txn_date <= b for a, b in locked):
                continue
            before = {"category": t.vat_category, "input_type": t.input_type, "excluded": t.excluded}
            t.vat_category, t.input_type, t.excluded = new_cat, new_input, new_excluded
            if classification_changed:
                t.classification_source = "user"
                t.classification_reason = f"Set by {user.name}"
                t.confidence = 1.0
                t.rule_id = None
            if classification_changed or review_now:
                t.reviewed = True
            audit.log(s, "edit_transaction", user_id=user.id, business_id=business.id, entity="transaction",
                      entity_id=t.id, before=before,
                      after={"category": new_cat, "input_type": new_input, "excluded": new_excluded,
                             "reviewed": t.reviewed})
            changed += 1
        s.commit()
        flash(request, f"Saved {changed} transaction(s).", "success")
        return redirect(form.get("return_to") or f"/b/{business.id}/transactions")

    @app.post("/b/{bid}/transactions/new", dependencies=[Depends(csrf_protect)])
    def add_transaction(request: Request, txn_date: str = Form(...), direction: str = Form(...),
                        description: str = Form(...), counterparty: str = Form(""), invoice_number: str = Form(""),
                        counterparty_vat_number: str = Form(""), amount: str = Form(...),
                        currency: str = Form("USD"), amount_includes_vat: str = Form("yes"),
                        business: Business = Depends(get_business), user: User = Depends(current_user),
                        s: Session = Depends(get_db)):
        d = parse_date(txn_date)
        value, _ = parse_amount(amount)
        if d is None or value is None or direction not in DIRECTIONS or currency not in CURRENCIES:
            flash(request, "Enter a valid date, amount, direction and currency.", "error")
            return redirect(f"/b/{business.id}/transactions")
        ensure_unlocked(s, business, d)
        c = classifier_for(s, business.id).classify(direction=direction, description=description,
                                                    counterparty=counterparty, on=d)
        t = Transaction(business_id=business.id, txn_date=d, description=description.strip(),
                        counterparty=counterparty.strip() or None, invoice_number=invoice_number.strip() or None,
                        counterparty_vat_number=counterparty_vat_number.strip() or None, direction=direction,
                        amount=value, currency=currency, amount_includes_vat=amount_includes_vat == "yes",
                        vat_category=c.vat_category, input_type=c.input_type, classification_source=c.source,
                        classification_reason=c.reason, rule_id=c.rule_id,
                        reference_item_id=c.reference_item_id, confidence=c.confidence,
                        raw={"entered_by": user.email})
        s.add(t)
        s.flush()
        audit.log(s, "add_transaction", user_id=user.id, business_id=business.id, entity="transaction",
                  entity_id=t.id, amount=str(value), currency=currency)
        s.commit()
        flash(request, "Transaction added.", "success")
        return redirect(f"/b/{business.id}/transactions/{t.id}")

    @app.get("/b/{bid}/transactions/{tid}", response_class=HTMLResponse)
    def transaction_detail(request: Request, tid: int, business: Business = Depends(get_business),
                           user: User = Depends(current_user), s: Session = Depends(get_db)):
        t = s.get(Transaction, tid)
        if not t or t.business_id != business.id:
            raise HTTPException(404, "Transaction not found")
        period = period_for(t.txn_date, business.filing_frequency)
        result = compute_return(s, business, period)
        calc = result.calcs.get(t.id)
        lines = [code for code, line in result.lines.items() if t.id in line.txn_ids
                 and code not in ("TOTAL_SUPPLIES", "TOTAL_INPUT", "NET_VAT")]
        history = list(s.scalars(select(AuditLog).where(AuditLog.entity == "transaction", AuditLog.entity_id == t.id)
                                 .order_by(AuditLog.at)))
        rule = s.get(ClassificationRule, t.rule_id) if t.rule_id else None
        ref_item = s.get(ReferenceItem, t.reference_item_id) if t.reference_item_id else None
        return render(request, "transaction_detail.html", user=user, business=business, t=t, calc=calc,
                      period=period, lines=lines, history=history, rule=rule, ref_item=ref_item)

    @app.get("/b/{bid}/review", response_class=HTMLResponse)
    def review_page(request: Request, business: Business = Depends(get_business), user: User = Depends(current_user),
                    s: Session = Depends(get_db)):
        groups = pending_groups(s, business, locked_ranges(s, business.id))
        return render(request, "review.html", user=user, business=business, groups=groups,
                      total=sum(len(g.txns) for g in groups))

    @app.post("/b/{bid}/review", dependencies=[Depends(csrf_protect)])
    def answer_question(request: Request, group: str = Form(...), answer: str = Form(...), remember: str = Form(""),
                        business: Business = Depends(get_business), user: User = Depends(current_user),
                        s: Session = Depends(get_db)):
        match = next((g for g in pending_groups(s, business, locked_ranges(s, business.id)) if g.token == group), None)
        if match is None:
            flash(request, "Those transactions have already been answered or changed.", "info")
            return redirect(f"/b/{business.id}/review")
        try:
            n = apply_answer(s, business, user, match, answer, remember == "yes")
        except ValueError:
            flash(request, "Choose one of the answers.", "error")
            return redirect(f"/b/{business.id}/review")
        s.commit()
        flash(request, f"Saved for {n} transaction{'s' if n != 1 else ''} from {match.title}"
              + (", and for similar ones in future." if remember == "yes" else "."), "success")
        return redirect(f"/b/{business.id}/review")

    @app.post("/b/{bid}/reclassify", dependencies=[Depends(csrf_protect)])
    def reclassify(request: Request, business: Business = Depends(get_business), user: User = Depends(current_user),
                   s: Session = Depends(get_db)):
        classifier = classifier_for(s, business.id)
        locked = locked_ranges(s, business.id)
        changed = 0
        for t in s.scalars(select(Transaction).where(Transaction.business_id == business.id,
                                                     Transaction.reviewed.is_(False),
                                                     Transaction.classification_source.in_(
                                                         ["rule", "reference", "default"]))):
            if any(a <= t.txn_date <= b for a, b in locked):
                continue
            c = classifier.classify(direction=t.direction, description=t.description, counterparty=t.counterparty,
                                    on=t.txn_date)
            if (c.vat_category, c.input_type, c.rule_id, c.reference_item_id) != \
                    (t.vat_category, t.input_type, t.rule_id, t.reference_item_id):
                t.vat_category, t.input_type, t.rule_id = c.vat_category, c.input_type, c.rule_id
                t.reference_item_id = c.reference_item_id
                t.classification_source, t.classification_reason, t.confidence = c.source, c.reason, c.confidence
                changed += 1
        audit.log(s, "reclassify", user_id=user.id, business_id=business.id, changed=changed)
        s.commit()
        flash(request, f"Re-applied rules: {changed} unreviewed transaction(s) changed.", "success")
        return redirect(request.headers.get("referer") or f"/b/{business.id}")

    # ---- returns -------------------------------------------------------

    def _return_context(s: Session, business: Business, period: Period):
        result = compute_return(s, business, period)
        issues = run_checks(s, result, date.today())
        saved = s.scalar(select(VatReturn).where(VatReturn.business_id == business.id,
                                                 VatReturn.period_start == period.start))
        drift = []
        if saved and saved.status == "finalised":
            for code, line in result.lines.items():
                snap = saved.snapshot["lines"].get(code)
                if snap and Decimal(snap["total"]) != line.total:
                    drift.append(code)
        return result, issues, saved, drift

    @app.get("/b/{bid}/returns/{pkey}", response_class=HTMLResponse)
    def return_page(request: Request, pkey: str, business: Business = Depends(get_business),
                    user: User = Depends(current_user), s: Session = Depends(get_db)):
        period = get_period(business, pkey)
        result, issues, saved, drift = _return_context(s, business, period)
        return render(request, "return.html", user=user, business=business, period=period, result=result,
                      issues=issues, saved=saved, drift=drift, today=date.today())

    @app.get("/b/{bid}/returns/{pkey}/lines/{code}", response_class=HTMLResponse)
    def return_line(request: Request, pkey: str, code: str, business: Business = Depends(get_business),
                    user: User = Depends(current_user), s: Session = Depends(get_db)):
        period = get_period(business, pkey)
        result = compute_return(s, business, period)
        if code not in result.lines:
            raise HTTPException(404, "Unknown return line")
        line = result.lines[code]
        calcs = [result.calcs[i] for i in line.txn_ids]
        return render(request, "line.html", user=user, business=business, period=period, result=result,
                      line=line, calcs=calcs)

    @app.get("/b/{bid}/returns/{pkey}/export.{ext}")
    def export_return(pkey: str, ext: str, business: Business = Depends(get_business), s: Session = Depends(get_db)):
        period = get_period(business, pkey)
        result = compute_return(s, business, period)
        safe = re.sub(r"[^A-Za-z0-9]+", "_", business.name).strip("_")
        name = f"VAT_{safe}_{period.start:%Y-%m}_{period.end:%Y-%m}"
        if ext == "xlsx":
            data = return_to_xlsx(result, business, run_checks(s, result, date.today()))
            return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
        if ext == "csv":
            return Response(return_to_csv(result), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
        raise HTTPException(404)

    @app.post("/b/{bid}/returns/{pkey}/finalise", dependencies=[Depends(csrf_protect)])
    def finalise_return(request: Request, pkey: str, business: Business = Depends(get_business),
                        user: User = Depends(current_user), s: Session = Depends(get_db)):
        period = get_period(business, pkey)
        result, issues, saved, _ = _return_context(s, business, period)
        if any(i.severity == "error" for i in issues):
            flash(request, "Fix the errors listed before finalising.", "error")
            return redirect(f"/b/{business.id}/returns/{pkey}")
        if saved:
            s.delete(saved)
            s.flush()
        s.add(VatReturn(business_id=business.id, period_start=period.start, period_end=period.end,
                        status="finalised", snapshot=result.to_snapshot(), finalised_by=user.id))
        audit.log(s, "finalise_return", user_id=user.id, business_id=business.id, entity="return",
                  period=period.key, net_vat=str(result.line("NET_VAT").total),
                  currency=result.reporting_currency)
        s.commit()
        flash(request, f"Return for {period.label} finalised. Its transactions are now locked.", "success")
        return redirect(f"/b/{business.id}/returns/{pkey}")

    @app.post("/b/{bid}/returns/{pkey}/reopen", dependencies=[Depends(csrf_protect)])
    def reopen_return(request: Request, pkey: str, reason: str = Form(""), business: Business = Depends(get_business),
                      user: User = Depends(current_user), s: Session = Depends(get_db)):
        period = get_period(business, pkey)
        saved = s.scalar(select(VatReturn).where(VatReturn.business_id == business.id,
                                                 VatReturn.period_start == period.start))
        if saved:
            audit.log(s, "reopen_return", user_id=user.id, business_id=business.id, entity="return",
                      period=period.key, reason=reason, previous_snapshot=saved.snapshot["lines"])
            s.delete(saved)
            s.commit()
            flash(request, "Return reopened. The finalised figures are kept in the audit log.", "success")
        return redirect(f"/b/{business.id}/returns/{pkey}")

    @app.get("/b/{bid}/audit", response_class=HTMLResponse)
    def audit_page(request: Request, business: Business = Depends(get_business), user: User = Depends(current_user),
                   s: Session = Depends(get_db)):
        entries = list(s.scalars(select(AuditLog).where(AuditLog.business_id == business.id)
                                 .order_by(AuditLog.at.desc()).limit(500)))
        return render(request, "audit.html", user=user, business=business, entries=entries)

    # ---- settings: reference lists -------------------------------------

    @app.get("/settings/reference", response_class=HTMLResponse)
    def reference_page(request: Request, q: str = "", source: str = "", test: str = "", test_date: str = "",
                       user: User = Depends(current_user), s: Session = Depends(get_db)):
        query = select(ReferenceItem).order_by(ReferenceItem.source, ReferenceItem.item)
        if source:
            query = query.where(ReferenceItem.source == source)
        if q:
            like = f"%{q.strip()}%"
            query = query.where(ReferenceItem.item.ilike(like) | ReferenceItem.keywords.ilike(like))
        items = list(s.scalars(query.limit(1000)))
        sources = list(s.execute(select(ReferenceItem.source, func.count(ReferenceItem.id))
                                 .group_by(ReferenceItem.source).order_by(ReferenceItem.source)))
        tested = None
        if test.strip():
            on = parse_date(test_date) or date.today()
            # Shows what the lists and global rules would decide, as for a business with no own rules.
            c = classifier_for(s, -1).classify(direction="sale", description=test, counterparty=None, on=on)
            tested = {"text": test, "on": on, "result": c}
        return render(request, "reference.html", user=user, items=items, sources=sources, q=q, source=source,
                      tested=tested, test_date=test_date)

    @app.get("/settings/reference/template.xlsx")
    def reference_template(user: User = Depends(current_user)):
        return Response(template_xlsx(),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="reference_list_template.xlsx"'})

    @app.post("/settings/reference/import", dependencies=[Depends(csrf_protect)])
    async def import_reference(request: Request, file: UploadFile, default_source: str = Form(""),
                               replace_source: str = Form(""), user: User = Depends(require_admin),
                               s: Session = Depends(get_db)):
        content = await file.read()
        try:
            result = import_items(s, file.filename or "list.csv", content, default_source.strip(),
                                  replace_source == "yes")
        except IngestError as e:
            flash(request, str(e), "error")
            return redirect("/settings/reference")
        if result.errors:
            s.rollback()
            more = f" (and {len(result.errors) - 10} more)" if len(result.errors) > 10 else ""
            flash(request, "Nothing imported. Fix these rows and try again: " + " · ".join(result.errors[:10]) + more,
                  "error")
            return redirect("/settings/reference")
        audit.log(s, "import_reference_list", user_id=user.id, entity="reference_list", filename=file.filename,
                  imported=result.imported, replaced=result.replaced)
        s.commit()
        flash(request, f"Imported {result.imported} item(s)" + (f", replacing {result.replaced}" if result.replaced
                                                               else "") +
              ". Use “Re-apply rules” on each business to update unreviewed transactions.", "success")
        return redirect("/settings/reference")

    @app.post("/settings/reference/{iid}/{action}", dependencies=[Depends(csrf_protect)])
    def change_reference_item(request: Request, iid: int, action: str, user: User = Depends(require_admin),
                              s: Session = Depends(get_db)):
        item = s.get(ReferenceItem, iid)
        if not item:
            raise HTTPException(404, "Item not found")
        if action == "toggle":
            item.active = not item.active
        elif action == "delete":
            s.execute(Transaction.__table__.update().where(Transaction.reference_item_id == iid)
                      .values(reference_item_id=None))
            s.delete(item)
        else:
            raise HTTPException(404)
        audit.log(s, f"{action}_reference_item", user_id=user.id, entity="reference_item", entity_id=iid,
                  item=item.item, source=item.source)
        s.commit()
        return redirect(request.headers.get("referer") or "/settings/reference")

    # ---- settings: rules -----------------------------------------------

    @app.get("/settings/rules", response_class=HTMLResponse)
    def rules_page(request: Request, business: int | None = None, pattern: str = "", category: str = "",
                   direction: str = "any", user: User = Depends(current_user), s: Session = Depends(get_db)):
        biz = get_business(business, user, s) if business else None
        rules = list(s.scalars(select(ClassificationRule).where(
            ClassificationRule.business_id.is_(None) | (ClassificationRule.business_id == (biz.id if biz else -1)))
            .order_by(ClassificationRule.business_id.is_(None), ClassificationRule.priority, ClassificationRule.id)))
        return render(request, "rules.html", user=user, business=biz, rules=rules,
                      prefill={"pattern": pattern, "category": category, "direction": direction})

    @app.post("/settings/rules", dependencies=[Depends(csrf_protect)])
    def add_rule(request: Request, pattern: str = Form(...), match_type: str = Form("contains"),
                 field: str = Form("any"), direction: str = Form("any"), vat_category: str = Form(...),
                 input_type: str = Form(""), priority: int = Form(100), note: str = Form(""),
                 effective_from: str = Form(""), effective_to: str = Form(""),
                 business_id: str = Form(""), user: User = Depends(current_user), s: Session = Depends(get_db)):
        biz = get_business(int(business_id), user, s) if business_id else None
        back = f"/settings/rules?business={biz.id}" if biz else "/settings/rules"
        if not biz and not user.is_admin:
            raise HTTPException(403, "Only administrators can add rules for all businesses")
        error = validate_rule_pattern(match_type, pattern)
        try:
            start = date.fromisoformat(effective_from) if effective_from else None
            end = date.fromisoformat(effective_to) if effective_to else None
        except ValueError:
            start = end = None
            error = "Enter rule dates as YYYY-MM-DD."
        if not error and start and end and end < start:
            error = "The rule's end date is before its start date."
        if error or vat_category not in VAT_CATEGORIES or match_type not in ("contains", "equals", "regex") \
                or field not in ("any", "description", "counterparty") or direction not in ("any", "sale", "purchase"):
            flash(request, error or "Invalid rule.", "error")
            return redirect(back)
        rule = ClassificationRule(business_id=biz.id if biz else None, pattern=pattern.strip(), match_type=match_type,
                                  field=field, direction=direction, vat_category=vat_category,
                                  input_type=input_type if input_type in INPUT_TYPES else None,
                                  priority=priority, note=note.strip() or None,
                                  effective_from=start, effective_to=end)
        s.add(rule)
        s.flush()
        audit.log(s, "add_rule", user_id=user.id, business_id=rule.business_id, entity="rule", entity_id=rule.id,
                  pattern=rule.pattern, category=vat_category)
        s.commit()
        flash(request, "Rule added. Use “Re-apply rules” on the business to update unreviewed transactions.",
              "success")
        return redirect(back)

    @app.post("/settings/rules/{rid}/{action}", dependencies=[Depends(csrf_protect)])
    def change_rule(request: Request, rid: int, action: str, user: User = Depends(current_user),
                    s: Session = Depends(get_db)):
        rule = s.get(ClassificationRule, rid)
        if not rule:
            raise HTTPException(404)
        if rule.business_id is None and not user.is_admin:
            raise HTTPException(403, "Only administrators can change global rules")
        if rule.business_id is not None:
            get_business(rule.business_id, user, s)
        back = f"/settings/rules?business={rule.business_id}" if rule.business_id else "/settings/rules"
        if action == "toggle":
            rule.active = not rule.active
        elif action == "delete":
            if s.scalar(select(Transaction.id).where(Transaction.rule_id == rid).limit(1)):
                rule.active = False
                flash(request, "Rule is used by existing transactions, so it was deactivated instead.", "info")
            else:
                s.delete(rule)
        else:
            raise HTTPException(404)
        audit.log(s, f"{action}_rule", user_id=user.id, business_id=rule.business_id, entity="rule", entity_id=rid)
        s.commit()
        return redirect(back)

    # ---- settings: VAT rates -------------------------------------------

    @app.get("/settings/rates", response_class=HTMLResponse)
    def rates_page(request: Request, user: User = Depends(current_user), s: Session = Depends(get_db)):
        rates = list(s.scalars(select(VatRate).order_by(VatRate.category, VatRate.effective_from)))
        return render(request, "rates.html", user=user, rates=rates)

    @app.post("/settings/rates", dependencies=[Depends(csrf_protect)])
    def add_rate(request: Request, category: str = Form(...), rate: str = Form(...),
                 effective_from: str = Form(...), note: str = Form(""), user: User = Depends(require_admin),
                 s: Session = Depends(get_db)):
        try:
            value, start = Decimal(rate), date.fromisoformat(effective_from)
        except (InvalidOperation, ValueError):
            flash(request, "Enter a valid rate and date.", "error")
            return redirect("/settings/rates")
        if category not in ("standard", "zero") or not (0 <= value < 100):
            flash(request, "Rates apply to standard or zero-rated categories, between 0 and 100.", "error")
            return redirect("/settings/rates")
        current = [r for r in s.scalars(select(VatRate).where(VatRate.category == category))
                   if r.effective_to is None or r.effective_to >= start]
        for r in current:
            if r.effective_from >= start:
                flash(request, "A rate already starts on or after that date. Delete it first.", "error")
                return redirect("/settings/rates")
            r.effective_to = date.fromordinal(start.toordinal() - 1)
        new = VatRate(category=category, rate=value, effective_from=start, note=note.strip() or None)
        s.add(new)
        s.flush()
        audit.log(s, "add_vat_rate", user_id=user.id, entity="vat_rate", entity_id=new.id, category=category,
                  rate=str(value), effective_from=start.isoformat())
        s.commit()
        flash(request, f"New {category} rate of {value}% applies from {start:%d %b %Y}.", "success")
        return redirect("/settings/rates")

    @app.post("/settings/rates/{rid}/delete", dependencies=[Depends(csrf_protect)])
    def delete_rate(request: Request, rid: int, user: User = Depends(require_admin), s: Session = Depends(get_db)):
        r = s.get(VatRate, rid)
        if r:
            prev = s.scalar(select(VatRate).where(VatRate.category == r.category,
                                                  VatRate.effective_to == date.fromordinal(r.effective_from.toordinal() - 1)))
            if prev and r.effective_to is None:
                prev.effective_to = None
            audit.log(s, "delete_vat_rate", user_id=user.id, entity="vat_rate", entity_id=rid, rate=str(r.rate),
                      category=r.category)
            s.delete(r)
            s.commit()
        return redirect("/settings/rates")

    # ---- settings: exchange rates --------------------------------------

    @app.get("/settings/fx", response_class=HTMLResponse)
    def fx_page(request: Request, user: User = Depends(current_user), s: Session = Depends(get_db)):
        rates = list(s.scalars(select(ExchangeRate).order_by(ExchangeRate.rate_date.desc()).limit(400)))
        return render(request, "fx.html", user=user, rates=rates)

    def _upsert_fx(s: Session, d: date, base: str, quote: str, rate: Decimal, source: str | None):
        existing = s.scalar(select(ExchangeRate).where(ExchangeRate.rate_date == d, ExchangeRate.base == base,
                                                       ExchangeRate.quote == quote))
        if existing:
            existing.rate, existing.source = rate, source
        else:
            s.add(ExchangeRate(rate_date=d, base=base, quote=quote, rate=rate, source=source))

    @app.post("/settings/fx", dependencies=[Depends(csrf_protect)])
    def add_fx(request: Request, rate_date: str = Form(...), rate: str = Form(...), source: str = Form("RBZ"),
               user: User = Depends(require_admin), s: Session = Depends(get_db)):
        try:
            d, value = date.fromisoformat(rate_date), Decimal(rate)
            if value <= 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            flash(request, "Enter a valid date and a positive rate.", "error")
            return redirect("/settings/fx")
        _upsert_fx(s, d, "USD", "ZWG", value, source.strip() or None)
        audit.log(s, "set_fx_rate", user_id=user.id, entity="fx", date=d.isoformat(), rate=str(value))
        s.commit()
        flash(request, f"Rate for {d:%d %b %Y} saved.", "success")
        return redirect("/settings/fx")

    @app.post("/settings/fx/import", dependencies=[Depends(csrf_protect)])
    async def import_fx(request: Request, file: UploadFile, user: User = Depends(require_admin),
                        s: Session = Depends(get_db)):
        text = (await file.read()).decode("utf-8-sig", errors="replace")
        added, bad = 0, 0
        for row in csv.DictReader(io.StringIO(text)):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            d = parse_date(row.get("date") or row.get("rate date"))
            value, _ = parse_amount(row.get("rate") or row.get("mid rate") or row.get("mid"))
            if d is None or value is None or value <= 0:
                bad += 1
                continue
            base = (row.get("base") or "USD").upper()
            quote = (row.get("quote") or "ZWG").upper().replace("ZIG", "ZWG")
            _upsert_fx(s, d, base, quote, value, row.get("source") or file.filename)
            added += 1
        audit.log(s, "import_fx_rates", user_id=user.id, entity="fx", filename=file.filename, rows=added,
                  rejected=bad)
        s.commit()
        flash(request, f"Imported {added} rate(s)." + (f" {bad} row(s) skipped." if bad else ""),
              "success" if not bad else "warning")
        return redirect("/settings/fx")

    @app.post("/settings/fx/{rid}/delete", dependencies=[Depends(csrf_protect)])
    def delete_fx(rid: int, user: User = Depends(require_admin), s: Session = Depends(get_db)):
        r = s.get(ExchangeRate, rid)
        if r:
            audit.log(s, "delete_fx_rate", user_id=user.id, entity="fx", date=r.rate_date.isoformat(),
                      rate=str(r.rate))
            s.delete(r)
            s.commit()
        return redirect("/settings/fx")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        uid = request.session.get("uid") if "session" in request.scope else None
        with db.session() as s:
            user = s.get(User, uid) if uid else None
            return render(request, "error.html", status_code=exc.status_code, user=user, status=exc.status_code,
                          detail=exc.detail)

    @app.exception_handler(IntegrityError)
    async def _integrity_error(request: Request, exc: IntegrityError):
        return render(request, "error.html", status_code=409, user=None, status=409,
                      detail="That change conflicts with existing data.")

    return app
