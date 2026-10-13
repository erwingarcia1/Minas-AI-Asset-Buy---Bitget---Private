from __future__ import annotations

from decimal import Decimal
from typing import Any


def to_decimal(value: Any) -> Decimal:
    """Convert any CCXT-returned numeric (float, int, str, None) to Decimal
    safely - always via str() first so float binary imprecision never leaks
    into money/quantity math. Never call Decimal(float) directly."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
