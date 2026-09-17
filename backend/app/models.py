"""Normalized historical business data.

Money columns are NUMERIC. Unit costs carry 4 decimals because vendor cost
sheets frequently do; everything customer-facing is 2 decimals.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

MONEY = Numeric(12, 2)
COST = Numeric(12, 4)


class Store(Base):
    __tablename__ = "stores"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)


class Brand(Base):
    __tablename__ = "brands"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"), nullable=True)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"), nullable=True)
    unit_cost: Mapped[Decimal] = mapped_column(COST)
    retail_price: Mapped[Decimal] = mapped_column(MONEY)

    category: Mapped[Category] = relationship()
    brand: Mapped[Brand | None] = relationship()
    vendor: Mapped[Vendor | None] = relationship()


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)


class Promotion(Base):
    __tablename__ = "promotions"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    discount_type: Mapped[str] = mapped_column(String(16))  # percent | amount | bogo
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    eligible_skus: Mapped[str | None] = mapped_column(Text, nullable=True)  # pipe-separated
    eligible_category: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Sale(Base):
    """One POS transaction (ticket)."""

    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[str] = mapped_column(String(64), unique=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    sold_at: Mapped[datetime] = mapped_column(DateTime)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="completed")  # completed | refunded | voided
    # "pos" = one real ticket (CSV / POS export). "headset" = an aggregate row from the
    # Headset feed (store x day x product) standing in for many tickets.
    source: Mapped[str] = mapped_column(String(16), default="pos")

    store: Mapped[Store] = relationship()
    employee: Mapped[Employee | None] = relationship()
    items: Mapped[list["SaleItem"]] = relationship(back_populates="sale", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_sales_store_sold_at", "store_id", "sold_at"),)


class SaleItem(Base):
    """The most important table. One line per item sold.

    regular_price / sale_price / unit_cost are PER UNIT.
    discount_amount is the LINE total: (regular_price - sale_price) * quantity.

    Aggregate feeds (Headset) report line TOTALS, not unit prices. For those rows
    gross_total / revenue_total / cogs_total carry the exact totals and the per-unit
    columns are informational (total / quantity, rounded). ticket_count is how many
    tickets the line represents: 1 for a real POS line, N for an aggregate row.
    """

    __tablename__ = "sale_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    regular_price: Mapped[Decimal] = mapped_column(MONEY)
    sale_price: Mapped[Decimal] = mapped_column(MONEY)
    discount_amount: Mapped[Decimal] = mapped_column(MONEY)
    unit_cost: Mapped[Decimal] = mapped_column(COST)
    promotion_id: Mapped[int | None] = mapped_column(ForeignKey("promotions.id"), nullable=True)
    gross_total: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    revenue_total: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    cogs_total: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    ticket_count: Mapped[int] = mapped_column(Integer, default=1)

    sale: Mapped[Sale] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()
    promotion: Mapped[Promotion | None] = relationship()

    __table_args__ = (UniqueConstraint("sale_id", "line_no", name="uq_sale_items_sale_line"),)


class InventorySnapshot(Base):
    """Stock position for a store/SKU on a given date."""

    __tablename__ = "inventory_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    snapshot_date: Mapped[date] = mapped_column(Date)
    quantity_on_hand: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal] = mapped_column(COST)
    received_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_sale_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    product: Mapped[Product] = relationship()

    __table_args__ = (
        UniqueConstraint("store_id", "product_id", "snapshot_date", name="uq_inventory_store_product_date"),
    )


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    expense_id: Mapped[str] = mapped_column(String(64), unique=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    expense_date: Mapped[date] = mapped_column(Date)
    category: Mapped[str] = mapped_column(String(64))
    vendor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)

    __table_args__ = (Index("ix_expenses_store_date", "store_id", "expense_date"),)


# ---------------------------------------------------------------------------
# Aggregate feeds (Headset)
# ---------------------------------------------------------------------------

class DailyStoreSummary(Base):
    """One row per store per day as reported by an aggregate feed. It is the
    feed's own total for the day, so it is the reconciliation target for the
    product-grain rows and the only exact source of the day's ticket count
    (product rows over-count tickets: a receipt with two products appears twice)."""

    __tablename__ = "daily_store_summaries"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    sale_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(16), default="headset")
    transaction_count: Mapped[int] = mapped_column(Integer, default=0)
    units: Mapped[int] = mapped_column(Integer, default=0)
    gross_sales: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    discount_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    revenue: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    cogs: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    gross_profit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))

    store: Mapped[Store] = relationship()

    __table_args__ = (UniqueConstraint("store_id", "sale_date", "source", name="uq_daily_store_summary"),)


class DiscountDaily(Base):
    """Discount / promo code performance per store per day (Headset's
    discount_name dimension). discount_total is attributable to this code only,
    revenue/units are of the items that carried it, transaction_count is the
    receipts that used it. A NULL code is undiscounted items (stored as '')."""

    __tablename__ = "discount_daily"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    sale_date: Mapped[date] = mapped_column(Date)
    discount_name: Mapped[str] = mapped_column(String(255), default="")
    revenue: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    units: Mapped[int] = mapped_column(Integer, default=0)
    discount_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    transaction_count: Mapped[int] = mapped_column(Integer, default=0)

    store: Mapped[Store] = relationship()

    __table_args__ = (
        UniqueConstraint("store_id", "sale_date", "discount_name", name="uq_discount_daily"),
        Index("ix_discount_daily_store_date", "store_id", "sale_date"),
    )


# ---------------------------------------------------------------------------
# External Intelligence Engine
# ---------------------------------------------------------------------------

class ExternalEvent(Base):
    """A normalized outside condition: weather alert, traffic incident, outage,
    local event, holiday, competitor move, economic signal. Any provider feeds
    the same shape."""

    __tablename__ = "external_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id"), nullable=True)  # explicit store, or None => match by radius
    event_type: Mapped[str] = mapped_column(String(32))  # weather|traffic|connectivity|utility|local_event|calendar|competition|economic|demand
    source: Mapped[str] = mapped_column(String(64))
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    affected_radius_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    severity: Mapped[str] = mapped_column(String(16), default="moderate")  # minor|moderate|major|severe
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("1.0"))  # confidence the event itself occurred as described
    source_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_forecast: Mapped[int] = mapped_column(Integer, default=0)  # 1 = scheduled/predicted, not yet observed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    raw_source_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)

    store: Mapped[Store | None] = relationship()

    __table_args__ = (Index("ix_external_events_time", "start_time", "end_time"),)


class WeatherObservation(Base):
    """Hourly observed (or forecast) weather at a store."""

    __tablename__ = "weather_observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime)
    is_forecast: Mapped[int] = mapped_column(Integer, default=0)
    temperature_f: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    precipitation_in: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("0"))
    snowfall_in: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("0"))
    wind_mph: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    condition: Mapped[str | None] = mapped_column(String(32), nullable=True)  # clear|cloudy|rain|storm|snow|fog
    alert: Mapped[str | None] = mapped_column(String(128), nullable=True)  # e.g. "Tropical Storm Warning"
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("store_id", "observed_at", "is_forecast", name="uq_weather_store_time_kind"),
        Index("ix_weather_store_time", "store_id", "observed_at"),
    )
