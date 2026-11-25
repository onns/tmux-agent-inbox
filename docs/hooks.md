# Agent hooks 手动配置

在 Git 安装插件后，按以下示例手动编辑对应 agent 的配置文件。插件不会改写这些文件，也不会自动注册任何 agent hooks。

示例使用默认安装路径 `~/.tmux/plugins/tmux-agent-inbox/`。若 Git checkout 位于其他目录，请调整命令中的路径。如果已经存在 `hooks`，将示例中的事件组追加到对应事件数组，保留其他 hook 和配置；不要直接覆盖整个文件或重复添加相同回调。

## Claude Code

配置文件：`~/.claude/settings.json`。

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.tmux/plugins/tmux-agent-inbox/tmux-agent-inbox\" hook Claude Stop",
            "timeout": 5
          }
        ]
      }
    ],
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.tmux/plugins/tmux-agent-inbox/tmux-agent-inbox\" hook Claude PermissionRequest",
            "timeout": 5
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "AskUserQuestion|ExitPlanMode|request_user_input|mcp__.*request_user_input",
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.tmux/plugins/tmux-agent-inbox/tmux-agent-inbox\" hook Claude PreToolUse",
            "timeout": 5
          }
        ]
      }
    ],
    "Notification": [
      {
        "matcher": "permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input",
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.tmux/plugins/tmux-agent-inbox/tmux-agent-inbox\" hook Claude Notification",
            "timeout": 5
          }
        ]
      }
    ],
    "StopFailure": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.tmux/plugins/tmux-agent-inbox/tmux-agent-inbox\" hook Claude StopFailure",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

## Codex

配置文件：`~/.codex/hooks.json`（设置了 `CODEX_HOME` 时使用 `$CODEX_HOME/hooks.json`）。

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.tmux/plugins/tmux-agent-inbox/tmux-agent-inbox\" hook Codex Stop",
            "timeout": 5
          }
        ]
      }
    ],
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.tmux/plugins/tmux-agent-inbox/tmux-agent-inbox\" hook Codex PermissionRequest",
            "timeout": 5
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "AskUserQuestion|ExitPlanMode|request_user_input|mcp__.*request_user_input",
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.tmux/plugins/tmux-agent-inbox/tmux-agent-inbox\" hook Codex PreToolUse",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

## 生效与说明

保存配置后重启对应 agent；Codex 可通过 `/hooks` 审阅并信任这些回调。回调接收事件的 JSON 输入，仅将通知写入本地收件箱，不替用户批准、拒绝或继续操作。请在 tmux pane 内启动 agent。Codex 的会话定位设置见 [README](../README.md#codex-会话定位)。
