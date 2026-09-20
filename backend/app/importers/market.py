"""Market context and competitor intelligence from spreadsheets.

  import_market_rows(session, rows, self_operator)   the state regulator's weekly
      dispensing report (Florida OMMU) as compiled in the market dashboard: one
      row per week per operator -> market_weekly
  market_competition_events(session, centroid)       operators that added dispensing
      locations week over week -> statewide `competition` event drafts
  deals_to_events(rows, centroid, self_operator)     the competitor deals library ->
      `competition` event drafts, severity by offer depth

Columns are matched by normalised header with aliases, so the tabs can be
renamed or re-ordered without code changes. Unknown or unparsable rows are
reported, never guessed.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.csv_importer import ImportResult
from app.integrations.events.common import EventDraft
from app.integrations.sheets import cell_bool, cell_date, cell_num, cell_str
from app.models import MarketWeekly

STATEWIDE_RADIUS_KM = 1000.0

MARKET_ALIASES = {
    "week_ending": ("week_ending", "week", "week_end"),
    "operator": ("mmtc_name_canonical", "operator_canonical", "operator", "mmtc", "mmtc_name", "mmtc_name_raw"),
    "is_self": ("is_p13_fl", "is_self", "is_us"),
    "is_total": ("is_totals_row", "is_total", "totals"),
    "dispensaries": ("dispensing_locations", "dispensaries", "locations"),
    "mg_thc": ("medical_marijuana_mg_thc", "mg_thc", "thc_mg"),
    "mg_cbd": ("low_thc_cannabis_mg_cbd", "mg_cbd", "cbd_mg"),
    "flower_oz": ("marijuana_smoking_oz", "flower_oz", "smoking_oz"),
    "share_thc_pct": ("share_thc_pct", "thc_share_pct", "thc_share"),
    "share_flower_pct": ("share_flower_pct", "flower_share_pct", "flower_share"),
    "share_locations_pct": ("share_locations_pct", "locations_share_pct"),
    "patients": ("statewide_qualified_patients", "patients", "qualified_patients"),
    "source_url": ("source_url", "source"),
    "report_date": ("report_date",),
    "superseded": ("supersedes_row_id",),
}
DEAL_ALIASES = {
    "id": ("deal_id", "row_id", "id"),
    "operator": ("operator_canonical", "operator_display", "operator", "operator_raw"),
    "when": ("observed_at_utc", "deal_date", "first_seen", "observed_at", "date"),
    "expires": ("expires", "expires_at", "valid_through", "end"),
    "offer_type": ("offer_type", "type"),
    "offer_value": ("offer_value", "offer", "value"),
    "hook": ("hook",),
    "audience": ("audience",),
    "confidence": ("confidence",),
    "link": ("source_url_or_msg_id", "original_link", "source_ref", "source_url", "link"),
    "subject": ("subject", "subject_gmail", "full_deal_text_ocr", "preview_text"),
    "source": ("observation_source", "seen_via", "source"),
}


def pick(row: dict, aliases: tuple[str, ...]):
    for a in aliases:
        if a in row and row[a] not in (None, ""):
            return row[a]
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


# ---------- market_weekly ----------

def import_market_rows(session: Session, rows: list[dict], self_operator: str | None = None) -> ImportResult:
    res = ImportResult("market_weekly")
    existing = {(m.week_ending, m.operator): m for m in session.execute(select(MarketWeekly)).scalars()}
    self_norm = (self_operator or "").casefold().strip()
    for i, r in enumerate(rows, start=2):
        week = cell_date(pick(r, MARKET_ALIASES["week_ending"]))
        operator = cell_str(pick(r, MARKET_ALIASES["operator"]))
        if week is None or not operator or operator.casefold() == "week_ending":
            res.skipped += 1
            continue
        if pick(r, MARKET_ALIASES["superseded"]):
            pass  # a restatement row: it carries the corrected numbers, keep it (same key overwrites)
        is_total = cell_bool(pick(r, MARKET_ALIASES["is_total"]))
        is_self = cell_bool(pick(r, MARKET_ALIASES["is_self"])) or (bool(self_norm) and operator.casefold() == self_norm)
        values = dict(
            is_self=int(is_self), is_total=int(is_total),
            dispensaries=int(cell_num(pick(r, MARKET_ALIASES["dispensaries"])) or 0) if cell_num(pick(r, MARKET_ALIASES["dispensaries"])) is not None else None,
            mg_thc=cell_num(pick(r, MARKET_ALIASES["mg_thc"])),
            mg_cbd=cell_num(pick(r, MARKET_ALIASES["mg_cbd"])),
            flower_oz=cell_num(pick(r, MARKET_ALIASES["flower_oz"])),
            share_thc_pct=cell_num(pick(r, MARKET_ALIASES["share_thc_pct"])),
            share_flower_pct=cell_num(pick(r, MARKET_ALIASES["share_flower_pct"])),
            share_locations_pct=cell_num(pick(r, MARKET_ALIASES["share_locations_pct"])),
            patients=int(cell_num(pick(r, MARKET_ALIASES["patients"])) or 0) or None,
            source_url=cell_str(pick(r, MARKET_ALIASES["source_url"])),
            report_date=cell_date(pick(r, MARKET_ALIASES["report_date"])),
        )
        key = (week, operator)
        m = existing.get(key)
        if m is None:
            m = MarketWeekly(week_ending=week, operator=operator, **values)
            session.add(m)
            existing[key] = m
            res.inserted += 1
        else:
            for k, v in values.items():
                setattr(m, k, v)
            res.updated += 1
    session.commit()
    return res


def market_competition_events(session: Session, centroid: tuple[float, float]) -> list[EventDraft]:
    """An operator whose dispensing-location count rose against its previous
    reported week opened stores that week. Statewide until an address is known."""
    rows = session.execute(
        select(MarketWeekly).where(MarketWeekly.is_total == 0, MarketWeekly.is_self == 0).order_by(MarketWeekly.operator, MarketWeekly.week_ending)
    ).scalars().all()
    drafts = []
    prev: dict[str, MarketWeekly] = {}
    for m in rows:
        p = prev.get(m.operator)
        if p is not None and m.dispensaries is not None and p.dispensaries is not None and m.dispensaries > p.dispensaries:
            n = m.dispensaries - p.dispensaries
            drafts.append(EventDraft(
                event_id=f"ommu:{m.week_ending.isoformat()}:{_slug(m.operator)}:+{n}",
                event_type="competition", source="ommu",
                start_time=datetime.combine(m.week_ending - timedelta(days=6), time.min),
                end_time=datetime.combine(m.week_ending, time(23, 59)),
                severity="major" if n >= 3 else "moderate",
                description=f"{m.operator} added {n} dispensing location{'s' if n > 1 else ''} ({p.dispensaries} → {m.dispensaries}), week ending {m.week_ending.isoformat()}",
                latitude=centroid[0], longitude=centroid[1], affected_radius_km=STATEWIDE_RADIUS_KM,
                source_reference=m.source_url, confidence=Decimal("0.9"),
                metadata={"operator": m.operator, "from": p.dispensaries, "to": m.dispensaries, "week_ending": m.week_ending.isoformat()},
            ))
        prev[m.operator] = m
    return drafts


# ---------- competitor deals ----------

def offer_severity(offer_value: str | None, offer_type: str | None) -> tuple[str, Decimal | None]:
    """Depth of the offer decides severity: 40%+ off (or BOGO) major, 20%+ moderate,
    else minor. With no figure at all, the classified offer type decides: bundles
    and multi-buys are moderate, freebies and the rest minor."""
    text = f"{offer_value or ''} {offer_type or ''}".casefold()
    kind = (offer_type or "").casefold()
    m = re.search(r"(\d{1,3})\s*%", text)
    pct = Decimal(m.group(1)) if m else None
    bare = re.fullmatch(r"\s*(\d{1,3}(?:\.\d+)?)\s*", str(offer_value or ""))
    if pct is None and bare and "percent" in kind:      # the tracker stores "40" + "percent_off"
        pct = Decimal(bare.group(1))
    if bare and "dollar" in kind:                         # "10" + "dollar_off"
        text += f" ${bare.group(1)}"
    if "bogo" in text or "buy one" in text or "b1g1" in text:
        return "major", pct
    if pct is not None:
        if pct >= 40:
            return "major", pct
        if pct >= 20:
            return "moderate", pct
        return "minor", pct
    m = re.search(r"\$\s*(\d+)", text)
    if m:
        return ("moderate" if int(m.group(1)) >= 20 else "minor"), None
    kind = (offer_type or "").casefold()
    if any(k in kind for k in ("bundle", "multiple", "dollar", "price_drop", "stack")):
        return "moderate", None
    return "minor", None


UNKNOWN_OPERATOR = {"unknown", "unresolved", "n/a", "na", "none", "tbd", "?"}

# Florida MMTC brand names as they appear in deal copy, mapped to the operator
# names the OMMU report uses. Names from the market_weekly table are added at
# run time, so this only needs the cases where the brand differs from the licensee.
FL_BRAND_ALIASES = {
    "muv": "AltMed Florida", "müv": "AltMed Florida", "altmed": "AltMed Florida",
    "trulieve": "Trulieve", "curaleaf": "Curaleaf", "sunburn": "Sunburn Cannabis", "fluent": "Fluent",
    "cansortium": "Fluent", "surterra": "Surterra Wellness", "parallel": "Surterra Wellness",
    "rise": "RISE Dispensaries", "green thumb": "RISE Dispensaries", "ayr": "AYR Cannabis Dispensary", "liberty health": "AYR Cannabis Dispensary",
    "vidacann": "VidaCann", "the flowery": "The Flowery", "flowery": "The Flowery", "cookies": "Cookies", "jungle boys": "Jungle Boys",
    "insa": "Insa", "sanctuary": "Sanctuary Cannabis", "growhealthy": "GrowHealthy", "grow healthy": "GrowHealthy",
    "gold flora": "Gold Flora", "goldflora": "Gold Flora", "house of platinum": "House of Platinum Cannabis",
    "mint cannabis": "Mint Cannabis", "the mint": "Mint Cannabis", "cannabist": "Cannabist", "columbia care": "Cannabist",
    "green dragon": "Green Dragon", "revolution": "Revolution", "verano": "MÜV", "harvest": "Harvest",
    "curio": "Curio Wellness", "ethos": "Ethos",
}


def _norm_text(s: str) -> str:
    """casefold, strip accents, collapse to letters/digits/spaces."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()


def operator_matcher(known_operators: list[str] | None = None):
    """Returns f(text) -> operator name or None. Longest alias wins, so
    'green thumb' beats 'thumb' and 'jungle boys' beats 'boys'."""
    aliases: dict[str, str] = {_norm_text(a): n for a, n in FL_BRAND_ALIASES.items()}
    # Names from the market report win over the static table, so deals attribute
    # to the same operator string the market panel uses.
    for name in known_operators or []:
        n = _norm_text(name)
        if len(n) >= 4:
            aliases[n] = name
        # the licensee's first word is usually the brand ("Trulieve, Inc." -> trulieve)
        first = n.split(" ")[0] if n else ""
        if len(first) >= 5 and first not in {"green", "florida", "the", "house", "gold", "med"}:
            aliases[first] = name
    ordered = sorted(aliases.items(), key=lambda kv: -len(kv[0]))
    patterns = [(re.compile(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])"), name) for a, name in ordered if a]

    def match(text: str | None) -> str | None:
        t = _norm_text(text or "")
        if not t:
            return None
        for pat, name in patterns:
            if pat.search(t):
                return name
        return None
    return match


@dataclass
class DealsReport:
    drafts: list[EventDraft] = field(default_factory=list)
    skipped_self: int = 0
    skipped_unparsed: int = 0
    skipped_no_date: int = 0
    skipped_no_operator: int = 0
    skipped_blank: int = 0
    recovered_operator: int = 0
    duplicates: int = 0
    unattributed_samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"events": len(self.drafts), "skipped_self": self.skipped_self, "skipped_unparsed": self.skipped_unparsed,
                "skipped_no_date": self.skipped_no_date, "skipped_no_operator": self.skipped_no_operator, "skipped_blank": self.skipped_blank,
                "recovered_operator": self.recovered_operator, "duplicates": self.duplicates, "unattributed_samples": self.unattributed_samples}


def deals_to_events(rows: list[dict], centroid: tuple[float, float], self_operator: str | None = None,
                    default_days: int = 7, known_operators: list[str] | None = None) -> DealsReport:
    rep = DealsReport()
    self_norm = (self_operator or "").casefold()
    seen = set()
    match_operator = operator_matcher(known_operators)
    for r in rows:
        operator = cell_str(pick(r, DEAL_ALIASES["operator"]))
        if operator and operator.casefold() in UNKNOWN_OPERATOR:
            operator = None
        when = cell_date(pick(r, DEAL_ALIASES["when"]))
        cells = [str(v) for v in r.values() if v not in (None, "")]
        text_blob = " ".join(cells)
        if not operator:
            if when is None and not text_blob.strip():
                rep.skipped_blank += 1   # an empty formatted row
                continue
            # Site scrapes often leave the operator blank while the brand sits in the
            # scanned text somewhere in the row. Recover it from the names we know.
            operator = match_operator(text_blob)
            if operator:
                rep.recovered_operator += 1
            else:
                rep.skipped_no_operator += 1
                if len(rep.unattributed_samples) < 5 and when is not None:
                    prose = [c for c in cells if c.count(" ") >= 2 and not re.match(r"^\d{4}-\d{2}-\d{2}", c)]
                    longest = max(prose or cells, key=len) if cells else ""
                    rep.unattributed_samples.append(f"{when.isoformat()} · {longest[:120]}")
                continue
        if self_norm and (self_norm in operator.casefold() or operator.casefold() in self_norm):
            rep.skipped_self += 1
            continue
        if when is None:
            rep.skipped_no_date += 1
            continue
        offer_value = cell_str(pick(r, DEAL_ALIASES["offer_value"]))
        offer_type = cell_str(pick(r, DEAL_ALIASES["offer_type"]))
        if offer_value and "unparsed" in offer_value.casefold():
            offer_value = None
        if not offer_value:
            # The tracker classifies many deals (bundle, multiple, freebie...) without a
            # single figure. They are still competitor activity: label them by type and
            # the start of the observed text.
            text = cell_str(pick(r, DEAL_ALIASES["subject"])) or ""
            if not offer_type and not text:
                rep.skipped_unparsed += 1
                continue
            offer_value = (offer_type.replace("_", " ") if offer_type else "deal") + (f": {text[:100]}" if text else "")
        # One event per operator, offer and week: the tracker re-observes live deals daily.
        week = when - timedelta(days=when.weekday())
        rid = cell_str(pick(r, DEAL_ALIASES["id"])) or f"{week.isoformat()}-{_slug(operator)}-{_slug(offer_value)[:40]}"
        eid = f"deal:{rid}"
        if eid in seen:
            rep.duplicates += 1
            continue
        seen.add(eid)
        expires = cell_date(pick(r, DEAL_ALIASES["expires"]))
        end_day = expires if expires and expires >= when else when + timedelta(days=default_days)
        severity, pct = offer_severity(offer_value, offer_type)
        conf_text = (cell_str(pick(r, DEAL_ALIASES["confidence"])) or "").casefold()
        confidence = {"high": Decimal("0.9"), "medium": Decimal("0.7"), "low": Decimal("0.4")}.get(conf_text, Decimal("0.7"))
        hook = cell_str(pick(r, DEAL_ALIASES["hook"]))
        audience = cell_str(pick(r, DEAL_ALIASES["audience"]))
        rep.drafts.append(EventDraft(
            event_id=eid, event_type="competition", source="deal-intel",
            start_time=datetime.combine(when, time.min), end_time=datetime.combine(end_day, time(23, 59)),
            severity=severity,
            description=f"{operator}: {offer_value}" + (f" ({offer_type})" if offer_type else "") + (f" · {hook}" if hook else ""),
            latitude=centroid[0], longitude=centroid[1], affected_radius_km=STATEWIDE_RADIUS_KM,
            confidence=confidence, source_reference=cell_str(pick(r, DEAL_ALIASES["link"])),
            metadata={"operator": operator, "offer_type": offer_type, "offer_value": offer_value, "pct_off": str(pct) if pct is not None else None,
                      "hook": hook, "audience": audience, "confidence": conf_text or None, "observation_source": cell_str(pick(r, DEAL_ALIASES["source"]))},
        ))
    return rep
