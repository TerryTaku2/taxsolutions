from datetime import date

from vatsys.periods import next_deadline, parse_period, period_for, periods_covering


def test_monthly():
    p = period_for(date(2025, 9, 17), "monthly")
    assert (p.start, p.end, p.due_date) == (date(2025, 9, 1), date(2025, 9, 30), date(2025, 10, 25))


def test_bimonthly_odd_and_even():
    odd = period_for(date(2025, 2, 10), "bimonthly_odd")
    assert (odd.start, odd.end) == (date(2025, 2, 1), date(2025, 3, 31))
    even = period_for(date(2025, 2, 10), "bimonthly_even")
    assert (even.start, even.end) == (date(2025, 1, 1), date(2025, 2, 28))
    # Period spanning a year end
    dec = period_for(date(2025, 12, 5), "bimonthly_odd")
    assert (dec.start, dec.end, dec.due_date) == (date(2025, 12, 1), date(2026, 1, 31), date(2026, 2, 25))


def test_next_deadline():
    assert next_deadline(date(2025, 10, 4), "monthly").start == date(2025, 9, 1)
    assert next_deadline(date(2025, 10, 25), "monthly").start == date(2025, 9, 1)
    assert next_deadline(date(2025, 10, 26), "monthly").start == date(2025, 10, 1)


def test_parse_period_rejects_mid_period():
    assert parse_period("2025-09-01", "monthly").end == date(2025, 9, 30)
    try:
        parse_period("2025-01-01", "bimonthly_odd")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_periods_covering():
    ps = periods_covering(date(2025, 8, 15), date(2025, 10, 2), "monthly")
    assert [p.start.month for p in ps] == [8, 9, 10]
