"""A merged publication is judged against every existing page on the subject.

Modeled on gap 0bad22b1 (assumption audit 2026-10-10, lane-c cause F): the question
"What documented operator recovery procedure applies to app-db when /mnt/fast
exhausts free space ...?" was failed three times although an existing page named for
that procedure (wiki/app-db-disk-full-and-recovery-mode.md) holds the answer. An
import that lands a different page must not hide the page that answers.
"""

import hashlib
import json

import httpx
import pytest
from test_verification_contract import git

from doclayer import resolver, verification
from doclayer.curator.config import CuratorConfig
from doclayer.curator.jobstore import JobStore
from doclayer.curator.model import ModelClient
from doclayer.curator.ops import _op_result
from doclayer.curator.state import JobState, PageChange, Publication, Usage
from doclayer.gap_budget import ResourceBudget
from doclayer.gaps import GapLedger

QUESTION = (
    "What documented operator recovery procedure applies to app-db when /mnt/fast "
    "exhausts free space and PostgreSQL enters recovery mode?"
)
ANSWER_PAGE = "wiki/app-db-disk-full-and-recovery-mode.md"
LANDED_PAGE = "wiki/fleet-fast-storage-observations.md"
PROCEDURE = (
    "Recovery procedure for app-db when /mnt/fast is full: free space on /mnt/fast first, "
    "then start app-db and let PostgreSQL finish crash recovery before starting app-coach."
)
PAGES = {
    ANSWER_PAGE: (
        "---\ntitle: app-db /mnt/fast exhaustion and recovery mode\ntags:\n- app-db\n- recovery-mode\n---\n"
        "# app-db: /mnt/fast exhaustion and recovery mode\n\n"
        "## Documented procedure for /mnt/fast exhaustion and recovery mode\n\n" + PROCEDURE + "\n"
    ),
    LANDED_PAGE: (
        "---\ntitle: Fleet fast storage observations\ntags:\n- mnt-fast\n---\n"
        "# Fleet fast storage observations\n\n## /mnt/fast usage\n\n"
        "/mnt/fast is a 750G ext4 disk shared by app-db and Prometheus volumes.\n"
    ),
}


def verdict(path):
    if path is None:
        return dict(
            answers=False,
            facets=[dict(facet="recovery procedure", answered=False, scope_preserved=True, evidence=[])],
            reason="The shown page records storage use, not a recovery procedure.",
        )
    return dict(
        answers=True,
        facets=[
            dict(
                facet="recovery procedure",
                answered=True,
                scope_preserved=True,
                evidence=[dict(path=path, quote=PROCEDURE)],
            )
        ],
        reason="The page states the documented recovery procedure.",
    )


@pytest.fixture
def merged(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "wiki").mkdir(parents=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / ANSWER_PAGE).write_text(PAGES[ANSWER_PAGE])
    git(root, "add", ANSWER_PAGE)
    git(root, "commit", "-m", "Existing recovery page")
    (root / LANDED_PAGE).write_text(PAGES[LANDED_PAGE])
    git(root, "add", LANDED_PAGE)
    git(root, "commit", "-m", "Import lands the storage page")
    commit = git(root, "rev-parse", "HEAD")
    ledger = GapLedger(tmp_path / "gaps")
    record, _ = ledger.submit_event(
        need_class="infrastructure",
        subject="app-db",
        representative_question=QUESTION,
        facets=["recovery procedure"],
    )
    state = JobState(
        job_id="job-0bad22b1aa",
        idempotency_key="idem",
        intent=QUESTION,
        status="published",
        stage="report",
        changes=[PageChange("group", LANDED_PAGE, "create", status="published")],
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
        files={p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in PAGES},
        file_writer_versions={p: "brain-v4-governing-context" for p in PAGES},
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
    return ledger, record, reply, index


def judging_client(tmp_path):
    """A model that answers only when the procedure page is in the frame."""
    cfg = CuratorConfig()
    cfg.credentials_file = str(tmp_path / "no-credentials")
    cfg.inference.retries = 0
    frames = []

    def handler(request):
        frame = request.content.decode()
        frames.append(frame)
        reply = verdict(ANSWER_PAGE if ANSWER_PAGE in frame else None)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]}
        )

    return ModelClient(cfg, Usage(), transport=httpx.MockTransport(handler)), frames


def test_existing_page_that_answers_resolves_a_merged_publication(merged, tmp_path):
    ledger, record, reply, index = merged
    model, frames = judging_client(tmp_path)
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "resolved", result.notes
    saved = ledger.load(record.gap_id)
    assert saved.status == "resolved"
    proof = saved.resolution_verification
    assert proof["basis"] == "merged publication"
    assert proof["landed_paths"] == [LANDED_PAGE]
    assert set(proof["verified_paths"]) == {ANSWER_PAGE, LANDED_PAGE}
    assert frames and ANSWER_PAGE in frames[0]


def test_without_the_existing_pages_the_same_publication_fails(merged, tmp_path, monkeypatch):
    """Control: judging only the landed page reproduces the 0bad22b1 false negative."""
    ledger, record, reply, index = merged
    real = verification._existing_candidates
    monkeypatch.setattr(verification, "_existing_candidates", lambda *a: (real(*a)[0], set()))
    model, frames = judging_client(tmp_path)
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "failed"
    assert ledger.load(record.gap_id).resolution_verification["verified_paths"] == [LANDED_PAGE]


def test_unindexed_existing_page_does_not_hold_the_publication_back(merged, tmp_path):
    """An existing page whose current generation the index has not acknowledged is
    left out of the judgement rather than blocking the landed page's proof."""
    ledger, record, reply, index = merged
    index["files"][ANSWER_PAGE] = "0" * 64
    model, frames = judging_client(tmp_path)
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.index_acknowledged and result.outcome == "failed"
    assert ledger.load(record.gap_id).resolution_verification["verified_paths"] == [LANDED_PAGE]


def test_frame_cap_reads_chars_per_token_from_config():
    cfg = CuratorConfig()
    inference = cfg.inference
    margin = max(1024, inference.serving_context_tokens // 10)
    tokens = inference.serving_context_tokens - inference.effective_output_target - margin
    inference.chars_per_token = 4.0
    cfg.limits.prompt_char_budget = inference.prompt_char_budget  # re-derived, as load_config does
    assert verification._frame_char_cap(cfg) == max(cfg.limits.prompt_char_budget, int(tokens * 4.0))
    del inference.chars_per_token  # a config from before the field existed
    if not hasattr(inference, "chars_per_token"):
        assert verification._frame_char_cap(cfg) == max(cfg.limits.prompt_char_budget, int(tokens * 2.5))
