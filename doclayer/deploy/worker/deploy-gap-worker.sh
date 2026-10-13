#!/usr/bin/env bash
# Install the owning gap timer from an already deployed, committed curator tree.
# Ledger migration and credential provisioning are separate, preserved steps.
set -euo pipefail
TARGET="${1:-worker1}"
SOURCE="${2:-$HOME/git/brain}"
if [ -n "$(git -C "$SOURCE" status --porcelain)" ]; then
    echo 'Refusing dirty deployment source' >&2
    exit 1
fi
REV="$(git -C "$SOURCE" rev-parse HEAD)"
ssh "$TARGET" bash -s "$REV" <<'REMOTE'
set -euo pipefail
EXPECTED="$1"
CHECKOUT="$HOME/git/brain-curator"
UNIT_DIR="$HOME/.config/systemd/user"
if [ "$(git -C "$CHECKOUT" rev-parse HEAD)" != "$EXPECTED" ] || [ -n "$(git -C "$CHECKOUT" status --porcelain)" ]; then
    echo 'Deploy the same clean curator revision first' >&2
    exit 1
fi
# Exact canonical pointer and existing API access must be provisioned first.
test -s "$HOME"/.config/doclayer/brain-repo
test -s "$HOME"/.config/doclayer/vo-rag-api-key
test -s "$HOME"/.config/brain-curator/config.yaml
BACKUP="$HOME/.local/state/doclayer/deployment-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP" "$UNIT_DIR"
chmod 700 "$BACKUP"
for UNIT in doclayer-gap-worker.service doclayer-gap-worker.timer; do
    if [ -f "$UNIT_DIR/$UNIT" ]; then cp "$UNIT_DIR/$UNIT" "$BACKUP/$UNIT"; fi
    install -m 644 "$CHECKOUT/doclayer/deploy/worker/$UNIT" "$UNIT_DIR/$UNIT"
done
systemctl --user daemon-reload
systemctl --user enable --now doclayer-gap-worker.timer
systemctl --user start doclayer-gap-worker.service
systemctl --user show doclayer-gap-worker.service -p Result -p ExecMainStatus
# This script deliberately does not write the curator-ready flag. Existing
# readiness and all daily allowances survive; qualification is recorded first.
REMOTE
