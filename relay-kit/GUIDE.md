# Shizuku Relay Kit — local host → cloud tunnel → run cmd → get response

Everything you need, no accounts, no build. One Python file + one folder.

```
YOU run:  1 → relay listens locally (localhost:8090)
          2 → cloudflared makes it public (https://….trycloudflare.com)
PHONE:    app polls that URL outbound → appears "online"
          3 → you fire a command → phone answers → you get stdout + exit code
```

**Prereqs:** Python 3.8+ and the `cloudflared` binary (already included in
`bin/` for Linux x86-64; other platforms: links below).

---

## STEP 1 — host locally

| where | command |
|---|---|
| Sandbox / Linux / Mac | `./1_start_local.sh` |
| Termux (Android) | `pkg update && pkg install python -y` → `cd relay-kit && sh 1_start_local.sh` |
| Windows (cmd) | `set RELAY_TOKEN=d31eeff29eb82855 && set PORT=8090 && python server.py` |
| Windows (PowerShell) | `$env:RELAY_TOKEN="d31eeff29eb82855"; $env:PORT=8090; python server.py` |

Leave it running. Expect:

```
============================================================
  Shizuku Relay Server
  Listening on 0.0.0.0:8090
  RELAY_TOKEN = d31eeff29eb82855
============================================================
```

**Check it (new window/session):**

```bash
curl http://localhost:8090/api/health
# → {"ok": true, "time": 1789900000}
```

Open **http://localhost:8090/** in a browser → your landing page.
Console → **http://localhost:8090/dashboard?token=d31eeff29eb82855**

> Port busy? `PORT=8081 ./1_start_local.sh`

---

## STEP 2 — cloud tunnel (make localhost reachable from the internet)

In a **new window** (step 1 keeps running):

| where | command |
|---|---|
| Sandbox / Linux | `./2_start_tunnel.sh` |
| Termux | `pkg install cloudflared -y` → `sh 2_start_tunnel.sh` |
| Windows | download `cloudflared-windows-amd64.exe` (see below) → `cloudflared-windows-amd64.exe tunnel --url http://localhost:8090 --no-autoupdate` |

cloudflared downloads: <https://github.com/cloudflare/cloudflared/releases/latest>
(Windows: `cloudflared-windows-amd64.msi` / `.exe` · mac: `brew install cloudflared`)

Watch the log for your **public URL**:

```
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
|  https://fifty-pandas-glow-never.trycloudflare.com                                          |
+--------------------------------------------------------------------------------------------+
```

That URL is now your server, worldwide. Keep this window open — closing it kills the tunnel.

**Verify from anywhere:**

```bash
curl https://fifty-pandas-glow-never.trycloudflare.com/api/health
# → {"ok": true, ...}
```

---

## STEP 3 — connect the phone, run a command, get the response

**On the phone** (Shizuku Relay app, v1.1 or v2.0):

1. Server type: **relay client** (not "phone server")
2. Server URL: `https://fifty-pandas-glow-never.trycloudflare.com`
3. Token: `d31eeff29eb82855`
4. **Start** → then check `…/dashboard?token=d31eeff29eb82855` → device shows **online** 📱

**Run a command and GET THE RESPONSE:**

```bash
# easiest — included script:
BASE=https://fifty-pandas-glow-never.trycloudflare.com ./3_run_cmd.sh "id"
```

…or raw curl anywhere:

```bash
URL=https://fifty-pandas-glow-never.trycloudflare.com
TOK=d31eeff29eb82855

# 3a. queue the command
curl -s -X POST $URL/api/commands \
  -H "X-Relay-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"device":"*","command":"id","timeout":60}'
# → {"job_id": "4a7f…", "status": "queued", "device": "*"}

# 3b. block until the phone answers (replace job_id)
curl -s "$URL/api/commands/4a7f…/wait?t=90" -H "X-Relay-Token: $TOK"
# → {"job_id":"4a7f…","device":"sm-a346e-…","command":"id","status":"done",
#    "exit_code":0,"stdout":"uid=2000(shell) gid=2000(shell) …","stderr":"",
#    "duration_ms":104, …}
```

**Windows cmd one-liners** (modern Windows has curl):

```cmd
curl -X POST %URL%/api/commands -H "X-Relay-Token: %TOK%" -H "Content-Type: application/json" -d "{\"device\":\"*\",\"command\":\"id\"}"
curl "%URL%/api/commands/<job_id>/wait?t=90" -H "X-Relay-Token: %TOK%"
```

More useful commands to try: `uptime` · `dumpsys battery | head -20` · `getprop ro.product.model` · `pm list packages | grep camera` · `input keyevent 3` (press Home) · `settings list system | head`

---

## Troubleshooting

| symptom | fix |
|---|---|
| `curl: (7) Failed to connect` | step 1 window got closed — restart it |
| 401 error everywhere | wrong token — must match `RELAY_TOKEN` env |
| job stuck `queued` forever | phone not connected: recheck app URL+token, battery-optimisation exemption, Shizuku running |
| tunnel URL changed after restart | normal — free quick tunnels are temporary. For a FIXED address: deploy to Render (`render.yaml` included) + your domain |
| Termux tunnel dies | `termux-wake-lock`, disable battery optimization for Termux |
| `cloudflared: not found` | use the bundled one: `./bin/cloudflared tunnel --url http://localhost:8090 --no-autoupdate` |

## After the demo — go permanent (no tunnel churn)

This kit IS the production service: upload these files to a GitHub repo →
render.com → New → Blueprint → done → CNAME your domain
(`shizukumcp.freedev.app`). Full click-path is in the earlier
`shizukumcp-webhost.zip` README — same files, same token.
