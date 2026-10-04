"""Plain-language questions that classify transactions the rules couldn't.

Users without VAT knowledge answer what the money was for ("Was this customer
outside Zimbabwe?") instead of choosing "zero-rated" or "exempt". Similar
transactions are grouped, by customer or supplier, or by description with
numbers removed, so one answer covers them all and can be remembered as a rule
for future uploads.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, config
from .models import (
    EXEMPT,
    INPUT_CAPITAL,
    INPUT_GENERAL,
    INPUT_IMPORT,
    INPUT_IMPORTED_SERVICE,
    INPUT_MIXED,
    INPUT_NON_DEDUCTIBLE,
    OUT_OF_SCOPE,
    PURCHASE,
    SALE,
    STANDARD,
    ZERO_RATED,
    Business,
    ClassificationRule,
    Transaction,
    User,
)
from .money import ZERO


@dataclass(frozen=True)
class Answer:
    key: str
    label: str
    help: str
    category: str
    input_type: str = INPUT_GENERAL


QUESTIONS = {
    SALE: ("What was this money for?", [
        Answer("local_sale", "I sold goods or services in Zimbabwe",
               "Most sales. VAT is charged at the standard rate.", STANDARD),
        Answer("export", "Goods exported, or transporting goods to or from another country",
               "No VAT is charged (0%), but keep the export documents or other proof ZIMRA accepts.", ZERO_RATED),
        Answer("foreign_client", "A service for a customer based outside Zimbabwe",
               "No VAT is charged (0%) if the customer is outside Zimbabwe. Keep proof, such as the contract.",
               ZERO_RATED),
        Answer("exempt_sale", "Rent for a home, school fees, medical care, bus or train fares, or interest",
               "These are exempt: no VAT is charged, and VAT on related costs can't be claimed.", EXEMPT),
        Answer("not_sale", "Not a sale: a loan, money from the owners, a refund, a grant, or a transfer "
               "between my own accounts", "This isn't a supply, so it doesn't go on the VAT return.", OUT_OF_SCOPE),
    ]),
    PURCHASE: ("What did you pay for?", [
        Answer("business_cost", "Something for the business, from a supplier who charged VAT",
               "Stock, fuel, repairs, phone, business rent and so on. You can claim the VAT if you have the "
               "supplier's tax invoice.", STANDARD),
        Answer("capital", "Equipment, machinery, computers or a goods vehicle that will last for years",
               "The VAT is claimed as input tax on capital goods. Keep the tax invoice.", STANDARD, INPUT_CAPITAL),
        Answer("import", "Goods I imported, with VAT paid at customs",
               "The VAT on the bill of entry is claimed back. Keep the bill of entry.", STANDARD, INPUT_IMPORT),
        Answer("foreign_service", "A service from a supplier outside Zimbabwe",
               "You must work out and pay the VAT yourself (imported services). Online platforms covered by "
               "the digital services withholding tax are the exception.", STANDARD, INPUT_IMPORTED_SERVICE),
        Answer("mixed", "Something used both for sales that carry VAT and for exempt sales",
               "Only part of the VAT can be claimed, in proportion to your taxable sales.", STANDARD, INPUT_MIXED),
        Answer("blocked", "Entertainment, club or membership fees, or a passenger car",
               "VAT on these can't be claimed by law, even with a tax invoice.", STANDARD, INPUT_NON_DEDUCTIBLE),
        Answer("no_vat_supplier", "From a supplier who doesn't charge VAT, or with no tax invoice",
               "No VAT can be claimed without a valid tax invoice from a VAT-registered supplier.", STANDARD,
               INPUT_NON_DEDUCTIBLE),
        Answer("exempt_purchase", "Interest, rent for a home, medical care, school fees or passenger fares",
               "These are exempt, so there is no VAT to claim.", EXEMPT),
        Answer("not_purchase", "Not a purchase: wages, tax payments, loan repayments, drawings, or a transfer "
               "between my own accounts", "This isn't a supply, so it doesn't go on the VAT return.", OUT_OF_SCOPE),
    ]),
}
ANSWERS = {d: {a.key: a for a in answers} for d, (_, answers) in QUESTIONS.items()}


def needs_review(t: Transaction) -> bool:
    return not t.reviewed and not t.excluded and t.confidence < config.REVIEW_CONFIDENCE_THRESHOLD


def _words(text: str | None) -> list[str]:
    """Lower-case words with numbers, dates and references removed."""
    return [w for w in re.findall(r"[a-z][a-z'&-]*", (text or "").lower()) if len(w) > 1]


def group_key(t: Transaction) -> tuple[str, str, str]:
    """(direction, 'party' or 'text', normalised value)."""
    party = " ".join(_words(t.counterparty))
    if party:
        return t.direction, "party", party
    return t.direction, "text", " ".join(_words(t.description)) or (t.description or "").strip().lower()


@dataclass
class Group:
    key: tuple[str, str, str]
    txns: list[Transaction] = field(default_factory=list)

    @property
    def direction(self) -> str:
        return self.key[0]

    @property
    def token(self) -> str:
        """Stable id for the group in forms and URLs."""
        return "|".join(self.key)

    @property
    def title(self) -> str:
        if self.key[1] == "party":
            return self.txns[0].counterparty.strip()
        return self.txns[0].description.strip()

    @property
    def descriptions(self) -> list[str]:
        seen = []
        for t in self.txns:
            if t.description and t.description not in seen:
                seen.append(t.description)
        return seen[:3]

    @property
    def totals(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for t in self.txns:
            out[t.currency] += t.amount
        return dict(out)

    @property
    def first_date(self) -> date:
        return min(t.txn_date for t in self.txns)

    @property
    def last_date(self) -> date:
        return max(t.txn_date for t in self.txns)

    @property
    def question(self) -> str:
        return QUESTIONS[self.direction][0]

    @property
    def answers(self) -> list[Answer]:
        return QUESTIONS[self.direction][1]


def pending_groups(session: Session, business: Business, locked: list[tuple[date, date]] = ()) -> list[Group]:
    groups: dict[tuple, Group] = {}
    for t in session.scalars(select(Transaction).where(
            Transaction.business_id == business.id, Transaction.reviewed.is_(False),
            Transaction.excluded.is_(False),
            Transaction.confidence < config.REVIEW_CONFIDENCE_THRESHOLD).order_by(Transaction.txn_date)):
        if any(a <= t.txn_date <= b for a, b in locked):
            continue
        key = group_key(t)
        groups.setdefault(key, Group(key)).txns.append(t)
    return sorted(groups.values(), key=lambda g: (-len(g.txns), g.title.lower()))


def rule_for(group: Group, answer: Answer, business: Business) -> ClassificationRule:
    """A business rule that gives future transactions like these the same answer."""
    if group.key[1] == "party":
        pattern, field_, match_type = group.title, "counterparty", "equals"
    else:
        words = group.key[2].split()
        pattern = r"\b" + r"\b.*\b".join(re.escape(w) for w in words) + r"\b"
        field_, match_type = "description", "regex"
    return ClassificationRule(business_id=business.id, priority=50, field=field_, match_type=match_type,
                              pattern=pattern, direction=group.direction, vat_category=answer.category,
                              input_type=answer.input_type, note=f"You answered: {answer.label}")


def apply_answer(session: Session, business: Business, user: User, group: Group, answer_key: str,
                 remember: bool) -> int:
    answer = ANSWERS[group.direction].get(answer_key)
    if answer is None:
        raise ValueError("Unknown answer")
    rule = None
    if remember:
        rule = rule_for(group, answer, business)
        session.add(rule)
        session.flush()
        audit.log(session, "add_rule", user_id=user.id, business_id=business.id, entity="rule", entity_id=rule.id,
                  pattern=rule.pattern, category=answer.category, from_question=answer.key)
    for t in group.txns:
        before = {"category": t.vat_category, "input_type": t.input_type}
        t.vat_category, t.input_type = answer.category, answer.input_type
        t.classification_source, t.confidence, t.reviewed = "user", 1.0, True
        t.classification_reason = f"{user.name} answered: {answer.label}"
        t.rule_id = rule.id if rule else None
        t.reference_item_id = None
        audit.log(session, "answer_question", user_id=user.id, business_id=business.id, entity="transaction",
                  entity_id=t.id, before=before, answer=answer.key,
                  after={"category": answer.category, "input_type": answer.input_type})
    return len(group.txns)
