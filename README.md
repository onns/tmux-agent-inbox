# tmux-agent-inbox

独立的 tmux 通知收件箱插件，使用 Python 标准库保存 Claude Code 和 Codex 的待处理通知。点击状态栏或 Prefix + N 打开列表，Prefix + Alt-n 处理下一条。在列表中按 Ctrl+D 可删除选中的通知，不跳转 pane。

## 安装

需要 Python 3.9+、tmux 和 fzf。使用 Git 安装插件；以下示例使用 GitHub 仓库 `onns/tmux-agent-inbox`，如果使用其他仓库地址，请替换对应 URL。

### TPM

在 tmux 配置中、TPM 初始化语句之前加入：

```tmux
set -g @plugin 'onns/tmux-agent-inbox'
```

重新加载 tmux 配置后，按 Prefix + I 安装插件。使用 TPM 的 Prefix + U 更新插件。

### Git clone（不使用 TPM）

```bash
git clone https://github.com/onns/tmux-agent-inbox.git ~/.tmux/plugins/tmux-agent-inbox
```

在 tmux 配置中加入：

```tmux
run-shell ~/.tmux/plugins/tmux-agent-inbox/agent-inbox.tmux
```

重新加载配置即可。更新时执行：

```bash
git -C ~/.tmux/plugins/tmux-agent-inbox pull --ff-only
~/.tmux/plugins/tmux-agent-inbox/tmux-agent-inbox reload
```

TPM 和手动加载选择其一。插件只在 tmux 运行时注册状态栏、快捷键及 pane 退出事件，不修改配置文件。

## 通知列表

| 操作 | 作用 |
| --- | --- |
| `Prefix + N` / 点击状态栏 | 打开通知列表 |
| `Enter` / 点击某条通知 | 处理通知，并在可以定位时跳转 pane |
| `Ctrl+D` | 删除选中的通知，留在列表中继续操作 |
| `Esc` | 关闭列表，保留剩余通知 |
| `Prefix + Alt-n` | 处理下一条通知 |

`Ctrl+D` 只清除收件箱中的该条通知和状态栏计数，不会关闭 pane、停止 agent 或改变其任务状态。如果选中后收到新的通知，旧条目的删除不会清除新的通知。

## Agent hooks

按照 [Claude Code / Codex hooks 配置](docs/hooks.md) 手动配置事件回调。插件不会读取、生成、合并或修改 agent 的配置文件。

## 选项

在插件加载之前设置：

| 选项 | 默认值 | 作用 |
| --- | --- | --- |
| `@agent-inbox-key` | `N` | 打开通知列表 |
| `@agent-inbox-next-key` | `M-n` | 处理下一条通知 |
| `@agent-inbox-status` | `on` | 显示状态栏通知 |
| `@agent-inbox-mouse` | `on` | 启用鼠标 |

通知数据保存在 `~/.local/state/tmux-agent-inbox/`，可通过 `TMUX_NOTIFICATIONS_DIR` 覆盖；Python 解释器可通过 `TMUX_AGENT_INBOX_PYTHON` 指定。

## Codex 会话定位

Codex 同目录多开时，在 Codex 配置的 `[tui]` 下将 `terminal_title` 设为 `['activity', 'thread-name', 'project-name', 'app-name', 'session-id']`。也可以在已打开的 Codex 里执行 `/title activity thread-name project-name app-name session-id`。插件从 tmux pane 标题读取会话标识，打开通知时跳到当前显示该会话的唯一 pane。

旧客户端在同目录只有一个 Codex pane 时可以按目录定位。若找不到唯一 pane，完成通知按会话进入收件箱，标为「未定位 pane」。这类通知在处理时显示摘要并清除。授权和待回复事件仍需定位到 pane。

## 卸载

从 tmux 配置删除插件注册或 `run-shell` 语句，手动删除对应的 agent hooks，再移除 Git checkout。通知数据库独立保存，可保留供以后使用。

## 测试

```bash
python3 -m unittest discover -s tests
```
