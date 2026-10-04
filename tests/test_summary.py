"""Plain-language summary of what is owed and when."""

import re
from datetime import date
from decimal import Decimal

from tests.test_compute import add
from tests.test_web import register
from vatsys.models import VatReturn
from vatsys.periods import period_for
from vatsys.summary import business_summaries, summarise

D = Decimal
SEP = period_for(date(2026, 9, 1), "monthly")  # due Sunday 25 Oct 2026


def test_payable(session, business):
    add(session, business, amount=D("1386.00"))  # 186.00 output tax
    add(session, business, direction="purchase", amount=D("231.00"))  # 31.00 input tax
    s = summarise(session, business, SEP, date(2026, 10, 4))
    assert s.status == "payable" and s.payable == {"USD": D("155.00")} and not s.attention
    assert s.headline == "You will pay USD 155.00 to ZIMRA by Sunday 25 October 2026."
    assert s.days_left == 21 and s.tone == "info"


def test_attention_makes_amount_approximate(session, business):
    add(session, business, amount=D("1155.00"), reviewed=False, confidence=0.5)
    s = summarise(session, business, SEP, date(2026, 10, 4))
    assert s.headline.startswith("You will pay about USD 155.00")
    assert [a.code for a in s.attention] == ["needs_review", "missing_month"]
    assert s.tone == "warning" and "2 things need your attention" in s.details[-1]


def test_two_currencies_cannot_offset(session, business, fx):
    add(session, business, amount=D("1155.00"))
    add(session, business, direction="purchase", amount=D("28875.00"), currency="ZWG")
    s = summarise(session, business, SEP, date(2026, 10, 4))
    assert s.payable == {"USD": D("155.00")} and s.refundable == {"ZWG": D("3875.00")}
    assert any("different currencies" in d for d in s.details)
    assert any("own currency" in d for d in s.details)


def test_small_refund_carried_forward(session, business):
    add(session, business, amount=D("115.50"))
    add(session, business, direction="purchase", amount=D("462.00"))
    s = summarise(session, business, SEP, date(2026, 10, 4))
    assert s.status == "refund" and "ZIMRA owes you USD 46.50" in s.headline
    assert any("carried forward" in d for d in s.details)


def test_no_data_and_nil_return(session, business):
    s = summarise(session, business, SEP, date(2026, 10, 4))
    assert s.status == "no_data" and "Upload your September 2026" in s.headline
    assert "nil return" in s.details[0]


def test_overdue_shows_cost(session, business):
    add(session, business, amount=D("1155.00"))
    s = summarise(session, business, SEP, date(2026, 11, 4))
    assert s.overdue and s.tone == "error"
    assert any("due 10 day(s) ago" in d and "US$300.00" in d and "USD 155.00" in d for d in s.details)


def test_last_days_warning_and_filed(session, business):
    add(session, business, amount=D("1155.00"))
    s = summarise(session, business, SEP, date(2026, 10, 24))
    assert "tomorrow" in s.headline and s.tone == "warning" and any("1 day(s) left" in d for d in s.details)
    session.add(VatReturn(business_id=business.id, period_start=SEP.start, period_end=SEP.end,
                          status="finalised", snapshot={}))
    session.flush()
    s = summarise(session, business, SEP, date(2026, 11, 4))
    assert s.headline.startswith("Return filed. Pay USD 155.00") and s.tone == "success" and not s.overdue


def test_running_estimate_for_current_period(session, business):
    add(session, business, amount=D("1155.00"))
    add(session, business, amount=D("231.00"), txn_date=date(2026, 10, 2))
    due, running = business_summaries(session, business, date(2026, 10, 4))
    assert (due.period.label, running.period.label) == ("September 2026", "October 2026")
    assert running.headline == "So far in October 2026 you would pay about USD 31.00."
    assert "1 sale(s) and 0 purchase(s)" in running.details[0]
    # After the September deadline, October is the next return and there's no separate running card
    assert len(business_summaries(session, business, date(2026, 10, 26))) == 1


def test_dashboard_shows_summary(client):
    token = register(client)
    r = client.post("/businesses/new", data={"csrf": token, "name": "Shop"})
    bid = int(re.search(r"/b/(\d+)", str(r.url)).group(1))
    r = client.get(f"/b/{bid}")
    assert "Upload your" in r.text and "owe-headline" in r.text


def test_dashboard_warns_about_unfiled_past_returns(client):
    token = register(client)
    r = client.post("/businesses/new", data={"csrf": token, "name": "Shop"})
    bid = int(re.search(r"/b/(\d+)", str(r.url)).group(1))
    client.post(f"/b/{bid}/transactions/new", data={"csrf": token, "txn_date": "2020-03-05", "direction": "sale",
                                                    "description": "Old sale", "amount": "115", "currency": "USD",
                                                    "amount_includes_vat": "yes"})
    assert "The March 2020 return was due on 25 April 2020" in client.get(f"/b/{bid}").text
