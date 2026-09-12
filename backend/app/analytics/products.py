"""Product and category profitability for a period."""
from collections import defaultdict

from sqlalchemy.orm import Session

from app.analytics.financial import summarize_lines
from app.analytics.lines import LineRow, completed, load_lines
from app.analytics.money import ZERO, rate, safe_div
from app.analytics.periods import Period


def _group(lines: list[LineRow], key) -> dict:
    groups: dict = defaultdict(list)
    for ln in completed(lines):
        groups[key(ln)].append(ln)
    return groups


def product_profitability(session: Session, period: Period, store_code: str | None = None) -> list[dict]:
    lines = load_lines(session, period, store_code)
    rows = []
    for (sku,), group in _group(lines, lambda ln: (ln.sku,)).items():
        first = group[0]
        summary = summarize_lines(group)
        rows.append(
            {
                "sku": sku,
                "product": first.product_name,
                "category": first.category,
                "brand": first.brand,
                "vendor": first.vendor,
                **summary,
            }
        )
    rows.sort(key=lambda r: (-r["gross_profit"], r["sku"]))
    return rows


def category_profitability(session: Session, period: Period, store_code: str | None = None) -> list[dict]:
    lines = load_lines(session, period, store_code)
    total_revenue = sum((ln.revenue for ln in completed(lines)), ZERO)
    rows = []
    for category, group in _group(lines, lambda ln: ln.category).items():
        summary = summarize_lines(group)
        share = rate(safe_div(summary["revenue"], total_revenue))
        rows.append({"category": category, "revenue_share": share, **summary})
    rows.sort(key=lambda r: (-r["gross_profit"], r["category"]))
    return rows
