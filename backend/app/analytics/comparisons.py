"""Historical comparisons: current week vs previous week vs four-week average.

When as_of falls mid-week the current week is PARTIAL. Comparing five days
against seven would read as a collapse, so partial weeks are compared
week-to-date: the same weekdays (Mon..as_of weekday) of the previous week and
of each of the four weeks before that. The response says which basis was used.
"""
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.analytics.financial import FinancialSummary, financial_summary, financial_summary_multi
from app.analytics.money import money, pct_change, rate, safe_div
from app.analytics.periods import Period, previous_week, trailing_weeks, week_containing

TOTAL_FIELDS = (
    "gross_sales",
    "discount_total",
    "revenue",
    "cogs",
    "gross_profit",
    "refund_amount",
    "operating_expenses",
    "operating_profit",
)
COUNT_FIELDS = ("transactions", "units", "refund_count", "void_count")
RATIO_FIELDS = ("discount_rate", "gross_margin", "avg_transaction_value", "units_per_transaction")


def average_week(window_summary: FinancialSummary, weeks: int) -> dict:
    """Totals divided by number of weeks; ratios re-derived from window totals so
    they are volume-weighted rather than an average of ratios."""
    s = asdict(window_summary)
    out = {"period": s["period"], "weeks": weeks}
    for f in TOTAL_FIELDS:
        out[f] = money(safe_div(s[f], weeks))
    for f in COUNT_FIELDS:
        out[f] = rate(safe_div(s[f], weeks))
    for f in RATIO_FIELDS:
        out[f] = s[f]
    return out


def _deltas(current: dict, baseline: dict) -> dict:
    deltas = {}
    for f in TOTAL_FIELDS + COUNT_FIELDS + RATIO_FIELDS:
        cur, base = Decimal(str(current[f])), Decimal(str(baseline[f]))
        deltas[f] = {"abs": money(cur - base) if f in TOTAL_FIELDS else rate(cur - base), "pct": pct_change(cur, base)}
    return deltas


def comparison_periods(as_of: date) -> tuple[Period, Period, list[Period], bool, int]:
    """current, previous, four trailing periods, is_partial, days_elapsed."""
    full = week_containing(as_of)
    is_partial = as_of < full.end
    days = (as_of - full.start).days + 1
    span = timedelta(days=days - 1)
    current = Period("current_week", full.start, as_of) if is_partial else full
    prev_full = previous_week(full)
    prev = Period("previous_week", prev_full.start, prev_full.start + span) if is_partial else prev_full
    trailing = [Period(w.label, w.start, w.start + span) if is_partial else w for w in trailing_weeks(full, 4)]
    return current, prev, trailing, is_partial, days


def weekly_comparison(session: Session, as_of: date, store_code: str | None = None) -> dict:
    current, prev, trailing, is_partial, days = comparison_periods(as_of)

    cur_s = financial_summary(session, current, store_code)
    prev_s = financial_summary(session, prev, store_code)
    win_s = financial_summary_multi(session, trailing, store_code)
    avg4 = average_week(win_s, 4)

    return {
        "as_of": as_of.isoformat(),
        "store": store_code,
        "is_partial": is_partial,
        "days_elapsed": days,
        "comparison_basis": (
            f"week-to-date: {current.start.strftime('%a')}..{current.end.strftime('%a')} of each week"
            if is_partial
            else "full week (Mon..Sun)"
        ),
        "current_week": cur_s.to_dict(),
        "previous_week": prev_s.to_dict(),
        "four_week_average": avg4,
        "vs_previous_week": _deltas(cur_s.to_dict(), prev_s.to_dict()),
        "vs_four_week_average": _deltas(cur_s.to_dict(), avg4),
    }


def weekly_trend(session: Session, as_of: date, weeks: int = 12, store_code: str | None = None) -> list[dict]:
    """One FULL-week summary per week, oldest first, ending with the week containing
    as_of (which may be partial; `is_partial` says so)."""
    current = week_containing(as_of)
    periods = trailing_weeks(current, weeks - 1) + [current]
    rows = []
    for p in periods:
        s = financial_summary(session, p, store_code).to_dict()
        s["week_start"] = p.start.isoformat()
        s["is_partial"] = p.end > as_of
        rows.append(s)
    return rows
