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

38 tests. Every monetary expectation is worked out by hand in the test body.

## CSV formats

Header names are exact; column order does not matter. Re-importing a file
updates rows in place (keyed on `sku`, `transaction_id`, `expense_id`,
promotion `name`, `event_id`, ...). Row-level problems are reported and skipped;
a missing required column fails the file.

| File | Columns |
|---|---|
| `stores.csv` | code, name, latitude, longitude, timezone |
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
| **V1 (this)** | CSV imports, Postgres schema, deterministic analytics, dashboard, external events + weather + baseline + evidence |
| V2 | Claude AI analyst with read-only tools over these endpoints; recommendation engine; evidence-based answers |
| V3 | Automatic QuickBooks and POS synchronization; live weather / traffic / outage feeds |
| V4 | Scheduled weekly owner report, forecasting, vendor intelligence |
| Later | Metrc integration only when there is a clear operational need |
