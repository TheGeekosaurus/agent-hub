# Agent Hub

Read-only dashboard over `~/brain/agents/` and brain's pull requests. Each agent folder there (except `_templates`)
appears automatically, with its agent.md, tasks, learnings, proposals, files and connection
state. Replaces the old OpenClaw "Agent OS" dashboard (`openclaw-workspace/agent-dashboard`)
on the same port, so the Windows tunnel needs no change.

- URL: http://localhost:45680 (via geeko-tunnel)
- Service: `systemctl --user {status,restart} agent-hub`  (runs as denisbrd, so it can read brain)
- Optional `.env` (gitignored, chmod 600): `SUPABASE_LOGS_URL`, `SUPABASE_LOGS_READ_KEY`
  to turn on the Activity tab from `agent_logs`.
- Page changes (index.html) are live on reload; server.py changes need a restart.
- Pull requests page reads brain's PRs with the `gh` CLI logged in on this server (cached 60s).
  The agent is taken from the branch name (`bot/<name>/…`, `<name>/…`, `<name>-…`), since every
  bot opens PRs under the same GitHub account. Merging stays on GitHub.
- Videos page reads `video_reviews` (same Supabase project and read key as the Activity tab)
  and streams each file from Drive with `brain/credentials/google-drive-sa.json` (override with
  `DRIVE_SA_FILE`). Only files that are in the queue are served. Setup and the bots' submit RPC:
  brain `knowledge/agent-building/video-reviews.md`.
