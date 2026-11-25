import json
import os
from pathlib import Path
import pty
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from inbox import Inbox, event


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="agent-inbox-test-")
        cls.directory = Path(cls.temp.name)
        cls.socket = str(cls.directory / "tmux.sock")
        cls.environment = dict(os.environ, TMUX_NOTIFICATIONS_DIR=str(cls.directory / "state"), TERM="xterm-256color")
        cls.environment.pop("TMUX", None)
        cls.tmux("-f", "/dev/null", "new-session", "-d", "-s", "main", "-x", "120", "-y", "32", "sleep 600")
        cls.pane = cls.tmux("display-message", "-p", "#{pane_id}").strip()
        cls.other = cls.tmux("new-session", "-d", "-s", "other", "-P", "-F", "#{pane_id}", "sleep 600").strip()
        cls.hidden = cls.tmux("split-window", "-h", "-t", cls.other, "-P", "-F", "#{pane_id}", "sleep 600").strip()
        cls.master, slave = pty.openpty()
        import fcntl
        import struct
        import termios
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
        cls.client_process = subprocess.Popen(["tmux", "-S", cls.socket, "attach-session", "-t", "main"],
                                               stdin=slave, stdout=slave, stderr=slave, env=cls.environment)
        os.close(slave)
        for _ in range(40):
            clients = cls.tmux("list-clients", "-F", "#{client_name}").strip()
            if clients:
                cls.client = clients.splitlines()[0]
                break
            time.sleep(.05)
        else:
            raise RuntimeError("test tmux client did not attach")

    @classmethod
    def tmux(cls, *args, check=True):
        result = subprocess.run(["tmux", "-S", cls.socket, *args], env=cls.environment,
                                text=True, capture_output=True, timeout=10)
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout

    @classmethod
    def tearDownClass(cls):
        cls.tmux("kill-server", check=False)
        cls.client_process.wait(timeout=5)
        os.close(cls.master)
        cls.temp.cleanup()

    def setUp(self):
        self.env_patch = patch.dict(os.environ, self.environment)
        self.env_patch.start()
        self.inbox = Inbox(self.socket)
        with self.inbox.db:
            self.inbox.db.execute("DELETE FROM notifications")
        self.tmux("switch-client", "-c", self.client, "-t", self.pane)
        while select.select([self.master], [], [], 0)[0]:
            os.read(self.master, 65536)

    def tearDown(self):
        self.inbox.db.close()
        self.env_patch.stop()

    def push(self, pane=None, kind="done", message="完成"):
        self.inbox.push(pane or self.other, "Codex", kind, message, "/tmp/project")

    def test_notifications_survive_focus_and_age(self):
        self.push()
        with self.inbox.db:
            self.inbox.db.execute("UPDATE notifications SET created=?,updated=?", (time.time()-864000, time.time()-864000))
        self.tmux("switch-client", "-c", self.client, "-t", self.other)
        self.assertEqual(len(self.inbox.records()), 1)
        self.assertIn("待处理 1", self.inbox.status())

    def test_merge_and_priority(self):
        self.push(message="first")
        first = self.inbox.records()[0]
        self.push(message="latest")
        self.push(self.pane, "permission")
        rows = self.inbox.records()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["kind"], "permission")
        self.assertEqual(rows[1]["count"], 2)
        self.assertEqual(rows[1]["message"], "latest")
        self.assertEqual(rows[1]["created"], first["created"])

    def test_jump_exact_pane_across_session_and_zoom(self):
        self.tmux("select-pane", "-t", self.hidden)
        self.tmux("resize-pane", "-Z", "-t", self.hidden)
        self.push()
        self.inbox.jump(self.inbox.records()[0]["token"], self.client)
        self.assertEqual(self.tmux("display-message", "-p", "-c", self.client, "#{pane_id}").strip(), self.other)
        self.assertEqual(self.inbox.records(), [])

    def test_failed_jump_keeps_notification(self):
        self.push()
        with self.assertRaises(RuntimeError):
            self.inbox.jump(self.inbox.records()[0]["token"], "not-a-client")
        self.assertEqual(len(self.inbox.records()), 1)

    def test_new_arrival_during_jump_survives(self):
        self.push()
        token = self.inbox.records()[0]["token"]
        original = self.inbox.call
        def call(*args):
            result = original(*args)
            if args[0] == "switch-client":
                self.push(message="new arrival")
            return result
        with patch.object(self.inbox, "call", side_effect=call):
            self.inbox.jump(token, self.client)
        self.assertEqual(self.inbox.records()[0]["message"], "new arrival")

    def test_closed_pane_is_pruned(self):
        pane = self.tmux("new-window", "-d", "-t", "main", "-P", "-F", "#{pane_id}", "sleep 600").strip()
        self.push(pane)
        self.tmux("kill-pane", "-t", pane)
        self.assertEqual(self.inbox.records(), [])

    def test_server_namespace_isolation(self):
        self.push()
        key = self.inbox.key
        self.inbox.key += "different-server"
        self.assertEqual(self.inbox.records(), [])
        self.inbox.key = key
        self.assertEqual(len(self.inbox.records()), 1)

    def test_untrusted_text_is_literal(self):
        self.push(message='a\n\t\x1b[31mb #[bg=red] #(touch /tmp/nope)')
        row = self.inbox.records()[0]
        self.assertNotIn("\x1b", row["message"])
        self.assertNotIn("\n", row["message"])
        self.inbox.push(self.pane, "Codex", "done", "x", "/tmp/#[bg=red]")
        self.assertNotIn("#[bg=red]", self.inbox.status())

    def test_hooks_only_notify_and_ignore_idle_reminder(self):
        with patch.dict(os.environ, TMUX_PANE=self.other):
            event(self.inbox, "Claude", "Stop", {"last_assistant_message": "done"})
            token = self.inbox.records()[0]["token"]
            for name in ("UserPromptSubmit", "PostToolUse", "SessionEnd"):
                event(self.inbox, "Claude", name, {})
            self.assertEqual(self.inbox.records()[0]["token"], token)
            self.inbox.jump(token, self.client)
            event(self.inbox, "Claude", "Notification", {"notification_type": "idle_prompt"})
            self.assertEqual(self.inbox.records(), [])
            event(self.inbox, "Claude", "PermissionRequest", {"tool_input": {"command": "go test"}})
            self.assertEqual(self.inbox.records()[0]["kind"], "permission")

    def test_codex_stop_without_a_pane_is_retained_and_acknowledged(self):
        cwd = str(self.directory / "project")
        with patch.dict(os.environ, TMUX_PANE=self.other):
            for message in ("first", "latest"):
                event(self.inbox, "Codex", "Stop", {"session_id": "test-thread", "cwd": cwd,
                                                     "last_assistant_message": message})
        record, = self.inbox.records()
        self.assertEqual((record["kind"], record["location"], record["count"], record["message"]),
                         ("done", "未定位 pane", 2, "latest"))
        self.assertIn("待处理 1", self.inbox.status())
        self.inbox.jump(record["token"], self.client)
        self.assertEqual(self.tmux("display-message", "-p", "-c", self.client, "#{pane_id}").strip(), self.pane)
        self.assertEqual(self.inbox.records(), [])

    def test_codex_notification_follows_the_session_to_another_pane(self):
        session_id = "01a1f31b-b9cc-7632-9ed5-9f8be0000000"
        other_id = "01a1f31b-b9d2-78b0-957b-5909a0000000"
        title = f"workspace | codex | {session_id[:29]}..."
        other_title = f"workspace | codex | {other_id[:29]}..."
        panes = self.inbox.panes()
        panes[self.other].update(command="codex", title=title)
        panes[self.pane].update(command="codex", title=other_title)
        with patch.object(self.inbox, "panes", return_value=panes), patch.dict(os.environ, TMUX_PANE=self.pane):
            event(self.inbox, "Codex", "Stop", {"session_id": session_id, "cwd": str(self.directory)})
            self.assertEqual(self.inbox.records()[0]["target_pane"], self.other)
            panes[self.pane]["title"] = title
            self.assertIsNone(self.inbox.records()[0]["target_pane"])
            panes[self.other]["title"] = other_title
            self.assertEqual(self.inbox.records()[0]["target_pane"], self.pane)
            self.inbox.jump(self.inbox.records()[0]["token"], self.client)
            self.assertEqual(self.tmux("display-message", "-p", "-c", self.client, "#{pane_id}").strip(), self.pane)
            self.assertEqual(self.inbox.records(), [])

    def test_concurrent_writers(self):
        processes = [subprocess.Popen([sys.executable, str(ROOT / "scripts/inbox.py"), "--socket", self.socket,
                                      "push", "--pane", self.other, str(i)], env=self.environment,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(12)]
        for process in processes:
            _, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error.decode())
        self.assertEqual(self.inbox.records()[0]["count"], 12)

    def test_plugin_load_idempotent_and_preserves_hooks(self):
        self.tmux("set-hook", "-g", "pane-focus-in[7]", "display-message existing-focus")
        self.tmux("set-option", "-gu", "@agent-inbox-mouse-fallback")
        self.tmux("bind-key", "-T", "root", "MouseDown1Status", "display-message", "original-mouse-click")
        self.tmux("set-option", "-g", "status", "2")
        self.tmux("set-option", "-g", "@agent-inbox-status-index", "1")
        self.tmux("set-option", "-g", "status-format[1]", f"#[align=left]#(python3 {ROOT}/scripts/inbox.py status)")
        with tempfile.TemporaryDirectory() as home:
            home = Path(home)
            config_paths = [home / ".claude/settings.json", home / "custom codex/hooks.json"]
            for path in config_paths:
                path.parent.mkdir()
                path.write_text("{existing configuration, intentionally invalid JSON}")
            before = [path.read_bytes() for path in config_paths]
            env = dict(self.environment, TMUX=f"{self.socket},0,0", HOME=str(home),
                       CODEX_HOME=str(home / "custom codex"), TMUX_AGENT_INBOX_PYTHON=sys.executable)
            for _ in range(2):
                process = subprocess.run([str(ROOT / "agent-inbox.tmux")],
                                         env=env, capture_output=True, text=True)
                self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual([path.read_bytes() for path in config_paths], before)
            self.assertFalse((home / ".codex").exists())
        self.assertIn(self.tmux("show-options", "-gv", "status").strip(), ("1", "on"))
        self.assertEqual(self.tmux("show-options", "-gv", "status-format[0]").count("#{E:@agent-inbox-widget}"), 0)
        self.assertEqual(self.tmux("show-options", "-gv", "status-right").count("#{E:@agent-inbox-widget}"), 1)
        self.assertIn("existing-focus", self.tmux("show-hooks", "-g", "pane-focus-in"))
        bindings = self.tmux("list-keys", "-T", "root")
        self.assertIn("agent-inbox", bindings)
        self.assertIn("original-mouse-click", bindings)
        self.assertIn("inbox.py", self.tmux("list-keys", "-T", "prefix"))

    def test_real_popup_enter_and_escape(self):
        self.push()
        command = [sys.executable, str(ROOT / "scripts/inbox.py"), "--socket", self.socket,
                   "popup", "--client", self.client]
        for key in (b"\x1b", b"\r"):
            proc = subprocess.Popen(command, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(.4)
            os.write(self.master, key)
            _, error = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, error.decode())
            if key == b"\x1b":
                self.assertEqual(len(self.inbox.records()), 1)
        for _ in range(30):
            if not self.inbox.records():
                break
            time.sleep(.05)
        self.assertEqual(self.inbox.records(), [])
        self.assertEqual(self.tmux("display-message", "-p", "-c", self.client, "#{pane_id}").strip(), self.other)

    def test_real_mouse_status_and_select_second_row(self):
        self.tmux("set-option", "-g", "status-right", "")
        env = dict(self.environment, TMUX=f"{self.socket},0,0")
        subprocess.run([sys.executable, str(ROOT / "scripts/configure.py"), "load"], env=env, check=True)
        self.push(message="FIRST_ROW")
        self.push(self.hidden, message="SECOND_ROW_TARGET")
        time.sleep(.3)
        while select.select([self.master], [], [], .05)[0]:
            os.read(self.master, 65536)
        # Click the inline badge at the right of the existing status line.
        os.write(self.master, b"\x1b[<0;115;32M\x1b[<0;115;32m")
        output = b""
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and b"SECOND_ROW_TARGET" not in output:
            if select.select([self.master], [], [], .1)[0]:
                output += os.read(self.master, 65536)
        self.assertIn(b"SECOND_ROW_TARGET", output)
        text = output.decode(errors="replace")
        before = text[:text.index("SECOND_ROW_TARGET")]
        positions = list(re.finditer(r"\x1b\[(\d+);(\d+)H", before))
        row = int(positions[-1].group(1))
        # A single click on the second entry must pick that row, not the initial selection.
        os.write(self.master, f"\x1b[<0;12;{row}M\x1b[<0;12;{row}m".encode())
        for _ in range(40):
            if len(self.inbox.records()) == 1:
                break
            time.sleep(.05)
        self.assertEqual([r["pane"] for r in self.inbox.records()], [self.other])
        self.assertEqual(self.tmux("display-message", "-p", "-c", self.client, "#{pane_id}").strip(), self.hidden)

    def test_hook_cli_and_neutral_codex_failure(self):
        env = dict(self.environment, TMUX=f"{self.socket},0,0", TMUX_PANE=self.other)
        result = subprocess.run([str(ROOT / "tmux-agent-inbox"), "hook", "Claude", "Stop"],
                                input=json.dumps({"last_assistant_message": "CLI done"}), text=True,
                                capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.inbox.records()[0]["message"], "CLI done")
        token = self.inbox.records()[0]["token"]
        # Missing session cwd must not let a daemon overwrite another pane's
        # notification; Stop still returns an explicit neutral hook response.
        result = subprocess.run([str(ROOT / "tmux-agent-inbox"), "hook", "Codex", "Stop"],
                                input='{}', text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertIn("cwd", result.stderr)
        self.assertEqual(self.inbox.records()[0]["token"], token)


class HookDocumentationTests(unittest.TestCase):
    def test_documented_hook_commands(self):
        import shlex
        text = (ROOT / "docs/hooks.md").read_text()
        blocks = re.findall(r"```json\n(.*?)\n```", text, flags=re.S)
        self.assertEqual(len(blocks), 2)
        for agent, block in zip(("Claude", "Codex"), blocks):
            hooks = json.loads(block)["hooks"]
            expected = {"Stop", "PermissionRequest", "PreToolUse"}
            if agent == "Claude":
                expected.update({"Notification", "StopFailure"})
            self.assertEqual(set(hooks), expected)
            for event_name, groups in hooks.items():
                group, = groups
                handler, = group["hooks"]
                self.assertEqual(handler["type"], "command")
                self.assertEqual(shlex.split(handler["command"]), [
                    "$HOME/.tmux/plugins/tmux-agent-inbox/tmux-agent-inbox",
                    "hook", agent, event_name])
                self.assertEqual(handler["timeout"], 5)
            self.assertEqual(hooks["PreToolUse"][0]["matcher"],
                             "AskUserQuestion|ExitPlanMode|request_user_input|mcp__.*request_user_input")
            if agent == "Claude":
                self.assertEqual(hooks["Notification"][0]["matcher"],
                                 "permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input")

    def test_automatic_install_is_not_available(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home / "custom codex"),
                       TMUX_AGENT_INBOX_PYTHON=sys.executable)
            for command in ([str(ROOT / "tmux-agent-inbox"), "install-hooks", "--apply"],
                            [sys.executable, str(ROOT / "scripts/configure.py"), "install", "--apply"]):
                result = subprocess.run(command, env=env, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(home.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
