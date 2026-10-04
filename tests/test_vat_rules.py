"""Rules from the VAT Rules Guide (section numbers refer to the VAT Act)."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from tests.test_compute import add, compute
from vatsys import config
from vatsys.checks import run_checks
from vatsys.classify import Classifier
from vatsys.models import ClassificationRule, ExchangeRate, VatRate, VatReturn
from vatsys.money import split_gross, vat_on_net
from vatsys.penalties import estimate, interest_months
from vatsys.periods import period_for
from vatsys.registration import rolling_turnover
from vatsys.seed import seed_defaults

D = Decimal


def test_rate_history_follows_2026_budget(session):
    rates = {r.effective_from: r.rate for r in session.scalars(select(VatRate).where(VatRate.category == "standard"))}
    assert rates[date(2023, 1, 1)] == D("15")
    assert rates[date(2026, 1, 1)] == D("15.5")
    assert date(2025, 1, 1) not in rates


def test_old_rate_seed_is_corrected(session):
    for r in session.scalars(select(VatRate)):
        session.delete(r)
    session.flush()
    session.add_all([VatRate(category="standard", rate=D("15"), effective_from=date(2023, 1, 1),
                             effective_to=date(2024, 12, 31)),
                     VatRate(category="standard", rate=D("15.5"), effective_from=date(2025, 1, 1))])
    session.flush()
    seed_defaults(session)
    rates = sorted((r.effective_from, r.effective_to, r.rate) for r in session.scalars(select(VatRate)))
    assert rates == [(date(2023, 1, 1), date(2025, 12, 31), D("15")), (date(2026, 1, 1), None, D("15.5"))]


def test_old_rules_are_upgraded_once(session):
    old = r"\b(bank charges?|service fees?|ledger fees?|bank commission|interest)\b"
    for r in session.scalars(select(ClassificationRule)):
        session.delete(r)
    session.add(ClassificationRule(business_id=None, pattern=old, match_type="regex", field="description",
                                   direction="any", vat_category="exempt", priority=20))
    session.flush()
    seed_defaults(session)
    rules = list(session.scalars(select(ClassificationRule)))
    assert not next(r for r in rules if r.pattern == old).active
    count = len(rules)
    seed_defaults(session)
    assert len(list(session.scalars(select(ClassificationRule)))) == count


def test_rounding_favours_taxpayer():
    # 0.10 at 15% is exactly 0.015: output tax rounds down, input tax up (s71).
    assert vat_on_net(D("0.10"), D("15"), favour_lower=True)[1] == D("0.01")
    assert vat_on_net(D("0.10"), D("15"), favour_lower=False)[1] == D("0.02")
    assert split_gross(D("115.50"), D("15.5"))[1] == D("15.50")  # tax fraction 15.5/115.5


def _classify(session, text, direction="purchase", on=None):
    c = Classifier(list(session.scalars(select(ClassificationRule))))
    r = c.classify(direction=direction, description=text, counterparty=None, on=on)
    return r.vat_category, r.input_type


def test_budget_2026_rules_apply_by_date(session):
    assert _classify(session, "Monthly bank charges", on=date(2025, 12, 31)) == ("exempt", "general")
    assert _classify(session, "Monthly bank charges", on=date(2026, 1, 1)) == ("standard", "general")
    assert _classify(session, "Interest earned", on=date(2026, 3, 1)) == ("exempt", "general")
    assert _classify(session, "Sale of business as a going concern", "sale", date(2025, 6, 1))[0] == "zero"
    assert _classify(session, "Sale of business as a going concern", "sale", date(2026, 6, 1))[0] == "standard"
    assert _classify(session, "Netflix subscription", on=date(2026, 2, 1))[0] == "out_of_scope"
    assert _classify(session, "Sunflower seeds 30t", on=date(2026, 2, 1))[0] == "exempt"


def test_blocked_inputs(session):
    for text in ("Client entertainment", "Golf club membership", "Passenger motor car", "Lithium export tax"):
        assert _classify(session, text, on=date(2026, 5, 1)) == ("standard", "non_deductible"), text


def test_mixed_use_apportioned_below_90_percent(session, business):
    add(session, business, amount=D("924.00"))  # net 800.00
    add(session, business, amount=D("200"), vat_category="exempt")
    add(session, business, direction="purchase", amount=D("1155.00"), input_type="mixed")  # VAT 155.00
    r = compute(session, business)
    assert r.taxable_share == D("0.8") and r.claim_ratio == D("0.8")
    assert r.lines["INPUT_OTHER"].total == D("124.00")
    assert r.lines["NON_DEDUCTIBLE"].total == D("31.00")
    assert any(i.code == "apportionment" for i in run_checks(session, r))


def test_mixed_use_claimed_in_full_at_90_percent(session, business):
    add(session, business, amount=D("1039.50"))  # net 900.00
    add(session, business, amount=D("100"), vat_category="exempt")
    add(session, business, direction="purchase", amount=D("1155.00"), input_type="mixed")
    r = compute(session, business)
    assert r.taxable_share == D("0.9") and r.lines["INPUT_OTHER"].total == D("155.00")


def test_exempt_supplies_warn_about_apportionment(session, business):
    add(session, business, amount=D("115.50"))
    add(session, business, amount=D("900"), vat_category="exempt")
    add(session, business, direction="purchase", amount=D("115.50"))
    issue = next(i for i in run_checks(session, compute(session, business)) if i.code == "apportionment")
    assert issue.severity == "warning"


def test_imported_services_self_accounted(session, business):
    add(session, business, direction="purchase", amount=D("1000"), input_type="imported_service",
        counterparty_vat_number=None)
    r = compute(session, business)
    assert r.lines["IMPORTED_SERVICES_TAX"].total == D("155.00")
    assert r.lines["TOTAL_OUTPUT"].total == D("155.00")
    assert r.lines["NET_VAT"].total == D("155.00")
    codes = {i.code for i in run_checks(session, r)}
    assert "imported_services" in codes and "missing_tax_invoice" not in codes


def test_currencies_are_not_set_off(session, business, fx):
    add(session, business, amount=D("1155.00"))  # USD 155.00 payable
    add(session, business, direction="purchase", amount=D("28875.00"), currency="ZWG")  # ZWG 3875.00 claim
    r = compute(session, business)
    assert r.net_by_currency() == {"USD": D("155.00"), "ZWG": D("-3875.00")}
    issue = next(i for i in run_checks(session, r) if i.code == "currency_setoff")
    assert issue.severity == "warning" and "155.00 USD" in issue.detail


def test_small_refund_carried_forward(session, business):
    add(session, business, amount=D("115.50"))
    add(session, business, direction="purchase", amount=D("462.00"))  # net -46.50
    assert "small_refund" in {i.code for i in run_checks(session, compute(session, business))}


def test_invoice_claimed_in_another_period(session, business):
    add(session, business, amount=D("115.50"))
    add(session, business, direction="purchase", amount=D("115.50"), invoice_number="T-1", counterparty="Tyre World")
    add(session, business, direction="purchase", amount=D("115.50"), invoice_number="t-1 ", counterparty="Tyre World",
        txn_date=date(2026, 8, 5))
    assert "claimed_elsewhere" in {i.code for i in run_checks(session, compute(session, business))}


def test_rate_change_inside_period(session, business):
    business.filing_frequency = "bimonthly_odd"  # Dec 2025 - Jan 2026
    add(session, business, amount=D("115"), txn_date=date(2025, 12, 10))
    r = compute(session, business, date(2025, 12, 1))
    assert "rate_change" in {i.code for i in run_checks(session, r)}


def test_interest_months():
    due = date(2026, 10, 25)
    assert interest_months(due, date(2026, 10, 31)) == 0
    assert interest_months(due, date(2026, 11, 1)) == 1
    assert interest_months(due, date(2027, 1, 15)) == 3


def test_penalty_estimate(monkeypatch):
    monkeypatch.setattr(config, "LATE_INTEREST_RATE", D("12"))
    p = period_for(date(2026, 9, 1), "monthly")  # due 25 Oct 2026
    assert estimate(p, {"USD": D("1000")}, date(2026, 10, 25)) is None
    est = estimate(p, {"USD": D("1000"), "ZWG": D("-50")}, date(2026, 11, 4))
    assert est.days_late == 10 and est.late_return_penalty == D("300")
    usd = est.by_currency["USD"]
    assert (usd.penalty, usd.interest) == (D("1000"), D("10.00"))
    assert "ZWG" not in est.by_currency
    assert estimate(p, {}, date(2027, 9, 1)).late_return_penalty == D("5430")  # capped at 181 days


def test_late_check_until_finalised(session, business):
    add(session, business, amount=D("1155.00"))
    r = compute(session, business)
    late = date(2026, 11, 4)
    assert "late" in {i.code for i in run_checks(session, r, late)}
    assert "late" not in {i.code for i in run_checks(session, r, date(2026, 10, 20))}
    session.add(VatReturn(business_id=business.id, period_start=r.period.start, period_end=r.period.end,
                          status="finalised", snapshot={}))
    session.flush()
    assert "late" not in {i.code for i in run_checks(session, r, late)}


def test_registration_threshold(session, business, fx):
    add(session, business, amount=D("16000"), txn_date=date(2026, 3, 1))
    add(session, business, amount=D("125000"), currency="ZWG", txn_date=date(2026, 9, 20))  # 4807.69 USD at 26
    add(session, business, amount=D("9000"), vat_category="exempt", txn_date=date(2026, 9, 2))
    add(session, business, amount=D("50000"), txn_date=date(2025, 9, 1))  # outside the 12 months
    st = rolling_turnover(session, business, date(2026, 10, 4))
    assert st.turnover == D("20807.69") and st.approaching and not st.must_register
    add(session, business, amount=D("6000"), txn_date=date(2026, 10, 1))
    assert rolling_turnover(session, business, date(2026, 10, 4)).must_register


def test_registration_skips_unconverted(session, business):
    session.add(ExchangeRate(rate_date=date(2026, 9, 1), base="USD", quote="ZWG", rate=D("25"), source="t"))
    add(session, business, amount=D("100"), currency="ZWG", txn_date=date(2026, 8, 1))
    assert rolling_turnover(session, business, date(2026, 10, 4)).unconverted == 1
