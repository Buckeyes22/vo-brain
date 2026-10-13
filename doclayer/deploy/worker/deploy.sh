#!/usr/bin/env bash
# Deploy the brain curator worker to the target host (default: worker1).
#
# Idempotent: safe to re-run. It (re)installs the committed implementation
# into the dedicated checkout, syncs the venv, writes/refreshes the systemd
# user unit, and restarts the service. It NEVER touches credentials (they
# live in ~/.config/brain-curator/credentials.env, created once by hand),
# never touches the live Obsidian vault checkout, and refuses to
# run against a dirty implementation checkout.
#
# Usage (from the workstation):
#   doclayer/deploy/worker/deploy.sh [user@host] [brain-checkout-on-workstation]
#
# What it creates on the target (the default paths; see docs/configuration.md):
#   ~/git/brain-curator                           dedicated brain clone + curator code
#   ~/.local/share/brain-curator/jobs              durable job store
#   ~/.local/share/brain-curator/worktrees         publication worktrees
#   ~/.cache/brain-curator                         source cache
#   ~/.config/brain-curator/config.yaml            worker config (if absent)
#   ~/.config/systemd/user/brain-curator.service   user unit
#
# Rollback: see docs/curator.md (Rollback).

set -euo pipefail

TARGET="${1:-worker1}"
SOURCE_CHECKOUT="${2:-$HOME/git/brain-curator-impl}"
BRAIN_REMOTE="${BRAIN_REMOTE:-https://git.example.org/example/brain.git}"
UNIT_NAME="brain-curator.service"

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 0. Preflight on the workstation: the implementation checkout must be clean.
# --------------------------------------------------------------------------- #
log "preflight: checking implementation checkout $SOURCE_CHECKOUT"
if [ ! -d "$SOURCE_CHECKOUT/doclayer" ]; then
    die "$SOURCE_CHECKOUT does not look like a brain checkout with doclayer/"
fi
if [ -n "$(git -C "$SOURCE_CHECKOUT" status --porcelain)" ]; then
    die "implementation checkout is dirty; deploy runs from a committed revision only"
fi
DEPLOY_REV="$(git -C "$SOURCE_CHECKOUT" rev-parse HEAD)"
log "deploying revision $DEPLOY_REV"

# --------------------------------------------------------------------------- #
# 1. Push the revision to the Forgejo remote so the target can fetch it.
#    (Deploys run from the workstation over SSH, never from CI.)
#    We do NOT force anything: the branch must fast-forward or fail loudly.
# --------------------------------------------------------------------------- #
CURATOR_BRANCH="${CURATOR_BRANCH:-curator-impl}"
CURATOR_BRANCH_PUSHED=0
if git -C "$SOURCE_CHECKOUT" push origin "HEAD:refs/heads/$CURATOR_BRANCH" 2>/dev/null; then
    CURATOR_BRANCH_PUSHED=1
    log "pushed $DEPLOY_REV to origin/$CURATOR_BRANCH"
else
    log "could not push origin/$CURATOR_BRANCH; will transfer the committed revision as a Git bundle"
fi

# --------------------------------------------------------------------------- #
# 2. Prepare the target: directories, clone, venv, unit, config.
# --------------------------------------------------------------------------- #
log "preparing target host $TARGET"
# Paths live under the target user's home, which may differ from this host's.
REMOTE_HOME="$(ssh "$TARGET" 'printf %s "$HOME"')" || die "ssh to $TARGET failed"
[ -n "$REMOTE_HOME" ] || die "could not resolve the home directory on $TARGET"
HOST_DIR="$REMOTE_HOME/git/brain-curator"
JOBS_DIR="$REMOTE_HOME/.local/share/brain-curator"
CACHE_DIR="$REMOTE_HOME/.cache/brain-curator"
CONFIG_DIR="$REMOTE_HOME/.config/brain-curator"
UNIT_DIR="$REMOTE_HOME/.config/systemd/user"
# Shell-quoted copies for remote command strings (the remote home may contain spaces).
Q_HOST_DIR="$(printf %q "$HOST_DIR")"
Q_CACHE_DIR="$(printf %q "$CACHE_DIR")"
Q_CONFIG_DIR="$(printf %q "$CONFIG_DIR")"
Q_UNIT_DIR="$(printf %q "$UNIT_DIR")"
if ssh "$TARGET" bash -s "$BRAIN_REMOTE" "$HOST_DIR" "$JOBS_DIR" "$CACHE_DIR" "$CONFIG_DIR" "$DEPLOY_REV" "$CURATOR_BRANCH" "$CURATOR_BRANCH_PUSHED" <<'REMOTE'
set -euo pipefail
BRAIN_REMOTE="$1"; HOST_DIR="$2"; JOBS_DIR="$3"; CACHE_DIR="$4"; CONFIG_DIR="$5"
DEPLOY_REV="$6"; CURATOR_BRANCH="$7"; PUSHED="$8"

echo "[deploy] creating state directories"
mkdir -p "$(dirname "$HOST_DIR")" "$JOBS_DIR/jobs" "$JOBS_DIR/worktrees" "$CACHE_DIR" "$CONFIG_DIR"

if [ ! -d "$HOST_DIR/.git" ]; then
    echo "[deploy] cloning $BRAIN_REMOTE into $HOST_DIR"
    git clone "$BRAIN_REMOTE" "$HOST_DIR"
else
    if [ -n "$(git -C "$HOST_DIR" status --porcelain)" ]; then
        echo "[deploy] refusing to overwrite dirty worker checkout" >&2
        exit 93
    fi
    git -C "$HOST_DIR" remote set-url origin "$BRAIN_REMOTE"
fi

if [ "$PUSHED" = "1" ]; then
    echo "[deploy] fetching + checking out committed revision"
    git -C "$HOST_DIR" fetch origin
    git -C "$HOST_DIR" checkout --detach -q "$DEPLOY_REV"
else
    echo "[deploy] committed Git bundle transfer needed (revision $DEPLOY_REV)"
    exit 91
fi

echo "[deploy] syncing venv (uv)"
cd "$HOST_DIR/doclayer"
if ! command -v uv >/dev/null 2>&1; then
    echo "[deploy] uv not found on PATH; install per https://docs.astral.sh/uv/ before deploying" >&2
    exit 92
fi
uv sync --frozen

echo "[deploy] smoke: venv entry points exist"
test -x "$HOST_DIR/doclayer/.venv/bin/brain-curator"
test -x "$HOST_DIR/doclayer/.venv/bin/brain-curator-worker"
test -x "$HOST_DIR/doclayer/.venv/bin/brain-curator-mcp"
REMOTE
then
    rc=0
else
    rc=$?
fi

if [ "$rc" -eq 91 ]; then
    log "bundle fallback: transferring committed Git history to target"
    BUNDLE_DIR="$(mktemp -d)"
    trap 'rm -rf "$BUNDLE_DIR"' EXIT
    git -C "$SOURCE_CHECKOUT" bundle create "$BUNDLE_DIR/revision.bundle" HEAD
    # Fixed remote paths, no source text or credentials in the command.
    # trap removes the temporary bundle on both success and failure.
    ssh "$TARGET" "set -eu; bundle=\$(mktemp $Q_CACHE_DIR/deploy-XXXXXX.bundle); trap 'rm -f \"\$bundle\"' EXIT; cat > \"\$bundle\"; test -z \"\$(git -C $Q_HOST_DIR status --porcelain)\"; git -C $Q_HOST_DIR fetch \"\$bundle\" HEAD; git -C $Q_HOST_DIR checkout --detach -q $DEPLOY_REV; cd $Q_HOST_DIR/doclayer; uv sync --frozen; test -x .venv/bin/brain-curator; test -x .venv/bin/brain-curator-worker; test -x .venv/bin/brain-curator-mcp" < "$BUNDLE_DIR/revision.bundle"
elif [ "$rc" -ne 0 ]; then
    die "remote preparation failed (rc=$rc)"
fi

log "installing systemd user unit (unit file comes from the deployed checkout)"
ssh "$TARGET" "mkdir -p $Q_UNIT_DIR"
ssh "$TARGET" "cp $Q_HOST_DIR/doclayer/deploy/worker/brain-curator.service $Q_UNIT_DIR/$UNIT_NAME"
ssh "$TARGET" "systemctl --user daemon-reload"

log "writing worker config if absent (existing config is never overwritten)"
ssh "$TARGET" "test -f $Q_CONFIG_DIR/config.yaml || cp $Q_HOST_DIR/doclayer/deploy/worker/config.example.yaml $Q_CONFIG_DIR/config.yaml"

log "checking credentials file (created by hand once; never written by deploys)"
ssh "$TARGET" "test -f $Q_CONFIG_DIR/credentials.env && echo '[deploy] credentials file present' || echo '[deploy] WARNING: credentials.env missing in the config directory; inference calls will fail until it exists (chmod 600)'"

log "enabling + restarting $UNIT_NAME"
ssh "$TARGET" "systemctl --user enable $UNIT_NAME"
ssh "$TARGET" "systemctl --user restart $UNIT_NAME"
sleep 2
ssh "$TARGET" "systemctl --user is-active $UNIT_NAME" || die "unit not active after restart"

log "deployment of $DEPLOY_REV complete; follow docs/curator.md (Deploy) for validation"
