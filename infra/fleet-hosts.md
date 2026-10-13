---
schema_version: 1
privacy: internal
source_origin: operator
source_lineage:
  - seed:demo-vault
tags:
  - demo
updated: 2026-10-12
title: Fleet hosts
---

# Fleet hosts

This is the fictional fleet of record for the demo vault. Every address is in the RFC 5737 documentation range (192.0.2.0/24), so none of it routes anywhere. See [[infra/index]] for the infra map and [[wiki/example-app-deployment]] and [[wiki/search-stack]] for how the hosts fit together.

## Hosts and roles

- **ws1** — `192.0.2.10` developer workstation. Ubuntu 24.04 LTS. Runs the editor, a local clone of this vault and the `example-cli` client ([[sources/example-cli/readme]]).
- **app1** — `192.0.2.20` application and GPU inference VM. Debian 12. Serves an OpenAI-compatible inference endpoint on port 8000 and a Postgres 16 database on port 5432 for the example app ([[wiki/example-app-deployment]]).
- **gpu1** — `192.0.2.30` embedding box. Ubuntu 24.04 LTS. Runs the embedding model server on port 8081 that feeds the vector index ([[wiki/search-stack]]).
- **svc1** — `192.0.2.40` services host. Debian 12. Runs the Git forge on port 3000 (the remote of record for this vault) and a Caddy reverse proxy on ports 80 and 443.
- **worker1** — `192.0.2.50` background worker. Ubuntu 24.04 LTS. Runs the doclayer curator and the gap worker as systemd services; both read the vault from a clone of the forge remote.
- **nas1** — `192.0.2.60` network storage. Debian 12. Holds nightly backups of the Postgres database and the vault, shared over NFS.
