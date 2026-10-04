"""Estimates of what a late return and late payment may cost.

- Late payment: a penalty equal to the unpaid tax, plus interest at the
  prescribed rate for each month or part month from the 1st of the month after
  the due date (s39(2)).
- Late return: a civil penalty per day late, for up to 181 days (s62(2)).

ZIMRA may waive penalties and interest where there was no intent to avoid tax
(s39(5)), so these are estimates of the exposure, not assessments.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import config
from .money import ZERO, q2
from .periods import Period


@dataclass
class CurrencyExposure:
    tax: Decimal
    penalty: Decimal
    interest: Decimal | None  # None when no interest rate is configured

    @property
    def total(self) -> Decimal:
        return self.tax + self.penalty + (self.interest or ZERO)


@dataclass
class PenaltyEstimate:
    days_late: int
    interest_months: int
    late_return_penalty: Decimal  # in USD
    by_currency: dict[str, CurrencyExposure] = field(default_factory=dict)


def interest_months(due: date, today: date) -> int:
    """Months or part months from the 1st of the month after the due date, up to today."""
    start_y, start_m = (due.year + 1, 1) if due.month == 12 else (due.year, due.month + 1)
    if today < date(start_y, start_m, 1):
        return 0
    return (today.year - start_y) * 12 + today.month - start_m + 1


def estimate(period: Period, net_by_currency: dict[str, Decimal], today: date) -> PenaltyEstimate | None:
    """Exposure if the return and payment were made today; None if the return isn't late yet."""
    if today <= period.due_date:
        return None
    days = (today - period.due_date).days
    months = interest_months(period.due_date, today)
    out = PenaltyEstimate(days, months, config.LATE_RETURN_PENALTY_PER_DAY * min(days, config.LATE_RETURN_MAX_DAYS))
    for ccy, net in net_by_currency.items():
        if net <= 0:
            continue
        interest = None
        if config.LATE_INTEREST_RATE is not None:
            interest = q2(net * config.LATE_INTEREST_RATE / 100 / 12 * months)
        out.by_currency[ccy] = CurrencyExposure(net, net, interest)
    return out
