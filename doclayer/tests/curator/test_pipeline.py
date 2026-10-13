"""End-to-end pipeline tests: material in, published pages out — with a
scripted local-Qwen (real HTTP contract), deterministic HTTP fixtures, real
git, and the fake Forgejo. These prove implementation behavior; live Qwen,
Forgejo, SSH, and indexing are separate follow-up checks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from conftest import run_git, valid_meta

from doclayer.curator.jobstore import JobStore
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.publish import Publisher
from doclayer.curator.state import JobState, SourceRecord


def make_job(store: JobStore, checkout: Path, intent: str, key: str = "") -> JobState:
    state = JobState(
        job_id="",
        idempotency_key=key,
        intent=intent,
        base_commit=run_git(checkout, "rev-parse", "HEAD"),
    )
    state, _ = store.create(state)
    return state


def url_source(url: str, label: str | None = None) -> SourceRecord:
    from doclayer.curator.intake import make_url_source

    return make_url_source(url)


def submit_payload_job(store, checkout, *, urls=(), texts=(), intent="test import", exclusions=()):
    state = make_job(store, checkout, intent)
    from doclayer.curator.intake import make_text_source, make_url_source

    state.sources = [make_url_source(u) for u in urls]
    state.sources += [make_text_source(label, text) for label, text in texts]
    for index, (_label, text) in enumerate(texts):
        path = store.staging_dir(state.job_id) / f"text-{index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        state.sources[len(urls) + index].local_path = str(path)
    state.exclusions = list(exclusions)
    store.save(state)
    return state


def run_pipeline(cfg, store, qwen, http_pages, state, forgejo=None) -> JobState:
    if isinstance(http_pages, dict):
        from conftest import HttpFixture

        http_pages = HttpFixture(dict(http_pages))
    pipeline = Pipeline(
        cfg,
        store,
        fetch_transport=http_pages.transport(),
        inference_transport=qwen.transport(),
        publisher=Publisher(cfg, store, forgejo=forgejo) if forgejo else None,
    )
    import asyncio

    return asyncio.run(pipeline.run(state))


def git_show(repo: Path, ref_path: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(repo), "show", ref_path], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


@pytest.mark.asyncio
async def test_same_section_retains_distinct_claims_and_excludes_out_of_scope(cfg, store, checkout, monkeypatch):
    from doclayer.curator import curate

    state = make_job(store, checkout, "model facts")
    source = url_source("https://docs.test/model")
    source.status = "fetched"
    state.sources = [source]
    store.save(state)
    pipeline = Pipeline(cfg, store)
    pipeline._store_extract(state, source, "model facts")

    async def claims(*args, **kwargs):
        return [
            {"claim": "Fact A", "locator": "Overview"},
            {"claim": "Fact B", "locator": "Overview"},
            {"claim": "Fact A", "locator": "Overview"},
            {"claim": "Bedrock usage", "locator": "Overview", "excluded_topic": True},
        ]

    monkeypatch.setattr(curate, "extract_claims", claims)
    await pipeline._stage_extract(state)
    await pipeline._stage_extract(state)
    assert [e["text"] for e in store.read_evidence(state.job_id)] == [
        "Fact A",
        "Fact B",
    ]


def test_worker_observes_external_cancel_and_does_not_overwrite_it(cfg, store, checkout):
    from doclayer.curator.pipeline import CancelledByRequest

    state = make_job(store, checkout, "cancel test")
    remote = store.load(state.job_id)
    remote.cancel_requested = True
    store.save(remote)
    pipeline = Pipeline(cfg, store)
    pipeline._persist(state)
    assert store.load(state.job_id).cancel_requested
    with pytest.raises(CancelledByRequest):
        pipeline._check_cancel(state)


@pytest.mark.asyncio
async def test_large_source_reviews_every_section_group():
    """No primary/supplementary group maximum and no selection call: every
    section group of a large source is extracted."""
    from doclayer.curator import curate

    extracted = []

    class Client:
        async def chat_json(self, system, user):
            if "Verify selected claims against original source excerpts" in system:
                items = json.loads(user.split("\n", 1)[1])
                return {
                    "verdicts": [
                        {
                            "id": item["id"],
                            "supported": True,
                            "reason": "supported source fixture",
                        }
                        for item in items
                    ]
                }
            assert "Select the most useful" not in system
            index = next(i for i in range(5) if f"Facts {i}" in user)
            extracted.append(index)
            return {
                "doc_kind": "system-card",
                "claims": [{"claim": f"Supported fact {index}", "locator": f"#Facts {index}"}],
            }

    source = url_source("https://docs.test/card.pdf")
    text = "\n".join(f"## Facts {i}\n" + f"Evidence {i}. " * 100 for i in range(5))
    claims = await curate.extract_claims(Client(), source, text, "model", [], char_budget=5000)
    assert len(claims) == 5 and sorted(extracted) == [0, 1, 2, 3, 4]
    assert source.reviewed_sections == [f"#Facts {i}" for i in range(5)]
    assert source.omitted_sections == []


def test_bounded_evidence_includes_later_sources():
    from doclayer.curator.curate import balanced_evidence
    from doclayer.curator.state import Evidence

    records = [Evidence(source_id="long", label="long", locator="p", modality="data", text=str(i)) for i in range(20)]
    records.append(
        Evidence(
            source_id="later",
            label="later",
            locator="s",
            modality="data",
            text="important",
        )
    )
    assert [e.source_id for e in balanced_evidence(records, 3)] == [
        "long",
        "later",
        "long",
    ]


def test_source_id_citations_resolve_to_urls_without_phantom_wikilinks():
    from doclayer.curator.pipeline import hydrate_citations
    from doclayer.curator.state import Evidence

    e = Evidence(
        source_id="src-1234567890",
        label="card",
        locator="Page 3",
        modality="data",
        text="fact",
        url="https://docs.test/card.pdf",
    )
    text = hydrate_citations(
        "Fact [src-1234567890]. Another [[src-1234567890]]. Unknown [src-0000000000].",
        [e],
    )
    assert text.count("[Source](<https://docs.test/card.pdf>)") == 2
    assert "[[src-1234567890]]" not in text
    assert "[src-0000000000]" in text


@pytest.mark.asyncio
async def test_drafting_mints_distinct_ids_and_internal_metadata(cfg, store, checkout, monkeypatch):
    from doclayer.curator import curate
    from doclayer.curator.state import PageChange

    state = make_job(store, checkout, "two pages")
    state.changes = [
        PageChange(group_id=f"g{i}", path=f"wiki/new-{i}.md", action="create", title=f"New {i}") for i in range(2)
    ]

    async def draft(*args, **kwargs):
        return {
            "frontmatter": dict(valid_meta(cid="c-a1b2c3d4"), privacy="public"),
            "body": "# New\n\nSupported.",
        }

    monkeypatch.setattr(curate, "draft_page", draft)
    await Pipeline(cfg, store)._stage_draft(state)
    assert len({c.draft_meta["concept_id"] for c in state.changes}) == 2
    assert all(c.draft_meta["privacy"] == "internal" for c in state.changes)
    assert all(f"curator:{state.job_id}" in c.draft_meta["source_lineage"] for c in state.changes)


LONG_BODY = (
    "The model supports a 128,000-token maximum output. "
    + "Real documentation content about test-model capabilities. " * 40
)


def placement_reply(template):
    """Bind each scripted placement to the actual whole incoming claim IDs."""

    def respond(user):
        selected = json.loads(user.split("Claims JSON:\n", 1)[1])
        pages = json.loads(json.dumps(template.get("pages", [])))
        if template.get("already_covered"):
            return {
                "pages": [],
                "covered": [{"claim_id": item["id"], "path": "wiki/existing-model.md"} for item in selected],
            }
        for index, page in enumerate(pages):
            assigned = selected[index :: len(pages)]
            page["claim_ids"] = [item["id"] for item in assigned]
            page["source_ids"] = list(dict.fromkeys(item["source_id"] for item in assigned))
        return {"pages": pages, "covered": []}

    return respond


def test_full_flow_publishes_connected_pages(cfg, store, qwen, checkout, origin, forgejo) -> None:
    # Ordinary imports must publish useful connected pages under the normal
    # gap-dispatch allowance, without the retired atomic proof engine.
    pages = {
        "https://docs.test/model": (
            200,
            f"<html><head><title>Model X</title></head><body><article><h1>Model X</h1><p>{LONG_BODY}</p><p><a href='https://docs.test/model-card'>model card</a></p></article></body></html>",
        ),
        "https://docs.test/model-card": (
            200,
            f"<html><head><title>Model X card</title></head><body><article><h1>Card</h1><p>{LONG_BODY}</p></article></body></html>",
        ),
    }
    qwen.overrides["relevance"] = [
        json.dumps(
            {
                "assessments": [
                    {
                        "url": "https://docs.test/model-card",
                        "relevant": True,
                        "priority": "high",
                        "why": "model card adds model evidence",
                    }
                ]
            }
        )
    ]
    qwen.overrides["plan"] = [
        placement_reply(
            {
                "already_covered": False,
                "explanation": "no page for this model",
                "pages": [
                    {
                        "action": "create",
                        "path": "wiki/test-model.md",
                        "title": "Test model",
                        "subject_type": "model",
                        "rationale": "model overview",
                        "related": ["wiki/test-model-card-findings.md"],
                        "source_ids": [],
                    },
                    {
                        "action": "create",
                        "path": "wiki/test-model-card-findings.md",
                        "title": "Test model card findings",
                        "subject_type": "model",
                        "rationale": "card evidence",
                        "related": ["wiki/test-model.md"],
                        "source_ids": [],
                    },
                ],
                "consolidation_opportunities": [],
            }
        )
    ]
    draft_bodies = [
        {
            "frontmatter": dict(valid_meta(cid="c-aaaa0001"), title="Test model"),
            "body": "# Test model\n\n## Capabilities\n\nThe model supports a 128,000-token maximum output. [source: https://docs.test/model]\n\n## Related\n\n[[test-model-card-findings]]\n",
        },
        {
            "frontmatter": dict(valid_meta(cid="c-aaaa0002"), title="Test model card findings"),
            "body": "# Test model card findings\n\nThe model supports a 128,000-token maximum output. [source: https://docs.test/model-card]\n\nBack to [[test-model]].\n",
        },
    ]
    qwen.overrides["draft"] = [json.dumps(d) for d in draft_bodies]
    # repair pass uses the same contract
    qwen.overrides["repair"] = [json.dumps(draft_bodies[0])]
    state = submit_payload_job(
        store,
        checkout,
        urls=["https://docs.test/model"],
        intent="build test-model knowledge",
    )
    state.limits["max_model_calls"] = 50
    store.save(state)
    state = run_pipeline(cfg, store, qwen, pages, state, forgejo)

    assert state.status == "published", state.summary or state.error
    assert {c.status for c in state.changes} == {"published"}
    landed = git_show(origin, "main:wiki/test-model.md")
    assert landed and "[[test-model-card-findings]]" in landed
    landed2 = git_show(origin, "main:wiki/test-model-card-findings.md")
    assert landed2 and "Back to [[test-model]]" in landed2
    assert forgejo.prs and all(pr["merged"] for pr in forgejo.prs.values())
    # evidence retained durably in the job dir, not the cache
    evidence = store.read_evidence(state.job_id)
    assert len(evidence) >= 1
    assert state.usage.model_calls > 0
    assert state.usage.model_calls <= 50
    assert not any("ENUMERATION FIDELITY" in call["user"] or "SCOPE WITNESS" in call["user"] for call in qwen.calls)
    # discovered link provenance recorded
    discovered = [s for s in state.sources if not s.submitted]
    assert any(s.parent_id for s in discovered)
    # draft files live in the job dir, never canonical
    assert (store.job_dir(state.job_id) / "drafts").is_dir()
    # the full summary digest exists
    assert "## Sources" in (store.read_summary(state.job_id) or "")


def test_already_covered_is_a_successful_noop(cfg, store, qwen, checkout) -> None:
    target = checkout / "wiki/existing-model.md"
    target.write_text(target.read_text() + "\nThe model supports a 128,000-token maximum output.\n")
    run_git(checkout, "add", str(target))
    run_git(checkout, "commit", "-qm", "canonical fixture represents incoming whole claim")
    run_git(checkout, "push", "origin", "main")
    qwen.overrides["plan"] = [
        placement_reply(
            {
                "already_covered": True,
                "explanation": "the existing page already covers this material",
                "pages": [],
                "consolidation_opportunities": [],
            }
        )
    ]
    state = submit_payload_job(
        store,
        checkout,
        texts=[
            (
                "notes",
                "The model supports a 128,000-token maximum output. " + "Some operator notes " * 50,
            )
        ],
        intent="already known material",
    )
    state = run_pipeline(cfg, store, qwen, {}, state, forgejo=None)
    assert state.status == "covered"
    assert state.changes == []
    # no publication attempted, no PR
    assert state.publication.pr_number is None


def test_repeat_import_of_same_input_is_deduplicated(cfg, store, qwen, checkout, origin, forgejo) -> None:
    """The first import publishes; a byte-identical resubmit must not create
    pages or PRs again."""
    texts = [
        (
            "notes",
            "The model supports a 128,000-token maximum output. " + "Operator observations about the deployment. " * 40,
        )
    ]
    qwen.overrides["plan"] = [
        placement_reply(
            {
                "already_covered": False,
                "explanation": "new",
                "pages": [
                    {
                        "action": "create",
                        "path": "wiki/observations.md",
                        "title": "Observations",
                        "subject_type": "general",
                        "rationale": "r",
                        "related": [],
                        "source_ids": [],
                    }
                ],
                "consolidation_opportunities": [],
            }
        )
    ]
    qwen.overrides["draft"] = [
        json.dumps(
            {
                "frontmatter": dict(valid_meta(cid="c-aaaa0010"), title="Observations"),
                "body": "# Observations\n\nThe model supports a 128,000-token maximum output. [source: notes]\n",
            }
        )
    ]
    from doclayer.curator.ops import derive_idempotency_key

    payload = {
        "intent": "import observations",
        "texts": [{"label": "notes", "text": texts[0][1]}],
    }
    key = derive_idempotency_key(payload)

    state1 = submit_payload_job(store, checkout, texts=texts, intent="import observations")
    state1.idempotency_key = key
    store.save(state1)
    state1 = run_pipeline(cfg, store, qwen, {}, state1, forgejo)
    assert state1.status == "published"

    # same input again: content-index short-circuit
    index = store.find_published_input(state1.input_digest)
    assert index is not None and index["job_id"] == state1.job_id


def test_model_budget_exhaustion_preserves_work_and_reports(cfg, store, qwen, checkout, forgejo) -> None:
    pages = {
        "https://docs.test/model": (
            200,
            f"<html><head><title>M</title></head><body><article><h1>M</h1><p>{LONG_BODY}</p></article></body></html>",
        ),
    }
    qwen.overrides["plan"] = [
        placement_reply(
            {
                "already_covered": False,
                "explanation": "new",
                "pages": [
                    {
                        "action": "create",
                        "path": "wiki/budget-model.md",
                        "title": "Budget model",
                        "subject_type": "model",
                        "rationale": "r",
                        "related": [],
                        "source_ids": [],
                    }
                ],
                "consolidation_opportunities": [],
            }
        )
    ]
    qwen.overrides["draft"] = [
        json.dumps(
            {
                "frontmatter": dict(valid_meta(cid="c-aaaa0011"), title="Budget model"),
                "body": "# Budget model\n\nThe model supports a 128,000-token maximum output. [source: https://docs.test/model]\n",
            }
        ),
        json.dumps(
            {
                "frontmatter": dict(valid_meta(cid="c-aaaa0012"), title="Second"),
                "body": "# Second\n\nContent. [source: https://docs.test/model]\n",
            }
        ),
    ]
    state = submit_payload_job(store, checkout, urls=["https://docs.test/model"], intent="budget test")
    import asyncio
    import copy

    from conftest import HttpFixture

    from doclayer.curator.state import Usage

    # Prepare real source/draft work under its normal grant, then create a
    # distinct linked editorial job retaining the original five-call cap.
    pipeline = Pipeline(
        cfg,
        store,
        fetch_transport=HttpFixture(pages).transport(),
        inference_transport=qwen.transport(),
        publisher=Publisher(cfg, store, forgejo=forgejo),
    )

    async def prepare():
        stage = "intake"
        while stage != "validate":
            state.stage = stage
            stage = await getattr(pipeline, "_stage_" + stage)(state)
            assert stage is not None
        state.stage = "validate"
        store.save(state)

    asyncio.run(prepare())
    parent = store.load(state.job_id)
    parent_snapshot = copy.deepcopy(parent.to_dict())
    state = copy.deepcopy(parent)
    state.job_id, state.idempotency_key = "", ""
    state.continuation_of = parent.job_id
    state.usage = Usage()
    state.limits = {"max_model_calls": 5}
    state = store.create(state)[0]
    store.append_evidence(state.job_id, store.read_evidence(parent.job_id))
    original_validate = pipeline._stage_validate

    async def exhaust_at_editorial(current):
        client = pipeline._model(current)
        assert client.usage is current.usage
        for _ in range(5):
            await client.chat_json("Budget fixture JSON response", "{}")
        return await original_validate(current)

    pipeline._stage_validate = exhaust_at_editorial
    state = asyncio.run(pipeline.run(state))
    assert store.load(parent.job_id).to_dict() == parent_snapshot
    assert state.publication.pr_number is None
    # the final editorial check blows the budget: resumable with gaps
    assert state.status == "retryable", state.summary
    assert state.coverage == "incomplete"
    assert any("limit" in gap for gap in state.coverage_gaps)
    # counters persisted, not reset; drafted work preserved
    assert state.usage.model_calls == 5
    assert any(c.draft_body for c in state.changes)


def test_inference_outage_is_retryable_not_fallback(cfg, store, qwen, checkout) -> None:
    """Inference failing must preserve work and mark retryable — never route
    to another provider."""
    qwen.failures = [httpx.ConnectError("inference down") for _ in range(cfg.inference.retries + 1)]
    state = submit_payload_job(store, checkout, texts=[("n", "note " * 60)], intent="outage test")
    state = run_pipeline(cfg, store, qwen, {}, state, forgejo=None)
    assert state.status == "retryable"
    assert (state.error or "").startswith("inference:")
    assert state.stage == "extract"
    # nothing published
    assert state.publication.pr_number is None


def test_excluded_links_never_become_sources(cfg, store, qwen, checkout) -> None:
    pages = {
        "https://docs.test/model": (
            200,
            f"<html><head><title>M</title></head><body><article><h1>M</h1><p>{LONG_BODY}</p>"
            "<p><a href='https://provider.test/bedrock-usage'>bedrock usage guide</a></p></article></body></html>",
        ),
    }
    state = submit_payload_job(
        store,
        checkout,
        urls=["https://docs.test/model"],
        intent="model knowledge",
        exclusions=["bedrock usage"],
    )
    state = run_pipeline(cfg, store, qwen, pages, state, forgejo=None)
    bedrock = [s for s in state.sources if s.url and "bedrock" in (s.url or "")]
    assert len(bedrock) == 1
    assert bedrock[0].status == "rejected" and bedrock[0].exclusions_hit
    assert bedrock[0].content_hash is None  # durable disposition; never fetched


def test_unreachable_source_fails_and_work_continues(cfg, store, qwen, checkout, forgejo) -> None:
    pages = {
        "https://docs.test/good": (
            200,
            f"<html><head><title>G</title></head><body><article><h1>G</h1><p>{LONG_BODY}</p></article></body></html>",
        ),
        # https://docs.test/dead is absent -> 404 (permanent failure)
        "https://docs.test/dead": (404, "missing"),
    }
    qwen.overrides["plan"] = [
        placement_reply(
            {
                "already_covered": False,
                "explanation": "new",
                "pages": [
                    {
                        "action": "create",
                        "path": "wiki/dead-or-alive.md",
                        "title": "D",
                        "subject_type": "general",
                        "rationale": "r",
                        "related": [],
                        "source_ids": [],
                    }
                ],
                "consolidation_opportunities": [],
            }
        )
    ]
    qwen.overrides["draft"] = [
        json.dumps(
            {
                "frontmatter": dict(valid_meta(cid="c-aaaa0013"), title="D"),
                "body": "# D\n\nThe model supports a 128,000-token maximum output. [source: https://docs.test/good]\n",
            }
        )
    ]
    state = submit_payload_job(
        store,
        checkout,
        urls=["https://docs.test/good", "https://docs.test/dead"],
        intent="failure isolation",
    )
    state = run_pipeline(cfg, store, qwen, pages, state, forgejo)
    dead = next(s for s in state.sources if s.url and s.url.endswith("/dead"))
    assert dead.status == "failed" and "404" in (dead.error or "")
    good = next(s for s in state.sources if s.url and s.url.endswith("/good"))
    assert good.status == "fetched"
    assert state.status == "published"


def test_reports_distinguish_failed_skipped_and_duplicate_sources(cfg, store, checkout):
    from doclayer.curator.ops import _status_view

    state = make_job(store, checkout, "source accounting")
    for label, status in [
        ("manifest metadata", "skipped"),
        ("same content", "duplicate"),
        ("broken URL", "failed"),
    ]:
        source = url_source("https://docs.test/" + status)
        source.label = label
        source.status = status
        state.sources.append(source)
    status = _status_view(state, store)
    assert {key: status["sources"][key] for key in ("total", "fetched", "failed", "skipped", "duplicate")} == {
        "total": 3,
        "fetched": 0,
        "failed": 1,
        "skipped": 1,
        "duplicate": 1,
    }
    digest = Pipeline(cfg, store).build_digest(state)
    assert "FAILED broken URL" in digest
    assert "SKIPPED manifest metadata" in digest
    assert "DUPLICATE same content" in digest
    assert "FAILED manifest metadata" not in digest
