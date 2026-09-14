# session-chat Protocol

session-chat turns [Herdr](https://herdr.dev) panes into a chat network between
agent sessions. A **main session** (the agent a human talks to) dispatches tasks
to **worker sessions** (permission-free agents in sibling panes). Workers do the
work, write results to files, and report back over the same channel — without
anyone blocking.

The protocol has three layers with one boundary rule:

> **While an agent can act, the protocol drives it. When it cannot act
> (blocked, dead, forgot), the guardian plugin takes over.**

- **Skill** (this repo, `skills/herdr-session-chat/`) — instructions for agent
  sessions. Reference implementation targets Claude Code, but every rule below
  is agent-agnostic.
- **Guardian plugin** (`plugin/`) — a Herdr plugin that subscribes to pane
  lifecycle events and delivers what agents cannot deliver themselves.
- **State files** — the contract between the two, all under one directory.

Everything is built on Herdr's public CLI (`herdr agent`, `herdr pane`,
`herdr notification`). No private APIs, no Herdr source changes.

## Participants and addresses

- Every pane is identified by its Herdr pane ID (e.g. `wY:p1`). This is the
  **only** address used on the wire.
- The main session knows its own pane ID from the `HERDR_PANE_ID` environment
  variable injected into every Herdr pane.
- Agent names (`[a-z][a-z0-9_-]{0,31}`, unique among live agents) are used to
  *drive* a worker (`herdr agent prompt <name> ...`) but never appear inside
  message payloads as an address.

### Normative: reply targets are literal pane IDs

When the main session composes a task for a worker, the reply command inside
that task text MUST contain the main session's pane ID as a **literal value**
(e.g. `herdr agent prompt wY:p1 ...`), never an unexpanded `$HERDR_PANE_ID`.

Rationale: the task text is pasted into the worker's input box; any variable
that survives into the worker's shell expands to the **worker's own** pane ID,
and the reply loops back to its sender. Main sessions must expand the variable
at composition time (or inline the literal) and verify the outgoing text
contains no `$HERDR` residue.

## State files

Root directory: `/tmp/session-chat/` (a fixed path keeps every participant and
the guardian in sync without configuration; it is scratch space by design).

The root and its subdirectories MUST be owner-only (mode `0700`). Pending
receipts are delivery credentials — a world-writable root would let any local
user forge a receipt and inject messages into the main session. The guardian
enforces the mode on every invocation.

### `manifest.json` — dispatch registry (written by main)

```json
{
  "main_pane": "wY:p1",
  "tasks": [
    {
      "name": "sc-auth-fix",
      "pane_id": "wY:p3",
      "task": "one-line task description",
      "dispatched_at": "2026-09-14 21:36:13 +0800",
      "status": "outstanding"
    }
  ]
}
```

- `main_pane` — reply address for every worker in this batch. This is how the
  guardian routes signals; written once at dispatch.
- `tasks[].status` — `outstanding` → `done` (main confirmed the reply), or
  `failed` (worker disappeared; respawn is always a human decision).
- The `status` field is a registry, not live state: there is a window between
  a reply arriving and the status update. Consumers that need live state use
  `herdr agent get` and the pending files.

### `pending/<worker-pane-id>.json` — delivery receipt (written by worker)

```json
{
  "target": "wY:p1",
  "summary": "auth test fixed, 3 files changed",
  "detail": "/tmp/session-chat/sc-auth-fix.md"
}
```

A worker with a finished result writes this **before** attempting its reply.
It then sends the reply itself and, on success, **deletes the pending file**.
If the reply is rejected (`agent_blocked` — the main session is waiting on a
dialog) or never attempted, the pending file stays and the guardian delivers
it later. Delivery is deduplicated by moving the file to `sent/`.

### `sent/`, `.notified/` — guardian-private

`sent/` holds archived receipts (move = dedupe). `.notified/` holds one-shot
markers so alarms (blocked, dead) fire exactly once per episode; the blocked
marker is cleared when the worker resumes `working`.

### `<task>.md` — full results

Workers write complete results (summaries, conclusions, touched files) to
`/tmp/session-chat/<task>.md`. Messages carry only a one-line summary plus the
path; the file is the source of truth. A settled lifecycle state never counts
as proof of work — the main session reads the file before reporting to the
human.

## Messages

All inter-session messages are submitted with `herdr agent prompt <pane-id>
<text>` (no `--wait` — dispatch and report are both fire-and-forget). First
line, three shapes:

```
[session-chat] <name>: <one-line summary> 详情: <result path>     # normal report
[session-chat] ⚠ <name> blocked,需要审批/回答…                    # worker blocked (guardian)
[session-chat] ☠ <name> 进程退出,任务未完成                        # worker died (guardian)
```

Replies land in the main session's input box: immediately if it is idle, or
queued until its current turn ends. Each reply opens exactly one turn.

## Lifecycle rules

1. **Dispatch is non-blocking.** Main splits a sibling pane (right if wide,
   down if narrow/tall; alternate directions for multiple workers), starts the
   worker with its CLI's unattended flag, sends the task, and ends its turn.
2. **Health check.** 30–60 s after dispatch, main runs `herdr agent get` once.
   If the worker is `blocked`, main classifies the dialog: *mechanical*
   (folder trust, task-scoped permission asks) → main answers it via
   `herdr agent send-keys`, but only after verifying the dialog content matches
   the task, and always choosing the least-privilege option (one-shot, never
   session-wide grants). *Substantive* (beyond task scope) → main asks the
   human. Until a guardian plugin is installed, periodic health checks are the
   only defense against a silently stuck worker.
3. **Reporting.** Each reply: read the detail file, verify, update the
   manifest, report to the human in one or two sentences (parallel batches as
   "N of M done"). Never paste raw worker output.
4. **Death.** A missing agent (`agent_not_found`) is reported to the human as
   an unfinished task. Main never respawns on its own; the manifest entry
   becomes `failed` and the human decides.
5. **The confirmation chain terminates at the human.** Mechanical confirmations
   may be automated; every substantive decision reaches the human. The guardian
   forwards signals only — it never answers anything.

## Guardian contract

Subscribes to `pane.agent_status_changed`, `pane.exited`, `pane.closed`
(envelope `event` field uses underscores: `pane_agent_status_changed`, …).

| Event | Condition | Action |
|---|---|---|
| status → `idle`/`done` | `pending/<pane>.json` exists | deliver receipt to its `target`, move to `sent/`, toast |
| status → `blocked` | worker pane, first occurrence | `⚠` message to `main_pane` + toast; marker set |
| status → `working` | blocked marker present | clear marker (episode over) |
| `pane.exited`/`pane.closed` | task still `outstanding` | `☠` message to `main_pane` + toast; marker set |
| main pane → `working`/`idle`/`done` | any pending receipts exist | deliver all (covers receipts that were rejected with `agent_blocked` while main was busy) |

Panes not listed in the manifest are ignored on first byte.

## Porting to other agent kinds

The channel and file contracts are CLI-independent. Only the *launch* step is
per-CLI — its unattended flag and pre-launch configuration:

| kind | unattended launch | status |
|---|---|---|
| `claude` | `--dangerously-skip-permissions` (does not skip the folder-trust dialog on first run) | machine-verified |
| `codex` | `--dangerously-bypass-approvals-and-sandbox` (unattended, no sandbox) | documented, verify before relying on it |
| `pi` | none needed — pi never asks by design; `-a` pre-approves project-local settings if a manifest exists | documented, verify before relying on it |
| `opencode` | `--auto` (auto-approves everything not explicitly `deny`-ed) | documented, verify before relying on it |

Unknown kinds: launch without flags, and if the worker blocks, surface it to
the human — never guess flags.
