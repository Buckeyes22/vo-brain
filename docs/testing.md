# Testing

The test suite is the main evidence that the system does what these docs say. It runs offline: no ssh, no model endpoint, no forge and no search service are needed.

## Contents

- [Running the suite](#running-the-suite)
- [Layout](#layout)
- [Isolation](#isolation)
- [Fakes](#fakes)
- [Fixtures](#fixtures)
- [The routing evaluation set](#the-routing-evaluation-set)
- [Adding a regression test](#adding-a-regression-test)

## Running the suite

```bash
cd doclayer
uv run pytest -q -p no:cacheprovider 2>&1 | tail -2
```

When these docs were written, that command reported `4244 passed in 251.46s (0:04:11)`. Async tests run under `pytest-asyncio` with `asyncio_mode = "auto"` (`pyproject.toml`).

Run a part of it:

```bash
uv run pytest -q tests/curator                    # curator only
uv run pytest -q tests/test_gap_worker.py         # one file
uv run pytest -q -k "backoff"                     # by name
```

## Layout

| Directory | Files | What it covers |
|---|---|---|
| `doclayer/tests/` | 97 test files | Resolver, routing, coverage, applicability, the gap ledger, worker, dispatcher, discovery, verification, private writer, fetcher, vault sync, MCP server |
| `doclayer/tests/curator/` | 127 test files | Intake, extraction, placement, editing, review, preservation, validation, reconciliation, publication, job store, worker, transport, model client, config |
| `doclayer/tests/fixtures/` | | The routing evaluation set, long wiki pages, example local evidence profiles |

Test files are named for the behaviour they pin, for example `test_gap_no_daily_quotas.py`, `test_single_pass_editing.py` or `test_publish_existing_merge_recovery.py`. [curator-quality.md](curator-quality.md) maps each publishing guarantee to its tests.

## Isolation

`doclayer/tests/conftest.py` makes sure no test touches real user state:

- Before the package is imported, it points `DOC_LAYER_CATALOG_CACHE_DIR`, `DOC_LAYER_NEGATIVE_CACHE` and `DOC_LAYER_GAPS_DIR` at a temporary directory and sets `DOC_LAYER_GAP_OWNER=local`, so even a test that builds its own `Settings()` stays isolated.
- An autouse fixture gives every test its own negative cache, gap ledger and an empty local profiles path.
- Host and subject recognition is stubbed to empty (`applicability.known_identities`), so the real `~/.ssh/config` and vault are never read. The `identities` fixture installs specific hosts and subjects for a test that needs them.
- The dispatcher's live transport is replaced with one that fails the test, so a test that forgets to supply a fake curator cannot submit a real job.
- The private writer is turned off by default; the `real_private_writer` fixture turns it back on for tests that pass a fake command runner.
- The `local_profiles` fixture installs the fictional-fleet profiles from `tests/fixtures/local-profiles.yaml`.

## Fakes

`doclayer/tests/curator/conftest.py` provides the curator's test doubles:

| Fake | What it is |
|---|---|
| `origin`, `checkout` fixtures | A real bare Git repository with a seeded vault, and a clone of it |
| `cfg`, `store` fixtures | A curator config and a job store under the test's temporary directory |
| `HttpFixture` | Scripted HTTP over `httpx.MockTransport`: a URL maps to a status and body, bytes, a redirect, or an exception |
| `ScriptedQwen` | A model endpoint that speaks the real OpenAI-style HTTP contract over `httpx.MockTransport`. It answers each task type (keyed on the system prompt) with scripted JSON, records every call, and accepts per-test overrides and injected failures |
| `FakeForgejo` | An in-memory Forgejo backed by the bare origin: merging really advances `main` by fast-forward. It can fail the next N pull-request creations or merges, or merge and then lose the reply |
| `make_pdf` | Builds a small text PDF for intake tests |

Because `ScriptedQwen` and `FakeForgejo` sit at the HTTP and API boundaries, the code under test is the production code: retries, idempotency, recovery from lost replies and reconciliation all run for real.

## Fixtures

- `tests/fixtures/routing-evaluation.yaml`: the routing evaluation set (below).
- `tests/fixtures/long_pages/`: six long wiki-style pages used by `test_bounded_editing.py` and `test_reconciliation.py` to prove that long pages survive bounded edits and three-way merges at the same input and output limits.
- `tests/fixtures/local-profiles.yaml`: local evidence profiles for the fictional fleet.

## The routing evaluation set

`tests/fixtures/routing-evaluation.yaml` is a versioned set of 19 prompts, each with the route and nudge decision it must produce. It covers local, public and mixed questions, explicit lookups, unknown libraries, editing prompts, code questions, follow-ups, multi-subject questions and ambiguous aliases, plus prompts that once triggered a lookup they should not have. `tests/test_routing_evaluation.py` checks:

- `test_expected_route`: every case gets its expected intent and nudge decision.
- `test_hook_warm_path_under_budget`: classifying the whole set five times averages under 5 ms per prompt.
- `test_route_reduction_against_standing_instruction_baseline`: the router triggers fewer lookups than a rule of "look up every subject mention".

Add a case here whenever a prompt routes wrongly.

## Adding a regression test

1. Reproduce the bug with the smallest input: a prompt, a gap record, a scripted model reply, a page body.
2. Put the test in the file that owns the behaviour, or a new file named for it. Use the fixtures above rather than real paths, hosts or network calls.
3. For the curator, script the model's replies with `qwen.overrides[...]` and inject failures with `qwen.failures`. For publication, use `forgejo.fail_create`, `fail_merge` or `fail_after_merge`.
4. For the gap loop, build a `GapLedger` on `tmp_path` and pass a fake transport to `GapDispatcher(ledger, transport=...)`.
5. Watch the test fail before the fix and pass after it, then run the whole suite.
