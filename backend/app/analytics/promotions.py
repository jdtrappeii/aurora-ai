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

from app.analytics.financial import summarize_lines
from app.analytics.lines import LineRow, completed, load_lines
from app.analytics.money import ZERO, money, pct_change, rate, safe_div
from app.analytics.periods import Period
from app.models import Promotion

BASELINE_DAYS = 28


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
