from sqlalchemy import select

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
