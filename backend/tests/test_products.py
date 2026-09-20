from decimal import Decimal

from app.analytics.periods import week_containing
from app.analytics.products import category_profitability, product_profitability
from tests.conftest import MONDAY, dt


def test_product_and_category_profitability(session, seed):
    seed.product("A", "Flower", "10", "25")
    seed.product("B", "Edibles", "4", "10")
    seed.product("C", "Flower", "20", "50")
    seed.sale(dt(MONDAY, 10), [("A", 2, "25"), ("B", 1, "10")])  # A: rev 50 cogs 20 gp 30 ; B: rev 10 cogs 4 gp 6
    seed.sale(dt(MONDAY, 11), [("C", 1, "40")])  # C: rev 40 cogs 20 gp 20 (disc 10)
    seed.sale(dt(MONDAY, 12), [("B", 5, "10")], status="voided")
    seed.commit()
    period = week_containing(MONDAY)

    products = product_profitability(session, period)
    assert [p["sku"] for p in products] == ["A", "C", "B"]  # sorted by gross profit desc
    a, c, b = products
    assert (a["revenue"], a["cogs"], a["gross_profit"], a["gross_margin"]) == (Decimal("50.00"), Decimal("20.00"), Decimal("30.00"), Decimal("0.6000"))
    assert (c["discount_total"], c["discount_rate"]) == (Decimal("10.00"), Decimal("0.2000"))
    assert b["units"] == 1  # voided sale ignored

    cats = category_profitability(session, period)
    flower = next(x for x in cats if x["category"] == "Flower")
    edibles = next(x for x in cats if x["category"] == "Edibles")
    assert flower["revenue"] == Decimal("90.00")
    assert flower["gross_profit"] == Decimal("50.00")
    assert flower["revenue_share"] == Decimal("0.9000")
    assert edibles["revenue_share"] == Decimal("0.1000")
    assert flower["transactions"] == 2 and edibles["transactions"] == 1
