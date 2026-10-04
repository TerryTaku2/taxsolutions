"""Tax periods and filing deadlines.

Returns and payment are due on the 25th of the month after the period ends.
"""

import calendar
from dataclasses import dataclass
from datetime import date

DUE_DAY = 25


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    @property
    def key(self) -> str:
        return self.start.isoformat()

    @property
    def due_date(self) -> date:
        year, month = (self.end.year + 1, 1) if self.end.month == 12 else (self.end.year, self.end.month + 1)
        return date(year, month, DUE_DAY)

    @property
    def label(self) -> str:
        if self.start.month == self.end.month:
            return self.start.strftime("%B %Y")
        if self.start.year == self.end.year:
            return f"{self.start:%b}–{self.end:%b %Y}"
        return f"{self.start:%b %Y}–{self.end:%b %Y}"


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + n
    return idx // 12, idx % 12 + 1


def period_for(day: date, frequency: str) -> Period:
    if frequency == "monthly":
        return Period(day.replace(day=1), _month_end(day.year, day.month))
    # Two-monthly: find the month in which the period ends.
    end_parity = 1 if frequency == "bimonthly_odd" else 0
    end_y, end_m = (day.year, day.month) if day.month % 2 == end_parity else _add_months(day.year, day.month, 1)
    start_y, start_m = _add_months(end_y, end_m, -1)
    return Period(date(start_y, start_m, 1), _month_end(end_y, end_m))


def parse_period(key: str, frequency: str) -> Period:
    start = date.fromisoformat(key)
    period = period_for(start, frequency)
    if period.start != start:
        raise ValueError(f"{key} is not the start of a {frequency} period")
    return period


def next_period(period: Period, frequency: str) -> Period:
    y, m = _add_months(period.end.year, period.end.month, 1)
    return period_for(date(y, m, 1), frequency)


def previous_period(period: Period, frequency: str) -> Period:
    y, m = _add_months(period.start.year, period.start.month, -1)
    return period_for(date(y, m, 1), frequency)


def periods_covering(first: date, last: date, frequency: str) -> list[Period]:
    out = []
    p = period_for(first, frequency)
    while p.start <= last:
        out.append(p)
        p = next_period(p, frequency)
    return out


def next_deadline(today: date, frequency: str) -> Period:
    """The earliest period whose return is due today or later."""
    p = previous_period(period_for(today, frequency), frequency)
    while p.due_date < today:
        p = next_period(p, frequency)
    return p
