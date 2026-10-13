---
schema_version: 1
privacy: internal
source_origin: operator
source_lineage:
  - seed:demo-vault
tags:
  - demo
updated: 2026-10-12
title: Search stack
---

# Search stack

How the demo vault answers a question from its pages.

## Components

- **Embeddings** — the embedding model server on `gpu1` (port 8081) turns page sections into vectors.
- **Index** — a vector index plus a keyword index over the same sections, rebuilt by `worker1` when the vault changes.
- **Inference** — the OpenAI-compatible endpoint on `app1` (port 8000) writes the final answer from the retrieved sections.

Host details are in [[infra/fleet-hosts]].

## Query flow

1. The question is embedded on `gpu1`.
2. The vector and keyword indexes each return candidate sections; the results are merged.
3. Sections whose page `privacy` is above the caller's clearance are dropped.
4. The remaining sections go to the inference endpoint on `app1`, which answers and cites the page and section it used.

## Keeping the index fresh

The curator and gap worker on `worker1` pull the Git forge remote on `svc1`. After a merge, `worker1` re-embeds only the changed sections. Backups of the index live on `nas1`.

## Related

- [[wiki/example-app-deployment]]
- [[sources/example-cli/readme]] — the client used to run queries from `ws1`.
