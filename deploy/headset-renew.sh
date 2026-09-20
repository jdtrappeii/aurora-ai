#!/usr/bin/env bash
# Renew Aurora's Headset sign-in from the credential Claude Code holds on this
# box, then (optionally) run the nightly sync. Headset's sign-in server only
# honours client documents it has allow-listed, so Aurora cannot sign in under
# its own name; it reuses the token Claude Code obtained for the same server.
# That token lasts about a day and carries no refresh token, so the browser
# step happens in Claude Code (`claude`, then `/mcp`, pick the server,
# Authenticate) and this script copies the result into Aurora.
#
#   bash deploy/headset-renew.sh            # import if newer, print status
#   bash deploy/headset-renew.sh --sync     # ...then sync-all for the trailing days (--days=3, --stores="FL -")
#   bash deploy/headset-renew.sh --quiet    # cron-friendly: only speak when something is wrong
#
# Cron (hourly; harmless when nothing changed):
#   0 * * * * bash $HOME/aurora/deploy/headset-renew.sh --quiet --sync >> $HOME/aurora/renew.log 2>&1
set -euo pipefail

HOME_DIR="${AURORA_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CREDS="${CLAUDE_CREDENTIALS:-$HOME/.claude/.credentials.json}"
SYNC=0; QUIET=0; DAYS=3; STORES=""
for a in "$@"; do
  case "$a" in
    --sync) SYNC=1 ;;
    --quiet) QUIET=1 ;;
    --days=*) DAYS="${a#--days=}" ;;
    --stores=*) STORES="${a#--stores=}" ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done
log() { [ "$QUIET" = 1 ] || echo "$*"; }
cd "$HOME_DIR"

url="$( { sed -n 's/^HEADSET_MCP_URL=//p' backend/.env 2>/dev/null || true; } | tr -d '"' | sed 's/[[:space:]]*#.*//' | head -1)"
url="${url:-https://mcp.headset.io}"
[ -r "$CREDS" ] || { echo "No Claude Code credential at $CREDS. Run: claude   then /mcp, pick the Headset server, Authenticate." >&2; exit 1; }

# Expiry of the Claude Code token (ms since epoch) vs what Aurora already holds.
cc_exp="$(python3 - "$CREDS" "$url" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
want = sys.argv[2].rstrip("/")
for e in (d.get("mcpOAuth") or {}).values():
    if str(e.get("serverUrl", "")).rstrip("/") == want and e.get("accessToken"):
        print(int((e.get("expiresAt") or 0) / 1000)); break
else:
    print(0)
PY
)"
now="$(date +%s)"
if [ "$cc_exp" -le "$now" ]; then
  echo "Claude Code's Headset token is missing or expired. Run: claude   then /mcp, pick the Headset server, Authenticate, and re-run this script." >&2
  exit 1
fi

status="$(docker compose run --rm -T api python -m app.cli headset-status 2>/dev/null || true)"
aurora_left="$(printf '%s' "$status" | python3 -c 'import json,sys
try: print(int(json.load(sys.stdin).get("expires_in_s") or 0))
except Exception: print(0)')"
cc_left=$((cc_exp - now))
if [ "$cc_left" -gt "$aurora_left" ]; then
  docker compose run --rm -T -v "$CREDS:/tmp/cc.json:ro" api python -m app.cli headset-login --from-claude-code /tmp/cc.json >/dev/null
  log "Imported Claude Code's Headset token: valid for $((cc_left / 3600)) h $(( (cc_left % 3600) / 60 )) min."
else
  log "Aurora's token is already the newest one: valid for $((aurora_left / 3600)) h $(( (aurora_left % 3600) / 60 )) min."
fi

if [ "$SYNC" = 1 ]; then
  args=(sync-all --backfill-days "$DAYS")
  [ -z "$STORES" ] || args+=(--stores "$STORES")
  if [ "$QUIET" = 1 ]; then
    docker compose run --rm -T api python -m app.cli "${args[@]}" >/dev/null || { echo "sync-all failed; run it by hand to see why: docker compose run --rm api python -m app.cli ${args[*]}" >&2; exit 1; }
  else
    docker compose run --rm -T api python -m app.cli "${args[@]}"
  fi
fi
