"""Publication closure uses real curator ops, committed Git and charged HTTP."""

import hashlib
import json
import subprocess

import httpx
import pytest

from doclayer import resolver, verification
from doclayer.brainrepo import BrainRepo
from doclayer.curator.config import CuratorConfig
from doclayer.curator.jobstore import JobStore
from doclayer.curator.model import ModelClient
from doclayer.curator.ops import _op_result
from doclayer.curator.state import JobState, PageChange, Publication, Usage
from doclayer.gap_budget import ResourceBudget, today
from doclayer.gaps import GapLedger
from doclayer.negative_cache import NegativeCache


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def published(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    path = "wiki/nimbus.md"
    (root / "wiki").mkdir()
    (root / path).write_text(
        "---\ntitle: Nimbus\nproduct: nimbus\n---\n# Nimbus\nEnterprise edition only.\n## Rate\nNimbus request rate limit is 40 RPM.\n"
    )
    git(root, "add", path)
    git(root, "commit", "-m", "Publish Nimbus rate")
    commit = git(root, "rev-parse", "HEAD")
    ledger = GapLedger(tmp_path / "gaps")
    record, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="nimbus",
        catalog_subject=True,
        representative_question="What is Nimbus rate limit?",
        facets=["rate"],
    )
    state = JobState(
        job_id="job-test12345",
        idempotency_key="idem",
        intent=record.representative_question,
        status="published",
        stage="report",
        changes=[PageChange("group", path, "create", status="published")],
        publication=Publication(merged=True, merge_commit=commit),
    )
    store = JobStore(tmp_path / "jobs")
    store.create(state)
    reply = _op_result({"job_id": state.job_id}, store)
    record.job_links = [{"job_id": state.job_id}]
    ledger.save(record)
    index = dict(
        last_commit=commit,
        writer_version="brain-v4-governing-context",
        files={path: hashlib.sha256((root / path).read_bytes()).hexdigest()},
        file_writer_versions={path: "brain-v4-governing-context"},
        last_error=None,
    )
    monkeypatch.setattr(verification, "brain_sync_state", lambda: index)
    monkeypatch.setattr(resolver.settings, "brain_repo_selection", lambda: (root, True))
    monkeypatch.setattr(resolver.settings, "negative_cache_path", tmp_path / "negative.json")
    monkeypatch.setattr(resolver.settings, "catalog_cache_dir", tmp_path / "catalog")

    async def search(*args):
        return [], None

    async def docs(*args):
        return {"error": "fixture public service unavailable"}

    monkeypatch.setattr(resolver, "query_vo_rag", search)
    monkeypatch.setattr(resolver, "query_context7", docs)
    monkeypatch.setattr(resolver, "_safe_index_lag", lambda repo: [])
    monkeypatch.setattr(resolver, "stale_observation_flags", lambda repo: [])
    return root, path, commit, ledger, record, reply, index


def client(tmp_path, reply):
    cfg = CuratorConfig()
    cfg.credentials_file = str(tmp_path / "no-credentials")
    cfg.inference.retries = 0
    usage = Usage()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]},
        )
    )
    return ModelClient(cfg, usage, transport=transport), usage


def positive(path):
    return dict(
        answers=True,
        facets=[
            dict(
                facet="rate",
                answered=True,
                scope_preserved=True,
                evidence=[dict(path=path, quote="Nimbus request rate limit is 40 RPM.")],
            )
        ],
        reason="Qualified actual rate statement",
    )


def test_real_ops_git_semantic_publication_closure_and_relevant_cache_only(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    cache = NegativeCache(resolver.settings.negative_cache_path)
    cache.record(record.representative_question, "nimbus", generation="old")
    cache.record("What is Other quota?", "other", generation="old")
    model, usage = client(tmp_path, positive(path))
    budget = ResourceBudget(ledger.root)
    result = verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=model, budget=budget)
    assert result.outcome == "resolved" and result.publication_commit == commit and result.index_acknowledged
    assert usage.model_calls == 2 and budget.load(today())["model_calls"] == 2
    saved = ledger.load(record.gap_id)
    assert saved.status == "resolved" and saved.resolution_verification["page_hashes"][path] == index["files"][path]
    assert cache.check("What is Other quota?", "other", generation="old")
    assert cache.check(record.representative_question, "nimbus", generation="old") is None


def test_later_descendant_requires_actual_page_generation_and_no_error(published):
    root, path, commit, ledger, record, reply, index = published
    (root / "other.txt").write_text("Unrelated change")
    git(root, "add", "other.txt")
    git(root, "commit", "-m", "Later unrelated canonical revision")
    index["last_commit"] = git(root, "rev-parse", "HEAD")
    repo = BrainRepo(root, snapshot=True)
    assert verification._index_acknowledged(commit, [path], repo, index)
    index["files"][path] = "0" * 64
    assert not verification._index_acknowledged(commit, [path], repo, index)
    index["files"][path] = hashlib.sha256((root / path).read_bytes()).hexdigest()
    index["last_error"] = "failed affected page"
    assert not verification._index_acknowledged(commit, [path], repo, index)


@pytest.mark.parametrize(
    "bad",
    [
        {"answers": "true", "facets": [], "reason": "true"},
        {
            "answers": True,
            "facets": [
                {
                    "facet": "rate",
                    "answered": True,
                    "scope_preserved": True,
                    "evidence": [
                        {
                            "path": "wiki/unrelated.md",
                            "quote": "Nimbus request rate limit is 40 RPM.",
                        }
                    ],
                }
            ],
            "reason": "true",
        },
        {
            "answers": True,
            "facets": [
                {
                    "facet": "rate",
                    "answered": True,
                    "scope_preserved": False,
                    "evidence": [
                        {
                            "path": "wiki/nimbus.md",
                            "quote": "Nimbus request rate limit is 40 RPM.",
                        }
                    ],
                }
            ],
            "reason": "true",
        },
    ],
)
def test_invalid_semantics_never_close_actual_publication(published, tmp_path, bad):
    root, path, commit, ledger, record, reply, index = published
    model, usage = client(tmp_path, bad)
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    assert result.outcome == "verifying" and ledger.load(record.gap_id).status != "resolved" and usage.model_calls == 1


def test_unrelated_fact_and_multifacet_partial_not_answered():
    a = resolver.Answer(
        "What is Nimbus rate limit and output limit?",
        "nimbus",
        git_facts=[
            {
                "fact": "Documentation",
                "value": "Nimbus has documentation",
                "source": "wiki/nimbus.md@abc",
                "evidence_context": "Nimbus has documentation.",
            }
        ],
    )
    a.checked_sources = ["brain_git"]
    assert "answered" not in resolver._dispositions(a, lag_flags=[], called={"brain_git": True})
    a.git_facts = [
        {
            "fact": "Request rate",
            "value": "40 RPM",
            "source": "wiki/nimbus.md@abc",
            "evidence_context": "Request rate is 40 RPM.",
        }
    ]
    assert "partial" in resolver._dispositions(
        a, lag_flags=[], called={"brain_git": True}
    ) and "answered" not in resolver._dispositions(a, lag_flags=[], called={"brain_git": True})


def test_hook_cache_requires_current_generation(tmp_path):
    cache = NegativeCache(tmp_path / "cache")
    cache.record("Nimbus rate?", "nimbus", generation="old")
    assert cache.check_any_sources("Nimbus rate?", "nimbus") is None
    assert cache.check_any_sources("Nimbus rate?", "nimbus", generation="new") is None
    assert cache.check_any_sources("Nimbus rate?", "nimbus", generation="old")


def test_completed_nonanswering_import_and_whole_bound_no_call(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    model, usage = client(
        tmp_path,
        dict(
            answers=False,
            facets=[dict(facet="rate", answered=False, scope_preserved=True, evidence=[])],
            reason="Documentation only does not establish a rate",
        ),
    )
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    assert result.outcome == "failed" and usage.model_calls == 1
    # Complete context does not fit: no partial prefix is sent to the model and
    # no false completed negative is recorded.
    model2, usage2 = client(tmp_path, positive(path))
    model2.cfg.limits.prompt_char_budget = 20
    result2 = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply,
        model_client=model2,
        budget=ResourceBudget(ledger.root),
    )
    assert result2.outcome == "verifying" and usage2.model_calls == 0


def test_unrelated_actual_answer_with_landed_search_hit_is_not_landed_citation(
    published,
):
    root, path, commit, ledger, record, reply, index = published
    page = BrainRepo(root, snapshot=True).read_page(path)
    answer = resolver.Answer(
        "What is Nimbus rate limit?",
        "nimbus",
        git_facts=[
            dict(
                fact="Other rate",
                value="50 RPM",
                source="wiki/other.md@" + commit,
                evidence_context="Other rate is 50 RPM.",
            )
        ],
        search_hits=[dict(path=path, commit=commit, content=page.body, _context_attested=True)],
    )
    assert verification._delivered_pages(answer, [dict(path=path, revision=commit, body=page.body)]) == []


def test_real_covered_noop_requires_same_generation_and_semantics_without_fake_publication(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    store = JobStore(tmp_path / "jobs")
    state = store.load(record.job_links[-1]["job_id"])
    state.status = "covered"
    state.changes = []
    state.publication = Publication()
    store.save(state)
    covered = _op_result({"job_id": state.job_id}, store)
    model, usage = client(tmp_path, positive(path))
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=covered,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    assert (
        result.outcome == "resolved"
        and result.publication_commit is None
        and result.landed_paths == []
        and usage.model_calls == 2
    )
    saved = ledger.load(record.gap_id)
    assert saved.publication_revision is None
    assert saved.resolution_verification["basis"] == "existing canonical evidence (covered no-op)"
    assert saved.resolution_verification["verified_paths"] == [path]


def test_catalog_existing_need_requires_actual_semantics_not_membership(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    record.job_links = []
    ledger.save(record)
    model, usage = client(tmp_path, positive(path))
    result = verification.verify_existing_gap(
        record.gap_id, ledger, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "resolved" and result.publication_commit is None and usage.model_calls == 2


def test_explicit_units_and_withheld_context_do_not_grant_answered():
    question = "What is Nimbus storage capacity in GiB?"
    answer = resolver.Answer(
        question,
        "nimbus",
        git_facts=[
            dict(
                fact="Storage capacity",
                value="40 GB",
                source="wiki/nimbus.md@abc",
                evidence_context="Storage capacity is 40 GB.",
            )
        ],
    )
    assert "answered" not in resolver._dispositions(answer, lag_flags=[], called={"brain_git": True})
    answer.git_facts[0]["value"] = "40 GiB"
    answer.git_facts[0]["evidence_context"] = "x" * (resolver.EVIDENCE_CHARS + 1)
    assert "answered" not in resolver._dispositions(answer, lag_flags=[], called={"brain_git": True})


def test_canonical_frontmatter_scope_cannot_be_overridden_by_model(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    body = (root / path).read_text().replace("product: nimbus", "product: nimbus\nedition: Enterprise")
    (root / path).write_text(body)
    git(root, "add", path)
    git(root, "commit", "-m", "Explicit edition scope")
    index["last_commit"] = git(root, "rev-parse", "HEAD")
    index["files"][path] = hashlib.sha256((root / path).read_bytes()).hexdigest()
    record.applicability = [{"dimension": "edition", "value": "Personal"}]
    ledger.save(record)
    seen = []
    model, usage = client(tmp_path, positive(path))

    def response(request):
        frame = json.loads(request.content)["messages"][-1]["content"]
        seen.append(json.loads(frame))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(positive(path))},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    # Use the actual client constructor contract rather than patch internals.
    model = ModelClient(model.cfg, usage, transport=httpx.MockTransport(response))
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    assert result.outcome == "verifying" and ledger.load(record.gap_id).status != "resolved"
    assert usage.model_calls == 1 and seen[0]["pages"][0]["page_metadata"]["edition"] == "Enterprise"
    assert seen[0]["pages"][0]["authority"]["source_revision"] == index["last_commit"]


def test_delivered_frame_preserves_canonical_metadata(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    frames = []
    model, usage = client(tmp_path, positive(path))

    def response(request):
        frames.append(json.loads(json.loads(request.content)["messages"][-1]["content"]))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(positive(path))},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    model = ModelClient(model.cfg, usage, transport=httpx.MockTransport(response))
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    assert result.outcome == "resolved" and len(frames) == 2
    assert frames[1]["pages"][0]["page_metadata"] == frames[0]["pages"][0]["page_metadata"]
    assert frames[1]["pages"][0]["authority"] == frames[0]["pages"][0]["authority"]


def test_concurrent_gap_capture_survives_model_verification(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    model, usage = client(tmp_path, positive(path))
    calls = 0

    def response(request):
        nonlocal calls
        calls += 1
        ledger.submit_event(
            event_key="event-during-" + str(calls),
            need_class=record.need_class,
            subject="nimbus",
            catalog_subject=True,
            representative_question=record.representative_question,
            facets=["rate"],
            source_candidates=[{"url": "https://docs.example.invalid/fresh-" + str(calls)}],
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(positive(path))},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    model = ModelClient(model.cfg, usage, transport=httpx.MockTransport(response))
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    saved = ledger.load(record.gap_id)
    assert result.outcome == "resolved" and saved.status == "resolved", result.notes
    assert saved.recurrence == record.recurrence + 2
    assert {c["url"] for c in saved.source_candidates} >= {
        "https://docs.example.invalid/fresh-1",
        "https://docs.example.invalid/fresh-2",
    } and saved.job_links == record.job_links


def test_no_daily_attempt_charge_when_native_hard_budget_already_exhausted(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    model, usage = client(tmp_path, positive(path))
    usage.model_calls = model.max_calls
    budget = ResourceBudget(ledger.root)
    result = verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=model, budget=budget)
    assert result.outcome == "verifying" and usage.model_calls == model.max_calls
    assert budget.load(today())["model_calls"] == 0 and ledger.load(record.gap_id).status != "resolved"


def test_literal_quantity_mention_does_not_establish_missing_quantity(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    text = (
        (root / path)
        .read_text()
        .replace(
            "Nimbus request rate limit is 40 RPM.",
            "Nimbus request rate limits are documented.",
        )
    )
    (root / path).write_text(text)
    git(root, "add", path)
    git(root, "commit", "-m", "Only a rate mention")
    index["last_commit"] = git(root, "rev-parse", "HEAD")
    index["files"][path] = hashlib.sha256((root / path).read_bytes()).hexdigest()
    verdict = positive(path)
    verdict["facets"][0]["evidence"][0]["quote"] = "Nimbus request rate limits are documented."
    model, usage = client(tmp_path, verdict)
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    # The row that cites no quantity is downgraded (a final negative), the reply is not rejected.
    assert result.outcome == "failed" and ledger.load(record.gap_id).status != "resolved" and usage.model_calls == 1


def test_whole_yaml_date_metadata_survives_both_semantic_frames(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    text = (root / path).read_text().replace("product: nimbus", "product: nimbus\nupdated: 2026-10-03")
    (root / path).write_text(text)
    git(root, "add", path)
    git(root, "commit", "-m", "YAML date metadata")
    index["last_commit"] = git(root, "rev-parse", "HEAD")
    index["files"][path] = hashlib.sha256((root / path).read_bytes()).hexdigest()
    frames = []
    model, usage = client(tmp_path, positive(path))

    def response(request):
        frames.append(json.loads(json.loads(request.content)["messages"][-1]["content"]))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(positive(path))},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    model = ModelClient(model.cfg, usage, transport=httpx.MockTransport(response))
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    assert result.outcome == "resolved" and usage.model_calls == 2
    assert [frame["pages"][0]["page_metadata"]["updated"] for frame in frames] == [
        "2026-10-03",
        "2026-10-03",
    ]


def test_worker_actual_ops_published_transition_runs_same_verifier(published, tmp_path, monkeypatch):
    from doclayer import gap_worker
    from doclayer.curator import model as model_module
    from doclayer.curator import transport as transport_module
    from doclayer.curator.jobstore import atomic_write_json
    from doclayer.curator.ops import _op_status
    from doclayer.dispatcher import GapDispatcher

    root, path, commit, ledger, record, reply, index = published
    store = JobStore(tmp_path / "jobs")

    class ActualOpsTransport:
        def call(self, operation, payload, **kwargs):
            assert payload["job_id"] == reply["job"]["job_id"]
            return (_op_status if operation == "status" else _op_result)(payload, store)

    wire = ActualOpsTransport()
    model, usage = client(tmp_path, positive(path))
    monkeypatch.setattr(model_module, "ModelClient", lambda cfg, counters: model)
    monkeypatch.setattr(transport_module, "transport_for", lambda cfg: wire)
    record.status = "curating"
    record.submission = {"state": "accepted", "job_id": reply["job"]["job_id"]}
    ledger.save(record)
    active = ledger.root / "active-submission.json"
    atomic_write_json(
        active,
        dict(
            gap_id=record.gap_id,
            attempt_id="actual-attempt",
            calls=50,
            job_id=reply["job"]["job_id"],
        ),
    )
    notes = []
    dispatcher = GapDispatcher(ledger, transport=wire)
    gap_worker._verify_published(ledger, dispatcher, notes)
    saved = ledger.load(record.gap_id)
    assert saved.status == "resolved" and saved.publication_revision == commit
    assert saved.submission["state"] == "terminal" and not active.exists()
    assert usage.model_calls == 2 and ResourceBudget(ledger.root).load(today())["model_calls"] == 2
    assert saved.job_links == record.job_links


def test_resolve_only_precheck_records_nothing_when_the_page_does_not_answer(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    record.job_links = []
    ledger.save(record)
    before = ledger.load(record.gap_id).status
    assert before != "failed"
    model, usage = client(
        tmp_path,
        dict(
            answers=False,
            facets=[dict(facet="rate", answered=False, scope_preserved=True, evidence=[])],
            reason="Documentation only does not establish a rate",
        ),
    )
    result = verification.verify_existing_gap(
        record.gap_id,
        ledger,
        model_client=model,
        budget=ResourceBudget(ledger.root),
        resolve_only=True,
    )
    assert result.outcome == "failed" and usage.model_calls == 1
    assert ledger.load(record.gap_id).status == before


def test_resolve_only_precheck_records_a_proven_answer(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    record.job_links = []
    ledger.save(record)
    model, usage = client(tmp_path, positive(path))
    result = verification.verify_existing_gap(
        record.gap_id,
        ledger,
        model_client=model,
        budget=ResourceBudget(ledger.root),
        resolve_only=True,
    )
    assert result.outcome == "resolved" and ledger.load(record.gap_id).status == "resolved"


def _partial_nothing_landed(record, tmp_path):
    store = JobStore(tmp_path / "jobs")
    state = store.load(record.job_links[-1]["job_id"])
    state.status = "partial"
    state.changes = []
    state.publication = Publication()
    store.save(state)
    return _op_result({"job_id": state.job_id}, store)


def test_partial_job_that_landed_nothing_is_audited_against_existing_pages(published, tmp_path):
    """job-e04876a6a1 ended partial with nothing merged; awaiting an index that
    would never come kept its gap in awaiting_index forever."""
    root, path, commit, ledger, record, reply, index = published
    model, usage = client(tmp_path, positive(path))
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=_partial_nothing_landed(record, tmp_path),
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    assert result.outcome == "resolved" and result.publication_commit is None and result.landed_paths == []
    assert (
        ledger.load(record.gap_id).resolution_verification["basis"]
        == "existing canonical evidence (partial, nothing published)"
    )


def test_partial_job_that_landed_nothing_with_no_candidate_fails(published, tmp_path, monkeypatch):
    root, path, commit, ledger, record, reply, index = published
    monkeypatch.setattr(verification, "_host_relevant", lambda *a, **k: set())
    monkeypatch.setattr(verification, "_catalog_subject_pages", lambda *a, **k: [])
    model, usage = client(tmp_path, positive(path))
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=_partial_nothing_landed(record, tmp_path),
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    assert result.outcome == "failed" and usage.model_calls == 0
    assert any("nothing published" in note for note in result.notes)
    assert ledger.load(record.gap_id).status == "failed"
