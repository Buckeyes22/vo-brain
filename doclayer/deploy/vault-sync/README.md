---
schema_version: 1
privacy: internal
source_origin: agent
source_lineage: [agent:systemic-vault-propagation]
tags: [brain, propagation, operations]
updated: 2026-10-02
title: Safe consuming-vault propagation
---

# Supported ownership

The `brain-vault-sync` timer owns reconciliation of **explicitly registered consuming
vaults**, not development, publisher, or indexing checkouts. A consumer's vault
clone is the verified case. The fetcher/indexer clones on svc1 and the curator
clone on worker1 retain their existing owners. Never discover clones and update all of them.
A new consumer opts in with its absolute repo/state paths and exact canonical remote
in `~/.config/brain-vault-sync/consumers.json`. Each consumer is independent:
one failure does not prevent attempts on the others. The timer reconciles every minute
with at most ten seconds of jitter, including after missed ticks; it does not depend
on a particular publishing callback. No shared consumer service is restarted.

Deploy the committed package into `~/git/brain-curator` using the curator's
normal deploy script, then run `doclayer/deploy/vault-sync/install-worker.sh <ssh-host>` from the
workstation. The installer backs up existing config/units, preserves existing consumer
registration, enables the timer, and runs the first reconciliation. It uses the vault's
existing Git authentication, with interactive credential prompts disabled.

Each pass takes a lock in Git's common directory, verifies branch/registered origin,
fetches canonical main, and requires local HEAD to be an ancestor. Dirty paths that
overlap incoming changes block the update, with paths recorded in status. Divergent
or locally ahead history also blocks it. Unrelated dirty logs/runtime files survive.
Git's index lock and `--no-overwrite-ignore` guard external Git operations and ignored
runtime collisions. Existing consumer hooks and Git controls remain enabled. Local
configuration is not changed; a hook failure is reported like other merge failures.

Before a changing fast-forward, state-adjacent private backups preserve previous HEAD,
index and exact dirty/untracked bytes (including deleted files and symlink identity).
Original modes and SHA-256 checksums are in the manifest; copied backup files are mode
0600. Post-update byte/mode drift is reported, never silently repaired or rolled back:
a concurrently appended log remains appended. A subsequent pass may report current
revision while the prior error remains visible in the journal. No automatic stash,
reset, log commit or conflict resolution happens. Backups currently retain indefinitely;
inspect and prune them deliberately after verifying they are no longer needed.

# Inspection and recovery

Inspect `systemctl --user status brain-vault-sync.timer brain-vault-sync.service`,
`journalctl --user -u brain-vault-sync.service`, or run
`~/git/brain-curator/doclayer/.venv/bin/brain-vault-sync --config ~/.config/brain-vault-sync/consumers.json --status`.
State exposes local/canonical revisions, commit lag, error, last successful check,
preserved paths and backup location. On network failure canonical revision/lag are
unknown for that attempt; old success time remains. Failures return nonzero and retry
on the next timer tick. For local conflicts, preserve edits and resolve the listed
paths manually; for divergent history, reconcile through ordinary reviewed Git work.
Do not reset/stash user work to make status green.

To stop automation: `systemctl --user disable --now brain-vault-sync.timer`; wait for
an already-running oneshot to finish before any manual Git operation. Units/config
can be restored from the installer's private deployment backup and daemon-reloaded.
To reverse a vault revision, stop automation and inspect current edits first. The
previous commit and dirty-file/index manifest are recorded in each update backup;
restore only through a deliberate operator-reviewed Git operation. Do not copy an old
log over newer appends. Canonical commits remain history; reverting published knowledge
uses the repository's normal publication workflow rather than resetting consumers.

The workstation doclayer also consumes canonical knowledge. Its developer checkout at
`~/git/brain` must not be fast-forwarded by a background consumer service.
The dedicated reference installer instead clones canonical `main` into
`~/.local/share/doclayer/brain-reference`, adds only that clone to
`~/.config/brain-vault-sync/consumers.json`, and installs the same conservative
user timer with executable `~/git/brain/doclayer/.venv/bin/brain-vault-sync`.
After installing the committed package, run:

```bash
~/git/brain/doclayer/.venv/bin/python ~/git/brain/doclayer/deploy/vault-sync/install-workstation.py
systemctl --user status brain-vault-sync.timer
~/git/brain/doclayer/.venv/bin/brain-vault-sync --config ~/.config/brain-vault-sync/consumers.json --status
```

The installer uses normal Git authentication and rejects unrelated existing directories,
origins, branches, or conflicting registrations. It preserves other consumer registrations;
initial synchronization tests the new reference independently. Existing pointer, registry,
units (with their modes/timestamps), and private JSON status are copied beneath
`~/.local/state/brain-vault-sync/deployment-backups` before replacements.
The installer prints the particular backup directory. A synchronization failure leaves the
previous pointer intact and preserves operator edits; inspect the reference status for
conflicts or divergence instead of resetting its checkout. Re-running is idempotent.

Doclayer resolves `~/.config/doclayer/brain-repo` dynamically on every request,
including requests in an existing MCP process running the updated package. The file contains
one absolute repository path. `DOC_LAYER_BRAIN_REPO` takes precedence. An absent pointer retains
the existing developer-checkout default for compatibility; a present invalid or unavailable
pointer produces an actionable source flag and disables Git facts while API/documentation
sources remain available. A process still running old Python modules must be restarted once
when installing this package change. Later pointer changes do not require another restart.
Reference facts come from a captured committed revision, rather than mutable working-tree
bytes, so a timer update cannot cause an inaccurate `path@commit` citation.

For rollback, stop the user timer, restore the pointer/registry/service/timer from the printed
backup directory with `cp -p`, run `systemctl --user daemon-reload`, and restore the prior timer
activation state. If there was no prior pointer, remove only
`~/.config/doclayer/brain-repo` to restore the compatibility default. Keep the dedicated
clone, status and backups for inspection; rollback never resets a developer or consumer checkout.

Existing Git `url.*.insteadOf` authentication routes are honored. The registered
remote must match the repository's configured origin, and its effective route
must retain the canonical host and repository path. HTTPS-to-SSH routes may use
the SSH service's port (including a nonstandard port); redirects to another host,
repository, insecure HTTP or local files are refused. Neither installer nor
reconciler changes Git authentication or rewrite configuration.


The workstation installer renders `PrivateTmp=false` for its reference service.
Live read-only probes showed the established Git/SSH authentication route fails
under a private temporary-file namespace and succeeds with the normal namespace,
while `NoNewPrivileges=true` remains compatible. The service retains its private
umask and other existing restrictions; the worker's working unit keeps `PrivateTmp=true`.
No SSH agent environment, keys, helpers or credential configuration are replaced.
Validate the installed service through a timer-triggered run and a later periodic
run: initial installer synchronization alone is insufficient evidence of unattended
access. Check `last_success_at` advances and that `status` remains `current` in
`~/.local/state/brain-vault-sync/doclayer-reference.json`.
