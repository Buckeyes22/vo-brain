---
schema_version: 1
privacy: internal
source_origin: external
source_lineage:
  - seed:demo-vault
tags:
  - demo
updated: 2026-10-12
title: example-cli configuration
url: https://example.org/example-cli/docs-configuration
fetched_at: '2026-10-12T00:00:00Z'
product: example-cli
---

# example-cli configuration

`example-cli` reads a TOML file, then environment variables, then command-line flags; later sources override earlier ones. Back to [[sources/example-cli/readme]].

## File location

The default file is `~/.config/example-cli/config.toml`. Set `EXAMPLE_CLI_CONFIG` or pass `--config <path>` to use another.

## Keys

| Key | Default | Meaning |
| --- | --- | --- |
| `endpoint` | `http://192.0.2.20:8000/v1` | Base URL of the OpenAI-compatible inference endpoint. |
| `default_host` | `app1` | Host used when `--host` is omitted. |
| `timeout_seconds` | `30` | Per-request timeout. |
| `output` | `text` | `text` or `json`. |

## Environment variables

Each key maps to `EXAMPLE_CLI_<KEY>` in upper case, for example `EXAMPLE_CLI_TIMEOUT_SECONDS=60`. The API token is read only from `EXAMPLE_CLI_TOKEN` and is never written to the file.

## Example

```toml
endpoint = "http://192.0.2.20:8000/v1"
default_host = "app1"
output = "json"
```

See [[wiki/example-app-deployment]] for how these settings are used when deploying.
