# Agent Hub

Read-only dashboard over `~/brain/agents/`. Each agent folder there (except `_templates`)
appears automatically, with its agent.md, tasks, learnings, proposals, files and connection
state. Replaces the old OpenClaw "Agent OS" dashboard (`openclaw-workspace/agent-dashboard`)
on the same port, so the Windows tunnel needs no change.

- URL: http://localhost:45680 (via geeko-tunnel)
- Service: `systemctl --user {status,restart} agent-hub`  (runs as denisbrd, so it can read brain)
- Optional `.env` (gitignored, chmod 600): `SUPABASE_LOGS_URL`, `SUPABASE_LOGS_READ_KEY`
  to turn on the Activity tab from `agent_logs`.
- Page changes (index.html) are live on reload; server.py changes need a restart.
