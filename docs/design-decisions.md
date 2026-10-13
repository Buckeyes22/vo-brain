# Design decisions

Short records of the decisions that shape the system: the context, the decision, and what follows from it. A glossary of the project's terms is at the end.

## Contents

- [Git is the source of truth; vectors are for fuzzy search](#git-is-the-source-of-truth-vectors-are-for-fuzzy-search)
- [Augment Context7, do not replace it](#augment-context7-do-not-replace-it)
- [No new database, graph engine or hand-kept catalog](#no-new-database-graph-engine-or-hand-kept-catalog)
- [Declared and observed stay apart](#declared-and-observed-stay-apart)
- [Always answer, and say what was missing](#always-answer-and-say-what-was-missing)
- [Route selectively and deterministically](#route-selectively-and-deterministically)
- [A miss is a fact about the lookup](#a-miss-is-a-fact-about-the-lookup)
- [Single-pass curation](#single-pass-curation)
- [Review is optional and after the fact for scoped curator writes](#review-is-optional-and-after-the-fact-for-scoped-curator-writes)
- [Private evidence only from allowlisted read-only commands](#private-evidence-only-from-allowlisted-read-only-commands)
- [Publication is not resolution](#publication-is-not-resolution)
- [One ledger owner, many capturing clients](#one-ledger-owner-many-capturing-clients)
- [Per-job grants, no daily quotas, charges before I/O](#per-job-grants-no-daily-quotas-charges-before-io)
- [One model endpoint, no fallback provider](#one-model-endpoint-no-fallback-provider)
- [The fetcher self-merges only doc copies](#the-fetcher-self-merges-only-doc-copies)
- [Glossary](#glossary)

## Git is the source of truth; vectors are for fuzzy search

**Context.** Agents ask two kinds of question: exact ones ("which port", "which version") and fuzzy ones ("how does X handle Y"). A vector index answers fuzzy questions well, but it can lag, drop a passage, or return a near miss for an exact fact.

**Decision.** Exact facts are read from pages in Git at a known commit (`brainrepo.py`). The vector index is used only for fuzzy search and is treated as a rebuildable copy of Git.

**Consequence.** Every exact fact carries `path@commit`. When the index is down or behind, Git facts still come through and the answer flags the lag. The index can be dropped and rebuilt from Git at any time.

## Augment Context7, do not replace it

**Context.** Context7 already serves public library documentation. It is broad but sometimes thin: it may list a product and still lack the plan, quota or endpoint facts a question needs.

**Decision.** Keep Context7 as the public-docs source. Keep local Markdown copies under `sources/<product>/` only for products where Context7 is thin, and check Context7's text against the facts in those copies before trusting it.

**Consequence.** The fetcher stays small (one recipe per product). A Context7 result that states none of the expected facts is withheld instead of being presented as an answer.

## No new database, graph engine or hand-kept catalog

**Context.** A routing layer needs to know what subjects the brain covers. The tempting fix is a separate catalog, database or graph that someone must keep in step with the pages.

**Decision.** Derive the catalog from committed pages and one committed policy file (`coverage.py`, `doclayer/routing-policy.yaml`). Keep all runtime state in files with atomic writes.

**Consequence.** The catalog can never disagree with the vault, because it is rebuilt from the vault's `HEAD`. A policy entry cannot fabricate coverage: a subject with no committed page is marked `policy`, not covered. Backups are Git plus a few state directories.

## Declared and observed stay apart

**Context.** The most common wrong answer is a page that says one thing while the machine runs another: a pinned version that was upgraded, a port that moved, a CI step that does not exist.

**Decision.** Pages record what is declared. Live checks record what is observed, with the time. Facts tables keep both in separate columns, and a live check never overwrites a declared value.

**Consequence.** A disagreement becomes a visible flag in the answer instead of being silently resolved one way. Old observations are flagged as stale after `DOC_LAYER_STALE_COPY_DAYS`.

## Always answer, and say what was missing

**Context.** An agent that gets nothing back falls back to guessing. A down search service or a slow docs service should not cause that.

**Decision.** Each source has its own timeout. A source that fails becomes a named `unavailable:` line and a `source_unavailable` disposition. The answer uses whatever came back. Unknown facts are written as "unknown", never guessed, and "not documented" is never written as "not supported".

**Consequence.** Answering degrades gradually. Privacy is the one place the system fails closed: a page without a `privacy:` line is treated as `sensitive`.

## Route selectively and deterministically

**Context.** A standing instruction of "consult the brain for every documentation question" fired on generic phrases and file-name fragments, cost a lookup per prompt, and taught agents to ignore it.

**Decision.** One deterministic router (`routing.py`) decides, from the catalog alone, whether a prompt needs a lookup and which sources. It makes no model call and no network call. The hook prints a suggestion only when a lookup is justified.

**Consequence.** The router can run in a prompt hook (measured at 0.149 ms per classification on the demo vault). Its behaviour is pinned by a versioned evaluation set. An unknown or stale catalog never turns into "nothing is covered".

## A miss is a fact about the lookup

**Context.** Repeating a lookup that just failed wastes time, but caching "no answer" risks turning one bad search into a permanent "this product has no docs".

**Decision.** Cache only observed misses, bound to the exact question, scope, sources and evidence generation, for six hours (`negative_cache.py`). Never cache an outage. Word every suppression as a statement about the lookup.

**Consequence.** A new commit, a re-index or a policy change invalidates the cache. `force` bypasses it.

## Single-pass curation

**Context.** The original curator ran a multi-stage pipeline: link discovery, relevance scoring, sectioned review, preservation audits and repair passes. It was careful but took hours per import, which made it impractical as the default path.

**Decision.** Make single-pass the default (`pipeline.default_mode: single-pass`): submitted sources only, quotes checked verbatim without a model, one insert-only draft per page, deterministic checks plus one grounding check. Keep multi-stage available per job and for corrections.

**Consequence.** Single-pass jobs only add text; they cannot reword or remove it. Imports that need restructuring use multi-stage. See [curator.md](curator.md#single-pass-and-multi-stage).

## Review is optional and after the fact for scoped curator writes

**Context.** A gap loop that waits for a human to approve every page stalls whenever the human is busy. But unattended writes must not damage the vault.

**Decision.** The curator publishes without a reviewer, inside a scope enforced by code: only `wiki/` and `infra/`, never `index.md` or `log.md`, no deletes or renames, insert-only by default, operator corrections replay-protected. Every publication is a pull request and a commit. Everything else in the repository keeps the normal propose-and-review flow.

**Consequence.** Review becomes an audit: digests, diffs, corrections, `git revert`. [curator-quality.md](curator-quality.md) lists what stops a bad draft.

## Private evidence only from allowlisted read-only commands

**Context.** Many questions are about the operator's own machines. Public documentation cannot answer them, and letting a model run shell commands is dangerous.

**Decision.** The model may only propose commands. Code checks each one against a fixed allowlist of read-only forms and allowed hosts before it runs (`private_commands.py`). Output that looks like a secret is withheld whole, not redacted. The resulting text reaches the curator only as a private single-pass job from the gap worker.

**Consequence.** Some questions cannot be answered automatically and wait for a person, which is the intended trade. A redaction bug cannot leak a partial secret, because nothing is partially shown.

## Publication is not resolution

**Context.** A merged page might not answer the question, and a page that answers it might not be delivered by retrieval.

**Decision.** A gap closes only when the index has acknowledged the publishing commit, a model finds a literal quote on the page for every requested facet, and the same judgment passes on the evidence the resolver actually delivers (`verification.py`).

**Consequence.** A delivery problem is recorded as `retrieval_defect` and does not trigger a duplicate import. Some gaps wait in `verifying` while the index catches up.

## One ledger owner, many capturing clients

**Context.** Agents run on several machines. Two machines researching the same gap would duplicate work and imports.

**Decision.** One host owns the ledger and runs the worker. Other machines write events to a local outbox and deliver them over ssh to a fixed command (`gap_client.py`, `gap_ops.py`).

**Consequence.** Capture never waits on the network. A lost reply is retried with the same event key and counted once. Moving the owner is an offline export (`gap_migration.py`).

## Per-job grants, no daily quotas, charges before I/O

**Context.** Daily quotas made unrelated work wait for midnight and did not protect against a runaway job.

**Decision.** Each automatic job gets an immutable model-call grant (`DOC_LAYER_GAP_MODEL_CALLS_JOB`, default 50). There is no daily cap. Every call is charged before it is sent.

**Consequence.** A crash loop cannot exceed its grant. An exhausted job is retired with a receipt and needs explicit operator readmission to continue.

## One model endpoint, no fallback provider

**Context.** Silently falling back to a second provider changes cost, quality and where data goes, without anyone noticing.

**Decision.** The curator uses one configured OpenAI-compatible endpoint, with optional per-stage overrides that the operator writes down. When it fails, work waits or stops `retryable` with progress kept.

**Consequence.** Data only goes where the config says. A shared local GPU server can be protected with the idle gate and request priority ([models.md](models.md)).

## The fetcher self-merges only doc copies

**Context.** Refreshing vendor docs weekly should not need a click per refresh, but the fetcher must not land anything else.

**Decision.** The fetcher opens a pull request for every refresh and merges it itself only when the diff touches nothing but `sources/<product>/` and its recipe and every page passed the junk checks (`landing.py`).

**Consequence.** Every copy has a pull-request record. Anything unusual waits for a person.

## Glossary

| Term | Meaning |
|---|---|
| **Gap** | A durable record of one documentation need the brain could not meet (`gaps.GapRecord`), identified by `gap-<hash>` |
| **Need class** | Why a gap exists: `missing_coverage`, `enrichment`, `retrieval_defect`, `infrastructure`, `needs_local_evidence` or `ambiguous` |
| **Subject** | A thing the brain can route to: a product, host, account, model or service, named by a committed page or the routing policy |
| **Catalog** | The coverage catalog: all subjects, derived from committed pages and the routing policy, cached by `HEAD` |
| **Owner, ledger** | The ledger is the directory of gap records; the owner is the one host that holds it and runs the worker |
| **Profile** | A local evidence profile: an operator-written YAML entry that marks a kind of question as local, so the gap worker routes it to the private path |
| **Curator job** | One import in the curator's job store, from submit to digest, with its own state, evidence and drafts |
| **Single-pass** | The default curator mode: submitted sources only, insert-only drafts, one grounding check |
| **Declared vs observed** | What a page or config says, versus what a dated live check saw |
| **Privacy taint** | A page's `privacy:` level, ordered `public` < `internal` < `sensitive`, with `credential` for pages that reference a secret. A consumer may show a page only to profiles cleared for its level |
| **Disposition** | A label on an answer's outcome, such as `answered`, `partial` or `source_unavailable` |
| **Evidence generation** | The vault `HEAD`, index commit and policy version that an answer or cached miss was based on |
| **Slot** | One active curator submission held by the gap dispatcher |
| **Doc copy** | A Markdown copy of a vendor page in `sources/<product>/`, with its URL and fetch date |
