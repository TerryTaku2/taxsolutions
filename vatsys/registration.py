"""Registration threshold tracking for businesses not yet registered for VAT.

A person must register when taxable supplies in any 12 months exceed the
threshold, and apply within 30 days of becoming liable (s23).
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .models import SALE, STANDARD, ZERO_RATED, Business, Transaction
from .money import ZERO, q2
from .rates import FxBook, MissingRate

THRESHOLD_CURRENCY = "USD"


@dataclass
class RegistrationStatus:
    window_start: date
    window_end: date
    turnover: Decimal  # taxable supplies in USD over the window
    threshold: Decimal
    unconverted: int  # sales left out because no exchange rate was found

    @property
    def share(self) -> Decimal:
        return self.turnover / self.threshold if self.threshold else ZERO

    @property
    def must_register(self) -> bool:
        return self.turnover > self.threshold

    @property
    def approaching(self) -> bool:
        return not self.must_register and self.share >= config.REGISTRATION_WARNING_SHARE


def rolling_turnover(session: Session, business: Business, today: date) -> RegistrationStatus:
    """Taxable (standard and zero-rated) sales in the 12 months up to today, converted to USD."""
    start = today - timedelta(days=364)
    sales = session.scalars(select(Transaction).where(
        Transaction.business_id == business.id, Transaction.excluded.is_(False), Transaction.direction == SALE,
        Transaction.vat_category.in_([STANDARD, ZERO_RATED]),
        Transaction.txn_date >= start, Transaction.txn_date <= today))
    fx = FxBook(session)
    total, missing = ZERO, 0
    for t in sales:
        try:
            total += fx.convert(t.amount, t.currency, THRESHOLD_CURRENCY, t.txn_date).amount
        except MissingRate:
            missing += 1
    return RegistrationStatus(start, today, q2(total), config.REGISTRATION_THRESHOLD, missing)
