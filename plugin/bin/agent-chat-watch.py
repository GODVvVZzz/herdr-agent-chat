#!/usr/bin/env python3
"""agent-chat transport guardian.

Reads one herdr plugin event from HERDR_PLUGIN_EVENT_JSON and routes the
signals agents cannot send themselves. State lives on disk:

  /tmp/herdr-agent-chat/manifest.json        written by the main session at dispatch
      {"main_pane": "wY:p1", "tasks": [{"name","pane_id","task","dispatched_at","status"}, ...]}
  /tmp/herdr-agent-chat/pending/<pane>.json  written by a worker with a finished result
      {"target": "...", "summary": "...", "detail": "/tmp/herdr-agent-chat/<task>.md"}
  /tmp/herdr-agent-chat/sent/                archived pending files (dedupe by move)
  /tmp/herdr-agent-chat/.notified/           one-shot markers so alarms fire once

Delivery rules:
  worker idle/done + pending exists   -> deliver the reply, archive pending
  worker blocked                      -> tell main + toast (once, until resumed)
  worker pane exited/closed while its task is still outstanding
                                      -> tell main + toast (once)
  main back from blocked with pending -> deliver everything (worker self-reply
                                         was rejected with agent_blocked earlier)
"""
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path("/tmp/herdr-agent-chat")
PENDING = ROOT / "pending"
SENT = ROOT / "sent"
NOTIFIED = ROOT / ".notified"


def herdr(args, timeout=20):
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    try:
        return subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def notify(body):
    herdr(["notification", "show", "agent-chat", "--body", body, "--sound", "request"])


def deliver(pf, main_pane, name):
    try:
        p = json.loads(pf.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    target = p.get("target") or main_pane
    summary = p.get("summary", "completed")
    detail = p.get("detail", "")
    text = f"[agent-chat] {name}: {summary}"
    if detail:
        text += f" details: {detail}"
    result = herdr(["agent", "prompt", target, text])
    if result is None or result.returncode != 0:
        return False
    pf.rename(SENT / pf.name)
    return True


def main():
    # Only the owner may read or write agent-chat state: pending receipts are
    # delivery credentials, and a world-writable directory would let any local
    # user inject messages into the main session.
    ROOT.chmod(0o700) if ROOT.exists() else None
    for d in (PENDING, SENT, NOTIFIED):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)

    def log(msg):
        try:
            with open(ROOT / ".watch.log", "a") as f:
                import time

                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except OSError:
            pass

    try:
        event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON", "{}"))
    except json.JSONDecodeError:
        return
    kind = event.get("event", "").replace(".", "_")
    data = event.get("data", {}) or {}
    pane_id = data.get("pane_id", "")
    status = (data.get("agent_status") or "").lower()
    log(f"event={kind} pane={pane_id} status={status}")
    if not pane_id:
        return

    try:
        manifest = json.loads((ROOT / "manifest.json").read_text())
    except (json.JSONDecodeError, OSError):
        return
    main_pane = manifest.get("main_pane", "")
    tasks = {t.get("pane_id"): t for t in manifest.get("tasks", []) if t.get("pane_id")}
    if not main_pane:
        return

    # Main pane events: after a blocked period ends, sweep pending replies that
    # were rejected with agent_blocked while the main session was busy/blocked.
    if pane_id == main_pane:
        if kind == "pane_agent_status_changed" and status in ("working", "idle", "done"):
            for pf in sorted(PENDING.glob("*.json")):
                t = tasks.get(pf.stem) or {}
                deliver(pf, main_pane, t.get("name", pf.stem))
        return

    # Worker pane events.
    task = tasks.get(pane_id)
    if task is None:
        return
    name = task.get("name", pane_id)

    if kind == "pane_agent_status_changed" and status in ("idle", "done"):
        pf = PENDING / f"{pane_id}.json"
        if pf.exists():
            deliver(pf, main_pane, name)
            notify(f"{name} completed")

    elif kind == "pane_agent_status_changed" and status == "blocked":
        marker = NOTIFIED / f"{pane_id}.blocked"
        if not marker.exists():
            marker.touch()
            herdr([
                "agent", "prompt", main_pane,
                f"[agent-chat] ⚠ {name} blocked — needs approval or an answer; inspect with agent read",
            ])
            notify(f"{name} blocked — main session notified")

    elif kind == "pane_agent_status_changed" and status == "working":
        marker = NOTIFIED / f"{pane_id}.blocked"
        if marker.exists():
            marker.unlink()

    elif kind in ("pane_exited", "pane_closed"):
        if task.get("status") != "outstanding":
            return
        marker = NOTIFIED / f"{pane_id}.dead"
        if not marker.exists():
            marker.touch()
            herdr(["agent", "prompt", main_pane, f"[agent-chat] ☠ {name} exited — task unfinished"])
            notify(f"{name} exited")


if __name__ == "__main__":
    sys.exit(main())
