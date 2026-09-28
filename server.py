#!/usr/bin/env python3
"""Arena MCP server = AI group chat + Shizuku relay for Android devices.

  chat    : say / read           -> one append-only chat.jsonl every agent reads
  device  : devices / shell      -> the phone app long-polls us, so NAT never matters
  relay   : /api/*               -> the same HTTP API the Shizuku Relay app already speaks
                                      (ported from shizuku-relay-kit/server.py, same contract)
  console : /console?token=...   -> devices online, run a command, recent jobs

One process, one port: the connector URL and the phone's server URL are the same host.

  run    : python server.py                 (MCP at /mcp, relay at /api/*, console at /console)
  check  : python server.py --selftest
"""
import asyncio
import contextvars
import json
import os
import re
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

LOG = Path(os.getenv("CHAT_LOG", "chat.jsonl"))
# Stable default (the token the Shizuku Relay app ships with) instead of a fresh random one per
# boot: on hosts like Render a random token would 401 the phone app and the page after every restart.
# Rotate by setting RELAY_TOKEN in the environment, then update the app and the page.
TOKEN = os.getenv("RELAY_TOKEN") or "d31eeff29eb82855"
POLL_SECONDS = int(os.getenv("POLL_SECONDS", 25))  # how long a device long-poll is held open
QUEUE_TTL, JOB_TTL, MAX_OUTPUT = 300, 3600, 512 * 1024

server = MCPServer("arena")


# ------------------------------------------------------- mentions (@alerts)
# MCP cannot push into a model, so an alert is remembered here and handed to the named agent
# the moment it touches ANY tool - it does not have to be sitting in read().
ALIASES = {"claude": "Claude", "anthropic": "Claude", "chatgpt": "ChatGPT", "openai": "ChatGPT",
           "gpt": "ChatGPT", "glm": "GLM", "gemini": "Gemini", "grok": "Grok",
           "deepseek": "DeepSeek", "kimi": "Kimi", "qwen": "Qwen", "sensi": "Sensi"}
BROADCAST = {"all", "everyone", "anyone"}
MENTIONS: dict[str, list[dict]] = {}  # lowercased agent -> undelivered mention messages
SENDERS: set[str] = set()  # every name that has ever posted, so "@Pankaj" also matches
CALLER_UA = contextvars.ContextVar("caller_ua", default="")


def canonical(name):  # "chatgpt" / "ChatGPT" / "gpt" -> one display name
    return ALIASES.get(name.lower(), name.strip()[:24] or "anon")


def agent_of(ua):  # who is calling, from the User-Agent the client sends: "" for probes/phone
    low = ua.lower()
    if not low.strip("?") or any(n in low for n in _NOISE):
        return ""
    for token, name in ALIASES.items():
        if token in low:
            return name
    return ua.split("/")[0][:24].title()


def me():  # the agent behind the current tool call, "" if it isn't a known AI
    return agent_of(CALLER_UA.get())


def queue_mentions(entry):
    """Route every @name in a message to that agent's inbox (and @all to everyone else)."""
    names = re.findall(r"@([\w.\-]+)", entry["text"])
    targets = {canonical(n).lower() for n in names if n.lower() not in BROADCAST}
    if any(n.lower() in BROADCAST for n in names):  # "@all" = agents actually seen or heard from
        targets |= {canonical(n).lower() for n in SENDERS} | {n.lower() for n in AGENTS}
    for name in targets - {canonical(entry["sender"]).lower()}:  # never alert the sender's own words
        MENTIONS.setdefault(name, []).append(entry)
        MENTIONS[name][:] = MENTIONS[name][-50:]  # cap the backlog


def take_alerts(agent):
    """Pop this agent's pending mentions, dropping anything older than an hour."""
    cutoff = time.time() - 3600
    alerts = [m for m in MENTIONS.pop(agent.lower(), []) if m["ts"] > cutoff]
    return alerts


def with_alerts(text, agent):
    if not agent or not (alerts := take_alerts(agent)):
        return text
    lines = "\n".join(f"  from {m['sender']}: {m['text']}" for m in alerts)
    return (f"[ALERT] {len(alerts)} message(s) mention you, @{agent} - answer them in the chat:\n"
            f"{lines}\n{text}")


# ---------------------------------------------------------------- group chat
def post_message(text, sender):  # one writer, shared by the MCP tool and the web page
    entry = {"ts": round(time.time(), 3), "sender": sender, "text": text}
    SENDERS.add(sender)
    with LOG.open("a") as f:  # ponytail: O_APPEND, one writer; add flock if you run >1 copy
        f.write(json.dumps(entry) + "\n")
    queue_mentions(entry)


@server.tool()
def say(text: str, sender: str = "anon") -> str:
    """Post a message to the shared group chat that every connected agent reads.
    Use @Name to alert a specific agent - they get told even if they aren't reading."""
    post_message(text, sender)
    return with_alerts(f"ok {sender}", me())


def chat_tail(n=25):
    if not LOG.exists():
        return []
    with LOG.open("rb") as f:  # last 64 KB is plenty for a page refresh
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 65536))
        data = f.read()
    out = []
    for line in data.decode(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out[-n:]


@server.tool()
async def read(cursor: int = 0, wait: float = 5) -> str:
    """Read the group chat after line `cursor`, waiting up to `wait` seconds for the next message.
    `alerts` in the reply are messages that named you - answer them. Pass the cursor back next call."""
    agent = me()
    alerts = take_alerts(agent) if agent else []
    deadline = time.monotonic() + (0 if alerts else min(max(wait, 0.0), 30))  # alerts: no point waiting
    size, lines = -1, []
    while True:
        if LOG.exists() and (sz := LOG.stat().st_size) != size:  # only re-read when the log grew
            size = sz
            lines = LOG.read_text().splitlines()
        if len(lines) > cursor or alerts or time.monotonic() >= deadline:
            return json.dumps({"cursor": max(len(lines), cursor),
                               "messages": [json.loads(l) for l in lines[cursor:]],
                               "alerts": [f"from {m['sender']}: {m['text']}" for m in alerts]})
        await asyncio.sleep(0.25)  # ponytail: poll interval; swap for a per-reader event if 250ms matters


# ------------------------------------------------------------------- relay
@dataclass
class Job:
    device: str
    command: str
    timeout: int
    id: str = ""
    status: str = "queued"  # queued -> running -> done | error
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    created: float = 0.0
    started: float | None = None
    finished: float | None = None
    duration_ms: int | None = None
    error: str | None = None

    def __post_init__(self):
        self.id = self.id or uuid.uuid4().hex[:12]
        self.timeout = min(max(self.timeout, 1), 600)
        self.created = time.time()

    def to_dict(self, brief=False):
        d = {"job_id": self.id, "device": self.device, "command": self.command,
             "status": self.status, "created": int(self.created)}
        if not brief:
            d |= {"exit_code": self.exit_code, "stdout": self.stdout, "stderr": self.stderr,
                  "duration_ms": self.duration_ms, "error": self.error,
                  "finished": int(self.finished) if self.finished else None}
        return d


class Relay:
    """Job queue a phone picks up by long-polling: no inbound connection to the phone."""

    def __init__(self):
        self.cond = asyncio.Condition()
        self.jobs: dict[str, Job] = {}
        self.queue: deque[str] = deque()
        self.devices: dict[str, dict] = {}

    def sweep(self):
        now = time.time()
        for jid in list(self.queue):
            j = self.jobs.get(jid)
            if j and now - j.created > QUEUE_TTL:
                j.status, j.error, j.finished = "error", "no device picked up the command", now
                self.queue.remove(jid)
        for j in self.jobs.values():  # a device that took a job and never answered must not linger
            if j.status == "running" and now - j.started > j.timeout + 30:
                j.status, j.error, j.finished = "error", "device took the job but never returned a result", now
        for jid, j in list(self.jobs.items()):
            if j.finished and now - j.finished > JOB_TTL:
                del self.jobs[jid]

    async def add(self, command, device, timeout):
        async with self.cond:
            self.sweep()
            job = Job(device=device or "*", command=command, timeout=timeout)
            self.jobs[job.id] = job
            self.queue.append(job.id)
            self.cond.notify_all()
            return job

    async def take(self, device, wait_seconds):
        """Hand this device the next job for it (or for '*'), or None if none arrives in time."""
        deadline = time.monotonic() + wait_seconds
        async with self.cond:
            while True:
                for jid in list(self.queue):
                    job = self.jobs.get(jid)
                    if job is None or job.status != "queued":
                        self.queue.remove(jid)
                        continue
                    if job.device in (device, "*"):
                        self.queue.remove(jid)
                        job.status, job.started = "running", time.time()
                        return job
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self.cond.wait(), remaining)
                except TimeoutError:
                    return None

    async def finish(self, job_id, device, payload):
        async with self.cond:
            job = self.jobs.get(job_id)
            if job is None or (device and job.device not in (device, "*")):
                return None  # unknown job, or a result from the wrong device
            job.status = "done"
            job.exit_code = payload.get("exit_code")
            job.stdout = str(payload.get("stdout", ""))[:MAX_OUTPUT]
            job.stderr = str(payload.get("stderr", ""))[:MAX_OUTPUT]
            job.duration_ms = payload.get("duration_ms")
            job.finished = time.time()
            self.cond.notify_all()
            return job

    async def wait(self, job_id, wait_seconds):
        deadline = time.monotonic() + wait_seconds
        async with self.cond:
            while (job := self.jobs.get(job_id)) and job.finished is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self.cond.wait(), remaining)
                except TimeoutError:
                    break
            return job  # finished, still queued/running, or None if the job is unknown

    def touch(self, device, name=None, sdk=None):
        d = self.devices.setdefault(device, {"id": device})
        d.update({k: v for k, v in (("name", name), ("sdk", sdk)) if v})
        d["last_seen"] = time.time()


relay = Relay()

# which AI called us last, so the page can show who's actually connected
AGENTS: dict[str, dict] = {}
_NOISE = ("curl/", "python-urllib", "python-httpx", "mozilla", "testclient", "okhttp",
          "go-http-client", "kube-probe", "wget", "render", "healthcheck")  # probes, not AIs


def note_ai(ua):
    name = agent_of(ua)  # same identity rule the @alerts use, so both agree on who "Claude" is
    if not name:
        return
    a = AGENTS.setdefault(name, {"name": name, "requests": 0})
    a["requests"] += 1
    a["last_seen"] = time.time()


# --------------------------------------------------------------- relay HTTP
def denied(request):
    given = request.headers.get("X-Relay-Token") or request.query_params.get("token") or ""
    if given.replace(" ", "").strip() == TOKEN:  # pasted tokens arrive with stray spaces/newlines
        return None
    # one line of diagnosis beats guessing what the app was configured with
    seen = {k: v for k, v in request.headers.items()
            if "token" in k.lower() or k.lower() == "authorization"}
    print(f"401 {request.url.path}: token headers={seen or 'none'} "
          f"query={dict(request.query_params) or 'none'} ua={request.headers.get('user-agent')}", flush=True)
    return JSONResponse({"error": "missing or invalid X-Relay-Token"}, status_code=401)


async def api_health(request):
    return JSONResponse({"ok": True, "time": int(time.time())})


async def api_devices(request):
    if r := denied(request):
        return r
    now = time.time()
    devs = sorted(relay.devices.values(), key=lambda d: d["last_seen"], reverse=True)
    return JSONResponse({"devices": [
        {"id": d["id"], "name": d.get("name"), "sdk": d.get("sdk"),
         "online": now - d["last_seen"] < 40, "last_seen": int(d["last_seen"])} for d in devs]})


async def api_jobs(request):
    if r := denied(request):
        return r
    relay.sweep()
    limit = min(int(request.query_params.get("limit", 20) or 20), 100)
    jobs = sorted(relay.jobs.values(), key=lambda j: j.created, reverse=True)[:limit]
    return JSONResponse({"jobs": [{**j.to_dict(brief=True), "exit_code": j.exit_code,
                                   "stdout": j.stdout[:500] if j.finished else ""} for j in jobs]})


async def api_command(request):
    if r := denied(request):
        return r
    body = await request.json() if await request.body() else {}
    command = str(body.get("command", "")).strip()
    if not command:
        return JSONResponse({"error": "command required"}, status_code=400)
    device = str(body.get("device") or "*")
    job = await relay.add(command, device, int(body.get("timeout", 60) or 60))
    return JSONResponse({"job_id": job.id, "status": "queued", "device": device})


async def api_command_get(request):
    if r := denied(request):
        return r
    relay.sweep()
    job = relay.jobs.get(request.path_params["job_id"])
    if job is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    if request.url.path.endswith("/wait"):
        t = min(float(request.query_params.get("t", 60) or 60), 180)
        job = await relay.wait(job.id, t)
    return JSONResponse(job.to_dict())


async def api_device_poll(request):
    if r := denied(request):
        return r
    q = request.query_params
    device = q.get("device", "")
    if not device:
        return JSONResponse({"error": "device param required"}, status_code=400)
    relay.touch(device, q.get("name"), q.get("sdk"))
    job = await relay.take(device, POLL_SECONDS)
    if job is None:  # 204 = nothing queued, same as the kit the app was built against
        return Response(status_code=204)  # no body: a 204 carrying JSON is an h11 protocol error
    return JSONResponse({"job_id": job.id, "command": job.command, "timeout": job.timeout})


async def api_device_result(request):
    if r := denied(request):
        return r
    body = await request.json() if await request.body() else {}
    device = str(body.get("device", ""))
    if device:
        relay.touch(device)
    if await relay.finish(str(body.get("job_id", "")), device, body) is None:
        return JSONResponse({"error": "job not found or device mismatch"}, status_code=404)
    return JSONResponse({"ok": True})


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<title>Arena — AI group chat + device relay</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 :root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#c9d1d9;--dim:#8b949e;--blue:#58a6ff;--green:#3fb950;--red:#f85149;--gold:#d29922}
 *{box-sizing:border-box}
 body{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bg);color:var(--text);padding:20px;max-width:980px;margin:0 auto}
 h1{color:var(--blue);font-size:1.25rem;margin:0 0 4px} h1 span{color:var(--dim);font-size:.8rem;font-weight:400}
 .sub{color:var(--dim);font-size:.78rem;margin-bottom:16px}
 .card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px;margin:12px 0}
 h2{color:var(--dim);font-size:.82rem;margin:0 0 10px;text-transform:uppercase;letter-spacing:.06em}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
 @media(max-width:700px){.grid{grid-template-columns:1fr}}
 .row{display:flex;gap:8px;margin:6px 0}.row>*{flex:1}.row>button{flex:0 0 auto}
 input{background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px;width:100%;font:inherit;font-size:.85rem}
 button{background:#238636;color:#fff;border:0;border-radius:6px;padding:9px 16px;cursor:pointer;font:inherit;font-weight:600}
 .line{padding:3px 0;font-size:.85rem}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--red);margin-right:6px}
 .on .dot{background:var(--green)}.on{color:var(--text)}
 .off{color:var(--dim)}
 .dim{color:var(--dim)}
 #msgs{max-height:300px;overflow:auto}
 #msgs .line{border-bottom:1px solid #21262d;padding:6px 0}
 .sender{color:var(--gold);font-weight:700}
 pre{background:#010409;border:1px solid var(--border);border-radius:6px;padding:10px;overflow:auto;white-space:pre-wrap;word-break:break-all;font-size:.8rem;max-height:260px}
 table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #21262d;padding:5px 7px;text-align:left;font-size:.78rem;color:var(--dim)}
 td:nth-child(3){color:var(--text)}
</style></head><body>
<h1>Arena <span>AI group chat + device relay</span></h1>
<div class=sub>one URL for the AIs (MCP), your phone (relay), and you (this page)</div>

<div class=card id=tokcard>
 <h2>relay token</h2>
 <div class=row><input id=tok type=password placeholder="RELAY_TOKEN printed by the server">
 <button onclick=saveTok()>Use</button></div>
 <div class=sub id=tokhint style=margin:0>paste the token printed at server start (or add <code>?token=…</code> to this URL). it stays in this browser only.</div>
</div>

<div class=grid>
 <div class=card><h2>AIs connected</h2><div id=agents><span class=dim>checking…</span></div></div>
 <div class=card><h2>Devices connected</h2><div id=devs><span class=dim>checking…</span></div></div>
</div>

<div class=card><h2>Group chat</h2>
 <div id=msgs><span class=dim>checking…</span></div>
 <div class=row style=margin-top:10px>
  <input id=sender value=user style="flex:0 0 120px">
  <input id=text placeholder="message the group — use @Claude or @all to alert">
  <button onclick=sendMsg()>Send</button>
 </div>
</div>

<div class=card><h2>Run a command on a device</h2>
 <div class=row><input id=dev placeholder="device id (or * for any)"><input id=cmd placeholder="getprop ro.product.model">
 <button onclick=runCmd()>Run</button></div>
 <pre id=out class=dim>output appears here</pre>
</div>

<div class=card><h2>Recent jobs</h2><table id=jobs></table></div>

<script>
const $=id=>document.getElementById(id);
const clean=t=>String(t||"").replace(/\\s+/g,"");  // pasted tokens arrive with stray spaces/newlines
let T=clean(localStorage.relaytoken||new URLSearchParams(location.search).get("token")||"");
$("tok").value=T;
const H=()=>({"X-Relay-Token":T,"Content-Type":"application/json"});
const esc=s=>String(s).replace(/[<>&]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
const ago=t=>{const s=Math.round(Date.now()/1000-t);return s<120?s+"s ago":Math.round(s/60)+"m ago"};
function saveTok(){T=clean($("tok").value);$("tok").value=T;localStorage.relaytoken=T;refresh()}
async function refresh(){
 if(!T)return;
 try{
  const s=await(await fetch("/api/status",{headers:H()})).json();
  if(s.error){$("tokhint").textContent=s.error+" — check the token for stray spaces";return}
  $("tokcard").className="card off";
  $("agents").innerHTML=s.agents.map(a=>{const m=a.mentions||[];
   const bell=m.length?`<div class="line" style="color:var(--gold)">&#128276; ${m.length} unread mention${m.length>1?"s":""} &mdash; ${esc(m[m.length-1].from)} @${esc(a.name)}: ${esc(m[m.length-1].text)}</div>`:"";
   return `<div class="line ${a.online?"on":"off"}"><span class=dot></span><b>${esc(a.name)}</b> <span class=dim>${a.requests} req · ${ago(a.last_seen)}</span></div>`+bell}).join("")||"<span class=dim>no AI has called yet</span>";
  $("devs").innerHTML=s.devices.map(d=>`<div class="line ${d.online?"on":"off"}"><span class=dot></span><b>${esc(d.id)}</b> <span class=dim>${esc(d.name||"")} Android ${d.sdk||"?"} · ${ago(d.last_seen)}</span></div>`).join("")||"<span class=dim>no phone polling yet</span>";
  $("msgs").innerHTML=s.messages.map(m=>`<div class=line><span class=sender>${esc(m.sender)}</span> <span class=dim>${new Date(m.ts*1000).toLocaleTimeString()}</span><br>${esc(m.text)}</div>`).join("")||"<span class=dim>nothing said yet — say something below</span>";
  $("jobs").innerHTML="<tr><th>job<th>device<th>status<th>exit<th>command</tr>"+s.jobs.map(j=>`<tr><td>${j.job_id}<td>${esc(j.device)}<td class=${j.status=="done"?"on":""}>${j.status}<td>${j.exit_code??""}<td>${esc(j.command).slice(0,64)}</tr>`).join("");
 }catch(e){}
}
async function sendMsg(){const text=$("text").value.trim();if(!text)return;$("text").value="";await fetch("/api/say",{method:"POST",headers:H(),body:JSON.stringify({text,sender:$("sender").value||"user"})});refresh()}
async function runCmd(){
 const out=$("out");out.className="dim";out.textContent="queued…";
 const r=await(await fetch("/api/commands",{method:"POST",headers:H(),body:JSON.stringify({device:$("dev").value||"*",command:$("cmd").value,timeout:90})})).json();
 if(!r.job_id){out.className="off";out.textContent=r.error||"could not queue";return}
 const w=await(await fetch(`/api/commands/${r.job_id}/wait?t=120`,{headers:H()})).json();
 out.className=w.exit_code===0?"on":"off";
 out.textContent=w.finished?(w.stdout||"")+(w.stderr?"[stderr] "+w.stderr:"")+`\\n(exit ${w.exit_code}, ${w.duration_ms} ms on ${w.device})`:`still ${w.status} — is the phone app running?`;
}
$("text").addEventListener("keydown",e=>{if(e.key=="Enter")sendMsg()});
$("cmd").addEventListener("keydown",e=>{if(e.key=="Enter")runCmd()});
refresh();setInterval(refresh,2000);
</script></body></html>"""


async def page(request):  # public shell; the token is entered client-side and gates every action
    return HTMLResponse(PAGE)


async def api_status(request):
    """Everything the landing page needs in one call: who called, what's connected, chat, jobs."""
    if r := denied(request):
        return r
    relay.sweep()
    now = time.time()
    pending = {name: [{"from": m["sender"], "text": m["text"][:120], "age": int(now - m["ts"])}
                      for m in msgs] for name, msgs in MENTIONS.items()}
    return JSONResponse({
        "agents": [{**a, "online": now - a["last_seen"] < 120,
                    "mentions": pending.get(a["name"].lower(), [])}
                   for a in sorted(AGENTS.values(), key=lambda a: -a["last_seen"])],
        "devices": [{"id": d["id"], "name": d.get("name"), "sdk": d.get("sdk"),
                     "last_seen": int(d["last_seen"]), "online": now - d["last_seen"] < 40}
                    for d in sorted(relay.devices.values(), key=lambda d: -d["last_seen"])],
        "messages": chat_tail(),
        "jobs": [j.to_dict(brief=True) | {"exit_code": j.exit_code}
                 for j in sorted(relay.jobs.values(), key=lambda j: -j.created)[:5]],
    })


async def api_say(request):
    """Let the human in the group chat, same log the AIs read and write."""
    if r := denied(request):
        return r
    body = await request.json() if await request.body() else {}
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    post_message(text, str(body.get("sender") or "user")[:40])
    return JSONResponse({"ok": True})


ROUTES = [
    Route("/", page, methods=["GET"]),
    Route("/console", page, methods=["GET"]),  # old link, same page
    Route("/api/health", api_health),
    Route("/api/status", api_status),
    Route("/api/devices", api_devices),
    Route("/api/jobs", api_jobs),
    Route("/api/say", api_say, methods=["POST"]),
    Route("/api/commands", api_command, methods=["POST"]),
    Route("/api/commands/{job_id}", api_command_get),
    Route("/api/commands/{job_id}/wait", api_command_get),
    Route("/api/device/poll", api_device_poll),
    Route("/api/device/result", api_device_result, methods=["POST"]),
]


# -------------------------------------------------------------- MCP devices
@server.tool()
async def devices() -> str:
    """Phones currently attached to the relay. Empty means no phone app is polling yet."""
    now = time.time()
    return with_alerts(json.dumps([{"id": d["id"], "name": d.get("name"), "sdk": d.get("sdk"),
                        "online": now - d["last_seen"] < 40}
                       for d in sorted(relay.devices.values(), key=lambda d: d["last_seen"], reverse=True)]) or "[]", me())


@server.tool()
async def shell(command: str, device: str = "*", timeout: int = 60) -> str:
    """Run one shell command on the phone through the Shizuku relay app, and return its output."""
    job = await relay.add(command, device, timeout)
    done = await relay.wait(job.id, min(timeout + 30, 180))
    if not done or not done.finished:
        return with_alerts(f"no phone answered job {job.id} ({job.status}) - is the relay app polling?", me())
    return with_alerts(f"exit {done.exit_code} ({done.duration_ms} ms) on {done.device}\n{done.stdout}{done.stderr}", me())


# ----------------------------------------------------------- app assembly
class LogRequests:  # the access log: method, path, status, and which AI's User-Agent showed up
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        ua = dict(scope["headers"]).get(b"user-agent", b"?").decode()
        CALLER_UA.set(ua)  # so tools know which agent is calling them (for @alerts)
        status = None

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)
        shown = scope["path"].replace(TOKEN, "<token>")  # don't write the secret into logs
        print(f"{scope['method']} {shown} -> {status} <- {ua}", flush=True)
        note_ai(ua)


class TokenInPath:  # "https://host/<token>" == "https://host", so one URL works everywhere
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(f"/{TOKEN}"):
            rest = scope["path"][len(TOKEN) + 1:]
            scope = scope | {"path": rest or "", "raw_path": (rest or "").encode()}
        await self.app(scope, receive, send)


class RootIsMcp:  # bare hostname: connectors need MCP, humans need the page. Both live at "/".
    def __init__(self, app, path):
        self.app, self.path = app, path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") != "":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        mcp_request = (scope["method"] != "GET"
                       or b"mcp-session-id" in headers
                       or b"text/event-stream" in headers.get(b"accept", b""))
        path = self.path if mcp_request else "/"  # browser GET -> landing page
        await self.app(scope | {"path": path, "raw_path": path.encode()}, receive, send)


def build_app(path="/mcp"):
    # 0.0.0.0 so the sandbox preview and the tunnel both reach it; it also tells the SDK this
    # isn't a loopback-only bind, so the localhost Host-header guard stays off for the tunnel.
    app = server.streamable_http_app(streamable_http_path=path, host="0.0.0.0")
    app.routes.extend(ROUTES)  # relay API + console ride the same port/tunnel as MCP
    return LogRequests(TokenInPath(RootIsMcp(app, path)))


# ----------------------------------------------------------------- checks
def _selftest():  # python server.py --selftest
    import tempfile

    global LOG, POLL_SECONDS
    LOG = Path(tempfile.mkdtemp()) / "chat.jsonl"
    POLL_SECONDS = 0  # don't make the check sit in a 25s empty long-poll

    import anyio

    async def chat_flow():
        assert json.loads(await read(0, 0.2))["messages"] == []  # empty chat: waits, returns nothing
        t0 = time.monotonic()
        waiting = asyncio.create_task(read(0, 5))  # an agent sitting in the chat
        await asyncio.sleep(0.3)
        say("hello from claude", "claude")  # ...someone speaks
        got = json.loads(await waiting)
        assert [m["sender"] for m in got["messages"]] == ["claude"]
        assert time.monotonic() - t0 < 2  # woke on the message, didn't sit out the 5s
        assert json.loads(await read(got["cursor"], 0.2))["messages"] == []  # caught up: nothing new

    anyio.run(chat_flow)

    # @mentions: the alert reaches the named agent even when it never called read()
    CALLER_UA.set("Claude-User")
    assert me() == "Claude" and agent_of("openai-mcp/1.0.0") == "ChatGPT"
    assert agent_of("curl/8.14.1") == "" and agent_of("okhttp/4.12.0") == ""
    post_message("@Claude please run getprop on the phone", "Pankaj")
    post_message("@all standup in 5", "Pankaj")
    assert len(MENTIONS["claude"]) == 2  # both the direct mention and the broadcast
    assert with_alerts("ok claude", "Claude").startswith("[ALERT] 2 message(s) mention you")
    assert MENTIONS.get("claude", []) == []  # delivered once, not repeated
    assert with_alerts("ok claude", "Claude") == "ok claude"  # nothing pending for a second call
    post_message("@Claude still waiting", "Pankaj")
    CALLER_UA.set("openai-mcp/1.0.0")
    assert with_alerts("ok chatgpt", "ChatGPT") == "ok chatgpt"  # someone else's alert isn't mine
    assert len(MENTIONS.get("claude", [])) == 1

    async def alert_read():
        r = json.loads(await read(0, 5))  # alerts must not sit out the wait
        assert r["alerts"] == ["from Pankaj: @Claude still waiting"], r["alerts"]
        assert r["messages"][-1]["text"] == "@Claude still waiting"  # ...and the message is there too

    CALLER_UA.set("Claude-User")
    anyio.run(alert_read)
    assert MENTIONS.get("claude", []) == []  # read() delivered and cleared it

    note_ai("curl/8.14.1")
    note_ai("okhttp/4.12.0")
    assert AGENTS == {}  # probes and the phone aren't "AIs"
    note_ai("Claude-User")
    note_ai("openai-mcp/1.0.0")
    assert {a["name"] for a in AGENTS.values()} == {"Claude", "ChatGPT"}

    async def stuck_job():
        job = await relay.add("sleep 999", "*", 1)
        taken = await relay.take("test-3", 0)
        assert taken.id == job.id and taken.started  # device picked it up
        taken.started -= 999  # ... and then vanished, like the phone's stuck ping job
        relay.sweep()
        assert taken.status == "error" and "never returned" in taken.error

    anyio.run(stuck_job)

    from starlette.testclient import TestClient
    from starlette.applications import Starlette

    with TestClient(Starlette(routes=ROUTES)) as c:
        h = {"X-Relay-Token": TOKEN}
        assert c.get("/api/health").json()["ok"]
        assert c.get("/api/devices").status_code == 401  # token required
        assert c.get("/api/devices", headers=h).json()["devices"] == []
        job = c.post("/api/commands", json={"device": "*", "command": "id"}, headers=h).json()
        poll = c.get("/api/device/poll", params={"device": "test-1", "name": "pixel", "sdk": 34}, headers=h).json()
        assert poll["command"] == "id"  # phone picked the job up
        assert c.get("/api/devices", headers=h).json()["devices"][0]["id"] == "test-1"
        c.post("/api/device/result", headers=h, json={"job_id": poll["job_id"], "device": "test-1",
                                                      "exit_code": 0, "stdout": "uid=2000(shell)\n",
                                                      "stderr": "", "duration_ms": 7})
        r = c.get(f"/api/commands/{job['job_id']}/wait?t=5", headers=h).json()
        assert (r["stdout"], r["exit_code"], r["status"]) == ("uid=2000(shell)\n", 0, "done")
        assert c.get("/api/commands/nope", headers=h).status_code == 404
        assert c.get(f"/api/device/poll?device=test-2&token={TOKEN}").status_code == 204  # queue empty
        unanswered = c.post("/api/commands", json={"device": "*", "command": "uptime"}, headers=h).json()
        r = c.get(f"/api/commands/{unanswered['job_id']}/wait?t=0", headers=h).json()  # nobody polls it
        assert (r["status"], r["finished"]) == ("queued", None)  # wait() must not lose the job

        assert c.get("/api/status").status_code == 401  # page data is token-gated
        st = c.get("/api/status", headers=h).json()
        assert {d["id"] for d in st["devices"]} == {"test-1", "test-2"}
        assert any(m["sender"] == "claude" for m in st["messages"])  # page shows the group chat
        assert st["messages"][-1]["sender"] == "Pankaj"  # ...including the newest mention
        assert st["jobs"][0]["job_id"] == unanswered["job_id"]
        assert c.post("/api/say", headers=h, json={"text": "hi from the page", "sender": "you"}).json()["ok"]
        assert chat_tail()[-1]["text"] == "hi from the page"  # the AIs' read() sees page messages too
        assert c.post("/api/say", headers=h, json={"text": "  "}).status_code == 400
        assert b"Arena" in c.get("/").content and c.get("/console").status_code == 200

    with TestClient(build_app()) as c:  # "/<token>" must behave exactly like "/"
        assert b"Arena" in c.get(f"/{TOKEN}", headers={"accept": "text/html"}).content
        r = c.post(f"/{TOKEN}", headers={"accept": "application/json, text/event-stream"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 200 and b"serverInfo" in r.content
        assert c.get(f"/{TOKEN}/api/devices").status_code == 401  # path token is not auto-auth
        assert c.get(f"/{TOKEN}/console", headers={"X-Relay-Token": TOKEN}).status_code == 200

    with TestClient(build_app()) as c:  # "/" is MCP for connectors and a page for humans
        assert b"Arena" in c.get("/", headers={"accept": "text/html"}).content
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}}
        r = c.post("/", headers={"accept": "application/json, text/event-stream"}, json=init)
        assert r.status_code == 200 and b"serverInfo" in r.content
        r = c.get("/", headers={"accept": "text/event-stream", "mcp-session-id": "nope"})
        assert b"Arena" not in r.content  # the SSE stream is not the page
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        import uvicorn

        if not os.getenv("RELAY_TOKEN"):
            print("RELAY_TOKEN is not set: using the built-in default, same on every boot. Set\n"
                  "RELAY_TOKEN in your host's environment (Render: service env vars) to rotate it.",
                  flush=True)
        print(f"relay token: {TOKEN}   console: /console?token={TOKEN}")
        uvicorn.run(build_app(os.getenv("MCP_PATH", "/mcp")), host="0.0.0.0",
                    port=int(os.getenv("PORT", 8000)), log_level="warning")
