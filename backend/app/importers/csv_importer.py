"""CSV importers. Idempotent: re-importing the same file updates rows in place
keyed on natural keys (sku, transaction_id, expense_id, promotion name, ...).

Expected files and columns (header names are exact, order does not matter):

products.csv    sku, name, category, brand, vendor, unit_cost, retail_price
sales.csv       transaction_id, store, sold_at (ISO 8601), employee, customer, status
sale_items.csv  transaction_id, line_no, sku, quantity, regular_price, sale_price, unit_cost, promotion
inventory.csv   store, sku, snapshot_date, quantity_on_hand, unit_cost, received_date, last_sale_date
expenses.csv    expense_id, store, expense_date, category, vendor, description, amount
promotions.csv  name, start_date, end_date, discount_type, discount_value, eligible_skus, eligible_category

External Intelligence Engine:
stores.csv           code, name, latitude, longitude, timezone[, address, state]
external_events.csv  event_id, store, event_type, source, latitude, longitude, affected_radius_km,
                     start_time, end_time, severity, description, confidence, source_reference, is_forecast
weather.csv          store, observed_at, is_forecast, temperature_f, precipitation_in, snowfall_in,
                     wind_mph, condition, alert, source

Prices in sale_items are per unit. discount_amount is derived: (regular_price - sale_price) * quantity.
"""
import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.money import money
from app.models import (
    Brand,
    Category,
    Customer,
    Employee,
    Expense,
    ExternalEvent,
    InventorySnapshot,
    Product,
    Promotion,
    Sale,
    SaleItem,
    Store,
    Vendor,
    WeatherObservation,
)

SALE_STATUSES = {"completed", "refunded", "voided"}
DISCOUNT_TYPES = {"percent", "amount", "bogo"}
EVENT_TYPES = {"weather", "traffic", "connectivity", "utility", "local_event", "calendar", "competition", "economic", "demand"}
SEVERITIES = {"minor", "moderate", "major", "severe"}
WEATHER_CONDITIONS = {"clear", "cloudy", "rain", "storm", "snow", "fog"}


class ImportError_(ValueError):
    pass


@dataclass
class ImportResult:
    kind: str
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "inserted": self.inserted, "updated": self.updated, "skipped": self.skipped, "removed": self.removed, "errors": self.errors}


# ---------- parsing helpers ----------

def _dec(value: str, field_name: str, row_no: int) -> Decimal:
    try:
        return Decimal(str(value).strip().replace("$", "").replace(",", ""))
    except (InvalidOperation, AttributeError):
        raise ImportError_(f"row {row_no}: {field_name} is not a number: {value!r}")


def _int(value: str, field_name: str, row_no: int) -> int:
    try:
        return int(Decimal(str(value).strip()))
    except (InvalidOperation, ValueError):
        raise ImportError_(f"row {row_no}: {field_name} is not an integer: {value!r}")


def _date(value: str | None, field_name: str, row_no: int, required: bool = True) -> date | None:
    value = (value or "").strip()
    if not value:
        if required:
            raise ImportError_(f"row {row_no}: {field_name} is required")
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ImportError_(f"row {row_no}: {field_name} is not a date: {value!r}")


def _datetime(value: str, field_name: str, row_no: int) -> datetime:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ImportError_(f"row {row_no}: {field_name} is not a datetime: {value!r}")


def _id(obj) -> int | None:
    return obj.id if obj is not None else None


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _read_rows(source) -> list[dict]:
    if isinstance(source, (str, Path)):
        text = Path(source).read_text(encoding="utf-8-sig")
    elif isinstance(source, bytes):
        text = source.decode("utf-8-sig")
    else:
        text = source.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip().lower(): v for k, v in row.items()} for row in reader]


def _require(rows: list[dict], columns: tuple[str, ...], kind: str) -> None:
    if not rows:
        return
    missing = [c for c in columns if c not in rows[0]]
    if missing:
        raise ImportError_(f"{kind}: missing columns {missing}")


# ---------- lookup / upsert helpers ----------

class Lookups:
    """Get-or-create dimension rows, cached for the life of an import."""

    def __init__(self, session: Session):
        self.session = session
        self._cache: dict[tuple, object] = {}

    def _get_or_create(self, model, key_col: str, key: str, **defaults):
        ck = (model.__name__, key)
        if ck in self._cache:
            return self._cache[ck]
        obj = self.session.execute(select(model).where(getattr(model, key_col) == key)).scalar_one_or_none()
        if obj is None:
            obj = model(**{key_col: key}, **defaults)
            self.session.add(obj)
            self.session.flush()
        self._cache[ck] = obj
        return obj

    def store(self, code: str) -> Store:
        return self._get_or_create(Store, "code", code, name=code)

    def category(self, name: str) -> Category:
        return self._get_or_create(Category, "name", name)

    def brand(self, name: str | None) -> Brand | None:
        return self._get_or_create(Brand, "name", name) if name else None

    def vendor(self, name: str | None) -> Vendor | None:
        return self._get_or_create(Vendor, "name", name) if name else None

    def employee(self, code: str | None) -> Employee | None:
        return self._get_or_create(Employee, "code", code, name=code) if code else None

    def customer(self, code: str | None) -> Customer | None:
        return self._get_or_create(Customer, "code", code) if code else None

    def product(self, sku: str) -> Product | None:
        ck = ("Product", sku)
        if ck not in self._cache:
            self._cache[ck] = self.session.execute(select(Product).where(Product.sku == sku)).scalar_one_or_none()
        return self._cache[ck]

    def promotion(self, name: str | None) -> Promotion | None:
        if not name:
            return None
        ck = ("Promotion", name)
        if ck not in self._cache:
            self._cache[ck] = self.session.execute(select(Promotion).where(Promotion.name == name)).scalar_one_or_none()
        return self._cache[ck]


# ---------- importers ----------

def import_products(session: Session, source) -> ImportResult:
    res = ImportResult("products")
    rows = _read_rows(source)
    _require(rows, ("sku", "name", "category", "unit_cost", "retail_price"), res.kind)
    lk = Lookups(session)
    for i, r in enumerate(rows, start=2):
        try:
            sku = (r.get("sku") or "").strip()
            if not sku:
                raise ImportError_(f"row {i}: sku is required")
            product = lk.product(sku)
            values = dict(
                name=(r.get("name") or "").strip() or sku,
                category_id=lk.category((r.get("category") or "Uncategorized").strip()).id,
                brand_id=_id(lk.brand(_clean(r.get("brand")))),
                vendor_id=_id(lk.vendor(_clean(r.get("vendor")))),
                unit_cost=_dec(r["unit_cost"], "unit_cost", i),
                retail_price=money(_dec(r["retail_price"], "retail_price", i)),
            )
            if product is None:
                product = Product(sku=sku, **values)
                session.add(product)
                session.flush()
                lk._cache[("Product", sku)] = product
                res.inserted += 1
            else:
                for k, v in values.items():
                    setattr(product, k, v)
                res.updated += 1
        except ImportError_ as e:
            res.errors.append(str(e))
    session.commit()
    return res


def import_promotions(session: Session, source) -> ImportResult:
    res = ImportResult("promotions")
    rows = _read_rows(source)
    _require(rows, ("name", "start_date", "end_date", "discount_type", "discount_value"), res.kind)
    lk = Lookups(session)
    for i, r in enumerate(rows, start=2):
        try:
            name = (r.get("name") or "").strip()
            if not name:
                raise ImportError_(f"row {i}: name is required")
            dtype = (r.get("discount_type") or "").strip().lower()
            if dtype not in DISCOUNT_TYPES:
                raise ImportError_(f"row {i}: discount_type must be one of {sorted(DISCOUNT_TYPES)}")
            start, end = _date(r["start_date"], "start_date", i), _date(r["end_date"], "end_date", i)
            if end < start:
                raise ImportError_(f"row {i}: end_date precedes start_date")
            values = dict(
                start_date=start,
                end_date=end,
                discount_type=dtype,
                discount_value=_dec(r["discount_value"], "discount_value", i),
                eligible_skus=_clean(r.get("eligible_skus")),
                eligible_category=_clean(r.get("eligible_category")),
            )
            promo = lk.promotion(name)
            if promo is None:
                promo = Promotion(name=name, **values)
                session.add(promo)
                session.flush()
                lk._cache[("Promotion", name)] = promo
                res.inserted += 1
            else:
                for k, v in values.items():
                    setattr(promo, k, v)
                res.updated += 1
        except ImportError_ as e:
            res.errors.append(str(e))
    session.commit()
    return res


def import_sales(session: Session, source) -> ImportResult:
    res = ImportResult("sales")
    rows = _read_rows(source)
    _require(rows, ("transaction_id", "store", "sold_at"), res.kind)
    lk = Lookups(session)
    existing = {s.transaction_id: s for s in session.execute(select(Sale)).scalars()}
    for i, r in enumerate(rows, start=2):
        try:
            tid = (r.get("transaction_id") or "").strip()
            if not tid:
                raise ImportError_(f"row {i}: transaction_id is required")
            status = (r.get("status") or "completed").strip().lower() or "completed"
            if status not in SALE_STATUSES:
                raise ImportError_(f"row {i}: status must be one of {sorted(SALE_STATUSES)}")
            values = dict(
                store_id=lk.store((r.get("store") or "MAIN").strip()).id,
                sold_at=_datetime(r["sold_at"], "sold_at", i),
                employee_id=_id(lk.employee(_clean(r.get("employee")))),
                customer_id=_id(lk.customer(_clean(r.get("customer")))),
                status=status,
            )
            sale = existing.get(tid)
            if sale is None:
                sale = Sale(transaction_id=tid, **values)
                session.add(sale)
                existing[tid] = sale
                res.inserted += 1
            else:
                for k, v in values.items():
                    setattr(sale, k, v)
                res.updated += 1
        except ImportError_ as e:
            res.errors.append(str(e))
    session.commit()
    return res


def import_sale_items(session: Session, source) -> ImportResult:
    res = ImportResult("sale_items")
    rows = _read_rows(source)
    _require(rows, ("transaction_id", "sku", "quantity", "regular_price", "sale_price"), res.kind)
    lk = Lookups(session)
    sales = {s.transaction_id: s.id for s in session.execute(select(Sale)).scalars()}
    existing = {(it.sale_id, it.line_no): it for it in session.execute(select(SaleItem)).scalars()}
    auto_line: dict[int, int] = {}
    for i, r in enumerate(rows, start=2):
        try:
            tid = (r.get("transaction_id") or "").strip()
            sale_id = sales.get(tid)
            if sale_id is None:
                raise ImportError_(f"row {i}: unknown transaction_id {tid!r} (import sales.csv first)")
            sku = (r.get("sku") or "").strip()
            product = lk.product(sku)
            if product is None:
                raise ImportError_(f"row {i}: unknown sku {sku!r} (import products.csv first)")
            if _clean(r.get("line_no")):
                line_no = _int(r["line_no"], "line_no", i)
            else:
                auto_line[sale_id] = auto_line.get(sale_id, 0) + 1
                line_no = auto_line[sale_id]
            qty = _int(r["quantity"], "quantity", i)
            if qty <= 0:
                raise ImportError_(f"row {i}: quantity must be positive")
            regular = money(_dec(r["regular_price"], "regular_price", i))
            sale_price = money(_dec(r["sale_price"], "sale_price", i))
            if sale_price > regular:
                raise ImportError_(f"row {i}: sale_price {sale_price} exceeds regular_price {regular}")
            unit_cost = _dec(r["unit_cost"], "unit_cost", i) if _clean(r.get("unit_cost")) else product.unit_cost
            promo_name = _clean(r.get("promotion"))
            promo = lk.promotion(promo_name)
            if promo_name and promo is None:
                raise ImportError_(f"row {i}: unknown promotion {promo_name!r} (import promotions.csv first)")
            values = dict(
                product_id=product.id,
                quantity=qty,
                regular_price=regular,
                sale_price=sale_price,
                discount_amount=money((regular - sale_price) * qty),
                unit_cost=unit_cost,
                promotion_id=promo.id if promo else None,
            )
            item = existing.get((sale_id, line_no))
            if item is None:
                item = SaleItem(sale_id=sale_id, line_no=line_no, **values)
                session.add(item)
                existing[(sale_id, line_no)] = item
                res.inserted += 1
            else:
                for k, v in values.items():
                    setattr(item, k, v)
                res.updated += 1
        except ImportError_ as e:
            res.errors.append(str(e))
    session.commit()
    return res


def import_inventory(session: Session, source) -> ImportResult:
    res = ImportResult("inventory")
    rows = _read_rows(source)
    _require(rows, ("store", "sku", "snapshot_date", "quantity_on_hand"), res.kind)
    lk = Lookups(session)
    existing = {
        (s.store_id, s.product_id, s.snapshot_date): s for s in session.execute(select(InventorySnapshot)).scalars()
    }
    for i, r in enumerate(rows, start=2):
        try:
            store = lk.store((r.get("store") or "MAIN").strip())
            sku = (r.get("sku") or "").strip()
            product = lk.product(sku)
            if product is None:
                raise ImportError_(f"row {i}: unknown sku {sku!r} (import products.csv first)")
            snap_date = _date(r["snapshot_date"], "snapshot_date", i)
            qoh = _int(r["quantity_on_hand"], "quantity_on_hand", i)
            if qoh < 0:
                raise ImportError_(f"row {i}: quantity_on_hand cannot be negative")
            values = dict(
                quantity_on_hand=qoh,
                unit_cost=_dec(r["unit_cost"], "unit_cost", i) if _clean(r.get("unit_cost")) else product.unit_cost,
                received_date=_date(r.get("received_date"), "received_date", i, required=False),
                last_sale_date=_date(r.get("last_sale_date"), "last_sale_date", i, required=False),
            )
            key = (store.id, product.id, snap_date)
            snap = existing.get(key)
            if snap is None:
                snap = InventorySnapshot(store_id=store.id, product_id=product.id, snapshot_date=snap_date, **values)
                session.add(snap)
                existing[key] = snap
                res.inserted += 1
            else:
                for k, v in values.items():
                    setattr(snap, k, v)
                res.updated += 1
        except ImportError_ as e:
            res.errors.append(str(e))
    session.commit()
    return res


def import_expenses(session: Session, source) -> ImportResult:
    res = ImportResult("expenses")
    rows = _read_rows(source)
    _require(rows, ("expense_id", "store", "expense_date", "category", "amount"), res.kind)
    lk = Lookups(session)
    existing = {e.expense_id: e for e in session.execute(select(Expense)).scalars()}
    for i, r in enumerate(rows, start=2):
        try:
            eid = (r.get("expense_id") or "").strip()
            if not eid:
                raise ImportError_(f"row {i}: expense_id is required")
            values = dict(
                store_id=lk.store((r.get("store") or "MAIN").strip()).id,
                expense_date=_date(r["expense_date"], "expense_date", i),
                category=(r.get("category") or "Other").strip(),
                vendor=_clean(r.get("vendor")),
                description=_clean(r.get("description")),
                amount=money(_dec(r["amount"], "amount", i)),
            )
            exp = existing.get(eid)
            if exp is None:
                exp = Expense(expense_id=eid, **values)
                session.add(exp)
                existing[eid] = exp
                res.inserted += 1
            else:
                for k, v in values.items():
                    setattr(exp, k, v)
                res.updated += 1
        except ImportError_ as e:
            res.errors.append(str(e))
    session.commit()
    return res


def _flag(value) -> int:
    return 1 if (value or "").strip().lower() in ("1", "true", "yes", "y") else 0


def _float_or_none(r: dict, key: str, row_no: int) -> float | None:
    return float(_dec(r[key], key, row_no)) if _clean(r.get(key)) else None


def import_stores(session: Session, source) -> ImportResult:
    res = ImportResult("stores")
    rows = _read_rows(source)
    _require(rows, ("code",), res.kind)
    existing = {s.code: s for s in session.execute(select(Store)).scalars()}
    for i, r in enumerate(rows, start=2):
        try:
            code = (r.get("code") or "").strip()
            if not code:
                raise ImportError_(f"row {i}: code is required")
            values = dict(
                name=(r.get("name") or "").strip() or code,
                latitude=_float_or_none(r, "latitude", i),
                longitude=_float_or_none(r, "longitude", i),
                timezone=_clean(r.get("timezone")),
            )
            if "address" in r:
                values["address"] = _clean(r.get("address"))
            if "state" in r:
                values["state"] = (_clean(r.get("state")) or "").upper() or None
            store = existing.get(code)
            if store is None:
                store = Store(code=code, **values)
                session.add(store)
                existing[code] = store
                res.inserted += 1
            else:
                for k, v in values.items():
                    setattr(store, k, v)
                res.updated += 1
        except ImportError_ as e:
            res.errors.append(str(e))
    session.commit()
    return res


def import_external_events(session: Session, source) -> ImportResult:
    res = ImportResult("external_events")
    rows = _read_rows(source)
    _require(rows, ("event_id", "event_type", "source", "start_time", "end_time"), res.kind)
    lk = Lookups(session)
    existing = {e.event_id: e for e in session.execute(select(ExternalEvent)).scalars()}
    for i, r in enumerate(rows, start=2):
        try:
            eid = (r.get("event_id") or "").strip()
            if not eid:
                raise ImportError_(f"row {i}: event_id is required")
            etype = (r.get("event_type") or "").strip().lower()
            if etype not in EVENT_TYPES:
                raise ImportError_(f"row {i}: event_type must be one of {sorted(EVENT_TYPES)}")
            severity = (r.get("severity") or "moderate").strip().lower() or "moderate"
            if severity not in SEVERITIES:
                raise ImportError_(f"row {i}: severity must be one of {sorted(SEVERITIES)}")
            start, end = _datetime(r["start_time"], "start_time", i), _datetime(r["end_time"], "end_time", i)
            if end < start:
                raise ImportError_(f"row {i}: end_time precedes start_time")
            store_code = _clean(r.get("store"))
            confidence = _dec(r["confidence"], "confidence", i) if _clean(r.get("confidence")) else Decimal("1")
            if not (0 <= confidence <= 1):
                raise ImportError_(f"row {i}: confidence must be between 0 and 1")
            values = dict(
                store_id=lk.store(store_code).id if store_code else None,
                event_type=etype,
                source=(r.get("source") or "manual").strip(),
                latitude=_float_or_none(r, "latitude", i),
                longitude=_float_or_none(r, "longitude", i),
                affected_radius_km=_float_or_none(r, "affected_radius_km", i),
                start_time=start,
                end_time=end,
                severity=severity,
                description=_clean(r.get("description")),
                confidence=confidence,
                source_reference=_clean(r.get("source_reference")),
                is_forecast=_flag(r.get("is_forecast")),
                raw_source_metadata=_clean(r.get("raw_source_metadata")),
            )
            if values["store_id"] is None and (values["latitude"] is None or values["longitude"] is None):
                raise ImportError_(f"row {i}: an event needs either a store or latitude/longitude")
            ev = existing.get(eid)
            if ev is None:
                ev = ExternalEvent(event_id=eid, **values)
                session.add(ev)
                existing[eid] = ev
                res.inserted += 1
            else:
                for k, v in values.items():
                    setattr(ev, k, v)
                res.updated += 1
        except ImportError_ as e:
            res.errors.append(str(e))
    session.commit()
    return res


def import_weather(session: Session, source) -> ImportResult:
    res = ImportResult("weather")
    rows = _read_rows(source)
    _require(rows, ("store", "observed_at"), res.kind)
    lk = Lookups(session)
    existing = {
        (w.store_id, w.observed_at, w.is_forecast): w for w in session.execute(select(WeatherObservation)).scalars()
    }
    for i, r in enumerate(rows, start=2):
        try:
            store = lk.store((r.get("store") or "MAIN").strip())
            observed_at = _datetime(r["observed_at"], "observed_at", i)
            is_forecast = _flag(r.get("is_forecast"))
            condition = (_clean(r.get("condition")) or "").lower() or None
            if condition and condition not in WEATHER_CONDITIONS:
                raise ImportError_(f"row {i}: condition must be one of {sorted(WEATHER_CONDITIONS)}")
            values = dict(
                temperature_f=_dec(r["temperature_f"], "temperature_f", i) if _clean(r.get("temperature_f")) else None,
                precipitation_in=_dec(r["precipitation_in"], "precipitation_in", i) if _clean(r.get("precipitation_in")) else Decimal("0"),
                snowfall_in=_dec(r["snowfall_in"], "snowfall_in", i) if _clean(r.get("snowfall_in")) else Decimal("0"),
                wind_mph=_dec(r["wind_mph"], "wind_mph", i) if _clean(r.get("wind_mph")) else None,
                condition=condition,
                alert=_clean(r.get("alert")),
                source=_clean(r.get("source")),
            )
            key = (store.id, observed_at, is_forecast)
            obs = existing.get(key)
            if obs is None:
                obs = WeatherObservation(store_id=store.id, observed_at=observed_at, is_forecast=is_forecast, **values)
                session.add(obs)
                existing[key] = obs
                res.inserted += 1
            else:
                for k, v in values.items():
                    setattr(obs, k, v)
                res.updated += 1
        except ImportError_ as e:
            res.errors.append(str(e))
    session.commit()
    return res


IMPORTERS = {
    "stores": import_stores,
    "products": import_products,
    "promotions": import_promotions,
    "sales": import_sales,
    "sale_items": import_sale_items,
    "inventory": import_inventory,
    "expenses": import_expenses,
    "external_events": import_external_events,
    "weather": import_weather,
}
# Dependency order: dimensions before facts.
IMPORT_ORDER = ("stores", "products", "promotions", "sales", "sale_items", "inventory", "expenses", "external_events", "weather")


def import_directory(session: Session, directory: str | Path) -> list[ImportResult]:
    directory = Path(directory)
    results = []
    for kind in IMPORT_ORDER:
        path = directory / f"{kind}.csv"
        if path.exists():
            results.append(IMPORTERS[kind](session, path))
    return results
