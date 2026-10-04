"""Detect columns in a raw table and turn its rows into normalised transactions."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from ..models import CURRENCIES, PURCHASE, SALE
from .readers import IngestError

FIELD_LABELS = {
    "date": "Date",
    "description": "Description",
    "counterparty": "Customer / supplier",
    "amount": "Amount",
    "debit": "Debit (money out)",
    "credit": "Credit (money in)",
    "currency": "Currency",
    "invoice_number": "Invoice number",
    "reference": "Reference",
    "vat_number": "Counterparty VAT / BP number",
    "vat_amount": "VAT amount",
    "direction": "Sale / purchase type",
    "vat_category": "VAT category",
}

FIELD_SYNONYMS = {
    "date": ["date", "transaction date", "txn date", "trans date", "value date", "posting date", "post date",
             "invoice date", "document date", "date time", "date and time", "completion time", "tran date"],
    "description": ["description", "narration", "narrative", "details", "particulars", "memo",
                    "transaction details", "transaction description", "item", "remarks", "item description"],
    "counterparty": ["customer", "supplier", "payee", "payer", "counterparty", "name", "customer name",
                     "supplier name", "party", "recipient", "sender", "vendor", "client", "beneficiary",
                     "opposite party", "account name"],
    "amount": ["amount", "value", "total", "gross", "gross amount", "amount incl vat", "total incl vat",
               "amount inclusive", "transaction amount", "total amount", "invoice total", "amount vat inclusive"],
    "debit": ["debit", "debits", "debit amount", "withdrawal", "withdrawals", "money out", "paid out", "dr",
              "out", "amount out"],
    "credit": ["credit", "credits", "credit amount", "deposit", "deposits", "money in", "paid in", "cr", "in",
               "amount in"],
    "currency": ["currency", "ccy", "cur", "curr", "currency code"],
    "invoice_number": ["invoice", "invoice no", "invoice number", "inv no", "inv", "receipt no",
                       "receipt number", "fiscal invoice", "fiscal invoice no", "fiscal invoice number",
                       "tax invoice no", "tax invoice number", "document no", "doc no"],
    "reference": ["reference", "ref", "ref no", "reference number", "transaction id", "txn id", "trans id",
                  "transaction reference", "cheque no"],
    "vat_number": ["vat no", "vat number", "vat reg no", "vat registration number", "supplier vat",
                   "customer vat", "bp number", "bp no", "supplier vat no", "customer vat no"],
    "vat_amount": ["vat", "vat amount", "tax", "tax amount", "output vat", "input vat", "vat charged"],
    "direction": ["type", "transaction type", "direction", "dr cr", "sale purchase", "flow", "entry type"],
    "vat_category": ["vat category", "tax category", "vat type", "vat code", "tax code", "vat rate",
                     "vat status"],
}

# Short synonyms only count on an exact header match, never as a substring.
_EXACT_ONLY = {"dr", "cr", "in", "out", "inv", "ref", "cur", "ccy", "vat", "tax", "name", "type", "value", "total"}

_CURRENCY_TOKENS = {"usd": "USD", "us": "USD", "zwg": "ZWG", "zig": "ZWG"}

CURRENCY_ALIASES = {
    "USD": "USD", "US$": "USD", "$": "USD", "US": "USD", "USDOLLAR": "USD",
    "ZWG": "ZWG", "ZIG": "ZWG", "ZIGS": "ZWG", "ZIMBABWEGOLD": "ZWG",
}

SKIP_DESCRIPTION = re.compile(
    r"^\s*((opening|closing|available|ledger) balance|balance (b/?f|c/?f|brought|carried)|"
    r"brought forward|carried forward|sub-?total|grand total|total)\b", re.IGNORECASE)

DOC_SALE = {"sale", "sales", "income", "revenue", "invoice", "sales invoice", "output", "tax invoice"}
DOC_PURCHASE = {"purchase", "purchases", "expense", "expenses", "expenditure", "bill", "supplier invoice",
                "input", "cost", "purchase invoice"}
FLOW_IN = {"cr", "credit", "in", "money in", "deposit", "receipt", "received", "cash in", "c"}
FLOW_OUT = {"dr", "debit", "out", "money out", "withdrawal", "payment", "paid", "cash out", "sent", "d"}


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def match_header(cell) -> tuple[str | None, int, str | None]:
    """Return (field, score, currency_hint) for one header cell."""
    tokens = _norm(cell).split()
    ccy = next((_CURRENCY_TOKENS[t] for t in tokens if t in _CURRENCY_TOKENS), None)
    if ccy and len(tokens) > 1:
        tokens = [t for t in tokens if t not in _CURRENCY_TOKENS]
    header = " ".join(tokens)
    if not header:
        return None, 0, None
    best, best_score = None, 0
    for fld, synonyms in FIELD_SYNONYMS.items():
        for syn in synonyms:
            if header == syn:
                score = 100 + len(syn)
            elif syn not in _EXACT_ONLY and re.search(rf"\b{re.escape(syn)}\b", header):
                score = len(syn)
            else:
                continue
            if score > best_score:
                best, best_score = fld, score
    # "Debit amount" / "Credit amount": the flow word decides, not "amount".
    if best == "amount" and re.search(r"\b(debit|dr|withdrawals?)\b", header):
        best = "debit"
    elif best == "amount" and re.search(r"\b(credit|cr|deposits?)\b", header):
        best = "credit"
    elif best == "amount" and re.search(r"\b(vat|tax)\b", header) and not re.search(r"\bincl", header):
        best = "vat_amount"
    if best and "balance" in header:
        return None, 0, None
    return best, best_score, ccy


def map_headers(row: list) -> tuple[dict[str, int], dict[int, str]]:
    """Map fields to column indexes. Returns (mapping, currency hints per column)."""
    candidates: dict[str, tuple[int, int]] = {}
    hints: dict[int, str] = {}
    for idx, cell in enumerate(row):
        fld, score, ccy = match_header(cell)
        if ccy:
            hints[idx] = ccy
        if fld and (fld not in candidates or score > candidates[fld][1]):
            candidates[fld] = (idx, score)
    return {f: i for f, (i, _) in candidates.items()}, hints


def _is_usable(mapping: dict) -> bool:
    return "date" in mapping and any(k in mapping for k in ("amount", "debit", "credit"))


def detect_header(rows: list[list], scan: int = 30) -> tuple[int, dict[str, int]]:
    best = None
    for i, row in enumerate(rows[:scan]):
        mapping, _ = map_headers(row)
        if _is_usable(mapping) and (best is None or len(mapping) > len(best[1])):
            best = (i, mapping)
    if best is None:
        raise IngestError(
            "Could not find a header row with a date column and an amount (or debit/credit) column. "
            "Check that the file has column headings, or set the columns manually.")
    return best


# ---- value parsing -------------------------------------------------------

_DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y",
                 "%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%d %b %y", "%d %B %Y", "%d-%B-%Y", "%b %d, %Y",
                 "%B %d, %Y", "%Y%m%d"]


def parse_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 20000 <= value <= 80000:  # Excel serial date
        return date(1899, 12, 30) + timedelta(days=int(value))
    text = str(value).strip()
    candidates = [text]
    head = re.split(r"[ T](?=\d{1,2}:\d{2})", text)[0]  # drop a trailing time
    if head != text:
        candidates.append(head)
    for candidate in candidates:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def normalise_currency(value) -> str | None:
    if value is None:
        return None
    key = re.sub(r"[^A-Z$]", "", str(value).upper())
    return CURRENCY_ALIASES.get(key)


def parse_amount(value) -> tuple[Decimal | None, str | None]:
    """Parse '1,234.50', '(12.00)', 'USD 50', '100.00 DR', '1 234,50'. Returns (amount, currency)."""
    if value is None or value == "":
        return None, None
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)), None
    text = str(value).strip().upper()
    currency = None
    m = re.search(r"US\$|USD|ZWG|ZIG|\$", text)
    if m:
        currency = normalise_currency(m.group())
        text = text.replace(m.group(), " ")
    negative = False
    if re.search(r"\bDR\b", text):
        negative = True
        text = re.sub(r"\bDR\b", " ", text)
    text = re.sub(r"\bCR\b", " ", text)
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    if text.endswith("-"):
        negative, text = True, text[:-1]
    if text.startswith("-"):
        negative, text = not negative, text[1:]
    text = text.replace(" ", "").replace(" ", "").replace("'", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):  # 1.234,56
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        if text.count(",") == 1 and re.search(r",\d{2}$", text):
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", text):
        return None, currency
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None, currency
    return (-amount if negative else amount), currency


# ---- row normalisation ---------------------------------------------------

@dataclass
class ParsedRow:
    row_number: int  # 1-based, as seen in Excel
    txn_date: date
    description: str
    counterparty: str | None
    invoice_number: str | None
    reference: str | None
    vat_number: str | None
    direction: str
    amount: Decimal
    currency: str
    stated_vat: Decimal | None
    file_category: str | None
    raw: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    rows: list[ParsedRow]
    errors: list[dict]
    ignored: int
    header_row: int
    mapping: dict[str, int]
    headers: list[str]


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


def _direction_word(value) -> tuple[str | None, bool]:
    """Return (direction, is_flow). Flow words (DR/CR) describe money movement, so amounts are made positive."""
    v = _norm(value)
    if v in DOC_SALE:
        return SALE, False
    if v in DOC_PURCHASE:
        return PURCHASE, False
    if v in FLOW_IN:
        return SALE, True
    if v in FLOW_OUT:
        return PURCHASE, True
    return None, False


def parse_rows(rows: list[list], header_row: int, mapping: dict[str, int], *, default_direction: str,
               default_currency: str) -> ParseResult:
    headers = [str(c) if c is not None else f"Column {i + 1}" for i, c in enumerate(rows[header_row])]
    _, hints = map_headers(rows[header_row])
    parsed, errors, ignored = [], [], 0

    def cell(row, fld):
        idx = mapping.get(fld)
        return row[idx] if idx is not None and idx < len(row) else None

    for offset, row in enumerate(rows[header_row + 1:], start=header_row + 2):
        if not any(c not in (None, "") for c in row):
            continue
        raw = {headers[i] if i < len(headers) else f"Column {i + 1}": _text(v)
               for i, v in enumerate(row) if v not in (None, "")}
        description = _text(cell(row, "description")) or ""
        date_value = cell(row, "date")
        amount_cells = [cell(row, f) for f in ("amount", "debit", "credit")]
        if SKIP_DESCRIPTION.search(description) or (
                date_value in (None, "") and all(v in (None, "") for v in amount_cells)):
            ignored += 1
            continue

        problems = []
        txn_date = parse_date(date_value)
        if txn_date is None:
            problems.append(f"Unreadable date '{date_value}'" if date_value not in (None, "") else "Missing date")

        amount, amount_ccy, direction = None, None, None
        if "debit" in mapping or "credit" in mapping:
            debit, dccy = parse_amount(cell(row, "debit"))
            credit, cccy = parse_amount(cell(row, "credit"))
            if credit:
                amount, direction, amount_ccy = abs(credit), SALE, cccy or hints.get(mapping.get("credit"))
            elif debit:
                amount, direction, amount_ccy = abs(debit), PURCHASE, dccy or hints.get(mapping.get("debit"))
            elif "amount" in mapping:
                pass  # fall through to the amount column
            elif debit is None and credit is None and (cell(row, "debit") or cell(row, "credit")):
                problems.append("Unreadable debit/credit amount")
            else:
                ignored += 1
                continue
        if amount is None and "amount" in mapping:
            value = cell(row, "amount")
            amount, amount_ccy = parse_amount(value)
            amount_ccy = amount_ccy or hints.get(mapping["amount"])
            if amount is None:
                problems.append(f"Unreadable amount '{value}'" if value not in (None, "") else "Missing amount")
            else:
                word_dir, is_flow = _direction_word(cell(row, "direction"))
                if _norm(cell(row, "direction")) == "credit note":
                    direction, amount = SALE, -abs(amount)
                elif word_dir:
                    direction = word_dir
                    if is_flow:
                        amount = abs(amount)
                elif default_direction in (SALE, PURCHASE):
                    direction = default_direction
                else:
                    direction = SALE if amount >= 0 else PURCHASE
                    amount = abs(amount)

        if amount is not None and amount == 0:
            ignored += 1
            continue

        currency_value = cell(row, "currency")
        currency = normalise_currency(currency_value) if currency_value not in (None, "") else None
        if currency_value not in (None, "") and currency is None:
            problems.append(f"Unsupported currency '{currency_value}' (only {', '.join(CURRENCIES)})")
        currency = currency or amount_ccy or default_currency

        stated_vat, _ = parse_amount(cell(row, "vat_amount"))
        if stated_vat is not None and amount is not None:
            stated_vat = abs(stated_vat) if amount >= 0 else -abs(stated_vat)

        if problems:
            errors.append({"row": offset, "errors": problems, "values": raw})
            continue
        parsed.append(ParsedRow(
            row_number=offset, txn_date=txn_date, description=description,
            counterparty=_text(cell(row, "counterparty")), invoice_number=_text(cell(row, "invoice_number")),
            reference=_text(cell(row, "reference")), vat_number=_text(cell(row, "vat_number")),
            direction=direction, amount=amount, currency=currency, stated_vat=stated_vat,
            file_category=_text(cell(row, "vat_category")), raw=raw,
        ))
    return ParseResult(parsed, errors, ignored, header_row, mapping, headers)
