# Aurora AI — Profitability Intelligence Platform

A template for a read-only business intelligence application that sits above
the POS, the accounting system and the inventory system of **any retail or
service business** — and answers one question every week:

> Where are we making money, where are we losing money, why is it happening,
> and what should we change next?

**Core rule: code calculates the money.** Every figure Aurora shows is a
deterministic, tested server-side calculation. No AI is wired in yet — V1 exists
to get the numbers right first (see the build plan in the two source PDFs).

## Use this as a template

**Keep your data out of this repository.** The template is public. Your
instance runs locally (or in a private clone): POS exports, recorded Headset
pulls, `.env` files and the SQLite/Postgres database all live under paths that
`.gitignore` already excludes (`data/`, `backend/data/`, `backend/*.db`,
`backend/.env`). Only generic code belongs upstream.

Nothing in the engine is industry-specific. It needs sales, sale line items,
products, an inventory snapshot, expenses, and (optionally) promotions,
external events and weather. The bundled sample dataset happens to be a
cannabis dispensary; swap it for a coffee shop, a bike store or a clinic and
nothing else changes.

1. Click **Use this template** on GitHub, clone your copy.
2. Export your own CSVs in the formats under *CSV formats* below (or adapt the
   importers in `backend/app/importers/csv_importer.py` to your POS export).
3. Tune the named thresholds at the top of `backend/app/analytics/inventory.py`
   (slow/hot sell-through) and `external.py` (materiality, minimum observations,
   default event radii) to your category economics.
4. Run the tests, import, open the dashboard.

Cannabis retailers on **Headset** skip the CSV step: see *Connecting Headset*
below. The connector is generic to any Headset retailer account.

## What is in V1

| Layer | Technology | What it does |
|---|---|---|
| Backend | Python 3.11, FastAPI, SQLAlchemy 2 | CSV imports, metrics engine, JSON API |
| Database | PostgreSQL (SQLite for zero-setup dev/tests) | Normalized historical business data |
| Analytics | Pure Python `Decimal` | Financial, product, inventory, promotion, external |
| Frontend | Next.js 15, React 19, TypeScript, Recharts | Owner dashboard |

### Internal intelligence (`app/analytics/`)

- **financial.py** — revenue, COGS, gross profit, gross margin, discount rate,
  transactions, average ticket, units per ticket, refunds, voids, operating
  expenses, operating profit.
- **comparisons.py** — current week vs previous week vs four-week average. A
  partial week is compared **week-to-date** against the same weekdays of prior
  weeks so Friday never reads as a 35% collapse.
- **products.py** — product and category profitability.
- **inventory.py** — inventory value, aging buckets, cash tied up over 90 days,
  30-day sell-through, days of supply, hot/normal/slow/dead classification.
- **promotions.py** — deal autopsy: every promotion vs the 28 days before it,
  per day, with a verdict (`profitable`, `revenue_up_profit_down`,
  `unprofitable`, `no_baseline`).
- **discounts.py** — discount / promo-code report from the aggregate feed: what
  each code cost, how deep it cut on the items it touched, its share of all
  discounting, and the change against the prior period.

### External intelligence (`app/analytics/external.py`)

- `external_events` table (weather, traffic, connectivity, utility, local
  events, calendar, competition, economic, demand) and hourly `weather_observations`.
- Store geolocation; events match a store explicitly or by haversine distance
  inside their affected radius.
- **Expected vs actual** for any interval: mean of the same weekday+hour over the
  previous four weeks, excluding hours inside other major/severe events,
  adverse-weather days, and weeks with no data.
- **Historical effects**: mean variance per event type once there are ≥3
  observations moving the same direction ≥75% of the time.
- **Evidence levels** — never promoted automatically: `no_material_variance`
  (<10%), `correlation`, `historical_relationship`, `likely_contributor`
  (history + location + severity + no promotion in the window).
- **Weather learning** per store per condition (rain, heavy rain, storm, heat,
  cold, wind, alert, pre-storm) and a **7-day proactive forecast** that only
  prices in effects the store has actually shown.
- **Resilience value**: annualised revenue lost to outages, to weigh against
  backup power / cellular failover.

Every rule and threshold is a named constant at the top of the module and is
echoed back in the API response under `rules`.

## Connecting Headset

Headset's MCP connector reports **aggregates**, not receipts: totals by store
and day, by product for a window, by discount code, and per-SKU on-hand
inventory. Aurora maps them without losing a cent:

| Headset call | Aurora table | Notes |
|---|---|---|
| `retailer_get_stores` | `stores` | code `HS<storeId>`, timezone from state (FL panhandle → Central) |
| `retailer_get_inventory` per store | `products`, `inventory_snapshots` | unit cost = on-hand cost value ÷ units; category / brand / vendor learned here |
| `retailer_sales_by_dimension` product, one store, one day | `sales` + `sale_items` | one synthetic sale per (store, day, SKU), `source="headset"`, carrying the **exact** line totals and the ticket count |
| `retailer_sales_by_dimension` discount_name, one store, one day | `discount_daily` | feeds the discount-code report |
| `retailer_sales_trend` day × store | `daily_store_summaries` | the day's true totals and ticket count; the reconciliation target |

Because a receipt with two products appears under both, ticket counts at
period level come from `daily_store_summaries`; at product level they are
Headset's own per-product counts. Every other figure (revenue, gross, COGS,
discounts, units) is the feed's number, stored as a line total rather than
`quantity × rounded unit price`.

### Pull

Set `HEADSET_MCP_URL` and `HEADSET_MCP_TOKEN` in `backend/.env` (the remote
MCP endpoint and bearer token from your Headset / claude.ai connector), then:

```bash
python -m app.cli headset-sync --start 2026-09-01 --end 2026-09-14 --stores "FL -"
```

One call per store per day for products and for discount codes, plus stores,
inventory and the store-day trend, so 35 stores × 14 days ≈ 1,000 calls.
Re-running a range is idempotent. Every raw response is recorded under
`HEADSET_DATA_DIR` (default `backend/data/headset`, git-ignored) as a JSON
*envelope* — `{"kind", "store_name", "sold_date", "result"}` — and can be
replayed without touching Headset again:

```bash
python -m app.cli headset-import-dir data/headset
python -m app.cli headset-reconcile          # product lines vs store-day totals
python -m app.cli discounts --as-of 2026-09-15 --store HS10136
```

Envelopes can also come from any other MCP client (Claude, a script): save the
tool result inside the envelope shape and `POST /api/import/headset` it. The
`McpHeadsetClient` speaks MCP streamable HTTP per the spec; the envelope path
is what the test suite verifies end to end.

### Reconciliation

`headset-reconcile` compares the sum of product lines against Headset's own
store-day totals for every day the feed reported. `ok` ties to the cent,
`mismatch` means a partial page (`hasMore`) or a restated day, `missing` means
the feed reported the day but no product detail has been pulled yet (period
revenue understated, ticket counts complete). The sync prints the same report.

Limits of an aggregate feed: no hourly grain (the external-events engine needs
POS-level timestamps, so it stays on the CSV path), no received dates (aging
reads *unknown*), no operating expenses (bring those through `expenses.csv`),
and category / brand / vendor for a SKU come from inventory, so run one sync
with `--full-catalog` first or a SKU that sold before it was ever in stock reads
*Uncategorized* until it appears.

## Free external-event stack

Everything the external engine needs, from free sources, in one command:

```bash
python -m app.cli geocode-stores                              # once: coordinates from addresses (Nominatim)
python -m app.cli events-sync --start 2026-09-01 --end 2026-10-15
```

| Source | Event type | Key | What it gives |
|---|---|---|---|
| Ticketmaster Discovery API | `local_event` | free, 5,000 calls/day | concerts, pro sports, family shows within `EVENTS_RADIUS_KM` of each store; stadiums / arenas and sports are *major* |
| SeatGeek Platform API | `local_event` | free | second ticket source; an event at the same venue on the same day as a Ticketmaster one is dropped |
| FL511 (FDOT) | `traffic` | free | crashes, closures, roadwork within `TRAFFIC_RADIUS_KM`; full closures are *major*; roadwork longer than 14 days is skipped |
| `holidays` package | `calendar` | none | federal + state holidays (`HOLIDAY_COUNTRY` / `HOLIDAY_SUBDIVISION`) |
| Cannabis calendar | `calendar` | none | 4/20 and Green Wednesday (*major*), 7/10, Black Friday, New Year's Eve; edit `CANNABIS_CALENDAR` in `integrations/events/calendar.py` |
| Store heartbeat | `utility`, `connectivity` | your device | a pinger at each store; silences become outage events (below) |
| Open-Meteo + NWS | `weather_observations`, `weather` events | none | hourly history and 16-day forecast per store; NWS alerts (below) |

A provider runs only when its key is set, so `events-sync` with no keys still
writes the calendar. Re-running a range is idempotent. Events dated after today
are stored as forecasts and feed the 7-day projection; past ones are matched
against actuals by the evidence engine.

School calendars vary by county and are not machine-readable: put them in
`external_events.csv` with `event_type=calendar` (one row per break, per store
or with a county-sized radius) and import as usual.

FL511's endpoint follows the 511 platform several states share
(`/api/v2/get/event?key=&format=json`); register at fl511.com/developers. The
client is written from the published platform docs and the test suite exercises
it against recorded rows, not the live feed.

### Weather (Open-Meteo + National Weather Service, no keys)

```bash
python -m app.cli weather-sync --start 2025-01-01 --end 2026-10-01      # backfill, then nightly for the last few days
```

Open-Meteo supplies hourly temperature, precipitation, snowfall, wind and a
WMO weather code per store, already in °F / inches / mph: the archive endpoint
(ERA5, from 1940, about five days behind) for the old part of the range and the
forecast endpoint (92 days back, 16 ahead) for the recent part. Hours after the
sync time are stored as forecasts; the next sync writes the observation as its
own row, so forecast history is kept and the engine reads observations only.
NWS alerts for each store's point (tropical storm warnings, heat advisories,
flood watches) stamp the hours they cover, which the engine turns into an
*alert* day tag, and become `weather` events explicit to the store with NWS
severity (Extreme → severe, Severe → major). The same `GEOCODER_USER_AGENT`
identifies you to NWS. Open-Meteo is free for non-commercial use up to 10,000
calls a day and asks for attribution.

### Store heartbeats (power and internet outages, to the minute)

Utility outage maps say a county had trouble; a $30 device in the back office
says *this store* lost power at 2:17pm. Point anything that can make an HTTP
request (a Raspberry Pi, a smart plug with a webhook, a cron job on the POS
machine) at:

```
POST /api/heartbeat?store=HS10136&kind=power&token=<HEARTBEAT_TOKEN>      every minute
POST /api/heartbeat?store=HS10136&kind=network&token=<HEARTBEAT_TOKEN>
```

Pings extend a `heartbeat_runs` row (one row per contiguous run, not per ping).
Then, nightly or on demand:

```bash
python -m app.cli heartbeat-events            # silences >= HEARTBEAT_GAP_MINUTES -> utility / connectivity events
python -m app.cli heartbeat-status            # last seen per store and kind
```

Severity is by duration: under 15 min *minor*, under an hour *moderate*, under
four hours *major*, else *severe*. A device that was unplugged looks exactly like
an outage, so treat a lone finding with suspicion; the resilience value on the
dashboard is what these events are for.

## Quick start

### Backend

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # macOS/Linux: .venv/bin/pip
```

Pick a database:

- **PostgreSQL (production target):** `docker compose up -d` from the repo root,
  then `cp .env.example .env` (the default `DATABASE_URL` matches the compose file).
- **SQLite (zero setup):** do nothing; `aurora.db` is created in `backend/`.

Generate the fake dispensary dataset, import it, and start the API:

```bash
.venv/Scripts/python scripts/generate_fake_data.py --out ../sample_data
.venv/Scripts/python -m app.cli import-dir ../sample_data
.venv/Scripts/uvicorn app.main:app --reload --port 8000
```

Interactive API docs: <http://localhost:8000/docs>

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:3000>. Set `NEXT_PUBLIC_API_URL` if the API is not on
`localhost:8000`.

### Tests

```bash
cd backend
.venv/Scripts/python -m pytest
```

74 tests. Every monetary expectation is worked out by hand in the test body.

If you upgrade an existing SQLite database from before the Headset connector,
delete `backend/aurora.db` and re-import: there are no migrations yet.

## CSV formats

Header names are exact; column order does not matter. Re-importing a file
updates rows in place (keyed on `sku`, `transaction_id`, `expense_id`,
promotion `name`, `event_id`, ...). Row-level problems are reported and skipped;
a missing required column fails the file.

| File | Columns |
|---|---|
| `stores.csv` | code, name, latitude, longitude, timezone, address (optional; used by `geocode-stores`) |
| `products.csv` | sku, name, category, brand, vendor, unit_cost, retail_price |
| `sales.csv` | transaction_id, store, sold_at, employee, customer, status (`completed`/`refunded`/`voided`) |
| `sale_items.csv` | transaction_id, line_no, sku, quantity, regular_price, sale_price, unit_cost, promotion |
| `inventory.csv` | store, sku, snapshot_date, quantity_on_hand, unit_cost, received_date, last_sale_date |
| `expenses.csv` | expense_id, store, expense_date, category, vendor, description, amount |
| `promotions.csv` | name, start_date, end_date, discount_type (`percent`/`amount`/`bogo`), discount_value, eligible_skus (pipe-separated), eligible_category |
| `external_events.csv` | event_id, store, event_type, source, latitude, longitude, affected_radius_km, start_time, end_time, severity, description, confidence, source_reference, is_forecast |
| `weather.csv` | store, observed_at, is_forecast, temperature_f, precipitation_in, snowfall_in, wind_mph, condition, alert, source |

Prices in `sale_items` are **per unit**; `discount_amount` is derived as
`(regular_price − sale_price) × quantity`.

## API

| Endpoint | Returns |
|---|---|
| `POST /api/import/{kind}` | Upload one CSV (`kind` = any file name above without `.csv`) |
| `GET /api/dashboard?as_of=&store=` | Everything the home screen needs |
| `GET /api/metrics/weekly` | Current vs previous vs four-week average with deltas |
| `GET /api/metrics/weekly-trend?weeks=12` | One summary per week |
| `GET /api/metrics/summary?start=&end=` | Financial summary for any period |
| `GET /api/metrics/products`, `/categories` | Profitability breakdowns |
| `GET /api/metrics/inventory` | Position, aging, sell-through, classification |
| `GET /api/metrics/promotions` | Deal autopsies |
| `GET /api/metrics/discounts?start=&end=&store=` | Discount-code report (aggregate feed) |
| `POST /api/import/headset` | Upload one recorded Headset envelope |
| `GET /api/headset/reconcile?store=` | Product lines vs store-day totals |
| `POST /api/heartbeat?store=&kind=&token=` | Store device ping (power / network) |
| `GET /api/heartbeat/status` | Last ping per store and kind |
| `GET /api/external/events` | Matched events with expected vs actual, evidence, impact |
| `GET /api/external/weather` | Learned weather effects and daily variance |
| `GET /api/external/forecast?days=7` | Proactive projection |

`as_of` defaults to the latest sale date in the database.

## The sample dataset

`scripts/generate_fake_data.py` builds a fictional cannabis dispensary (one
store, 42 SKUs, 13 weeks). It is seeded and regenerates identically. It plants
patterns the analytics must surface: a dead SKU, two SKUs about to stock out,
slow-moving high-value flower, a 12% vendor cost increase, an untracked employee
discount, a payroll spike, a Flower promotion that gives away more margin than
it earns and a Gummies promotion that pays for itself, three outages, a tropical
storm with a pre-storm surge, three heavy-rain days, a heat wave, a road closure,
a concert, two holidays, a competitor opening, and a 7-day forecast.

## Roadmap (from the build plan)

| Version | Scope |
|---|---|
| **V1** | CSV imports, Postgres schema, deterministic analytics, dashboard, external events + weather + baseline + evidence |
| **V1.5 (this)** | Headset connector (sync, recorded envelopes, exact aggregate totals, reconciliation, discount-code report); free event stack (Ticketmaster, SeatGeek, FL511, holiday + cannabis calendar, store heartbeats, Nominatim geocoding); weather from Open-Meteo + NWS alerts |
| V2 | Claude AI analyst with read-only tools over these endpoints; recommendation engine; evidence-based answers |
| V3 | Automatic QuickBooks and Dutchie POS synchronization; scheduled nightly Headset / weather / events sync |
| V4 | Scheduled weekly owner report, forecasting, vendor intelligence |
| Later | Metrc integration only when there is a clear operational need |
