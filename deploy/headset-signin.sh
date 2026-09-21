#!/usr/bin/env bash
# Daily Headset sign-in: opens Claude Code so you can authenticate the Headset
# MCP server (/mcp, pick the server, Authenticate, then /exit), and the moment
# Claude Code closes, copies the fresh token into Aurora with headset-renew.sh.
# The nightly sync then runs on that token. One command each morning:
#
#   bash ~/aurora/deploy/headset-signin.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v claude >/dev/null 2>&1 || { echo "Claude Code is not installed on this box (the 'claude' command is missing)." >&2; exit 1; }
echo "Opening Claude Code. Type /mcp, pick the Headset server, choose Authenticate, finish in the browser, then /exit."
claude
echo
bash "$HERE/headset-renew.sh" "$@"
