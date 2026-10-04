"""Plain-language answers to "how much do I pay ZIMRA, and by when?".

The figures come from the computation and checks; this module only words them for
someone who doesn't know VAT. The structured fields (amounts, dates, status,
attention items) are kept alongside the sentences so other channels, such as the
filing wizard, reminders or an assistant, can reuse them.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .checks import ERROR, WARNING, run_checks
from .compute import compute_return
from .models import PURCHASE, SALE, Business, VatReturn
from .penalties import PenaltyEstimate, estimate
from .periods import Period, period_for
from .reminders import deadline_for

CURRENCY_NAMES = {"USD": "USD", "ZWG": "ZiG"}
# Checks shown elsewhere in the summary, or not something the user must act on.
NOT_ATTENTION = {"late", "currency_setoff"}


def money(amount: Decimal, currency: str) -> str:
    return f"{CURRENCY_NAMES.get(currency, currency)} {abs(amount):,.2f}"


def join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def when(d: date, today: date) -> str:
    days = (d - today).days
    label = f"{d:%A} {d.day} {d:%B %Y}"
    if days == 0:
        return f"today, {label}"
    if days == 1:
        return f"tomorrow, {label}"
    return label


@dataclass
class AttentionItem:
    code: str
    severity: str
    title: str


@dataclass
class PeriodSummary:
    period: Period
    kind: str  # "due" (the next return to file) or "running" (the period still under way)
    status: str  # no_data | payable | refund | nil
    payable: dict[str, Decimal] = field(default_factory=dict)  # positive amounts per currency
    refundable: dict[str, Decimal] = field(default_factory=dict)  # positive amounts per currency
    finalised: bool = False
    days_left: int = 0
    sales: int = 0
    purchases: int = 0
    attention: list[AttentionItem] = field(default_factory=list)
    penalty: PenaltyEstimate | None = None
    headline: str = ""
    details: list[str] = field(default_factory=list)

    @property
    def due_date(self) -> date:
        return self.period.due_date

    @property
    def overdue(self) -> bool:
        return self.days_left < 0 and not self.finalised

    @property
    def tone(self) -> str:
        if self.finalised:
            return "success"
        if self.overdue or any(a.severity == ERROR for a in self.attention):
            return "error"
        if self.attention or (self.kind == "due" and not self.finalised and self.days_left <= 5):
            return "warning"
        return "info"


def summarise(session: Session, business: Business, period: Period, today: date, kind: str = "due") -> PeriodSummary:
    result = compute_return(session, business, period)
    finalised = session.scalar(select(VatReturn.id).where(
        VatReturn.business_id == business.id, VatReturn.period_start == period.start,
        VatReturn.status == "finalised")) is not None
    nets = result.net_by_currency()
    s = PeriodSummary(period, kind, "nil", finalised=finalised, days_left=(period.due_date - today).days)
    s.payable = {c: v for c, v in nets.items() if v > 0}
    s.refundable = {c: -v for c, v in nets.items() if v < 0}
    s.sales = sum(1 for c in result.calcs.values() if c.txn.direction == SALE)
    s.purchases = sum(1 for c in result.calcs.values() if c.txn.direction == PURCHASE)
    if not result.calcs:
        s.status = "no_data"
    elif s.payable:
        s.status = "payable"
    elif s.refundable:
        s.status = "refund"
    if kind == "due" and not finalised:
        issues = run_checks(session, result, today)
        s.attention = [AttentionItem(i.code, i.severity, i.title) for i in issues
                       if i.severity in (ERROR, WARNING) and i.code not in NOT_ATTENTION]
        s.penalty = estimate(period, nets, today)
    _word(s, today)
    return s


def _amounts(amounts: dict[str, Decimal]) -> str:
    return join([money(v, c) for c, v in sorted(amounts.items())])


def _word(s: PeriodSummary, today: date) -> None:
    p, due = s.period, when(s.period.due_date, today)
    unsure = "about " if s.attention and not s.finalised else ""

    if s.kind == "running":
        if s.status == "no_data":
            s.headline = f"Nothing recorded for {p.label} yet."
            s.details.append("Upload this period's sales and purchases as they happen, so you can see what "
                             "you will owe before the return is due.")
        elif s.status == "payable":
            s.headline = f"So far in {p.label} you would pay about {_amounts(s.payable)}."
        elif s.status == "refund":
            s.headline = f"So far in {p.label}, ZIMRA would owe you about {_amounts(s.refundable)}."
        else:
            s.headline = f"So far in {p.label} nothing would be payable."
        if s.status != "no_data":
            s.details.append(f"Based on {s.sales} sale(s) and {s.purchases} purchase(s) recorded so far. This "
                             f"changes as you add transactions; the return is due by {due}.")
        return

    if s.status == "no_data":
        s.headline = f"Upload your {p.label} sales and purchases. The return is due by {due}."
        s.details.append("A return must be filed for every period, even when there were no sales and nothing "
                         "is owed (a nil return).")
    elif s.finalised:
        if s.payable:
            s.headline = f"Return filed. Pay {_amounts(s.payable)} to ZIMRA by {due}."
        elif s.refundable:
            s.headline = f"Return filed. ZIMRA owes you {_amounts(s.refundable)}."
        else:
            s.headline = f"Return filed. Nothing to pay for {p.label}."
    elif s.status == "payable":
        s.headline = f"You will pay {unsure}{_amounts(s.payable)} to ZIMRA by {due}."
    elif s.status == "refund":
        s.headline = f"Nothing to pay for {p.label}: ZIMRA owes you {unsure}{_amounts(s.refundable)}."
    else:
        s.headline = f"Nothing to pay for {p.label}, but you must still file the return by {due}."

    if s.status == "payable" and len(s.payable) > 1 or s.payable and s.refundable:
        s.details.append("Pay each amount in its own currency. VAT received in US dollars must be paid in US "
                         "dollars; paying in the wrong currency costs double the tax.")
    if s.payable and s.refundable:
        s.details.append(f"The {_amounts(s.refundable)} ZIMRA owes you can't be used to reduce the "
                         f"{_amounts(s.payable)} you pay, because they are in different currencies.")
    small = s.refundable.get("USD")
    if small is not None and small <= config.MIN_REFUND:
        s.details.append(f"A refund of US${config.MIN_REFUND:,.0f} or less isn't paid out; it is carried "
                         "forward and reduces what you pay next period.")
    if s.overdue and s.penalty:
        cost = [f"about US${s.penalty.late_return_penalty:,.2f} for filing late (US$"
                f"{config.LATE_RETURN_PENALTY_PER_DAY:,.0f} a day)"]
        cost += [f"a penalty of {money(e.penalty, c)} for paying late" for c, e in s.penalty.by_currency.items()]
        s.details.append(f"This was due {s.penalty.days_late} day(s) ago. So far that could cost {join(cost)}, "
                         "plus interest. File and pay as soon as you can.")
    elif not s.finalised and s.status != "no_data" and 0 <= s.days_left <= 5:
        s.details.append(f"Only {s.days_left} day(s) left. After the due date ZIMRA can charge US$"
                         f"{config.LATE_RETURN_PENALTY_PER_DAY:,.0f} a day, plus a penalty equal to the unpaid tax.")
    if s.attention and not s.finalised:
        n = len(s.attention)
        s.details.append(f"{n} thing{'s need' if n != 1 else ' needs'} your attention first, so the amount may "
                         "change.")


def business_summaries(session: Session, business: Business, today: date) -> list[PeriodSummary]:
    """The next return to file and, when different, the period still under way."""
    due = deadline_for(session, business, today).period
    out = [summarise(session, business, due, today, "due")]
    current = period_for(today, business.filing_frequency)
    if current != due:
        out.append(summarise(session, business, current, today, "running"))
    return out
