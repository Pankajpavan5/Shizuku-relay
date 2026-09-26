#!/usr/bin/env bash
# STEP 1 — start the relay locally
cd "$(dirname "$0")"
export RELAY_TOKEN="${RELAY_TOKEN:-d31eeff29eb82855}"
export PORT="${PORT:-8090}"
echo "══════════════════════════════════════════════════"
echo "  Relay starting on http://localhost:$PORT"
echo "  Token: $RELAY_TOKEN"
echo "  Site:      http://localhost:$PORT/"
echo "  Dashboard: http://localhost:$PORT/dashboard?token=$RELAY_TOKEN"
echo "  Health:    http://localhost:$PORT/api/health"
echo "══════════════════════════════════════════════════"
exec python3 server.py
