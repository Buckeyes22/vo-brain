---
schema_version: 1
privacy: internal
source_origin: operator
source_lineage:
  - seed:demo-vault
tags:
  - staging
  - contract
updated: 2026-10-01
concept_id: c-27182818
address: c-000012
title: README
---

# inbox — agent staging

Agents write **here**, never to `wiki/` or `infra/` directly. This preserves the operator's
curation control and gives canonical pages exactly one promotion path.

## The flow

1. Create a page here (copy [[templates/page]]; set a truthful `privacy:`).
2. Commit on a short-lived branch and open a PR against `main`.
3. The operator reviews; **merge = promotion** to canonical (`wiki/` or `infra/`). Checks run on the PR.

Pages left in `inbox/` are drafts, not canonical knowledge — readers should treat them as
unreviewed.

**Curator exception:** the brain curator does not use this staging flow for
its imports. The operator can authorize it to publish validated knowledge to `wiki/`/`infra/`
automatically (intake → model curation → automated checks → unattended landing), with
optional retrospective review via its job digests and a `correct` operation. See the root
[[CLAUDE]] and `docs/curator.md`. Everything else submitted by agents still
follows the flow above.
