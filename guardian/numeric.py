from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any

# The platform's rule for crypto amounts shown to people (CLAUDE.md): at most
# eight decimals.
MAX_DISPLAY_DECIMALS = 8


def to_decimal(value: Any) -> Decimal:
    """Convert any CCXT-returned numeric (float, int, str, None) to Decimal
    safely - always via str() first so float binary imprecision never leaks
    into money/quantity math. Never call Decimal(float) directly."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def plain(value: Decimal) -> Decimal:
    """The same number, with the padding the database adds taken off.

    Guardian's quantities live in numeric(38,18) columns and come back as
    Decimal("1.000000000000000000") and Decimal("0E-18"): equal to 1 and 0,
    and rendering as neither. Only the exponent changes here, never the value,
    so a detector that subtracts or compares these sees exactly what it saw
    before - `plain(d) == d`, asserted in the tests. Zero is Decimal("0"), and
    a whole number keeps its zeros as digits rather than in the exponent
    (normalize() alone turns 10.000 into 1E+1)."""
    if not value.is_finite():
        return value
    if value == 0:
        return Decimal("0")
    stripped = value.normalize()
    sign, digits, exponent = stripped.as_tuple()
    if exponent > 0:
        # Built from the tuple rather than quantized: quantize is bound by the
        # context precision (28 digits) and a 1E+30 would raise mid-alert.
        stripped = Decimal((sign, digits + (0,) * exponent, 0))
    return stripped


def format_amount(value: Any, max_decimals: int = MAX_DISPLAY_DECIMALS) -> str:
    """An amount for a message a person reads.

    At most `max_decimals` decimals, trailing zeros stripped, plain "0" for
    zero, never scientific notation: "0.5", "1234.5", "10", "0.00000001", "0".
    A non-zero amount too small to show at that precision is "<0.00000001"
    rather than "0" - a drop that fired is not a drop of nothing, and the text
    must not say it is. Strings and floats are taken the way `to_decimal` takes
    them; a value that is not a number at all comes back as its own text."""
    try:
        amount = to_decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    if not amount.is_finite():
        return str(value)
    unit = Decimal(1).scaleb(-max_decimals)
    with localcontext() as ctx:
        # quantize refuses a result wider than the context's precision (28 by
        # default); an amount never needs 60 digits, so this only ever matters
        # to a bug, and a bug should format rather than raise mid-alert.
        ctx.prec = 60
        rounded = amount.quantize(unit, rounding=ROUND_HALF_UP)
    if rounded == 0:
        if amount == 0:
            return "0"
        return f"{'-' if amount < 0 else ''}<{format(unit, 'f')}"
    # format(..., "f") is fixed-point: str() would write 1E-8 for a satoshi.
    return format(plain(rounded), "f")
