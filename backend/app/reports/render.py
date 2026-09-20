"""Render the weekly report as self-contained HTML (email-safe: inline styles,
no scripts) or plain text. Every number comes from app.analytics.report."""
from __future__ import annotations

import html
from decimal import Decimal


def usd(v, cents: bool = False) -> str:
    if v is None:
        return "–"
    d = Decimal(str(v))
    return f"${d:,.2f}" if cents else f"${d:,.0f}"


def pct(v, digits: int = 1, signed: bool = False) -> str:
    if v is None:
        return "–"
    d = Decimal(str(v)) * 100
    s = f"{d:.{digits}f}%"
    return (f"+{s}" if d >= 0 else s) if signed else s


def num(v) -> str:
    return "–" if v is None else f"{Decimal(str(v)):,.0f}"


def _cls(v) -> str:
    if v is None:
        return "color:#6b7280"
    return "color:#15803d" if Decimal(str(v)) > 0 else ("color:#b91c1c" if Decimal(str(v)) < 0 else "color:#6b7280")


def _e(s) -> str:
    return html.escape(str(s)) if s is not None else ""


TABLE = 'style="border-collapse:collapse;width:100%;font-size:13px"'
TH = 'style="text-align:left;padding:6px 8px;border-bottom:2px solid #e5e7eb;color:#6b7280;font-weight:600"'
THN = 'style="text-align:right;padding:6px 8px;border-bottom:2px solid #e5e7eb;color:#6b7280;font-weight:600"'
TD = 'style="padding:6px 8px;border-bottom:1px solid #f3f4f6"'
TDN = 'style="padding:6px 8px;border-bottom:1px solid #f3f4f6;text-align:right;font-variant-numeric:tabular-nums"'


def _section(title: str, body: str, hint: str = "") -> str:
    return (f'<h2 style="font-size:16px;margin:28px 0 8px 0;color:#111827">{_e(title)}'
            + (f' <span style="font-weight:400;color:#6b7280;font-size:12px">{_e(hint)}</span>' if hint else "") + f"</h2>{body}")


def _kpis(h: dict) -> str:
    cw, d = h["current"], h["vs_previous_week"]
    cells = [
        ("Revenue", usd(cw["revenue"]), pct(d["revenue"]["pct"], signed=True), d["revenue"]["pct"]),
        ("Gross profit", usd(cw["gross_profit"]), pct(d["gross_profit"]["pct"], signed=True), d["gross_profit"]["pct"]),
        ("Gross margin", pct(cw["gross_margin"]), f"{Decimal(str(d['gross_margin']['abs'])) * 100:+.1f} pts", d["gross_margin"]["abs"]),
        ("Discount rate", pct(cw["discount_rate"]), f"{Decimal(str(d['discount_rate']['abs'])) * 100:+.1f} pts", -Decimal(str(d["discount_rate"]["abs"]))),
        ("Tickets", num(cw["transactions"]), pct(d["transactions"]["pct"], signed=True), d["transactions"]["pct"]),
        ("Avg ticket", usd(cw["avg_transaction_value"], True), pct(d["avg_transaction_value"]["pct"], signed=True), d["avg_transaction_value"]["pct"]),
    ]
    tds = "".join(
        f'<td style="padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;vertical-align:top">'
        f'<div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em">{_e(k)}</div>'
        f'<div style="font-size:20px;font-weight:700;color:#111827;margin:2px 0">{_e(v)}</div>'
        f'<div style="font-size:12px;{_cls(sgn)}">{_e(delta)} vs last week</div></td>'
        for k, v, delta, sgn in cells)
    return f'<table style="border-spacing:8px;width:100%"><tr>{tds}</tr></table>'


def _stores(rows: list[dict]) -> str:
    if not rows:
        return ""
    worst, best = rows[:8], list(reversed(rows[-8:]))

    def table(title, rs):
        body = "".join(
            f"<tr><td {TD}>{_e(r['name'])}</td><td {TDN}>{usd(r['revenue'])}</td><td {TDN};{_cls(r['revenue_pct'])}\">{pct(r['revenue_pct'], signed=True)}</td>"
            f"<td {TDN}>{usd(r['gross_profit'])}</td><td {TDN};{_cls(r['gross_profit_delta'])}\">{usd(r['gross_profit_delta'])}</td>"
            f"<td {TDN}>{pct(r['gross_margin'])}</td><td {TDN}>{pct(r['discount_rate'])}</td></tr>" for r in rs)
        return (f'<div style="font-size:12px;color:#6b7280;margin:10px 0 4px">{_e(title)}</div><table {TABLE}><tr><th {TH}>Store</th><th {THN}>Revenue</th>'
                f"<th {THN}>vs LW</th><th {THN}>Gross profit</th><th {THN}>GP Δ</th><th {THN}>Margin</th><th {THN}>Disc.</th></tr>{body}</table>")
    return table("Biggest gross-profit declines", worst) + table("Biggest gross-profit gains", best)


def _movers(m: dict) -> str:
    def table(title, rows, key):
        body = "".join(
            f"<tr><td {TD}>{_e(r.get('name') or r[key])}</td><td {TDN}>{usd(r['gross_profit'])}</td><td {TDN}>{usd(r['previous_gross_profit'])}</td>"
            f"<td {TDN};{_cls(r['delta'])}\">{usd(r['delta'])}</td><td {TDN}>{pct(r['pct'], signed=True)}</td></tr>" for r in rows)
        return (f'<div style="font-size:12px;color:#6b7280;margin:10px 0 4px">{_e(title)}</div><table {TABLE}><tr><th {TH}></th><th {THN}>GP this week</th>'
                f"<th {THN}>last week</th><th {THN}>Δ</th><th {THN}>%</th></tr>{body}</table>")
    out = table("Categories down", m["categories"]["down"], "category") + table("Categories up", m["categories"]["up"], "category")
    out += table("Products down", m["products"]["down"], "sku") + table("Products up", m["products"]["up"], "sku")
    return out


def _promos(rows: list[dict]) -> str:
    if not rows:
        return '<div style="color:#6b7280;font-size:13px">No promotions in or near this week.</div>'
    labels = {"profitable": "Profitable · repeat", "revenue_up_profit_down": "Revenue up, profit down · narrow",
              "unprofitable": "Unprofitable · discontinue", "no_baseline": "No POS-line baseline"}
    body = ""
    for p in rows:
        feed = p.get("feed") if p.get("feed") and p["feed"].get("window") else None
        feed_txt = (f"feed: {usd(feed['window']['discount_per_day'])}/day given away, {pct(feed['window']['discount_depth'], 0)} depth"
                    + (f", revenue {pct(feed['vs_baseline']['revenue_per_day_pct'], signed=True)} vs before" if feed.get("vs_baseline") else "")) if feed else ""
        body += (f"<tr><td {TD}><b>{_e(p['promotion'])}</b><br><span style=\"color:#6b7280;font-size:12px\">{_e(p['start_date'])} → {_e(p['end_date'])}"
                 f"{' · ' + _e(p['weekdays']) if p.get('weekdays') else ''}</span></td>"
                 f"<td {TD}>{_e(labels.get(p['verdict'], p['verdict']))}</td><td {TDN}>{usd(p['gross_profit_per_day'])}</td>"
                 f"<td {TD} style=\"font-size:12px;color:#374151\">{_e(feed_txt)}</td></tr>")
    return f"<table {TABLE}><tr><th {TH}>Promotion</th><th {TH}>Verdict</th><th {THN}>GP / day</th><th {TH}>Aggregate feed</th></tr>{body}</table>"


def _discounts(d: dict) -> str:
    if not d or d.get("code_count", 0) == 0:
        return '<div style="color:#6b7280;font-size:13px">No discount-code data for this week.</div>'
    body = "".join(f"<tr><td {TD}>{_e(c['discount_name'][:70])}</td><td {TDN}>{usd(c['discount_total'])}</td><td {TDN}>{pct(c['share'], 0)}</td>"
                   f"<td {TDN}>{pct(c['discount_depth'], 0)}</td><td {TDN}>{pct(c['vs_previous_pct'], signed=True)}</td></tr>" for c in d["codes"])
    head = f'<div style="font-size:13px;margin-bottom:6px">Total given away <b>{usd(d["total_discounts"])}</b> ({pct(d["vs_previous_pct"], signed=True)} vs prior period), {d["code_count"]} codes.</div>'
    return head + f"<table {TABLE}><tr><th {TH}>Code</th><th {THN}>$</th><th {THN}>Share</th><th {THN}>Depth</th><th {THN}>vs prior</th></tr>{body}</table>"


def _inventory(inv: dict) -> str:
    head = (f'<div style="font-size:13px;margin-bottom:6px">Inventory at cost <b>{usd(inv["inventory_value"])}</b>; '
            f'over 90 days old <b>{usd(inv["cash_tied_over_90_days"])}</b>; status: '
            + ", ".join(f"{k} {v}" for k, v in sorted(inv["status_counts"].items())) + "</div>")

    def table(title, rows):
        if not rows:
            return ""
        body = "".join(f"<tr><td {TD}>{_e(r['store'])}</td><td {TD}>{_e(r['product'])}</td><td {TDN}>{num(r['quantity_on_hand'])}</td>"
                       f"<td {TDN}>{usd(r['inventory_value'])}</td><td {TDN}>{num(r['units_sold_30d'])}</td><td {TDN}>{_e(r['days_of_supply'] if r['days_of_supply'] is not None else '–')}</td><td {TD}>{_e(r['status'])}</td></tr>" for r in rows)
        return (f'<div style="font-size:12px;color:#6b7280;margin:10px 0 4px">{_e(title)}</div><table {TABLE}><tr><th {TH}>Store</th><th {TH}>Product</th>'
                f"<th {THN}>On hand</th><th {THN}>Value</th><th {THN}>Sold 30d</th><th {THN}>Days supply</th><th {TH}>Status</th></tr>{body}</table>")
    return head + table("Stockout risk (under 7 days of supply)", inv["stockout_risk"]) + table("Dead and slow stock", inv["watch"])


def _outside(o: dict) -> str:
    rows = o["findings"]
    if not rows:
        return '<div style="color:#6b7280;font-size:13px">No material external findings this week.</div>'
    body = "".join(f"<tr><td {TD}>{_e(f['store'])}</td><td {TD}>{_e(f['start_time'][:10])}</td><td {TD}>{_e(f['event_type'])} · {_e(f['severity'])}</td>"
                   f"<td {TD}>{_e((f['description'] or '')[:80])}</td><td {TDN};{_cls(f['variance'])}\">{usd(f['variance'])} ({pct(f['variance_pct'], signed=True)})</td>"
                   f"<td {TD}>{_e(f['evidence_level'].replace('_', ' '))}</td></tr>" for f in rows)
    return f"<table {TABLE}><tr><th {TH}>Store</th><th {TH}>Date</th><th {TH}>Event</th><th {TH}>What</th><th {THN}>Revenue vs expected</th><th {TH}>Evidence</th></tr>{body}</table>"


def _pressure(p: dict) -> str:
    if not p or not p.get("operators"):
        return '<div style="color:#6b7280;font-size:13px">No competitor deals observed this period.</div>'
    body = "".join(f"<tr><td {TD}>{_e(o['operator'])}</td><td {TDN}>{o['deals']}</td><td {TDN}>{o['major']}</td><td {TDN}>{o['previous_deals']}</td>"
                   f"<td {TD} style=\"font-size:12px;color:#374151\">{_e((o['sample'] or '')[:70])}</td></tr>" for o in p["operators"])
    return (f'<div style="font-size:13px;margin-bottom:6px">{p["total_deals"]} competitor deals observed ({pct(p["vs_previous_pct"], signed=True)} vs prior period), {p["operators_active"]} operators.</div>'
            f"<table {TABLE}><tr><th {TH}>Operator</th><th {THN}>Deals</th><th {THN}>Deep</th><th {THN}>Prior</th><th {TH}>Latest</th></tr>{body}</table>")


def _next_week(n: dict) -> str:
    out = ""
    if n["events"]:
        body = "".join(f"<tr><td {TD}>{_e(e['date'])}</td><td {TD}>{_e(e['type'])} · {_e(e['severity'])}</td><td {TD}>{_e((e['description'] or '')[:90])}</td></tr>" for e in n["events"])
        out += f"<table {TABLE}><tr><th {TH}>Date</th><th {TH}>Kind</th><th {TH}>What</th></tr>{body}</table>"
    if n["forecast"]:
        body = "".join(
            f"<tr><td {TD}>{_e(d['date'])} {_e(d['weekday'])}</td><td {TDN}>{usd(d['expected_revenue_baseline'])}</td><td {TDN}>{usd(d['projected_low'])} – {usd(d['projected_high'])}</td>"
            f"<td {TD}>{_e(', '.join(d['weather']['tags']) if d.get('weather') and d['weather']['tags'] else '')}{' · ' + _e(', '.join(d['weather']['alerts'])) if d.get('weather') and d['weather']['alerts'] else ''}</td></tr>" for d in n["forecast"])
        out += f'<div style="font-size:12px;color:#6b7280;margin:10px 0 4px">Projection</div><table {TABLE}><tr><th {TH}>Day</th><th {THN}>Baseline</th><th {THN}>Projected</th><th {TH}>Weather</th></tr>{body}</table>'
    return out or '<div style="color:#6b7280;font-size:13px">Nothing on the calendar for next week.</div>'


def _coverage(c: dict) -> str:
    bits = [f"{c['stores_reporting']} store(s) reporting", "weather " + ("on" if c["weather"] else "off"),
            "market report " + ("on" if c["market"] else "off"), f"{c['promotions']} promotion(s) tracked"]
    miss = c["sales_days_missing_detail"]
    if miss:
        bits.append(f"{len(miss)} store-day(s) without product detail: " + ", ".join(miss[:6]) + (" …" if len(miss) > 6 else ""))
    return f'<div style="font-size:12px;color:#6b7280">{_e(" · ".join(bits))}</div>'


def render_html(r: dict) -> str:
    title = f"Aurora weekly · {r['store_name'] or 'all stores'} · week of {r['period']['start']}"
    body = (
        f'<h1 style="font-size:22px;margin:0 0 4px 0;color:#111827">{_e(title)}</h1>'
        f'<div style="color:#6b7280;font-size:13px;margin-bottom:12px">{_e(r["period"]["start"])} to {_e(r["period"]["end"])} · {_e(r["comparison_basis"])}'
        f'{" · partial week" if r["is_partial"] else ""} · as of {_e(r["as_of"])}</div>'
        f'<p style="font-size:15px;line-height:1.5;color:#111827;margin:0 0 8px 0">{_e(r["headline"]["read"])}</p>'
        + _kpis(r["headline"])
        + (_section("Stores", _stores(r["stores"]), "ranked by gross-profit change vs last week") if r["stores"] else "")
        + _section("What moved", _movers(r["movers"]), "gross profit vs last week")
        + _section("Promotions", _promos(r["promotions"]), "deal autopsy vs the 28 days before each")
        + _section("Discount codes", _discounts(r["discounts"]), "what each code cost this week")
        + _section("Inventory", _inventory(r["inventory"]))
        + _section("Outside the four walls", _outside(r["outside"]), "weather, outages, events, competition this week")
        + _section("Competitor pressure", _pressure(r["pressure"]))
        + _section("Next week", _next_week(r["next_week"]), f"{r['next_week']['start']} to {r['next_week']['end']}")
        + _section("Data coverage", _coverage(r["coverage"]))
    )
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{_e(title)}</title></head>'
            f'<body style="margin:0;background:#f9fafb"><div style="max-width:960px;margin:0 auto;padding:24px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#fff">{body}'
            f'<div style="margin-top:28px;font-size:11px;color:#9ca3af">Every figure is a deterministic calculation over stored rows. Evidence levels never promote correlation to causation.</div></div></body></html>')


def render_text(r: dict) -> str:
    h = r["headline"]
    lines = [f"AURORA WEEKLY · {r['store_name'] or 'all stores'} · {r['period']['start']} to {r['period']['end']}", "", h["read"], ""]
    cw, d = h["current"], h["vs_previous_week"]
    lines += [f"Revenue {usd(cw['revenue'])} ({pct(d['revenue']['pct'], signed=True)})  Gross profit {usd(cw['gross_profit'])} ({pct(d['gross_profit']['pct'], signed=True)})  "
              f"Margin {pct(cw['gross_margin'])}  Discount {pct(cw['discount_rate'])}  Tickets {num(cw['transactions'])}  Avg {usd(cw['avg_transaction_value'], True)}"]
    if r["stores"]:
        lines += ["", "STORES (gross-profit change vs last week)"]
        for s in r["stores"][:5]:
            lines.append(f"  ↓ {s['name']}: {usd(s['gross_profit_delta'])} ({pct(s['gross_profit_pct'], signed=True)})")
        for s in reversed(r["stores"][-5:]):
            lines.append(f"  ↑ {s['name']}: {usd(s['gross_profit_delta'])} ({pct(s['gross_profit_pct'], signed=True)})")
    lines += ["", "MOVERS"]
    for m in r["movers"]["categories"]["down"][:3]:
        lines.append(f"  ↓ {m['name']}: {usd(m['delta'])}")
    for m in r["movers"]["categories"]["up"][:3]:
        lines.append(f"  ↑ {m['name']}: {usd(m['delta'])}")
    if r["promotions"]:
        lines += ["", "PROMOTIONS"] + [f"  {p['promotion']}: {p['verdict']}" for p in r["promotions"][:6]]
    if r["outside"]["findings"]:
        lines += ["", "OUTSIDE"] + [f"  {f['store']} {f['start_time'][:10]} {f['event_type']}: {usd(f['variance'])} ({f['evidence_level']})" for f in r["outside"]["findings"][:6]]
    if r["next_week"]["events"]:
        lines += ["", "NEXT WEEK"] + [f"  {e['date']} {e['type']} {e['severity']}: {e['description']}" for e in r["next_week"]["events"][:8]]
    lines += ["", "COVERAGE: " + (", ".join(r["coverage"]["sales_days_missing_detail"][:4]) + " missing detail" if r["coverage"]["sales_days_missing_detail"] else "complete")]
    return "\n".join(lines)
