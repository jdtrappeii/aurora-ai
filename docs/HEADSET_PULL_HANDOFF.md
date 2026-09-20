# Pulling Headset data through the Claude connector and importing it on the server

Use this when the server has no Headset credential of its own yet. A Claude
session with the Headset connector attached pulls the aggregate data, writes it
as Aurora envelope files, and hands the files to the operator, who imports them
on the server with `headset-import-dir`. Nothing here is stored anywhere but
the operator's download and the server.

## What to pull (90 days, all Florida stores)

Calls, in order. Record each raw tool result inside an envelope (below).

| Step | Tool | Arguments | Envelope |
|---|---|---|---|
| 1 | `retailer_get_stores` | none | `{"kind": "stores", "result": <result>}` -> `stores.json` |
| 2 | `retailer_sales_trend` | `grain=day`, `dimension=store`, date range = the 90 days ending yesterday, store filter: names starting `FL -` | `{"kind": "store_days", "result": <result>}` -> `store_days__<start>__<end>.json` |
| 3 (optional) | `retailer_sales_by_dimension` | `dimension=discount_name`, one store, one `sold_date`, for the last 14 days | `{"kind": "discounts", "store_name": ..., "sold_date": ..., "result": <result>}` -> `discounts__<slug>__<date>.json` |
| 4 (optional) | `retailer_get_inventory` | one store | `{"kind": "inventory", "store_name": ..., "snapshot_date": <today>, "result": <result>}` -> `inventory__<slug>__<date>.json` |

Step 2 is the one that matters: it fills the dashboard tiles, the twelve-week
chart, the store ranking, the weekly report and the store-day promotion
verdicts. Steps 3 and 4 are per store per day and add up quickly; pull a
sample (a few stores, recent days) rather than everything. Product-level
lines (`dimension=product`) are not worth pulling this way; the server's own
nightly sync does that once it has a credential.

If a result carries `hasMore: true`, request the next page and append its rows
to the same envelope's `result.rows`.

Row shapes Aurora expects (as the connector returns them):

- store_days rows: `sold_date, store_name, total_revenue, total_gross_sales, total_units, total_discounts, total_cost, total_profit, transaction_count`
- discounts rows: `discount_name, total_revenue, total_units, total_discounts, transaction_count`
- inventory rows: `product_name, sku, brand, category, unit, vendor, on_hand_units, price, avg_daily_units, days_of_supply, weeks_of_supply, on_hand_retail_value, on_hand_cost_value`
- stores: `{"stores": [{"storeId", "accountId", "name", "address": {"state", "postalCode"}}]}`

Dates: the connector's ranges are end-exclusive. Ask for `end = last day + 1`.

## Packaging

Put every envelope in one folder, e.g. `headset-pull-<date>/`, zip it, and send
the zip to the operator (in Claude Code: `SendUserFile`). Do not commit it: the
repository is public and `data/` is ignored for that reason.

## Importing on the server

On the server, with the zip in the home directory:

```bash
cd ~ && unzip -o headset-pull-*.zip -d headset-pull
cd ~/aurora
docker compose cp ~/headset-pull api:/tmp/headset-pull
docker compose run --rm api python -m app.cli headset-import-dir /tmp/headset-pull
docker compose run --rm api python -m app.cli headset-reconcile
```

The import prints one line per file with inserted/updated counts. The reconcile
step compares product lines (if any) with the store-day totals. Refresh the
dashboard afterwards; the scope selector will list every imported store.

Re-importing the same files is safe: rows are upserted by store and day.
