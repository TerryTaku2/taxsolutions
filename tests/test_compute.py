from datetime import date
from decimal import Decimal

from vatsys.checks import run_checks
from vatsys.compute import compute_return
from vatsys.money import split_gross
from vatsys.models import Transaction
from vatsys.periods import period_for

D = Decimal


def add(session, business, **kw):
    defaults = dict(business_id=business.id, txn_date=date(2026, 9, 10), description="x", direction="sale",
                    amount=D("0"), currency="USD", vat_category="standard", input_type="general",
                    classification_source="user", confidence=1.0, reviewed=True, amount_includes_vat=True,
                    invoice_number="INV1", counterparty_vat_number="123")
    defaults.update(kw)
    t = Transaction(**defaults)
    session.add(t)
    session.flush()
    return t


def test_split_gross():
    assert split_gross(D("115.50"), D("15.5")) == (D("100.00"), D("15.50"))
    assert split_gross(D("115"), D("15")) == (D("100.00"), D("15.00"))


def compute(session, business, d=date(2026, 9, 1)):
    return compute_return(session, business, period_for(d, business.filing_frequency))


def test_manual_example_matches(session, business, fx):
    """Worked example, computed by hand:

    Sales:     1155.00 USD standard  -> net 1000.00, VAT 155.00
               500.00 USD zero       -> 500.00
               200.00 USD exempt     -> 200.00
               -115.50 USD credit note -> net -100.00, VAT -15.50
    Purchases: 577.50 USD standard  -> VAT 77.50 (other)
               11550 USD capital     -> VAT 1550.00
               231 USD entertainment -> VAT 31.00, not deductible
               300 USD import, stated VAT 300 -> VAT 300.00
               50 USD exempt (bank charges) -> no VAT
               1000 USD out of scope (salaries)
    Output tax = 155.00 - 15.50 = 139.50
    Input tax  = 77.50 + 1550.00 + 300.00 = 1927.50
    Net        = 139.50 - 1927.50 = -1788.00 (refund)
    """
    add(session, business, amount=D("1155.00"))
    add(session, business, amount=D("500.00"), vat_category="zero")
    add(session, business, amount=D("200.00"), vat_category="exempt")
    add(session, business, amount=D("-115.50"), description="credit note")
    add(session, business, direction="purchase", amount=D("577.50"))
    add(session, business, direction="purchase", amount=D("11550"), input_type="capital")
    add(session, business, direction="purchase", amount=D("231"), input_type="non_deductible")
    add(session, business, direction="purchase", amount=D("300"), input_type="import", stated_vat=D("300"))
    add(session, business, direction="purchase", amount=D("50"), vat_category="exempt")
    add(session, business, direction="purchase", amount=D("1000"), vat_category="out_of_scope")
    add(session, business, amount=D("999"), excluded=True)  # excluded rows are ignored
    add(session, business, amount=D("999"), txn_date=date(2026, 10, 1))  # next period

    r = compute(session, business)
    total = {code: line.total for code, line in r.lines.items()}
    assert total["SR_SUPPLIES"] == D("900.00")
    assert total["ZR_SUPPLIES"] == D("500.00")
    assert total["EX_SUPPLIES"] == D("200.00")
    assert total["TOTAL_SUPPLIES"] == D("1600.00")
    assert total["OUTPUT_TAX"] == D("139.50")
    assert total["INPUT_OTHER"] == D("77.50")
    assert total["INPUT_CAPITAL"] == D("1550.00")
    assert total["INPUT_IMPORTS"] == D("300.00")
    assert total["TOTAL_INPUT"] == D("1927.50")
    assert total["NET_VAT"] == D("-1788.00")
    assert total["NON_DEDUCTIBLE"] == D("31.00")
    assert total["PURCHASES_NO_VAT"] == D("50")
    assert total["OUT_OF_SCOPE"] == D("1000")
    assert len(r.lines["OUTPUT_TAX"].txn_ids) == 2


def test_rate_change_uses_transaction_date(session, business):
    add(session, business, txn_date=date(2025, 12, 31), amount=D("115"))
    add(session, business, txn_date=date(2026, 1, 1), amount=D("115.50"))
    dec = compute(session, business, date(2025, 12, 1))
    jan = compute(session, business, date(2026, 1, 1))
    assert dec.lines["OUTPUT_TAX"].total == D("15.00")
    assert jan.lines["OUTPUT_TAX"].total == D("15.50")


def test_zwg_conversion_shows_rate_and_date(session, business, fx):
    t1 = add(session, business, amount=D("2887.50"), currency="ZWG", txn_date=date(2026, 9, 10))  # rate 25
    t2 = add(session, business, amount=D("2887.50"), currency="ZWG", txn_date=date(2026, 9, 20))  # rate 26
    r = compute(session, business)
    c1, c2 = r.calcs[t1.id], r.calcs[t2.id]
    assert c1.vat == D("387.50") and c1.fx_date == date(2026, 9, 1)
    assert c1.vat_rep == D("15.50")  # 387.50 / 25
    assert c2.fx_date == date(2026, 9, 15) and c2.vat_rep == D("14.90")  # 387.50 / 26 = 14.904
    assert r.lines["OUTPUT_TAX"].by_currency["ZWG"] == D("775.00")
    assert r.lines["OUTPUT_TAX"].total == D("30.40")
    assert {f["date"] for f in r.fx_used} == {date(2026, 9, 1), date(2026, 9, 15)}


def test_missing_fx_rate_is_an_error(session, business):
    add(session, business, amount=D("100"), currency="ZWG")
    r = compute(session, business)
    assert len(r.errors) == 1
    issues = run_checks(session, r)
    assert issues[0].severity == "error" and issues[0].code == "calc_error"


def test_checks_flag_problems(session, business, fx):
    add(session, business, amount=D("100"), reviewed=False, confidence=0.5, classification_source="default")
    add(session, business, direction="purchase", amount=D("115.50"), invoice_number=None)
    add(session, business, direction="purchase", amount=D("115.50"), invoice_number=None)
    add(session, business, direction="purchase", amount=D("231"), stated_vat=D("30"), invoice_number="X9")
    codes = {i.code for i in run_checks(session, compute(session, business))}
    assert {"needs_review", "missing_tax_invoice", "duplicates", "vat_mismatch"} <= codes


def test_empty_period(session, business):
    issues = run_checks(session, compute(session, business))
    assert [i.code for i in issues] == ["no_data"]
