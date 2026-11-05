#!/usr/bin/env bash
# Install the current checkout's automatic project indexing as a user service.
set -euo pipefail
project_dir="$(dirname "$(dirname "$(realpath "$0")")")"
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir"
# Systemd uses quoting for paths with spaces; escape backslash and quote explicitly.
service_command="${project_dir//\\/\\\\}/scripts/rag"
service_command="${service_command//\"/\\\"}"
cat > "$unit_dir/tdlib-rag.service" <<UNIT
[Unit]
Description=Incremental tdlib-multi-mcp knowledge index
After=ollama.service

[Service]
Type=oneshot
ExecStart="$service_command" sync
TimeoutStartSec=15min
UMask=0077
Nice=10
UNIT
cat > "$unit_dir/tdlib-rag.timer" <<'UNIT'
[Unit]
Description=Refresh tdlib-multi-mcp RAG every minute

[Timer]
OnBootSec=30s
OnUnitInactiveSec=60s
AccuracySec=5s
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now tdlib-rag.timer
systemctl --user start --no-block tdlib-rag.service
