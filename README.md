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
| FDOT public incident layer (FL511) | `traffic` | none | the live FL511 incident list from FDOT's public ArcGIS service: crashes, closures, construction within `TRAFFIC_RADIUS_KM`; "all lanes blocked" and road closures are *major*; the default traffic source |
| FL511 keyed feed | `traffic` | key issued by FDOT on request | the same platform feed with planned end dates; used instead of the public layer when `FL511_API_KEY` is set |
| Road511 | `traffic` | paid after a 14-day trial | the same FL511 events with a tracked lifecycle (start, end, archived) and **history**: one radius query per store, active and archived; wins over both FL511 clients when `ROAD511_API_KEY` is set |
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

Traffic needs no key: FDOT publishes the FL511 incident list as a public
ArcGIS feature layer (`FL511_2026_feed_view`, layer `FL511_Unified_Incidents`)
and Aurora reads it directly, reprojected to WGS84, one page of 1,000 rows at
a time. It is the *current* list, so an incident's end is the last time a sync
saw it; a nightly sync captures what was open at sync time, and an hourly
`events-sync` in cron catches short incidents. The keyed feed
(`fl511.com/api/v2/get/event?key=`) exists on the same platform other states
document publicly, but Florida has no self-service sign-up: ask FDOT through
the FL511 feedback form (Comment type "Question") for developer API access,
and set `FL511_API_KEY` if they grant it. Both clients are tested against
recorded rows, not the live feeds.

Road511 (portal.road511.com) aggregates the same FL511 events and adds what
the free sources lack: `end_time` on cleared incidents and archived events
that can be queried back in time, which is what the evidence engine needs to
reach three comparable past events. Set `ROAD511_API_KEY` and it becomes the
traffic source; `ROAD511_HISTORY=true` (default) also pulls archived events
in the sync range. On the free trial that archived pull may be refused by a
plan gate; the sync reports the gate's code as a warning and keeps the active
events, so the trial tells you whether the history is worth the Starter plan.

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

### Spreadsheets: market report, competitor deals, promotions (no OAuth)

Three living spreadsheets feed Aurora directly from their share links; nothing
is exported by hand and Aurora never writes to them.

```bash
python -m app.cli sheets-sync                       # all three, whichever are configured
python -m app.cli sheets-headers "<share link>"      # see a sheet's headers before mapping columns
python -m app.cli market --as-of 2026-09-11
```

| Sheet | Setting | What Aurora takes | Where it lands |
|---|---|---|---|
| State weekly dispensing report (Florida OMMU), one row per week per operator | `MARKET_SHEET_ID` + tab | week, operator, dispensing locations, mg THC, flower oz, shares, patients | `market_weekly`; **market context** on the dashboard (market volume vs ours, share in bps, "outpaced / trailed the market by N points"); an operator whose location count rises writes a statewide `competition` event |
| Competitor deals library (observed deals, any source) | `DEALS_SHEET_ID` + tab | operator, date, offer, type, hook, audience, confidence | `competition` events with severity by depth (40%+ or BOGO → major, 20%+ → moderate) and a **promo pressure** table per operator, this period vs last |
| Promotions workbook (OneDrive / SharePoint) | `PROMOTIONS_URL` | name, start, end, discount, weekdays, stores, SKUs / category, **POS discount names** | `promotions` with recurrence (`weekdays`) and store scope; the POS names join each promotion to `discount_daily`, so the deal autopsy shows what the feed says it cost per scheduled day |

Google Sheets are read from the share link (set the sheet to *anyone with the
link can view*). Leave the tab setting blank and Aurora downloads the workbook
and picks the tab by its columns (`week_ending` + `mmtc_name_canonical` for the
market report, `deal_id` + `operator_canonical` for deals); or name the tab or
give its `gid`. A OneDrive / SharePoint *anyone with the link* URL is fetched
with `download=1`, no app registration; the promotions tab is found by a
`Promo` / `Promotion` / `Name` column unless `PROMOTIONS_SHEET` names it. Column headers are matched by name with aliases (`Promo`,
`Start Date`, `Days`, `Stores`, `POS Discount Name`…); an unusual layout gets
`PROMOTIONS_COLUMN_MAP`. Rows with no name or no start date are reported and
skipped; the same deal on several rows (one per store) is merged.

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

## Ask Aurora: the analyst

"Why was Pace down on Tuesday?" "Which stores lost the most gross profit and
did we move with the market?" The analyst is Claude (`claude-opus-5`, adaptive
thinking, effort `ANALYST_EFFORT`) with sixteen **read-only tools** that call
the same deterministic analytics as the dashboard: weekly summary and trend,
store ranking, categories, products, inventory, promotions, discount codes,
market, competitor deals, external findings, weather effects, forecast,
upcoming events, store list, and data coverage. It never computes money
itself; every figure in an answer came back from a tool, and the answer
carries the list of tools it looked at. Evidence levels are reported as
graded, never promoted. The system prompt is stable (cached); the day and
the default scope go in the question.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m app.cli ask "which stores lost the most gross profit this week and why" --scope state:FL
```

`POST /api/analyst {question, scope?, as_of?, history?}` powers the "Ask
Aurora" panel on the dashboard (conversation history is kept in the browser
and sent back, at most ten turns). Server-side refusal fallbacks are enabled;
a refusal, a cut-off answer, or a run that stops on tool calls is reported as
such rather than hidden. The route answers 503 until `ANTHROPIC_API_KEY` is
set on the server.

## Google Business Profile export

Business Profile Manager's locations export (the `Ungrouped_locations-*.csv`)
has no coordinates, but it has the clean street address, opening hours, the
opening date and the GMB store code. `python -m app.cli gmb-import
<file.csv>` matches rows to Headset stores by postal code (then by the
locality appearing in the store name; ambiguous rows are reported, never
guessed) and fills address, state, `gmb_code`, phone, `opened_on` and `hours`.
Run `geocode-stores` afterwards. Store rankings then carry `weeks_open` and an
`is_new` flag (under 26 weeks), so a new store's climb is read as a climb.

## State view, then the store

Every analytics call takes one scope: nothing (everything), a state
(`state:FL`) or a store code. The dashboard opens on `DEFAULT_SCOPE` (set it
to `state:FL`), offers "All FL stores" above the store list, and every store
option shows its state so a Nevada store never blends into the Florida read.
The weekly report at state scope carries the store ranking for the drill-down;
at store scope it carries that store's resilience and forecast instead.
Store state comes from the Headset address (or a `state` column in
`stores.csv`).

## The weekly owner report

Every Monday (or on demand) one page: the week versus last week and the
four-week average, the market read, every store ranked by gross-profit
change, the categories and products that moved, deal autopsies with what the
feed says each promotion cost, the discount codes that cost the most, cash
tied up in slow stock and stockout risk, external findings with their
evidence level, competitor pressure, and what next week's calendar and
weather hold. It ends with a data-coverage line so nobody reads a partial
week as a collapse.

```bash
python -m app.cli weekly-report --as-of 2026-09-14                 # text to the terminal
python -m app.cli weekly-report --out report.html                   # self-contained HTML (email-safe)
python -m app.cli weekly-report --store HS10136 --email             # one store, sent via SMTP
```

`GET /api/report/weekly?format=html` serves the same page (the dashboard's
"Weekly report" button). With `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` and
`REPORT_TO` set, the scheduler emails it after the nightly sync on
`REPORT_WEEKDAY` (1 = Monday). `REPORT_STORE` narrows it to one store.

## Schema changes

There is no migration tool yet and none is needed so far: start-up creates
missing tables and adds missing columns (`ALTER TABLE … ADD COLUMN`, printed
as `[schema] …` by the CLI), which is every schema change Aurora has made.
A column removal or type change would need a hand migration.

## Go-live checklist

What Aurora needs from outside the repository, in the order it pays off:

| # | Item | Where it goes | Without it |
|---|---|---|---|
| 1 | Headset MCP endpoint URL + token, callable from the server | `HEADSET_MCP_URL`, `HEADSET_MCP_TOKEN` | no nightly sales; replay recorded pulls with `headset-import-dir` |
| 2 | `DEFAULT_SCOPE=state:FL` and a Headset store filter of `FL -` on the first backfill | `backend/.env` | the state view blends other states |
| 3 | Google Sheets shared "anyone with the link", SharePoint link "anyone with the link" | `MARKET_SHEET_ID`, `DEALS_SHEET_ID`, `PROMOTIONS_URL` | no market read, no competitor pressure, promotions by CSV only |
| 4 | A POS discount name column in the promotions workbook | the workbook | promotions import but cannot be measured against the feed |
| 5 | Store coordinates: `gmb-import` the Business Profile export (addresses, hours, opening dates), then `geocode-stores` | database | no local events, traffic or weather per store |
| 6 | Free keys: Ticketmaster, SeatGeek (FL511 traffic needs none) | `backend/.env` | calendar and traffic only |
| 7 | SMTP credentials and recipients | `SMTP_*`, `REPORT_TO` | report on the dashboard only, no Monday email |
| 8 | Login password hash, Postgres password, optional hostname for HTTPS | `.env` next to compose | the stack refuses to start without the hash |
| 9 | Heartbeat devices at stores (later) | `HEARTBEAT_TOKEN` | outages from utility maps or POS gaps only |
| 10 | Dutchie location keys (when approved) | next connector | no receipt-level detail; aggregate feed continues |
| 11 | `ANTHROPIC_API_KEY` | `backend/.env` | no "Ask Aurora"; dashboard and report unaffected |

Backups: the Postgres volume is the system of record for imported data, but
every Headset pull is also recorded as JSON under the data volume, so
`docker compose exec postgres pg_dump -U aurora aurora > backup.sql` nightly
plus a copy of `/data/headset` is a full recovery set.

## Deploy on a headless server

On a Linux VPS, as a normal user with sudo. Read the script first, then run it:

```bash
git clone -b claude/sleepy-ritchie-hufvuf https://github.com/jdtrappeii/aurora-ai.git ~/aurora
less ~/aurora/deploy/install.sh
bash ~/aurora/deploy/install.sh
```

`deploy/install.sh` starts with a read-only preflight (Docker present or not,
compose plugin, git, disk, RAM, Tailscale IP, whether the port is free) and
changes nothing until you answer `y`. It reuses an existing Docker and asks
before installing one. It then asks for each key and share link (secrets are
not echoed; blank keeps the current value or leaves that source disabled, and
the sync reports it as skipped), generates the Postgres password, heartbeat
token and dashboard login hash, builds the stack in its own compose project
(`aurora`, under `~/aurora`, touching nothing else on the box), and runs the
first 90-day, Florida-only sync. The proxy binds to `127.0.0.1:8080` unless you
give it the box's Tailscale IP; it refuses `0.0.0.0`. No public port is opened.
Headset is not asked for until you run it with `AURORA_HEADSET=1`. Re-running
the script pulls updates and fills gaps only.

Reach the dashboard from a laptop with an SSH tunnel, then open
`http://localhost:8080`:

```bash
ssh -L 8080:127.0.0.1:8080 user@vps
```

The manual equivalent:

One box, five containers: Postgres, the API, the dashboard, a scheduler that
runs every configured sync once a night, and a Caddy proxy that is the only
published port and puts a login in front of everything. The API and dashboard
are never exposed directly. Store heartbeat pings bypass the login because the
devices carry their own `HEARTBEAT_TOKEN`. Secrets and share links live in the
two `.env` files on the server and nowhere else.

```bash
git clone <your private clone or this template> aurora && cd aurora
cp backend/.env.example backend/.env          # keys, share links, HEARTBEAT_TOKEN, GEOCODER_USER_AGENT
cp .env.example .env                          # POSTGRES_PASSWORD, AURORA_USER, AURORA_DOMAIN
docker compose run --rm proxy caddy hash-password   # type the dashboard password; paste the hash as AURORA_PASSWORD_HASH in .env
docker compose up -d --build
docker compose run --rm api python -m app.cli sync-all --backfill-days 90 --stores "FL -"   # first load
docker compose logs -f scheduler
```

Open `http://localhost:8080` through the tunnel (or the Tailscale address) and
log in. For a public hostname instead, set `AURORA_DOMAIN` to it, `AURORA_BIND`
to `0.0.0.0`, `AURORA_HTTP_PORT=80`, `AURORA_HTTPS_PORT=443`, and Caddy obtains
an HTTPS certificate on its own. The
scheduler service runs `sync-all` at
`SYNC_HOUR_UTC` (default 08:00 UTC, 4am Eastern) and once on start-up. Each
step runs only when configured, never blocks the others, and the run ends with
a JSON summary of what ran, what was skipped and why, and what failed:

| Step | Needs | What it does |
|---|---|---|
| headset | `HEADSET_MCP_URL` + token | store-day totals, product-grain sales, discount codes, inventory for the trailing days; raw pulls recorded under `/data/headset` |
| geocode | store addresses | coordinates for any store still without them |
| weather | nothing | Open-Meteo hourly + 7-day forecast, NWS alerts |
| events | keys per provider | Ticketmaster, SeatGeek, FL511 for the next 30 days; holidays + cannabis calendar always |
| sheets | share links | market report, competitor deals, promotions workbook |
| heartbeats | device pings | silences become outage events |
| reconcile | nothing | product lines vs feed totals; missing days listed |

Without Docker: run the API with `uvicorn`, the dashboard with `next start`
after `npm run build`, and put `python -m app.cli sync-all` in cron. Recorded
Headset pulls can be replayed on any machine with `headset-import-dir`, so a
laptop pull and a server import are the same data. Schema changes apply themselves at start-up (see *Schema changes*).

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

112 tests. Every monetary expectation is worked out by hand in the test body.

An existing database from an older version is upgraded in place at start-up.

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
| `promotions.csv` | name, start_date, end_date, discount_type (`percent`/`amount`/`bogo`), discount_value, eligible_skus (pipe-separated), eligible_category (the promotions workbook adds weekdays, stores, POS discount names) |
| `external_events.csv` | event_id, store, event_type, source, latitude, longitude, affected_radius_km, start_time, end_time, severity, description, confidence, source_reference, is_forecast |
| `weather.csv` | store, observed_at, is_forecast, temperature_f, precipitation_in, snowfall_in, wind_mph, condition, alert, source |

Prices in `sale_items` are **per unit**; `discount_amount` is derived as
`(regular_price − sale_price) × quantity`.

## API

| Endpoint | Returns |
|---|---|
| `POST /api/import/{kind}` | Upload one CSV (`kind` = any file name above without `.csv`) |
| `GET /api/dashboard?as_of=&store=` | Everything the home screen needs (`store` = code, `state:FL`, or blank) |
| `GET /api/metrics/weekly` | Current vs previous vs four-week average with deltas |
| `GET /api/metrics/weekly-trend?weeks=12` | One summary per week |
| `GET /api/metrics/summary?start=&end=` | Financial summary for any period |
| `GET /api/metrics/products`, `/categories` | Profitability breakdowns |
| `GET /api/metrics/inventory` | Position, aging, sell-through, classification |
| `GET /api/metrics/promotions` | Deal autopsies |
| `GET /api/metrics/discounts?start=&end=&store=` | Discount-code report (aggregate feed) |
| `GET /api/report/weekly?as_of=&store=&format=json\|html` | The weekly owner report |
| `POST /api/analyst` | Ask the analyst (question, scope, as_of, history) |
| `GET /api/metrics/market?as_of=` | Market context from the state weekly report |
| `GET /api/metrics/pressure?start=&end=` | Competitor promo pressure per operator |
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
| **V1.5 (this)** | Headset connector (sync, recorded envelopes, exact aggregate totals, reconciliation, discount-code report); free event stack (Ticketmaster, SeatGeek, FL511, holiday + cannabis calendar, store heartbeats, Nominatim geocoding); weather from Open-Meteo + NWS alerts; live spreadsheets (state market report, competitor deals, promotions workbook) |
| **V2 (this)** | Claude analyst with read-only tools over the analytics; evidence-graded answers with a trace |
| V3 | Automatic QuickBooks and Dutchie POS synchronization; scheduled nightly Headset / weather / events sync |
| V4 | Scheduled weekly owner report, forecasting, vendor intelligence |
| Later | Metrc integration only when there is a clear operational need |
