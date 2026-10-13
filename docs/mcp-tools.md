# MCP tools

Two MCP servers, both over stdio:

- `doclayer-mcp` (`mcp_server.py`): four tools for asking the brain and recording needs.
- `brain-curator-mcp` (`curator/mcp_server.py`): eight tools for curator jobs. It is a separate server so the answering tools never depend on the curator.

The example outputs below are real, captured against the demo vault and temporary state directories while writing these docs. `<commit>` stands for the commit hash the real output shows.

## Contents

- [doclayer-mcp](#doclayer-mcp)
  - [resolve](#resolve)
  - [route](#route)
  - [record_gap](#record_gap)
  - [attach_source](#attach_source)
- [brain-curator-mcp](#brain-curator-mcp)
  - [submit_import](#submit_import)
  - [job_status](#job_status)
  - [job_result](#job_result)
  - [job_inspect](#job_inspect)
  - [job_resume](#job_resume)
  - [job_continue](#job_continue)
  - [job_cancel](#job_cancel)
  - [job_correct](#job_correct)
- [Registering the servers](#registering-the-servers)

## doclayer-mcp

### resolve

Ask one question; get one cited answer.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `question` | string | required | The question |
| `product` | string | `null` | Narrows the subject to a catalog slug |
| `live` | bool | `null` | `true` forces live checks; `null` runs them when the question is about running state |
| `force` | bool | `null` | `true` bypasses the negative cache |

**Returns** a Markdown string: the question as a heading, `**Flags**`, `## Answer` with facts and sources, any unknowns, `## Search`, `## Context7`, `## Live checks` when run, and `## Dispositions`. [resolver.md](resolver.md#the-answer-format) shows a full annotated answer.

```json
{"name": "resolve", "arguments": {"question": "Which port does Postgres listen on for the example app on app1?"}}
```

### route

The routing decision for a question, without calling any source.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `question` | string | required | The question or prompt |
| `explicit` | bool | `null` | `true` routes it as an explicit brain lookup |

**Returns** plain text lines: `intent`, `subjects`, `sources`, `coverage` (status and revision), `gap capture eligible`, `reason`, and when present `uncovered subject` and `cache`.

```json
{"name": "route", "arguments": {"question": "Refactor this function to use a dataclass"}}
```

```text
intent: code_task
subjects: none
sources: none
coverage: ok @ <commit>
gap capture eligible: False
reason: repository code or task work; no doclayer lookup
```

### record_gap

Record a documentation need the agent could not meet, or one it answered with outside tools.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `question` | string | required | The need, as a question (at most 2,000 characters) |
| `subject` | string | `null` | The subject; matched to a catalog slug when possible |
| `need` | string | `missing_coverage` | One of `missing_coverage`, `enrichment`, `retrieval_defect`, `infrastructure`, `needs_local_evidence`, `ambiguous` |
| `detail` | string | `null` | Short technical detail. No transcripts, no secrets |
| `facets` | list of strings | `null` | What exactly is asked, for example `["flags"]` |

**Returns** one line: `recorded: <gap_id> (<need>, <status>, seen Nx)` for a new gap, `existing gap updated: ...` for a repeat, `gap capture is disabled; the need was not recorded`, or `failed to record the gap: <error>`. It never raises.

```json
{"name": "record_gap", "arguments": {"question": "Which flags does example-cli deploy accept?", "subject": "example-cli", "facets": ["flags"]}}
```

```text
recorded: gap-12472eff2f780fbfd35c (missing_coverage, candidate, seen 1x)
```

The same call a second time returned `existing gap updated: gap-12472eff2f780fbfd35c (missing_coverage, candidate, seen 2x)`.

### attach_source

Attach documentation found during the task, so the gap worker can use it instead of searching again.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `subject` | string or null | required | The subject |
| `url` | string | required | An `http` or `https` documentation URL |
| `note` | string | `null` | What the source covers (first 500 characters kept) |
| `question` | string | `null` | The need it answers; defaults to `documentation for <subject>` |
| `gap_id` | string | `null` | Attach to this existing gap instead of recording an `enrichment` need |
| `authority` | string | `unverified` | `official` only after checking the project owns the source |

**Returns** `source attached to <gap_id> (<status>, seen Nx)`, or `source attached to <gap_id>` with `gap_id`, or an error line. Attached sources keep the order they were attached in, ahead of other candidates.

```json
{"name": "attach_source", "arguments": {"subject": "example-cli", "url": "https://example.org/example-cli/deploy", "note": "deploy flags reference", "question": "Which flags does example-cli deploy accept?"}}
```

```text
source attached to gap-c85f8d0417cebc041c95 (candidate, seen 1x)
```

## brain-curator-mcp

Every tool returns a JSON string. A failure is also JSON, `{"ok": false, "error": "..."}`, never a protocol error. The tools call the same ssh transport as the `brain-curator` CLI ([curator.md](curator.md#commands)). Source bodies never enter the agent's context; long digests, drafts and diffs are cut at 20,000 characters.

### submit_import

Submit material for curation. Returns as soon as the job is stored.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `intent` | string | required | What to build or update |
| `urls` | list | `null` | Seed URLs |
| `text`, `text_label` | string | `null`, `pasted-text` | Pasted text and its label |
| `files` | list | `null` | Local `.md`, `.txt` or `.pdf` paths to transfer |
| `directory` | string | `null` | A local directory of supported files |
| `manifest` | string | `null` | A local YAML batch manifest |
| `exclusions` | list | `null` | Material to leave out |
| `idempotency_key` | string | `null` | Retries with the same key never create a second job; derived from content when absent |
| `source_policy` | object | `null` | Include and exclude rules for sources |
| `mode` | string | `null` | `single-pass` or `multi-stage`; the config default when absent |
| `targets` | list | `null` | `wiki/` or `infra/` pages to update or create (single-pass only) |

**Returns** `{"ok": true, "job_id": ..., "created": true|false, "status": ...}`. Real reply from the equivalent CLI call against a temporary job store:

```json
{"ok": true, "job_id": "job-868cafc682", "created": true, "status": "queued"}
```

### job_status

| Argument | Type | Meaning |
|---|---|---|
| `job_id` | string | The job |

**Returns** `{"ok": true, "job": {...}}` with `status`, `stage`, `mode`, `private`, `single_pass_calls`, `intent`, `coverage`, `coverage_gaps`, `sources` (counts by status and review), `changes` (per page), `usage` (model calls, tokens, bytes, active and idle seconds, calls by stage), `limits`, `publication` (branch, pull request, merged, merge commit), `error` and timestamps. Excerpt of a real reply for a job that had not started (some keys left out):

```json
{"ok": true, "job": {"job_id": "job-868cafc682", "status": "queued", "stage": "intake", "mode": "single-pass",
 "coverage": "incomplete", "coverage_gaps": ["notes.md [src-287d29f437]: source pending; not reviewed"],
 "sources": {"total": 1, "fetched": 0, "pending": 1},
 "usage": {"model_calls": 0, "active_seconds": 0.0, "calls_by_stage": {}, "idle_wait_seconds": 0.0},
 "publication": {"branch": null, "pr_number": null, "merged": false, "merge_commit": null}, "error": null}}
```

### job_result

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `job_id` | string | required | The job |
| `include_digest` | bool | `true` | Include the completion digest |

**Returns** `{"ok": true, "job": {...same as job_status...}, "digest": "..."}`. The digest lists created and updated pages, sources, coverage gaps, conflicts, usage, and the commit and pull request.

### job_inspect

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `job_id` | string | required | The job |
| `what` | string | `diff` | `diff`, `draft` or `evidence` |
| `path` | string | `""` | The page path, for `diff` and `draft` |

**Returns** `{"ok": true, "what": "diff", "path": ..., "diff": ...}`, `{"ok": true, "what": "draft", "path": ..., "content": ...}`, or `{"ok": true, "what": "evidence", "records": [...], "total": N}` (at most 100 records).

### job_resume

Resume a stopped or `retryable` job at its checkpoint, keeping its counters.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `job_id` | string | required | The job |
| `extend` | object | `null` | Limits to raise, for example `{"max_model_calls": 50}` |
| `operator_readmission` | bool | `false` | Needed to continue a job whose automatic allowance was retired; requires a positive `max_model_calls` extension |

**Returns** `{"ok": true, "job_id": ..., "status": "queued", "stage": ...}`, or the current status with `"note": "already finished"`.

```json
{"name": "job_resume", "arguments": {"job_id": "job-868cafc682", "extend": {"max_model_calls": 50}, "operator_readmission": true}}
```

### job_continue

Create a new linked job that reuses the parent's evidence and cache. The parent's publication is never replayed.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `job_id` | string | required | The parent job |
| `source_ids` | list | `null` | Parent sources whose omitted sections to review |
| `review_omitted` | bool | `true` | Review sections the parent omitted |
| `discover` | bool | `true` | Allow link discovery |
| `source_policy` | object | `null` | Replace the source policy |
| `intent` | string | `""` | Replace the intent |
| `urls` | list | `null` | More primary URLs (needs `discover`) |
| `refresh_source_ids` | list | `null` | Fetched URL sources to fetch again |
| `mode` | string | parent's mode | `single-pass` or `multi-stage` |
| `targets` | list | parent's targets | Pages to update (single-pass) |

**Returns** `{"ok": true, "job_id": <new job>, "created": true|false, "continuation_of": <parent>, ...}`.

### job_cancel

| Argument | Type | Meaning |
|---|---|---|
| `job_id` | string | The job |

**Returns** `{"ok": true, "job_id": ..., "status": "cancelled"}` for a queued or `retryable` job, or the current status with a note that the worker will stop at the next stage boundary.

### job_correct

Record an operator correction for a page and queue a job that applies it.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `page` | string | required | The page path, for example `wiki/example-app-deployment.md` |
| `instruction` | string | required | What to change |
| `claim` | string | `""` | The wrong claim's text; later imports may not restore it |

**Returns** `{"ok": true, "correction": {...}, "job_id": ..., "created": true|false}`.

```json
{"name": "job_correct", "arguments": {"page": "wiki/example-app-deployment.md", "claim": "Postgres listens on port 5433", "instruction": "Postgres listens on port 5432 on app1."}}
```

## Registering the servers

For Claude Code, in `.mcp.json` or the user's MCP config:

```json
{
  "mcpServers": {
    "doclayer": {
      "command": "/path/to/vo-brain/doclayer/.venv/bin/doclayer-mcp",
      "env": { "DOC_LAYER_BRAIN_REPO": "/path/to/vo-brain" }
    },
    "brain-curator": {
      "command": "/path/to/vo-brain/doclayer/.venv/bin/brain-curator-mcp"
    }
  }
}
```

`brain-curator-mcp` reads the curator config (`BRAIN_CURATOR_CONFIG`, default `~/.config/brain-curator/config.yaml`) once, on its first tool call, to find `ssh.host`.
