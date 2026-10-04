"""Turn an uploaded file into classified transactions."""

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePath

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import audit
from ..classify import Classifier
from ..models import Business, ClassificationRule, Transaction, Upload, VatReturn
from .parse import FIELD_LABELS, ParseResult, detect_header, map_headers, parse_rows
from .readers import SUPPORTED_EXTENSIONS, IngestError, read_table

__all__ = ["FIELD_LABELS", "IngestError", "SUPPORTED_EXTENSIONS", "UploadOptions", "import_file",
           "reprocess_upload", "load_table_preview", "classifier_for", "locked_ranges"]


@dataclass
class UploadOptions:
    source_type: str = "ledger"  # ledger | bank | mobile_money
    default_direction: str = "auto"  # auto | sale | purchase
    default_currency: str = "USD"
    amounts_include_vat: bool = True


def classifier_for(session: Session, business_id: int) -> Classifier:
    from ..reference import load_matcher  # imported here: reference.py uses this package's parsers

    rules = list(session.scalars(select(ClassificationRule).where(
        (ClassificationRule.business_id.is_(None)) | (ClassificationRule.business_id == business_id))))
    return Classifier(rules, business_id, load_matcher(session))


def locked_ranges(session: Session, business_id: int) -> list[tuple[date, date]]:
    return [(r.period_start, r.period_end) for r in session.scalars(
        select(VatReturn).where(VatReturn.business_id == business_id, VatReturn.status == "finalised"))]


def _in_locked(d: date, ranges) -> bool:
    return any(s <= d <= e for s, e in ranges)


def _store(storage_dir: Path, business_id: int, filename: str, content: bytes) -> tuple[str, str]:
    sha = hashlib.sha256(content).hexdigest()
    folder = storage_dir / "uploads" / str(business_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sha}{PurePath(filename).suffix.lower()}"
    path.write_bytes(content)
    return str(path), sha


def _apply(session: Session, business: Business, upload: Upload, result: ParseResult) -> None:
    classifier = classifier_for(session, business.id)
    locked = locked_ranges(session, business.id)
    errors = list(result.errors)
    imported = 0
    for row in result.rows:
        if _in_locked(row.txn_date, locked):
            errors.append({"row": row.row_number, "values": row.raw,
                           "errors": ["Falls in a tax period whose return is already finalised"]})
            continue
        c = classifier.classify(direction=row.direction, description=row.description,
                                counterparty=row.counterparty, file_category=row.file_category, on=row.txn_date)
        session.add(Transaction(
            business_id=business.id, upload_id=upload.id, row_number=row.row_number, txn_date=row.txn_date,
            description=row.description, counterparty=row.counterparty,
            counterparty_vat_number=row.vat_number, invoice_number=row.invoice_number,
            reference=row.reference, direction=row.direction, amount=row.amount, currency=row.currency,
            amount_includes_vat=upload.amounts_include_vat, stated_vat=row.stated_vat,
            vat_category=c.vat_category, input_type=c.input_type, classification_source=c.source,
            classification_reason=c.reason, rule_id=c.rule_id, reference_item_id=c.reference_item_id,
            confidence=c.confidence, raw=row.raw,
        ))
        imported += 1
    upload.header_row = result.header_row
    upload.mapping = result.mapping
    upload.headers = result.headers
    upload.rows_imported = imported
    upload.rows_failed = len(errors)
    upload.rows_ignored = result.ignored
    upload.row_errors = sorted(errors, key=lambda e: e["row"])


def _headers(row: list) -> list[str]:
    return [str(c) if c not in (None, "") else f"Column {i + 1}" for i, c in enumerate(row)]


def _parse(rows, options: UploadOptions, header_row: int | None, mapping: dict | None) -> ParseResult:
    if not rows:
        raise IngestError("The file is empty.")
    if header_row is None or mapping is None:
        header_row, detected = detect_header(rows)
        mapping = mapping or detected
    if "date" not in mapping or not any(k in mapping for k in ("amount", "debit", "credit")):
        raise IngestError("Choose at least a date column and an amount (or debit/credit) column.")
    return parse_rows(rows, header_row, mapping, default_direction=options.default_direction,
                      default_currency=options.default_currency)


def import_file(session: Session, business: Business, filename: str, content: bytes, options: UploadOptions,
                *, storage_dir: Path, user_id: int | None = None) -> Upload:
    sha = hashlib.sha256(content).hexdigest()
    existing = session.scalar(select(Upload).where(Upload.business_id == business.id, Upload.sha256 == sha))
    if existing:
        raise IngestError(f"This file was already uploaded on {existing.created_at:%d %b %Y} "
                          f"as '{existing.filename}'. Delete that upload first to import it again.")
    rows = read_table(filename, content)
    if not rows:
        raise IngestError("The file is empty.")
    try:
        result = _parse(rows, options, None, None)
    except IngestError:
        result = None  # columns not recognised: keep the file so the user can map them by hand
    stored_path, sha = _store(storage_dir, business.id, filename, content)
    upload = Upload(business_id=business.id, filename=filename, stored_path=stored_path, sha256=sha,
                    source_type=options.source_type, default_direction=options.default_direction,
                    default_currency=options.default_currency,
                    amounts_include_vat=options.amounts_include_vat, uploaded_by=user_id)
    session.add(upload)
    session.flush()
    if result:
        _apply(session, business, upload, result)
    else:
        upload.header_row, upload.headers, upload.mapping, upload.row_errors = 0, _headers(rows[0]), {}, []
    audit.log(session, "upload", user_id=user_id, business_id=business.id, entity="upload",
              entity_id=upload.id, filename=filename, imported=upload.rows_imported, failed=upload.rows_failed)
    return upload


def load_table_preview(upload: Upload, limit: int = 8) -> tuple[list[list], int]:
    rows = read_table(upload.filename, Path(upload.stored_path).read_bytes())
    header = upload.header_row or 0
    return rows[header:header + limit + 1], header


def reprocess_upload(session: Session, business: Business, upload: Upload, options: UploadOptions,
                     header_row: int, mapping: dict[str, int], *, user_id: int | None = None) -> Upload:
    """Re-import an upload with corrected column mapping or options, replacing its transactions."""
    locked = locked_ranges(session, business.id)
    if any(_in_locked(t.txn_date, locked) for t in session.scalars(
            select(Transaction).where(Transaction.upload_id == upload.id))):
        raise IngestError("Some rows from this upload are in a finalised return. Reopen that return first.")
    rows = read_table(upload.filename, Path(upload.stored_path).read_bytes())
    if not 0 <= header_row < len(rows):
        raise IngestError("Header row is outside the file.")
    # Saved before parsing so a failed attempt still shows the right column names.
    upload.header_row, upload.headers = header_row, _headers(rows[header_row])
    if not mapping:
        mapping, _ = map_headers(rows[header_row])
    result = _parse(rows, options, header_row, mapping)
    session.execute(delete(Transaction).where(Transaction.upload_id == upload.id))
    upload.source_type = options.source_type
    upload.default_direction = options.default_direction
    upload.default_currency = options.default_currency
    upload.amounts_include_vat = options.amounts_include_vat
    _apply(session, business, upload, result)
    audit.log(session, "reprocess_upload", user_id=user_id, business_id=business.id, entity="upload",
              entity_id=upload.id, mapping=mapping, header_row=header_row,
              imported=upload.rows_imported, failed=upload.rows_failed)
    return upload
