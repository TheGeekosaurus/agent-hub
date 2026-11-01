#!/usr/bin/env python3
"""Agent Hub: a read-only dashboard over brain/agents/, brain's pull requests and the video review queue.

Every folder in brain/agents/ (except _templates) is an agent. Nothing about the agents
is hardcoded here: names, runtimes, records and connection state are all read from disk
on each request, so a new agent folder shows up on the next page load.

Run: python3 server.py   (HOST/PORT/BRAIN_DIR overridable via environment)
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("HUB_HOST", "127.0.0.1")
PORT = int(os.environ.get("HUB_PORT", "45681"))
BRAIN = Path(os.environ.get("BRAIN_DIR", Path.home() / "brain")).resolve()
AGENTS_DIR = BRAIN / "agents"
HUB_DIR = Path(__file__).resolve().parent

# Activity log (agent_logs in the Geeko Supabase project). Optional: until both are set
# the Activity panel says "not connected". Read with a key that may read the table,
# never the bots' publishable key (knowledge/agent-building/activity-log.md).
LOGS_URL = os.environ.get("SUPABASE_LOGS_URL", "").rstrip("/")
LOGS_READ_KEY = os.environ.get("SUPABASE_LOGS_READ_KEY", "")

RECORD_KINDS = ("tasks", "learnings", "proposals")
STUCK_AFTER_DAYS = 14
ROUTINE_TZ = "America/Los_Angeles"  # routines.md records schedules in PT


# ── Parsing ────────────────────────────────────────────────────────────────

def parse_frontmatter(text):
    """Return (dict, body) for a markdown file with a simple YAML frontmatter block."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        val = re.sub(r"\s+#.*$", "", val).strip()  # template-style inline comments
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        else:
            val = val.strip("'\"")
        meta[key] = val
    body = text[end + 4:].lstrip("\n")
    return meta, body


def split_sections(body):
    """Map '## Heading' -> section text. Also returns the H1 title."""
    title = ""
    m = re.search(r"^# (.+)$", body, re.M)
    if m:
        title = m.group(1).strip()
    sections = {}
    parts = re.split(r"^## (.+)$", body, flags=re.M)
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].strip()] = parts[i + 1].strip()
    return title, sections


def first_paragraph(text):
    for para in re.split(r"\n\s*\n", text or ""):
        if para.strip():
            return " ".join(para.split())
    return ""


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def parse_day(val):
    try:
        return date.fromisoformat(str(val)[:10])
    except ValueError:
        return None


# ── Agents ─────────────────────────────────────────────────────────────────

def agent_dirs():
    if not AGENTS_DIR.is_dir():
        return []
    return sorted(
        d for d in AGENTS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_"))
    )


def load_records(adir, kind):
    root = adir / kind
    if not root.is_dir():
        return []
    out = []
    for f in sorted(root.rglob("*.md")):
        if any(p.startswith(".") for p in f.relative_to(root).parts):
            continue
        meta, body = parse_frontmatter(read_text(f))
        out.append({
            "path": str(f.relative_to(adir)),
            "slug": f.stem,
            "title": meta.get("title") or f.stem,
            "status": meta.get("status", ""),
            "priority": meta.get("priority", ""),
            "checkAt": meta.get("checkAt", ""),
            "createdAt": meta.get("createdAt", ""),
            "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes"),
        })
    out.sort(key=lambda r: (r["createdAt"] or r["modified"]), reverse=True)
    return out


# ── Routines (agents/<name>/routines.md) ───────────────────────────────────

def _cron_field(spec, lo, hi):
    """Expand one cron field ('*', '1-5', '*/15', '1,3,5') into a set of ints."""
    out = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step = part.split("/", 1)
            step = int(step)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        out.update(range(a, b + 1, step))
    return out


def next_cron_run(expr, after):
    """Next datetime (in after's tz) matching a 5-field cron expression, or None."""
    try:
        f = expr.split()
        minutes, hours = _cron_field(f[0], 0, 59), _cron_field(f[1], 0, 23)
        doms, months = _cron_field(f[2], 1, 31), _cron_field(f[3], 1, 12)
        dows = {d % 7 for d in _cron_field(f[4], 0, 7)}  # 0 and 7 are Sunday
    except (IndexError, ValueError):
        return None
    dom_any, dow_any = f[2] == "*", f[4] == "*"
    day = after.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(400):
        cron_dow = (day.weekday() + 1) % 7
        dom_ok, dow_ok = day.day in doms, cron_dow in dows
        day_ok = (dom_ok and dow_ok) if (dom_any or dow_any) else (dom_ok or dow_ok)
        if day.month in months and day_ok:
            for h in sorted(hours):
                for m in sorted(minutes):
                    cand = day.replace(hour=h, minute=m)
                    if cand > after:
                        return cand
        day = (day + timedelta(days=1)).replace(hour=0, minute=0)
    return None


def parse_table(text, must_have):
    """First markdown table whose header contains `must_have` → list of {header: cell}."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("|") and must_have.lower() in line.lower() and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1]):
            heads = [h.strip() for h in line.strip("|").split("|")]
            rows = []
            for row in lines[i + 2:]:
                if not row.startswith("|"):
                    break
                cells = [c.strip() for c in row.strip("|").split("|")]
                rows.append(dict(zip(heads, cells)))
            return rows
    return []


def load_routines(adir):
    path = adir / "routines.md"
    if not path.is_file():
        return None
    meta, body = parse_frontmatter(read_text(path))
    _, sections = split_sections(body)
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(ROUTINE_TZ))
    except Exception:
        now = datetime.now()

    def col(row, *keys):
        for h, v in row.items():
            if any(k in h.lower() for k in keys):
                return v
        return ""

    routines = []
    for row in parse_table(body, "routine id"):
        name = row.get("Name", "")
        rid = col(row, "routine id").strip("`")
        sched = col(row, "cron", "schedule")
        plain = col(row, "plain")
        if " · " in sched:  # one combined column: "`cron` · weekdays 8:30 AM PT"
            combined = sched
            sched, rest = sched.split(" · ", 1)
            if not plain or plain == combined:
                plain = rest
        cron = sched.strip().strip("`")
        cron = re.sub(r"^CRON_TZ=\S+\s+", "", cron)
        status = col(row, "status")
        active = status.lower().startswith("active")

        # The routine's own section: heading starts with its name or mentions its id.
        prompt, section_title = "", ""
        for title, text in sections.items():
            if title.startswith(name) or (rid and rid in title):
                m = re.search(r"```(?:text)?\n(.*?)```", text, re.S)
                prompt, section_title = (m.group(1).strip() if m else ""), title
                break

        nxt = next_cron_run(cron, now) if active else None
        routines.append({
            "name": name, "id": rid, "cron": cron, "plain": plain,
            "window": col(row, "timing", "window"), "status": status, "active": active,
            "loopJob": col(row, "loop"), "prompt": prompt,
            "nextRun": nxt.isoformat(timespec="minutes") if nxt else "",
        })

    changelog = [l[2:].strip() for l in sections.get("Change log", "").splitlines() if l.startswith("- ")]
    return {
        "status": (meta.get("status") or "").lower(),
        "updatedAt": meta.get("updatedAt", ""),
        "routines": routines,
        "changelog": changelog,
        "notes": {k: v for k, v in sections.items() if k in ("Legacy",)},
    }


def runtime_connection(runtime, runtime_id):
    """What this server can actually see of the agent's runtime."""
    rt = (runtime or "").lower()
    if not rt:
        return {"state": "missing", "label": "Runtime", "detail": "No runtime set in agent.md"}
    if rt == "hermes":
        p = Path(os.path.expanduser(runtime_id or "")) if runtime_id else None
        if p and p.is_dir():
            return {"state": "linked", "label": "Hermes", "detail": f"Profile found at {runtime_id}"}
        return {"state": "missing", "label": "Hermes", "detail": f"Profile not found on this server ({runtime_id or 'no runtimeId'})"}
    if rt == "openclaw":
        p = Path(os.path.expanduser(runtime_id or "")) if runtime_id else None
        if p and p.is_dir():
            return {"state": "linked", "label": "OpenClaw", "detail": f"Workspace found at {runtime_id}"}
        return {"state": "missing", "label": "OpenClaw", "detail": f"Workspace not found ({runtime_id or 'no runtimeId'})"}
    if rt == "grok-bot":
        return {"state": "external", "label": "Grok Bot",
                "detail": "Runs in Grok Bot; this server can't see its runs yet. Activity shows up here through the activity log."}
    if rt == "claude-code":
        return {"state": "linked", "label": "Claude Code", "detail": "Runs from Claude Code sessions"}
    return {"state": "external", "label": runtime, "detail": "Unknown runtime; not connected"}


def summarize_agent(adir, today):
    meta, body = parse_frontmatter(read_text(adir / "agent.md"))
    title, sections = split_sections(body)
    mission = first_paragraph(sections.get("Mission", ""))

    routines = load_routines(adir)

    # Status: agent.md `status:` wins, then routines.md's (the agent as a whole), then a
    # guess from the mission text.
    status = (meta.get("status") or (routines or {}).get("status") or "").lower()
    if not status:
        status = "idle" if re.search(r"\bidle\b", mission, re.I) else "active"

    role = title.split("—", 1)[1].strip() if "—" in title else ""
    records = {k: load_records(adir, k) for k in RECORD_KINDS}
    tasks = records["tasks"]

    counts = {}
    for t in tasks:
        counts[t["status"] or "unknown"] = counts.get(t["status"] or "unknown", 0) + 1

    due = [t for t in tasks if t["status"] in ("in_review", "todo", "running")
           and parse_day(t["checkAt"]) and parse_day(t["checkAt"]) <= today]
    stuck = [t for t in tasks if t["status"] == "running"
             and parse_day(t["createdAt"]) and (today - parse_day(t["createdAt"])).days > STUCK_AFTER_DAYS]
    pending = [p for p in records["proposals"] if p["status"] == "pending"]
    active_learnings = [l for l in records["learnings"] if l["status"] == "active"]

    connections = [
        {"key": "brain", "state": "linked", "label": "Brain folder", "detail": f"agents/{adir.name}/"},
        dict(runtime_connection(meta.get("runtime"), meta.get("runtimeId")), key="runtime"),
        {"key": "soul", "label": "SOUL.md",
         "state": "linked" if (adir / "SOUL.md").is_file() else "missing",
         "detail": "Versioned in brain" if (adir / "SOUL.md").is_file() else "No SOUL.md in brain yet"},
        {"key": "routines", "label": "Routines",
         "state": "linked" if routines is not None else "missing",
         "detail": (f"routines.md: {len(routines['routines'])} recorded, updated {routines['updatedAt'] or '?'}"
                    if routines is not None else "No routines.md yet")},
        {"key": "evals", "label": "Evals",
         "state": "linked" if (adir / "evals").is_dir() else "missing",
         "detail": "evals/ present" if (adir / "evals").is_dir() else "No eval suite yet"},
        {"key": "activity", "label": "Activity log",
         "state": "linked" if (LOGS_URL and LOGS_READ_KEY) else "missing",
         "detail": "Reading agent_logs" if (LOGS_URL and LOGS_READ_KEY)
         else "Not connected: SUPABASE_LOGS_URL / SUPABASE_LOGS_READ_KEY not set on this server"},
    ]

    metric = sections.get("Success metric", "")
    return {
        "slug": adir.name,
        "name": meta.get("name") or adir.name,
        "role": role,
        "title": title,
        "status": status,
        "runtime": meta.get("runtime", ""),
        "runtimeId": meta.get("runtimeId", ""),
        "owner": meta.get("owner", ""),
        "reportsTo": meta.get("reportsTo", ""),
        "rung": meta.get("rung", ""),
        "updatedAt": meta.get("updatedAt", ""),
        "mission": mission,
        "metricSet": bool(metric) and not re.search(r"not set", metric, re.I),
        "taskCounts": counts,
        "taskTotal": len(tasks),
        "due": due,
        "stuck": stuck,
        "pendingProposals": pending,
        "activeLearnings": len(active_learnings),
        "connections": connections,
        "routines": routines,
        "_records": records,
        "_sections": sections,
    }


def public(summary):
    return {k: v for k, v in summary.items() if not k.startswith("_")}


def brain_git():
    def git(*args):
        try:
            return subprocess.run(["git", "-C", str(BRAIN), *args], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    info = {"path": str(BRAIN), "branch": git("rev-parse", "--abbrev-ref", "HEAD")}
    last = git("log", "-1", "--format=%h%x09%cI%x09%s")
    if last:
        h, when, subject = (last.split("\t") + ["", "", ""])[:3]
        info.update(commit=h, committedAt=when, subject=subject)
    counts = git("rev-list", "--left-right", "--count", "HEAD...@{u}")
    if counts:
        ahead, behind = (counts.split() + ["0", "0"])[:2]
        info.update(ahead=int(ahead), behind=int(behind))
    info["lastFetch"] = ""
    fetch_head = BRAIN / ".git" / "FETCH_HEAD"
    if fetch_head.exists():
        info["lastFetch"] = datetime.fromtimestamp(fetch_head.stat().st_mtime).isoformat(timespec="minutes")
    return info


# ── Pull requests (brain on GitHub, via the gh CLI logged in on this server) ──

PR_FIELDS = ("number,title,body,state,isDraft,url,headRefName,createdAt,updatedAt,mergedAt,"
             "closedAt,additions,deletions,changedFiles,files,mergeable,mergeStateStatus,reviewDecision")
PR_CACHE_SECONDS = 60
_pr_cache = {"at": 0.0, "data": None}


def pr_agent(branch):
    """Bots name branches bot/<name>/..., <name>/... or <name>-...; all PRs are opened
    under the same GitHub account, so the branch is the only reliable author."""
    m = re.match(r"(?:bot/)?([a-z][a-z0-9]*)(?:[/-]|$)", branch or "")
    return m.group(1) if m else ""


def fetch_prs(force=False):
    now = datetime.now().timestamp()
    if not force and _pr_cache["data"] and now - _pr_cache["at"] < PR_CACHE_SECONDS:
        return _pr_cache["data"]
    def gh(state, limit):
        out = subprocess.run(["gh", "pr", "list", "--state", state, "--limit", str(limit), "--json", PR_FIELDS],
                             cwd=BRAIN, capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise RuntimeError(out.stderr.strip() or f"gh exited {out.returncode}")
        return json.loads(out.stdout)
    try:
        prs = gh("open", 50) + [p for p in gh("all", 30) if p["state"] != "OPEN"]
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}", "prs": []}
    for p in prs:
        p["agent"] = pr_agent(p["headRefName"])
    data = {"prs": prs, "fetchedAt": datetime.now().isoformat(timespec="seconds")}
    _pr_cache.update(at=now, data=data)
    return data


# ── Activity log ───────────────────────────────────────────────────────────

def fetch_activity(agent_name, limit=50):
    if not (LOGS_URL and LOGS_READ_KEY):
        return {"connected": False, "rows": []}
    q = urllib.parse.urlencode({
        "select": "created_at,agent_name,business,client,action,result,status,task_description,details,link,run_id",
        "agent_name": f"eq.{agent_name}",
        "order": "created_at.desc",
        "limit": str(limit),
    })
    req = urllib.request.Request(f"{LOGS_URL}/rest/v1/agent_logs?{q}", headers={
        "apikey": LOGS_READ_KEY, "Authorization": f"Bearer {LOGS_READ_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return {"connected": True, "rows": json.loads(r.read().decode())}
    except Exception as e:  # report, don't crash the page
        return {"connected": True, "rows": [], "error": f"{type(e).__name__}: {e}"}


# ── Video reviews (video_reviews in the same Supabase project; files stay in Drive) ──
# Bots add a row per video with the submit_video_for_review RPC
# (brain knowledge/agent-building/video-reviews.md). The hub reads rows with the logs read
# key and streams each file from Drive with the service account, so nothing is copied.

DRIVE_SA_FILE = Path(os.environ.get("DRIVE_SA_FILE", BRAIN / "credentials" / "google-drive-sa.json"))
VIDEO_COLUMNS = ("id,created_at,updated_at,agent_name,business,account,product,stage,title,drive_file_id,"
                 "airtable_base,airtable_table,airtable_record_id,clickup_task_id,run_id,notes,status,"
                 "reviewed_at,reviewer_note")
THUMB_DIR = HUB_DIR / ".cache" / "thumbs"
_drive = {"creds": None}
_thumbs = {}


def supabase_get(table, params):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{LOGS_URL}/rest/v1/{table}?{q}", headers={
        "apikey": LOGS_READ_KEY, "Authorization": f"Bearer {LOGS_READ_KEY}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def fetch_videos(limit=300):
    if not (LOGS_URL and LOGS_READ_KEY):
        return {"connected": False, "videos": []}
    try:
        rows = supabase_get("video_reviews", {"select": VIDEO_COLUMNS, "order": "created_at.desc", "limit": str(limit)})
        return {"connected": True, "videos": rows}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        missing = e.code == 404 or "PGRST205" in body or "42P01" in body
        return {"connected": True, "videos": [], "missingTable": missing, "error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"connected": True, "videos": [], "error": f"{type(e).__name__}: {e}"}


def video_in_queue(file_id):
    """The stream/thumb endpoints only serve Drive files a bot put in the review queue."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", file_id or "") or not (LOGS_URL and LOGS_READ_KEY):
        return False
    try:
        return bool(supabase_get("video_reviews", {"select": "id", "drive_file_id": f"eq.{file_id}", "limit": "1"}))
    except Exception:
        return False


def drive_token():
    from google.oauth2 import service_account          # optional dependency: only the video
    import google.auth.transport.requests as gtr       # pages need it
    c = _drive["creds"]
    if c is None:
        c = _drive["creds"] = service_account.Credentials.from_service_account_file(
            str(DRIVE_SA_FILE), scopes=["https://www.googleapis.com/auth/drive.readonly"])
    if not c.valid:
        c.refresh(gtr.Request())
    return c.token


def drive_open(file_id, range_header=None):
    headers = {"Authorization": f"Bearer {drive_token()}"}
    if range_header:
        headers["Range"] = range_header
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)


def frame_grab(file_id):
    """Drive makes no thumbnail for files it doesn't see as video (e.g. uploaded as
    application/mp4), so take a frame ourselves and keep it on disk. The file is
    downloaded first: the static ffmpeg build on this server crashes reading https."""
    cached = THUMB_DIR / f"{file_id}.jpg"
    if cached.is_file():
        return cached.read_bytes()
    ffmpeg = shutil.which("ffmpeg") or str(Path.home() / ".local" / "bin" / "ffmpeg")
    with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
        with drive_open(file_id) as src:
            shutil.copyfileobj(src, tmp)
        tmp.flush()
        out = subprocess.run([ffmpeg, "-v", "error", "-ss", "0.5", "-i", tmp.name, "-frames:v", "1",
                              "-vf", "scale=360:-2", "-q:v", "4", "-f", "image2", "pipe:1"],
                             capture_output=True, timeout=60)
    if out.returncode or not out.stdout:
        return None
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(out.stdout)
    return out.stdout


def drive_thumbnail(file_id):
    if file_id in _thumbs:
        return _thumbs[file_id]
    token = drive_token()
    meta_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=thumbnailLink&supportsAllDrives=true"
    with urllib.request.urlopen(urllib.request.Request(meta_url, headers={"Authorization": f"Bearer {token}"}), timeout=15) as r:
        link = json.loads(r.read().decode()).get("thumbnailLink")
    if link:
        link = re.sub(r"=s\d+$", "=s640", link)
        with urllib.request.urlopen(urllib.request.Request(link, headers={"Authorization": f"Bearer {token}"}), timeout=15) as r:
            thumb = (r.read(), r.headers.get("Content-Type", "image/jpeg"))
    else:
        jpg = frame_grab(file_id)
        if not jpg:
            return None
        thumb = (jpg, "image/jpeg")
    _thumbs[file_id] = thumb
    return thumb


# ── HTTP ───────────────────────────────────────────────────────────────────

def resolve_agent_file(slug, rel):
    """Only .md files inside agents/<slug>/ are readable."""
    adir = AGENTS_DIR / slug
    if not slug or slug.startswith((".", "_")) or not adir.is_dir() or not rel.endswith(".md"):
        return None
    try:
        target = (adir / rel).resolve()
        target.relative_to(adir.resolve())
    except (ValueError, OSError):
        return None
    return target if target.is_file() else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, data):
        self._send(code, json.dumps(data).encode(), "application/json")

    def stream_drive(self, file_id):
        """Pass the browser's Range through to Drive so the player can seek."""
        try:
            src = drive_open(file_id, self.headers.get("Range"))
        except urllib.error.HTTPError as e:
            return self._json(e.code if e.code in (404, 416) else 502, {"error": f"Drive HTTP {e.code}"})
        except Exception as e:
            return self._json(502, {"error": f"{type(e).__name__}: {e}"})
        with src:
            self.send_response(src.status)
            for h in ("Content-Length", "Content-Range"):
                if src.headers.get(h):
                    self.send_header(h, src.headers[h])
            ctype = src.headers.get("Content-Type", "")
            # Uploads typed application/mp4 or octet-stream are still MP4 video
            self.send_header("Content-Type", ctype if ctype.startswith("video/") else "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            try:
                while chunk := src.read(256 * 1024):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the player cancels ranges it no longer needs

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        arg = lambda k: (qs.get(k) or [""])[0]
        today = date.today()

        if url.path == "/":
            self._send(200, (HUB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")

        elif url.path == "/api/overview":
            agents = [summarize_agent(d, today) for d in agent_dirs()]
            needs = {"due": [], "stuck": [], "proposals": []}
            schedule = []
            for a in agents:
                for key, src in (("due", "due"), ("stuck", "stuck"), ("proposals", "pendingProposals")):
                    needs[key] += [dict(r, agent=a["slug"], agentName=a["name"]) for r in a[src]]
                for r in (a["routines"] or {}).get("routines", []):
                    schedule.append({k: r[k] for k in ("name", "id", "plain", "status", "active", "nextRun", "loopJob")}
                                    | {"agent": a["slug"], "agentName": a["name"]})
            schedule.sort(key=lambda r: (not r["active"], r["nextRun"] or "9999"))
            self._json(200, {
                "today": today.isoformat(),
                "brain": brain_git(),
                "agents": [public(a) for a in agents],
                "needs": needs,
                "schedule": schedule,
                "activityConnected": bool(LOGS_URL and LOGS_READ_KEY),
            })

        elif url.path == "/api/agent":
            adir = AGENTS_DIR / arg("slug")
            if not arg("slug") or arg("slug").startswith((".", "_")) or not adir.is_dir():
                return self._json(404, {"error": "agent not found"})
            a = summarize_agent(adir, today)
            files = [f for f in ("agent.md", "SOUL.md", "routines.md") if (adir / f).is_file()]
            if (adir / "evals").is_dir():
                files += [str(f.relative_to(adir)) for f in sorted((adir / "evals").glob("*.md"))]
            self._json(200, dict(public(a), sections=a["_sections"], records=a["_records"], files=files))

        elif url.path == "/api/file":
            target = resolve_agent_file(arg("slug"), arg("path"))
            if not target:
                return self._json(404, {"error": "file not found"})
            self._send(200, target.read_bytes(), "text/plain; charset=utf-8")

        elif url.path == "/api/activity":
            self._json(200, fetch_activity(arg("agent")))

        elif url.path == "/api/prs":
            self._json(200, fetch_prs(force=arg("refresh") == "1"))

        elif url.path == "/api/videos":
            self._json(200, fetch_videos())

        elif url.path == "/api/video-thumb":
            if not video_in_queue(arg("id")):
                return self._json(404, {"error": "not in the review queue"})
            try:
                thumb = drive_thumbnail(arg("id"))
            except Exception as e:
                return self._json(502, {"error": f"{type(e).__name__}: {e}"})
            if not thumb:
                return self._json(404, {"error": "Drive has no thumbnail yet"})
            self._send(200, *thumb)

        elif url.path == "/api/video-stream":
            if not video_in_queue(arg("id")):
                return self._json(404, {"error": "not in the review queue"})
            self.stream_drive(arg("id"))

        else:
            self._json(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"Agent Hub on http://{HOST}:{PORT} reading {AGENTS_DIR}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
