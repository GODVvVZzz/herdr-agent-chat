#!/usr/bin/env python3
"""agent-chat sweep — recycle your own settled worker panes at dispatch time.

Run by the main session right before it would open a new worker pane. It joins
three sources: the manifest (ownership + task status), `herdr agent list`
(live agent state, focused flag, session UUID) and the worker's Claude Code
transcript (context occupancy, last-activity time).

Ownership is strict: only manifest entries whose `main_pane` equals this
session's pane ($HERDR_PANE_ID) are considered. Legacy entries without a
per-task `main_pane` are skipped — the sweep never guesses an owner.

Verdicts, per owned worker pane:
  REUSE  settled + agent idle/done + same realpath cwd as --target-cwd +
         context below --ctx-limit  -> keep; main renames it and dispatches
         into it instead of opening a pane (lowest-context candidate wins)
  CLOSE  settled (status done/failed) and not the reuse pick and not focused
         -> `herdr pane close`; the entry is already settled so this cannot
         trip the guardian's death alarm (mark-then-close is already done)
  KEEP   everything else: outstanding tasks, focused panes, agent-missing
         panes with an outstanding task (respawn is a human decision)
  PRUNE  pane no longer exists and status still outstanding -> mark `failed`

Set AGENT_CHAT_ROOT to redirect the state directory (tests); defaults to the
protocol path /tmp/herdr-agent-chat.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(os.environ.get("AGENT_CHAT_ROOT", "/tmp/herdr-agent-chat"))
TRANSCRIPTS = pathlib.Path("~/.claude/projects").expanduser()


def herdr(args, timeout=20):
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    try:
        r = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def transcript_path(cwd, session_uuid):
    # Claude Code escapes project paths by replacing '/' and '.' with '-'
    # (/Users/vvv/work/x -> -Users-vvv-work-x). Probe the raw cwd, its
    # realpath, and both without the macOS /private symlink prefix.
    cands = {cwd, os.path.realpath(cwd)}
    cands |= {c[len("/private"):] for c in cands if c.startswith("/private")}
    for c in cands:
        p = TRANSCRIPTS / re.sub(r"[/.]", "-", c) / f"{session_uuid}.jsonl"
        if p.is_file():
            return p
    return None


def context_state(cwd, session_uuid):
    """(ctx_tokens, idle_minutes) from the worker's transcript, or (None, None)."""
    p = transcript_path(cwd, session_uuid)
    if p is None:
        return None, None
    try:
        size = p.stat().st_size
        idle = max(0.0, (time.time() - p.stat().st_mtime) / 60)
        with p.open("rb") as f:
            f.seek(max(0, size - 1_000_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None, None
    for line in reversed(tail.splitlines()):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        usage = (e.get("message") or {}).get("usage")
        if not usage or e.get("isApiErrorMessage"):
            continue
        ctx = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        return ctx, idle
    return None, idle


def main():
    ap = __import__("argparse").ArgumentParser(description=__doc__)
    ap.add_argument("--target-cwd", required=True,
                    help="cwd the incoming task will run in (reuse match)")
    ap.add_argument("--ctx-limit", type=int, default=40,
                    help="max context %% for a reuse candidate (default 40)")
    ap.add_argument("--context-window", type=int, default=200_000)
    ap.add_argument("--dry-run", action="store_true",
                    help="report only: no pane close, no manifest write")
    args = ap.parse_args()

    my_pane = os.environ.get("HERDR_PANE_ID", "")
    if not my_pane:
        print("sweep: HERDR_PANE_ID unset — not inside a herdr pane, skipping")
        return 0

    try:
        manifest = json.loads((ROOT / "manifest.json").read_text())
    except (json.JSONDecodeError, OSError):
        print("sweep: no dispatch registry — nothing to sweep")
        return 0

    owned = [t for t in manifest.get("tasks", [])]
    legacy = [t for t in owned if not t.get("main_pane")]
    owned = [t for t in owned if t.get("main_pane") == my_pane]
    if not owned and not legacy:
        print(f"sweep: no own workers registered for {my_pane} — nothing to sweep")
        return 0

    agents = {
        a.get("pane_id"): a
        for a in ((herdr(["agent", "list"]) or {}).get("result", {}).get("agents", []))
        if a.get("pane_id")
    }
    panes = {
        p.get("pane_id")
        for p in ((herdr(["pane", "list"]) or {}).get("result", {}).get("panes", []))
        if p.get("pane_id")
    }

    target = os.path.realpath(args.target_cwd)
    rows, reuse_cands, to_close, to_prune = [], [], [], []
    header_cwd = args.target_cwd
    for t in owned:
        pid_, name = t.get("pane_id", ""), t.get("name", "?")
        status = t.get("status", "outstanding")
        a = agents.get(pid_)
        if pid_ not in panes:
            verdict = "PRUNE" if status == "outstanding" else "GONE"
            if status == "outstanding":
                to_prune.append(t)
            rows.append((pid_, name, status, "?", "?", "?", verdict))
            continue
        if a is None:
            # pane alive but no agent: worker died, shell left. Settled panes
            # are dead weight; outstanding ones stay (respawn is a human call).
            if status != "outstanding":
                to_close.append((pid_, name, "no agent"))
            verdict = "CLOSE" if status != "outstanding" else "KEEP"
            rows.append((pid_, name, status, "-", "-", "-", verdict))
            continue
        uuid = (a.get("agent_session") or {}).get("value", "")
        ctx, idle = context_state(a.get("cwd", ""), uuid) if uuid else (None, None)
        ctx_pct = round(100 * ctx / args.context_window) if ctx is not None else None
        settled = status != "outstanding"
        same_cwd = os.path.realpath(a.get("cwd", "")) == target
        reusable = (
            settled
            and a.get("agent_status") in ("idle", "done")
            and same_cwd
            and ctx_pct is not None
            and ctx_pct < args.ctx_limit
        )
        if reusable:
            reuse_cands.append((ctx_pct, pid_, name, idle))
        elif settled and not a.get("focused"):
            to_close.append((pid_, name, "settled"))
        rows.append((
            pid_, name, status,
            f"{ctx_pct}%" if ctx_pct is not None else "?",
            f"{int(idle)}m" if idle is not None else "?",
            a.get("cwd", "?"),
            "REUSE?" if reusable else ("CLOSE" if settled and not a.get("focused") else "KEEP"),
        ))

    best = min(reuse_cands) if reuse_cands else None
    if best:
        to_close = [c for c in to_close if c[0] != best[1]]

    width = max(len(r[1]) for r in rows) if rows else 1
    print(f"sweep: own workers of {my_pane}, target cwd {header_cwd}")
    for pid_, name, status, ctx, idle, cwd, verdict in rows:
        print(f"  {pid_:8} {name:<{width}}  {status:<12} ctx={ctx:<4} idle={idle:<5} {verdict}")
    if legacy:
        print(f"  ({len(legacy)} legacy entry/entries without main_pane skipped — owner unknown)")

    if best:
        _, pid_, name, idle = best
        print(f"REUSE {pid_} {name} — rename + redispatch into it instead of a new pane")
    for pid_, name, why in to_close:
        if args.dry_run:
            print(f"CLOSE {pid_} {name} (dry-run, skipped)")
            continue
        if herdr(["pane", "close", pid_]) is not None:
            print(f"CLOSE {pid_} {name}")
        else:
            print(f"CLOSE FAILED {pid_} {name} — close it manually")
    for t in to_prune:
        if args.dry_run:
            print(f"PRUNE {t.get('pane_id')} {t.get('name')} -> failed (dry-run, skipped)")
            continue
        fresh = None
        try:
            fresh = json.loads((ROOT / "manifest.json").read_text())
            for ft in fresh.get("tasks", []):
                if ft.get("pane_id") == t.get("pane_id"):
                    ft["status"] = "failed"
            (ROOT / "manifest.json").write_text(json.dumps(fresh))
            print(f"PRUNE {t.get('pane_id')} {t.get('name')} -> failed")
        except (json.JSONDecodeError, OSError):
            print(f"PRUNE FAILED {t.get('pane_id')} {t.get('name')} — update the manifest manually")
    if not (best or to_close or to_prune):
        print("sweep: nothing to reuse or close")
    return 0


if __name__ == "__main__":
    sys.exit(main())
