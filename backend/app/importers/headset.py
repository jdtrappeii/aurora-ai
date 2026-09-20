"""Headset importer: turns the Headset MCP tool results (aggregate cannabis
retail data) into Aurora's normalized tables.

Headset does not expose receipt-level line items through the connector. It
reports totals by dimension, so the mapping is:

  retailer_get_stores                       -> stores            (code = "HS<storeId>")
  retailer_get_inventory (per store)        -> products + inventory_snapshots
  retailer_sales_by_dimension product,
      one store, one sold_date              -> one synthetic Sale per (store, day, sku),
                                               source="headset", one SaleItem carrying
                                               the EXACT line totals and ticket_count
  retailer_sales_by_dimension discount_name,
      one store, one sold_date              -> discount_daily
  retailer_sales_trend grain=day
      dimension=store                       -> daily_store_summaries (the day's true totals
                                               and ticket count)

Every payload handed to this module is an *envelope*:

  {"kind": "stores" | "inventory" | "products" | "discounts" | "store_days",
   "store_name": "<Headset store_name>"   (inventory / products / discounts),
   "sold_date": "YYYY-MM-DD"              (products / discounts),
   "snapshot_date": "YYYY-MM-DD"          (inventory),
   "result": <the raw tool result, i.e. {"rows": [...], "hasMore": bool} or {"stores": [...]}>}

`headset_sync` (app.integrations.headset.pull) builds these envelopes from a
live client; `import_headset_directory` reads them back from *.json files so a
pull can be recorded once and replayed. Nothing here is Planet-13 specific:
any Headset retailer account produces the same shapes.

Money arrives as JSON floats. They are converted through Decimal(repr(x)) and
rounded once to cents, so 67037.64 stays 67037.64.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.money import D, ZERO, money, safe_div
from app.analytics.scope import store_predicate
from app.importers.csv_importer import ImportError_, ImportResult, Lookups
from app.models import DailyStoreSummary, DiscountDaily, InventorySnapshot, Product, Sale, SaleItem, Store

SOURCE = "headset"
KINDS = ("stores", "inventory", "products", "discounts", "store_days")
UNKNOWN_CATEGORY = "Uncategorized"

# Headset store names for Florida panhandle stores sit in Central time. A
# postal-code prefix is a good enough default; override in stores.csv if needed.
STATE_TIMEZONES = {
    "FL": "America/New_York", "GA": "America/New_York", "NY": "America/New_York", "NJ": "America/New_York",
    "PA": "America/New_York", "MA": "America/New_York", "MD": "America/New_York", "OH": "America/New_York",
    "MI": "America/Detroit", "IL": "America/Chicago", "MO": "America/Chicago", "MN": "America/Chicago",
    "OK": "America/Chicago", "AR": "America/Chicago", "CO": "America/Denver", "MT": "America/Denver",
    "NM": "America/Denver", "AZ": "America/Phoenix", "NV": "America/Los_Angeles", "CA": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "WA": "America/Los_Angeles", "AK": "America/Anchorage",
}
CENTRAL_FL_ZIP_PREFIXES = ("324", "325")


def store_code_for(store_id: int | str) -> str:
    return f"HS{int(store_id)}"


def timezone_for(state: str | None, postal_code: str | None) -> str | None:
    if state == "FL" and postal_code and postal_code[:3] in CENTRAL_FL_ZIP_PREFIXES:
        return "America/Chicago"
    return STATE_TIMEZONES.get(state or "")


# ---------- envelope handling ----------

@dataclass
class Envelope:
    kind: str
    result: dict
    store_name: str | None = None
    sold_date: date | None = None
    snapshot_date: date | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict) -> "Envelope":
        kind = payload.get("kind")
        if kind not in KINDS:
            raise ImportError_(f"headset envelope: unknown kind {kind!r}; expected one of {KINDS}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ImportError_(f"headset envelope ({kind}): 'result' must be the raw tool result object")
        return cls(
            kind=kind,
            result=result,
            store_name=_clean(payload.get("store_name")),
            sold_date=_date(payload.get("sold_date"), "sold_date"),
            snapshot_date=_date(payload.get("snapshot_date"), "snapshot_date"),
        )

    @classmethod
    def load(cls, source) -> "Envelope":
        if isinstance(source, (str, Path)):
            text = Path(source).read_text(encoding="utf-8")
        elif isinstance(source, bytes):
            text = source.decode("utf-8")
        else:
            text = source.read()
            if isinstance(text, bytes):
                text = text.decode("utf-8")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise ImportError_(f"headset envelope: not valid JSON ({e})")
        return cls.from_dict(payload)

    @property
    def rows(self) -> list[dict]:
        rows = self.result.get("rows")
        if rows is None:
            raise ImportError_(f"headset {self.kind}: result has no 'rows'")
        return rows


def _clean(value) -> str | None:
    value = (str(value) if value is not None else "").strip()
    return value or None


def _date(value, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ImportError_(f"headset envelope: {field_name} is not a date: {value!r}")


def _money(row: dict, key: str) -> Decimal:
    return money(D(row.get(key)))


def _int(row: dict, key: str) -> int:
    value = row.get(key)
    if value is None:
        return 0
    return int(Decimal(repr(value)) if isinstance(value, float) else Decimal(str(value)))


def _unit(total: Decimal, quantity: int, places: str) -> Decimal:
    """Per-unit informational price: total / qty, or the total itself for a 0-qty row."""
    per = safe_div(total, quantity) if quantity else total
    return per.quantize(Decimal(places))


# ---------- store resolution ----------

def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def resolve_store(session: Session, store_name: str | None) -> Store:
    """Match a Headset store_name (whitespace-insensitive) to a stores row. Stores
    must be imported first so codes stay stable across pulls."""
    if not store_name:
        raise ImportError_("headset: store_name is required for this payload")
    wanted = _norm_name(store_name)
    for s in session.execute(select(Store)).scalars():
        if _norm_name(s.name) == wanted:
            return s
    raise ImportError_(f"headset: unknown store {store_name!r}; import the stores payload first")


# ---------- importers ----------

def import_stores(session: Session, env: Envelope) -> ImportResult:
    res = ImportResult("headset_stores")
    stores = env.result.get("stores")
    if stores is None:
        raise ImportError_("headset stores: result has no 'stores'")
    for i, row in enumerate(stores, start=1):
        try:
            code = store_code_for(row["storeId"])
            name = re.sub(r"\s+", " ", str(row["name"])).strip()
        except (KeyError, TypeError, ValueError):
            res.errors.append(f"store {i}: needs storeId and name")
            res.skipped += 1
            continue
        addr = row.get("address") or {}
        tz = timezone_for(_clean(addr.get("state")), _clean(addr.get("postalCode")))
        address = ", ".join(
            p for p in (_clean(addr.get("address1")), _clean(addr.get("city")),
                        " ".join(x for x in (_clean(addr.get("state")), _clean(addr.get("postalCode"))) if x) or None)
            if p
        ) or None
        state = (_clean(addr.get("state")) or "").upper() or None
        store = session.execute(select(Store).where(Store.code == code)).scalar_one_or_none()
        if store is None:
            session.add(Store(code=code, name=name, timezone=tz, address=address, state=state))
            res.inserted += 1
        else:
            store.name = name
            if tz and not store.timezone:
                store.timezone = tz
            if address and not store.address:
                store.address = address
            if state and not store.state:
                store.state = state
            res.updated += 1
    session.commit()
    return res


def _upsert_product(session: Session, lk: Lookups, sku: str, name: str, category: str | None,
                    brand: str | None, vendor: str | None, unit_cost: Decimal | None, price: Decimal | None) -> tuple[Product, bool]:
    """Products are keyed on Headset's retailer SKU. Sales rows only carry sku +
    name, so category/brand/vendor are filled from whichever payload knows them
    (inventory) and never downgraded to Uncategorized once known."""
    product = lk.product(sku)
    created = False
    if product is None:
        product = Product(
            sku=sku, name=name, category_id=lk.category(category or UNKNOWN_CATEGORY).id,
            brand_id=lk.brand(brand).id if brand else None, vendor_id=lk.vendor(vendor).id if vendor else None,
            unit_cost=unit_cost if unit_cost is not None else ZERO, retail_price=price if price is not None else ZERO,
        )
        session.add(product)
        session.flush()
        lk._cache[("Product", sku)] = product
        created = True
    else:
        if name:
            product.name = name
        if category:
            product.category_id = lk.category(category).id
        if brand:
            product.brand_id = lk.brand(brand).id
        if vendor:
            product.vendor_id = lk.vendor(vendor).id
        if unit_cost is not None and unit_cost > ZERO:
            product.unit_cost = unit_cost
        if price is not None and price > ZERO:
            product.retail_price = price
    return product, created


def import_inventory(session: Session, env: Envelope) -> ImportResult:
    """Per-product rows of retailer_get_inventory for ONE store. Unit cost is
    on_hand_cost_value / on_hand_units; a zero-stock row keeps the product's
    known cost. Headset gives no received_date, so aging stays 'unknown'."""
    res = ImportResult("headset_inventory")
    store = resolve_store(session, env.store_name)
    snapshot_date = env.snapshot_date or date.today()
    lk = Lookups(session)
    for i, row in enumerate(env.rows, start=1):
        sku = _clean(row.get("sku"))
        if not sku:
            res.errors.append(f"inventory row {i}: missing sku")
            res.skipped += 1
            continue
        qoh = _int(row, "on_hand_units")
        cost_value = _money(row, "on_hand_cost_value")
        unit_cost = _unit(cost_value, qoh, "0.0001") if qoh > 0 else None
        price = _money(row, "price") if row.get("price") is not None else None
        product, _ = _upsert_product(
            session, lk, sku, _clean(row.get("product_name")) or sku, _clean(row.get("category")),
            _clean(row.get("brand")), _clean(row.get("vendor")), unit_cost, price,
        )
        snap = session.execute(
            select(InventorySnapshot).where(
                InventorySnapshot.store_id == store.id,
                InventorySnapshot.product_id == product.id,
                InventorySnapshot.snapshot_date == snapshot_date,
            )
        ).scalar_one_or_none()
        values = dict(quantity_on_hand=qoh, unit_cost=unit_cost if unit_cost is not None else product.unit_cost)
        if snap is None:
            session.add(InventorySnapshot(store_id=store.id, product_id=product.id, snapshot_date=snapshot_date, **values))
            res.inserted += 1
        else:
            for k, v in values.items():
                setattr(snap, k, v)
            res.updated += 1
    session.commit()
    return res


def transaction_id_for(store_code: str, sold_date: date, sku: str) -> str:
    return f"HS:{store_code}:{sold_date.isoformat()}:{sku}"


def import_products(session: Session, env: Envelope) -> ImportResult:
    """retailer_sales_by_dimension(dimension="product") for ONE store and ONE
    sold_date -> one synthetic sale + line per SKU carrying the exact totals."""
    res = ImportResult("headset_products")
    store = resolve_store(session, env.store_name)
    if env.sold_date is None:
        raise ImportError_("headset products: sold_date is required")
    sold_at = datetime.combine(env.sold_date, time(12, 0))
    lk = Lookups(session)
    for i, row in enumerate(env.rows, start=1):
        sku = _clean(row.get("sku"))
        if not sku:
            res.errors.append(f"product row {i}: missing sku ({row.get('product_name')!r})")
            res.skipped += 1
            continue
        qty = _int(row, "total_units")
        gross = _money(row, "total_gross_sales")
        revenue = _money(row, "total_revenue")
        cogs = _money(row, "total_cost")
        discounts = _money(row, "total_discounts") if row.get("total_discounts") is not None else gross - revenue
        tickets = _int(row, "transaction_count")
        product, _ = _upsert_product(session, lk, sku, _clean(row.get("product_name")) or sku, None, None, None, None, None)

        txn = transaction_id_for(store.code, env.sold_date, sku)
        sale = session.execute(select(Sale).where(Sale.transaction_id == txn)).scalar_one_or_none()
        if sale is None:
            sale = Sale(transaction_id=txn, store_id=store.id, sold_at=sold_at, status="completed", source=SOURCE)
            session.add(sale)
            session.flush()
            created = True
        else:
            sale.sold_at = sold_at
            created = False
        values = dict(
            product_id=product.id, quantity=qty,
            regular_price=_unit(gross, qty, "0.01"), sale_price=_unit(revenue, qty, "0.01"),
            discount_amount=discounts, unit_cost=_unit(cogs, qty, "0.0001"),
            gross_total=gross, revenue_total=revenue, cogs_total=cogs, ticket_count=tickets,
        )
        item = session.execute(select(SaleItem).where(SaleItem.sale_id == sale.id, SaleItem.line_no == 1)).scalar_one_or_none()
        if item is None:
            session.add(SaleItem(sale_id=sale.id, line_no=1, **values))
        else:
            for k, v in values.items():
                setattr(item, k, v)
        if created:
            res.inserted += 1
        else:
            res.updated += 1
    session.commit()
    return res


def import_discounts(session: Session, env: Envelope) -> ImportResult:
    """retailer_sales_by_dimension(dimension="discount_name") for ONE store and
    ONE sold_date. The null-code row (undiscounted items) is kept as ''."""
    res = ImportResult("headset_discounts")
    store = resolve_store(session, env.store_name)
    if env.sold_date is None:
        raise ImportError_("headset discounts: sold_date is required")
    for row in env.rows:
        name = _clean(row.get("discount_name")) or ""
        values = dict(
            revenue=_money(row, "total_revenue"), units=_int(row, "total_units"),
            discount_total=_money(row, "total_discounts"), transaction_count=_int(row, "transaction_count"),
        )
        existing = session.execute(
            select(DiscountDaily).where(
                DiscountDaily.store_id == store.id, DiscountDaily.sale_date == env.sold_date, DiscountDaily.discount_name == name
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(DiscountDaily(store_id=store.id, sale_date=env.sold_date, discount_name=name, **values))
            res.inserted += 1
        else:
            for k, v in values.items():
                setattr(existing, k, v)
            res.updated += 1
    session.commit()
    return res


def import_store_days(session: Session, env: Envelope) -> ImportResult:
    """retailer_sales_trend(grain="day", dimension="store") rows -> one
    daily_store_summaries row per (store, day). Any number of stores and days."""
    res = ImportResult("headset_store_days")
    for i, row in enumerate(env.rows, start=1):
        try:
            store = resolve_store(session, _clean(row.get("store_name")))
            sale_date = date.fromisoformat(str(row["sold_date"])[:10])
        except (ImportError_, KeyError, ValueError) as e:
            res.errors.append(f"store-day row {i}: {e}")
            res.skipped += 1
            continue
        gross = _money(row, "total_gross_sales")
        revenue = _money(row, "total_revenue")
        cogs = _money(row, "total_cost")
        values = dict(
            transaction_count=_int(row, "transaction_count"), units=_int(row, "total_units"),
            gross_sales=gross, revenue=revenue, cogs=cogs, gross_profit=revenue - cogs,
            discount_total=_money(row, "total_discounts") if row.get("total_discounts") is not None else gross - revenue,
        )
        existing = session.execute(
            select(DailyStoreSummary).where(
                DailyStoreSummary.store_id == store.id, DailyStoreSummary.sale_date == sale_date, DailyStoreSummary.source == SOURCE
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(DailyStoreSummary(store_id=store.id, sale_date=sale_date, source=SOURCE, **values))
            res.inserted += 1
        else:
            for k, v in values.items():
                setattr(existing, k, v)
            res.updated += 1
    session.commit()
    return res


IMPORTERS = {
    "stores": import_stores,
    "inventory": import_inventory,
    "products": import_products,
    "discounts": import_discounts,
    "store_days": import_store_days,
}


def import_envelope(session: Session, payload: dict | Envelope) -> ImportResult:
    env = payload if isinstance(payload, Envelope) else Envelope.from_dict(payload)
    return IMPORTERS[env.kind](session, env)


def import_headset_directory(session: Session, directory: str | Path) -> list[ImportResult]:
    """Replay every *.json envelope under `directory` (recursively), dimensions
    first: stores, inventory (which knows the catalog), then sales facts."""
    directory = Path(directory)
    files = sorted(directory.rglob("*.json"))
    envelopes: list[tuple[Path, Envelope]] = []
    results: list[ImportResult] = []
    for path in files:
        try:
            envelopes.append((path, Envelope.load(path)))
        except ImportError_ as e:
            r = ImportResult(f"skip:{path.name}")
            r.errors.append(str(e))
            results.append(r)
    order = {k: i for i, k in enumerate(KINDS)}
    envelopes.sort(key=lambda pe: (order[pe[1].kind], str(pe[0])))
    for path, env in envelopes:
        r = IMPORTERS[env.kind](session, env)
        r.kind = f"{r.kind} <- {path.name}"
        results.append(r)
    return results


# ---------- reconciliation ----------

def reconcile(session: Session, store_code: str | None = None) -> list[dict]:
    """Compare the sum of product-grain lines against the feed's store-day totals.
    A non-zero difference means a product pull was partial (hasMore=true), a day
    was pulled before it closed, or Headset restated the day. Both sides come
    from the same feed, so the expected difference is 0.00.

    coverage: "ok" (ties to the cent), "mismatch", or "missing" (the feed
    reported the day but no product detail was pulled for it, so period revenue
    is understated while ticket counts are complete)."""
    from app.analytics.lines import load_lines
    from app.analytics.periods import Period

    stmt = select(DailyStoreSummary, Store.code).join(Store, DailyStoreSummary.store_id == Store.id)
    if store_code:
        stmt = stmt.where(store_predicate(store_code))
    out = []
    for summary, code in session.execute(stmt.order_by(Store.code, DailyStoreSummary.sale_date)).all():
        lines = [ln for ln in load_lines(session, Period("day", summary.sale_date, summary.sale_date), code) if ln.source == SOURCE]
        line_rev = money(sum((ln.revenue for ln in lines), ZERO))
        line_gp = money(sum((ln.gross_profit for ln in lines), ZERO))
        if not lines:
            coverage = "missing"
        elif line_rev == summary.revenue and line_gp == summary.gross_profit:
            coverage = "ok"
        else:
            coverage = "mismatch"
        out.append({
            "coverage": coverage,
            "store": code,
            "date": summary.sale_date.isoformat(),
            "feed_revenue": summary.revenue,
            "lines_revenue": line_rev,
            "revenue_diff": money(line_rev - summary.revenue),
            "feed_gross_profit": summary.gross_profit,
            "lines_gross_profit": line_gp,
            "gross_profit_diff": money(line_gp - summary.gross_profit),
            "feed_tickets": summary.transaction_count,
            "product_line_tickets": sum(ln.ticket_count for ln in lines),
            "skus": len(lines),
        })
    return out
