"""Amounts read as amounts: no padding, no E-notation, at most 8 decimals.

Guardian's quantities moved into numeric(38,18) columns, which hand back
Decimal("1.000000000000000000") and Decimal("0E-18"). Those are the numbers
the quantity-drop detector interpolates into its alert text, so a customer
read "dropped by 0.500000000000000000 but only 0E-18 is explained". Two
helpers in guardian/numeric.py: `plain` takes the padding off a value without
changing it (what baseline.py returns), and `format_amount` is how an amount
is written for a person (what the operator rendering uses for evidence).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from guardian.alerts.base import AlertMessage
from guardian.numeric import format_amount, plain


@pytest.mark.parametrize("value, shown", [
    (Decimal("1E-18"), "<0.00000001"),
    (Decimal("0E-18"), "0"),
    (Decimal("0.10000000000000000000"), "0.1"),
    (Decimal("1234.5"), "1234.5"),
    (1234.5, "1234.5"),
    ("1234.50", "1234.5"),
    (Decimal("10.000000000000000000"), "10"),
    (Decimal("0.00000001"), "0.00000001"),
    (Decimal("0.123456789"), "0.12345679"),
    (Decimal("0"), "0"),
    (None, "0"),
    (Decimal("-0.5000"), "-0.5"),
    (Decimal("-1E-18"), "-<0.00000001"),
    (Decimal("12345678901234567890.123456789"), "12345678901234567890.12345679"),
])
def test_format_amount(value, shown):
    assert format_amount(value) == shown


@pytest.mark.parametrize("value", [
    Decimal("1E-18"), Decimal("1E-8"), Decimal("1E-7"), Decimal("1E+30"), Decimal("0E-18"),
    Decimal("123456789.000000000000000001"), 1e-7, 1e21,
])
def test_format_amount_never_uses_scientific_notation(value):
    assert "e" not in format_amount(value).lower()


def test_format_amount_of_a_non_number_is_its_own_text():
    assert format_amount("not-a-number") == "not-a-number"


@pytest.mark.parametrize("padded, reads", [
    ("1.000000000000000000", "1"),
    ("0E-18", "0"),
    ("0.100000000000000000", "0.1"),
    ("10.000000000000000000", "10"),
    ("1234.500000000000000000", "1234.5"),
    ("0.000000010000000000", "0.00000001"),
])
def test_plain_keeps_the_value_and_drops_the_padding(padded, reads):
    value = Decimal(padded)

    cleaned = plain(value)

    assert cleaned == value, "plain() may change how a number reads, never what it is"
    assert cleaned - value == 0 and (cleaned > value) is False and (cleaned < value) is False
    assert format_amount(cleaned) == reads
    assert "E+" not in str(cleaned), "a whole number keeps its zeros as digits"


def test_plain_is_exact_in_arithmetic_with_exchange_values():
    """What the detector does with these: subtract the current balance from
    the stored one and compare against a tolerance."""
    previous = plain(Decimal("1.000000000000000000"))
    current = Decimal("0.5")
    assert previous - current == Decimal("1.000000000000000000") - current
    assert str(previous - current) == "0.5"


def test_evidence_decimals_render_as_amounts():
    """The operator rendering is the one place in guardian/alerts that writes
    numbers out. Only real numbers are formatted: a numeric-looking string is
    an identifier."""
    alert = AlertMessage(
        account_label="acct", rule="unexplained_quantity_decrease", severity="CRITICAL", confidence="high",
        message="m",
        evidence={"amount": Decimal("0.500000000000000000"), "zero": Decimal("0E-18"),
                  "order_id": "000123450000", "count": 3, "ratio": 2.5},
    )

    text = alert.render()

    assert "  amount: 0.5\n" in text and "  zero: 0\n" in text
    assert "  order_id: 000123450000\n" in text
    assert "  count: 3\n" in text and "  ratio: 2.5\n" in text
