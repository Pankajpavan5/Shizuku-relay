#!/usr/bin/env python3
"""
Shizuku Relay Server
====================
A small single-file HTTP relay that connects an internet agent (anything that
can make HTTP requests) to an Android phone running the "Shizuku Relay" app.

    Internet agent --HTTP--> this server <--long-poll-- Android app (Shizuku)

Endpoints
---------
Agent side (header: X-Relay-Token):
  GET  /api/devices                    List known devices
  POST /api/commands                   {"device": "id-or-*", "command": "...", "timeout": 60}
  GET  /api/commands/<job_id>          Job status / result
  GET  /api/commands/<job_id>/wait?t=60  Block until job finishes (or t seconds)
  GET  /api/jobs?limit=20              Recent jobs

Device side (header: X-Relay-Token, called by the Android app):
  GET  /api/device/poll?device=<id>&name=<model>&sdk=<int>   Long-poll (25s) for next job
  POST /api/device/result              {"job_id": "...", "device": "...", "exit_code": 0,
                                        "stdout": "...", "stderr": "...", "duration_ms": 123}

Misc:
  GET  /            HTML dashboard (status, token, live jobs, quick-exec form)
  GET  /api/health  {"ok": true}

No dependencies beyond the Python standard library. Deploy anywhere:
    RELAY_TOKEN=mysecret PORT=8080 python3 server.py
"""
import json
import os
import secrets
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

TOKEN = os.environ.get("RELAY_TOKEN") or secrets.token_hex(8)
PORT = int(os.environ.get("PORT", "8080"))
POLL_SECONDS = 25          # long-poll duration for devices
MAX_OUTPUT = 512 * 1024    # cap stdout/stderr per job
JOB_TTL = 3600             # keep results for 1 hour
MAX_BODY = 4 * 1024 * 1024  # reject POST bodies larger than this
DEVICE_TTL = 86400         # drop devices silent for 24h (they re-register on next poll)
RUN_GRACE = 30             # grace seconds beyond job.timeout before a running job times out

# Landing page served at "/" (the public website). Dashboard lives at /dashboard.
_SITE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
def _load_site():
    try:
        with open(_SITE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None
SITE_HTML = _load_site()


class Job:
    __slots__ = ("id", "device", "command", "timeout", "status", "stdout",
                 "stderr", "exit_code", "created", "delivered", "finished",
                 "duration_ms", "error")

    def __init__(self, device, command, timeout):
        self.id = uuid.uuid4().hex[:12]
        self.device = device
        self.command = command
        self.timeout = min(max(int(timeout or 60), 1), 600)
        self.status = "queued"      # queued -> running -> done | error | timeout
        self.stdout = ""
        self.stderr = ""
        self.exit_code = None
        self.created = time.time()
        self.delivered = None
        self.finished = None
        self.duration_ms = None
        self.error = None

    def to_dict(self, brief=False):
        d = {
            "job_id": self.id,
            "device": self.device,
            "command": self.command,
            "status": self.status,
            "created": int(self.created),
        }
        if not brief:
            d.update({
                "exit_code": self.exit_code,
                "stdout": self.stdout,
                "stderr": self.stderr,
                "duration_ms": self.duration_ms,
                "error": self.error,
                "finished": int(self.finished) if self.finished else None,
            })
        return d


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)      # signals new/changed jobs
        self.jobs = {}                                   # job_id -> Job
        self.queue = deque()                             # job_ids not yet delivered
        self.devices = {}                                # device_id -> meta dict

    def add_job(self, device, command, timeout):
        with self.cond:
            job = Job(device, command, timeout)
            self.jobs[job.id] = job
            self.queue.append(job.id)
            self.cond.notify_all()
            return job

    def take_job(self, device_id, wait_seconds):
        """Long-poll: return a Job for this device (or '*'), or None on timeout."""
        deadline = time.time() + wait_seconds
        with self.cond:
            while True:
                for jid in list(self.queue):
                    job = self.jobs.get(jid)
                    if job is None or job.status != "queued":
                        self.queue.remove(jid)
                        continue
                    if job.device in (device_id, "*"):
                        self.queue.remove(jid)
                        job.status = "running"
                        job.delivered = time.time()
                        self.cond.notify_all()
                        return job
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self.cond.wait(timeout=remaining)

    def finish_job(self, job_id, device_id, payload):
        with self.cond:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if device_id and job.device not in (device_id, "*"):
                # anti-confusion: ignore results from the wrong device
                return None
            job.status = "done"   # executed; success is conveyed via exit_code
            job.exit_code = payload.get("exit_code")
            job.stdout = str(payload.get("stdout", ""))[:MAX_OUTPUT]
            job.stderr = str(payload.get("stderr", ""))[:MAX_OUTPUT]
            job.duration_ms = payload.get("duration_ms")
            job.finished = time.time()
            self.cond.notify_all()
            return job

    def wait_job(self, job_id, wait_seconds):
        deadline = time.time() + wait_seconds
        with self.cond:
            while True:
                job = self.jobs.get(job_id)
                if job is None:
                    return None
                if job.finished:
                    return job
                remaining = deadline - time.time()
                if remaining <= 0:
                    return job
                self.cond.wait(timeout=remaining)

    def touch_device(self, device_id, meta):
        with self.lock:
            d = self.devices.setdefault(device_id, {"id": device_id, "first_seen": time.time()})
            d.update(meta)
            d["last_seen"] = time.time()

    def reap(self):
        """Expire stale queued jobs, time out orphaned running jobs,
        drop old finished jobs and devices that went silent."""
        with self.cond:
            now = time.time()
            for jid in list(self.queue):
                job = self.jobs.get(jid)
                if job and now - job.created > 300:
                    job.status = "error"
                    job.error = "no device picked up the command within 5 minutes"
                    job.finished = now
                    self.queue.remove(jid)   # deque has remove(), not discard()
            for jid, job in list(self.jobs.items()):
                if job.finished and now - job.finished > JOB_TTL:
                    del self.jobs[jid]
                elif (job.status == "running" and job.delivered
                        and now - job.delivered > job.timeout + RUN_GRACE):
                    job.status = "timeout"
                    job.error = "device took the command but never returned a result"
                    job.finished = now
            for did, meta in list(self.devices.items()):
                if now - meta.get("last_seen", 0) > DEVICE_TTL:
                    del self.devices[did]
            self.cond.notify_all()


STATE = State()


def _num(qs, key, default, lo, hi):
    """Best-effort numeric query param, clamped; falls back to default."""
    try:
        v = float(qs.get(key, [default])[0] or default)
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi)


def reaper_loop():
    while True:
        time.sleep(30)
        try:
            STATE.reap()
        except Exception as exc:            # never let the sweeper die silently
            print(f"[reaper] {exc!r}", flush=True)


DASHBOARD = """<!doctype html>
<html><head><meta charset="utf-8"><title>Shizuku Relay</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#0d1117;color:#c9d1d9;margin:0;padding:24px;max-width:960px}
 h1{color:#58a6ff;font-size:1.3rem} h2{color:#8b949e;font-size:1rem;margin-top:28px}
 code{background:#161b22;padding:2px 6px;border-radius:4px;color:#79c0ff}
 .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin:12px 0}
 input,select{background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:8px;width:100%%;box-sizing:border-box}
 button{background:#238636;color:#fff;border:0;border-radius:6px;padding:9px 18px;cursor:pointer;font-weight:600}
 .row{display:flex;gap:8px;margin:8px 0}.row>*{flex:1}
 pre{background:#010409;border:1px solid #30363d;border-radius:6px;padding:12px;overflow:auto;white-space:pre-wrap;word-break:break-all}
 .ok{color:#3fb950}.bad{color:#f85149}.dim{color:#8b949e}.tok{color:#d29922;font-weight:700}
 table{border-collapse:collapse;width:100%%}td,th{border-bottom:1px solid #21262d;padding:6px 8px;text-align:left;font-size:.85rem}
</style></head><body>
<h1>⚡ Shizuku Relay Server</h1>
<div class="card">
  <div>Relay auth token (use as <code>X-Relay-Token</code> header or in the Android app):</div>
  <div style="margin-top:8px">token = <span class="tok" id="tok">__TOKEN__</span>
  <button style="padding:4px 10px;margin-left:10px" onclick="navigator.clipboard.writeText(document.getElementById('tok').textContent)">copy</button></div>
  <div class="dim" style="margin-top:8px">Enter this same server URL + token + a device name in the Android app.</div>
</div>
<div class="card"><h2 style="margin-top:0">Devices</h2><div id="devices" class="dim">none yet — start the app on your phone</div></div>
<div class="card"><h2 style="margin-top:0">Run a command (agent quick test)</h2>
 <div class="row"><input id="dev" placeholder="device id (or * for any)"><input id="cmd" placeholder="shell command, e.g. pm list packages | grep camera"><button style="flex:0 0 auto" onclick="run()">Run ▶</button></div>
 <pre id="out" class="dim">output will appear here…</pre></div>
<div class="card"><h2 style="margin-top:0">Recent jobs</h2><table id="jobs"><tr><th>id</th><th>device</th><th>status</th><th>exit</th><th>command</th></tr></table></div>
<div class="card"><h2 style="margin-top:0">Agent API cheat-sheet</h2><pre id="cheat"></pre></div>
<script>
const T = "__TOKEN__", H = {"X-Relay-Token": T, "Content-Type": "application/json"};
async function refresh(){
  try{
    const dv = await (await fetch("/api/devices",{headers:H})).json();
    const el = document.getElementById("devices");
    if(dv.devices.length===0){el.textContent="none yet — start the app on your phone";el.className="dim";}
    else el.innerHTML = dv.devices.map(d=>`<div>📱 <b>${d.id}</b> <span class="dim">${d.name||""} · Android ${d.sdk||"?"} · last seen ${Math.round(Date.now()/1000-d.last_seen)}s ago · <span class="ok">online${Date.now()/1000-d.last_seen>40?"? (stale)":""}</span></span></div>`).join("");
    const jb = await (await fetch("/api/jobs?limit=15",{headers:H})).json();
    document.getElementById("jobs").innerHTML="<tr><th>id</th><th>device</th><th>status</th><th>exit</th><th>command</th></tr>"+
      jb.jobs.map(j=>`<tr><td>${j.job_id}</td><td>${j.device}</td><td class="${j.status==='done'?'ok':''}">${j.status}</td><td>${j.exit_code??""}</td><td>${j.command.slice(0,60)}</td></tr>`).join("");
  }catch(e){}
}
async function run(){
  const dev=document.getElementById("dev").value||"*", cmd=document.getElementById("cmd").value;
  if(!cmd)return; const out=document.getElementById("out"); out.textContent="queued…"; out.className="dim";
  const r = await (await fetch("/api/commands",{method:"POST",headers:H,body:JSON.stringify({device:dev,command:cmd,timeout:90})})).json();
  const w = await (await fetch(`/api/commands/${r.job_id}/wait?t=120`,{headers:H})).json();
  out.className = w.exit_code===0?"ok":"bad";
  out.textContent = w.finished ? (w.stdout || "") + (w.stderr ? "\\n[stderr] "+w.stderr : "") + `\\n(exit ${w.exit_code}, ${w.duration_ms} ms)` : ("still "+w.status+" — phone offline?");
}
document.getElementById("cheat").textContent =
`curl -H "X-Relay-Token: ${document.getElementById('tok')?.textContent||T}" ${location.origin}/api/devices
curl -X POST -H "X-Relay-Token: ${T}" -H "Content-Type: application/json" \\
     -d '{"device":"*","command":"input keyevent 3"}' ${location.origin}/api/commands
curl -H "X-Relay-Token: ${T}" ${location.origin}/api/commands/<job_id>/wait?t=60`;
refresh(); setInterval(refresh, 2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ShizukuRelay/1.0"

    # ---------- helpers ----------
    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        if code == 204:
            body = b""          # 204 No Content must not carry a body
        else:
            body = raw.encode() if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        """Parse the JSON body. Returns None when it must be rejected (too large)."""
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > MAX_BODY:
            self.close_connection = True    # unread bytes would corrupt the next request
            return None
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def _auth(self, qs):
        tok = self.headers.get("X-Relay-Token") or qs.get("token", [None])[0]
        return bool(tok) and secrets.compare_digest(tok, TOKEN)

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}", flush=True)

    # ---------- routing ----------
    def _route(self, fn):
        """One bad request must not silently kill the connection with no response."""
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.log_error("%s %s failed: %r", self.command, self.path, exc)
            try:
                self._send(500, {"error": "internal server error"})
            except Exception:
                pass

    def do_GET(self):
        self._route(self._route_GET)

    def do_POST(self):
        self._route(self._route_POST)

    def _route_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        p = u.path.rstrip("/") or "/"
        if p == "/":
            page = SITE_HTML if SITE_HTML is not None else DASHBOARD.replace("__TOKEN__", TOKEN)
            self._send(200, raw=page, ctype="text/html; charset=utf-8")
            return
        if p == "/index.html":
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/dashboard":
            # The dashboard embeds the relay token — gate it. Open as:
            #   https://your-host/dashboard?token=YOUR_RELAY_TOKEN
            if not self._auth(qs):
                self._send(401, {"error": "add ?token=YOUR_RELAY_TOKEN to open the console"})
                return
            self._send(200, raw=DASHBOARD.replace("__TOKEN__", TOKEN), ctype="text/html; charset=utf-8")
            return
        if p in ("/api/health", "/healthz"):
            self._send(200, {"ok": True, "time": int(time.time())})
            return
        if not self._auth(qs):
            self._send(401, {"error": "missing or invalid X-Relay-Token"})
            return
        if p == "/api/devices":
            with STATE.lock:
                devs = sorted(STATE.devices.values(), key=lambda d: d.get("last_seen", 0), reverse=True)
            self._send(200, {"devices": [
                {"id": d["id"], "name": d.get("name"), "sdk": d.get("sdk"),
                 "last_seen": int(d.get("last_seen", 0))} for d in devs]})
            return
        if p == "/api/jobs":
            limit = int(_num(qs, "limit", 20, 1, 100))
            with STATE.lock:
                jobs = sorted(STATE.jobs.values(), key=lambda j: j.created, reverse=True)[:limit]
            self._send(200, {"jobs": [
                {**j.to_dict(brief=True),
                 "exit_code": j.exit_code,
                 "stdout": j.stdout[:500] if j.finished else ""} for j in jobs]})
            return
        if p == "/api/device/poll":
            device = qs.get("device", [""])[0]
            if not device:
                self._send(400, {"error": "device param required"})
                return
            STATE.touch_device(device, {
                "name": qs.get("name", [None])[0],
                "sdk": qs.get("sdk", [None])[0]})
            job = STATE.take_job(device, POLL_SECONDS)
            if job is None:
                self._send(204, {})
            else:
                self._send(200, {"job_id": job.id, "command": job.command, "timeout": job.timeout})
            return
        if p.startswith("/api/commands/"):
            parts = p.split("/")
            if len(parts) >= 4:
                job_id = parts[3]
                wait = len(parts) >= 5 and parts[4] == "wait"
                if wait:
                    t = _num(qs, "t", 60, 0, 180)
                    job = STATE.wait_job(job_id, t)
                else:
                    with STATE.lock:
                        job = STATE.jobs.get(job_id)
                if job is None:
                    self._send(404, {"error": "job not found"})
                else:
                    self._send(200, job.to_dict())
                return
        self._send(404, {"error": "not found"})

    def _route_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        p = u.path.rstrip("/") or "/"
        if not self._auth(qs):
            self.close_connection = True    # body stays unread; don't parse it as a request
            self._send(401, {"error": "missing or invalid X-Relay-Token"})
            return
        if p == "/api/commands":
            b = self._body()
            if b is None:
                self._send(413, {"error": "body too large (max %d bytes)" % MAX_BODY})
                return
            cmd = str(b.get("command", "")).strip()
            if not cmd:
                self._send(400, {"error": "command required"})
                return
            device = str(b.get("device") or "*")
            job = STATE.add_job(device, cmd, b.get("timeout", 60))
            self._send(200, {"job_id": job.id, "status": "queued", "device": device})
            return
        if p == "/api/device/result":
            b = self._body()
            if b is None:
                self._send(413, {"error": "body too large (max %d bytes)" % MAX_BODY})
                return
            job_id = str(b.get("job_id", ""))
            device = str(b.get("device", ""))
            if device:
                STATE.touch_device(device, {})
            job = STATE.finish_job(job_id, device, b)
            if job is None:
                self._send(404, {"error": "job not found or device mismatch"})
            else:
                self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    threading.Thread(target=reaper_loop, daemon=True).start()
    print("=" * 60)
    print("  Shizuku Relay Server")
    print(f"  Listening on 0.0.0.0:{PORT}")
    print(f"  RELAY_TOKEN = {TOKEN}")
    if TOKEN in ("d31eeff29eb82855", "change-me"):
        print("  WARNING: RELAY_TOKEN is a known kit default — set your own secret!", flush=True)
    print("=" * 60, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
