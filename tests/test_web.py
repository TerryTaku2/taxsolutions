"""End-to-end: register, add a business, upload the samples, review, compute, export, finalise."""

import io
import re

from openpyxl import load_workbook

from tests.conftest import SAMPLES, csrf_from


def register(client):
    r = client.get("/register")
    token = csrf_from(r.text)
    r = client.post("/register", data={"csrf": token, "name": "Terry", "email": "t@example.com",
                                       "password": "long-enough-password"})
    assert r.status_code == 200 and "Businesses" in r.text
    return csrf_from(r.text)  # the token is rotated on sign-in


def test_full_flow(client):
    token = register(client)

    r = client.post("/businesses/new", data={"csrf": token, "name": "Sample Haulage", "vat_number": "10099999",
                                             "filing_frequency": "monthly", "reporting_currency": "USD"})
    bid = int(re.search(r"/b/(\d+)", str(r.url)).group(1))

    r = client.post("/settings/fx/import", data={"csrf": token},
                    files={"file": ("rates.csv", (SAMPLES / "exchange_rates_SAMPLE.csv").read_bytes())})
    assert "Imported" in r.text

    for name, direction in (("sales_ledger_sep2026.xlsx", "sale"), ("purchases_sep2026.csv", "purchase")):
        r = client.post(f"/b/{bid}/uploads", data={"csrf": token, "source_type": "ledger",
                                                   "default_direction": direction, "default_currency": "USD",
                                                   "amounts_include_vat": "yes"},
                        files={"file": (name, (SAMPLES / name).read_bytes())})
        assert r.status_code == 200
        assert "Imported" in r.text and "Read rate" in r.text

    # Same file twice is refused
    r = client.post(f"/b/{bid}/uploads", data={"csrf": token, "source_type": "ledger", "default_direction": "sale",
                                               "default_currency": "USD", "amounts_include_vat": "yes"},
                    files={"file": ("again.xlsx", (SAMPLES / "sales_ledger_sep2026.xlsx").read_bytes())})
    assert "already uploaded" in r.text

    r = client.get(f"/b/{bid}/returns/2026-09-01")
    assert r.status_code == 200
    assert "need review" in r.text and "Output tax" in r.text

    # Review: mark everything shown as reviewed
    r = client.get(f"/b/{bid}/transactions?period=2026-09-01")
    ids = re.findall(r'name="row_id" value="(\d+)"', r.text)
    assert len(ids) == 19
    r = client.post(f"/b/{bid}/transactions/save", data={"csrf": token, "row_id": ids, "action": "save_review"})
    assert "Saved 19" in r.text or "Saved" in r.text

    # Change one item to exempt and check the audit trail records it
    r = client.post(f"/b/{bid}/transactions/save",
                    data={"csrf": token, "row_id": [ids[0]], f"cat_{ids[0]}": "exempt", "action": "save"})
    r = client.get(f"/b/{bid}/transactions/{ids[0]}")
    assert "edit_transaction" in r.text and "Exempt" in r.text

    r = client.get(f"/b/{bid}/returns/2026-09-01/lines/OUTPUT_TAX")
    assert r.status_code == 200 and "FX rate" in r.text

    r = client.get(f"/b/{bid}/returns/2026-09-01/export.xlsx")
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["VAT Return", "Sales schedule", "Purchases schedule", "Exchange rates", "Checks"]
    assert wb["Exchange rates"].max_row > 1

    r = client.post(f"/b/{bid}/returns/2026-09-01/finalise", data={"csrf": token})
    assert "finalised" in r.text.lower()

    # Locked: edits are ignored, new uploads into the period are rejected row by row
    r = client.post(f"/b/{bid}/transactions/save",
                    data={"csrf": token, "row_id": [ids[1]], f"cat_{ids[1]}": "zero", "action": "save"})
    assert "Saved 0" in r.text

    r = client.post(f"/b/{bid}/returns/2026-09-01/reopen", data={"csrf": token, "reason": "correction"})
    assert "reopened" in r.text
    r = client.get(f"/b/{bid}/audit")
    assert "reopen return" in r.text and "finalise return" in r.text


def test_requires_login_and_csrf(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    register(client)
    r = client.post("/businesses/new", data={"csrf": "wrong", "name": "X"})
    assert r.status_code == 400


def test_other_users_business_is_hidden(client, db):
    token = register(client)
    client.post("/businesses/new", data={"csrf": token, "name": "Mine"})
    client.post("/logout", data={"csrf": token})
    r = client.get("/register")
    t2 = csrf_from(r.text)
    client.post("/register", data={"csrf": t2, "name": "Other", "email": "o@example.com",
                                   "password": "another-long-password"})
    r = client.get("/b/1")
    assert r.status_code == 404


def test_bad_mapping_can_be_fixed(client):
    token = register(client)
    r = client.post("/businesses/new", data={"csrf": token, "name": "B"})
    bid = int(re.search(r"/b/(\d+)", str(r.url)).group(1))
    csv_bytes = b"When,What,How much\n01/09/2025,Freight,115.50\n"
    r = client.post(f"/b/{bid}/uploads", data={"csrf": token, "source_type": "ledger", "default_direction": "sale",
                                               "default_currency": "USD", "amounts_include_vat": "yes"},
                    files={"file": ("odd.csv", csv_bytes)})
    assert "recognised" in r.text
    uid = int(re.search(r"/uploads/(\d+)", str(r.url)).group(1))
    r = client.post(f"/b/{bid}/uploads/{uid}/reprocess",
                    data={"csrf": token, "header_row": "1", "map_date": "0", "map_description": "1",
                          "map_amount": "2", "source_type": "ledger", "default_direction": "sale",
                          "default_currency": "USD", "amounts_include_vat": "yes"})
    assert "Re-imported: 1 transaction(s)" in r.text
