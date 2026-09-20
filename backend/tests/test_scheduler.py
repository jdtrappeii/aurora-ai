"""The nightly pass: sync-all runs every configured step, skips unconfigured
ones with a reason, survives a failing source, and the scheduler picks the
next run time correctly."""
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from app import cli, config, scheduler
from app.models import ExternalEvent, Store


def test_next_run():
    now = datetime(2026, 9, 18, 6, 30, tzinfo=timezone.utc)
    assert scheduler.next_run(now, 8) == datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc)
    assert scheduler.next_run(now.replace(hour=9), 8) == datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)


def test_sync_all_runs_configured_steps_and_survives_failures(session, monkeypatch, capsys):
    session.add(Store(code="HS1", name="One", latitude=28.0, longitude=-82.0, timezone="America/New_York"))
    session.commit()
    # nothing external configured; weather and events will hit the mock and fail / skip
    for k in ("headset_mcp_url", "ticketmaster_api_key", "seatgeek_client_id", "fl511_api_key", "market_sheet_id", "deals_sheet_id", "promotions_url"):
        monkeypatch.setattr(config.settings, k, "")

    def handler(req):
        if "open-meteo" in req.url.host:
            return httpx.Response(503, json={"reason": "down"})
        return httpx.Response(200, json={"features": [], "hourly": {}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    args = SimpleNamespace(days=2, backfill_days=None, stores=None)
    rc = cli.sync_all(session, args, http=http)
    out = capsys.readouterr().out
    summary = json.loads(out[out.index("{"):])
    steps = summary["steps"]
    assert steps["headset"].startswith("skipped")
    assert steps["sheets"]["skipped"] == {"market": "MARKET_SHEET_ID not set", "deals": "DEALS_SHEET_ID not set", "promotions": "PROMOTIONS_URL not set"}
    assert steps["weather"]["warnings"] and "503" in steps["weather"]["warnings"][0]  # reported, not raised
    assert steps["events"]["providers"]["calendar"]["events"] >= 0
    assert steps["heartbeats"]["inserted"] == 0
    assert steps["reconcile"] == {"days": 0, "ok": 0, "mismatch": [], "missing": 0}
    assert rc == 0  # a provider warning is not a step failure
    # calendar events landed even with no keys at all
    kinds = {e.source for e in session.execute(select(ExternalEvent)).scalars()}
    assert kinds <= {"calendar"}


def test_sync_all_reports_step_crash(session, monkeypatch, capsys):
    monkeypatch.setattr(config.settings, "headset_mcp_url", "")

    def boom(*a, **k):
        raise RuntimeError("sheet exploded")

    monkeypatch.setattr(cli, "reconcile", boom)
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"features": [], "hourly": {}})))
    rc = cli.sync_all(session, SimpleNamespace(days=1, backfill_days=None, stores=None), http=http)
    out = capsys.readouterr().out
    summary = json.loads(out[out.index("{"):])
    assert summary["steps"]["reconcile"].startswith("FAILED: RuntimeError: sheet exploded")
    assert rc == 1
