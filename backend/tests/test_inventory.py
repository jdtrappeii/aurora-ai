from datetime import timedelta
from decimal import Decimal

from app.analytics.inventory import age_bucket, classify, inventory_report
from tests.conftest import MONDAY, days_ago, dt


def test_age_buckets_and_classification_rules():
    assert age_bucket(0) == "0-30" and age_bucket(30) == "0-30"
    assert age_bucket(31) == "31-60" and age_bucket(60) == "31-60"
    assert age_bucket(61) == "61-90" and age_bucket(90) == "61-90"
    assert age_bucket(91) == "90+" and age_bucket(400) == "90+"
    assert age_bucket(None) == "unknown"

    assert classify(0, 10, Decimal("0")) == "dead"
    assert classify(2, 10, Decimal("0.1667")) == "slow"
    assert classify(5, 5, Decimal("0.5")) == "normal"
    assert classify(9, 3, Decimal("0.75")) == "hot"
    assert classify(9, 0, Decimal("1")) == "out_of_stock"


def test_inventory_report_uses_latest_snapshot_and_30_day_sales(session, seed):
    as_of = MONDAY
    seed.product("A", "Flower", "10", "25")
    seed.product("B", "Edibles", "4", "10")
    seed.product("D", "Topicals", "8", "22")
    # A: older snapshot must be ignored in favour of the latest one on/before as_of
    seed.inventory("A", days_ago(10), 999, days_ago(120))
    seed.inventory("A", days_ago(1), 20, days_ago(100), cost="10")   # aged 100d -> 90+ ; value 200
    seed.inventory("B", days_ago(1), 5, days_ago(5))                  # value 20 ; 0-30
    seed.inventory("D", days_ago(1), 48, days_ago(140))               # value 384 ; dead
    seed.inventory("A", days_ago(-2), 1, days_ago(0))                 # future snapshot: ignored
    # 30-day sales: A sold 20 units, B sold 15 units, D none
    seed.sale(dt(days_ago(5)), [("A", 20, "25"), ("B", 15, "10")])
    seed.sale(dt(days_ago(40)), [("D", 3, "22")])  # outside 30 days
    seed.commit()

    rep = inventory_report(session, as_of)
    assert rep["inventory_value"] == Decimal("604.00")
    assert rep["aging_value"]["90+"] == Decimal("584.00")
    assert rep["cash_tied_over_90_days"] == Decimal("584.00")
    assert rep["aging_value"]["0-30"] == Decimal("20.00")

    by_sku = {i["sku"]: i for i in rep["items"]}
    a, b, d = by_sku["A"], by_sku["B"], by_sku["D"]
    assert a["quantity_on_hand"] == 20 and a["units_sold_30d"] == 20
    assert a["sell_through_30d"] == Decimal("0.5000")
    assert a["days_of_supply"] == Decimal("30.00")  # 20 on hand / (20/30 per day)
    assert a["status"] == "normal"
    assert b["sell_through_30d"] == Decimal("0.7500") and b["status"] == "hot"
    assert b["days_of_supply"] == Decimal("10.00")
    assert d["status"] == "dead" and d["days_of_supply"] is None
    assert rep["status_counts"] == {"normal": 1, "hot": 1, "dead": 1}
    assert rep["status_value"]["dead"] == Decimal("384.00")
