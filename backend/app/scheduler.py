"""Nightly scheduler for a headless box: runs `sync-all` once a day at
SYNC_HOUR_UTC (default 08 = 4am Eastern) and on start-up when the last run is
older than a day. No cron dependency, one process, logs to stdout.

    python -m app.scheduler            # docker compose service "scheduler"
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

from app.cli import main as cli_main

log = logging.getLogger("aurora.scheduler")


def next_run(now: datetime, hour_utc: int) -> datetime:
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def run_once(today: datetime | None = None) -> int:
    log.info("sync-all starting")
    try:
        rc = cli_main(["sync-all"])
    except Exception:  # noqa: BLE001 — the loop must survive a bad night
        log.exception("sync-all crashed")
        rc = 1
    log.info("sync-all finished rc=%s", rc)
    today = today or datetime.now(timezone.utc)
    if today.isoweekday() == int(os.environ.get("REPORT_WEEKDAY", "1")):
        try:
            log.info("weekly report starting")
            cli_main(["weekly-report", "--email"])
        except Exception:  # noqa: BLE001
            log.exception("weekly report crashed")
    return rc


def loop(hour_utc: int, run_immediately: bool = True) -> None:
    if run_immediately:
        run_once()
    while True:
        now = datetime.now(timezone.utc)
        nxt = next_run(now, hour_utc)
        log.info("next sync at %s", nxt.isoformat())
        time.sleep(max(1, (nxt - now).total_seconds()))
        run_once()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    loop(int(os.environ.get("SYNC_HOUR_UTC", "8")), run_immediately=os.environ.get("SYNC_ON_START", "1") == "1")
