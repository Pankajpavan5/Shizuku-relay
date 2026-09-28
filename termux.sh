#!/usr/bin/env bash
# Arena MCP server on Termux: group chat + Shizuku relay + web page, one process, one port.
#
#   ./termux.sh          install deps if needed, start the server, open a public tunnel, print URLs
#   ./termux.sh local    server only, no tunnel (phone app can poll http://127.0.0.1:8000)
#   ./termux.sh test     run the built-in selftest and exit
#
# Token: reuses ./.relay_token so the phone app never has to be reconfigured.
# Rotate with:  python -c "import secrets;print(secrets.token_hex(8))" > .relay_token
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
TOKEN_FILE=".relay_token"
DEFAULT_TOKEN="d31eeff29eb82855"   # the token the Shizuku Relay app ships configured with
say() { printf '\033[36m%s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---- python + the one dependency ------------------------------------------------
if ! have python; then
  have pkg || { echo "python not found, and this is not Termux."; exit 1; }
  say "installing python…"; pkg install -y python
fi
if ! python -c "import mcp" 2>/dev/null; then
  say "installing mcp…"
  if ! pip install --quiet mcp; then
    # pydantic-core has no Android wheel: on Termux this needs a Rust build toolchain.
    if have pkg; then
      say "building from source (Termux has no prebuilt pydantic-core) — this takes a while…"
      pkg install -y rust binutils libffi openssl
      pip install --quiet mcp || {
        echo
        echo "mcp still won't install here. Two ways out:"
        echo "  1) run the server on your laptop instead — same file, same token; the phone app only needs a URL"
        echo "  2) keep retrying here after: pkg install -y rust clang make   (first build can take 10+ min)"
        exit 1
      }
    else
      echo "pip install mcp failed"; exit 1
    fi
  fi
fi

# ---- token: reuse the file so the phone app keeps working across restarts --------
if [ -f "$TOKEN_FILE" ]; then
  RELAY_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
else
  RELAY_TOKEN="${RELAY_TOKEN:-$DEFAULT_TOKEN}"
  printf '%s' "$RELAY_TOKEN" > "$TOKEN_FILE"
fi
export RELAY_TOKEN PORT

[ "${1:-}" = "test" ] && exec python server.py --selftest
[ "${1:-}" = "help" ] && sed -n '2,9p' "$0" && exit 0

# ---- keep Android from freezing us while this runs in the background -------------
if have termux-wake-lock; then
  termux-wake-lock
  trap 'termux-wake-unlock' EXIT
fi

python server.py & SERVER=$!
TUNNEL=""
cleanup() { kill "$SERVER" $TUNNEL 2>/dev/null || true; }
trap cleanup EXIT INT TERM
sleep 2
kill -0 "$SERVER" 2>/dev/null || { echo "server exited immediately — run: ./termux.sh test"; exit 1; }

echo
say "running. token for the phone app and the page: $RELAY_TOKEN"
echo "  page   http://127.0.0.1:$PORT/"
echo "  relay  http://127.0.0.1:$PORT/api/health"
echo "  mcp    http://127.0.0.1:$PORT/mcp"

if [ "${1:-}" = "local" ]; then
  echo
  echo "  local only: the phone app can use http://127.0.0.1:$PORT, but the AIs can't reach this."
  wait "$SERVER"
  exit
fi

# ---- public URL for the AI connectors -------------------------------------------
if ! have cloudflared && have pkg; then
  say "installing cloudflared…"; pkg install -y cloudflared || true
fi
if have cloudflared; then
  LOG="$(mktemp)"
  cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >"$LOG" 2>&1 & TUNNEL=$!
  URL=""
  for _ in $(seq 1 45); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
    if [ -n "$URL" ]; then break; fi
    kill -0 "$TUNNEL" 2>/dev/null || break
    sleep 1
  done
  if [ -n "$URL" ]; then
    echo
    say "PUBLIC — paste these into the AI connectors and the phone app:"
    echo "  phone app server URL:  $URL"
    echo "  page (for you):        $URL/"
    echo "  mcp (for the AIs):     $URL/mcp"
  else
    echo "  tunnel didn't come up; log: $LOG"
  fi
else
  echo
  echo "  no cloudflared: pkg install cloudflared   (or run './termux.sh local')"
fi

echo
say "Ctrl-C stops both the server and the tunnel."
wait "$SERVER" || echo "server exited with code $?"
