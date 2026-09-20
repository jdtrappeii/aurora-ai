"""Discount / promo-code report from the aggregate feed (discount_daily).

For a period and optional store, one row per discount code:
  discount_total   = dollars given away under this code (attributable to it only)
  revenue          = net revenue of the items that carried the code
  units            = units of those items
  transaction_count= receipts that used the code (NOT additive across codes:
                     a receipt with two codes appears under both)
  discount_depth   = discount_total / (revenue + discount_total): how deep the
                     code cuts on the items it touches
  share            = discount_total / all discounts in the period
The '' code is undiscounted items and is reported separately as `undiscounted`.
Deltas compare against the previous period of the same length.
"""
import re
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.money import ZERO, money, pct_change, rate, safe_div
from app.analytics.periods import Period
from app.analytics.scope import is_state, store_predicate, stores_in_scope
from app.models import DiscountDaily, Store


def _load(session: Session, period: Period, store_code: str | None) -> dict[str, dict]:
    stmt = select(DiscountDaily).where(DiscountDaily.sale_date >= period.start, DiscountDaily.sale_date <= period.end)
    if store_code:
        stmt = stmt.join(Store, DiscountDaily.store_id == Store.id).where(store_predicate(store_code))
    agg: dict[str, dict] = defaultdict(lambda: {"discount_total": ZERO, "revenue": ZERO, "units": 0, "transaction_count": 0, "days": 0})
    for row in session.execute(stmt).scalars():
        a = agg[row.discount_name]
        a["discount_total"] += row.discount_total
        a["revenue"] += row.revenue
        a["units"] += row.units
        a["transaction_count"] += row.transaction_count
        a["days"] += 1
    return agg


def discount_report(session: Session, period: Period, store_code: str | None = None, limit: int = 25) -> dict:
    current = _load(session, period, store_code)
    prev_period = Period("previous", period.start - timedelta(days=period.days), period.start - timedelta(days=1))
    previous = _load(session, prev_period, store_code)

    undiscounted = current.pop("", None)
    previous.pop("", None)
    total = sum((a["discount_total"] for a in current.values()), ZERO)
    prev_total = sum((a["discount_total"] for a in previous.values()), ZERO)

    codes = []
    for name, a in current.items():
        prev = previous.get(name)
        codes.append({
            "discount_name": name,
            "discount_total": money(a["discount_total"]),
            "revenue": money(a["revenue"]),
            "units": a["units"],
            "transaction_count": a["transaction_count"],
            "discount_depth": rate(safe_div(a["discount_total"], a["revenue"] + a["discount_total"])),
            "share": rate(safe_div(a["discount_total"], total)),
            "previous_discount_total": money(prev["discount_total"]) if prev else None,
            "vs_previous_pct": pct_change(a["discount_total"], prev["discount_total"]) if prev else None,
        })
    codes.sort(key=lambda c: (-c["discount_total"], c["discount_name"]))
    return {
        "period": period.to_dict(),
        "previous_period": prev_period.to_dict(),
        "store": store_code,
        "total_discounts": money(total),
        "previous_total_discounts": money(prev_total),
        "vs_previous_pct": pct_change(total, prev_total),
        "code_count": len(codes),
        "undiscounted": {
            "revenue": money(undiscounted["revenue"]), "units": undiscounted["units"], "transaction_count": undiscounted["transaction_count"]
        } if undiscounted else None,
        "codes": codes[:limit],
    }


_KEYWORDS = ("flower", "vape", "vapes", "cart", "carts", "distillate", "rosin", "edible", "edibles", "pre-roll", "preroll", "prerolls",
             "pre-rolls", "tincture", "tinctures", "tablet", "tablets", "topical", "topicals", "derivative", "storewide", "apparel",
             "manager", "special", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "monday", "senior", "seniors",
             "veteran", "veterans", "medizin", "dreamland", "haha", "oni", "leaf", "vine", "nano", "syringe", "syringes", "concentrate")


def _tokens(text: str) -> tuple[set[str], set[str]]:
    """(percent/dollar figures, product keywords) in a deal name or POS code."""
    t = (text or "").casefold().replace("!", " ")
    figs = set(re.findall(r"(\d{1,3})\s*%", t)) | {f"${m}" for m in re.findall(r"\$\s*(\d+)", t)}
    words = set(re.findall(r"[a-z][a-z\-]+", t))
    return figs, {w for w in words if w in _KEYWORDS}


def match_discount_names(promo_name: str, candidates: list[str]) -> list[str]:
    """POS discount codes whose figure and product words agree with a calendar
    deal ("35% Off Planet 13 Flower" <-> "DD - Auto - 35% OFF ALL Planet 13 Flower!").
    Same figure and at least one shared product word; the generic patient
    programme codes (first-time, veterans...) never match a calendar deal."""
    figs, kws = _tokens(promo_name)
    if not figs:
        return []
    out = []
    for c in candidates:
        cf, ck = _tokens(c)
        if cf & figs and (ck & kws or (not kws and not ck)):
            out.append(c)
    return out


def auto_discount_names(session: Session, promo, store_code: str | None = None) -> list[str]:
    """Codes seen in the feed on the promotion's days that match its wording."""
    from app.importers.promotions_sheet import active_days
    days = active_days(promo, promo.start_date, promo.end_date)
    if not days:
        return []
    stmt = select(DiscountDaily.discount_name).where(DiscountDaily.sale_date >= min(days), DiscountDaily.sale_date <= max(days)).distinct()
    pred = store_predicate(store_code)
    if pred is not None:
        stmt = stmt.join(Store, DiscountDaily.store_id == Store.id).where(pred)
    names = [n for (n,) in session.execute(stmt)]
    return match_discount_names(promo.name, names)


def promotion_feed(session: Session, promo, store_code: str | None = None) -> dict | None:
    """What the aggregate feed (discount_daily) says about one promotion: the
    rows whose discount name is one of the promotion's POS names, on the days
    the promotion runs, in the stores it applies to; versus the same codes over
    the equal-length window before it started. None when the promotion has no
    POS discount names to join on."""
    from app.importers.promotions_sheet import active_days
    from app.models import Store

    matched = "sheet"
    listed = [n.strip() for n in (promo.discount_names or "").split("|") if n.strip()]
    if not listed:
        listed = auto_discount_names(session, promo, store_code)
        matched = "auto"
    if not listed:
        return None
    names = {n.casefold() for n in listed}
    codes = {c for c in (promo.store_codes or "").split("|") if c}
    if store_code and is_state(store_code):
        in_state = {s.code for s in stores_in_scope(session, store_code)}
        codes = (codes & in_state) if codes else in_state
        if not codes:
            return {"discount_names": sorted(names), "applies_to_store": False}
    elif store_code:
        if codes and store_code not in codes:
            return {"discount_names": sorted(names), "applies_to_store": False}
        codes = {store_code}

    def load(days: list) -> dict:
        if not days:
            return {"discount_total": ZERO, "revenue": ZERO, "units": 0, "transaction_count": 0, "days_with_data": 0}
        stmt = select(DiscountDaily).where(DiscountDaily.sale_date >= min(days), DiscountDaily.sale_date <= max(days))
        if codes:
            stmt = stmt.join(Store, DiscountDaily.store_id == Store.id).where(Store.code.in_(sorted(codes)))
        dayset = set(days)
        agg = {"discount_total": ZERO, "revenue": ZERO, "units": 0, "transaction_count": 0}
        seen_days = set()
        for row in session.execute(stmt).scalars():
            if row.sale_date not in dayset or row.discount_name.casefold() not in names:
                continue
            agg["discount_total"] += row.discount_total
            agg["revenue"] += row.revenue
            agg["units"] += row.units
            agg["transaction_count"] += row.transaction_count
            seen_days.add(row.sale_date)
        agg["days_with_data"] = len(seen_days)
        return agg

    window_days = active_days(promo, promo.start_date, promo.end_date)
    span = (promo.end_date - promo.start_date).days + 1
    base_promo_like = type("P", (), {})()
    base_promo_like.weekdays = promo.weekdays
    base_promo_like.start_date = promo.start_date - timedelta(days=span)
    base_promo_like.end_date = promo.start_date - timedelta(days=1)
    base_days = active_days(base_promo_like, base_promo_like.start_date, base_promo_like.end_date)
    cur, base = load(window_days), load(base_days)

    def per_day(a):
        n = a["days_with_data"]
        return {
            "discount_per_day": money(safe_div(a["discount_total"], n)), "revenue_per_day": money(safe_div(a["revenue"], n)),
            "tickets_per_day": rate(safe_div(a["transaction_count"], n)),
        }

    return {
        "discount_names": sorted(listed),
        "matched": matched,
        "applies_to_store": True,
        "scheduled_days": len(window_days),
        "window": {**{k: (money(v) if isinstance(v, Decimal) else v) for k, v in cur.items()},
                   "discount_depth": rate(safe_div(cur["discount_total"], cur["revenue"] + cur["discount_total"])), **per_day(cur)},
        "baseline": {**{k: (money(v) if isinstance(v, Decimal) else v) for k, v in base.items()}, **per_day(base)} if base["days_with_data"] else None,
        "vs_baseline": {
            "discount_per_day_pct": pct_change(per_day(cur)["discount_per_day"], per_day(base)["discount_per_day"]),
            "revenue_per_day_pct": pct_change(per_day(cur)["revenue_per_day"], per_day(base)["revenue_per_day"]),
            "tickets_per_day_pct": pct_change(per_day(cur)["tickets_per_day"], per_day(base)["tickets_per_day"]),
        } if base["days_with_data"] else None,
    }
