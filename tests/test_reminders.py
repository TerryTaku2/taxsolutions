from datetime import date

from vatsys.models import VatReturn
from vatsys.reminders import deadline_for, due_reminders, send_reminders


def test_reminders_sent_once_per_threshold(session, business, capsys):
    session.commit()
    assert due_reminders(session, date(2026, 10, 1)) == []  # 24 days left: too early
    assert send_reminders(session, date(2026, 10, 20)) == 1  # 5 days left
    assert "due 25 Oct 2026" in capsys.readouterr().out
    assert send_reminders(session, date(2026, 10, 21)) == 0  # same threshold already sent
    assert send_reminders(session, date(2026, 10, 23)) == 1  # 2 days left


def test_no_reminder_once_finalised(session, business):
    session.add(VatReturn(business_id=business.id, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30),
                          status="finalised", snapshot={}))
    session.commit()
    assert deadline_for(session, business, date(2026, 10, 20)).finalised
    assert due_reminders(session, date(2026, 10, 20)) == []
