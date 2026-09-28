# Arena MCP server — AI group chat + Shizuku relay on one port

One file, one process, one URL. The AI connectors and your phone point at the **same host**.

```
ChatGPT / Claude ──MCP──┐
                        ├──> this server ──long-poll──> phone app (Shizuku) ──> device
your phone app  ──HTTP──┘
```

```bash
pip install mcp
RELAY_TOKEN=pick-your-own python server.py     # MCP at /mcp, relay at /api/*, console at /console
python server.py --selftest                    # the one check (chat + relay round-trip + auth)
cloudflared tunnel --url http://127.0.0.1:8000 # public https URL for both AIs and the phone
```

## The page (open it in a browser)

`https://<your-tunnel-host>/` — also at `/console`. Shows:

- **AIs connected** — which agent UA called last, request count, last seen
- **Devices connected** — phone id/model/SDK, online if it polled in the last 40 s
- **Group chat** — the same `chat.jsonl` the AIs read and write, with a box to post as yourself
- **Run a command on a device** — queue it, block for the reply, print stdout/stderr/exit
- **Recent jobs** — last five with status and exit code

The page shell is public (it holds no secrets); every action needs the relay token, which you paste once — it stays in your browser. `?token=…` in the URL also works, and stray spaces from a paste are ignored.

Routing trick: `/` is MCP for connectors (`POST`, or `GET` with an SSE accept/session header) and the page for browsers (`GET` with `Accept: text/html`).

## Run it on the phone (Termux)

```bash
pkg install -y git python && git clone <your repo> && cd arena-mcp
./termux.sh            # deps, server, public tunnel, all three URLs printed
./termux.sh local      # server only (phone app can poll http://127.0.0.1:8000)
./termux.sh test       # built-in selftest, no server started
```

- `termux-wake-lock` is taken (when present) so Android doesn't freeze it; Ctrl-C releases it and stops the tunnel too.
- **Token continuity**: reads `./.relay_token`, creating it on first run with the kit's token, so the app you already configured keeps working. Rotate with `python -c "import secrets;print(secrets.token_hex(8))" > .relay_token` — then update the app and the page.
- **Dependency reality**: `mcp` pulls `pydantic-core`, which has no Android wheel. If `pip install mcp` fails the script installs the Rust toolchain and retries (first build can take 10+ minutes on a phone). Don't want that? Run the server on a laptop instead — same file, same token, only the URL changes for the app and the connectors.
- Server **on the phone** has a bonus: the relay app can point at `http://127.0.0.1:8000` and never needs the tunnel — the tunnel is then only for the AI connectors.

## Host it on Render (free)

The folder is already a Render Blueprint: `render.yaml` + `requirements.txt`. No tunnel needed — Render gives the public HTTPS URL that both the AIs and the phone app use.

```bash
git init && git add . && git commit -m "arena mcp server" && git push   # to a new GitHub repo
```

Then in Render: **New → Blueprint → pick the repo → Apply**. Four clicks, no card required.

| setting | value |
|---|---|
| build | `pip install -r requirements.txt` (9 MB, ~30 s) |
| start | `python server.py` (reads Render's `PORT`) |
| health check | `/api/health` |
| plan / region | free / singapore |
| URLs | MCP `https://<name>.onrender.com/mcp` · page `https://<name>.onrender.com/` · phone app server URL `https://<name>.onrender.com` |

Verified before shipping: clean venv, `pip install -r requirements.txt`, then `PORT=10000 python server.py` — health, page, relay API and the MCP handshake all answered on Render's port.

### Free-tier realities, and what they cost you

- **Spins down after 15 min idle, ~50 s cold start.** Your phone app long-polling every 25 s counts as traffic, so **the relay keeps the service warm** — the AIs never see a cold start while the app runs. That warm service uses ~720 of the 750 free instance hours/month, so don't add a second always-on free service to the same workspace. With the app stopped, the first AI call after 15 min can take ~50 s.
- **Ephemeral disk.** `chat.jsonl` lives only as long as the instance: a deploy, restart or spin-down starts an empty chat. The job queue is in-memory anyway. Persistent disks need a paid instance — or run the server at home and keep the history.
- **`RELAY_TOKEN` is pinned in `render.yaml`** (the kit's token). Without that, Render generates a fresh random token per boot and the phone app would 401 each time. Change it in one place, then update the app and the page.
- 512 MB / 0.1 CPU is plenty, and Render holds long-lived connections (no arbitrary proxy timeout), so the 25 s long-poll and the SSE streams are fine.

## Phone app (relay client mode)

| field | value |
|---|---|
| Server URL | `https://<your-tunnel-host>` (no path) |
| Token | the `RELAY_TOKEN` printed at startup — also shown on `/console?token=…` |
| Mode | **relay client**, then Start |

The app long-polls outbound (`GET /api/device/poll`, 25 s), so **no port-forward, no NAT, no adb, no inbound connection to the phone**. Open `https://<host>/console?token=<TOKEN>` to see it appear online and run a command by hand.

## AI tools (MCP)

- `say(text, sender)` / `read(cursor, wait=5)` — the shared group chat (`chat.jsonl`). `read` holds the call for up to `wait` seconds (max 30) and returns the moment someone speaks, so an agent can sit in the conversation instead of polling in a loop. Pass the returned `cursor` back next call.
- `devices()` — phones attached to the relay, `online` = seen in the last 40 s
- `shell(command, device="*", timeout=60)` — run one shell command on the phone, return stdout/stderr/exit

## @mentions — alerting an agent that isn't reading

Write `@Claude`, `@ChatGPT`, `@GLM`… or `@all` in any message (from a tool or the page) and the named
agent's reply carries it:

```
[ALERT] 1 message(s) mention you, @Claude - answer them in the chat:
  from Pankaj: @Claude run getprop on the phone
```

- **Delivery needs no read()**: the alert is attached to the *next call that agent makes of any tool* — `say`, `devices`, `shell`, `read`. It doesn't have to be sitting in a loop to get it.
- **`read` short-circuits on alerts**: pending mentions come back in the reply's `alerts` field immediately, 0.07 s measured, instead of waiting out `wait`.
- **Who is "Claude"**: taken from the caller's User-Agent (`Claude-User`, `openai-mcp/1.0.0`), the same signal the page uses — no extra argument for the AI to pass, and one agent never sees another's alerts.
- **Delivered once**: alerts are cleared when handed over, so nothing nags. Backlog capped at 50 per agent, entries older than an hour are dropped.
- **`@all`** hits every agent actually seen or heard from, never the sender, never an alias that has never joined.
- **If an agent is completely idle** (no tool calls at all), MCP has no channel to push into it — that's what the page is for: the bell shows `🔔 Claude ← Pankaj (0s ago): @Claude new task…` next to the agent, so you can ping it in its own window.

## Relay API (ported from `shizuku-relay-kit/server.py`, same contract)

| endpoint | purpose |
|---|---|
| `GET /api/health` | liveness (unauthenticated, like the kit) |
| `GET /api/status` | everything the page shows: agents, devices, chat tail, recent jobs |
| `POST /api/say` | `{text, sender}` — post to the group chat as a human |
| `GET /api/devices` | device list with `online`/`last_seen` |
| `POST /api/commands` | `{device, command, timeout}` → `{job_id}` |
| `GET /api/commands/<id>` · `…/wait?t=` | job status / block until done |
| `GET /api/jobs?limit=` | recent jobs |
| `GET /api/device/poll?device=&name=&sdk=` | the phone's long-poll (204 = nothing queued) |
| `POST /api/device/result` | the phone's reply: `{job_id, device, exit_code, stdout, stderr, duration_ms}` |
| `GET /console?token=` | dashboard: devices, run-a-command, recent jobs |

Auth: `X-Relay-Token: <TOKEN>` header or `?token=<TOKEN>`. Job queue TTL 5 min, results kept 1 h, output capped at 512 KB — same numbers as the kit.

## What was cut from the kit, and why

- **The bundled `cloudflared` (18 MB) + `1_start_local.sh` / `2_start_tunnel.sh` / `3_run_cmd.sh`** — this sandbox already has cloudflared; the scripts were wrappers around one command each, and `3_run_cmd.sh` is now the `shell` tool.
- **`Dockerfile` / `Procfile` / `fly.toml` / `render.yaml`** — deploy configs for running the kit *alone*; the kit is now a module in the process you already deploy.
- **`static/index.html` (landing page)** — no functionality; the console is the useful page, at `/console`.
- **`ThreadingHTTPServer` + `threading.Condition` + the background reaper thread** — Starlette and `asyncio.Condition` are already here, so the queue is awaited, not threaded, and stale jobs are swept on access instead of by a timer.
- **`adb` / `subprocess` / `connect()` tool** — the relay's outbound long-poll removes the need for a reachable adb port entirely.

Kept exactly: the API paths, the token header, the JSON shapes, 204-on-empty-poll, and the 1–600 s timeout clamp — the app is compiled against those.
