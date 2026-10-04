"""VAT computation for one tax period.

Every figure on the return is a sum of per-transaction calculations, and each
line keeps the ids of the transactions behind it, so any figure can be traced
back to its source rows.

Rules applied (VAT Act section numbers):
- VAT at the rate in force on the transaction date, extracted from inclusive
  amounts with the tax fraction rate / (100 + rate) (s6, s9(2)).
- Rounding to the cent in the taxpayer's favour (s71).
- Mixed-use purchases are apportioned by the period's taxable share of supplies,
  claimed in full when that share is 90% or more (s16(1)).
- VAT on imported services is self-accounted by the recipient (s13).
- Each currency is totalled separately; the reporting-currency total is for
  information and does not set one currency off against another (s38(4)).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    EXEMPT,
    INPUT_CAPITAL,
    INPUT_IMPORT,
    INPUT_IMPORTED_SERVICE,
    INPUT_MIXED,
    INPUT_NON_DEDUCTIBLE,
    OUT_OF_SCOPE,
    SALE,
    STANDARD,
    ZERO_RATED,
    Business,
    Transaction,
)
from .money import ZERO, q2, q2_vat, split_gross, vat_on_net
from .periods import Period
from .rates import FxBook, MissingRate, VatRateBook


@dataclass
class TxnCalc:
    txn: Transaction
    rate: Decimal | None = None
    net: Decimal = ZERO
    vat: Decimal = ZERO
    fx_rate: Decimal = Decimal("1")
    fx_date: date | None = None
    fx_source: str | None = None
    net_rep: Decimal = ZERO
    vat_rep: Decimal = ZERO
    gross_rep: Decimal = ZERO
    # Share of the VAT that may be claimed (mixed-use purchases), and that amount
    claim_ratio: Decimal = Decimal("1")
    claim: Decimal = ZERO
    claim_rep: Decimal = ZERO
    error: str | None = None

    @property
    def gross(self) -> Decimal:
        return self.net + self.vat

    def value(self, key: str, reporting: bool = False) -> Decimal:
        if key == "disallowed":
            return (self.vat_rep - self.claim_rep) if reporting else (self.vat - self.claim)
        return getattr(self, f"{key}_rep" if reporting else key)


# (code, label, section, which value of each transaction is summed)
LINE_DEFS = [
    ("SR_SUPPLIES", "Standard-rated supplies (excl. VAT)", "supplies", "net"),
    ("ZR_SUPPLIES", "Zero-rated supplies", "supplies", "net"),
    ("EX_SUPPLIES", "Exempt supplies", "supplies", "net"),
    ("TOTAL_SUPPLIES", "Total supplies", "supplies", "total"),
    ("OUTPUT_TAX", "Output tax on standard-rated supplies", "output", "vat"),
    ("IMPORTED_SERVICES_TAX", "VAT on imported services (self-accounted)", "output", "vat"),
    ("TOTAL_OUTPUT", "Total output tax", "output", "total"),
    ("INPUT_CAPITAL", "Input tax on capital goods", "input", "claim"),
    ("INPUT_IMPORTS", "Input tax on imports", "input", "claim"),
    ("INPUT_OTHER", "Input tax on other goods and services", "input", "claim"),
    ("TOTAL_INPUT", "Total input tax", "input", "total"),
    ("NET_VAT", "Net VAT payable / (refundable)", "net", "total"),
    ("PURCHASES_STANDARD", "Standard-rated purchases (excl. VAT)", "info", "net"),
    ("PURCHASES_NO_VAT", "Zero-rated and exempt purchases", "info", "net"),
    ("NON_DEDUCTIBLE", "VAT on purchases not deductible (blocked or apportioned out)", "info", "disallowed"),
    ("OUT_OF_SCOPE", "Out-of-scope transactions (not on the return)", "info", "gross"),
]
LINE_LABELS = {code: label for code, label, _, _ in LINE_DEFS}


@dataclass
class Line:
    code: str
    label: str
    section: str
    by_currency: dict[str, Decimal] = field(default_factory=dict)
    total: Decimal = ZERO  # in the reporting currency
    txn_ids: list[int] = field(default_factory=list)


@dataclass
class ReturnResult:
    business_id: int
    period: Period
    reporting_currency: str
    currencies: list[str]
    lines: dict[str, Line]
    calcs: dict[int, TxnCalc]
    fx_used: list[dict]
    taxable_share: Decimal | None = None  # taxable supplies / all supplies; None when there were no supplies
    claim_ratio: Decimal = Decimal("1")  # applied to mixed-use input tax

    def net_by_currency(self) -> dict[str, Decimal]:
        """Net VAT in each currency. These cannot be set off against each other (s38(4))."""
        return {c: v for c, v in self.lines["NET_VAT"].by_currency.items() if v}

    @property
    def errors(self) -> list[TxnCalc]:
        return [c for c in self.calcs.values() if c.error]

    def line(self, code: str) -> Line:
        return self.lines[code]

    def to_snapshot(self) -> dict:
        """JSON-safe copy stored when a return is finalised."""
        return {
            "period_start": self.period.start.isoformat(),
            "period_end": self.period.end.isoformat(),
            "reporting_currency": self.reporting_currency,
            "currencies": self.currencies,
            "taxable_share": str(self.taxable_share) if self.taxable_share is not None else None,
            "claim_ratio": str(self.claim_ratio),
            "lines": {
                code: {"label": l.label, "section": l.section, "total": str(l.total),
                       "by_currency": {k: str(v) for k, v in l.by_currency.items()}, "txn_ids": l.txn_ids}
                for code, l in self.lines.items()
            },
            "transactions": {
                str(tid): {"date": c.txn.txn_date.isoformat(), "description": c.txn.description,
                           "direction": c.txn.direction, "category": c.txn.vat_category,
                           "input_type": c.txn.input_type, "amount": str(c.txn.amount),
                           "currency": c.txn.currency, "rate": str(c.rate) if c.rate is not None else None,
                           "net": str(c.net), "vat": str(c.vat), "claim": str(c.claim),
                           "claim_ratio": str(c.claim_ratio), "fx_rate": str(c.fx_rate),
                           "fx_date": c.fx_date.isoformat() if c.fx_date else None,
                           "net_rep": str(c.net_rep), "vat_rep": str(c.vat_rep)}
                for tid, c in self.calcs.items()
            },
            "fx_used": [{**f, "rate": str(f["rate"]), "date": f["date"].isoformat()} for f in self.fx_used],
        }


def calculate_transaction(txn: Transaction, vat_rates: VatRateBook, fx: FxBook, reporting_ccy: str,
                          claim_ratio: Decimal = Decimal("1")) -> TxnCalc:
    calc = TxnCalc(txn)
    is_input = txn.direction != SALE and txn.input_type != INPUT_IMPORTED_SERVICE
    try:
        if txn.vat_category in (STANDARD, ZERO_RATED):
            calc.rate = vat_rates.rate_for(txn.vat_category, txn.txn_date)
            if txn.input_type == INPUT_IMPORT and is_input and txn.stated_vat is not None:
                # Import VAT is taken from the customs documents, not recomputed.
                calc.vat = txn.stated_vat
                calc.net = txn.amount - txn.stated_vat if txn.amount_includes_vat else txn.amount
            elif txn.input_type == INPUT_IMPORTED_SERVICE and txn.direction != SALE:
                # A foreign supplier charges no VAT; the recipient adds it to the amount paid (s13).
                calc.net, calc.vat = vat_on_net(txn.amount, calc.rate, favour_lower=True)
            elif txn.amount_includes_vat:
                calc.net, calc.vat = split_gross(txn.amount, calc.rate, favour_lower=not is_input)
            else:
                calc.net, calc.vat = vat_on_net(txn.amount, calc.rate, favour_lower=not is_input)
        else:
            calc.net, calc.vat = txn.amount, ZERO
        if is_input and txn.vat_category == STANDARD:
            if txn.input_type == INPUT_NON_DEDUCTIBLE:
                calc.claim_ratio = ZERO
            elif txn.input_type == INPUT_MIXED:
                calc.claim_ratio = claim_ratio
            calc.claim = calc.vat if calc.claim_ratio == 1 else q2_vat(calc.vat * calc.claim_ratio, False)
        conv = fx.convert(Decimal(1), txn.currency, reporting_ccy, txn.txn_date)
        calc.fx_rate, calc.fx_date, calc.fx_source = conv.rate, conv.rate_date, conv.source
        calc.net_rep = q2(calc.net * conv.rate)
        calc.vat_rep = q2(calc.vat * conv.rate)
        calc.claim_rep = calc.vat_rep if calc.claim == calc.vat else q2(calc.claim * conv.rate)
        calc.gross_rep = calc.net_rep + calc.vat_rep
    except MissingRate as e:
        calc.error = str(e)
    return calc


FULL_CLAIM_THRESHOLD = Decimal("0.9")  # s16(1): claim in full when taxable use is 90% or more


def mixed_use_ratio(calcs: list[TxnCalc]) -> tuple[Decimal | None, Decimal]:
    """(taxable share of supplies, ratio applied to mixed-use input tax) from the period's sales."""
    taxable = total = ZERO
    for c in calcs:
        t = c.txn
        if c.error or t.direction != SALE or t.vat_category == OUT_OF_SCOPE:
            continue
        total += c.net_rep
        if t.vat_category in (STANDARD, ZERO_RATED):
            taxable += c.net_rep
    if total <= 0:
        return None, Decimal("1")
    share = max(ZERO, min(Decimal("1"), taxable / total)).quantize(Decimal("0.0001"))
    return share, Decimal("1") if share >= FULL_CLAIM_THRESHOLD else share


def _line_codes(c: TxnCalc) -> list[tuple[str, str]]:
    """Which lines a transaction contributes to, and with which value."""
    t = c.txn
    if t.vat_category == OUT_OF_SCOPE:
        return [("OUT_OF_SCOPE", "gross")]
    if t.direction == SALE:
        return {
            STANDARD: [("SR_SUPPLIES", "net"), ("OUTPUT_TAX", "vat")],
            ZERO_RATED: [("ZR_SUPPLIES", "net")],
            EXEMPT: [("EX_SUPPLIES", "net")],
        }[t.vat_category]
    if t.vat_category != STANDARD:
        return [("PURCHASES_NO_VAT", "net")]
    if t.input_type == INPUT_IMPORTED_SERVICE:
        return [("PURCHASES_STANDARD", "net"), ("IMPORTED_SERVICES_TAX", "vat")]
    out = [("PURCHASES_STANDARD", "net")]
    if c.claim:
        tax_line = {INPUT_CAPITAL: "INPUT_CAPITAL", INPUT_IMPORT: "INPUT_IMPORTS"}.get(t.input_type, "INPUT_OTHER")
        out.append((tax_line, "claim"))
    if c.claim != c.vat:
        out.append(("NON_DEDUCTIBLE", "disallowed"))
    return out


def compute_return(session: Session, business: Business, period: Period) -> ReturnResult:
    txns = list(session.scalars(
        select(Transaction).where(Transaction.business_id == business.id, Transaction.excluded.is_(False),
                                  Transaction.txn_date >= period.start, Transaction.txn_date <= period.end)
        .order_by(Transaction.txn_date, Transaction.id)))
    vat_rates, fx = VatRateBook(session), FxBook(session)
    rep = business.reporting_currency
    lines = {code: Line(code, label, section) for code, label, section, _ in LINE_DEFS}
    calcs: dict[int, TxnCalc] = {}
    fx_used: dict[tuple, dict] = {}
    currencies = sorted({t.currency for t in txns} | {rep})

    def add(line: Line, ccy: str, amount: Decimal, amount_rep: Decimal, tid: int | None):
        line.by_currency[ccy] = line.by_currency.get(ccy, ZERO) + amount
        line.total += amount_rep
        if tid is not None and tid not in line.txn_ids:
            line.txn_ids.append(tid)

    # Sales first, so the taxable share is known before mixed-use purchases are apportioned.
    for t in txns:
        if t.direction == SALE:
            calcs[t.id] = calculate_transaction(t, vat_rates, fx, rep)
    taxable_share, claim_ratio = mixed_use_ratio(list(calcs.values()))
    for t in txns:
        if t.direction != SALE:
            calcs[t.id] = calculate_transaction(t, vat_rates, fx, rep, claim_ratio)
    calcs = {t.id: calcs[t.id] for t in txns}  # back in date order

    for t in txns:
        c = calcs[t.id]
        if c.error:
            continue
        if t.currency != rep:
            key = (t.currency, rep, c.fx_date)
            entry = fx_used.setdefault(key, {"from": t.currency, "to": rep, "date": c.fx_date,
                                             "rate": c.fx_rate, "source": c.fx_source, "count": 0})
            entry["count"] += 1
        for code, value in _line_codes(c):
            add(lines[code], t.currency, c.value(value), c.value(value, reporting=True), t.id)

    def combine(target: str, parts: list[str], signs: list[int] | None = None):
        signs = signs or [1] * len(parts)
        line = lines[target]
        for code, sign in zip(parts, signs):
            src = lines[code]
            for ccy, amount in src.by_currency.items():
                line.by_currency[ccy] = line.by_currency.get(ccy, ZERO) + sign * amount
            line.total += sign * src.total
            line.txn_ids.extend(i for i in src.txn_ids if i not in line.txn_ids)

    combine("TOTAL_SUPPLIES", ["SR_SUPPLIES", "ZR_SUPPLIES", "EX_SUPPLIES"])
    combine("TOTAL_INPUT", ["INPUT_CAPITAL", "INPUT_IMPORTS", "INPUT_OTHER"])
    combine("TOTAL_OUTPUT", ["OUTPUT_TAX", "IMPORTED_SERVICES_TAX"])
    combine("NET_VAT", ["TOTAL_OUTPUT", "TOTAL_INPUT"], [1, -1])

    for line in lines.values():
        for ccy in currencies:
            line.by_currency.setdefault(ccy, ZERO)

    fx_list = sorted(fx_used.values(), key=lambda f: (f["from"], f["date"]))
    return ReturnResult(business.id, period, rep, currencies, lines, calcs, fx_list, taxable_share, claim_ratio)


def totals_by_month(result: ReturnResult) -> dict[str, dict[str, int]]:
    """Count of sales and purchases per month, used for missing-data checks."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"sale": 0, "purchase": 0})
    for c in result.calcs.values():
        out[c.txn.txn_date.strftime("%Y-%m")][c.txn.direction] += 1
    return out
