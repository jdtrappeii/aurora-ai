"""Market context from the state's weekly report, and competitor promo pressure.

market_context(session, as_of):
  the latest reported market week on or before as_of, versus the week before and
  the mean of the four before that. Reports the statewide volume (mg THC), our
  volume, our share of THC and flower, our dispensary count, and patients.
  All arithmetic is on stored rows: a week-over-week percent and a share delta in
  basis points (100 bps = 1 percentage point). Market weeks are the regulator's
  (Florida: Friday report covering the week ending Thursday), so they are
  reported by week_ending rather than aligned to Aurora's Monday weeks.

competitor_pressure(session, period):
  competition events from the deals feed inside the period versus the previous
  period of the same length, per operator, plus how many operators were active.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.money import ZERO, D, money, pct_change, rate, safe_div
from app.analytics.periods import Period
from app.models import ExternalEvent, MarketWeekly


def _week_rows(session: Session, week_ending: date) -> list[MarketWeekly]:
    return session.execute(select(MarketWeekly).where(MarketWeekly.week_ending == week_ending)).scalars().all()


def _summarise(rows: list[MarketWeekly]) -> dict | None:
    if not rows:
        return None
    totals = [r for r in rows if r.is_total]
    operators = [r for r in rows if not r.is_total]
    market_mg = D(totals[0].mg_thc) if totals and totals[0].mg_thc is not None else sum((D(r.mg_thc) for r in operators if r.mg_thc is not None), ZERO)
    market_oz = D(totals[0].flower_oz) if totals and totals[0].flower_oz is not None else sum((D(r.flower_oz) for r in operators if r.flower_oz is not None), ZERO)
    market_disp = totals[0].dispensaries if totals and totals[0].dispensaries else sum(r.dispensaries or 0 for r in operators)
    us = next((r for r in operators if r.is_self), None)
    patients = next((r.patients for r in rows if r.patients), None)
    return {
        "week_ending": rows[0].week_ending.isoformat(),
        "market_mg_thc": money(market_mg),
        "market_flower_oz": money(market_oz),
        "market_dispensaries": market_disp,
        "operators": len(operators),
        "patients": patients,
        "self_operator": us.operator if us else None,
        "self_mg_thc": money(D(us.mg_thc)) if us and us.mg_thc is not None else None,
        "self_flower_oz": money(D(us.flower_oz)) if us and us.flower_oz is not None else None,
        "self_dispensaries": us.dispensaries if us else None,
        "share_thc_pct": rate(D(us.share_thc_pct)) if us and us.share_thc_pct is not None else (rate(safe_div(D(us.mg_thc) * 100, market_mg)) if us and us.mg_thc is not None and market_mg else None),
        "share_flower_pct": rate(D(us.share_flower_pct)) if us and us.share_flower_pct is not None else None,
    }


def _bps(cur, prev) -> Decimal | None:
    if cur is None or prev is None:
        return None
    return rate((D(cur) - D(prev)) * 100)


def market_context(session: Session, as_of: date) -> dict | None:
    weeks = session.execute(
        select(MarketWeekly.week_ending).where(MarketWeekly.week_ending <= as_of).distinct().order_by(MarketWeekly.week_ending.desc()).limit(6)
    ).scalars().all()
    if not weeks:
        return None
    current = _summarise(_week_rows(session, weeks[0]))
    previous = _summarise(_week_rows(session, weeks[1])) if len(weeks) > 1 else None
    trailing = [_summarise(_week_rows(session, w)) for w in weeks[2:6]]
    trailing = [t for t in trailing if t]

    def mean(key):
        vals = [D(t[key]) for t in trailing if t.get(key) is not None]
        return (sum(vals, ZERO) / len(vals)) if vals else None

    out = {
        "as_of": as_of.isoformat(),
        "current": current,
        "previous": previous,
        "weeks_in_average": len(trailing),
        "vs_previous_week": {
            "market_mg_thc_pct": pct_change(current["market_mg_thc"], previous["market_mg_thc"]) if previous else None,
            "self_mg_thc_pct": pct_change(current["self_mg_thc"], previous["self_mg_thc"]) if previous and current["self_mg_thc"] is not None and previous["self_mg_thc"] is not None else None,
            "share_thc_bps": _bps(current["share_thc_pct"], previous["share_thc_pct"]) if previous else None,
            "share_flower_bps": _bps(current["share_flower_pct"], previous["share_flower_pct"]) if previous else None,
            "self_dispensaries_delta": (current["self_dispensaries"] - previous["self_dispensaries"]) if previous and current["self_dispensaries"] is not None and previous["self_dispensaries"] is not None else None,
            "market_dispensaries_delta": (current["market_dispensaries"] - previous["market_dispensaries"]) if previous else None,
            "patients_delta": (current["patients"] - previous["patients"]) if previous and current["patients"] and previous["patients"] else None,
        },
        "vs_four_week_average": {
            "market_mg_thc_pct": pct_change(current["market_mg_thc"], mean("market_mg_thc")) if trailing else None,
            "self_mg_thc_pct": pct_change(current["self_mg_thc"], mean("self_mg_thc")) if trailing and current["self_mg_thc"] is not None and mean("self_mg_thc") is not None else None,
            "share_thc_bps": _bps(current["share_thc_pct"], mean("share_thc_pct")) if trailing else None,
            "share_flower_bps": _bps(current["share_flower_pct"], mean("share_flower_pct")) if trailing else None,
        },
    }
    # The one sentence a COO wants: did we move with the market or against it?
    m, s = out["vs_previous_week"]["market_mg_thc_pct"], out["vs_previous_week"]["self_mg_thc_pct"]
    if m is not None and s is not None:
        gap = rate(s - m)
        pts = (abs(gap) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if abs(gap) < Decimal("0.01"):
            out["read"] = "Moved with the market."
        elif gap > 0:
            out["read"] = f"Outpaced the market by {pts} points week over week."
        else:
            out["read"] = f"Trailed the market by {pts} points week over week."
    else:
        out["read"] = None
    return out


def competitor_pressure(session: Session, period: Period, source: str = "deal-intel", limit: int = 15) -> dict:
    prev = Period("previous", period.start - timedelta(days=period.days), period.start - timedelta(days=1))

    def load(p: Period) -> dict[str, dict]:
        stmt = select(ExternalEvent).where(
            ExternalEvent.event_type == "competition", ExternalEvent.source == source,
            ExternalEvent.start_time < datetime.combine(p.end + timedelta(days=1), time.min),
            ExternalEvent.end_time >= datetime.combine(p.start, time.min),
        )
        out: dict[str, dict] = defaultdict(lambda: {"deals": 0, "major": 0, "latest": None, "sample": None})
        for ev in session.execute(stmt).scalars():
            import json

            meta = json.loads(ev.raw_source_metadata) if ev.raw_source_metadata else {}
            op = meta.get("operator") or ev.description.split(":")[0]
            o = out[op]
            o["deals"] += 1
            o["major"] += 1 if ev.severity in ("major", "severe") else 0
            if o["latest"] is None or ev.start_time > o["latest"]:
                o["latest"] = ev.start_time
                o["sample"] = ev.description
        return out

    cur, before = load(period), load(prev)
    ops = []
    for op, o in cur.items():
        b = before.get(op)
        ops.append({
            "operator": op, "deals": o["deals"], "major": o["major"],
            "previous_deals": b["deals"] if b else 0,
            "vs_previous_pct": pct_change(o["deals"], b["deals"]) if b else None,
            "latest": o["latest"].date().isoformat() if o["latest"] else None, "sample": o["sample"],
        })
    ops.sort(key=lambda x: (-x["deals"], x["operator"]))
    total, prev_total = sum(o["deals"] for o in cur.values()), sum(o["deals"] for o in before.values())
    return {
        "period": period.to_dict(), "previous_period": prev.to_dict(),
        "total_deals": total, "previous_total_deals": prev_total, "vs_previous_pct": pct_change(total, prev_total),
        "operators_active": len(cur), "operators": ops[:limit],
    }
