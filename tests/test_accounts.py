"""An admin registers a business for someone else, who signs in with a temporary password."""

import re

from tests.conftest import csrf_from
from tests.test_web import register


def sign_in(client, email, password):
    client.cookies.clear()  # sign out
    token = csrf_from(client.get("/login").text)
    return client.post("/login", data={"csrf": token, "email": email, "password": password})


def test_admin_creates_business_for_owner(client):
    token = register(client)  # first account: admin
    r = client.post("/businesses/new", data={"csrf": token, "name": "Moyo Transport", "owner_email": "Moyo@Example.com",
                                             "owner_name": "T. Moyo", "temp_password": "temporary-pass-1"})
    assert "moyo@example.com can now sign in" in r.text
    bid = int(re.search(r"/b/(\d+)", str(r.url)).group(1))
    assert "moyo@example.com" in client.get("/").text  # admin sees the account holder

    # The owner must change the temporary password before anything else
    r = sign_in(client, "moyo@example.com", "temporary-pass-1")
    assert str(r.url).endswith("/account/password") and "temporary password" in r.text
    assert str(client.get(f"/b/{bid}").url).endswith("/account/password")
    token = csrf_from(r.text)
    r = client.post("/account/password", data={"csrf": token, "current_password": "temporary-pass-1",
                                               "new_password": "short", "confirm_password": "short"})
    assert "at least 10" in r.text
    r = client.post("/account/password", data={"csrf": token, "current_password": "temporary-pass-1",
                                               "new_password": "moyos-own-password", "confirm_password":
                                               "moyos-own-password"})
    assert "Password changed" in r.text and "Moyo Transport" in r.text
    assert client.get(f"/b/{bid}").status_code == 200

    # The temporary password no longer works; the new one does
    assert "Incorrect" in sign_in(client, "moyo@example.com", "temporary-pass-1").text
    assert "Moyo Transport" in sign_in(client, "moyo@example.com", "moyos-own-password").text


def test_new_owner_needs_temporary_password(client):
    token = register(client)
    r = client.post("/businesses/new", data={"csrf": token, "name": "X", "owner_email": "new@example.com"})
    assert "Enter a temporary password" in r.text
    assert "X</a>" not in client.get("/").text  # nothing was created


def test_admin_moves_business_and_resets_password(client):
    token = register(client)
    r = client.post("/businesses/new", data={"csrf": token, "name": "Chiedza Stores"})
    bid = int(re.search(r"/b/(\d+)", str(r.url)).group(1))
    client.post("/businesses/new", data={"csrf": token, "name": "Other", "owner_email": "c@example.com",
                                         "temp_password": "first-temp-pass"})
    # Move Chiedza Stores to the existing account and reset its password
    r = client.post(f"/b/{bid}/settings", data={"csrf": token, "name": "Chiedza Stores", "owner_email": "c@example.com",
                                                "temp_password": "second-temp-pass", "filing_frequency": "monthly",
                                                "reporting_currency": "USD"})
    assert "Business details saved" in r.text
    r = sign_in(client, "c@example.com", "second-temp-pass")
    token = csrf_from(r.text)
    r = client.post("/account/password", data={"csrf": token, "current_password": "second-temp-pass",
                                               "new_password": "chiedza-password", "confirm_password":
                                               "chiedza-password"})
    assert "Chiedza Stores" in r.text and "Other" in r.text


def test_owner_cannot_assign_owners(client):
    token = register(client)
    client.post("/businesses/new", data={"csrf": token, "name": "A", "owner_email": "o@example.com",
                                         "temp_password": "temporary-pass-1"})
    r = sign_in(client, "o@example.com", "temporary-pass-1")
    token = csrf_from(r.text)
    r = client.post("/account/password", data={"csrf": token, "current_password": "temporary-pass-1",
                                               "new_password": "owner-password-1", "confirm_password":
                                               "owner-password-1"})
    assert "Account holder" not in client.get("/businesses/new").text
    r = client.post("/businesses/new", data={"csrf": token, "name": "B", "owner_email": "x@example.com",
                                             "temp_password": "temporary-pass-2"})
    assert "Incorrect" in sign_in(client, "x@example.com", "temporary-pass-2").text
