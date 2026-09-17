"""Store heartbeat receiver and outage derivation.

A device at the store (a Raspberry Pi, a smart plug with a webhook, a script on
the POS machine) calls POST /api/heartbeat?store=HS10136&kind=power every
minute. `record_ping` extends the current HeartbeatRun or opens a new one when
the silence exceeded the gap. `heartbeat_events` turns every silence between
runs into an external event: kind=power -> utility, kind=network ->
connectivity, severity by duration. The events are explicit to the store, so
no radius matching is involved, and the event engine's expected-vs-actual then
prices what the outage cost.

Silence is only evidence of an outage if the device itself was healthy; a
device that was unplugged looks the same. Keep that in mind when a finding is
the only one of its kind.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.events.common import EventDraft
from app.models import HeartbeatRun, Store

KINDS = {"power": "utility", "network": "connectivity"}


def severity_for(minutes: float) -> str:
    if minutes < 15:
        return "minor"
    if minutes < 60:
        return "moderate"
    if minutes < 240:
        return "major"
    return "severe"


def record_ping(session: Session, store: Store, kind: str, at: datetime, gap_minutes: int) -> HeartbeatRun:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    run = session.execute(
        select(HeartbeatRun).where(HeartbeatRun.store_id == store.id, HeartbeatRun.kind == kind)
        .order_by(HeartbeatRun.last_seen_at.desc()).limit(1)
    ).scalar_one_or_none()
    if run is not None and at >= run.last_seen_at - timedelta(minutes=gap_minutes) and (at - run.last_seen_at) <= timedelta(minutes=gap_minutes):
        run.last_seen_at = max(run.last_seen_at, at)
        run.pings += 1
    else:
        run = HeartbeatRun(store_id=store.id, kind=kind, started_at=at, last_seen_at=at, pings=1)
        session.add(run)
    session.commit()
    return run


@dataclass
class HeartbeatStatus:
    store: str
    kind: str
    last_seen_at: datetime | None
    minutes_silent: float | None
    run_started_at: datetime | None


def status(session: Session, now: datetime | None = None) -> list[HeartbeatStatus]:
    now = now or datetime.utcnow()
    out = []
    latest: dict[tuple[int, str], HeartbeatRun] = {}
    for run in session.execute(select(HeartbeatRun).order_by(HeartbeatRun.last_seen_at)).scalars():
        latest[(run.store_id, run.kind)] = run
    stores = {s.id: s.code for s in session.execute(select(Store)).scalars()}
    for (sid, kind), run in sorted(latest.items(), key=lambda kv: (stores.get(kv[0][0], ""), kv[0][1])):
        out.append(HeartbeatStatus(stores.get(sid, str(sid)), kind, run.last_seen_at,
                                   round((now - run.last_seen_at).total_seconds() / 60, 1), run.started_at))
    return out


def heartbeat_events(session: Session, min_gap_minutes: int, now: datetime | None = None,
                     open_gap_after_minutes: int | None = None) -> list[EventDraft]:
    """Every silence between consecutive runs (per store and kind) longer than
    min_gap_minutes becomes an event. With open_gap_after_minutes, a store that
    is silent *right now* for longer than that becomes an open-ended event too."""
    now = now or datetime.utcnow()
    stores = {s.id: s.code for s in session.execute(select(Store)).scalars()}
    runs: dict[tuple[int, str], list[HeartbeatRun]] = {}
    for run in session.execute(select(HeartbeatRun).order_by(HeartbeatRun.store_id, HeartbeatRun.kind, HeartbeatRun.started_at)).scalars():
        runs.setdefault((run.store_id, run.kind), []).append(run)
    drafts = []
    for (sid, kind), seq in runs.items():
        code = stores.get(sid)
        for prev, nxt in zip(seq, seq[1:]):
            gap_start, gap_end = prev.last_seen_at, nxt.started_at
            minutes = (gap_end - gap_start).total_seconds() / 60
            if minutes >= min_gap_minutes:
                drafts.append(_draft(code, kind, gap_start, gap_end, minutes, closed=True))
        last = seq[-1]
        if open_gap_after_minutes is not None:
            minutes = (now - last.last_seen_at).total_seconds() / 60
            if minutes >= open_gap_after_minutes:
                drafts.append(_draft(code, kind, last.last_seen_at, now, minutes, closed=False))
    return drafts


def _draft(code: str, kind: str, start: datetime, end: datetime, minutes: float, closed: bool) -> EventDraft:
    label = "Power" if kind == "power" else "Network"
    return EventDraft(
        event_id=f"hb:{code}:{kind}:{start.strftime('%Y%m%dT%H%M')}",
        event_type=KINDS[kind],
        source="heartbeat",
        start_time=start,
        end_time=end,
        severity=severity_for(minutes),
        description=f"{label} heartbeat silent for {int(round(minutes))} min" + ("" if closed else " (ongoing)"),
        store_code=code,
        metadata={"minutes": round(minutes, 1), "closed": closed},
    )
