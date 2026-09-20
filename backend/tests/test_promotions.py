from datetime import timedelta
from decimal import Decimal

from app.analytics.promotions import promotion_results, verdict
from tests.conftest import MONDAY, days_ago, dt


def test_verdict_rules():
    base = {"revenue_per_day": Decimal("100"), "gross_profit_per_day": Decimal("50")}
    assert verdict({"revenue_per_day": Decimal("130"), "gross_profit_per_day": Decimal("55")}, base)[0] == "profitable"
    assert verdict({"revenue_per_day": Decimal("130"), "gross_profit_per_day": Decimal("40")}, base)[0] == "revenue_up_profit_down"
    assert verdict({"revenue_per_day": Decimal("90"), "gross_profit_per_day": Decimal("40")}, base)[0] == "unprofitable"
    assert verdict({"revenue_per_day": Decimal("90"), "gross_profit_per_day": Decimal("40")}, None)[0] == "no_baseline"


def test_deal_autopsy_against_28_day_baseline(session, seed):
    """Flower 30% off for 7 days.

    Baseline (28 days before): eligible Flower sold 1x A @25 per day for 28 days
      -> revenue 700, GP 420 (cost 10) -> 25/day revenue, 15/day GP
    Promo week: 2x A @17.50 per day for 7 days
      -> revenue 245, GP 105 (14 units x 7.50)  -> 35/day revenue, 15/day GP... make it worse: cost 10, price 17.50 => GP 7.50/unit
      2 units/day => 15/day GP. Push volume to 3/day => 52.50 rev/day, 22.50 GP/day  (profitable)
    Use 2/day to get the margin-killer case: revenue up (35 > 25), GP flat (15 == 15) -> not gp_up -> revenue_up_profit_down.
    """
    seed.product("A", "Flower", "10", "25")
    seed.product("B", "Edibles", "4", "10")
    start, end = days_ago(7), days_ago(1)
    promo = seed.promotion("Flower 30", start, end, category="Flower", value="30")
    for i in range(28):
        d = start - timedelta(days=28 - i)
        seed.sale(dt(d), [("A", 1, "25"), ("B", 1, "10")])  # B is not eligible and must not enter the baseline
    for i in range(7):
        d = start + timedelta(days=i)
        seed.sale(dt(d), [("A", 2, "17.50")], promo=promo)
        seed.sale(dt(d, 15), [("B", 1, "10")])  # a non-promo ticket in the window for attachment rate
    seed.commit()

    (r,) = promotion_results(session)
    assert r["promotion"] == "Flower 30"
    assert r["days"] == 7
    assert r["revenue"] == Decimal("245.00")
    assert r["gross_profit"] == Decimal("105.00")
    assert r["discount_total"] == Decimal("105.00")  # 7.50 x 14 units
    assert r["revenue_per_day"] == Decimal("35.00")
    assert r["gross_profit_per_day"] == Decimal("15.00")
    assert r["attachment_rate"] == Decimal("0.5000")  # 7 promo tickets of 14
    assert r["baseline"]["revenue"] == Decimal("700.00")
    assert r["baseline"]["revenue_per_day"] == Decimal("25.00")
    assert r["baseline"]["gross_profit_per_day"] == Decimal("15.00")
    assert r["vs_baseline"]["revenue_per_day_pct"] == Decimal("0.4000")
    assert r["vs_baseline"]["gross_profit_per_day_pct"] == Decimal("0.0000")
    assert r["vs_baseline"]["gross_margin_delta"] == Decimal("0.4286") - Decimal("0.6000")
    assert r["verdict"] == "revenue_up_profit_down"
