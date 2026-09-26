#!/usr/bin/env bash
# STEP 2 — punch a free Cloudflare quick-tunnel to your local relay
cd "$(dirname "$0")"
PORT="${PORT:-8090}"
CF=./bin/cloudflared
[ -x "$CF" ] || CF="$(command -v cloudflared || true)"
if [ -z "$CF" ]; then
  echo "cloudflared not found. Get it: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
  echo "On Termux: pkg install cloudflared   (or: pkg install cloudflared -y)"
  exit 1
fi
chmod +x "$CF" 2>/dev/null
echo "Opening tunnel → http://localhost:$PORT  (look for the trycloudflare URL below)"
exec "$CF" tunnel --url "http://localhost:$PORT" --no-autoupdate
