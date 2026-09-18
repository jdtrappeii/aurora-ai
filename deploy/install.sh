#!/usr/bin/env bash
# Aurora on a headless server in one command. Idempotent: run it again to
# update the code, add keys you did not have the first time, or rebuild.
#
#   curl -fsSL https://raw.githubusercontent.com/jdtrappeii/aurora-ai/claude/sleepy-ritchie-hufvuf/deploy/install.sh | bash
#   # or, from a clone:  bash deploy/install.sh
#
# What it does, in order:
#   1. installs Docker (with the compose plugin) if the box does not have it
#   2. clones or updates the repository into $AURORA_HOME (default ~/aurora)
#   3. creates the two .env files from their examples if missing
#   4. asks for each key and link, without echoing secrets; blank keeps the
#      current value, so re-running only fills gaps
#   5. generates the Postgres password, heartbeat token and dashboard login hash
#   6. builds and starts the stack, runs the first sync, prints the summary
#
# Nothing typed here leaves the server: values go into backend/.env and .env,
# both gitignored. Set AURORA_NONINTERACTIVE=1 to skip the prompts (update only).
set -euo pipefail

REPO_URL="${AURORA_REPO:-https://github.com/jdtrappeii/aurora-ai.git}"
BRANCH="${AURORA_BRANCH:-claude/sleepy-ritchie-hufvuf}"
HOME_DIR="${AURORA_HOME:-$HOME/aurora}"
BACKFILL_DAYS="${AURORA_BACKFILL_DAYS:-90}"
STORE_FILTER="${AURORA_STORE_FILTER:-FL -}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. docker
need_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return 0
  fi
  say "Installing Docker (official convenience script)"
  command -v curl >/dev/null 2>&1 || { sudo apt-get update -qq && sudo apt-get install -y -qq curl; }
  curl -fsSL https://get.docker.com | sudo sh
  if [ "$(id -u)" != "0" ]; then
    sudo usermod -aG docker "$USER" || true
    warn "Added $USER to the docker group. If the next step fails with a permission error,"
    warn "log out and back in (or run: newgrp docker) and run this script again."
  fi
  docker compose version >/dev/null 2>&1 || die "Docker installed but the compose plugin is missing; install docker-compose-plugin and re-run."
}

# ---------------------------------------------------------------- 2. code
fetch_code() {
  if [ -d "$HOME_DIR/.git" ]; then
    say "Updating $HOME_DIR ($BRANCH)"
    git -C "$HOME_DIR" fetch --quiet origin "$BRANCH"
    git -C "$HOME_DIR" checkout --quiet "$BRANCH"
    git -C "$HOME_DIR" pull --ff-only --quiet origin "$BRANCH"
  else
    say "Cloning $REPO_URL ($BRANCH) into $HOME_DIR"
    command -v git >/dev/null 2>&1 || { sudo apt-get update -qq && sudo apt-get install -y -qq git; }
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$HOME_DIR"
  fi
  cd "$HOME_DIR"
  [ -f .env ] || cp .env.example .env
  [ -f backend/.env ] || cp backend/.env.example backend/.env
  chmod 600 .env backend/.env
}

# ---------------------------------------------------------------- env helpers
# setenv FILE KEY VALUE: replace the KEY= line (dropping any trailing comment) or append it.
setenv() {
  local file="$1" key="$2" value="$3" tmp
  tmp="$(mktemp)"
  if grep -qE "^${key}=" "$file"; then
    awk -v k="$key" -v v="$value" 'BEGIN{done=0} { if (!done && index($0, k "=") == 1) { print k "=" v; done=1 } else print }' "$file" > "$tmp"
  else
    cat "$file" > "$tmp"; printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  cat "$tmp" > "$file"; rm -f "$tmp"
}
getenv() {  # current value, comments stripped
  grep -E "^$2=" "$1" | head -1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+|[[:space:]]+$//g' || true
}
# ask FILE KEY "prompt" [secret]
ask() {
  local file="$1" key="$2" label="$3" secret="${4:-}" cur val
  cur="$(getenv "$file" "$key")"
  if [ -n "$cur" ] && [ "$cur" != "change-me" ]; then
    if [ -n "$secret" ]; then printf '  %-24s [set]  ' "$label"; else printf '  %-24s [%s]  ' "$label" "$cur"; fi
  else
    printf '  %-24s [blank = skip]  ' "$label"
  fi
  if [ -n "$secret" ]; then read -r -s val </dev/tty; echo; else read -r val </dev/tty; fi
  [ -z "$val" ] && return 0
  setenv "$file" "$key" "$val"
}
sheet_id() {  # accept a full Google Sheets URL or a bare id
  local v="$1"
  if [[ "$v" =~ /spreadsheets/d/([A-Za-z0-9_-]+) ]]; then echo "${BASH_REMATCH[1]}"; else echo "$v"; fi
}

# ---------------------------------------------------------------- 3/4. prompts
collect() {
  local be="backend/.env" ce=".env"
  say "Keys and links (press Enter to keep what is already there; nothing is echoed for secrets)"
  echo "Headset"
  ask $be HEADSET_MCP_URL     "MCP endpoint URL"
  ask $be HEADSET_MCP_TOKEN   "MCP token" secret
  echo "Events and traffic (free keys; blank skips that source)"
  ask $be TICKETMASTER_API_KEY  "Ticketmaster key" secret
  ask $be SEATGEEK_CLIENT_ID    "SeatGeek client id"
  ask $be SEATGEEK_CLIENT_SECRET "SeatGeek secret" secret
  ask $be ROAD511_API_KEY       "Road511 key" secret
  ask $be FL511_API_KEY         "FL511 key (if FDOT sent one)" secret
  echo "Sheets (paste the full link or the id; must be shared 'anyone with the link')"
  ask $be MARKET_SHEET_ID  "OMMU dashboard sheet"
  ask $be DEALS_SHEET_ID   "Competitor deals sheet"
  ask $be PROMOTIONS_URL   "Promotions OneDrive link"
  for k in MARKET_SHEET_ID DEALS_SHEET_ID; do
    cur="$(getenv $be $k)"; [ -n "$cur" ] && setenv $be $k "$(sheet_id "$cur")"
  done
  echo "Ask Aurora"
  ask $be ANTHROPIC_API_KEY "Anthropic key" secret
  echo "Weekly email (all blank = dashboard report only)"
  ask $be SMTP_HOST     "SMTP host"
  ask $be SMTP_USER     "SMTP user"
  ask $be SMTP_PASSWORD "SMTP password" secret
  ask $be SMTP_FROM     "From address"
  ask $be REPORT_TO     "Recipients (comma-separated)"
  echo "Dashboard login"
  ask $ce AURORA_USER   "Login user name"
  ask $ce AURORA_DOMAIN "Hostname for HTTPS (':80' = plain HTTP on the LAN)"

  [ -n "$(getenv $be DEFAULT_SCOPE)" ] || setenv $be DEFAULT_SCOPE "state:FL"
  [ -n "$(getenv $be GEOCODER_USER_AGENT)" ] || setenv $be GEOCODER_USER_AGENT "aurora-ai/1.0 (${AURORA_CONTACT:-ops@example.com})"
}

# ---------------------------------------------------------------- 5. generated secrets
generate() {
  local pw tok hash
  pw="$(getenv .env POSTGRES_PASSWORD)"
  if [ -z "$pw" ] || [ "$pw" = "change-me" ]; then
    say "Generating the Postgres password"
    setenv .env POSTGRES_PASSWORD "$(openssl rand -hex 24 2>/dev/null || head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 48)"
  fi
  tok="$(getenv backend/.env HEARTBEAT_TOKEN)"
  if [ -z "$tok" ]; then
    setenv backend/.env HEARTBEAT_TOKEN "$(openssl rand -hex 16 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
  fi
  hash="$(getenv .env AURORA_PASSWORD_HASH)"
  if [ -z "$hash" ]; then
    say "Dashboard password (this is what you type at the login box)"
    local p1 p2
    while :; do
      printf '  password: '; read -r -s p1 </dev/tty; echo
      printf '  again:    '; read -r -s p2 </dev/tty; echo
      [ -n "$p1" ] && [ "$p1" = "$p2" ] && break
      warn "Empty or mismatched, try again."
    done
    hash="$(docker run --rm caddy:2 caddy hash-password --plaintext "$p1" | tr -d '\r\n')"
    [[ "$hash" == \$2a\$* ]] || die "Could not hash the password (got: $hash)"
    setenv .env AURORA_PASSWORD_HASH "$hash"
    unset p1 p2
  fi
}

# ---------------------------------------------------------------- 6. run
launch() {
  say "Building and starting the stack"
  docker compose up -d --build
  say "Waiting for the API"
  local i
  for i in $(seq 1 60); do
    if docker compose exec -T api python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health',timeout=3)" >/dev/null 2>&1; then break; fi
    sleep 2
  done
  say "First sync: $BACKFILL_DAYS days back, Headset stores matching '$STORE_FILTER'"
  docker compose run --rm -T api python -m app.cli sync-all --backfill-days "$BACKFILL_DAYS" --stores "$STORE_FILTER" || warn "sync-all exited non-zero; the summary above says which step failed"
  say "Done"
  local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "  Dashboard:  http://${ip:-<server-ip>}   (user: $(getenv .env AURORA_USER))"
  echo "  Next syncs: nightly at $(getenv .env SYNC_HOUR_UTC):00 UTC by the scheduler container"
  echo "  Logs:       cd $HOME_DIR && docker compose logs -f scheduler"
  echo "  Re-run:     bash $HOME_DIR/deploy/install.sh   (adds keys, pulls updates, rebuilds)"
  echo "  GMB export: docker compose cp locations.csv api:/tmp/ && docker compose run --rm api python -m app.cli gmb-import /tmp/locations.csv && docker compose run --rm api python -m app.cli geocode-stores"
}

need_docker
fetch_code
if [ "${AURORA_NONINTERACTIVE:-0}" != "1" ]; then collect; fi
generate
launch
