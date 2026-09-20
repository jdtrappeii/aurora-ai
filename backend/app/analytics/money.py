"""Deterministic money arithmetic.

All core figures are computed with Decimal and rounded ONCE at the boundary
with ROUND_HALF_UP. Never compute money with floats.
"""
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")
RATE = Decimal("0.0001")


def D(value) -> Decimal:
    """Coerce any numeric/str value to Decimal without going through float repr."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def money(value) -> Decimal:
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def rate(value) -> Decimal:
    """A ratio stored to 4 decimal places (e.g. 0.5432 == 54.32%)."""
    return D(value).quantize(RATE, rounding=ROUND_HALF_UP)


def safe_div(numerator, denominator) -> Decimal:
    numerator, denominator = D(numerator), D(denominator)
    if denominator == ZERO:
        return ZERO
    return numerator / denominator


def pct_change(current, previous) -> Decimal | None:
    """(current - previous) / |previous|. None when there is no baseline."""
    current, previous = D(current), D(previous)
    if previous == ZERO:
        return None
    return rate((current - previous) / abs(previous))
