#!/usr/bin/env python3
"""session-chat transport guardian.

Reads one herdr plugin event from HERDR_PLUGIN_EVENT_JSON and routes the
signals agents cannot send themselves. State lives on disk:

  /tmp/session-chat/manifest.json        written by the main session at dispatch
      {"main_pane": "wY:p1", "tasks": [{"name","pane_id","task","dispatched_at","status"}, ...]}
  /tmp/session-chat/pending/<pane>.json  written by a worker with a finished result
      {"target": "...", "summary": "...", "detail": "/tmp/session-chat/<task>.md"}
  /tmp/session-chat/sent/                archived pending files (dedupe by move)
  /tmp/session-chat/.notified/           one-shot markers so alarms fire once

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

ROOT = pathlib.Path("/tmp/session-chat")
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
    herdr(["notification", "show", "session-chat", "--body", body, "--sound", "request"])


def deliver(pf, main_pane, name):
    try:
        p = json.loads(pf.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    target = p.get("target") or main_pane
    summary = p.get("summary", "已完成")
    detail = p.get("detail", "")
    text = f"[session-chat] {name}: {summary}"
    if detail:
        text += f" 详情: {detail}"
    result = herdr(["agent", "prompt", target, text])
    if result is None or result.returncode != 0:
        return False
    pf.rename(SENT / pf.name)
    return True


def main():
    # Only the owner may read or write session-chat state: pending receipts are
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
            notify(f"{name} 完成")

    elif kind == "pane_agent_status_changed" and status == "blocked":
        marker = NOTIFIED / f"{pane_id}.blocked"
        if not marker.exists():
            marker.touch()
            herdr([
                "agent", "prompt", main_pane,
                f"[session-chat] ⚠ {name} blocked,需要审批/回答,详情用 agent read 查看",
            ])
            notify(f"{name} 被阻塞,已通知主会话")

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
            herdr(["agent", "prompt", main_pane, f"[session-chat] ☠ {name} 进程退出,任务未完成"])
            notify(f"{name} 进程退出")


if __name__ == "__main__":
    sys.exit(main())
