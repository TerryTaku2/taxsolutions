"""Visitor counting is off unless VATSYS_GOATCOUNTER is set, and sends only grouped page addresses."""

from tests.conftest import csrf_from
from tests.test_web import register
from vatsys import config


def test_no_counter_by_default(client):
    assert "goatcounter" not in client.get("/register").text


def test_counter_and_sign_in_event(client, monkeypatch):
    monkeypatch.setattr(config, "GOATCOUNTER_URL", "https://example.goatcounter.com/count")
    r = client.get("/register")
    assert 'data-goatcounter="https://example.goatcounter.com/count"' in r.text
    assert "event: true" not in r.text
    token = register(client)
    client.post("/logout", data={"csrf": token})
    token = csrf_from(client.get("/login").text)
    r = client.post("/login", data={"csrf": token, "email": "t@example.com", "password": "long-enough-password"})
    assert "path: 'sign-in'" in r.text and "event: true" in r.text
    assert "event: true" not in client.get("/").text  # counted once
