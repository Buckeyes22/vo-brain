#!/usr/bin/env bash
# After deploying this committed package, install only the registered consumer timer.
# No live consumer process, provider config, credentials, or Git config is changed.
# Usage: install-worker.sh <ssh-host>
set -euo pipefail
TARGET="${1:?usage: install-worker.sh <ssh-host>}"
ssh "$TARGET" bash -s <<'REMOTE'
set -euo pipefail
artifacts="$HOME/git/brain-curator/doclayer/deploy/vault-sync"
command="$HOME/git/brain-curator/doclayer/.venv/bin/brain-vault-sync"
config_dir="$HOME/.config/brain-vault-sync"
unit_dir="$HOME/.config/systemd/user"
state_dir="$HOME/.local/state/brain-vault-sync"
backup_dir="$state_dir/deployment-backups/$(date -u +%Y%m%dT%H%M%SZ)"
test -x "$command"
mkdir -p "$config_dir" "$unit_dir" "$backup_dir"
chmod 700 "$config_dir" "$state_dir" "$backup_dir"
for file in "$config_dir/consumers.json" "$unit_dir/brain-vault-sync.service" "$unit_dir/brain-vault-sync.timer"; do
  if test -f "$file"; then cp -p "$file" "$backup_dir/$(basename "$file")"; fi
done
systemctl --user is-enabled brain-vault-sync.timer > "$backup_dir/previous-enabled.txt" 2>&1 || true
if ! test -e "$config_dir/consumers.json"; then
  install -m 600 "$artifacts/consumers.example.json" "$config_dir/consumers.json"
fi
install -m 600 "$artifacts/brain-vault-sync.service" "$unit_dir/brain-vault-sync.service"
install -m 600 "$artifacts/brain-vault-sync.timer" "$unit_dir/brain-vault-sync.timer"
systemctl --user daemon-reload
systemctl --user enable --now brain-vault-sync.timer
systemctl --user start brain-vault-sync.service
"$command" --config "$config_dir/consumers.json" --status
REMOTE
