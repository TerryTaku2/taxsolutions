"""Default VAT rates and classification rules.

These are starting points stored in the database, not fixed code: an admin can
edit them in Settings when the law changes. They follow the VAT Rules Guide
(VAT Act [Chapter 23:12] to 1 Dec 2024, and the 2026 National Budget Statement).
Budget measures only take effect once enacted in a Finance Act, so confirm every
default before relying on it.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    EXEMPT,
    INPUT_CAPITAL,
    INPUT_IMPORT,
    INPUT_NON_DEDUCTIBLE,
    OUT_OF_SCOPE,
    PURCHASE,
    SALE,
    STANDARD,
    ZERO_RATED,
    ClassificationRule,
    VatRate,
)

BUDGET_2026 = date(2026, 1, 1)
BEFORE_2026 = date(2025, 12, 31)

DEFAULT_RATES = [
    (STANDARD, "14.5", date(2020, 1, 1), date(2022, 12, 31), "Standard rate 2020-2022"),
    (STANDARD, "15", date(2023, 1, 1), BEFORE_2026, "Standard rate 2023-2025"),
    (STANDARD, "15.5", BUDGET_2026, None, "Standard rate from 1 Jan 2026 (2026 Budget; confirm Finance Act)"),
    (ZERO_RATED, "0", date(2000, 1, 1), None, "Zero rate"),
]

NOTE = "Default rule; confirm with your tax adviser"

# (priority, field, pattern (regex), direction, category, input_type, note, effective_from, effective_to)
DEFAULT_RULES = [
    (10, "description", r"\b(salar(y|ies)|wages?|nssa|paye|pension contribution)\b", "any", OUT_OF_SCOPE, None,
     "Payroll is outside VAT", None, None),
    (10, "description", r"\b(zimra|vat payment|tax payment|aids levy|imtt|intermediated money transfer)\b", "any",
     OUT_OF_SCOPE, None, "Tax payments are outside VAT", None, None),
    (10, "description", r"\b(own account|internal transfer|inter-?account|drawings|loan (drawdown|repayment)|"
     r"capital injection|dividends?)\b", "any", OUT_OF_SCOPE, None, "Funding movements are not supplies", None, None),
    (15, "any", r"\b(netflix|spotify|starlink|uber|bolt|indrive|showmax|amazon prime|youtube premium|google play|"
     r"app store)\b", PURCHASE, OUT_OF_SCOPE, None,
     "Offshore digital service: the bank deducts Digital Services Withholding Tax instead of VAT (2026 Budget)",
     BUDGET_2026, None),
    (20, "description", r"\binterest\b", "any", EXEMPT, None, "Interest is an exempt financial service (s11)",
     None, None),
    (20, "description", r"\b(bank charges?|service fees?|ledger fees?|bank commission)\b", "any", EXEMPT, None,
     "Financial services are exempt (s11)", None, BEFORE_2026),
    (20, "description", r"\b(bank charges?|service fees?|ledger fees?|bank commission)\b", PURCHASE, STANDARD, None,
     "Financial services to corporates are standard-rated from 1 Jan 2026 (2026 Budget)", BUDGET_2026, None),
    (20, "description", r"\b(residential (rent|lease)|rent.*(house|flat|residential))\b", "any", EXEMPT, None,
     "Residential rent is exempt (s11)", None, None),
    (20, "description", r"\b(school fees|tuition)\b", "any", EXEMPT, None,
     "Education by a registered institution is exempt (s11)", None, None),
    (20, "description", r"\b(medical (services?|consultations?|fees)|doctors? fees|hospital (fees|bills?)|"
     r"clinic fees)\b", "any", EXEMPT, None, "Medical services are exempt (s11)", None, None),
    (20, "description", r"\b(bus fares?|passenger fares?|kombi fares?|commuter omnibus|rail passenger|"
     r"train tickets?)\b", "any", EXEMPT, None, "Road and rail passenger transport is exempt (s11)", None, None),
    (20, "description", r"\b(sunflower seeds?|oil ?seeds?)\b", PURCHASE, EXEMPT, None,
     "Inputs for making cooking oil are exempt from 1 Jan 2026 (2026 Budget)", BUDGET_2026, None),
    (30, "description", r"\b(exports?|exported)\b", SALE, ZERO_RATED, None,
     "Exports of goods are zero-rated (s10); keep export documents", None, None),
    (30, "description", r"\b(cross[- ]border|international (freight|haulage|transport)|"
     r"transit (freight|cargo|load))\b", SALE, ZERO_RATED, None,
     "International transport of goods is zero-rated (s10); keep proof", None, None),
    (30, "description", r"\b(non-?resident|offshore client|foreign client)\b", SALE, ZERO_RATED, None,
     "Services to non-residents outside Zimbabwe are zero-rated (s10); keep proof", None, None),
    (30, "description", r"\bgoing concern\b", SALE, ZERO_RATED, None,
     "Sale of a going concern between registered operators is zero-rated (s10)", None, BEFORE_2026),
    (30, "description", r"\bgoing concern\b", SALE, STANDARD, None,
     "Going concern sales are standard-rated from 1 Jan 2026, except to a government-owned entity (2026 Budget)",
     BUDGET_2026, None),
    (5, "description", r"\b(customs|import vat|bill of entry)\b", PURCHASE, STANDARD, INPUT_IMPORT,
     "VAT paid on imports; keep the bill of entry (s15(2))", None, None),
    (40, "description", r"\b(entertainment|entertaining)\b", PURCHASE, STANDARD, INPUT_NON_DEDUCTIBLE,
     "Input tax on entertainment is blocked (s16(2))", None, None),
    (40, "description", r"\b((club|gym|golf|sports?|sporting|social) (membership|subscriptions?|fees)|"
     r"membership fees?)\b", PURCHASE, STANDARD, INPUT_NON_DEDUCTIBLE,
     "Input tax on club, sporting or social membership fees is blocked (s16(2))", None, None),
    (40, "description", r"\b(passenger (motor )?(car|vehicle)|motor car|sedan)\b", PURCHASE, STANDARD,
     INPUT_NON_DEDUCTIBLE,
     "Input tax on motor vehicles is blocked unless the regulations allow it or you are a motor dealer (s16(2))",
     None, None),
    (40, "description", r"\bexport (tax|duty|levy)\b", PURCHASE, STANDARD, INPUT_NON_DEDUCTIBLE,
     "Export taxes (lithium, hides, platinum, dimensional stone, cannabis) are not input tax (s16(2))", None, None),
    (50, "description", r"\b((truck|trailer|vehicle|horse|equipment|machinery) purchase|purchase of (a |an )?"
     r"(truck|trailer|vehicle|horse|machinery|equipment)|new (truck|trailer)|machinery|computers?|laptops?)\b",
     PURCHASE, STANDARD, INPUT_CAPITAL, "Capital goods", None, None),
]

# Patterns from the first release's defaults that the rules above replace.
REPLACED_PATTERNS = {
    r"\b(bank charges?|service fees?|ledger fees?|bank commission|interest)\b",
    r"\b(entertainment|club membership|passenger (motor )?(car|vehicle))\b",
}


def _fix_rate_history(session: Session) -> None:
    """The first release seeded 15.5% from 1 Jan 2025; the 2026 Budget sets it from 1 Jan 2026."""
    rates = list(session.scalars(select(VatRate).where(VatRate.category == STANDARD)))
    old_15 = [r for r in rates if r.rate == Decimal("15") and r.effective_to == date(2024, 12, 31)]
    old_155 = [r for r in rates if r.rate == Decimal("15.5") and r.effective_from == date(2025, 1, 1)]
    if len(old_15) == 1 and len(old_155) == 1:
        old_15[0].effective_to, old_15[0].note = BEFORE_2026, DEFAULT_RATES[1][4]
        old_155[0].effective_from, old_155[0].note = BUDGET_2026, DEFAULT_RATES[2][4]


def _add_rules(session: Session, skip: set) -> None:
    for priority, field, pattern, direction, category, input_type, note, start, end in DEFAULT_RULES:
        if (pattern, direction, category) in skip:
            continue
        session.add(ClassificationRule(
            business_id=None, priority=priority, field=field, match_type="regex",
            pattern=pattern, direction=direction, vat_category=category,
            input_type=input_type, note=f"{note}. {NOTE}", effective_from=start, effective_to=end,
        ))


def seed_defaults(session: Session) -> None:
    if session.scalar(select(VatRate.id).limit(1)) is None:
        for category, rate, start, end, note in DEFAULT_RATES:
            session.add(VatRate(category=category, rate=Decimal(rate), effective_from=start,
                                effective_to=end, note=note))
    else:
        _fix_rate_history(session)

    global_rules = list(session.scalars(select(ClassificationRule).where(ClassificationRule.business_id.is_(None))))
    if not global_rules:
        _add_rules(session, set())
        return
    # Upgrade a database seeded by the first release, once: retire the replaced
    # rules and add the new defaults (keeping any rule that already exists).
    replaced = [r for r in global_rules if r.pattern in REPLACED_PATTERNS and r.active]
    if replaced:
        for r in replaced:
            r.active = False
            r.note = f"Replaced by the 2026 default rules. {r.note or ''}".strip()
        _add_rules(session, {(r.pattern, r.direction, r.vat_category) for r in global_rules
                             if r.pattern not in REPLACED_PATTERNS})
