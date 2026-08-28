"""Content-level PII scan/redact (item 2.15) -- regex pattern matching, not
NER; see the module docstring for exactly what that does and doesn't catch.
"""

from __future__ import annotations

from odyssey.pii import redact_pii, scan_pii
from odyssey.primitives import PiiRule

ALL_RULES: list[PiiRule] = ["EMAIL", "PHONE", "CREDIT_CARD", "SSN"]


def test_scan_finds_an_email():
    preview = scan_pii("reach me at jane.doe@example.com please", ["EMAIL"])
    assert preview.total_rule_counts == {"EMAIL": 1}


def test_scan_finds_a_phone_number():
    preview = scan_pii("call 555-123-4567 today", ["PHONE"])
    assert preview.total_rule_counts == {"PHONE": 1}


def test_scan_finds_an_ssn():
    preview = scan_pii("ssn is 123-45-6789", ["SSN"])
    assert preview.total_rule_counts == {"SSN": 1}


def test_scan_finds_a_luhn_valid_credit_card():
    preview = scan_pii("card 4111 1111 1111 1111 on file", ["CREDIT_CARD"])
    assert preview.total_rule_counts == {"CREDIT_CARD": 1}


def test_scan_rejects_a_luhn_invalid_digit_run():
    """A random 16-digit run is not automatically a card -- Luhn is the
    real filter, not just "looks like the right shape"."""
    preview = scan_pii("order number 1234 5678 9012 3456", ["CREDIT_CARD"])
    assert preview.total_rule_counts == {}


def test_scan_only_applies_requested_rules():
    text = "email a@b.com and phone 555-123-4567"
    preview = scan_pii(text, ["EMAIL"])
    assert preview.total_rule_counts == {"EMAIL": 1}


def test_scan_samples_never_contain_the_raw_match():
    preview = scan_pii("email jane.doe@example.com now", ["EMAIL"])
    assert "jane.doe@example.com" not in preview.samples[0]["context"]
    assert "[EMAIL]" in preview.samples[0]["context"]


def test_scan_caps_samples_per_rule_at_three():
    text = " ".join(f"user{i}@example.com" for i in range(10))
    preview = scan_pii(text, ["EMAIL"])
    assert preview.total_rule_counts == {"EMAIL": 10}
    assert len(preview.samples) == 3


def test_scan_of_clean_text_finds_nothing():
    preview = scan_pii("just a normal sentence about booking Tuesday", ALL_RULES)
    assert preview.total_rule_counts == {}
    assert preview.samples == []


def test_redact_replaces_every_rule_requested():
    text = "email jane.doe@example.com, call 555-123-4567, ssn 123-45-6789"
    out = redact_pii(text, ALL_RULES)
    assert "jane.doe@example.com" not in out
    assert "555-123-4567" not in out
    assert "123-45-6789" not in out
    assert "[REDACTED_EMAIL]" in out
    assert "[REDACTED_PHONE]" in out
    assert "[REDACTED_SSN]" in out


def test_redact_only_touches_requested_rules():
    text = "email a@b.com and ssn 123-45-6789"
    out = redact_pii(text, ["EMAIL"])
    assert "[REDACTED_EMAIL]" in out
    assert "123-45-6789" in out  # SSN untouched -- not in the rule list


def test_redact_leaves_a_luhn_invalid_digit_run_alone():
    out = redact_pii("order 1234 5678 9012 3456", ["CREDIT_CARD"])
    assert "1234 5678 9012 3456" in out


def test_redact_a_valid_card_number():
    out = redact_pii("card 4111 1111 1111 1111", ["CREDIT_CARD"])
    assert "4111 1111 1111 1111" not in out
    assert "[REDACTED_CREDIT_CARD]" in out


def test_redact_of_clean_text_is_a_no_op():
    text = "just booking an appointment for Tuesday"
    assert redact_pii(text, ALL_RULES) == text
