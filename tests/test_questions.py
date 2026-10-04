"""Plain-language questions for transactions the rules couldn't classify."""

import re
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from tests.test_compute import add
from tests.test_web import register
from vatsys.ingest import classifier_for
from vatsys.models import AuditLog, ClassificationRule, User
from vatsys.questions import ANSWERS, QUESTIONS, apply_answer, pending_groups

D = Decimal


def unreviewed(session, business, **kw):
    return add(session, business, reviewed=False, confidence=0.5, classification_source="default", **kw)


def test_groups_by_party_then_description(session, business):
    unreviewed(session, business, counterparty="Zuva Fuels", description="Diesel 2000 litres", direction="purchase")
    unreviewed(session, business, counterparty="ZUVA FUELS ", description="Diesel 500 litres", direction="purchase")
    unreviewed(session, business, counterparty=None, description="POS 4411 Spar Avondale 02/09")
    unreviewed(session, business, counterparty=None, description="POS 9917 Spar Avondale 15/09")
    unreviewed(session, business, counterparty="Zuva Fuels", description="Fuel refund")  # a sale: separate group
    add(session, business, description="Already reviewed")
    groups = pending_groups(session, business)
    # Largest groups first, then alphabetical
    assert [(g.direction, g.title, len(g.txns)) for g in groups] == [
        ("sale", "POS 4411 Spar Avondale 02/09", 2), ("purchase", "Zuva Fuels", 2), ("sale", "Zuva Fuels", 1)]
    assert groups[1].totals == {"USD": D("0")}  # add() defaults amounts to 0
    assert groups[1].question == "What did you pay for?"


def test_locked_periods_are_left_out(session, business):
    unreviewed(session, business, description="x")
    assert pending_groups(session, business, [(date(2026, 9, 1), date(2026, 9, 30))]) == []


def test_every_answer_is_a_valid_classification():
    from vatsys.models import INPUT_TYPES, VAT_CATEGORIES

    for direction, (_, answers) in QUESTIONS.items():
        for a in answers:
            assert a.category in VAT_CATEGORIES and a.input_type in INPUT_TYPES
            assert ANSWERS[direction][a.key] is a


def test_answer_classifies_group_and_remembers(session, business):
    user = session.get(User, business.owner_id)
    unreviewed(session, business, counterparty=None, description="Payment Lusaka Copper 4411", direction="sale")
    unreviewed(session, business, counterparty=None, description="Payment Lusaka Copper 9917", direction="sale")
    group = pending_groups(session, business)[0]
    assert apply_answer(session, business, user, group, "export", remember=True) == 2
    session.flush()
    assert all((t.vat_category, t.reviewed, t.classification_source) == ("zero", True, "user") for t in group.txns)
    assert "answered: Goods exported" in group.txns[0].classification_reason
    assert pending_groups(session, business) == []
    assert session.scalar(select(AuditLog.id).where(AuditLog.action == "answer_question"))

    rule = session.scalar(select(ClassificationRule).where(ClassificationRule.business_id == business.id))
    c = classifier_for(session, business.id).classify(direction="sale", description="Payment Lusaka Copper 1234",
                                                     counterparty=None, on=date(2026, 10, 1))
    assert (c.vat_category, c.rule_id) == ("zero", rule.id)


def test_party_rule_and_input_type(session, business):
    user = session.get(User, business.owner_id)
    unreviewed(session, business, counterparty="Netsuite Ltd", description="Licence", direction="purchase")
    group = pending_groups(session, business)[0]
    apply_answer(session, business, user, group, "foreign_service", remember=True)
    session.flush()
    t = group.txns[0]
    assert (t.vat_category, t.input_type) == ("standard", "imported_service")
    c = classifier_for(session, business.id).classify(direction="purchase", description="Annual renewal",
                                                     counterparty="netsuite ltd", on=date(2026, 10, 1))
    assert c.input_type == "imported_service"


def test_unknown_answer_rejected(session, business):
    user = session.get(User, business.owner_id)
    unreviewed(session, business, description="x")
    group = pending_groups(session, business)[0]
    try:
        apply_answer(session, business, user, group, "capital", remember=False)  # a purchase answer, for a sale
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_review_page_flow(client):
    token = register(client)
    r = client.post("/businesses/new", data={"csrf": token, "name": "Shop"})
    bid = int(re.search(r"/b/(\d+)", str(r.url)).group(1))
    sales = b"Date,Description,Customer,Amount\n2026-09-02,Delivery,Moyo Stores,115.50\n" \
            b"2026-09-09,Delivery,Moyo Stores,231.00\n"
    client.post(f"/b/{bid}/uploads", data={"csrf": token, "source_type": "ledger", "default_direction": "sale",
                                           "default_currency": "USD", "amounts_include_vat": "yes"},
                files={"file": ("sales.csv", sales)})
    r = client.get(f"/b/{bid}/review")
    assert "What was this money for?" in r.text and "Moyo Stores" in r.text and "2 transactions" in r.text
    group = re.search(r'name="group" value="([^"]+)"', r.text).group(1)
    r = client.post(f"/b/{bid}/review", data={"csrf": token, "group": group, "answer": "local_sale",
                                              "remember": "yes"})
    assert "Saved for 2 transactions from Moyo Stores" in r.text and "No questions right now" in r.text
    # The same group posted again is reported, not applied twice
    r = client.post(f"/b/{bid}/review", data={"csrf": token, "group": group, "answer": "export"})
    assert "already been answered" in r.text
