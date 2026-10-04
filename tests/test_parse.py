from datetime import date
from decimal import Decimal

import pytest

from vatsys.ingest.parse import detect_header, match_header, parse_amount, parse_date, parse_rows
from vatsys.ingest.readers import IngestError, read_table


@pytest.mark.parametrize("value,expected", [
    ("1,234.50", Decimal("1234.50")),
    ("(12.00)", Decimal("-12.00")),
    ("-5", Decimal("-5")),
    ("100.00 DR", Decimal("-100.00")),
    ("100.00 CR", Decimal("100.00")),
    ("1 234,56", Decimal("1234.56")),
    ("1.234,56", Decimal("1234.56")),
    ("1,234", Decimal("1234")),
    ("25-", Decimal("-25")),
    (12.5, Decimal("12.5")),
    ("abc", None),
    ("", None),
])
def test_parse_amount(value, expected):
    assert parse_amount(value)[0] == expected


def test_parse_amount_currency():
    assert parse_amount("USD 50.00") == (Decimal("50.00"), "USD")
    assert parse_amount("ZiG 1,000") == (Decimal("1000"), "ZWG")
    assert parse_amount("US$20") == (Decimal("20"), "USD")


@pytest.mark.parametrize("value,expected", [
    ("2025-09-03", date(2025, 9, 3)),
    ("03/09/2025", date(2025, 9, 3)),  # day first
    ("03-Sep-2025", date(2025, 9, 3)),
    ("3 September 2025", date(2025, 9, 3)),
    ("2025-08-03 09:15:22", date(2025, 8, 3)),
    ("03/09/2025 14:30", date(2025, 9, 3)),
    (45903, date(2025, 9, 3)),  # Excel serial
    ("not a date", None),
])
def test_parse_date(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize("header,field", [
    ("Transaction Date", "date"), ("Narration", "description"), ("Debit Amount", "debit"),
    ("Money In", "credit"), ("Amount (USD)", "amount"), ("VAT Amount", "vat_amount"), ("VAT", "vat_amount"),
    ("Total incl VAT", "amount"), ("Supplier VAT No", "vat_number"), ("Tax Invoice No", "invoice_number"),
    ("Balance", None), ("Running balance", None),
])
def test_match_header(header, field):
    assert match_header(header)[0] == field


def test_detect_header_skips_preamble():
    rows = [["ACME Bank statement"], ["Account 123"], [], ["Date", "Description", "Debit", "Credit", "Balance"],
            ["01/09/2025", "x", "10", "", "90"]]
    idx, mapping = detect_header(rows)
    assert idx == 3
    assert mapping == {"date": 0, "description": 1, "debit": 2, "credit": 3}


def test_detect_header_fails_without_amount():
    with pytest.raises(IngestError):
        detect_header([["Date", "Description"], ["01/01/2025", "x"]])


def _parse(rows, default_direction="auto"):
    idx, mapping = detect_header(rows)
    return parse_rows(rows, idx, mapping, default_direction=default_direction, default_currency="USD")


def test_bank_statement_directions_and_skips():
    rows = [["Date", "Description", "Debit", "Credit", "Balance"],
            ["01/09/2025", "Balance b/f", "", "", "100"],
            ["02/09/2025", "Customer payment", "", "1,150.00", "1250"],
            ["03/09/2025", "Diesel", "500.00", "", "750"],
            ["bad", "Broken row", "5", "", ""],
            ["30/09/2025", "Closing balance", "", "", "750"]]
    r = _parse(rows)
    assert [(p.direction, p.amount) for p in r.rows] == [("sale", Decimal("1150.00")), ("purchase", Decimal("500.00"))]
    assert r.ignored == 2
    assert len(r.errors) == 1 and r.errors[0]["row"] == 5


def test_signed_amounts_and_type_column():
    rows = [["Date", "Type", "Details", "Amount", "Currency"],
            ["2025-08-03", "Merchant Payment", "In", "45.00", "USD"],
            ["2025-08-04", "Bill Payment", "Out", "-80.50", "USD"],
            ["2025-08-05", "DR", "Fee", "-2.00", "ZiG"],
            ["2025-08-06", "Payment", "x", "10", "ZAR"]]
    r = _parse(rows)
    assert [(p.direction, p.amount, p.currency) for p in r.rows] == [
        ("sale", Decimal("45.00"), "USD"), ("purchase", Decimal("80.50"), "USD"), ("purchase", Decimal("2.00"), "ZWG")]
    assert "Unsupported currency" in r.errors[0]["errors"][0]


def test_ledger_credit_note_keeps_sign():
    rows = [["Date", "Description", "Amount"], ["2025-09-01", "Sale", "100"], ["2025-09-02", "Credit note", "-20"]]
    r = _parse(rows, default_direction="sale")
    assert [(p.direction, p.amount) for p in r.rows] == [("sale", Decimal("100")), ("sale", Decimal("-20"))]


def test_read_sample_files():
    from tests.conftest import SAMPLES

    for name in ("sales_ledger_sep2026.xlsx", "purchases_sep2026.csv", "bank_statement_aug2026.csv",
                 "ecocash_aug2026.csv"):
        rows = read_table(name, (SAMPLES / name).read_bytes())
        r = _parse(rows)
        assert r.rows and not r.errors, name


def test_unsupported_extension():
    with pytest.raises(IngestError):
        read_table("file.pdf", b"%PDF")
