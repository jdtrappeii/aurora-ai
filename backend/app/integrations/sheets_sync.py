"""Pull the three spreadsheet sources into Aurora in one go.

    report = sheets_sync(session, http, settings)           # every configured source
    report = sheets_sync(session, http, settings, which={"market"})

  market      OMMU market dashboard raw tab  -> market_weekly + competition events (new locations)
  deals       competitor deals library tab   -> competition events (severity by offer depth)
  promotions  promotions workbook            -> promotions (recurrence, store scope, POS names)

A source runs when its setting is filled in. Nothing is cached: every run
re-reads the live sheet and upserts, so an edit in the sheet is in Aurora on
the next sync.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import Session

from app.importers.csv_importer import ImportResult
from app.importers.market import deals_to_events, import_market_rows, market_competition_events
from app.importers.promotions_sheet import import_promotion_rows
from app.integrations.events.common import ProviderError
from app.integrations.events.sync import upsert_events
from app.integrations.sheets import fetch, google_sheet_csv, google_sheet_xlsx, pick_tab, read_table, workbook_tabs

MARKET_REQUIRED = ("week_ending", "mmtc_name_canonical")
DEALS_REQUIRED = ("deal_id", "operator_canonical")
PROMO_REQUIRED_ANY = (("promo",), ("promotion",), ("name",), ("deal",), ("promo_type",), ("offer",))


def _google_rows(http, sheet_id: str, tab: str, required: tuple[str, ...]) -> tuple[str, list[dict]]:
    """A named / gid tab as CSV, or the whole workbook with the tab found by headers."""
    if tab:
        return tab, read_table(google_sheet_csv(http, sheet_id, tab), "sheet.csv")
    return pick_tab(workbook_tabs(google_sheet_xlsx(http, sheet_id)), required)


@dataclass
class SheetsReport:
    ran: list[str] = field(default_factory=list)
    skipped: dict = field(default_factory=dict)
    results: list[ImportResult] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ran": self.ran, "skipped": self.skipped, "details": self.details, "warnings": self.warnings,
                "results": [r.to_dict() for r in self.results]}


def sheets_sync(session: Session, http: httpx.Client, settings, which: set[str] | None = None) -> SheetsReport:
    rep = SheetsReport()
    which = which or {"market", "deals", "promotions"}
    centroid = (settings.market_centroid_lat, settings.market_centroid_lon)

    if "market" in which:
        if not settings.market_sheet_id:
            rep.skipped["market"] = "MARKET_SHEET_ID not set"
        else:
            try:
                tab, rows = _google_rows(http, settings.market_sheet_id, settings.market_sheet_tab, MARKET_REQUIRED)
                rep.results.append(import_market_rows(session, rows, settings.market_self_operator or None))
                drafts = market_competition_events(session, centroid)
                rep.results.append(upsert_events(session, drafts, "ommu-competition"))
                rep.details["market"] = {"tab": tab, "rows": len(rows), "competition_events": len(drafts)}
                rep.ran.append("market")
            except ProviderError as e:
                rep.warnings.append(f"market: {e}")

    if "deals" in which:
        if not settings.deals_sheet_id:
            rep.skipped["deals"] = "DEALS_SHEET_ID not set"
        else:
            try:
                tab, rows = _google_rows(http, settings.deals_sheet_id, settings.deals_sheet_tab, DEALS_REQUIRED)
                dr = deals_to_events(rows, centroid, settings.market_self_operator or None)
                rep.results.append(upsert_events(session, dr.drafts, "deal-intel"))
                rep.details["deals"] = {"tab": tab, "rows": len(rows), **dr.to_dict()}
                rep.ran.append("deals")
            except ProviderError as e:
                rep.warnings.append(f"deals: {e}")

    if "promotions" in which:
        if not settings.promotions_url:
            rep.skipped["promotions"] = "PROMOTIONS_URL not set"
        else:
            try:
                data, name = fetch(http, settings.promotions_url, settings.promotions_sheet or None)
                if data[:2] == b"PK":
                    tabs = workbook_tabs(data)
                    tab, rows = None, []
                    if settings.promotions_sheet:
                        tab, rows = pick_tab(tabs, (), settings.promotions_sheet)
                    else:
                        for req in PROMO_REQUIRED_ANY:
                            try:
                                tab, rows = pick_tab(tabs, req)
                                break
                            except ProviderError:
                                continue
                        if tab is None:
                            tab, rows = next(iter(tabs.items()))
                else:
                    tab, rows = name, read_table(data, name)
                r = import_promotion_rows(session, rows, settings.promotions_column_map or None, source="sheet")
                rep.results.append(r)
                rep.details["promotions"] = {"tab": tab, "rows": len(rows), "headers": sorted(rows[0].keys()) if rows else []}
                rep.ran.append("promotions")
            except ProviderError as e:
                rep.warnings.append(f"promotions: {e}")
    return rep


def show_headers(http: httpx.Client, location: str, tab: str | None = None) -> dict:
    """For configuring PROMOTIONS_COLUMN_MAP: the normalised headers and the first rows."""
    data, name = fetch(http, location, tab)
    if data[:2] == b"PK":
        tabs = workbook_tabs(data)
        return {name: {"rows": len(rows), "headers": sorted(rows[0].keys()) if rows else [], "sample": rows[:2]} for name, rows in tabs.items()}
    rows = read_table(data, name, tab)
    return {"rows": len(rows), "headers": sorted(rows[0].keys()) if rows else [], "sample": rows[:3]}
