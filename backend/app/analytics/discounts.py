"""Discount / promo-code report from the aggregate feed (discount_daily).

For a period and optional store, one row per discount code:
  discount_total   = dollars given away under this code (attributable to it only)
  revenue          = net revenue of the items that carried the code
  units            = units of those items
  transaction_count= receipts that used the code (NOT additive across codes:
                     a receipt with two codes appears under both)
  discount_depth   = discount_total / (revenue + discount_total): how deep the
                     code cuts on the items it touches
  share            = discount_total / all discounts in the period
The '' code is undiscounted items and is reported separately as `undiscounted`.
Deltas compare against the previous period of the same length.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.money import ZERO, money, pct_change, rate, safe_div
from app.analytics.periods import Period
from app.models import DiscountDaily, Store


def _load(session: Session, period: Period, store_code: str | None) -> dict[str, dict]:
    stmt = select(DiscountDaily).where(DiscountDaily.sale_date >= period.start, DiscountDaily.sale_date <= period.end)
    if store_code:
        stmt = stmt.join(Store, DiscountDaily.store_id == Store.id).where(Store.code == store_code)
    agg: dict[str, dict] = defaultdict(lambda: {"discount_total": ZERO, "revenue": ZERO, "units": 0, "transaction_count": 0, "days": 0})
    for row in session.execute(stmt).scalars():
        a = agg[row.discount_name]
        a["discount_total"] += row.discount_total
        a["revenue"] += row.revenue
        a["units"] += row.units
        a["transaction_count"] += row.transaction_count
        a["days"] += 1
    return agg


def discount_report(session: Session, period: Period, store_code: str | None = None, limit: int = 25) -> dict:
    current = _load(session, period, store_code)
    prev_period = Period("previous", period.start - timedelta(days=period.days), period.start - timedelta(days=1))
    previous = _load(session, prev_period, store_code)

    undiscounted = current.pop("", None)
    previous.pop("", None)
    total = sum((a["discount_total"] for a in current.values()), ZERO)
    prev_total = sum((a["discount_total"] for a in previous.values()), ZERO)

    codes = []
    for name, a in current.items():
        prev = previous.get(name)
        codes.append({
            "discount_name": name,
            "discount_total": money(a["discount_total"]),
            "revenue": money(a["revenue"]),
            "units": a["units"],
            "transaction_count": a["transaction_count"],
            "discount_depth": rate(safe_div(a["discount_total"], a["revenue"] + a["discount_total"])),
            "share": rate(safe_div(a["discount_total"], total)),
            "previous_discount_total": money(prev["discount_total"]) if prev else None,
            "vs_previous_pct": pct_change(a["discount_total"], prev["discount_total"]) if prev else None,
        })
    codes.sort(key=lambda c: (-c["discount_total"], c["discount_name"]))
    return {
        "period": period.to_dict(),
        "previous_period": prev_period.to_dict(),
        "store": store_code,
        "total_discounts": money(total),
        "previous_total_discounts": money(prev_total),
        "vs_previous_pct": pct_change(total, prev_total),
        "code_count": len(codes),
        "undiscounted": {
            "revenue": money(undiscounted["revenue"]), "units": undiscounted["units"], "transaction_count": undiscounted["transaction_count"]
        } if undiscounted else None,
        "codes": codes[:limit],
    }
