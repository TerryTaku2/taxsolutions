import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

os.environ.setdefault("VATSYS_SECRET_KEY", "test-secret")

from vatsys.db import Database  # noqa: E402
from vatsys.models import Business, ExchangeRate, User  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "sample_data"


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    database.create_all()
    return database


@pytest.fixture
def session(db):
    with db.session() as s:
        yield s


@pytest.fixture
def business(session):
    user = User(email="owner@example.com", name="Owner", password_hash="x")
    session.add(user)
    session.flush()
    b = Business(owner_id=user.id, name="Test Haulage", filing_frequency="monthly", reporting_currency="USD")
    session.add(b)
    session.flush()
    return b


@pytest.fixture
def fx(session):
    """USD/ZWG at 25 on 1 Sep 2026 and 26 from 15 Sep 2026."""
    session.add_all([
        ExchangeRate(rate_date=date(2026, 9, 1), base="USD", quote="ZWG", rate=Decimal("25"), source="test"),
        ExchangeRate(rate_date=date(2026, 9, 15), base="USD", quote="ZWG", rate=Decimal("26"), source="test"),
    ])
    session.flush()


@pytest.fixture
def client(db, tmp_path):
    from fastapi.testclient import TestClient

    from vatsys.web.app import create_app

    return TestClient(create_app(db, storage_dir=tmp_path))


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)
