# Doclayer capture delivery on the workstation

The workstation timer forwards the durable event outbox to worker1. It does not
research sources, dispatch imports, or run semantic verification. Prompt hooks
only enqueue, so they never wait for SSH or inference. Explicit MCP tools try
delivery immediately and retain the same event if the reply is lost.

## Install

Install the package from the reviewed committed revision first. Then:

```bash
mkdir -p ~/.config/systemd/user
cp ~/git/brain/doclayer/deploy/workstation/doclayer-gap-worker.service ~/.config/systemd/user/
cp ~/git/brain/doclayer/deploy/workstation/doclayer-gap-worker.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now doclayer-gap-worker.timer
```

The outbox is `~/.local/state/doclayer/knowledge-gaps/outbox`, capped
at 8 MiB. `doclayer-gaps flush` retries exact event identities; a full outbox
creates a visible `capture-degraded` marker rather than silently dropping demand.
Operational CLI commands use the same owner as the MCP tools. `--state-dir` is
an explicit local diagnostic override, not a second background owner.

## One owner on worker1

The ledger, reservations, discovery, dispatch and verification live at
`~/.local/state/doclayer/knowledge-gaps` on worker1. Its five-minute
unit runs `~/git/brain-curator/doclayer/.venv/bin/doclayer-gap-worker`
with `DOC_LAYER_GAP_OWNER=local` and `BRAIN_CURATOR_LOCAL=1`. Install it using
`~/git/brain/doclayer/deploy/worker/deploy-gap-worker.sh` only after
that host has the identical clean curator revision, canonical-reference pointer,
existing vo-rag API key and preserved owner state.

During migration stop the old workstation timer, back up both ledgers and units,
copy the existing ledger/events/budgets intact to an empty owning state directory,
and preserve the old workstation bytes. Do not combine divergent owner budgets
or reset counters. Existing records retain their identities and publication
history; old collapsed questions cannot be reconstructed. New observations use
complete question/facet/applicability identity.

Readiness remains withheld until the curator repair and real end-to-end proof
pass. The flag is `~/.local/state/doclayer/knowledge-gaps/curator-ready`
on worker1. The installer never creates it. `doclayer-gaps dispatch-pause`
stops new submissions; active attempts continue terminal observation. Retry and
suppression refuse active or uncertain attempts so their slot cannot be stranded.

## Rollback

Pause new dispatch before changing code or units. Restore the backed-up units
and reviewed code; retain owner ledger, outbox, jobs and all budget files. A
running/uncertain submission must recover through its immutable payload and job,
never a newly minted attempt. Do not run workstation and worker1 research workers
at the same time, or restore an older state archive over newer progress.
