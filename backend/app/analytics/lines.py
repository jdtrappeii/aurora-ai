"""Load sale lines for a period into plain rows. Every metric module works
from these rows so definitions stay consistent."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.periods import Period
from app.models import Brand, Category, Employee, Product, Promotion, Sale, SaleItem, Store, Vendor


@dataclass(frozen=True)
class LineRow:
    sale_id: int
    transaction_id: str
    sold_at: datetime
    status: str
    store_code: str
    employee_code: str | None
    customer_id: int | None
    product_id: int
    sku: str
    product_name: str
    category: str
    brand: str | None
    vendor: str | None
    quantity: int
    regular_price: Decimal
    sale_price: Decimal
    discount_amount: Decimal
    unit_cost: Decimal
    promotion_id: int | None
    promotion_name: str | None

    @property
    def revenue(self) -> Decimal:
        return self.sale_price * self.quantity

    @property
    def gross_sales(self) -> Decimal:
        return self.regular_price * self.quantity

    @property
    def cogs(self) -> Decimal:
        return self.unit_cost * self.quantity

    @property
    def gross_profit(self) -> Decimal:
        return self.revenue - self.cogs


def load_lines(
    session: Session,
    period: Period,
    store_code: str | None = None,
    statuses: tuple[str, ...] | None = None,
) -> list[LineRow]:
    stmt = (
        select(
            Sale.id,
            Sale.transaction_id,
            Sale.sold_at,
            Sale.status,
            Store.code,
            Employee.code,
            Sale.customer_id,
            Product.id,
            Product.sku,
            Product.name,
            Category.name,
            Brand.name,
            Vendor.name,
            SaleItem.quantity,
            SaleItem.regular_price,
            SaleItem.sale_price,
            SaleItem.discount_amount,
            SaleItem.unit_cost,
            SaleItem.promotion_id,
            Promotion.name,
        )
        .join(Sale, SaleItem.sale_id == Sale.id)
        .join(Store, Sale.store_id == Store.id)
        .join(Product, SaleItem.product_id == Product.id)
        .join(Category, Product.category_id == Category.id)
        .outerjoin(Brand, Product.brand_id == Brand.id)
        .outerjoin(Vendor, Product.vendor_id == Vendor.id)
        .outerjoin(Employee, Sale.employee_id == Employee.id)
        .outerjoin(Promotion, SaleItem.promotion_id == Promotion.id)
        .where(Sale.sold_at >= period.start_dt, Sale.sold_at < period.end_dt_exclusive)
        .order_by(Sale.sold_at, Sale.id, SaleItem.line_no)
    )
    if store_code:
        stmt = stmt.where(Store.code == store_code)
    if statuses:
        stmt = stmt.where(Sale.status.in_(statuses))
    return [LineRow(*row) for row in session.execute(stmt).all()]


def completed(lines: list[LineRow]) -> list[LineRow]:
    return [ln for ln in lines if ln.status == "completed"]
