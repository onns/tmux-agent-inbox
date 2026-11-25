#!/usr/bin/env python3
"""Load the plugin's tmux bindings and status widget at runtime."""
import argparse
from pathlib import Path
import re
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inbox import Inbox, ROOT, PYTHON


MARKER = "#{E:@agent-inbox-widget}"


def after_clock(text):
    """Place one marker immediately after a strftime clock, without changing theme sections."""
    text = re.sub(r"\s*" + re.escape(MARKER) + r"\s*", "", text)
    # Ignore escaped %% and support common tmux clock formats.
    clocks = list(re.finditer(r"(?<!%)%(?:R|T|H(?::%M(?::%S)?)?|I:%M(?:\s*%p)?)", text))
    if clocks:
        end = clocks[-1].end()
        return text[:end].rstrip() + MARKER + " " + text[end:].lstrip()
    return text.rstrip() + MARKER


def load():
    inbox = Inbox()
    def option(key, default):
        return inbox.call("show-options", "-gqv", key).strip() or default
    base = shlex.join([PYTHON, str(ROOT / "scripts/inbox.py"), "--socket", inbox.socket])
    popup = base + ' popup --client "#{client_name}"'
    inbox.call("bind-key", option("@agent-inbox-key", "N"), "run-shell", popup)
    inbox.call("bind-key", option("@agent-inbox-next-key", "M-n"), "run-shell",
               base + ' next --client "#{client_name}"')
    stored = inbox.call("show-options", "-gqv", "@agent-inbox-mouse-fallback").strip()
    if not stored:
        # Read the table: some tmux configurations filter an individual-key query to empty.
        bindings = inbox.call("list-keys", "-T", "root")
        match = re.search(r"(?m)^bind-key\s+-T\s+root\s+MouseDown1Status\s+(.+)$", bindings)
        stored = match.group(1) if match and "agent-inbox" not in match.group(1) else "select-window -t ="
        inbox.call("set-option", "-g", "@agent-inbox-mouse-fallback", stored)
    inbox.call("bind-key", "-T", "root", "MouseDown1Status", "if-shell", "-F",
               "#{==:#{mouse_status_range},agent-inbox}", "run-shell " + shlex.quote(popup), stored)
    if option("@agent-inbox-mouse", "on") == "on":
        inbox.call("set-option", "-g", "mouse", "on")
    # Migrate the former dedicated row, removing only a row owned by this plugin.
    index = option("@agent-inbox-status-index", "")
    if index:
        index = int(index)
        previous = option(f"status-format[{index}]", "")
        if "scripts/inbox.py" in previous and previous.endswith(" status)"):
            count = int(option("status", "1").replace("on", "1").replace("off", "0"))
            for row in range(index, count - 1):
                inbox.call("set-option", "-g", f"status-format[{row}]", option(f"status-format[{row+1}]", ""))
            inbox.call("set-option", "-gu", f"status-format[{max(index, count-1)}]")
            if count > index:
                inbox.call("set-option", "-g", "status", "on" if count == 2 else str(count - 1))
        inbox.call("set-option", "-gu", "@agent-inbox-status-index")
    # Inject the widget at runtime; no theme template needs plugin-specific markers.
    layout = option("status-format[0]", "")
    if MARKER in layout:
        inbox.call("set-option", "-g", "status-format[0]", layout.replace(MARKER, ""))
    right = inbox.call("show-options", "-gqv", "status-right").rstrip("\n")
    if option("@agent-inbox-status", "on") == "on":
        inbox.call("set-option", "-g", "@agent-inbox-widget", "#(" + base + " status)")
        inbox.call("set-option", "-g", "status-right", after_clock(right))
    else:
        inbox.call("set-option", "-g", "@agent-inbox-widget", "")
    # Appended hooks don't replace anyone else's hooks. Never clear on focus.
    inbox.call("set-hook", "-g", "pane-exited[9384]", "run-shell " + shlex.quote(base + " prune"))
    inbox.refresh()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["load"])
    parser.parse_args()
    load()


if __name__ == "__main__":
    main()
