"""Lookups for VAT rates and exchange rates, loaded once per computation."""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ZERO_RATED, ExchangeRate, VatRate


class MissingRate(Exception):
    pass


class VatRateBook:
    def __init__(self, session: Session):
        self.rates = list(session.scalars(select(VatRate).order_by(VatRate.effective_from)))

    def rate_for(self, category: str, on: date) -> Decimal:
        for r in self.rates:
            if r.category == category and r.effective_from <= on and (r.effective_to is None or on <= r.effective_to):
                return r.rate
        if category == ZERO_RATED:
            return Decimal("0")
        raise MissingRate(f"No {category} VAT rate configured for {on.isoformat()}")


@dataclass
class Conversion:
    amount: Decimal
    rate: Decimal
    rate_date: date | None
    source: str | None


class FxBook:
    """Exchange rates; uses the latest published rate on or before the transaction date."""

    def __init__(self, session: Session):
        self.series: dict[tuple[str, str], tuple[list[date], list[ExchangeRate]]] = {}
        for r in session.scalars(select(ExchangeRate).order_by(ExchangeRate.rate_date)):
            dates, rows = self.series.setdefault((r.base, r.quote), ([], []))
            dates.append(r.rate_date)
            rows.append(r)

    def _lookup(self, base: str, quote: str, on: date) -> ExchangeRate | None:
        if (base, quote) not in self.series:
            return None
        dates, rows = self.series[(base, quote)]
        i = bisect_right(dates, on)
        return rows[i - 1] if i else None

    def convert(self, amount: Decimal, from_ccy: str, to_ccy: str, on: date) -> Conversion:
        """Convert `amount`. The returned rate is always units of `to_ccy` per 1 `from_ccy`."""
        if from_ccy == to_ccy:
            return Conversion(amount, Decimal("1"), None, None)
        direct = self._lookup(from_ccy, to_ccy, on)
        if direct:
            return Conversion(amount * direct.rate, direct.rate, direct.rate_date, direct.source)
        inverse = self._lookup(to_ccy, from_ccy, on)
        if inverse:
            return Conversion(amount / inverse.rate, Decimal(1) / inverse.rate, inverse.rate_date, inverse.source)
        raise MissingRate(f"No {from_ccy}/{to_ccy} exchange rate on or before {on.isoformat()}")
