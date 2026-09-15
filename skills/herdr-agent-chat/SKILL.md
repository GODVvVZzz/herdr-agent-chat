---
name: herdr-agent-chat
description: "Chat-like delegation over Herdr panes: dispatch tasks to permission-free worker sessions in new panes; workers report back over the same channel while you keep talking to the user. Use only when running inside Herdr (HERDR_ENV=1) and the user wants to delegate tasks to panes, spawn parallel sub-sessions, or mentions agent-chat / 派活 / 委派子任务 / 开 pane 干活. Not for plain terminals."
---

# Agent Chat — chat-like delegation across Herdr panes

Treat Herdr as a chat channel between sessions: the main session (you) opens
panes running permission-free claude workers, sends tasks as messages, and
workers send results back as messages. Fully asynchronous — nothing ever blocks.

## Prerequisite

```bash
test "${HERDR_ENV:-}" = 1
```

If it fails you are not running inside Herdr: tell the user and stop. When it
passes you may combine this with the herdr skill; this skill is self-contained.

## Your identity

- `$HERDR_PANE_ID` is your own pane ID — also the address workers reply to.
- Optional self-check: `herdr agent get "$HERDR_PANE_ID"`. If your pane is not
  recognized as an agent (rare), replies can still reach the pane but the
  harvest fallback is unavailable — fall back to pure file handoff (see Fallback).

## Spawning a worker

```bash
# 1. directory for result files
mkdir -p /tmp/herdr-agent-chat

# 2. geometry first: wide pane → right, narrow or tall → down
herdr pane layout --pane "$HERDR_PANE_ID"
herdr pane split --current --direction right --cwd "$PWD" --no-focus
# read the new pane ID from .result.pane.pane_id

# 3. permission-free claude (unique name, [a-z][a-z0-9_-]{0,31}, sc- prefix recommended)
herdr agent start sc-<task> --kind claude --pane <pane-id> -- --dangerously-skip-permissions

# 4. dispatch: one submission, no --wait; returns immediately
#    ⚠️ the reply target must be your pane ID as a LITERAL (e.g. wY:p1), never the
#    $HERDR_PANE_ID variable: task text is pasted into the worker's input box, and
#    any variable surviving into the worker's shell expands to the WORKER's own
#    pane ID — the reply loops back to its sender. Use double quotes so the
#    variable expands here, and verify no "$HERDR" residue remains in the text.
herdr agent prompt sc-<task> "<task description>

Rules:
- Work autonomously to completion; do not stop to ask for confirmation.
- When done, write the full result (summary, conclusions, key files) to /tmp/herdr-agent-chat/<task>.md
- Then send the reply (execute verbatim; the target is the main session's pane ID literal):
  herdr agent prompt <main-pane-ID-literal> '[agent-chat] <task>: <one-line summary> details: /tmp/herdr-agent-chat/<task>.md'
- If the reply is rejected with agent_blocked, retry once after 30 s. If it still
  fails (or cannot be attempted), write a delivery receipt so the guardian plugin
  delivers on your behalf:
  /tmp/herdr-agent-chat/pending/<value-of-your-HERDR_PANE_ID>.json containing
  {"pane":"<value of your own HERDR_PANE_ID env var>","target":"<main-pane-ID-literal>","summary":"<one-line summary>","detail":"/tmp/herdr-agent-chat/<task>.md"}
  The guardian matches receipts by the pane field, not the file name. A successful
  reply means NO receipt — never write one preemptively."
```

Key points:

- `agent start` waits until the worker is interactive-ready (default 30 s).
  `agent_not_ready` means it got stuck during startup: inspect with
  `herdr agent read <pane-id> --source recent-unwrapped --lines 40`, then classify —
  *mechanical* dialogs (folder trust, task-scoped permission asks) may be answered
  on the worker's behalf, but only after verifying the dialog content matches the
  dispatched task, and always choosing the least-privilege option (one-shot, never
  session-wide grants). Anything *uncertain or beyond task scope* → ask the user.
- Run one `agent get` health check 30–60 s after dispatch; handle blocked states as
  above until the worker returns to working/idle/done. Without the guardian plugin,
  periodic health checks are the main defense against a silently stuck worker.
- `--dangerously-skip-permissions` is mandatory; otherwise the worker stalls at
  permission prompts nobody answers. If environment policy disables it, report to
  the user — never work around it.
- Parallel workers: one pane and one unique name each; avoid consecutive
  same-direction splits (alternate right/down or use `--ratio`).
- **Always write the dispatch registry** `/tmp/herdr-agent-chat/manifest.json`:
  `{"main_pane":"<your-pane-ID>","tasks":[{"name","pane_id","task","dispatched_at","status":"outstanding"}]}`.
  It is the routing contract of the guardian plugin — keep the field names stable.
  Set a task's `status` to `done` after verifying its reply; mark `failed` when a
  worker disappears. Never treat `status` as live state — a reply can arrive before
  the update lands; live checks use pending receipts and `agent get`.
- Workers inherit the pane's cwd and environment; a fresh claude session carries
  your global skills and the `HERDR_*` variables and can execute the reply command.
- Language: protocol markers (`[agent-chat]`, field keywords) are fixed; the task
  text, summaries and your reports follow the user's conversation language.

## Receiving replies and multi-turn threads

- A worker reply arrives as input starting with `[agent-chat]`. If you are idle
  it opens a new turn immediately; if you are working it queues until the turn
  ends. On receipt: **read the detail file → verify → update the manifest → report
  to the user in one or two sentences** — never paste raw output. For parallel
  batches report progress as "N of M done".
- The detail file is the source of truth. A settled state or a one-line summary is
  never proof of work — `herdr agent get sc-<task>` first, then read the file.
- Follow-ups: send another `herdr agent prompt sc-<task> "..."` (still no --wait);
  the worker replies again when done.
- Progress questions (non-blocking): `herdr agent get sc-<task>` for state,
  `herdr agent read sc-<task> --source recent-unwrapped --lines 60` for output.

## Fallback harvest

Only when a reply is overdue or the user asks directly:

```bash
herdr agent wait sc-<task> --timeout 300000   # blocking — only when the user is waiting right now
herdr agent read sc-<task> --source recent-unwrapped --lines 120
```

A reply rejected with `agent_blocked` leaves the result in the files; read them
directly. If your own pane is not recognized as an agent (replies have nowhere to
land), tell the user to check `/tmp/herdr-agent-chat/` manually.

- A missing worker (`agent_not_found`): report honestly that the task is
  unfinished and **never respawn on your own**; mark the manifest entry `failed` —
  respawning is the user's decision.

## Discipline

- Never dispatch with `--wait`. Blocking waits are allowed only when the user is
  waiting for that exact result right now.
- Worker panes are yours but stay **open by default** — the user watches them.
  Close only on explicit request (`herdr pane close`).
- Never answer a worker's approval/question dialog blindly: inspect with
  `agent get` + `agent read`, classify (mechanical → least-privilege answer;
  substantive → ask the user).
- Do not dispatch into unrelated existing panes; only into panes you opened for
  this delegation.
- A task text always carries three things: the **result file** (full content), the
  **receipt** (guardian takeover on failure), and the **reply** (instant
  notification).
