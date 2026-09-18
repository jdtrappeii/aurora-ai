"""Financial metrics for a period.

Definitions (all from completed sales unless stated):
  gross_sales            = sum(regular_price * qty)
  discount_total         = sum(discount_amount)             (line totals)
  revenue                = sum(sale_price * qty)            (net of discounts, pre-tax)
  cogs                   = sum(unit_cost * qty)
  gross_profit           = revenue - cogs
  gross_margin           = gross_profit / revenue
  discount_rate          = discount_total / gross_sales
  transactions           = distinct completed tickets (POS) + feed-reported tickets
                           (aggregate rows; a period total takes the store-day figure
                           from daily_store_summaries when the feed supplied one)
  units                  = sum(qty)
  avg_transaction_value  = revenue / transactions
  units_per_transaction  = units / transactions
  refunds                = count/amount of tickets with status 'refunded'
  voids                  = count of tickets with status 'voided'
  operating_expenses     = sum(expenses.amount) in period
  operating_profit       = gross_profit - operating_expenses
"""
from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.lines import LineRow, completed, count_tickets, load_lines
from app.analytics.money import ZERO, D, money, rate, safe_div
from app.analytics.periods import Period
from app.analytics.scope import store_predicate
from app.models import DailyStoreSummary, Expense, Store


@dataclass
class FinancialSummary:
    period: dict
    gross_sales: Decimal
    discount_total: Decimal
    discount_rate: Decimal
    revenue: Decimal
    cogs: Decimal
    gross_profit: Decimal
    gross_margin: Decimal
    transactions: int
    units: int
    avg_transaction_value: Decimal
    units_per_transaction: Decimal
    refund_count: int
    refund_amount: Decimal
    void_count: int
    operating_expenses: Decimal
    operating_profit: Decimal

    def to_dict(self) -> dict:
        return asdict(self)


def summarize_lines(lines: list[LineRow], feed_tickets: int | None = None) -> dict:
    """Core aggregation used by every breakdown (product, category, promo...).

    feed_tickets, when given, replaces the ticket count of the aggregate-feed
    lines with the feed's own store-day total (see count_tickets)."""
    done = completed(lines)
    gross_sales = sum((ln.gross_sales for ln in done), ZERO)
    discount_total = sum((ln.discount_amount for ln in done), ZERO)
    revenue = sum((ln.revenue for ln in done), ZERO)
    cogs = sum((ln.cogs for ln in done), ZERO)
    gross_profit = revenue - cogs
    if feed_tickets is None:
        transactions = count_tickets(done)
    else:
        transactions = count_tickets([ln for ln in done if ln.source == "pos"]) + feed_tickets
    units = sum(ln.quantity for ln in done)
    return {
        "gross_sales": money(gross_sales),
        "discount_total": money(discount_total),
        "discount_rate": rate(safe_div(discount_total, gross_sales)),
        "revenue": money(revenue),
        "cogs": money(cogs),
        "gross_profit": money(gross_profit),
        "gross_margin": rate(safe_div(gross_profit, revenue)),
        "transactions": transactions,
        "units": units,
        "avg_transaction_value": money(safe_div(revenue, transactions)),
        "units_per_transaction": rate(safe_div(units, transactions)),
    }


def operating_expenses(session: Session, period: Period, store_code: str | None = None) -> Decimal:
    stmt = select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.expense_date >= period.start, Expense.expense_date <= period.end
    )
    if store_code:
        stmt = stmt.join(Store, Expense.store_id == Store.id).where(store_predicate(store_code))
    return money(D(session.execute(stmt).scalar_one()))


def feed_tickets(session: Session, period: Period, store_code: str | None = None) -> int | None:
    """Sum of feed-reported tickets for the period, or None when the feed has no
    store-day rows in it (then the line-level count stands)."""
    stmt = select(func.count(DailyStoreSummary.id), func.coalesce(func.sum(DailyStoreSummary.transaction_count), 0)).where(
        DailyStoreSummary.sale_date >= period.start, DailyStoreSummary.sale_date <= period.end
    )
    if store_code:
        stmt = stmt.join(Store, DailyStoreSummary.store_id == Store.id).where(store_predicate(store_code))
    rows, tickets = session.execute(stmt).one()
    return int(tickets) if rows else None


def _build(period_dict: dict, lines: list[LineRow], opex: Decimal, tickets: int | None = None) -> FinancialSummary:
    core = summarize_lines(lines, tickets)
    refunded = [ln for ln in lines if ln.status == "refunded"]
    void_ids = {ln.sale_id for ln in lines if ln.status == "voided"}
    return FinancialSummary(
        period=period_dict,
        **core,
        refund_count=len({ln.sale_id for ln in refunded}),
        refund_amount=money(sum((ln.revenue for ln in refunded), ZERO)),
        void_count=len(void_ids),
        operating_expenses=opex,
        operating_profit=money(core["gross_profit"] - opex),
    )


def financial_summary(session: Session, period: Period, store_code: str | None = None) -> FinancialSummary:
    return _build(
        period.to_dict(),
        load_lines(session, period, store_code),
        operating_expenses(session, period, store_code),
        feed_tickets(session, period, store_code),
    )


def financial_summary_multi(session: Session, periods: list[Period], store_code: str | None = None) -> FinancialSummary:
    """One summary over several disjoint periods (e.g. the same weekdays of four
    different weeks). Totals add; ratios are derived from the combined totals."""
    lines: list[LineRow] = []
    opex = ZERO
    tickets: int | None = None
    for p in periods:
        lines.extend(load_lines(session, p, store_code))
        opex += operating_expenses(session, p, store_code)
        t = feed_tickets(session, p, store_code)
        if t is not None:
            tickets = (tickets or 0) + t
    period_dict = {
        "label": "multi",
        "start": min(p.start for p in periods).isoformat(),
        "end": max(p.end for p in periods).isoformat(),
        "days": sum(p.days for p in periods),
        "periods": [p.to_dict() for p in periods],
    }
    return _build(period_dict, lines, money(opex), tickets)
