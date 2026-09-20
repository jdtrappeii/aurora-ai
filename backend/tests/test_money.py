from decimal import Decimal

from app.analytics.money import D, money, pct_change, rate, safe_div


def test_money_rounds_half_up_not_bankers():
    assert money("2.345") == Decimal("2.35")
    assert money("2.355") == Decimal("2.36")
    assert money("0.005") == Decimal("0.01")


def test_float_input_does_not_leak_binary_noise():
    assert D(0.1) + D(0.2) == Decimal("0.3")
    assert money(19.99 * 3) == Decimal("59.97")


def test_safe_div_returns_zero_on_zero_denominator():
    assert safe_div(10, 0) == Decimal("0")
    assert safe_div("1", "3") == Decimal(1) / Decimal(3)


def test_rate_four_places():
    assert rate(Decimal(1) / Decimal(3)) == Decimal("0.3333")
    assert rate("0.54325") == Decimal("0.5433")


def test_pct_change():
    assert pct_change(110, 100) == Decimal("0.1000")
    assert pct_change(90, 100) == Decimal("-0.1000")
    assert pct_change(50, -100) == Decimal("1.5000")  # improvement from a loss
    assert pct_change(5, 0) is None
