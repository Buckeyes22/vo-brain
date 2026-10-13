---
schema_version: 1
privacy: internal
source_origin: external
source_lineage:
  - seed:demo-vault
tags:
  - demo
updated: 2026-10-12
title: example-cli readme
url: https://example.org/example-cli/readme
fetched_at: '2026-10-12T00:00:00Z'
product: example-cli
---

# example-cli

`example-cli` is a fictional command-line client used in this demo vault to deploy and query the example app. It is documented here only so the vault has imported-source pages to cite.

## Install

Download the release archive for your platform, unpack it, and put the `example-cli` binary on your `PATH`. Check it with `example-cli --version`.

## Commands

- `example-cli deploy --host <host> --ref <tag>` — deploy a release to a host. See [[wiki/example-app-deployment]].
- `example-cli query "<question>"` — ask the search stack a question and print the cited answer. See [[wiki/search-stack]].
- `example-cli config get|set <key> [value]` — read or change settings; see [[sources/example-cli/docs-configuration]].
- `example-cli status` — show the endpoint, the host and the last deploy.

## Exit codes

`0` success, `1` a command failed, `2` bad arguments or configuration.
