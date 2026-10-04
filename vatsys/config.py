"""Runtime configuration, read from environment variables."""

import os
import secrets
import warnings
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("VATSYS_DATA_DIR", BASE_DIR / "data"))
DATABASE_URL = os.environ.get(
    "VATSYS_DATABASE_URL", f"sqlite:///{(DATA_DIR / 'vatsys.db').as_posix()}"
)

SECRET_KEY = os.environ.get("VATSYS_SECRET_KEY")
if not SECRET_KEY:
    warnings.warn("VATSYS_SECRET_KEY not set; using a random key (sessions reset on restart)")
    SECRET_KEY = secrets.token_hex(32)

# Classifications below this confidence are flagged for user review.
REVIEW_CONFIDENCE_THRESHOLD = float(os.environ.get("VATSYS_REVIEW_THRESHOLD", "0.8"))

# Days before a filing deadline on which reminders are sent.
REMINDER_DAYS = [int(d) for d in os.environ.get("VATSYS_REMINDER_DAYS", "10,5,2").split(",")]

# GoatCounter visitor counting, e.g. https://name.goatcounter.com/count. Unset: nothing is counted.
GOATCOUNTER_URL = os.environ.get("VATSYS_GOATCOUNTER") or None

SMTP_HOST = os.environ.get("VATSYS_SMTP_HOST")
SMTP_PORT = int(os.environ.get("VATSYS_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("VATSYS_SMTP_USER")
SMTP_PASSWORD = os.environ.get("VATSYS_SMTP_PASSWORD")
SMTP_FROM = os.environ.get("VATSYS_SMTP_FROM", "noreply@ttech.co.zw")

MAX_UPLOAD_BYTES = int(os.environ.get("VATSYS_MAX_UPLOAD_MB", "20")) * 1024 * 1024

# Amounts set by the VAT Act. Check them against the current law; all are in USD.
# Registration is compulsory above this taxable turnover in any 12 months (s23(1)).
REGISTRATION_THRESHOLD = Decimal(os.environ.get("VATSYS_REGISTRATION_THRESHOLD", "25000"))
# Unregistered businesses are warned once turnover reaches this share of the threshold.
REGISTRATION_WARNING_SHARE = Decimal(os.environ.get("VATSYS_REGISTRATION_WARNING_SHARE", "0.8"))
# Refunds at or below this amount are carried forward to the next period (s44).
MIN_REFUND = Decimal(os.environ.get("VATSYS_MIN_REFUND", "60"))
# Civil penalty for a late return: per day, for up to this many days (s62(2)).
LATE_RETURN_PENALTY_PER_DAY = Decimal(os.environ.get("VATSYS_LATE_RETURN_PENALTY_PER_DAY", "30"))
LATE_RETURN_MAX_DAYS = int(os.environ.get("VATSYS_LATE_RETURN_MAX_DAYS", "181"))
# Prescribed interest rate on late payment, percent per year (s39(2)). Unset: interest is not estimated.
_interest = os.environ.get("VATSYS_LATE_INTEREST_RATE")
LATE_INTEREST_RATE = Decimal(_interest) if _interest else None
