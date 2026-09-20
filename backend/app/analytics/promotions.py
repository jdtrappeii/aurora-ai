"""Promotion profitability with a "deal autopsy" against a pre-promotion baseline.

For each promotion:
  promo lines     = completed sale lines tagged with the promotion, inside its date window
  attachment_rate = tickets containing the promo / all completed tickets in the window
  baseline        = the promotion's eligible products over the 28 days before start_date,
                    excluding lines that carried any promotion, normalised per day
  verdict         = compares revenue/day and gross profit/day against the baseline
"""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.discounts import promotion_feed
from app.analytics.financial import summarize_lines
from app.analytics.lines import LineRow, completed, load_lines
from app.analytics.money import ZERO, money, pct_change, rate, safe_div
from app.analytics.periods import Period
from app.analytics.scope import store_predicate
from app.models import DailyStoreSummary, PromoDayPerformance, Promotion, Store

BASELINE_DAYS = 28
SAME_WEEKDAY_WEEKS = 4


def day_totals_verdict(session: Session, promo: Promotion, store_code: str | None = None) -> dict | None:
    """Judge the promotion on the store-by-day totals the aggregate feed always
    carries: each promo day against the same weekday over the previous four
    weeks, in the stores it applies to (within the requested scope). Returns None
    when no totals exist for the promo days."""
    from app.importers.promotions_sheet import active_days
    days = active_days(promo, promo.start_date, promo.end_date)
    if not days:
        return None
    codes = {c for c in (promo.store_codes or "").split("|") if c}
    baseline_days = {d - timedelta(weeks=w) for d in days for w in range(1, SAME_WEEKDAY_WEEKS + 1)}
    lo, hi = min(baseline_days), max(days)
    stmt = select(DailyStoreSummary, Store.code).join(Store, DailyStoreSummary.store_id == Store.id).where(
        DailyStoreSummary.sale_date >= lo, DailyStoreSummary.sale_date <= hi)
    pred = store_predicate(store_code)
    if pred is not None:
        stmt = stmt.where(pred)
    if codes:
        stmt = stmt.where(Store.code.in_(sorted(codes)))
    promo_rows, base_rows = [], []
    dayset = set(days)
    for row, code in session.execute(stmt):
        if row.sale_date in dayset:
            promo_rows.append(row)
        elif row.sale_date in baseline_days:
            base_rows.append(row)
    if not promo_rows:
        return None

    def agg(rows):
        n_days = len({r.sale_date for r in rows})
        tot = {"revenue": sum(r.revenue for r in rows), "gross_profit": sum(r.gross_profit for r in rows),
               "discount_total": sum(r.discount_total for r in rows), "gross_sales": sum(r.gross_sales for r in rows),
               "transaction_count": sum(r.transaction_count for r in rows)}
        # per store-day, so a state view and a store view are comparable
        store_days = len({(r.store_id, r.sale_date) for r in rows})
        return {
            "days_with_data": n_days, "store_days": store_days,
            "revenue": money(tot["revenue"]), "gross_profit": money(tot["gross_profit"]), "discount_total": money(tot["discount_total"]),
            "transaction_count": tot["transaction_count"],
            "revenue_per_store_day": money(safe_div(tot["revenue"], store_days)),
            "gross_profit_per_store_day": money(safe_div(tot["gross_profit"], store_days)),
            "discount_per_store_day": money(safe_div(tot["discount_total"], store_days)),
            "tickets_per_store_day": rate(safe_div(tot["transaction_count"], store_days)),
            "discount_rate": rate(safe_div(tot["discount_total"], tot["gross_sales"])) if tot["gross_sales"] else None,
            "gross_margin": rate(safe_div(tot["gross_profit"], tot["revenue"])) if tot["revenue"] else None,
        }
    promo_agg = agg(promo_rows)
    base_agg = agg(base_rows) if base_rows else None
    out = {"scheduled_days": len(days), "window": promo_agg, "baseline": base_agg,
           "baseline_rule": f"same weekday, previous {SAME_WEEKDAY_WEEKS} weeks", "source": "store-day totals (Headset)"}
    if base_agg:
        out["vs_baseline"] = {
            "revenue_pct": pct_change(promo_agg["revenue_per_store_day"], base_agg["revenue_per_store_day"]),
            "gross_profit_pct": pct_change(promo_agg["gross_profit_per_store_day"], base_agg["gross_profit_per_store_day"]),
            "discount_pct": pct_change(promo_agg["discount_per_store_day"], base_agg["discount_per_store_day"]),
            "tickets_pct": pct_change(promo_agg["tickets_per_store_day"], base_agg["tickets_per_store_day"]),
            "gross_margin_delta": rate((promo_agg["gross_margin"] or ZERO) - (base_agg["gross_margin"] or ZERO)),
        }
        gp_up = promo_agg["gross_profit_per_store_day"] > base_agg["gross_profit_per_store_day"]
        rev_up = promo_agg["revenue_per_store_day"] > base_agg["revenue_per_store_day"]
        if gp_up:
            out["verdict"], out["explanation"] = "profitable", "Gross profit per store-day beat the same weekdays over the prior four weeks."
        elif rev_up:
            out["verdict"], out["explanation"] = "revenue_up_profit_down", "Revenue per store-day rose but gross profit fell: the discount cost more than the extra volume earned."
        else:
            out["verdict"], out["explanation"] = "unprofitable", "Revenue and gross profit per store-day both fell versus the same weekdays over the prior four weeks."
    else:
        out["verdict"], out["explanation"] = "no_baseline", "No store-day totals for the same weekdays in the prior four weeks."
    return out


def statewide_day_totals(session: Session, promo: Promotion) -> dict | None:
    """The workbook's own statewide totals for the promotion's days, when they
    were imported: net/gross/discount summed over the window, the return figure
    and the four-week comparison from the sheet. None when nothing is loaded."""
    from app.importers.promotions_sheet import active_days
    days = active_days(promo, promo.start_date, promo.end_date)
    if not days:
        return None
    rows = session.execute(select(PromoDayPerformance).where(PromoDayPerformance.day.in_(days))).scalars().all()
    if not rows:
        return None
    n = len(rows)
    net = sum((r.net_sales or ZERO) for r in rows)
    gross = sum((r.gross_sales or ZERO) for r in rows)
    disc = sum((r.discount_amount or ZERO) for r in rows)
    four = [r.four_week_avg_sales for r in rows if r.four_week_avg_sales is not None]
    rois = [r.promo_roi for r in rows if r.promo_roi is not None]
    four_avg = safe_div(sum(four), len(four)) if four else None
    return {
        "days_with_data": n, "scheduled_days": len(days),
        "net_sales": money(net), "gross_sales": money(gross), "discount_amount": money(disc),
        "net_sales_per_day": money(safe_div(net, n)),
        "discount_rate": rate(safe_div(disc, gross)) if gross else None,
        "promo_roi": rate(safe_div(sum(rois), len(rois))) if rois else None,
        "four_week_avg_sales": money(four_avg) if four_avg is not None else None,
        "vs_four_week_pct": pct_change(safe_div(net, n), four_avg) if four_avg else None,
        "source": "promotions workbook (statewide)",
    }


def eligible_skus(promo: Promotion) -> set[str]:
    return {s.strip() for s in (promo.eligible_skus or "").split("|") if s.strip()}


def _is_eligible(promo: Promotion, ln: LineRow) -> bool:
    skus = eligible_skus(promo)
    if skus and ln.sku in skus:
        return True
    if promo.eligible_category and ln.category == promo.eligible_category:
        return True
    return False


def _per_day(summary: dict, days: int) -> dict:
    return {
        "revenue_per_day": money(safe_div(summary["revenue"], days)),
        "gross_profit_per_day": money(safe_div(summary["gross_profit"], days)),
        "units_per_day": rate(safe_div(summary["units"], days)),
    }


def verdict(promo_daily: dict, base_daily: dict | None) -> tuple[str, str]:
    if base_daily is None:
        return "no_baseline", "No pre-promotion sales of eligible products to compare against."
    rev_up = promo_daily["revenue_per_day"] > base_daily["revenue_per_day"]
    gp_up = promo_daily["gross_profit_per_day"] > base_daily["gross_profit_per_day"]
    if gp_up:
        return "profitable", "Gross profit per day rose versus the baseline. Candidate to repeat."
    if rev_up and not gp_up:
        return "revenue_up_profit_down", "Revenue rose but gross profit fell: the discount gave away more than the extra volume earned. Narrow or modify."
    return "unprofitable", "Both revenue and gross profit per day fell versus the baseline. Discontinue."


def promotion_results(session: Session, store_code: str | None = None, promotion_name: str | None = None) -> list[dict]:
    stmt = select(Promotion).order_by(Promotion.start_date, Promotion.name)
    if promotion_name:
        stmt = stmt.where(Promotion.name == promotion_name)
    promos = session.execute(stmt).scalars().all()

    results = []
    for promo in promos:
        window = Period(promo.name, promo.start_date, promo.end_date)
        window_lines = completed(load_lines(session, window, store_code))
        promo_lines = [ln for ln in window_lines if ln.promotion_id == promo.id]
        promo_summary = summarize_lines(promo_lines)
        all_tickets = {ln.sale_id for ln in window_lines}
        promo_tickets = {ln.sale_id for ln in promo_lines}
        promo_daily = _per_day(promo_summary, window.days)

        base_period = Period("baseline", promo.start_date - timedelta(days=BASELINE_DAYS), promo.start_date - timedelta(days=1))
        base_lines = [
            ln
            for ln in completed(load_lines(session, base_period, store_code))
            if ln.promotion_id is None and _is_eligible(promo, ln)
        ]
        base_summary = summarize_lines(base_lines) if base_lines else None
        base_daily = _per_day(base_summary, base_period.days) if base_summary else None

        label, explanation = verdict(promo_daily, base_daily)
        day_totals = day_totals_verdict(session, promo, store_code)
        if label == "no_baseline" and day_totals and day_totals.get("verdict") not in (None, "no_baseline"):
            # no product lines to judge on, but the feed's store-day totals can
            label, explanation = day_totals["verdict"], day_totals["explanation"] + " (store-day totals)"
        results.append(
            {
                "promotion": promo.name,
                "start_date": promo.start_date.isoformat(),
                "end_date": promo.end_date.isoformat(),
                "days": window.days,
                "discount_type": promo.discount_type,
                "discount_value": promo.discount_value,
                "eligible_skus": sorted(eligible_skus(promo)),
                "eligible_category": promo.eligible_category,
                "weekdays": promo.weekdays,
                "store_codes": promo.store_codes.split("|") if promo.store_codes else [],
                "audience": promo.audience,
                "source": promo.source,
                "feed": promotion_feed(session, promo, store_code),
                "statewide": statewide_day_totals(session, promo),
                "day_totals": day_totals,
                **promo_summary,
                **promo_daily,
                "attachment_rate": rate(safe_div(len(promo_tickets), len(all_tickets))),
                "baseline": {"period": base_period.to_dict(), **base_summary, **base_daily} if base_summary else None,
                "vs_baseline": {
                    "revenue_per_day_pct": pct_change(promo_daily["revenue_per_day"], base_daily["revenue_per_day"]),
                    "gross_profit_per_day_pct": pct_change(promo_daily["gross_profit_per_day"], base_daily["gross_profit_per_day"]),
                    "gross_margin_delta": rate(Decimal(promo_summary["gross_margin"]) - Decimal(base_summary["gross_margin"])),
                }
                if base_daily
                else None,
                "verdict": label,
                "explanation": explanation,
            }
        )
    return results
