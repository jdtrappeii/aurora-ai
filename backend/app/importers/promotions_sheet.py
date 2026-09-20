"""Promotions from the marketing team's workbook (OneDrive / Google / local).

The sheet is the source of truth; Aurora reads it and never writes. Columns are
matched by normalised header with aliases (below), or by an explicit map from
PROMOTIONS_COLUMN_MAP. What a row needs:

  name            the promotion; also the upsert key
  start           first day it runs
  end             last day (blank = that day only; with weekdays = recurring for a year, noted)
  value           "35%", "$10 off", "BOGO", "60% OFF ALL 1G VAPES" — the type is
                  inferred when there is no type column
  weekdays        "Tuesday", "Tue", "Mon-Wed", "Tue, Thu", "Daily" (blank = every day)
  stores          store codes or Headset store names, comma / pipe separated (blank = all)
  skus / category what it applies to (for the deal autopsy baseline)
  discount_names  the POS discount name(s) exactly as the feed reports them, so the
                  promotion joins to discount_daily: "DD - Auto - Try it Tuesday: 60% OFF ..."

Rows with no name or no start date are reported and skipped, never invented.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.csv_importer import ImportResult
from app.integrations.sheets import cell_date, cell_num, cell_str, norm_header
from app.models import PromoDayPerformance, Promotion, Store

ALIASES = {
    "name": ("name", "promotion", "promo", "promo_name", "promotion_name", "deal", "deal_name", "offer", "title", "promo_type", "campaign"),
    "start": ("start", "start_date", "starts", "begin", "begins", "from", "send_date", "date"),
    "end": ("end", "end_date", "ends", "through", "thru", "to", "expires", "until", "stop_date", "stop"),
    "type": ("type", "discount_type", "kind"),
    "value": ("value", "discount", "discount_value", "amount", "percent", "pct_off", "off", "prices", "price"),
    "weekdays": ("weekdays", "weekday", "days", "day", "day_of_week", "dow", "recurrence"),
    "stores": ("stores", "store", "store_codes", "locations", "location", "region", "market"),
    "skus": ("skus", "sku", "eligible_skus", "products", "product", "items"),
    "category": ("category", "eligible_category", "product_category"),
    "discount_names": ("discount_names", "discount_name", "pos_name", "pos_discount", "pos_discount_name", "dutchie_name", "code", "codes"),
    "audience": ("audience", "segment", "customers"),
    "notes": ("notes", "note", "comments", "description", "status"),
}
WEEKDAY_NAMES = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}


_DAYNAME_HEADER = re.compile(r"(_daily_promos|^day$|^day_name$|^weekday_name$|^dow_name$)")


def day_name_of(row: dict) -> str | None:
    """A column that names the weekday of the row's date ("Thursday"), used only to
    catch a mistyped year. Promo calendars label it '<Month> Daily Promos'."""
    for h, v in row.items():
        if _DAYNAME_HEADER.search(h) and isinstance(v, str) and v.strip()[:3].casefold() in WEEKDAY_NAMES:
            return v.strip()
    return None


def fix_year_by_weekday(d: date, day_name: str | None) -> date:
    """2025-01-01 labelled 'Thursday' is really 2026-01-01: try the years either side."""
    if not day_name:
        return d
    want = WEEKDAY_NAMES[day_name[:3].casefold()]
    if d.isoweekday() == want:
        return d
    for delta in (1, -1):
        try:
            cand = d.replace(year=d.year + delta)
        except ValueError:   # Feb 29
            continue
        if cand.isoweekday() == want:
            return cand
    return d


def pick(row: dict, key: str, column_map: dict[str, str] | None = None):
    if column_map and key in column_map:
        return row.get(norm_header(column_map[key]))
    for a in ALIASES[key]:
        if a in row and row[a] not in (None, ""):
            return row[a]
    return None


def parse_weekdays(text) -> str | None:
    """'Tuesday' -> '2'; 'Mon-Wed' -> '1,2,3'; 'Tue, Thu' -> '2,4'; 'Daily'/'All'/blank -> None."""
    s = (str(text) if text is not None else "").casefold().strip()
    if not s or s in ("daily", "every day", "everyday", "all", "all week", "any"):
        return None
    if s in ("weekdays", "weekday"):
        return "1,2,3,4,5"
    if s in ("weekend", "weekends"):
        return "6,7"
    days: set[int] = set()
    for a, b in re.findall(r"([a-z]{3})[a-z]*\s*(?:-|–|to)\s*([a-z]{3})[a-z]*", s):
        if a in WEEKDAY_NAMES and b in WEEKDAY_NAMES:
            lo, hi = WEEKDAY_NAMES[a], WEEKDAY_NAMES[b]
            days.update(range(lo, hi + 1) if lo <= hi else list(range(lo, 8)) + list(range(1, hi + 1)))
    for tok in re.findall(r"[a-z]{3,}", s):
        if tok[:3] in WEEKDAY_NAMES:
            days.add(WEEKDAY_NAMES[tok[:3]])
    return ",".join(str(d) for d in sorted(days)) if days else None


def infer_type_value(type_text, value_text) -> tuple[str | None, Decimal | None]:
    t = (str(type_text) if type_text else "").casefold().strip()
    v = str(value_text) if value_text is not None else ""
    vl = v.casefold()
    if "bogo" in t or "bogo" in vl or "buy one" in vl or "b1g1" in vl:
        m = re.search(r"\d+", v)
        return "bogo", Decimal(m.group()) if m else Decimal("1")
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", v)
    if m:
        return "percent", Decimal(m.group(1))
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)", v)
    if m:
        return "amount", Decimal(m.group(1))
    n = cell_num(value_text)
    if t.startswith("perc") or t == "%":
        return "percent", n
    if t.startswith("amount") or t in ("$", "dollar", "dollars", "fixed"):
        return "amount", n
    if n is not None:
        return ("percent" if n <= 100 else "amount"), n
    return (t or None), None


def _split(text) -> list[str]:
    if text is None:
        return []
    return [p.strip() for p in re.split(r"[|;,\n]", str(text)) if p.strip()]


def resolve_store_codes(session: Session, tokens: list[str]) -> tuple[list[str], list[str]]:
    """Accept store codes or Headset store names (case/whitespace-insensitive); 'all' / 'FL' / blank = all."""
    stores = session.execute(select(Store)).scalars().all()
    by_code = {s.code.casefold(): s.code for s in stores}
    by_name = {" ".join(s.name.casefold().split()): s.code for s in stores}
    codes, unknown = [], []
    for t in tokens:
        k = " ".join(t.casefold().split())
        if k in ("all", "all stores", "florida", "fl", "all florida patients"):
            return [], []
        if k in by_code:
            codes.append(by_code[k])
        elif k in by_name:
            codes.append(by_name[k])
        else:
            hits = [c for n, c in by_name.items() if k in n]
            if len(hits) == 1:
                codes.append(hits[0])
            else:
                unknown.append(t)
    return sorted(set(codes)), unknown


_DEAL_SEP = re.compile(r"\s*(?:;|\n|\r|\u2022|\|)\s*")


def split_deals(text: str) -> list[str]:
    """A promo calendar packs a day's deals into one cell, separated by
    semicolons, bullets or line breaks. Each becomes its own promotion."""
    parts = [p.strip(" -\u00b7") for p in _DEAL_SEP.split(text or "")]
    return [p for p in parts if p]


def import_promotion_rows(session: Session, rows: list[dict], column_map: dict[str, str] | str | None = None,
                          source: str = "sheet") -> ImportResult:
    res = ImportResult("promotions_sheet")
    if isinstance(column_map, str):
        column_map = json.loads(column_map) if column_map.strip() else None
    existing = {(p.name, p.start_date): p for p in session.execute(select(Promotion)).scalars()}
    by_name: dict[str, list[Promotion]] = {}
    for p in existing.values():
        by_name.setdefault(p.name, []).append(p)
    touched: set[Promotion] = set()   # rows written by this import; only these merge
    year_fixes = 0
    for i, r in enumerate(rows, start=2):
        cell_name = cell_str(pick(r, "name", column_map))
        start = cell_date(pick(r, "start", column_map))
        if not cell_name:
            res.skipped += 1
            continue
        name = cell_name
        if start is None:
            res.errors.append(f"row {i} ({name}): no start date")
            res.skipped += 1
            continue
        end = cell_date(pick(r, "end", column_map))
        fixed = fix_year_by_weekday(start, day_name_of(r))
        if fixed != start:
            year_fixes += 1
            if end == start or end is None:
                end = fixed if end is not None else None
            start = fixed
        notes = cell_str(pick(r, "notes", column_map))
        weekdays = parse_weekdays(pick(r, "weekdays", column_map))
        if end is None:
            if weekdays:   # a recurring rule with no end: keep it running a year, and say so
                end = start + timedelta(days=365)
                notes = ((notes + " · ") if notes else "") + "open-ended (end assumed one year from start)"
            else:          # a calendar row: the deal runs that day
                end = start
        if end < start:
            res.errors.append(f"row {i} ({name}): end {end} precedes start {start}")
            res.skipped += 1
            continue
        codes, unknown = resolve_store_codes(session, _split(pick(r, "stores", column_map)))
        if unknown:
            res.errors.append(f"row {i} ({name}): unknown store(s) {unknown}; applied to all stores")
        skus = "|".join(_split(pick(r, "skus", column_map))) or None
        type_cell, value_cell = pick(r, "type", column_map), pick(r, "value", column_map)
        deals = split_deals(cell_name)
        for name in deals:
            # With explicit type/value columns use them; a packed calendar cell
            # carries the figure inside each deal's own text ("55% Off All Edibles").
            if len(deals) > 1 or (type_cell in (None, "") and value_cell in (None, "")):
                dtype, dvalue = infer_type_value(None, name)
            else:
                dtype, dvalue = infer_type_value(type_cell, value_cell)
            if dtype not in ("percent", "amount", "bogo") or dvalue is None:
                # a headline-only deal ("Manager's Special Menu") still has a window worth tracking
                dtype, dvalue = dtype if dtype in ("percent", "amount", "bogo") else "amount", dvalue or Decimal("0")
            values = dict(
                start_date=start, end_date=end, discount_type=dtype, discount_value=dvalue,
                eligible_skus=skus, eligible_category=cell_str(pick(r, "category", column_map)),
                weekdays=weekdays, store_codes="|".join(codes) or None,
                discount_names="|".join(_split(pick(r, "discount_names", column_map))) or None,
                audience=cell_str(pick(r, "audience", column_map)), notes=notes, source=source,
            )
            # The same deal on several rows (one per store, or one per day of a run):
            # merge only when the rows touch or overlap, so a deal that recurs on
            # separate dates stays separate windows instead of one long smear.
            promo = None
            for cand in by_name.get(name, []):
                if cand in touched and start <= cand.end_date + timedelta(days=1) and end >= cand.start_date - timedelta(days=1):
                    promo = cand
                    break
            if promo is not None:
                promo.store_codes = "|".join(sorted(set((promo.store_codes or "").split("|")) | set(codes) - {""})) or None if (promo.store_codes and codes) else None
                if weekdays and promo.weekdays:
                    promo.weekdays = ",".join(sorted(set(promo.weekdays.split(",")) | set(weekdays.split(",")), key=int))
                promo.start_date = min(promo.start_date, start)
                promo.end_date = max(promo.end_date, end)
                res.updated += 1
                continue
            promo = existing.get((name, start))
            if promo is None:
                promo = Promotion(name=name, **values)
                session.add(promo)
                existing[(name, start)] = promo
                by_name.setdefault(name, []).append(promo)
                res.inserted += 1
            else:
                for k, v in values.items():
                    setattr(promo, k, v)
                res.updated += 1
            touched.add(promo)
    # The sheet is the source of truth for its own rows: anything it no longer
    # lists (a deleted row, or a deal that was re-split) goes away. Manual and
    # CSV promotions are untouched.
    if year_fixes:
        res.errors.append(f"{year_fixes} row(s): date year corrected to match the weekday column")
    if touched:
        for promo in list(existing.values()):
            if promo.source == source and promo not in touched:
                session.delete(promo)
                res.removed += 1
    session.commit()
    return res


def active_days(promo: Promotion, start: date, end: date) -> list[date]:
    """Calendar days inside [start, end] on which the promotion runs."""
    days = {int(d) for d in promo.weekdays.split(",")} if promo.weekdays else None
    out = []
    d = max(start, promo.start_date)
    last = min(end, promo.end_date)
    while d <= last:
        if days is None or d.isoweekday() in days:
            out.append(d)
        d += timedelta(days=1)
    return out


PERF_COLUMNS = {   # workbook header (normalised) -> model column
    "net_sales": "net_sales", "gross_sales": "gross_sales", "discount_amount": "discount_amount", "discount_rate": "discount_rate",
    "promo_efficiency_roi": "promo_roi", "promo_roi": "promo_roi", "sales_per_hour": "sales_per_hour",
    "4_week_average_sales": "four_week_avg_sales", "forcasted_sales": "forecast_sales", "forecasted_sales": "forecast_sales",
    "transaction_count": "transaction_count", "average_ticket": "average_ticket",
}


def import_promo_day_performance(session: Session, rows: list[dict], tab: str | None = None,
                                 column_map: dict[str, str] | str | None = None) -> ImportResult:
    """The statewide totals the promo calendar records per day. One row per date;
    rows without any money column are ignored. Years are corrected from the
    weekday column the same way the calendar import does it."""
    res = ImportResult("promo_day_performance")
    if isinstance(column_map, str):
        column_map = json.loads(column_map) if column_map.strip() else None
    existing = {p.day: p for p in session.execute(select(PromoDayPerformance)).scalars()}
    for i, r in enumerate(rows, start=2):
        start = cell_date(pick(r, "start", column_map))
        if start is None:
            res.skipped += 1
            continue
        start = fix_year_by_weekday(start, day_name_of(r))
        values = {}
        for header, col in PERF_COLUMNS.items():
            v = cell_num(r.get(header))
            if v is not None:
                values[col] = int(v) if col == "transaction_count" else v
        if not values:
            res.skipped += 1
            continue
        values.update(source_tab=tab, weekday=day_name_of(r), notes=cell_str(pick(r, "notes", column_map)))
        row = existing.get(start)
        if row is None:
            session.add(PromoDayPerformance(day=start, **values))
            res.inserted += 1
        else:
            for k, v in values.items():
                setattr(row, k, v)
            res.updated += 1
    session.commit()
    return res
