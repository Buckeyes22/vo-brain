# Guide for AI coding agents

This repository holds a markdown knowledge vault and `doclayer`, the Python package that answers questions from it, records knowledge gaps and curates new pages. Read [README.md](README.md) first, then the page in [docs/](docs/) for the area you are changing.

## Layout

- `wiki/`, `infra/`: canonical vault pages. `sources/`: vendor doc copies. `inbox/`: proposals. `templates/page.md`: the page template.
- `index.md` and `log.md`: navigation and the librarian log. Do not edit them by hand.
- `doclayer/src/doclayer/`: the package. `doclayer/src/doclayer/curator/`: the curator.
- `doclayer/tests/`: the test suite. `doclayer/tests/curator/`: curator tests.
- `doclayer/deploy/`: systemd units and install scripts.
- `docs/`: system documentation. Keep it true to the code when you change behaviour.

## Running code

- Work from `doclayer/` with `uv run`. Never call `python` or `python3` directly; use `uv run python -I` for one-off scripts.
- Run the tests with `cd doclayer && uv run pytest -q -p no:cacheprovider 2>&1 | tail -15`. Filter the output; do not paste whole logs.
- Run the lint and format gate with `cd doclayer && uv run ruff check . && uv run ruff format --check .`. CI runs both.
- Tests must not touch real user state. `doclayer/tests/conftest.py` already points caches, the gap ledger and local profiles at temporary paths.

## Page rules

- Every page carries the frontmatter in `templates/page.md`. The `privacy:` field is mandatory: `public`, `internal`, `sensitive` or `credential`. A page without it is treated as `sensitive`.
- Default new pages to `privacy: internal`.
- Never put a secret value in the vault.
- Each new fact needs a source: `path@commit`, `URL @ fetch date`, or a dated live check.

## How changes land

- Propose a page under `inbox/` on a short-lived branch and open a pull request against `main`. The merge promotes it to `wiki/` or `infra/`.
- Code changes also land through a pull request, with tests for new behaviour.
- See [docs/vault-format.md](docs/vault-format.md) for the promotion flow and [docs/development.md](docs/development.md) for development steps.
