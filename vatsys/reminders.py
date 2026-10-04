"""Filing deadline reminders.

Run daily from a scheduler (cron / Windows Task Scheduler):

    python -m vatsys.reminders

Emails are sent when VATSYS_SMTP_HOST is configured; otherwise reminders are printed.
"""

import smtplib
import sys
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .models import Business, ReminderSent, VatReturn
from .periods import Period, next_deadline


@dataclass
class Deadline:
    business: Business
    period: Period
    days_left: int
    finalised: bool


def deadline_for(session: Session, business: Business, today: date) -> Deadline:
    period = next_deadline(today, business.filing_frequency)
    finalised = session.scalar(select(VatReturn.id).where(
        VatReturn.business_id == business.id, VatReturn.period_start == period.start,
        VatReturn.status == "finalised")) is not None
    return Deadline(business, period, (period.due_date - today).days, finalised)


def due_reminders(session: Session, today: date) -> list[tuple[Deadline, int]]:
    """Reminders to send today: (deadline, threshold) where threshold is one of REMINDER_DAYS."""
    out = []
    for business in session.scalars(select(Business)):
        d = deadline_for(session, business, today)
        if d.finalised:
            continue
        crossed = [n for n in config.REMINDER_DAYS if d.days_left <= n]
        if not crossed:
            continue
        threshold = min(crossed)
        already = session.scalar(select(ReminderSent.id).where(
            ReminderSent.business_id == business.id, ReminderSent.period_end == d.period.end,
            ReminderSent.days_before == threshold))
        if not already:
            out.append((d, threshold))
    return out


def _message(d: Deadline) -> EmailMessage:
    b, p = d.business, d.period
    msg = EmailMessage()
    msg["Subject"] = f"VAT return for {b.name} due {p.due_date:%d %b %Y} ({d.days_left} days)"
    msg["From"] = config.SMTP_FROM
    msg["To"] = b.contact_email or b.owner.email
    msg.set_content(
        f"The VAT return for {b.name} for {p.label} is due on {p.due_date:%A %d %B %Y}.\n\n"
        "Upload any missing sales and purchases, review flagged items, and finalise the return "
        "in the VAT Computation System before the deadline.\n")
    return msg


def send_reminders(session: Session, today: date | None = None) -> int:
    today = today or date.today()
    sent = 0
    for d, threshold in due_reminders(session, today):
        msg = _message(d)
        if config.SMTP_HOST:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as smtp:
                smtp.starttls()
                if config.SMTP_USER:
                    smtp.login(config.SMTP_USER, config.SMTP_PASSWORD or "")
                smtp.send_message(msg)
        else:
            print(f"[reminder] to {msg['To']}: {msg['Subject']}")
        session.add(ReminderSent(business_id=d.business.id, period_end=d.period.end, days_before=threshold))
        session.commit()
        sent += 1
    return sent


if __name__ == "__main__":
    from .db import Database

    db = Database()
    db.create_all()
    with db.session() as s:
        n = send_reminders(s)
    print(f"{n} reminder(s) sent")
    sys.exit(0)
