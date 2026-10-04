"""Checks run before filing: missing, doubtful or unusual data, and VAT rules
that need the user's attention (section numbers refer to the VAT Act)."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import median

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import config
from .compute import FULL_CLAIM_THRESHOLD, ReturnResult, totals_by_month
from .models import (
    EXEMPT,
    INPUT_GENERAL,
    INPUT_IMPORT,
    INPUT_IMPORTED_SERVICE,
    INPUT_MIXED,
    INPUT_NON_DEDUCTIBLE,
    PURCHASE,
    SALE,
    STANDARD,
    ZERO_RATED,
    Transaction,
    Upload,
    VatRate,
    VatReturn,
)
from .money import fmt
from .penalties import estimate

ERROR, WARNING, INFO = "error", "warning", "info"
VAT_TOLERANCE = Decimal("0.05")


@dataclass
class Issue:
    severity: str
    code: str
    title: str
    detail: str = ""
    txn_ids: list[int] = field(default_factory=list)


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def run_checks(session: Session, result: ReturnResult, today: date | None = None) -> list[Issue]:
    issues: list[Issue] = []
    calcs = list(result.calcs.values())
    txns = [c.txn for c in calcs]
    p = result.period

    if not txns:
        issues.append(Issue(ERROR, "no_data", "No transactions in this period",
                            "Upload sales and purchases for this period before preparing the return. "
                            "A return is still required when nothing is due (s28(2))."))
        issues += _late_filing(session, result, today)
        return issues

    failed = [c for c in calcs if c.error]
    if failed:
        reasons = sorted({c.error for c in failed})
        issues.append(Issue(ERROR, "calc_error", f"{len(failed)} transaction(s) could not be computed",
                            "; ".join(reasons[:3]) + (" …" if len(reasons) > 3 else ""),
                            [c.txn.id for c in failed]))

    review = [t for t in txns if not t.reviewed and t.confidence < config.REVIEW_CONFIDENCE_THRESHOLD]
    if review:
        issues.append(Issue(WARNING, "needs_review", f"{len(review)} classification(s) need review",
                            "No rule matched these transactions, so they were assumed standard-rated.",
                            [t.id for t in review]))

    claims = [t for t in txns if t.direction == PURCHASE and t.vat_category == STANDARD
              and t.input_type != INPUT_NON_DEDUCTIBLE]
    # Imports are supported by a bill of entry and imported services by the foreign
    # supplier's invoice, neither of which carries a local VAT number (s15(2)).
    no_invoice = [t for t in claims if not t.invoice_number or
                  (t.input_type not in (INPUT_IMPORT, INPUT_IMPORTED_SERVICE) and not t.counterparty_vat_number)]
    if no_invoice:
        issues.append(Issue(WARNING, "missing_tax_invoice",
                            f"{len(no_invoice)} input tax claim(s) lack an invoice number or supplier VAT number",
                            "Input tax can only be claimed with a valid fiscal tax invoice (or a bill of entry for "
                            "imports) held when the return is submitted (s15(2), s20). Check you hold one for each.",
                            [t.id for t in no_invoice]))

    mismatched = [c.txn.id for c in calcs if not c.error and c.txn.stated_vat is not None
                  and c.txn.vat_category == STANDARD and c.txn.input_type != INPUT_IMPORT
                  and abs(c.txn.stated_vat - c.vat) > VAT_TOLERANCE]
    if mismatched:
        issues.append(Issue(WARNING, "vat_mismatch", f"{len(mismatched)} transaction(s) where the VAT in the file "
                            "differs from the computed VAT",
                            "The invoice may use an old rate, or the item may be classified wrongly.", mismatched))

    groups = defaultdict(list)
    for t in txns:
        key = (t.direction, t.txn_date, t.amount, t.currency, _norm(t.invoice_number) or _norm(t.description))
        groups[key].append(t.id)
    dupes = [ids for ids in groups.values() if len(ids) > 1]
    if dupes:
        issues.append(Issue(WARNING, "duplicates", f"{len(dupes)} possible duplicate group(s)",
                            "Same date, amount and invoice or description. Exclude any that are double-counted.",
                            [i for ids in dupes for i in ids]))

    issues += _claimed_elsewhere(session, result, claims)

    by_group = defaultdict(list)
    for t in txns:
        by_group[(t.direction, t.currency)].append(t)
    unusual = []
    for group in by_group.values():
        if len(group) < 10:
            continue
        mid = median(abs(t.amount) for t in group)
        unusual += [t.id for t in group if mid > 0 and abs(t.amount) > mid * 10]
    if unusual:
        issues.append(Issue(INFO, "unusual_amount", f"{len(unusual)} unusually large amount(s)",
                            "More than ten times the typical amount for this period. Confirm they are correct.",
                            unusual))

    months = totals_by_month(result)
    month_keys = sorted({f"{p.start:%Y-%m}", f"{p.end:%Y-%m}"})
    for key in month_keys:
        counts = months.get(key, {SALE: 0, PURCHASE: 0})
        missing = [name for name, d in (("sales", SALE), ("purchases", PURCHASE)) if counts[d] == 0]
        if missing:
            issues.append(Issue(WARNING, "missing_month", f"No {' or '.join(missing)} recorded for {key}",
                                "Data for this month may not have been uploaded yet."))

    issues += _apportionment(result, txns)

    imported = [t.id for t in txns if t.direction == PURCHASE and t.input_type == INPUT_IMPORTED_SERVICE
                and t.vat_category == STANDARD]
    if imported:
        issues.append(Issue(INFO, "imported_services", f"VAT self-accounted on {len(imported)} imported service(s)",
                            "Declare and pay it by the 25th of the month after the supply, in the currency of "
                            "trade (s13). It does not apply where the service would be zero-rated or exempt "
                            "locally, or where Digital Services Withholding Tax was deducted.", imported))

    zero_rated = [t.id for t in txns if t.direction == SALE and t.vat_category == ZERO_RATED]
    if zero_rated:
        issues.append(Issue(INFO, "zero_rating_proof", f"{len(zero_rated)} zero-rated sale(s)",
                            "Keep the export or other documents ZIMRA accepts as proof for each; without them "
                            "the sale is taxed at the standard rate (s10(3), s37).", zero_rated))

    changes = session.scalars(select(VatRate).where(VatRate.category == STANDARD, VatRate.effective_from > p.start,
                                                    VatRate.effective_from <= p.end)).all()
    for r in changes:
        issues.append(Issue(INFO, "rate_change", f"The standard rate changes to {r.rate.normalize():f}% on "
                            f"{r.effective_from:%d %b %Y}",
                            "Each transaction uses the rate in force on its date. Supplies that span the change "
                            "(for example services performed partly before and partly after it) must be split "
                            "between the old and new rates (s73)."))

    nets = result.net_by_currency()
    if len(nets) > 1:
        payable = [f"{fmt(v)} {c}" for c, v in nets.items() if v > 0]
        refund = [f"{fmt(-v)} {c}" for c, v in nets.items() if v < 0]
        mixed = payable and refund
        issues.append(Issue(WARNING if mixed else INFO, "currency_setoff", "VAT is due separately in each currency",
                            "Output and input tax in different currencies cannot be set off against each other "
                            f"(s38(4)). Payable: {', '.join(payable) or 'none'}. Refundable: "
                            f"{', '.join(refund) or 'none'}. The converted total is for information only; pay "
                            "foreign-currency VAT in that currency, or face a penalty of double the tax (s38A)."))

    usd_net = nets.get("USD")
    if usd_net is not None and -config.MIN_REFUND <= usd_net < 0:
        issues.append(Issue(INFO, "small_refund", f"USD refund of {fmt(-usd_net)} will be carried forward",
                            f"Refunds of US${fmt(config.MIN_REFUND)} or less are carried forward to the next "
                            "period rather than paid (s44)."))

    failed_uploads = session.scalars(select(Upload).where(
        Upload.business_id == result.business_id, Upload.rows_failed > 0)).all()
    if failed_uploads:
        n = sum(u.rows_failed for u in failed_uploads)
        issues.append(Issue(WARNING, "unread_rows", f"{n} uploaded row(s) could not be read",
                            "Fix them in the uploads list; they may belong to this period."))

    issues += _late_filing(session, result, today)

    order = {ERROR: 0, WARNING: 1, INFO: 2}
    return sorted(issues, key=lambda i: order[i.severity])


def _claimed_elsewhere(session: Session, result: ReturnResult, claims: list[Transaction]) -> list[Issue]:
    """Input tax invoices that were also claimed in another period."""
    numbers = {_norm(t.invoice_number) for t in claims if t.invoice_number}
    if not numbers:
        return []
    p = result.period
    others = session.scalars(select(Transaction).where(
        Transaction.business_id == result.business_id, Transaction.excluded.is_(False),
        Transaction.direction == PURCHASE, func.lower(func.trim(Transaction.invoice_number)).in_(numbers),
        or_(Transaction.txn_date < p.start, Transaction.txn_date > p.end))).all()
    seen = defaultdict(list)
    for o in others:
        seen[_norm(o.invoice_number)].append(o)
    hits = []
    for t in claims:
        for o in seen.get(_norm(t.invoice_number), []):
            same_supplier = (_norm(t.counterparty_vat_number) and
                             _norm(t.counterparty_vat_number) == _norm(o.counterparty_vat_number)) or \
                            (_norm(t.counterparty) and _norm(t.counterparty) == _norm(o.counterparty))
            if same_supplier:
                hits.append(t.id)
                break
    if not hits:
        return []
    return [Issue(WARNING, "claimed_elsewhere", f"{len(hits)} purchase invoice(s) also appear in another period",
                  "The same supplier invoice may be claimed twice. Each tax invoice supports one claim (s15(2), "
                  "s20(1)).", hits)]


def _apportionment(result: ReturnResult, txns: list[Transaction]) -> list[Issue]:
    share = result.taxable_share
    mixed = [t.id for t in txns if t.direction == PURCHASE and t.input_type == INPUT_MIXED]
    has_exempt = any(t.direction == SALE and t.vat_category == EXEMPT for t in txns)
    pct = f"{share * 100:.1f}%" if share is not None else "unknown"
    if mixed:
        if share is None:
            detail = "There are no supplies this period to base the ratio on, so mixed-use input tax is claimed " \
                     "in full. Confirm the basis with your tax adviser."
        elif share >= FULL_CLAIM_THRESHOLD:
            detail = f"Taxable supplies are {pct} of all supplies, at least 90%, so the full amount is claimed (s16(1))."
        else:
            detail = f"Taxable supplies are {pct} of all supplies, so {pct} of mixed-use input tax is claimed and " \
                     "the rest is not deductible (s16(1))."
        return [Issue(INFO, "apportionment", f"{len(mixed)} mixed-use purchase(s) apportioned", detail, mixed)]
    if has_exempt and share is not None and share < FULL_CLAIM_THRESHOLD and result.line("TOTAL_INPUT").total:
        general = [t.id for t in txns if t.direction == PURCHASE and t.vat_category == STANDARD
                   and t.input_type == INPUT_GENERAL]
        return [Issue(WARNING, "apportionment", "Exempt supplies made alongside input tax claims",
                      f"Taxable supplies are {pct} of all supplies. Input tax on purchases used for both taxable "
                      "and exempt supplies must be apportioned (s16(1)): set their input type to Mixed use. "
                      "Input tax on purchases used only for exempt supplies cannot be claimed.", general)]
    return []


def _late_filing(session: Session, result: ReturnResult, today: date | None) -> list[Issue]:
    if today is None:
        return []
    p = result.period
    finalised = session.scalar(select(VatReturn.id).where(
        VatReturn.business_id == result.business_id, VatReturn.period_start == p.start,
        VatReturn.status == "finalised"))
    est = None if finalised else estimate(p, result.net_by_currency(), today)
    if est is None:
        return []
    parts = [f"Late return penalty: about US${fmt(est.late_return_penalty)} "
             f"(US${fmt(config.LATE_RETURN_PENALTY_PER_DAY)} a day for up to {config.LATE_RETURN_MAX_DAYS} days, "
             "s62(2))."]
    for ccy, e in est.by_currency.items():
        interest = (f" plus interest of about {fmt(e.interest)} {ccy} ({est.interest_months} month(s))"
                    if e.interest is not None else " plus interest at the prescribed rate (not estimated: set "
                    "VATSYS_LATE_INTEREST_RATE)")
        parts.append(f"Late payment of {fmt(e.tax)} {ccy}: penalty of {fmt(e.penalty)} {ccy} (100% of the tax, "
                     f"s39(2)){interest}.")
    parts.append("ZIMRA may waive penalties and interest where there was no intent to avoid tax (s39(5)).")
    return [Issue(WARNING, "late", f"Return is {est.days_late} day(s) past its due date of {p.due_date:%d %b %Y}",
                  " ".join(parts))]
