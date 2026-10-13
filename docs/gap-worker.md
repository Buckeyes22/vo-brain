# Operating the gap worker

This is the operator reference for the knowledge-gap worker: its commands, units, settings and common problems. For what the worker does and why, read [knowledge-loop.md](knowledge-loop.md) first.

## Contents

- [Where it runs](#where-it-runs)
- [doclayer-gaps](#doclayer-gaps)
- [doclayer-gap-worker](#doclayer-gap-worker)
- [doclayer-gap-ops](#doclayer-gap-ops)
- [Units and timers](#units-and-timers)
- [Settings](#settings)
- [Turning dispatch on](#turning-dispatch-on)
- [Stopped curator jobs](#stopped-curator-jobs)
- [Closing a gap by hand](#closing-a-gap-by-hand)
- [Troubleshooting](#troubleshooting)

## Where it runs

One host owns the ledger (default `~/.local/state/doclayer/knowledge-gaps`) and runs the worker. Agent machines only capture and forward events. Which machine owns the ledger is set by two variables:

- `DOC_LAYER_GAP_OWNER_HOST`: the owner's ssh alias. Empty means this machine owns the ledger.
- `DOC_LAYER_GAP_OWNER`: `local` or `ssh`. It defaults to `local` when the owner host is empty or names this machine (`settings.is_host`), else `ssh`. An explicit value wins.

## doclayer-gaps

`doclayer-gaps` (`gaps_cli.py`) is the operator CLI. `--state-dir DIR` points it at a ledger directory directly; use it for diagnostics, not as a second owner.

| Command | What it does |
|---|---|
| `status` | Ledger summary as JSON: counts by status and need class, outstanding needs, spool size, whether dispatch is paused and whether the curator is ready |
| `list` | Every gap record as JSON |
| `inspect GAP` | One record and its event trail, including `private_evidence` events with plans, refusals and withheld outputs |
| `retry GAP` | Clear the backoff and requeue the gap. Refused while an attempt owns the gap |
| `suppress GAP [--reason TEXT]` | Stop automatic handling of a gap. Evidence is kept |
| `supersede GAP --by PRIMARY [--reason TEXT]` | Mark a duplicate as superseded by its primary gap |
| `force-refresh GAP` | Ask the gap's question again with `force=True` and print the answer |
| `discover GAP` | Run bounded discovery for one gap now |
| `dispatch-pause` | Stop new automatic submissions. Capture, answering and running jobs continue |
| `dispatch-resume` | Allow new submissions again |
| `verify GAP [--existing]` | Verify a published gap end to end. `--existing` verifies a hand-written page with no curator job |
| `digest` | Run one worker cycle and print the digest |
| `flush` | Deliver the local event outbox to the owning ledger |

On a machine whose owner is `ssh`, `status`, `list`, `inspect`, `retry`, `suppress`, `supersede`, `dispatch-pause` and `dispatch-resume` are forwarded to the owner over ssh. `discover`, `verify` and `digest` must run on the owner. `force-refresh` runs the resolver locally.

## doclayer-gap-worker

`doclayer-gap-worker` (`gap_worker.py`) runs the cycle.

```bash
doclayer-gap-worker --once                 # one cycle, print the digest, exit
doclayer-gap-worker --loop --interval 300  # run forever, one cycle per interval
```

The digest is a short summary, never the whole ledger. A real digest from a temporary ledger with one gap:

```text
knowledge gaps @ /tmp/capt/gaps: 1 record(s), 1 outstanding
  statuses: needs_local_evidence=1
  needs: infrastructure=1
  dispatch is dry-run: curator readiness not proven
  - gap-c7a42b7d8fb136914f5c: private need; direct authoring required
```

Other lines can appear: `⚠ capture degraded: spool over cap`, `⚠ N quarantined record(s)`, counts of needs parked at their allowance or after a model task failed, jobs holding their slot while publication retries, `dispatch paused (manual)`, and up to eight notes from the cycle.

## doclayer-gap-ops

`doclayer-gap-ops` (`gap_ops.py`) is not meant to be run by hand. It is the fixed remote command that `gap_client` runs over ssh. It reads one JSON object `{"operation": ..., "payload": {...}}` of at most 16,384 bytes from stdin and prints one JSON reply. Operations: `submit_event`, `summary`, `list`, `inspect`, `attach_source`, `pause`, `resume`, `retry`, `suppress`, `supersede`. Errors come back as `{"ok": false, "error": "..."}`.

```bash
echo '{"operation": "summary", "payload": {}}' | doclayer-gap-ops
```

## Units and timers

All units are systemd user units in `doclayer/deploy/`.

| File | Host | What it does |
|---|---|---|
| `worker/doclayer-gap-worker.service` | Worker host | Oneshot: `doclayer-gap-worker --once` with `DOC_LAYER_GAP_OWNER=local` and `BRAIN_CURATOR_LOCAL=1`; `TimeoutStartSec=8h` is a hang guard |
| `worker/doclayer-gap-worker.timer` | Worker host | `OnBootSec=2min`, then `OnUnitInactiveSec=5min`: the next cycle starts five minutes after the last one ends |
| `workstation/doclayer-gap-worker.service` | Agent machines | Oneshot: `doclayer-gaps flush` |
| `workstation/doclayer-gap-worker.timer` | Agent machines | `OnBootSec=2min`, then `OnUnitActiveSec=5min` |

`worker/deploy-gap-worker.sh HOST SOURCE` installs the worker units. It refuses a dirty source checkout, requires the worker's checkout to be at the same revision, checks that the brain pointer, the vo-rag API key file and the curator config exist, backs up existing units, and enables the timer. `deploy/workstation/README.md` has the workstation steps.

## Settings

From the curator config file (`~/.config/brain-curator/config.yaml`; see [configuration.md](configuration.md)):

| Key | Default | Meaning |
|---|---|---|
| `gap_worker.cycle_budget_s` | `null` | Seconds a cycle may spend on research. `null` means no limit |
| `gap_worker.max_active_jobs` | `1` | Gaps kept in the curator at once. Must be a whole number of at least 1 |
| `private_writer.hosts` | `[]` | ssh aliases the private writer may run commands on |
| `private_writer.host_notes` | `{}` | A role note per host, shown to the planner |
| `private_writer.max_commands` | `30` | Commands per plan |
| `private_writer.command_timeout_s` | `60` | Timeout per command |
| `private_writer.output_cap_bytes` | `131072` | Output kept per stream; the rest is cut with a note |
| `private_writer.max_job_calls` | `200` | Model-call ceiling for a private curator job |

From the environment:

| Variable | Default | Meaning |
|---|---|---|
| `DOC_LAYER_GAPS_DIR` | `~/.local/state/doclayer/knowledge-gaps` | Ledger root |
| `DOC_LAYER_GAP_OWNER`, `DOC_LAYER_GAP_OWNER_HOST`, `DOC_LAYER_GAP_OWNER_COMMAND` | see above; `doclayer-gap-ops` | Who owns the ledger and how to reach it |
| `DOC_LAYER_GAP_CAPTURE` | `1` | `0` pauses capture |
| `DOC_LAYER_GAP_MODEL_CALLS_JOB` | `50` | Model-call grant per automatic curator job |
| `DOC_LAYER_SEARXNG_URL`, `DOC_LAYER_SEARXNG_CERT_DIR` | empty; `~/.config/doclayer/searxng` | Optional search endpoint for discovery, with mutual TLS files `ca.crt`, `client.crt`, `client.key` |
| `DOC_LAYER_PRIVATE_READ_ROOTS` | `~/git/:~/src/` | Directories under which the private writer may `head` files |
| `DOC_LAYER_LOCAL_PROFILES` | `~/.config/doclayer/local-profiles.yaml` | Local evidence profiles |

## Turning dispatch on

Until the file `<ledger>/curator-ready` exists, every dispatch is a dry run: the exact payload is staged in `<ledger>/spool/` and the gap waits in `awaiting_curator_readiness`. To turn dispatch on, run one manual end-to-end check (submit a real gap, watch it publish, index and verify), then:

```bash
touch ~/.local/state/doclayer/knowledge-gaps/curator-ready
```

Creating the file is a decision, not a proof. No installer creates it. To stop new submissions without removing it, use `doclayer-gaps dispatch-pause`.

## Stopped curator jobs

A curator job that stops (`retryable`) keeps its dispatcher slot until the worker decides what to do (`gap_worker:_verify_published`):

- **Publication started** (a commit, push, pull request or merge exists): the same job is resumed every 5 minutes, doubling to 6 hours, with no limit on tries (`dispatcher.PUBLICATION_RESUME_BASE_S`, `PUBLICATION_RESUME_CAP_S`). It may get up to 1,800 more seconds of active time (`PUBLICATION_RESUME_ACTIVE_S`). It is never replaced by a new attempt.
- **Outage or crash before publication**: resumed at its exact checkpoint at most 3 times (`dispatcher.MAX_STOP_RESUMES`).
- **Model-call allowance used up** (`limit reached: model_calls`): the allowance is retired with an immutable receipt, the slot is freed and the gap becomes `failed` with `resource_exhausted`.
- **A model task stopped before publication** (truncated output or a reply still malformed after its retries, recorded as a typed stop): the allowance is retired the same way and the gap becomes `failed` with `model_response_unresolved`.
- **Any other stop, or a stop after the third resume**: the allowance is retired and the gap goes back to `retry_wait` (a private gap to `needs_local_evidence`) on its backoff.

The stopped job keeps its checkpoint and sources. To continue it by hand with more model calls:

```bash
brain-curator resume --job job-xxxx --operator-readmission --extend max_model_calls=50
```

## Closing a gap by hand

For a private need the writer cannot fill:

1. Gather the facts with read-only commands.
2. Edit the page in `wiki/` or `infra/` in a reviewed pull request and merge it.
3. Wait for the index to pick up the commit.
4. Run `doclayer-gaps verify GAP --existing`. It exits 0 only when the outcome is `resolved`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Digest says "dispatch is dry-run" | `<ledger>/curator-ready` is absent. See [Turning dispatch on](#turning-dispatch-on) |
| Digest says "dispatch paused (manual)" | Run `doclayer-gaps dispatch-resume` |
| Digest says "capture degraded" | The spool or outbox is over its cap. Run `doclayer-gaps flush` on agent machines and inspect `<ledger>/spool/` on the owner |
| `curator config unreadable` in the cycle | `gap_worker.max_active_jobs` or `limits.parallel_jobs` is not a whole number of at least 1, or the config file does not parse |
| `retry` or `suppress` says "active gap must recover through its existing attempt" | A lease, a submission or a curator job owns the gap. Wait for it, or cancel the job with `brain-curator cancel` |
| A gap sits in `needs_local_evidence` | `doclayer-gaps inspect GAP` shows the `private_evidence` events with refusals and withheld outputs; status `not-configured` means `private_writer.hosts` is empty. Add the host to `private_writer.hosts`, or close it by hand |
| A gap sits in `retrieval_defect` | A page answers the question but the resolver does not deliver it. Fix retrieval; another import will not help |
| A gap stays in `verifying` after its pages exist | The verdict model rejected a reply. It retries on a 5-minute backoff that doubles to 6 hours. Treat it as missing content only after repeated `partial` or `failed` outcomes that name a missing facet |
| Pages merged but the gap is in `awaiting_index` | The search index has not acknowledged the commit yet. Check the indexer's state file (`DOC_LAYER_BRAIN_SYNC_STATE`) |
| `knowledge-gap owner unavailable; event retained locally` | ssh to `DOC_LAYER_GAP_OWNER_HOST` failed. The event stays in the outbox until the next flush |
