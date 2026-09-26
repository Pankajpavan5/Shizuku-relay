#!/usr/bin/env bash
# STEP 3 — run a command through the relay and get the response
# Usage:  ./3_run_cmd.sh "pm list packages | head -5"
#         BASE=https://xxx.trycloudflare.com ./3_run_cmd.sh "id"
BASE="${BASE:-http://localhost:8090}"
TOKEN="${RELAY_TOKEN:-d31eeff29eb82855}"
CMD="${1:-id}"

python3 - "$BASE" "$TOKEN" "$CMD" << 'PY'
import json, sys, urllib.request
base, token, cmd = sys.argv[1], sys.argv[2], sys.argv[3]
def req(path, data=None):
    r = urllib.request.Request(base + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"X-Relay-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.load(resp)
print(f"→ base={base}\n→ cmd: {cmd}")
job = req("/api/commands", {"device": "*", "command": cmd, "timeout": 90})
print(f"→ queued job {job['job_id']} … waiting for phone")
res = req(f"/api/commands/{job['job_id']}/wait?t=120")
if not res.get("finished"):
    print(f"⚠ still {res['status']} — is the phone app polling this URL?"); sys.exit(2)
print("─" * 46); print(res.get("stdout", ""), end="")
if res.get("stderr"): print(f"[stderr] {res['stderr']}")
print(f"─" * 46)
print(f"[exit {res['exit_code']} · {res['duration_ms']} ms · device {res['device']}]")
PY
