#!/usr/bin/env python3
"""Agent Hub: a read-only dashboard over brain/agents/.

Every folder in brain/agents/ (except _templates) is an agent. Nothing about the agents
is hardcoded here: names, runtimes, records and connection state are all read from disk
on each request, so a new agent folder shows up on the next page load.

Run: python3 server.py   (HOST/PORT/BRAIN_DIR overridable via environment)
"""

import json
import os
import re
import subprocess
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

    # Explicit `status:` in agent.md wins; otherwise infer idle from the mission text.
    status = (meta.get("status") or "").lower()
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
            for a in agents:
                for key, src in (("due", "due"), ("stuck", "stuck"), ("proposals", "pendingProposals")):
                    needs[key] += [dict(r, agent=a["slug"], agentName=a["name"]) for r in a[src]]
            self._json(200, {
                "today": today.isoformat(),
                "brain": brain_git(),
                "agents": [public(a) for a in agents],
                "needs": needs,
                "activityConnected": bool(LOGS_URL and LOGS_READ_KEY),
            })

        elif url.path == "/api/agent":
            adir = AGENTS_DIR / arg("slug")
            if not arg("slug") or arg("slug").startswith((".", "_")) or not adir.is_dir():
                return self._json(404, {"error": "agent not found"})
            a = summarize_agent(adir, today)
            files = [f for f in ("agent.md", "SOUL.md") if (adir / f).is_file()]
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

        else:
            self._json(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"Agent Hub on http://{HOST}:{PORT} reading {AGENTS_DIR}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
