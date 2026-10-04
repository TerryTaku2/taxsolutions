"""Rule-based VAT classification.

Order of precedence:
1. A VAT category given in the uploaded file itself.
2. The first matching rule set up for this business.
3. The goods or service the description names, looked up in the reference lists
   loaded from documents such as the VAT (General) Regulations (see reference.py).
4. The first matching rule for all businesses.
5. A default (standard-rated) with low confidence, so the item is flagged for review.
Rules and list items are used only when in force on the transaction date.
"""

import re
from dataclasses import dataclass
from datetime import date

from .models import (
    EXEMPT,
    INPUT_GENERAL,
    OUT_OF_SCOPE,
    STANDARD,
    VAT_CATEGORIES,
    ZERO_RATED,
    ClassificationRule,
)

FILE_CATEGORY_CONFIDENCE = 0.95
RULE_CONFIDENCE = 0.9
REFERENCE_CONFIDENCE = 0.9
DEFAULT_CONFIDENCE = 0.5


@dataclass
class Classification:
    vat_category: str
    input_type: str
    source: str
    confidence: float
    reason: str
    rule_id: int | None = None
    reference_item_id: int | None = None


def parse_category(value: str | None) -> str | None:
    """Interpret a category written in a user's spreadsheet."""
    if not value:
        return None
    v = re.sub(r"[^a-z0-9.%]", "", str(value).lower())
    if not v:
        return None
    if v.startswith("zero") or v in {"0%", "0", "zr", "z"}:
        return ZERO_RATED
    if v.startswith("exempt") or v in {"ex", "e"}:
        return EXEMPT
    if "outofscope" in v or v in {"oos", "na", "n/a", "nonvat", "novat"}:
        return OUT_OF_SCOPE
    if v.startswith("standard") or v in {"sr", "s", "15%", "15.5%", "14.5%", "vat", "vatable"}:
        return STANDARD
    return None


class Classifier:
    def __init__(self, rules: list[ClassificationRule], business_id: int | None = None, matcher=None):
        self.matcher = matcher  # a reference.ReferenceMatcher, or None
        usable = [r for r in rules if r.active and (r.business_id is None or r.business_id == business_id)]
        usable.sort(key=lambda r: (r.business_id is None, r.priority, r.id or 0))
        self.rules = []
        for r in usable:
            try:
                self.rules.append((r, self._compile(r)))
            except re.error:
                continue  # an invalid regex is reported when the rule is saved

    @staticmethod
    def _compile(rule: ClassificationRule) -> re.Pattern:
        if rule.match_type == "regex":
            return re.compile(rule.pattern, re.IGNORECASE)
        if rule.match_type == "equals":
            return re.compile(rf"^\s*{re.escape(rule.pattern)}\s*$", re.IGNORECASE)
        return re.compile(re.escape(rule.pattern), re.IGNORECASE)

    def classify(self, *, direction: str, description: str, counterparty: str | None,
                 file_category: str | None = None, on: date | None = None) -> Classification:
        cat = parse_category(file_category)
        if cat:
            return Classification(cat, INPUT_GENERAL, "file", FILE_CATEGORY_CONFIDENCE,
                                  f"Category '{file_category}' given in the uploaded file")
        global_rules = []
        for rule, rx in self.rules:
            if rule.business_id is None:
                global_rules.append((rule, rx))
                continue
            if found := self._try_rule(rule, rx, direction, description, counterparty, on):
                return found
        if self.matcher and (m := self.matcher.match(description or "", on)):
            item = m.item
            return Classification(item.vat_category, INPUT_GENERAL, "reference", REFERENCE_CONFIDENCE,
                                  f"{item.item}: {VAT_CATEGORIES[item.vat_category].lower()} under "
                                  f"{item.citation} (matched '{m.keyword}')", reference_item_id=item.id)
        for rule, rx in global_rules:
            if found := self._try_rule(rule, rx, direction, description, counterparty, on):
                return found
        return Classification(STANDARD, INPUT_GENERAL, "default", DEFAULT_CONFIDENCE,
                              "No rule matched; assumed standard-rated")


    @staticmethod
    def _try_rule(rule, rx, direction, description, counterparty, on) -> Classification | None:
        if rule.direction not in ("any", direction) or not rule.applies_on(on):
            return None
        if rule.field == "description":
            texts = [description]
        elif rule.field == "counterparty":
            texts = [counterparty or ""]
        else:
            texts = [description, counterparty or ""]
        if any(rx.search(t or "") for t in texts):
            return Classification(rule.vat_category, rule.input_type or INPUT_GENERAL, "rule",
                                  RULE_CONFIDENCE, rule.note or f"Matched rule '{rule.pattern}'", rule.id)
        return None


def validate_rule_pattern(match_type: str, pattern: str) -> str | None:
    """Return an error message, or None if the pattern is usable."""
    if not pattern.strip():
        return "Pattern is required"
    if match_type == "regex":
        try:
            re.compile(pattern)
        except re.error as e:
            return f"Invalid regular expression: {e}"
    return None
