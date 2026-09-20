"""Load sale lines for a period into plain rows. Every metric module works
from these rows so definitions stay consistent."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.periods import Period
from app.analytics.scope import store_predicate
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
    source: str = "pos"
    gross_total: Decimal | None = None
    revenue_total: Decimal | None = None
    cogs_total: Decimal | None = None
    ticket_count: int = 1

    @property
    def revenue(self) -> Decimal:
        return self.revenue_total if self.revenue_total is not None else self.sale_price * self.quantity

    @property
    def gross_sales(self) -> Decimal:
        return self.gross_total if self.gross_total is not None else self.regular_price * self.quantity

    @property
    def cogs(self) -> Decimal:
        return self.cogs_total if self.cogs_total is not None else self.unit_cost * self.quantity

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
            Sale.source,
            SaleItem.gross_total,
            SaleItem.revenue_total,
            SaleItem.cogs_total,
            SaleItem.ticket_count,
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
        stmt = stmt.where(store_predicate(store_code))
    if statuses:
        stmt = stmt.where(Sale.status.in_(statuses))
    return [LineRow(*row) for row in session.execute(stmt).all()]


def completed(lines: list[LineRow]) -> list[LineRow]:
    return [ln for ln in lines if ln.status == "completed"]


def count_tickets(lines: list[LineRow]) -> int:
    """Distinct tickets across a set of lines.

    A POS ticket counts once however many lines it has. An aggregate line
    (Headset store x day x product) stands for ticket_count receipts; each such
    sale has exactly one line, so the counts add. At product grain that is
    exact; summed across products it over-counts receipts that held several
    products, which is why period totals use DailyStoreSummary instead."""
    pos_ids = set()
    aggregate = 0
    for ln in lines:
        if ln.source == "pos":
            pos_ids.add(ln.sale_id)
        else:
            aggregate += ln.ticket_count
    return len(pos_ids) + aggregate
