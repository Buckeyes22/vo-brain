---
schema_version: 1
privacy: internal
source_origin: operator
source_lineage:
  - seed:demo-vault
tags:
  - demo
updated: 2026-10-12
title: Example app deployment
---

# Example app deployment

The example app is a small web service deployed to `app1` (see [[infra/fleet-hosts]]). This page records how it runs and how to roll it out.

## Services

The app runs as a container next to two services on the same host. The inference endpoint is OpenAI-compatible and listens on port 8000. Postgres 16 listens on port 5432 and is reachable only from the LAN. `svc1` terminates TLS in its Caddy reverse proxy and forwards requests to the app.

## Configuration

The app reads its settings from `/etc/example-app/config.toml` on `app1`. The same keys can be set from `ws1` with `example-cli`, as described in [[sources/example-cli/docs-configuration]]. Secrets are never stored in the vault; the service reads them from a root-only environment file.

## Rollout

1. Merge the change on the Git forge on `svc1`.
2. From `ws1`, run `example-cli deploy --host app1 --ref main`.
3. The CLI pulls the image, runs the database migrations, then restarts the container.
4. Check the health endpoint on port 8080 and confirm the migration version matches the release notes.

## Rollback

Re-run the deploy with the previous tag, for example `example-cli deploy --host app1 --ref v1.4.2`. Migrations are forward-only, so a rollback that crosses a migration restores the nightly Postgres backup from `nas1` first.

## Related

- [[wiki/search-stack]] — the retrieval side that shares the `app1` inference endpoint.
- [[infra/index]]
