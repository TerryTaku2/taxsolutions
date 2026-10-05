from sqlalchemy import select

from tests.conftest import csrf_from
from vatsys.demo import EMAIL, load_demo
from vatsys.models import Transaction, User


def test_demo_loads_once(db, tmp_path, capsys):
    load_demo(db, tmp_path)
    load_demo(db, tmp_path)
    assert "already loaded" in capsys.readouterr().out
    with db.session() as s:
        assert s.scalar(select(User).where(User.email == EMAIL)).is_admin
        assert len(s.scalars(select(Transaction)).all()) > 20


def test_public_demo_account_is_not_admin(db, tmp_path, monkeypatch):
    monkeypatch.setenv("VATSYS_PUBLIC_DEMO", "1")
    load_demo(db, tmp_path)
    with db.session() as s:
        assert not s.scalar(select(User).where(User.email == EMAIL)).is_admin


def test_one_click_demo_is_offered_for_the_public_demo_only(db, tmp_path, client):
    load_demo(db, tmp_path)  # the first account: an administrator, so its sign-in is never offered
    assert "Try the demo" not in client.get("/").text
    assert "Try the demo" not in client.get("/login").text

    with db.session() as s:
        s.scalar(select(User).where(User.email == EMAIL)).is_admin = False
        s.commit()
    assert "Try the demo" in client.get("/login").text
    r = client.get("/")
    assert "Try the demo" in r.text
    r = client.post("/login", data={"csrf": csrf_from(r.text), "email": EMAIL, "password": "demo-password-123"})
    assert "Sample Haulage" in r.text  # one click signs in to the demo business
