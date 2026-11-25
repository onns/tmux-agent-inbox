#!/usr/bin/env python3
"""Persistent, pane-addressed notification inbox. Python standard library only."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(Path(sys.executable).resolve())
PRIORITY = {"permission": 0, "input": 1, "error": 2, "done": 3}
LABELS = {"permission": "需授权", "input": "待回复", "error": "出错", "done": "已完成"}
CODEX_SESSION_TITLE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-(?:[0-9a-f]{12}|[0-9a-f]{5}\.\.\.)")


def clean(value, limit=180):
    # Untrusted agent/project text must not inject terminal escapes, TSV or tmux formats.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value or ""))
    return " ".join("".join(c for c in text if c.isprintable() or c.isspace()).split())[:limit]


class PaneResolutionError(RuntimeError):
    pass


def codex_pane_session(info):
    if not (info["command"] == "codex" or info["command"].startswith("codex-")):
        return None
    parts = info["title"].rsplit(" | ", 2)
    if len(parts) >= 2 and parts[-2] == "codex" and CODEX_SESSION_TITLE.fullmatch(parts[-1]):
        return parts[-1]
    return None


def codex_session_panes(panes, session_id):
    if not isinstance(session_id, str) or not CODEX_SESSION_TITLE.fullmatch(session_id) or len(session_id) != 36:
        return []
    # Codex 0.170 caps the terminal title segment at 32 characters, including the ellipsis.
    markers = {session_id, session_id[:29] + "..."}
    return [pane for pane, info in panes.items() if codex_pane_session(info) in markers]


class Inbox:
    def __init__(self, socket=None):
        self.socket = socket or os.environ.get("TMUX", "").rsplit(",", 2)[0]
        self.prefix = ["tmux"] + (["-S", self.socket] if self.socket else [])
        self.server = self.call("display-message", "-p", "#{pid}").strip()
        if not self.server.isdigit():
            raise RuntimeError("无法连接 tmux server")
        if not self.socket:
            self.socket = self.call("display-message", "-p", "#{socket_path}").strip()
            self.prefix = ["tmux", "-S", self.socket]
        self.key = self.socket + ":" + self.server
        directory = Path(os.environ.get("TMUX_NOTIFICATIONS_DIR", str(Path.home() / ".local/state/tmux-agent-inbox")))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(directory / "inbox.sqlite3", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS notifications (
            server TEXT, pane TEXT, pane_pid TEXT, token TEXT, source TEXT, kind TEXT,
            project TEXT, message TEXT, created REAL, updated REAL, count INTEGER,
            PRIMARY KEY(server, pane))""")

    def call(self, *args, timeout=5):
        result = subprocess.run(self.prefix + list(args), text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "tmux command failed")
        return result.stdout

    def panes(self):
        fields = ("#{pane_id}\t#{pane_pid}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{window_name}\t"
                  "#{pane_current_command}\t#{pane_current_path}\t#{pane_title}")
        result = {}
        for line in self.call("list-panes", "-a", "-F", fields).splitlines():
            values = line.split("\t", 8)
            if len(values) == 9:
                pane, pid, session, window, index, name, command, cwd, title = values
                result.setdefault(pane, dict(pid=pid, location=f"{session}:{window}.{index}",
                                            name=name, command=command, cwd=cwd, title=title))
        return result

    def hook_pane(self, source, data, session_id=None):
        inherited = os.environ.get("TMUX_PANE", "")
        if source != "Codex":
            return inherited
        panes = self.panes()
        matches = codex_session_panes(panes, session_id)
        if len(matches) == 1:
            return f"thread:{session_id}"
        if matches:
            raise PaneResolutionError("多个 Codex pane 显示同一个会话")
        # Clients without a session title can still be located by a unique working directory.
        cwd = data.get("cwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            raise PaneResolutionError("Codex hook 缺少有效 cwd，无法确定通知 pane")
        cwd = os.path.realpath(cwd)
        candidates = [pane for pane, info in panes.items()
                      if (info["command"] == "codex" or info["command"].startswith("codex-"))
                      and os.path.realpath(info["cwd"]) == cwd]
        if len(candidates) != 1:
            raise PaneResolutionError(f"无法唯一确定 Codex 通知 pane（同目录候选数：{len(candidates)}）")
        if codex_pane_session(panes[candidates[0]]):
            raise PaneResolutionError("同目录的 Codex pane 显示其他会话")
        return candidates[0]

    def records(self):
        panes = self.panes()  # If tmux fails, preserve all records rather than treating them as dead.
        rows = self.db.execute("SELECT * FROM notifications WHERE server=?", (self.key,)).fetchall()
        result = []
        with self.db:
            for row in rows:
                if row["pane"].startswith("thread:"):
                    matches = codex_session_panes(panes, row["pane"].removeprefix("thread:"))
                    target = matches[0] if len(matches) == 1 else None
                    pane = panes[target] if target else {"location": "未定位 pane", "name": ""}
                    result.append(dict(row, target_pane=target, location=clean(pane["location"]),
                                       window=clean(pane["name"])))
                    continue
                pane = panes.get(row["pane"])
                if not pane or pane["pid"] != row["pane_pid"]:
                    self.db.execute("DELETE FROM notifications WHERE server=? AND pane=? AND token=?",
                                    (self.key, row["pane"], row["token"]))
                else:
                    result.append(dict(row, location=clean(pane["location"]), window=clean(pane["name"])))
        return sorted(result, key=lambda r: (PRIORITY.get(r["kind"], 9), r["created"]))

    def push(self, pane, source, kind, message, cwd=""):
        if not re.fullmatch(r"%\d+", pane):
            raise ValueError("需要有效的 TMUX_PANE（例如 %3）")
        info = self.panes().get(pane)
        if not info:
            return
        self.store(pane, info["pid"], source, kind, message, cwd or info["cwd"])

    def store(self, key, pane_pid, source, kind, message, cwd):
        project = Path(cwd).name or cwd or source
        if Path(cwd).parent.name.endswith(".git"):
            project = Path(cwd).parent.name[:-4] + "/" + project
        now = time.time()
        with self.db:
            self.db.execute("""INSERT INTO notifications VALUES (?,?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(server,pane) DO UPDATE SET
                pane_pid=excluded.pane_pid, token=excluded.token, source=excluded.source,
                kind=excluded.kind, project=excluded.project, message=excluded.message,
                created=CASE WHEN notifications.pane_pid=excluded.pane_pid THEN notifications.created ELSE excluded.created END,
                updated=excluded.updated,
                count=CASE WHEN notifications.pane_pid=excluded.pane_pid THEN notifications.count+1 ELSE 1 END""",
                (self.key, key, pane_pid, uuid.uuid4().hex, clean(source, 30), kind,
                 clean(project, 60), clean(message), now, now))
        self.refresh()

    def refresh(self):
        try:
            self.call("refresh-client", "-S")
        except RuntimeError:
            pass  # No attached clients is normal for hook producers.

    def jump(self, token, client):
        record = next((r for r in self.records() if r["token"] == token), None)
        if record is None:
            raise RuntimeError("这条通知已更新或 pane 已关闭；请重新打开列表")
        if not client:
            raise RuntimeError("无法确定发起操作的 tmux client，通知已保留")
        pane = record.get("target_pane", record["pane"])
        if pane is None:
            message = f"{LABELS[record['kind']]} · {record['project']} · {record['message']}"
            self.call("display-message", "-l", "-c", client, "--", message)
        else:
            # Switch the requesting client only. -Z reveals panes hidden by a zoomed neighbour.
            self.call("switch-client", "-c", client, "-t", pane)
            self.call("select-pane", "-t", pane, "-Z")
            actual = self.call("display-message", "-p", "-c", client, "#{pane_id}").strip()
            if actual != pane:
                raise RuntimeError("pane 跳转未完成，通知已保留")
        with self.db:
            # A notification arriving during navigation must survive this acknowledgement.
            self.db.execute("DELETE FROM notifications WHERE server=? AND pane=? AND token=?",
                            (self.key, record["pane"], token))
        self.refresh()

    def lines(self):
        for row in self.records():
            age = max(0, int(time.time() - row["created"]))
            age_text = f"{age // 3600}h" if age >= 3600 else f"{age // 60}m" if age >= 60 else f"{age}s"
            yield "\t".join([row["token"], LABELS.get(row["kind"], row["kind"]), row["source"],
                             row["project"], row["location"], age_text, row["message"]])

    def picker(self, client):
        if not self.records():
            self.call("display-message", "-c", client, "没有待处理通知")
            return
        # Periodic reload includes new arrivals while retaining fzf's query/selection.
        reload_command = shlex.join([PYTHON, str(ROOT / "scripts/inbox.py"),
                                     "--socket", self.socket, "list"])
        env = dict(os.environ, FZF_DEFAULT_OPTS="", FZF_DEFAULT_OPTS_FILE="/dev/null")
        command = ["fzf", "--reverse", "--no-sort", "--delimiter=\t", "--with-nth=2..", "--no-multi",
                   "--header=待处理通知 · 单击 / Enter 处理 · 可定位时跳转 · Esc 保留退出",
                   "--prompt=搜索 > ", "--bind=left-click:accept,double-click:accept",
                   "--bind=" + "ctrl-r:reload(" + reload_command + ")",
                   "--bind=" + "every(2):reload(" + reload_command + ")"]
        result = subprocess.run(command, input="\n".join(self.lines()) + "\n",
                                stdout=subprocess.PIPE, text=True, env=env)
        if result.returncode == 0 and result.stdout.strip():
            self.jump(result.stdout.split("\t", 1)[0], client)

    def popup(self, client):
        if not client:
            raise RuntimeError("请在 tmux 内使用 prefix + N")
        command = shlex.join([PYTHON, str(ROOT / "scripts/inbox.py"), "--socket", self.socket,
                              "picker", "--client", client])
        # display-popup stays attached until the popup closes; the normal tmux
        # command timeout would kill it after five seconds.
        self.call("display-popup", "-c", client, "-w", "90%", "-h", "70%", "-E", command, timeout=None)

    def status(self):
        rows = self.records()
        if not rows:
            return ""
        style = "fg=colour232,bg=colour220,bold"
        return (" #[range=user|agent-inbox]#[" + style + "] 待处理 " + str(len(rows)) +
                " #[default]#[range=right]")


def event(inbox, source, name, data):
    pane = os.environ.get("TMUX_PANE", "")
    if not pane or data.get("agent_id") or name in ("SubagentStop", "SubagentStart"):
        return
    kind, message = None, ""
    if name == "Stop":
        kind, message = "done", data.get("last_assistant_message") or "任务已完成"
    elif name == "PermissionRequest":
        tool = data.get("tool_input") or {}
        tool = tool if isinstance(tool, dict) else {}
        kind = "permission"
        message = tool.get("description") or tool.get("command") or f'{data.get("tool_name", "工具")} 需要授权'
    elif name == "PreToolUse" and (data.get("tool_name") in ("AskUserQuestion", "ExitPlanMode", "request_user_input")
                                   or re.fullmatch(r"mcp__.*request_user_input", str(data.get("tool_name", "")))):
        kind, message = "input", "等待你的回答 / 方案确认"
    elif name == "Notification":
        subtype = data.get("notification_type", "")
        if subtype in ("permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
            kind = "permission" if subtype == "permission_prompt" else "input"
            message = data.get("message") or "等待处理"
        # idle_prompt is a delayed reminder of Stop; it must not recreate an acknowledged notification.
    elif name == "StopFailure":
        kind, message = "error", data.get("error") or "任务出错"
    elif name == "agent-turn-complete":
        kind, message = "done", data.get("last-assistant-message") or "任务已完成"
    if kind:
        cwd = data.get("cwd")
        cwd = cwd if isinstance(cwd, str) else ""
        session_id = data.get("session_id") or data.get("thread-id")
        try:
            target = inbox.hook_pane(source, data, session_id)
        except PaneResolutionError:
            if kind != "done" or not isinstance(session_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", session_id):
                raise
            target = f"thread:{session_id}"
        if target.startswith("thread:"):
            inbox.store(target, "", source, kind, message, cwd)
        else:
            inbox.push(target, source, kind, message, cwd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket")
    sub = parser.add_subparsers(dest="command", required=True)
    hook = sub.add_parser("hook")
    hook.add_argument("source", choices=["Claude", "Codex"])
    hook.add_argument("event")
    hook.add_argument("payload", nargs="?")
    for name in ("picker", "popup", "next"):
        sub.add_parser(name).add_argument("--client", required=True)
    jump = sub.add_parser("jump")
    jump.add_argument("token")
    jump.add_argument("--client", required=True)
    sub.add_parser("status")
    sub.add_parser("prune")
    listing = sub.add_parser("list")
    listing.add_argument("--json", action="store_true")
    push = sub.add_parser("push")
    push.add_argument("--pane", default=os.environ.get("TMUX_PANE", ""))
    push.add_argument("--source", default="Agent")
    push.add_argument("--kind", choices=PRIORITY, default="done")
    push.add_argument("message")
    args = parser.parse_args()
    is_hook = args.command == "hook"
    try:
        if is_hook and not os.environ.get("TMUX_PANE"):
            return
        inbox = Inbox(args.socket)
        if is_hook:
            payload = args.payload if args.payload is not None else sys.stdin.read()
            data = json.loads(payload or "{}")
            if not isinstance(data, dict):
                return
            name = data.get("type", "") if args.event == "notify" else args.event
            event(inbox, args.source, name, data)
        elif args.command == "push":
            inbox.push(args.pane, args.source, args.kind, args.message)
        elif args.command == "list":
            print(json.dumps(inbox.records(), ensure_ascii=False) if args.json else "\n".join(inbox.lines()))
        elif args.command == "status":
            print(inbox.status())
        elif args.command == "prune":
            inbox.records()
        elif args.command == "jump":
            inbox.jump(args.token, args.client)
        elif args.command == "next":
            rows = inbox.records()
            if rows:
                inbox.jump(rows[0]["token"], args.client)
        elif args.command == "popup":
            inbox.popup(args.client)
        elif args.command == "picker":
            inbox.picker(args.client)
    except (RuntimeError, ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"tmux-agent-inbox: {exc}", file=sys.stderr)
        if not is_hook and args.command != "status":
            raise SystemExit(1)
    finally:
        if is_hook and args.source == "Codex" and args.event == "Stop":
            print("{}")  # Explicit neutral hook response; never approve, deny or continue a turn.


if __name__ == "__main__":
    main()
