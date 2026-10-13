# Development

How to set up the package, run and extend the tests, and add a doc-fetch recipe or a local evidence profile.

## Contents

- [Setup](#setup)
- [Running things](#running-things)
- [Tests](#tests)
- [Lint and format](#lint-and-format)
- [Adding a fetch recipe](#adding-a-fetch-recipe)
- [Adding a local evidence profile](#adding-a-local-evidence-profile)
- [Adding a subject to the router](#adding-a-subject-to-the-router)
- [Changing behaviour](#changing-behaviour)

## Setup

You need Python 3.12 or later and [uv](https://docs.astral.sh/uv/). `doclayer/.python-version` pins 3.12, the version CI uses, so `uv run` builds the same interpreter locally.

```bash
git clone https://github.com/Buckeyes22/vo-brain.git
cd vo-brain/doclayer
uv sync                 # creates .venv with the package and the dev group (pytest, pytest-asyncio, ruff)
```

The package's dependencies are `httpx`, `beautifulsoup4`, `markdownify`, `pyyaml`, `mcp` and `pypdf` (`pyproject.toml`). It builds with `hatchling`.

## Running things

Always go through `uv run` from `doclayer/`, or the package's `.venv/bin/`. Do not call a bare `python`.

```bash
uv run doclayer-mcp                                  # the MCP server (stdio)
DOC_LAYER_BRAIN_REPO=.. uv run doclayer-mcp          # against the demo vault in this repository
uv run brain-curator --local list                    # the curator CLI against a local job store
uv run python -I -m doclayer.livechecks              # run the live checks
uv run python -I -c "import doclayer; print(doclayer.__doc__)"
```

The ten console scripts, from `[project.scripts]` in `pyproject.toml`:

| Script | Entry point | Docs |
|---|---|---|
| `doclayer-mcp` | `doclayer.mcp_server:main` | [mcp-tools.md](mcp-tools.md) |
| `doclayer-nudge` | `doclayer.nudge:main` | [agent-integration.md](agent-integration.md) |
| `doclayer-gaps` | `doclayer.gaps_cli:main` | [gap-worker.md](gap-worker.md#doclayer-gaps) |
| `doclayer-gap-worker` | `doclayer.gap_worker:main` | [gap-worker.md](gap-worker.md#doclayer-gap-worker) |
| `doclayer-gap-ops` | `doclayer.gap_ops:main` | [gap-worker.md](gap-worker.md#doclayer-gap-ops) |
| `doclayer-fetch` | `doclayer.fetch:main` | [doc-fetcher.md](doc-fetcher.md) |
| `brain-curator` | `doclayer.curator.cli:main` | [curator.md](curator.md#commands) |
| `brain-curator-worker` | `doclayer.curator.worker:main` | [curator.md](curator.md#commands) |
| `brain-curator-mcp` | `doclayer.curator.mcp_server:main` | [mcp-tools.md](mcp-tools.md#brain-curator-mcp) |
| `brain-vault-sync` | `doclayer.vault_sync:main` | [vault-sync.md](vault-sync.md) |

Two modules also run with `python -m`: `doclayer.livechecks` and `doclayer.gap_migration` (offline ledger moves: `--root DIR` alone prints an inventory; `--root DIR --journal DIR --apply --owner HOST:PATH` retires and exports; `--root DIR --journal DIR --apply --install-archive FILE` installs an export).

## Tests

```bash
uv run pytest -q -p no:cacheprovider 2>&1 | tail -2    # everything: 4244 passed in 251.46s when these docs were written
uv run pytest -q tests/curator 2>&1 | tail -2          # the curator
uv run pytest -q tests/test_routing_evaluation.py      # one file
```

- `tests/` holds 97 test files for the answering side, the gap loop, the fetcher and vault sync.
- `tests/curator/` holds 127 test files for the curator.
- `tests/fixtures/` holds the routing evaluation set, long pages and example profiles.
- `tests/conftest.py` isolates every test from real state: temporary caches and ledgers, no real ssh config, no live curator transport, private writer off unless a test opts in.
- `tests/curator/conftest.py` provides a real bare Git origin, a scripted model (`ScriptedQwen`), a fake forge (`FakeForgejo`) and scripted HTTP (`HttpFixture`).

[testing.md](testing.md) covers the fakes and how to write a regression test.

## Lint and format

CI runs `uv run ruff check .` and `uv run ruff format --check .` from `doclayer/`, and both must pass. The configuration is in `pyproject.toml`: `[tool.ruff]` sets line length 120 and target `py312`, `[tool.ruff.lint]` selects the rules E4, E7, E9, F and I, and `[tool.ruff.lint.per-file-ignores]` lists the few test files that re-declare shared fixtures. Run both commands before a pull request; `uv run ruff format .` applies the formatting. Match the surrounding style: type hints, `from __future__ import annotations`, docstrings that state the contract, argument arrays for every subprocess.

## Adding a fetch recipe

1. Create `doclayer/recipes/<product>.yaml`. The file must sit in `doclayer/recipes/` of the vault, because the fetcher finds the vault root from that path.

   ```yaml
   product: example-cli
   title: example-cli
   docs:
     site: example-cli documentation
     base_url: https://example.org/example-cli/
     version: current
   fetch_interval_hours: 168
   pages:
   - slug: readme
     url: https://example.org/example-cli/readme
   - slug: docs-configuration
     url: https://example.org/example-cli/docs-configuration
   ```

2. Run it: `uv run doclayer-fetch recipes/example-cli.yaml`. Each page prints `HTTP <status> ok` or a `junk:` reason. Copies land in `sources/example-cli/`.
3. Read the copies. If a page's main content was not found, `convert.py` may need a content selector for that site.
4. Optionally add the product to `doclayer/routing-policy.yaml` with aliases and `public_docs.base_url`.
5. Commit the recipe and copies on a branch and open a pull request, or run with `--land` to have the fetcher open it ([doc-fetcher.md](doc-fetcher.md#landing-as-a-pull-request)).

Add a recipe only when Context7 lacks facts your questions need ([design-decisions.md](design-decisions.md#augment-context7-do-not-replace-it)).

## Adding a local evidence profile

1. Copy `doclayer/examples/local-profiles.example.yaml` to `~/.config/doclayer/local-profiles.yaml`, or point `DOC_LAYER_LOCAL_PROFILES` at your file.
2. Add a profile with a unique `name` and at least one `match` regex. Add `require` patterns to narrow it, `always_local: true` if every match is about your machines, or `stop: true` to keep later profiles from claiming a match.
3. Profiles only classify questions; they collect nothing. The keys `documents`, `observations`, `sections` and a top-level `context` are not supported; the loader rejects them by name. Evidence for local needs comes from the private writer's allowlisted commands, configured with `private_writer.hosts`.
4. Check that it loads: `uv run python -I -c "from pathlib import Path; from doclayer.local_profiles import load_profiles; print([p.name for p in load_profiles(Path('~/.config/doclayer/local-profiles.yaml').expanduser()).profiles])"`. A mistake raises `ProfileError` naming the field.
5. For a test, use the `local_profiles` fixture in `tests/conftest.py`, which installs `tests/fixtures/local-profiles.yaml` or a file you pass.

[configuration.md](configuration.md#local-evidence-profiles) has the schema and explains which fields the gap loop uses at this commit.

## Adding a subject to the router

The router learns subjects from committed pages. To make a new product routable, commit a page under `wiki/` with `product: <slug>` and, if people use other names, `aliases: [...]` in its frontmatter. Use `doclayer/routing-policy.yaml` only for what a page cannot say: extra aliases, `facets`, `public_docs.base_url`, `live_checks`, known `hosts`, `local_first` and `early_projects`. Bump `version` when you change the policy's meaning. Add a case to `tests/fixtures/routing-evaluation.yaml` for any prompt that should or should not route to it.

## Changing behaviour

- Write the failing test first, in the file that owns the behaviour.
- Keep the docs true: when you change an environment variable, config key, CLI command or MCP tool, update [configuration.md](configuration.md), [mcp-tools.md](mcp-tools.md) or the page for that component in the same pull request.
- New pages and vault changes follow [vault-format.md](vault-format.md#how-a-page-becomes-canonical).
