# Aurora AI: notes for a Claude Code session on the server

Aurora is a self-hosted profitability dashboard for cannabis retail. On a
server it runs as the Docker Compose project `aurora` (postgres, api,
scheduler, web, proxy) from this directory. The scheduler runs `sync-all`
nightly (4am Eastern by default). The dashboard is served on the Tailscale
address at `AURORA_HTTP_PORT` (default 8080).

## Rules that always apply

- **Data stays on this machine.** The database, `/data/headset` recordings,
  `backend/.env`, and anything under `backend/data` never go into git, a chat
  message, a gist, or any upload. Do not print `.env`, tokens, or full table
  dumps. Summaries, counts, and status lines are fine.
- **Never commit or push from the server.** The server only pulls:
  `git fetch origin && git reset --hard origin/<branch>`; the branch is the
  one `git branch --show-current` reports. Code changes are made elsewhere
  and pulled here.
- **No public ports.** The proxy binds to the Tailscale address only; do not
  change `AURORA_BIND`, open firewall ports, or add tunnels.
- **Ask before anything destructive:** `docker compose down -v`, dropping
  tables, deleting `/data/headset`, or removing the compose volumes.

## Everyday commands (run from this directory)

```bash
docker compose ps                                   # stack health
docker compose logs --tail 100 scheduler            # nightly sync log
docker compose run --rm api python -m app.cli headset-status
docker compose run --rm api python -m app.cli sync-all --backfill-days 3 --stores "FL -"
bash deploy/headset-renew.sh [--sync]               # copy Claude Code's Headset token into Aurora
AURORA_NONINTERACTIVE=1 bash deploy/install.sh      # update: pull, rebuild, restart, keep every answer
docker compose cp locations.csv api:/tmp/ && docker compose run --rm api python -m app.cli gmb-import /tmp/locations.csv && docker compose run --rm api python -m app.cli geocode-stores
docker compose run --rm api python -m app.cli weekly-report
docker compose exec api python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/api/health').read().decode())"
```

Other CLI commands: `headset-sync`, `headset-import-dir`, `headset-reconcile`,
`events-sync`, `weather-sync`, `sheets-sync`, `sheets-headers`,
`heartbeat-status`, `ask`. `python -m app.cli --help` inside the api
container lists them all.

## Headset sign-in

Headset's MCP server issues day-long tokens with no refresh, and only
allow-listed clients may sign in, so Aurora reuses the token Claude Code
obtains. The browser step (`/mcp`, Headset, Authenticate) can only be done
in a terminal session; `deploy/headset-renew.sh` then imports it. If
`headset-status` shows the token expired, say so and ask the operator to
run `bash deploy/headset-signin.sh` in a terminal; do not try to sign in
another way.

## Layout

- `backend/app`: FastAPI API, CLI (`cli.py`), analytics, importers,
  integrations (Headset MCP client under `integrations/headset`).
- `frontend`: Next.js dashboard.
- `deploy/`: `install.sh` (installer/updater), `headset-renew.sh`,
  `headset-signin.sh`.
- `docs/`: handoff notes and the OAuth client document.
