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


def summarize_lines(lines: list[LineRow], feed_tickets: int | None = None, feed: "FeedTotals | None" = None) -> dict:
    """Core aggregation used by every breakdown (product, category, promo...).

    feed_tickets, when given, replaces the ticket count of the aggregate-feed
    lines with the feed's own store-day total (see count_tickets).

    feed, when given, goes further: for every store-day the aggregate feed
    reported, its totals stand in for that day's feed lines (which may be
    absent, because product detail is pulled for fewer days than totals, or
    partial). POS lines and feed lines for uncovered store-days still add."""
    done = completed(lines)
    if feed is not None and feed.covered:
        done = [ln for ln in done if not (ln.source == "headset" and (ln.store_code, ln.sold_at.date()) in feed.covered)]
    gross_sales = sum((ln.gross_sales for ln in done), ZERO)
    discount_total = sum((ln.discount_amount for ln in done), ZERO)
    revenue = sum((ln.revenue for ln in done), ZERO)
    cogs = sum((ln.cogs for ln in done), ZERO)
    units = sum(ln.quantity for ln in done)
    if feed is not None and feed.covered:
        gross_sales += feed.gross_sales
        discount_total += feed.discount_total
        revenue += feed.revenue
        cogs += feed.cogs
        units += feed.units
        transactions = count_tickets(done) + feed.transactions
    elif feed_tickets is None:
        transactions = count_tickets(done)
    else:
        transactions = count_tickets([ln for ln in done if ln.source == "pos"]) + feed_tickets
    gross_profit = revenue - cogs
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


@dataclass
class FeedTotals:
    """What the aggregate feed reported per store-day inside a period."""
    covered: set
    gross_sales: Decimal = ZERO
    discount_total: Decimal = ZERO
    revenue: Decimal = ZERO
    cogs: Decimal = ZERO
    units: int = 0
    transactions: int = 0


def feed_totals(session: Session, period: Period, store_code: str | None = None) -> FeedTotals:
    stmt = select(DailyStoreSummary, Store.code).join(Store, DailyStoreSummary.store_id == Store.id).where(
        DailyStoreSummary.sale_date >= period.start, DailyStoreSummary.sale_date <= period.end
    )
    if store_code:
        stmt = stmt.where(store_predicate(store_code))
    ft = FeedTotals(covered=set())
    for row, code in session.execute(stmt):
        ft.covered.add((code, row.sale_date))
        ft.gross_sales += row.gross_sales
        ft.discount_total += row.discount_total
        ft.revenue += row.revenue
        ft.cogs += row.cogs
        ft.units += row.units
        ft.transactions += row.transaction_count
    return ft


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


def _build(period_dict: dict, lines: list[LineRow], opex: Decimal, tickets: int | None = None, feed: FeedTotals | None = None) -> FinancialSummary:
    core = summarize_lines(lines, tickets, feed)
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
        None,
        feed_totals(session, period, store_code),
    )


def financial_summary_multi(session: Session, periods: list[Period], store_code: str | None = None) -> FinancialSummary:
    """One summary over several disjoint periods (e.g. the same weekdays of four
    different weeks). Totals add; ratios are derived from the combined totals."""
    lines: list[LineRow] = []
    opex = ZERO
    feed = FeedTotals(covered=set())
    for p in periods:
        lines.extend(load_lines(session, p, store_code))
        opex += operating_expenses(session, p, store_code)
        f = feed_totals(session, p, store_code)
        feed.covered |= f.covered
        feed.gross_sales += f.gross_sales; feed.discount_total += f.discount_total; feed.revenue += f.revenue
        feed.cogs += f.cogs; feed.units += f.units; feed.transactions += f.transactions
    period_dict = {
        "label": "multi",
        "start": min(p.start for p in periods).isoformat(),
        "end": max(p.end for p in periods).isoformat(),
        "days": sum(p.days for p in periods),
        "periods": [p.to_dict() for p in periods],
    }
    return _build(period_dict, lines, money(opex), None, feed)
