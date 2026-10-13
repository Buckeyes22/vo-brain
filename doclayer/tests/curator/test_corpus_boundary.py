"""Import evidence cannot silently become a wider corpus or canonical files."""

from pathlib import Path

import pytest
from conftest import HttpFixture, run_git
from test_publish import make_job, publish_with_branch_tracking, write_change

from doclayer.curator import curate, intake, ops
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.publish import Publisher
from doclayer.curator.state import JobState


def test_library_selection_is_not_import_enrollment(cfg, store, checkout, monkeypatch):
    """Search collection selection is not a curator source or publication API."""
    before = run_git(checkout, "rev-parse", "HEAD")
    cfg.vo_rag_url = "http://unrelated-library.test"

    def forbidden_network(*args, **kwargs):
        pytest.fail("Submission must only stage explicit inputs, not query a corpus")

    monkeypatch.setattr("httpx.Client.send", forbidden_network)
    monkeypatch.setattr("httpx.AsyncClient.send", forbidden_network)
    with pytest.raises(ops.OpsError, match="submit carries no inputs"):
        ops.handle("submit", {"intent": "Firmware recovery", "collections": ["books"]}, cfg, store)
    assert not list(store.jobs_dir.glob("job-*/state.json"))

    result = ops.handle(
        "submit",
        {
            "intent": "Firmware recovery",
            "collections": ["books"],
            "texts": [
                {
                    "label": "Selected library passage",
                    "text": "Firmware recovery requires disconnecting power. Library citation: manual p. 12.",
                }
            ],
        },
        cfg,
        store,
    )
    job = store.load(result["job_id"])
    assert job.status == "queued" and job.changes == []
    assert len(job.sources) == 1 and job.sources[0].kind == "text"
    staged = Path(job.sources[0].local_path)
    assert staged.is_relative_to(store.jobs_dir)
    assert "manual p. 12" in staged.read_text()
    assert run_git(checkout, "rev-parse", "HEAD") == before
    assert run_git(checkout, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_discovered_library_links_remain_bounded_job_evidence(cfg, store, checkout, monkeypatch):
    before = run_git(checkout, "rev-parse", "HEAD")
    state = store.create(JobState(job_id="", idempotency_key="", intent="Firmware recovery"))[0]
    state.limits = {"max_sources": 2}
    state.exclusions = ["billing"]
    state.source_policy = {"exclude": [{"domain": "library.example", "path": "/restricted/*"}]}
    seed = intake.make_url_source("https://library.example/manual")
    seed.status = "fetched"
    state.sources, state.usage.sources_fetched = [seed], 1
    cfg.vo_rag_url = "http://unrelated-library.test"
    fixture = HttpFixture(
        {
            "https://library.example/recovery": (
                200,
                "<article><h1>Recovery</h1><p>" + "Firmware recovery instructions. " * 30 + "</p></article>",
            )
        }
    )
    pipe = Pipeline(cfg, store, fetch_transport=fixture.transport())
    pipe._store_extract(
        state,
        seed,
        "[Recovery](https://library.example/recovery)\n"
        "[Billing](https://library.example/billing)\n"
        "[Restricted](https://library.example/restricted/manual)\n"
        + "\n".join(f"[Peripheral {i}](https://library.example/catalog-{i})" for i in range(15)),
    )
    scoring = []

    async def score(client, topic, exclusions, candidates):
        scoring.append((topic, exclusions, candidates))
        return {
            url: {
                "relevant": True,
                "priority": "high" if url.endswith("recovery") else "low",
                "why": "within submitted topic",
            }
            for url, _ in candidates
        }

    monkeypatch.setattr(curate, "score_link_relevance", score)
    await pipe._stage_discover(state)
    durable = store.load(state.job_id)
    assert fixture.requested == ["https://library.example/recovery"]
    assert all(topic == state.intent and "billing" in exclusions for topic, exclusions, _ in scoring)
    children = [s for s in durable.sources if s.parent_id == seed.id]
    assert all(s.role == "supplementary" for s in children)
    assert {s.url for s in children if s.status == "rejected"} == {
        "https://library.example/billing",
        "https://library.example/restricted/manual",
    }
    assert len([s for s in children if s.status == "scored"]) == 15
    assert durable.coverage == "incomplete" and durable.changes == []
    assert Path(next(s.extract_path for s in children if s.status == "fetched")).is_relative_to(Path(cfg.paths.cache))
    await pipe._stage_discover(durable)
    assert fixture.requested == ["https://library.example/recovery"]
    assert run_git(checkout, "rev-parse", "HEAD") == before
    assert run_git(checkout, "status", "--porcelain") == ""


def test_publication_promotes_only_allowed_pages_not_inputs_or_navigation(cfg, store, forgejo, checkout, origin):
    """Even already-validated forbidden paths cannot bypass the actual writer."""
    before = run_git(checkout, "rev-parse", "HEAD")
    job = make_job(store, checkout)
    source = intake.make_url_source("https://library.example/manual")
    source.status = "fetched"
    job.sources = [source]
    Pipeline(cfg, store)._store_extract(job, source, "# Manual\nSelected source material.")
    corrections = store.add_correction({"page": "wiki/recovery.md", "claim": "Keep operator qualification"})
    paths = [
        "wiki/recovery.md",
        "infra/recovery-service.md",
        "sources/library/manual.md",
        "inbox/library-proposal.md",
        "doclayer/job-state.md",
        "cache/extract.md",
        "wiki/index.md",
        "infra/log.md",
        "wiki/manual.pdf",
        "../outside.md",
    ]
    changes = [
        write_change(path, f"c-aaaa{i:04x}", "# Recovery\n\nSource-backed recovery. [source: manual]")
        for i, path in enumerate(paths)
    ]
    for i, change in enumerate(changes):
        change.group_id = f"g{i}"
    job.changes = changes
    outcome = publish_with_branch_tracking(
        Publisher(cfg, store, forgejo=forgejo), job, changes, forgejo, persist=lambda: store.save(job)
    )
    assert outcome.merged and set(outcome.published_changes) == set(paths[:2])
    assert all(c.status == "conflict" and "destination rejected" in c.error for c in changes[2:])
    assert set(run_git(origin, "diff", "--name-only", before, "main").splitlines()) == set(paths[:2])
    assert store.corrections()[0] == corrections
    assert Path(source.extract_path).is_file()
    assert run_git(checkout, "rev-parse", "HEAD") == before
    assert run_git(checkout, "status", "--porcelain") == ""
    assert store.load(job.job_id).publication.merged
