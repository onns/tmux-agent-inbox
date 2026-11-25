#!/bin/sh
plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$plugin_root/tmux-agent-inbox" integrate
