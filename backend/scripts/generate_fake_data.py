"""Generate a deterministic fake dispensary dataset as CSVs.

    python scripts/generate_fake_data.py --out ../sample_data --end 2026-09-11 --days 91 --seed 13

The data is seeded so it regenerates identically. It deliberately plants
patterns the analytics should surface: a dead SKU, a vendor cost increase,
a promotion that lifts revenue but kills margin, a payroll spike, and a
weekend-heavy traffic curve.

External Intelligence patterns (stores.csv, weather.csv, external_events.csv):
hourly weather with three heavy-rain days that halve afternoon traffic, a tropical
storm day preceded by a stocking-up surge, a heat wave, three power/internet
outages that black out the register, a road closure, a nearby concert, the
July 4 and Labor Day holidays, a competitor opening, and a 7-day forecast plus a
scheduled concert next week.
"""
import argparse
import csv
import random
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

CENT = Decimal("0.01")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


CATALOG = [
    # category, brand, vendor, name, unit_cost, retail_price, base_daily_units
    ("Flower", "Sunburst Farms", "Sunburst Farms", "Blue Dream 3.5g", "14.00", "35.00", 22),
    ("Flower", "Sunburst Farms", "Sunburst Farms", "Gelato 3.5g", "16.00", "40.00", 18),
    ("Flower", "Sunburst Farms", "Sunburst Farms", "Wedding Cake 7g", "28.00", "70.00", 8),
    ("Flower", "Northwind Gardens", "Northwind Gardens", "Sour Diesel 3.5g", "15.00", "38.00", 15),
    ("Flower", "Northwind Gardens", "Northwind Gardens", "OG Kush 3.5g", "15.50", "38.00", 14),
    ("Flower", "Northwind Gardens", "Northwind Gardens", "Purple Punch 14g", "52.00", "120.00", 3),
    ("Flower", "Valley Craft", "Valley Craft", "Zkittlez 3.5g", "13.00", "32.00", 12),
    ("Flower", "Valley Craft", "Valley Craft", "Pineapple Express 3.5g", "13.50", "32.00", 10),
    ("Flower", "Valley Craft", "Valley Craft", "GMO Cookies 28g", "95.00", "210.00", 1),
    ("Flower", "Valley Craft", "Valley Craft", "Shake 14g", "18.00", "45.00", 4),
    ("Pre-Rolls", "Sunburst Farms", "Sunburst Farms", "Blue Dream Pre-Roll 1g", "3.20", "10.00", 30),
    ("Pre-Rolls", "Sunburst Farms", "Sunburst Farms", "Gelato Pre-Roll 1g", "3.40", "10.00", 25),
    ("Pre-Rolls", "Northwind Gardens", "Northwind Gardens", "Infused Pre-Roll 1g", "6.00", "18.00", 12),
    ("Pre-Rolls", "Valley Craft", "Valley Craft", "Pre-Roll 5-Pack", "12.00", "35.00", 9),
    ("Pre-Rolls", "Valley Craft", "Valley Craft", "Mini Pre-Roll 0.5g", "1.80", "6.00", 20),
    ("Vapes", "Cloudline", "Cloudline Distribution", "Live Resin Cart 1g", "22.00", "55.00", 14),
    ("Vapes", "Cloudline", "Cloudline Distribution", "Distillate Cart 1g", "14.00", "40.00", 20),
    ("Vapes", "Cloudline", "Cloudline Distribution", "Disposable Vape 0.5g", "12.00", "32.00", 16),
    ("Vapes", "Ember Labs", "Ember Labs", "Rosin Cart 0.5g", "24.00", "60.00", 6),
    ("Vapes", "Ember Labs", "Ember Labs", "Distillate Cart 0.5g", "9.00", "28.00", 15),
    ("Vapes", "Ember Labs", "Ember Labs", "Battery 510", "4.00", "15.00", 8),
    ("Edibles", "Honeybee Kitchen", "Honeybee Kitchen", "Gummies 100mg", "6.00", "20.00", 24),
    ("Edibles", "Honeybee Kitchen", "Honeybee Kitchen", "Chocolate Bar 100mg", "7.00", "22.00", 12),
    ("Edibles", "Honeybee Kitchen", "Honeybee Kitchen", "Sour Belts 100mg", "6.50", "20.00", 10),
    ("Edibles", "Moonrise", "Moonrise Wholesale", "Sleep Gummies 100mg", "7.50", "25.00", 14),
    ("Edibles", "Moonrise", "Moonrise Wholesale", "THC Mints 50mg", "4.00", "14.00", 9),
    ("Edibles", "Moonrise", "Moonrise Wholesale", "Beverage 10mg", "3.00", "8.00", 11),
    ("Edibles", "Moonrise", "Moonrise Wholesale", "Cookies 100mg", "6.00", "18.00", 5),
    ("Concentrates", "Ember Labs", "Ember Labs", "Live Rosin 1g", "32.00", "75.00", 4),
    ("Concentrates", "Ember Labs", "Ember Labs", "Badder 1g", "18.00", "45.00", 6),
    ("Concentrates", "Cloudline", "Cloudline Distribution", "Shatter 1g", "12.00", "30.00", 7),
    ("Concentrates", "Cloudline", "Cloudline Distribution", "Diamonds 1g", "22.00", "55.00", 3),
    ("Tinctures", "Moonrise", "Moonrise Wholesale", "Tincture 1000mg", "18.00", "50.00", 3),
    ("Tinctures", "Moonrise", "Moonrise Wholesale", "CBD:THC Tincture 1:1", "16.00", "45.00", 2),
    ("Topicals", "Honeybee Kitchen", "Honeybee Kitchen", "Relief Balm 200mg", "12.00", "35.00", 3),
    ("Topicals", "Honeybee Kitchen", "Honeybee Kitchen", "Transdermal Patch", "9.00", "24.00", 2),
    ("Topicals", "Honeybee Kitchen", "Honeybee Kitchen", "Bath Soak 100mg", "8.00", "22.00", 0),  # dead SKU
    ("Accessories", "House", "Glassworks Supply", "Glass Pipe", "5.00", "20.00", 4),
    ("Accessories", "House", "Glassworks Supply", "Rolling Papers", "0.60", "3.00", 15),
    ("Accessories", "House", "Glassworks Supply", "Grinder", "6.00", "22.00", 3),
    ("Accessories", "House", "Glassworks Supply", "Lighter", "0.40", "2.00", 25),
    ("Accessories", "House", "Glassworks Supply", "Dab Tool", "2.50", "12.00", 1),
]

EMPLOYEES = ["E101", "E102", "E103", "E104", "E105", "E106"]
STORE = "MAIN"
DOW_MULT = {0: 0.85, 1: 0.8, 2: 0.9, 3: 1.0, 4: 1.35, 5: 1.4, 6: 1.05}
HOUR_WEIGHTS = {h: w for h, w in zip(range(9, 21), [3, 5, 7, 9, 10, 9, 8, 9, 11, 12, 9, 5])}

# ---- planted patterns ----
def promotions_for(end: date) -> list[dict]:
    """Two promotions in the last ~5 weeks: a margin-killer and a healthy one."""
    return [
        {
            "name": "Flower Friday 30% Off",
            "start_date": end - timedelta(days=24),
            "end_date": end - timedelta(days=18),
            "discount_type": "percent",
            "discount_value": "30",
            "eligible_skus": "",
            "eligible_category": "Flower",
            "_lift": 1.25,  # modest extra volume: not enough to pay for 30% off
        },
        {
            "name": "Gummies BOGO 50",
            "start_date": end - timedelta(days=10),
            "end_date": end - timedelta(days=4),
            "discount_type": "percent",
            "discount_value": "25",
            "eligible_skus": "",  # filled after SKUs are assigned
            "eligible_category": "",
            "_lift": 2.4,  # strong volume response: pays for 25% off
        },
    ]


def build_products() -> list[dict]:
    products = []
    counters: dict[str, int] = {}
    for cat, brand, vendor, name, cost, price, base in CATALOG:
        prefix = cat[:3].upper()
        counters[prefix] = counters.get(prefix, 0) + 1
        products.append(
            {
                "sku": f"{prefix}-{counters[prefix]:03d}",
                "name": name,
                "category": cat,
                "brand": brand,
                "vendor": vendor,
                "unit_cost": cost,
                "retail_price": price,
                "_base": base,
            }
        )
    return products


STORE_LAT, STORE_LON = 28.5383, -81.3792  # Orlando, FL


def external_scenario(start: date, end: date) -> tuple[dict[date, dict], list[dict]]:
    """Per-day planted conditions and the external_events rows that describe them.

    day_effects[d] = {"mult": traffic multiplier, "blocked": {hours with no sales}, "wx": weather tag}
    """
    fx: dict[date, dict] = {}

    def day(d: date, mult: float = 1.0, blocked: set[int] | None = None, wx: str | None = None):
        cur = fx.setdefault(d, {"mult": 1.0, "blocked": set(), "wx": None})
        cur["mult"] *= mult
        cur["blocked"] |= blocked or set()
        if wx:
            cur["wx"] = wx

    events: list[dict] = []

    def ev(event_id, etype, s_dt, e_dt, severity, desc, store="MAIN", lat="", lon="", radius="", conf="1", is_forecast=0, source="manual"):
        events.append({"event_id": event_id, "store": store, "event_type": etype, "source": source, "latitude": lat, "longitude": lon,
                       "affected_radius_km": radius, "start_time": s_dt.isoformat(timespec="seconds"),
                       "end_time": e_dt.isoformat(timespec="seconds"), "severity": severity, "description": desc,
                       "confidence": conf, "source_reference": "", "is_forecast": is_forecast})

    # Heavy rain: three Wednesdays, afternoons lose half the traffic.
    for i, off in enumerate((59, 38, 17)):
        d = end - timedelta(days=off)
        day(d, mult=0.72, wx="heavy_rain")
    # Tropical storm: surge the day before, collapse on the day, soft recovery after.
    storm = end - timedelta(days=30)
    day(storm - timedelta(days=1), mult=1.22, wx="pre_storm")
    day(storm, mult=0.35, blocked={16, 17, 18, 19, 20}, wx="storm")
    day(storm + timedelta(days=1), mult=0.9, wx="rain")
    ev("WX-TS-1", "weather", datetime.combine(storm, datetime.min.time()).replace(hour=6),
       datetime.combine(storm, datetime.min.time()).replace(hour=23), "severe", "Tropical Storm Warning, 4.1in rain, 55mph gusts", source="nws")
    # Heat wave: four days, small dip.
    for off in range(52, 48, -1):
        day(end - timedelta(days=off), mult=0.93, wx="heat")
    # Outages: power (3h) x2 and internet (2h) x1. Registers dark.
    for i, (off, hours, etype, desc) in enumerate((
        (48, {14, 15, 16}, "utility", "Power outage, feeder fault"),
        (27, {11, 12}, "connectivity", "ISP outage, POS offline"),
        (6, {15, 16, 17}, "utility", "Power outage, transformer"),
    ), start=1):
        d = end - timedelta(days=off)
        day(d, blocked=hours)
        h0, h1 = min(hours), max(hours)
        ev(f"OUT-{'POWER' if etype == 'utility' else 'NET'}-{i}", etype,
           datetime(d.year, d.month, d.day, h0, 5), datetime(d.year, d.month, d.day, h1, 50), "major", desc,
           source="utility-co" if etype == "utility" else "isp")
    # Road closure on the main access route: located 0.8 km away, radius 3 km, all day.
    closure = end - timedelta(days=13)
    day(closure, mult=0.78)
    ev("TR-CLOSE-1", "traffic", datetime(closure.year, closure.month, closure.day, 7), datetime(closure.year, closure.month, closure.day, 19),
       "major", "Road closure, water main repair on Main St", store="", lat="28.545", lon="-81.383", radius="3", source="dot")
    # Concert 6 km away: evening lift. Also a far-away one (Tampa) that must NOT match.
    concert = end - timedelta(days=21)
    day(concert, mult=1.28)
    ev("EVT-CONCERT-1", "local_event", datetime(concert.year, concert.month, concert.day, 16), datetime(concert.year, concert.month, concert.day, 23),
       "moderate", "Arena concert, 18k attendance", store="", lat="28.539", lon="-81.440", radius="15", source="ticketing")
    ev("EVT-FAR-1", "local_event", datetime(concert.year, concert.month, concert.day, 16), datetime(concert.year, concert.month, concert.day, 23),
       "moderate", "Tampa stadium game", store="", lat="27.976", lon="-82.503", radius="15", source="ticketing")
    # Holidays inside the range.
    for hol, name, mult in ((date(end.year, 7, 4), "Independence Day", 1.35), (date(2026, 9, 7), "Labor Day", 1.2)):
        if start <= hol <= end:
            day(hol, mult=mult)
            ev(f"CAL-{hol.isoformat()}", "calendar", datetime.combine(hol, datetime.min.time()), datetime.combine(hol, datetime.max.time()).replace(microsecond=0),
               "moderate", name, source="calendar")
    # Competitor opening 4 km away, three weeks before the end: mild ongoing dip.
    comp = end - timedelta(days=22)
    ev("COMP-OPEN-1", "competition", datetime.combine(comp, datetime.min.time()), datetime.combine(comp, datetime.max.time()).replace(microsecond=0),
       "moderate", "Competitor dispensary grand opening", store="", lat="28.56", lon="-81.40", radius="10", conf="0.9", source="news")
    for off in range(21, -1, -1):
        day(end - timedelta(days=off), mult=0.96)
    # Forecast / scheduled: concert next Saturday, storm forecast next Wednesday.
    nxt_sat = end + timedelta(days=(5 - end.weekday()) % 7 or 7)
    ev("EVT-CONCERT-NEXT", "local_event", datetime(nxt_sat.year, nxt_sat.month, nxt_sat.day, 17), datetime(nxt_sat.year, nxt_sat.month, nxt_sat.day, 23),
       "moderate", "Arena concert (scheduled)", store="", lat="28.539", lon="-81.440", radius="15", is_forecast=1, source="ticketing")
    return fx, events


def weather_rows(start: date, end: date, fx: dict[date, dict], rng: random.Random) -> list[dict]:
    """Hourly observations for the history plus a 7-day hourly forecast."""
    rows = []

    def hour_row(d: date, h: int, tag: str | None, is_forecast: int):
        base_t = 84 + 8 * (0.5 - abs(h - 15) / 15)  # warm Florida day curve
        temp, precip, wind, cond, alert = base_t + rng.uniform(-2, 2), 0.0, rng.uniform(3, 9), "clear", ""
        if tag == "heavy_rain":
            precip = rng.uniform(0.05, 0.15) if 11 <= h <= 19 else 0.0
            cond = "rain" if precip else "cloudy"
            temp -= 6
        elif tag == "rain":
            precip = rng.uniform(0.01, 0.04) if 12 <= h <= 17 else 0.0
            cond = "rain" if precip else "cloudy"
        elif tag == "storm":
            precip = rng.uniform(0.15, 0.35) if 6 <= h <= 22 else 0.02
            wind = rng.uniform(35, 55)
            cond = "storm"
            alert = "Tropical Storm Warning"
            temp -= 9
        elif tag == "pre_storm":
            cond = "cloudy"
            wind = rng.uniform(15, 25)
            alert = "Tropical Storm Watch"
        elif tag == "heat":
            temp += 12
        elif rng.random() < 0.12:
            cond = "cloudy"
        return {"store": STORE, "observed_at": datetime(d.year, d.month, d.day, h).isoformat(timespec="seconds"),
                "is_forecast": is_forecast, "temperature_f": f"{temp:.1f}", "precipitation_in": f"{precip:.3f}",
                "snowfall_in": "0", "wind_mph": f"{wind:.1f}", "condition": cond, "alert": alert, "source": "nws"}

    d = start
    while d <= end:
        for h in range(24):
            rows.append(hour_row(d, h, fx.get(d, {}).get("wx"), 0))
        d += timedelta(days=1)
    forecast_tags = {end + timedelta(days=i): None for i in range(1, 8)}
    forecast_tags[end + timedelta(days=5)] = "heavy_rain"
    for d, tag in forecast_tags.items():
        for h in range(24):
            rows.append(hour_row(d, h, tag, 1))
    return rows


def generate(out: Path, end: date, days: int, seed: int) -> None:
    rng = random.Random(seed)
    start = end - timedelta(days=days - 1)
    products = build_products()
    by_sku = {p["sku"]: p for p in products}
    promos = promotions_for(end)
    gummy_skus = [p["sku"] for p in products if "Gummies" in p["name"]]
    promos[1]["eligible_skus"] = "|".join(gummy_skus)

    def active_promo(d: date, product: dict) -> dict | None:
        for pr in promos:
            if not (pr["start_date"] <= d <= pr["end_date"]):
                continue
            if pr["eligible_category"] and product["category"] == pr["eligible_category"]:
                return pr
            if pr["eligible_skus"] and product["sku"] in pr["eligible_skus"].split("|"):
                return pr
        return None

    day_fx, external_events = external_scenario(start, end)

    # Vendor cost increase: Cloudline raises cost 12% for the last 3 weeks.
    cost_bump_from = end - timedelta(days=20)

    def unit_cost_on(d: date, product: dict) -> Decimal:
        cost = Decimal(product["unit_cost"])
        if product["vendor"] == "Cloudline Distribution" and d >= cost_bump_from:
            cost = (cost * Decimal("1.12")).quantize(Decimal("0.0001"))
        return cost

    sales, items, customers = [], [], [f"C{n:04d}" for n in range(1, 401)]
    txn_counter = 0
    sold_units: dict[str, int] = {p["sku"]: 0 for p in products}
    last_sale: dict[str, date] = {}

    for day_offset in range(days):
        d = start + timedelta(days=day_offset)
        dow_mult = DOW_MULT[d.weekday()]
        # gentle growth over time + weekly seasonality
        trend = 1.0 + 0.15 * day_offset / max(days - 1, 1)
        promo_traffic = 1.1 if any(pr["start_date"] <= d <= pr["end_date"] for pr in promos) else 1.0
        fx = day_fx.get(d, {"mult": 1.0, "blocked": set()})
        n_tickets = max(12, int(rng.gauss(95 * dow_mult * trend * promo_traffic * fx["mult"], 9)))
        open_hours = [h for h in HOUR_WEIGHTS if h not in fx["blocked"]]
        open_weights = [HOUR_WEIGHTS[h] for h in open_hours]
        n_tickets = int(n_tickets * sum(open_weights) / sum(HOUR_WEIGHTS.values()))

        for _ in range(n_tickets):
            txn_counter += 1
            tid = f"T{txn_counter:07d}"
            hour = rng.choices(open_hours, weights=open_weights)[0]
            sold_at = datetime(d.year, d.month, d.day, hour, rng.randrange(60), rng.randrange(60))
            r = rng.random()
            status = "completed" if r < 0.975 else ("refunded" if r < 0.99 else "voided")
            employee = rng.choice(EMPLOYEES)
            customer = rng.choice(customers) if rng.random() < 0.7 else ""
            sales.append(
                {"transaction_id": tid, "store": STORE, "sold_at": sold_at.isoformat(timespec="seconds"),
                 "employee": employee, "customer": customer, "status": status}
            )

            n_lines = rng.choices([1, 2, 3, 4, 5], weights=[42, 30, 16, 8, 4])[0]
            weights = [max(p["_base"], 0) * ((active_promo(d, p) or {}).get("_lift", 1.0)) for p in products]
            chosen = set()
            for line_no in range(1, n_lines + 1):
                p = rng.choices(products, weights=weights)[0]
                if p["sku"] in chosen:
                    continue
                chosen.add(p["sku"])
                qty = rng.choices([1, 2, 3], weights=[78, 17, 5])[0]
                regular = Decimal(p["retail_price"])
                pr = active_promo(d, p)
                promo_name = ""
                if pr:
                    promo_name = pr["name"]
                    sale_price = money(regular * (1 - Decimal(pr["discount_value"]) / 100))
                elif employee == "E104" and rng.random() < 0.35:
                    # Employee discount anomaly: E104 hands out untracked 20% discounts.
                    sale_price = money(regular * Decimal("0.80"))
                elif rng.random() < 0.08:
                    sale_price = money(regular * Decimal("0.90"))  # loyalty 10%
                else:
                    sale_price = regular
                items.append(
                    {"transaction_id": tid, "line_no": line_no, "sku": p["sku"], "quantity": qty,
                     "regular_price": f"{regular:.2f}", "sale_price": f"{sale_price:.2f}",
                     "unit_cost": f"{unit_cost_on(d, p):.4f}", "promotion": promo_name}
                )
                if status == "completed":
                    sold_units[p["sku"]] += qty
                    last_sale[p["sku"]] = d

    # Inventory snapshot as of `end`.
    inventory = []
    for p in products:
        base = p["_base"]
        if base == 0:
            qoh, received = 48, end - timedelta(days=140)  # dead stock
        elif p["name"] in ("GMO Cookies 28g", "Dab Tool", "Purple Punch 14g"):
            qoh, received = int(base * 45) + 20, end - timedelta(days=rng.randint(95, 130))  # slow / aging
        elif p["name"] in ("Blue Dream 3.5g", "Gummies 100mg"):
            qoh, received = int(base * 2), end - timedelta(days=rng.randint(3, 8))  # likely stockout
        else:
            qoh, received = int(base * rng.uniform(12, 35)) + 5, end - timedelta(days=rng.randint(5, 60))
        inventory.append(
            {"store": STORE, "sku": p["sku"], "snapshot_date": end.isoformat(), "quantity_on_hand": qoh,
             "unit_cost": f"{unit_cost_on(end, p):.4f}", "received_date": received.isoformat(),
             "last_sale_date": last_sale.get(p["sku"], "").isoformat() if last_sale.get(p["sku"]) else ""}
        )

    # Expenses: weekly rent/utilities/marketing, biweekly payroll with a spike.
    expenses, eid = [], 0
    d = start
    while d <= end:
        if d.weekday() == 0:  # Monday postings
            for cat, vendor, desc, amt in (
                ("Rent", "Harbor Properties", "Weekly rent accrual", "2650.00"),
                ("Utilities", "City Power", "Electric + water", rng.choice(["410.00", "455.00", "470.00"])),
                ("Marketing", "Weedmaps", "Listing + ads", "525.00"),
                ("Software", "POS Vendor", "POS subscription", "185.00"),
                ("Security", "SafeGuard", "Guard + monitoring", "900.00"),
                ("Supplies", "Glassworks Supply", "Bags, exit packaging", rng.choice(["140.00", "165.00", "210.00"])),
            ):
                eid += 1
                expenses.append({"expense_id": f"X{eid:05d}", "store": STORE, "expense_date": d.isoformat(),
                                 "category": cat, "vendor": vendor, "description": desc, "amount": amt})
        if d.weekday() == 4 and ((d - start).days // 7) % 2 == 1:  # biweekly Friday payroll
            eid += 1
            payroll = Decimal("11800.00")
            if end - timedelta(days=13) <= d <= end:  # overtime spike in the final pay period
                payroll = Decimal("14950.00")
            expenses.append({"expense_id": f"X{eid:05d}", "store": STORE, "expense_date": d.isoformat(),
                             "category": "Payroll", "vendor": "Gusto", "description": "Biweekly payroll", "amount": f"{payroll:.2f}"})
        d += timedelta(days=1)

    out.mkdir(parents=True, exist_ok=True)
    _write(out / "products.csv", [{k: v for k, v in p.items() if not k.startswith("_")} for p in products])
    _write(out / "promotions.csv", [{**{k: v for k, v in pr.items() if not k.startswith("_")},
                                      "start_date": pr["start_date"].isoformat(), "end_date": pr["end_date"].isoformat()} for pr in promos])
    _write(out / "sales.csv", sales)
    _write(out / "sale_items.csv", items)
    _write(out / "inventory.csv", inventory)
    _write(out / "expenses.csv", expenses)
    _write(out / "stores.csv", [{"code": STORE, "name": "Aurora Main St", "latitude": STORE_LAT, "longitude": STORE_LON, "timezone": "America/New_York"}])
    _write(out / "external_events.csv", external_events)
    weather = weather_rows(start, end, day_fx, rng)
    _write(out / "weather.csv", weather)
    print(f"wrote {len(products)} products, {len(sales)} sales, {len(items)} sale_items, "
          f"{len(inventory)} inventory rows, {len(expenses)} expenses, {len(promos)} promotions, "
          f"{len(external_events)} external events, {len(weather)} weather rows -> {out}")


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../sample_data")
    ap.add_argument("--end", default="2026-09-11")
    ap.add_argument("--days", type=int, default=91)
    ap.add_argument("--seed", type=int, default=13)
    a = ap.parse_args()
    generate(Path(a.out), date.fromisoformat(a.end), a.days, a.seed)
