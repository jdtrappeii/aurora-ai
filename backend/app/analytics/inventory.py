"""Inventory value, aging, and 30-day sell-through.

Position = latest snapshot per (store, product) on or before as_of.
  inventory_value     = sum(quantity_on_hand * unit_cost)
  age_days            = as_of - received_date
  sell_through_30d    = units_sold_30d / (units_sold_30d + quantity_on_hand)
  daily_velocity      = units_sold_30d / 30
  days_of_supply      = quantity_on_hand / daily_velocity  (None when velocity is 0)

Classification (by 30-day sell-through, applied only to SKUs with stock on hand):
  dead   : no units sold in 30 days
  slow   : sell-through < 0.25
  hot    : sell-through >= 0.75
  normal : otherwise
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.lines import completed, load_lines
from app.analytics.money import ZERO, D, money, rate, safe_div
from app.analytics.periods import trailing_days
from app.analytics.scope import store_predicate
from app.models import Category, InventorySnapshot, Product, Store

AGE_BUCKETS = (("0-30", 0, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("90+", 91, None))
SLOW_THRESHOLD = Decimal("0.25")
HOT_THRESHOLD = Decimal("0.75")


@dataclass
class Position:
    store_code: str
    sku: str
    product: str
    category: str
    quantity_on_hand: int
    unit_cost: Decimal
    received_date: date | None
    last_sale_date: date | None
    snapshot_date: date


def latest_positions(session: Session, as_of: date, store_code: str | None = None) -> list[Position]:
    latest = (
        select(
            InventorySnapshot.store_id,
            InventorySnapshot.product_id,
            func.max(InventorySnapshot.snapshot_date).label("snapshot_date"),
        )
        .where(InventorySnapshot.snapshot_date <= as_of)
        .group_by(InventorySnapshot.store_id, InventorySnapshot.product_id)
        .subquery()
    )
    stmt = (
        select(
            Store.code,
            Product.sku,
            Product.name,
            Category.name,
            InventorySnapshot.quantity_on_hand,
            InventorySnapshot.unit_cost,
            InventorySnapshot.received_date,
            InventorySnapshot.last_sale_date,
            InventorySnapshot.snapshot_date,
        )
        .join(
            latest,
            (InventorySnapshot.store_id == latest.c.store_id)
            & (InventorySnapshot.product_id == latest.c.product_id)
            & (InventorySnapshot.snapshot_date == latest.c.snapshot_date),
        )
        .join(Store, InventorySnapshot.store_id == Store.id)
        .join(Product, InventorySnapshot.product_id == Product.id)
        .join(Category, Product.category_id == Category.id)
        .order_by(Store.code, Product.sku)
    )
    if store_code:
        stmt = stmt.where(store_predicate(store_code))
    return [Position(*row) for row in session.execute(stmt).all()]


def age_bucket(age_days: int | None) -> str:
    if age_days is None:
        return "unknown"
    for label, lo, hi in AGE_BUCKETS:
        if age_days >= lo and (hi is None or age_days <= hi):
            return label
    return "unknown"


def classify(units_sold_30d: int, quantity_on_hand: int, sell_through: Decimal) -> str:
    if quantity_on_hand <= 0:
        return "out_of_stock"
    if units_sold_30d == 0:
        return "dead"
    if sell_through < SLOW_THRESHOLD:
        return "slow"
    if sell_through >= HOT_THRESHOLD:
        return "hot"
    return "normal"


def inventory_report(session: Session, as_of: date, store_code: str | None = None) -> dict:
    positions = latest_positions(session, as_of, store_code)
    window = trailing_days(as_of, 30)
    sold_30d: dict[tuple[str, str], int] = defaultdict(int)
    for ln in completed(load_lines(session, window, store_code)):
        sold_30d[(ln.store_code, ln.sku)] += ln.quantity

    items = []
    total_value = ZERO
    aging_value = {label: ZERO for label, _, _ in AGE_BUCKETS}
    aging_value["unknown"] = ZERO
    class_counts: dict[str, int] = defaultdict(int)
    class_value: dict[str, Decimal] = defaultdict(lambda: ZERO)

    for p in positions:
        value = D(p.quantity_on_hand) * D(p.unit_cost)
        total_value += value
        age = (as_of - p.received_date).days if p.received_date else None
        bucket = age_bucket(age)
        aging_value[bucket] += value

        units = sold_30d.get((p.store_code, p.sku), 0)
        st = rate(safe_div(units, units + p.quantity_on_hand))
        velocity = safe_div(units, 30)
        days_of_supply = money(safe_div(p.quantity_on_hand, velocity)) if velocity > ZERO else None
        status = classify(units, p.quantity_on_hand, st)
        class_counts[status] += 1
        class_value[status] += value

        items.append(
            {
                "store": p.store_code,
                "sku": p.sku,
                "product": p.product,
                "category": p.category,
                "quantity_on_hand": p.quantity_on_hand,
                "unit_cost": money(p.unit_cost),
                "inventory_value": money(value),
                "received_date": p.received_date.isoformat() if p.received_date else None,
                "last_sale_date": p.last_sale_date.isoformat() if p.last_sale_date else None,
                "age_days": age,
                "age_bucket": bucket,
                "units_sold_30d": units,
                "sell_through_30d": st,
                "daily_velocity": rate(velocity),
                "days_of_supply": days_of_supply,
                "status": status,
            }
        )

    items.sort(key=lambda r: (-r["inventory_value"], r["sku"]))
    return {
        "as_of": as_of.isoformat(),
        "store": store_code,
        "sku_count": len(items),
        "inventory_value": money(total_value),
        "aging_value": {k: money(v) for k, v in aging_value.items()},
        "cash_tied_over_90_days": money(aging_value["90+"]),
        "status_counts": dict(class_counts),
        "status_value": {k: money(v) for k, v in class_value.items()},
        "items": items,
    }
