"""Page evidence is bounded by the editorial review budget and an unfittable
page fails before any model call (67 records, about 84k chars against a
32,768-char review budget, must not be discovered only after 146 calls)."""

import httpx
import pytest
from conftest import run_git

from doclayer.curator import curate, gittools
from doclayer.curator.degrade import GAP_MARK
from doclayer.curator.editorial import EditorialContextOverflow
from doclayer.curator.model import ModelError
from doclayer.curator.pipeline import CAPACITY_GENERATION, Pipeline
from doclayer.curator.state import Evidence, JobState, PageChange, SourceRecord

PAGE = "wiki/existing-model.md"
FILLER = "reasoning effort tokens latency budget model "


def make_job(cfg, store, *, count, assigned, oversized=None):
    """`oversized`: index of one record whose own text cannot fit a review."""
    records = [
        Evidence(
            "manual",
            "Performance modes",
            f"# Fact {number}",
            "quote",
            f"Fact {number}: " + FILLER * 12,
            quote=f"Source sentence {number}: " + FILLER * 4,
        )
        for number in range(count)
    ]
    if oversized is not None:
        records[oversized].text = f"Fact {oversized}: " + FILLER * (cfg.limits.prompt_char_budget // len(FILLER) + 10)
    text = "\n\n".join(record.locator + "\n" + record.text for record in records)
    source = SourceRecord("manual", "text", "Performance modes", status="fetched", extraction_complete=True)
    change = PageChange(
        "g",
        PAGE,
        "update",
        title="Existing model",
        source_ids=["manual"],
        claim_ids=[curate.evidence_id(record) for record in records[:assigned]],
    )
    state = store.create(
        JobState("", "", "Performance modes", sources=[source], changes=[change], limits={"max_model_calls": 200})
    )[0]
    wire = []

    def refuse(request):
        wire.append(request)
        return httpx.Response(500, text="no model call expected")

    pipeline = Pipeline(cfg, store, inference_transport=httpx.MockTransport(refuse))
    pipeline._store_extract(state, source, text)
    store.append_evidence(state.job_id, [record.to_dict() for record in records])
    state.evidence_verification = {
        curate.evidence_id(record): {
            "supported": True,
            "status": "supported",
            "excerpt": record.quote,
            "support_version": curate.SOURCE_SUPPORT_VERSION,
            "source_binding": curate.source_support_binding(record, {"manual": text}),
        }
        for record in records
    }
    for record in records:
        record.support_status = "supported"
    state.base_commit = run_git(cfg.paths.brain_checkout, "rev-parse", "HEAD")
    state.stage = "draft"
    store.save(state)
    return state, records, pipeline, wire


def total_chars(records):
    return sum(len(r.text) + len(r.quote or "") for r in records)


@pytest.mark.asyncio
async def test_a_single_oversized_assigned_claim_fails_before_any_model_call_and_is_named(cfg, store):
    state, records, pipeline, wire = make_job(cfg, store, count=67, assigned=5, oversized=3)
    big = curate.evidence_id(records[3])
    with pytest.raises(ModelError) as caught:
        await pipeline._stage_draft(state)
    error = caught.value
    assert error.transient is False
    assert PAGE in str(error) and big in str(error) and "on its own" in str(error)
    assert error.diagnostic["claim_id"] == big
    assert state.usage.model_calls == 0 and wire == []
    assert state.changes[0].status == "pending" and not state.changes[0].draft_body


@pytest.mark.asyncio
async def test_assigned_claims_exceeding_one_review_together_proceed_to_batched_drafting(cfg, store, monkeypatch):
    """Only a single unfittable claim fails the page.
    67 normal claims that cannot be reviewed together are all drafted (the
    editor batches them; the reviews degrade soundly)."""
    state, records, pipeline, wire = make_job(cfg, store, count=67, assigned=67)
    assert total_chars(records) > cfg.limits.prompt_char_budget
    assert not pipeline._review_fits(state, state.changes[0], records, grow=True)
    captured = []

    class Stop(Exception):
        pass

    async def draft_page(client, intent, change, evidence, *args, **kwargs):
        captured.append(list(evidence))
        raise Stop()

    monkeypatch.setattr(curate, "draft_page", draft_page)
    with pytest.raises(Stop):
        await pipeline._stage_draft(state)
    assert {curate.evidence_id(r) for r in captured[0]} == {curate.evidence_id(r) for r in records}
    assert state.usage.model_calls == 0 and wire == []


@pytest.mark.asyncio
async def test_run_drops_an_unfittable_claim_from_its_page_and_continues(cfg, store):
    # A single claim too large for its page is dropped as a
    # current gap naming its sizes; the job goes on with the other claims (here
    # it then reaches the refusing model wire, an ordinary retryable outage).
    state, records, pipeline, wire = make_job(cfg, store, count=67, assigned=67, oversized=40)
    big = curate.evidence_id(records[40])
    result = await pipeline.run(state)
    assert big not in result.changes[0].claim_ids and len(result.changes[0].claim_ids) == 66
    assert any(
        GAP_MARK in gap and big in gap and "chars" in gap and "context budget" in gap for gap in result.coverage_gaps
    )
    assert result.status == "retryable" and big not in (result.error or "") and wire


class Tripwire:
    async def _reached(self, *args, **kwargs):
        raise AssertionError("first model call reached")

    chat = chat_json = chat_verdicts = chat_bounded_json = _reached


@pytest.mark.asyncio
async def test_draft_receives_assigned_claims_plus_only_what_the_review_can_hold(cfg, store, monkeypatch):
    state, records, pipeline, wire = make_job(cfg, store, count=67, assigned=12)
    assigned = {curate.evidence_id(record) for record in records[:12]}
    captured = []

    class Stop(Exception):
        pass

    async def draft_page(client, intent, change, evidence, *args, **kwargs):
        captured.append(list(evidence))
        raise Stop()

    monkeypatch.setattr(curate, "draft_page", draft_page)
    with pytest.raises(Stop):
        await pipeline._stage_draft(state)
    given = captured[0]
    identities = {curate.evidence_id(record) for record in given}
    assert assigned <= identities and len(given) < 67
    assert len(given) > 12  # optional claims that do fit are still offered
    # The page evidence is held by the actual editorial grounding review.
    change = state.changes[0]
    probe = PageChange(
        change.group_id,
        change.path,
        change.action,
        title=change.title,
        claim_ids=change.claim_ids,
        source_ids=change.source_ids,
        draft_body="# Existing model\n\n## Performance\n\n" + FILLER * 5 + "\n",
        draft_meta={"title": "Existing model"},
    )
    with pytest.raises(AssertionError, match="first model call reached"):
        await curate.check_draft(
            Tripwire(), "Performance modes", probe, given, [], char_budget=cfg.limits.prompt_char_budget
        )
    # The unbounded 67-record set is exactly what overflowed in a real job: the
    # capacity planner still refuses it. (The actual review of this uncited
    # probe text now reports new uncited facts instead of grounding them.)
    assert not pipeline._review_fits(state, change, records, grow=True)
    assert pipeline._review_fits(state, change, given, grow=True)

    class Quiet:
        async def chat_bounded_json(self, system, user, *, char_budget):
            assert len(system) + len(user) <= char_budget
            return {"complete": True, "failures": [], "conflicts": []}

    probe.check_progress = {}
    result = await curate.check_draft(
        Quiet(), "Performance modes", probe, records, [], char_budget=cfg.limits.prompt_char_budget
    )
    assert any(f["detail"].startswith("New fact without a citation") for f in result["failures"])
    assert state.usage.model_calls == 0 and wire == []


@pytest.mark.asyncio
async def test_validate_reviews_the_bounded_page_evidence_not_every_source_record(cfg, store, monkeypatch):
    import doclayer.curator.pipeline as module

    state, records, pipeline, wire = make_job(cfg, store, count=67, assigned=12)
    change = state.changes[0]
    change.status = "drafted"
    change.base_blob = gittools.blob_hash(cfg.paths.brain_checkout, f"{state.base_commit}:{PAGE}")
    change.draft_body = "# Existing model\n\n## Performance\n\n" + FILLER * 5 + "\n"
    state.evidence_selection["drafted:" + PAGE] = {"capacity_generation": CAPACITY_GENERATION}
    state.stage = "validate"
    seen = []

    async def check(client, topic, change, evidence, *args, **kwargs):
        seen.append(list(evidence))
        return {"failures": [], "conflicts": []}

    async def preserved(*args, **kwargs):
        return {"failures": [], "conflicts": []}

    monkeypatch.setattr(curate, "check_draft", check)
    monkeypatch.setattr(Pipeline, "_preserve_existing", preserved)
    monkeypatch.setattr(Pipeline, "_scope_failures", lambda *args: [])
    monkeypatch.setattr(module, "run_deterministic_checks", lambda *args: [])
    await pipeline._stage_validate(state)
    assert change.status == "validated"
    reviewed = {curate.evidence_id(record) for record in seen[0]}
    assert {curate.evidence_id(record) for record in records[:12]} <= reviewed
    assert 12 < len(reviewed) < 67
    assert wire == []


@pytest.mark.asyncio
async def test_resumed_draft_from_before_bounded_evidence_is_sent_back_to_drafting_cheaply(cfg, store, monkeypatch):
    # A stale-generation draft is redrafted inside the
    # job, not failed. Validation discards it without any model call.
    import doclayer.curator.pipeline as module

    state, records, pipeline, wire = make_job(cfg, store, count=67, assigned=12)
    change = state.changes[0]
    change.status = "drafted"
    change.base_blob = gittools.blob_hash(cfg.paths.brain_checkout, f"{state.base_commit}:{PAGE}")
    change.draft_body = "# Existing model\n\n## Performance\n\n" + FILLER * 5 + "\n"
    state.stage = "validate"  # no drafted-generation stamp: a pre-fix draft
    monkeypatch.setattr(Pipeline, "_scope_failures", lambda *args: [])
    monkeypatch.setattr(module, "run_deterministic_checks", lambda *args: [])
    assert await pipeline._stage_validate(state) == "draft"
    assert change.status == "pending" and change.draft_body == "" and change.repair_passes == 0
    assert not change.repair_context and change.error is None
    assert any("redrafted automatically" in note and "67 source records" in note for note in change.notes)
    assert state.usage.model_calls == 0 and wire == []


@pytest.mark.asyncio
async def test_any_unfittable_page_fails_the_job_before_the_first_model_call_of_drafting(cfg, store):
    state, records, pipeline, wire = make_job(cfg, store, count=67, assigned=3)
    # Page one is small but its claims still need source verification, which would
    # be a model call; page two cannot fit at all.
    for record in records[:3]:
        state.evidence_verification.pop(curate.evidence_id(record))
    records[50].text = "Fact 50: " + FILLER * (cfg.limits.prompt_char_budget // len(FILLER) + 10)
    store.append_evidence(state.job_id, [records[50].to_dict()])
    state.changes.append(
        PageChange(
            "g",
            "wiki/other-page.md",
            "update",
            title="Other",
            source_ids=["manual"],
            claim_ids=[curate.evidence_id(record) for record in records],
        )
    )
    store.save(state)
    with pytest.raises(EditorialContextOverflow) as caught:
        await pipeline._stage_draft(state)
    assert caught.value.transient is False and "wiki/other-page.md" in str(caught.value)
    assert curate.evidence_id(records[50]) in str(caught.value)
    assert wire == [] and state.usage.model_calls == 0
    assert [c.status for c in state.changes] == ["pending", "pending"]


@pytest.mark.asyncio
async def test_fail_fast_and_drafting_bound_agree_at_the_boundary(cfg, store):
    state, records, pipeline, wire = make_job(cfg, store, count=67, assigned=1)
    change = state.changes[0]
    evidence = pipeline._evidence(state)
    ids = [curate.evidence_id(record) for record in records]
    together = []
    for count in range(1, 68):
        change.claim_ids = ids[:count]
        pipeline._require_page_capacity(state, change, evidence)  # every claim fits on its own
        drafted = pipeline._page_evidence(state, change, evidence)
        assert set(ids[:count]) <= {curate.evidence_id(r) for r in drafted}  # never less than assigned
        if pipeline._review_fits(
            state, change, [r for r in drafted if curate.evidence_id(r) in ids[:count]], grow=True
        ):
            together.append(count)
            # Assigned claims that fit together: drafting gets what the review holds.
            assert pipeline._review_fits(state, change, drafted, grow=True)
        else:
            # Beyond one review: exactly the assigned claims, batched downstream.
            assert {curate.evidence_id(r) for r in drafted} == set(ids[:count])
    assert together and together == list(range(1, together[-1] + 1)) and 12 < together[-1] < 67
    assert wire == []


@pytest.mark.asyncio
async def test_review_evidence_is_pure_and_repeatable(cfg, store):
    import copy

    state, records, pipeline, wire = make_job(cfg, store, count=67, assigned=12)
    change = state.changes[0]
    change.draft_body = "# Existing model\n\n## Performance\n\n" + FILLER * 5 + "\n"
    state.evidence_selection["drafted:" + PAGE] = {"capacity_generation": CAPACITY_GENERATION}
    evidence = pipeline._evidence(state)  # reads (and snapshots) once, outside the calls under test
    before_state, before_change = copy.deepcopy(state.to_dict()), copy.deepcopy(change.to_dict())
    before_records = [record.to_dict() for record in evidence]
    first = pipeline._review_evidence(state, change, evidence)
    second = pipeline._review_evidence(state, change, evidence)
    assert [curate.evidence_id(r) for r in first] == [curate.evidence_id(r) for r in second]
    assert 12 < len(first) < 67  # the bounded path, not the trivial full set
    assert state.to_dict() == before_state and change.to_dict() == before_change
    assert [record.to_dict() for record in evidence] == before_records
    assert wire == []
    # An unstamped pre-fix draft raises the same way every time, still without mutation.
    del state.evidence_selection["drafted:" + PAGE]
    before_state = copy.deepcopy(state.to_dict())
    for _ in range(2):
        with pytest.raises(EditorialContextOverflow, match="redraft required"):
            pipeline._review_evidence(state, change, evidence)
    assert state.to_dict() == before_state and change.to_dict() == before_change


# --------------------------------------------------------------------------- #
# "Redraft required" pages are
# redrafted automatically from the bounded evidence, then validated.
# --------------------------------------------------------------------------- #


def stale_generation_page(cfg, store):
    """A page with 43 supported records, 10 assigned, a
    pre-stamp draft that resumed into validation and failed."""
    state, records, pipeline, wire = make_job(cfg, store, count=43, assigned=10)
    change = state.changes[0]
    change.status = "failed"
    change.base_blob = gittools.blob_hash(cfg.paths.brain_checkout, f"{state.base_commit}:{PAGE}")
    change.draft_meta = {"title": "Existing model"}
    change.draft_body = "# Existing model\n\n## Performance\n\n" + FILLER * 5 + "\n"
    change.draft_progress = {"editor_version": 4, "body_hash": "old", "completed_task_ids": ["t"]}
    change.check_progress = {"article_review": {"stale": True}}
    change.repair_passes = 1
    change.error = (
        "editorial-context: redraft required: wiki/existing-model.md was drafted before page "
        "evidence was bounded to the review budget"
    )
    state.coverage_gaps.append(
        PAGE + ": editorial review unresolved; drafts, source evidence and obligations retained for follow-up"
    )
    state.stage = "validate"
    store.save(state)
    return state, records, pipeline, wire


def no_model_review(monkeypatch):
    import doclayer.curator.pipeline as module

    async def check(*args, **kwargs):
        return {"failures": [], "conflicts": []}

    async def preserve(*args, **kwargs):
        return {"failures": [], "conflicts": []}

    monkeypatch.setattr(Pipeline, "_scope_failures", lambda *args: [])
    monkeypatch.setattr(module, "run_deterministic_checks", lambda *args: [])
    monkeypatch.setattr(curate, "check_draft", check)
    monkeypatch.setattr(Pipeline, "_preserve_existing", preserve)


@pytest.mark.asyncio
async def test_stale_page_is_redrafted_from_bounded_evidence_stamped_and_validated(cfg, store, monkeypatch):
    state, records, pipeline, wire = stale_generation_page(cfg, store)
    no_model_review(monkeypatch)
    drafted = []

    async def draft_page(client, topic, change, evidence, existing_raw, corrections, **kwargs):
        drafted.append((list(evidence), existing_raw))
        return {"frontmatter": {"title": "Existing model"}, "body": "# Existing model\n\nRedrafted.\n"}

    monkeypatch.setattr(curate, "draft_page", draft_page)
    assert await pipeline._stage_validate(state) == "draft"
    change = state.changes[0]
    assert change.status == "pending" and change.draft_body == "" and change.draft_meta == {}
    assert change.draft_progress == {} and change.check_progress == {} and change.validation == []
    assert not any(PAGE in gap for gap in state.coverage_gaps)
    assert change.repair_passes == 0  # a redraft gets a fresh repair allowance

    assert await pipeline._stage_draft(state) == "validate"
    ((evidence, existing_raw),) = drafted
    assigned = set(change.claim_ids)
    assert assigned <= {curate.evidence_id(record) for record in evidence}
    assert len(evidence) < 43  # bounded to what the review can hold
    assert "Redrafted" not in existing_raw and FILLER * 5 not in existing_raw  # drafted from base, not the stale draft
    assert state.evidence_selection["drafted:" + PAGE] == {
        "capacity_generation": CAPACITY_GENERATION,
        "records": len(evidence),
    }
    assert change.status == "drafted"

    await pipeline._stage_validate(state)
    assert change.status == "validated" and change.error is None
    assert len(drafted) == 1  # stamped now: never redrafted twice
    assert wire == [] and state.usage.model_calls == 0


@pytest.mark.asyncio
async def test_failed_redraft_fails_the_job_like_any_draft(cfg, store, monkeypatch):
    state, _, pipeline, _ = stale_generation_page(cfg, store)
    no_model_review(monkeypatch)

    async def draft_page(*args, **kwargs):
        raise ModelError("draft output invalid", transient=False)

    monkeypatch.setattr(curate, "draft_page", draft_page)
    assert await pipeline._stage_validate(state) == "draft"
    with pytest.raises(ModelError, match="draft output invalid"):
        await pipeline._stage_draft(state)
    assert state.changes[0].status == "pending"


@pytest.mark.asyncio
async def test_stale_page_whose_assigned_claim_cannot_fit_is_isolated_not_redrafted(cfg, store, monkeypatch):
    state, records, pipeline, wire = make_job(cfg, store, count=67, assigned=60, oversized=7)
    change = state.changes[0]
    change.status = "drafted"
    change.base_blob = gittools.blob_hash(cfg.paths.brain_checkout, f"{state.base_commit}:{PAGE}")
    change.draft_body = "# Existing model\n\nOld.\n"
    state.stage = "validate"
    no_model_review(monkeypatch)
    assert await pipeline._stage_validate(state) != "draft"
    assert change.status == "failed" and f"assigned claim {curate.evidence_id(records[7])}" in change.error
    assert change.draft_body == "# Existing model\n\nOld.\n"
    assert wire == []


@pytest.mark.asyncio
async def test_stale_validated_page_reaching_publication_is_redrafted(cfg, store, monkeypatch):
    state, _, pipeline, wire = stale_generation_page(cfg, store)
    no_model_review(monkeypatch)
    state.changes[0].status = "validated"
    state.changes[0].error = None
    state.stage = "publish"
    assert await pipeline._stage_publish(state) == "draft"
    assert state.changes[0].status == "pending" and wire == []


@pytest.mark.asyncio
async def test_eleven_claims_of_14166_chars_proceed_past_the_precheck(cfg, store, monkeypatch):
    """11 assigned claims, 14166 chars of claim text and source
    excerpts, refused by the old whole-set precheck at a 32768-char budget. Its
    canonical page is long (about 28 KB), like the live one."""
    cfg.limits.prompt_char_budget = 32768  # the fixture is sized against this budget
    from pathlib import Path

    page = Path(cfg.paths.brain_checkout) / PAGE
    page.write_text(page.read_text() + "\n## Streaming\n\n" + ("Streaming events arrive in order. " * 280) + "\n")
    run_git(cfg.paths.brain_checkout, "commit", "-qam", "long canonical page")
    state, records, pipeline, wire = make_job(cfg, store, count=11, assigned=11)
    for number, record in enumerate(records):
        record.text = f"Fact {number}: " + ("reasoning effort tokens latency " * 40)[: 900 - len(f"Fact {number}: ")]
        record.quote = f"Source sentence {number}: " + ("x" * 388)[: 388 - len(f"Source sentence {number}: ")]
    excess = total_chars(records) - 14166  # 11 x 1288 = 14168
    records[0].text = records[0].text[: len(records[0].text) - excess]
    change = state.changes[0]
    change.claim_ids = [curate.evidence_id(record) for record in records]
    text = "\n\n".join(record.locator + "\n" + record.text for record in records)
    pipeline._store_extract(state, state.sources[0], text)
    store.append_evidence(state.job_id, [record.to_dict() for record in records])
    state.evidence_verification = {
        curate.evidence_id(record): {
            "supported": True,
            "status": "supported",
            "excerpt": record.quote,
            "support_version": curate.SOURCE_SUPPORT_VERSION,
            "source_binding": curate.source_support_binding(record, {"manual": text}),
        }
        for record in records
    }
    store.save(state)
    assert total_chars(records) == 14166 and cfg.limits.prompt_char_budget == 32768
    assert not pipeline._review_fits(state, change, records, grow=True)  # the old precheck refused this page
    captured = []

    class Stop(Exception):
        pass

    async def draft_page(client, intent, change, evidence, *args, **kwargs):
        captured.append(list(evidence))
        raise Stop()

    monkeypatch.setattr(curate, "draft_page", draft_page)
    with pytest.raises(Stop):  # past the precheck: drafting is reached with every assigned claim
        await pipeline._stage_draft(state)
    assert {curate.evidence_id(r) for r in captured[0]} == set(change.claim_ids)
    assert wire == [] and state.usage.model_calls == 0


@pytest.mark.asyncio
async def test_thirty_claims_from_one_source_are_reviewed_at_validate_not_isolated(cfg, store, monkeypatch):
    """capacity-batch review (Critical): a page of 30 claims from one source
    passes the precheck AND its real editorial reviews complete (grounding
    checks the source's records in chunks); it is not isolated."""
    cfg.limits.prompt_char_budget = 32768  # the fixture is sized against this budget
    import json

    import doclayer.curator.pipeline as module

    state, _, pipeline, wire = make_job(cfg, store, count=30, assigned=30)
    change = state.changes[0]
    pipeline._require_page_capacity(state, change, pipeline._evidence(state))
    change.status = "drafted"
    change.base_blob = gittools.blob_hash(cfg.paths.brain_checkout, f"{state.base_commit}:{PAGE}")
    change.draft_body = "# Existing model\n\n## Performance\n\n" + "".join(
        f"Fact {i} is documented here [source: Performance modes].\n\n" for i in range(30)
    )
    state.evidence_selection["drafted:" + PAGE] = {"capacity_generation": CAPACITY_GENERATION}
    state.stage = "validate"
    store.save(state)
    frames = []

    class Reviewer:
        async def chat_json(self, system, user):
            assert len(system) + len(user) <= cfg.limits.prompt_char_budget
            frames.append(json.loads(user.split("\n", 1)[1]))
            return {"complete": True, "failures": [], "conflicts": []}

    monkeypatch.setattr(Pipeline, "_model", lambda *args: Reviewer())
    monkeypatch.setattr(Pipeline, "_scope_failures", lambda *args: [])
    monkeypatch.setattr(module, "run_deterministic_checks", lambda *args: [])
    await pipeline._stage_validate(state)
    change = store.load(state.job_id).changes[0]
    assert change.status == "validated", (change.status, change.error)
    assert "editorial-context" not in (change.error or "")
    assert any("evidence_chunk" in frame for frame in frames)  # grounded in record chunks
    assert wire == []
