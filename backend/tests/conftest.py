"""Shared fixtures: an in-memory SQLite database and helpers that build small,
hand-checkable datasets. Every expected number in the tests is worked out by
hand in the test body so the analytics are verified, not just exercised."""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    Category,
    Employee,
    Expense,
    ExternalEvent,
    InventorySnapshot,
    Product,
    Promotion,
    Sale,
    SaleItem,
    Store,
    WeatherObservation,
)


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Session:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        yield s


class Seed:
    """Tiny builder so tests read like a ledger."""

    def __init__(self, session: Session):
        self.s = session
        self.store = Store(code="MAIN", name="Main St", latitude=28.5383, longitude=-81.3792)
        self.s.add(self.store)
        self.cats: dict[str, Category] = {}
        self.products: dict[str, Product] = {}
        self.employee = Employee(code="E1", name="Alex")
        self.s.add(self.employee)
        self.s.flush()
        self._txn = 0

    def product(self, sku: str, category: str, cost: str, price: str) -> Product:
        cat = self.cats.get(category)
        if cat is None:
            cat = Category(name=category)
            self.s.add(cat)
            self.s.flush()
            self.cats[category] = cat
        p = Product(sku=sku, name=sku, category_id=cat.id, unit_cost=Decimal(cost), retail_price=Decimal(price))
        self.s.add(p)
        self.s.flush()
        self.products[sku] = p
        return p

    def sale(self, when: datetime, lines: list[tuple], status: str = "completed", promo: Promotion | None = None) -> Sale:
        """lines: (sku, qty, sale_price[, regular_price])"""
        self._txn += 1
        sale = Sale(transaction_id=f"T{self._txn:05d}", store_id=self.store.id, sold_at=when, status=status,
                    employee_id=self.employee.id)
        self.s.add(sale)
        self.s.flush()
        for i, line in enumerate(lines, start=1):
            sku, qty, sale_price = line[0], line[1], Decimal(line[2])
            product = self.products[sku]
            regular = Decimal(line[3]) if len(line) > 3 else product.retail_price
            self.s.add(SaleItem(
                sale_id=sale.id, line_no=i, product_id=product.id, quantity=qty,
                regular_price=regular, sale_price=sale_price, discount_amount=(regular - sale_price) * qty,
                unit_cost=product.unit_cost, promotion_id=promo.id if promo else None,
            ))
        self.s.flush()
        return sale

    def expense(self, when: date, amount: str, category: str = "Rent") -> Expense:
        e = Expense(expense_id=f"X{when.isoformat()}-{category}-{amount}", store_id=self.store.id, expense_date=when,
                    category=category, amount=Decimal(amount))
        self.s.add(e)
        self.s.flush()
        return e

    def inventory(self, sku: str, on: date, qoh: int, received: date | None, cost: str | None = None) -> InventorySnapshot:
        p = self.products[sku]
        snap = InventorySnapshot(store_id=self.store.id, product_id=p.id, snapshot_date=on, quantity_on_hand=qoh,
                                 unit_cost=Decimal(cost) if cost else p.unit_cost, received_date=received)
        self.s.add(snap)
        self.s.flush()
        return snap

    def promotion(self, name: str, start: date, end: date, category: str | None = None, skus: str | None = None,
                  dtype: str = "percent", value: str = "20") -> Promotion:
        pr = Promotion(name=name, start_date=start, end_date=end, discount_type=dtype, discount_value=Decimal(value),
                       eligible_category=category, eligible_skus=skus)
        self.s.add(pr)
        self.s.flush()
        return pr

    def event(self, event_id: str, etype: str, start: datetime, end: datetime, severity: str = "major",
              store: bool = True, lat: float | None = None, lon: float | None = None, radius: float | None = None,
              is_forecast: int = 0, description: str | None = None) -> ExternalEvent:
        ev = ExternalEvent(event_id=event_id, store_id=self.store.id if store else None, event_type=etype, source="test",
                           latitude=lat, longitude=lon, affected_radius_km=radius, start_time=start, end_time=end,
                           severity=severity, confidence=Decimal("1"), is_forecast=is_forecast, description=description)
        self.s.add(ev)
        self.s.flush()
        return ev

    def weather(self, at: datetime, temp: str = "80", precip: str = "0", snow: str = "0", wind: str = "5",
                condition: str = "clear", alert: str | None = None, is_forecast: int = 0) -> WeatherObservation:
        w = WeatherObservation(store_id=self.store.id, observed_at=at, is_forecast=is_forecast,
                               temperature_f=Decimal(temp), precipitation_in=Decimal(precip), snowfall_in=Decimal(snow),
                               wind_mph=Decimal(wind), condition=condition, alert=alert)
        self.s.add(w)
        self.s.flush()
        return w

    def commit(self):
        self.s.commit()


@pytest.fixture
def seed(session) -> Seed:
    return Seed(session)


# A fixed Monday so week math is predictable: 2026-09-07 is a Monday.
MONDAY = date(2026, 9, 7)


def dt(d: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute)


def days_ago(n: int, base: date = MONDAY) -> date:
    return base - timedelta(days=n)
