"""Load a demo account with the sample transport company data.

    python -m vatsys.demo

Sign in as demo@example.com / demo-password-123. The exchange rates loaded are
SAMPLE values, not official RBZ rates.

On a public demo server, set VATSYS_PUBLIC_DEMO=1 so the demo account is a regular
user: its password is published, so it must not be able to change VAT rates,
exchange rates or global rules.
"""

import csv
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from . import config
from .db import Database
from .ingest import UploadOptions, import_file
from .models import Business, ExchangeRate, User
from .security import hash_password

SAMPLES = config.BASE_DIR / "sample_data"
EMAIL, PASSWORD = "demo@example.com", "demo-password-123"


def load_demo(db: Database, storage_dir: Path) -> None:
    with db.session() as s:
        if s.scalar(select(User).where(User.email == EMAIL)):
            print("Demo data already loaded.")
            return
        first = s.scalar(select(User.id).limit(1)) is None
        public = os.environ.get("VATSYS_PUBLIC_DEMO", "").lower() in ("1", "true", "yes")
        user = User(email=EMAIL, name="Demo User", password_hash=hash_password(PASSWORD),
                    is_admin=first and not public)
        s.add(user)
        s.flush()
        business = Business(owner_id=user.id, name="Sample Haulage (Pvt) Ltd", vat_number="10099999",
                            bp_number="200123456", sector="Transport", filing_frequency="monthly",
                            reporting_currency="USD", contact_email=EMAIL)
        s.add(business)
        s.flush()
        with open(SAMPLES / "exchange_rates_SAMPLE.csv", newline="") as f:
            for row in csv.DictReader(f):
                d = date.fromisoformat(row["Date"])
                if not s.scalar(select(ExchangeRate.id).where(ExchangeRate.rate_date == d)):
                    s.add(ExchangeRate(rate_date=d, base=row["Base"], quote=row["Quote"],
                                       rate=Decimal(row["Rate"]), source=row["Source"]))
        files = [("sales_ledger_sep2026.xlsx", UploadOptions("ledger", "sale")),
                 ("purchases_sep2026.csv", UploadOptions("ledger", "purchase")),
                 ("bank_statement_aug2026.csv", UploadOptions("bank", "auto")),
                 ("ecocash_aug2026.csv", UploadOptions("mobile_money", "auto"))]
        for name, options in files:
            import_file(s, business, name, (SAMPLES / name).read_bytes(), options,
                        storage_dir=storage_dir, user_id=user.id)
        s.commit()
    print(f"Demo loaded. Sign in as {EMAIL} / {PASSWORD}")


if __name__ == "__main__":
    database = Database()
    database.create_all()
    load_demo(database, config.DATA_DIR)
