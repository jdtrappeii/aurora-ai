#!/usr/bin/env bash
# Aurora on a Linux server (VPS or LAN box). Idempotent: run it again to update
# the code, add keys you did not have the first time, or rebuild.
#
# Read it, then run it as a normal user with sudo rights:
#   git clone -b claude/sleepy-ritchie-hufvuf https://github.com/jdtrappeii/aurora-ai.git ~/aurora
#   bash ~/aurora/deploy/install.sh
# (or download just this file, read it, and run it: it clones the rest itself)
#
# What it does, in order:
#   0. preflight: reports Docker, compose, git, free disk/RAM, Tailscale, and
#      whether the chosen ports are taken; nothing is changed before you say y
#   1. Docker: reused when present; installed only after an explicit yes
#   2. clones or updates the repository into $AURORA_HOME (default ~/aurora),
#      its own directory and its own compose project ("aurora"), touching
#      nothing else on the box
#   3. creates the two .env files from their examples if missing (0600)
#   4. asks for each key and link, without echoing secrets; blank keeps the
#      current value or leaves that source disabled (the sync skips it)
#   5. generates the Postgres password, heartbeat token and dashboard login hash
#   6. builds and starts the stack bound to 127.0.0.1 (or the Tailscale IP you
#      choose), never a public interface; runs the first sync; prints the summary
#
# Nothing typed here leaves the server: values go into backend/.env and .env,
# both gitignored. Headset is not asked for unless AURORA_HEADSET=1 (the sync
# reports that step as skipped until HEADSET_MCP_URL is set).
# Set AURORA_NONINTERACTIVE=1 to skip every prompt (update only).
set -euo pipefail
USER="${USER:-$(id -un)}"

REPO_URL="${AURORA_REPO:-https://github.com/jdtrappeii/aurora-ai.git}"
BRANCH="${AURORA_BRANCH:-claude/sleepy-ritchie-hufvuf}"
HOME_DIR="${AURORA_HOME:-$HOME/aurora}"
BACKFILL_DAYS="${AURORA_BACKFILL_DAYS:-90}"
STORE_FILTER="${AURORA_STORE_FILTER:-FL -}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 0. preflight
confirm() {  # confirm "question" -> 0 on y/yes
  [ "${AURORA_NONINTERACTIVE:-0}" = "1" ] && return 1
  local a; printf '%s [y/N] ' "$1"; read -r a </dev/tty; [[ "$a" =~ ^[Yy]([Ee][Ss])?$ ]]
}
port_in_use() { (command -v ss >/dev/null && ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1$") ; }
preflight() {
  say "Preflight (read-only)"
  echo "  host:      $(hostname)  user: $USER  sudo: $(sudo -n true 2>/dev/null && echo yes || echo 'will prompt')"
  echo "  os:        $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -sr)"
  echo "  disk free: $(df -h "$HOME" | awk 'NR==2{print $4}')   ram: $(free -h 2>/dev/null | awk '/Mem:/{print $7" free of "$2}')"
  if command -v docker >/dev/null 2>&1; then
    echo "  docker:    $(docker --version 2>/dev/null)  -> will be REUSED, not reinstalled"
    docker compose version >/dev/null 2>&1 && echo "  compose:   $(docker compose version 2>/dev/null)" || echo "  compose:   plugin missing (needed)"
    docker info >/dev/null 2>&1 && echo "  daemon:    reachable as $USER" || echo "  daemon:    not reachable as $USER (docker group membership or sudo needed)"
    local n; n="$(docker ps --format '{{.Names}}' 2>/dev/null | wc -l)"; echo "  containers already running on this box: $n (left alone; Aurora uses its own 'aurora' project)"
  else
    echo "  docker:    not installed (you will be asked before it is installed)"
  fi
  echo "  git:       $(command -v git >/dev/null && git --version || echo 'missing (will be installed with apt)')"
  if command -v tailscale >/dev/null 2>&1; then
    echo "  tailscale: $(tailscale ip -4 2>/dev/null | head -1 || echo 'installed, no IP')"
  else
    echo "  tailscale: not installed (dashboard stays on 127.0.0.1; use an SSH tunnel)"
  fi
  local hp="${AURORA_HTTP_PORT:-$(getenv "$HOME_DIR/.env" AURORA_HTTP_PORT 2>/dev/null)}"; [[ "$hp" =~ ^[0-9]{2,5}$ ]] || hp=8080
  if port_in_use "$hp"; then warn "port $hp is already in use on this box; you will be asked for another"; else echo "  port $hp:  free"; fi
  echo "  install to: $HOME_DIR   branch: $BRANCH"
  echo "  first sync: $BACKFILL_DAYS days back, Headset store filter '$STORE_FILTER' (Headset step skipped until configured)"
  confirm "Continue?" || die "Stopped before making any change."
}

# ---------------------------------------------------------------- 1. docker
need_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return 0
  fi
  if command -v docker >/dev/null 2>&1; then
    warn "Docker is present but the compose v2 plugin is not."
    confirm "Install docker-compose-plugin with apt?" || die "Install the compose plugin and re-run."
    sudo apt-get update -qq && sudo apt-get install -y -qq docker-compose-plugin
    return 0
  fi
  confirm "Docker is not installed. Install Docker Engine + compose plugin now (official get.docker.com script)?" || die "Install Docker and re-run."
  say "Installing Docker"
  command -v curl >/dev/null 2>&1 || { sudo apt-get update -qq && sudo apt-get install -y -qq curl; }
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  warn "The installer was saved to /tmp/get-docker.sh; running it with sudo."
  sudo sh /tmp/get-docker.sh
  if [ "$(id -u)" != "0" ]; then
    sudo usermod -aG docker "$USER" || true
    warn "Added $USER to the docker group. If the next step fails with a permission error,"
    warn "log out and back in (or run: newgrp docker) and run this script again."
  fi
  docker compose version >/dev/null 2>&1 || die "Docker installed but the compose plugin is missing; install docker-compose-plugin and re-run."
}
# WSL without systemd has no service manager: start the daemon by hand.
start_docker() {
  if docker info >/dev/null 2>&1; then return 0; fi
  if grep -qi microsoft /proc/version 2>/dev/null; then
    say "Starting the Docker daemon (WSL)"
    sudo service docker start >/dev/null 2>&1 || sudo dockerd >/var/log/dockerd.log 2>&1 &
    local i; for i in $(seq 1 30); do docker info >/dev/null 2>&1 && return 0; sleep 1; done
  fi
  if sudo -n docker info >/dev/null 2>&1 || sudo docker info >/dev/null 2>&1; then
    die "Docker runs but $USER cannot talk to it. Run: sudo usermod -aG docker $USER && newgrp docker   then re-run."
  fi
  die "Docker daemon is not running. Start it (sudo systemctl start docker) and re-run."
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
  sanitize_env .env; sanitize_env backend/.env
}
# The examples carry "KEY=value   # explanation" lines. Not every parser strips
# the comment (compose kept one as the value), so strip them on disk.
sanitize_env() {
  sed -i -E 's/^([A-Z0-9_]+=)[[:space:]]+#.*$/\1/; s/^([A-Z0-9_]+=[^#]*[^[:space:]#])[[:space:]]+#.*$/\1/' "$1"
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
  while read -r -t 0.2 _ </dev/tty; do :; done   # drop any pasted-ahead lines
  say "Keys and links (press Enter to keep what is already there; nothing is echoed for secrets)"
  if [ "${AURORA_HEADSET:-0}" = "1" ]; then
    echo "Headset"
    ask $be HEADSET_MCP_URL     "MCP endpoint URL"
    ask $be HEADSET_MCP_TOKEN   "MCP token" secret
  else
    echo "Headset: not configured (run with AURORA_HEADSET=1 when cleared); the sync reports it as skipped"
  fi
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
  echo "Reachability (never a public interface)"
  local ts; ts="$(command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -1 || true)"
  [ -n "$ts" ] && echo "  Tailscale IP detected: $ts  (enter it below to reach Aurora over the tailnet; blank = 127.0.0.1 + SSH tunnel)"
  ask $ce AURORA_BIND      "Bind address"
  ask $ce AURORA_HTTP_PORT "HTTP port on that address"
  local bind port; bind="$(getenv $ce AURORA_BIND)"; port="$(getenv $ce AURORA_HTTP_PORT)"
  [ -n "$bind" ] || setenv $ce AURORA_BIND 127.0.0.1
  [[ "$port" =~ ^[0-9]{2,5}$ ]] || { warn "HTTP port '$port' is not a number; using 8080"; setenv $ce AURORA_HTTP_PORT 8080; }
  case "$(getenv $ce AURORA_BIND)" in
    0.0.0.0|::|"[::]") die "Refusing to bind to all interfaces on this install. Use 127.0.0.1 or the Tailscale IP." ;;
  esac
  while port_in_use "$(getenv $ce AURORA_HTTP_PORT)"; do
    warn "port $(getenv $ce AURORA_HTTP_PORT) is in use"; ask $ce AURORA_HTTP_PORT "Another HTTP port"
  done

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
  case "$hash" in
    *'$$'*) ;;                                   # already escaped for compose
    '$2'*) setenv .env AURORA_PASSWORD_HASH "${hash//\$/\$\$}"; hash="escaped" ;;   # written by an older run
  esac
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
    # compose interpolates "$name" inside .env values; "$$" is a literal "$"
    setenv .env AURORA_PASSWORD_HASH "${hash//\$/\$\$}"
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
  local bind port; bind="$(getenv .env AURORA_BIND)"; port="$(getenv .env AURORA_HTTP_PORT)"
  echo "  Dashboard:  http://${bind:-127.0.0.1}:${port:-8080}   (user: $(getenv .env AURORA_USER))"
  if [ "${bind:-127.0.0.1}" = "127.0.0.1" ]; then
    echo "  From your laptop:  ssh -L ${port:-8080}:127.0.0.1:${port:-8080} $USER@$(hostname)   then open http://localhost:${port:-8080}"
  fi
  echo "  Bound to ${bind:-127.0.0.1} only; no public port was opened."
  echo "  Next syncs: nightly at $(getenv .env SYNC_HOUR_UTC):00 UTC by the scheduler container"
  echo "  Logs:       cd $HOME_DIR && docker compose logs -f scheduler"
  echo "  Re-run:     bash $HOME_DIR/deploy/install.sh   (adds keys, pulls updates, rebuilds)"
  echo "  GMB export: docker compose cp locations.csv api:/tmp/ && docker compose run --rm api python -m app.cli gmb-import /tmp/locations.csv && docker compose run --rm api python -m app.cli geocode-stores"
}

preflight
need_docker
start_docker
fetch_code
if [ "${AURORA_NONINTERACTIVE:-0}" != "1" ]; then collect; fi
generate
launch
