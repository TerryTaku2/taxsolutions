from sqlalchemy import select

from vatsys.classify import Classifier, parse_category
from vatsys.models import ClassificationRule


def classifier(session, business_id=None):
    return Classifier(list(session.scalars(select(ClassificationRule))), business_id)


def test_default_rules(session):
    c = classifier(session)
    cases = [
        ("purchase", "Monthly bank charges", "exempt", "general"),
        ("purchase", "Salaries August", "out_of_scope", "general"),
        ("purchase", "ZIMRA VAT payment July", "out_of_scope", "general"),
        ("purchase", "Import VAT bill of entry", "standard", "import"),
        ("sale", "Cross-border freight Harare-Lusaka", "zero", "general"),
        ("purchase", "Client entertainment", "standard", "non_deductible"),
        ("purchase", "Truck purchase - Volvo", "standard", "capital"),
        ("purchase", "Truck tyres x6", "standard", "general"),
        ("purchase", "Truck service and parts", "standard", "general"),
    ]
    for direction, text, category, input_type in cases:
        result = c.classify(direction=direction, description=text, counterparty=None)
        assert (result.vat_category, result.input_type) == (category, input_type), text


def test_unmatched_is_low_confidence(session):
    r = classifier(session).classify(direction="sale", description="Local freight", counterparty="Acme")
    assert r.vat_category == "standard" and r.source == "default" and r.confidence < 0.8


def test_file_category_wins(session):
    r = classifier(session).classify(direction="purchase", description="Bank charges", counterparty=None,
                                     file_category="Zero rated")
    assert r.vat_category == "zero" and r.source == "file"


def test_business_rule_beats_global(session, business):
    session.add(ClassificationRule(business_id=business.id, priority=500, match_type="contains",
                                   field="any", pattern="bank charges", direction="any", vat_category="standard"))
    session.flush()
    r = classifier(session, business.id).classify(direction="purchase", description="Bank charges",
                                                  counterparty=None)
    assert r.vat_category == "standard" and r.source == "rule"
    other = classifier(session, business.id + 1).classify(direction="purchase", description="Bank charges",
                                                          counterparty=None)
    assert other.vat_category == "exempt"


def test_parse_category():
    assert parse_category("15.5%") == "standard"
    assert parse_category("Zero-rated") == "zero"
    assert parse_category("EXEMPT") == "exempt"
    assert parse_category("Out of scope") == "out_of_scope"
    assert parse_category("something") is None
