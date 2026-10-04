"""Database models."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DecimalType(TypeDecorator):
    """Stores Decimal as text so SQLite never rounds through float."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    pass


# VAT categories
STANDARD = "standard"
ZERO_RATED = "zero"
EXEMPT = "exempt"
OUT_OF_SCOPE = "out_of_scope"
VAT_CATEGORIES = {
    STANDARD: "Standard-rated",
    ZERO_RATED: "Zero-rated",
    EXEMPT: "Exempt",
    OUT_OF_SCOPE: "Out of scope",
}

# Input tax types for purchases
INPUT_GENERAL = "general"
INPUT_CAPITAL = "capital"
INPUT_IMPORT = "import"
INPUT_NON_DEDUCTIBLE = "non_deductible"
INPUT_MIXED = "mixed"
INPUT_IMPORTED_SERVICE = "imported_service"
INPUT_TYPES = {
    INPUT_GENERAL: "Other goods & services",
    INPUT_CAPITAL: "Capital goods",
    INPUT_IMPORT: "Imports",
    INPUT_MIXED: "Mixed use (apportioned)",
    INPUT_IMPORTED_SERVICE: "Imported service (self-accounted)",
    INPUT_NON_DEDUCTIBLE: "Not deductible",
}

SALE = "sale"
PURCHASE = "purchase"
DIRECTIONS = {SALE: "Sale (output)", PURCHASE: "Purchase (input)"}

CURRENCIES = ["USD", "ZWG"]

# Tax period categories (VAT Act s27). Category D (other approved periods) is not supported yet.
FILING_FREQUENCIES = {
    "monthly": "Category C: monthly",
    "bimonthly_odd": "Category A: two-monthly, periods ending Jan/Mar/May/Jul/Sep/Nov",
    "bimonthly_even": "Category B: two-monthly, periods ending Feb/Apr/Jun/Aug/Oct/Dec",
}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set when an admin creates the account or resets its password; cleared once the user picks their own.
    must_change_password: Mapped[bool | None] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    businesses: Mapped[list[Business]] = relationship(back_populates="owner")


class Business(Base):
    __tablename__ = "businesses"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(255))
    bp_number: Mapped[str | None] = mapped_column(String(50))
    vat_number: Mapped[str | None] = mapped_column(String(50))
    sector: Mapped[str | None] = mapped_column(String(100))
    filing_frequency: Mapped[str] = mapped_column(String(20), default="monthly")
    reporting_currency: Mapped[str] = mapped_column(String(3), default="USD")
    contact_email: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    owner: Mapped[User] = relationship(back_populates="businesses")


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(20))  # ledger | bank | mobile_money
    default_direction: Mapped[str] = mapped_column(String(10))  # auto | sale | purchase
    default_currency: Mapped[str] = mapped_column(String(3))
    amounts_include_vat: Mapped[bool] = mapped_column(Boolean, default=True)
    header_row: Mapped[int | None] = mapped_column(Integer)
    mapping: Mapped[dict | None] = mapped_column(JSON)
    headers: Mapped[list | None] = mapped_column(JSON)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_failed: Mapped[int] = mapped_column(Integer, default=0)
    rows_ignored: Mapped[int] = mapped_column(Integer, default=0)
    row_errors: Mapped[list | None] = mapped_column(JSON)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def read_rate(self) -> float | None:
        total = self.rows_imported + self.rows_failed
        return None if total == 0 else self.rows_imported / total


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    upload_id: Mapped[int | None] = mapped_column(ForeignKey("uploads.id"), index=True)
    row_number: Mapped[int | None] = mapped_column(Integer)
    txn_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    counterparty: Mapped[str | None] = mapped_column(String(255))
    counterparty_vat_number: Mapped[str | None] = mapped_column(String(50))
    invoice_number: Mapped[str | None] = mapped_column(String(100))
    reference: Mapped[str | None] = mapped_column(String(100))
    direction: Mapped[str] = mapped_column(String(10))
    amount: Mapped[Decimal] = mapped_column(DecimalType)
    currency: Mapped[str] = mapped_column(String(3))
    amount_includes_vat: Mapped[bool] = mapped_column(Boolean, default=True)
    stated_vat: Mapped[Decimal | None] = mapped_column(DecimalType)
    vat_category: Mapped[str] = mapped_column(String(20))
    input_type: Mapped[str] = mapped_column(String(20), default=INPUT_GENERAL)
    classification_source: Mapped[str] = mapped_column(String(20))  # file | rule | default | user
    classification_reason: Mapped[str | None] = mapped_column(String(255))
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("classification_rules.id"))
    reference_item_id: Mapped[int | None] = mapped_column(ForeignKey("reference_items.id"))
    confidence: Mapped[float] = mapped_column(default=0.5)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    upload: Mapped[Upload | None] = relationship()


class VatRate(Base):
    __tablename__ = "vat_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(20))
    rate: Mapped[Decimal] = mapped_column(DecimalType)  # percentage, e.g. 15.5
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(255))


class ClassificationRule(Base):
    __tablename__ = "classification_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id"), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower runs first
    field: Mapped[str] = mapped_column(String(20), default="any")  # description | counterparty | any
    match_type: Mapped[str] = mapped_column(String(20), default="contains")  # contains | equals | regex
    pattern: Mapped[str] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(10), default="any")  # any | sale | purchase
    vat_category: Mapped[str] = mapped_column(String(20))
    input_type: Mapped[str | None] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255))
    # Applies to transactions dated within this range (open-ended when empty)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)

    def applies_on(self, on: date | None) -> bool:
        if on is None:
            return True
        return (self.effective_from is None or self.effective_from <= on) and             (self.effective_to is None or on <= self.effective_to)


class ReferenceItem(Base):
    """A good or service whose VAT treatment is set by a document, such as the zero-rated
    and exempt schedules of the VAT (General) Regulations. Transactions are matched to
    items by keyword."""

    __tablename__ = "reference_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    item: Mapped[str] = mapped_column(String(255))
    keywords: Mapped[str] = mapped_column(Text)  # comma-separated words or phrases
    excludes: Mapped[str | None] = mapped_column(Text)  # comma-separated; any match rules the item out
    vat_category: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(255))  # document, e.g. "SI 273 of 2003, Second Schedule"
    reference: Mapped[str | None] = mapped_column(String(100))  # paragraph or item number
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    def applies_on(self, on: date | None) -> bool:
        if on is None:
            return True
        return (self.effective_from is None or self.effective_from <= on) and \
            (self.effective_to is None or on <= self.effective_to)

    @property
    def citation(self) -> str:
        return f"{self.source}{', ' + self.reference if self.reference else ''}"


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    __table_args__ = (UniqueConstraint("rate_date", "base", "quote"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date, index=True)
    base: Mapped[str] = mapped_column(String(3), default="USD")
    quote: Mapped[str] = mapped_column(String(3), default="ZWG")
    rate: Mapped[Decimal] = mapped_column(DecimalType)  # units of quote per 1 base
    source: Mapped[str | None] = mapped_column(String(100))


class VatReturn(Base):
    __tablename__ = "vat_returns"
    __table_args__ = (UniqueConstraint("business_id", "period_start"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="finalised")
    snapshot: Mapped[dict] = mapped_column(JSON)
    finalised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finalised_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    action: Mapped[str] = mapped_column(String(50))
    entity: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict | None] = mapped_column(JSON)

    user: Mapped[User | None] = relationship()


class ReminderSent(Base):
    __tablename__ = "reminders_sent"
    __table_args__ = (UniqueConstraint("business_id", "period_end", "days_before"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    period_end: Mapped[date] = mapped_column(Date)
    days_before: Mapped[int] = mapped_column(Integer)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
