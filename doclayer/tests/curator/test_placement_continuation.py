"""Real placement/continue boundaries advance every required source fact."""

import copy

import pytest
from conftest import run_git, valid_meta
from test_placement import Planner

from doclayer import frontmatter
from doclayer.curator import curate, placement
from doclayer.curator.ops import _op_continue
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import Evidence, JobState, SourceRecord


@pytest.mark.asyncio
@pytest.mark.parametrize("count,crowded", [(137, False), (17, True)])
async def test_required_claims_continue_into_actual_canonical_pages_without_parent_mutation(
    cfg, store, checkout, monkeypatch, count, crowded
):
    if crowded:
        for number in range(15):
            (checkout / f"wiki/broad-{number:02d}.md").write_text(
                frontmatter.build(
                    dict(
                        valid_meta(cid=f"c-{number:08x}"),
                        title=f"Broad reference {number}",
                        tags=["broad"],
                    ),
                    f"# Broad reference {number}\nUnrelated material.\n",
                )
            )
        run_git(checkout, "add", "wiki")
        run_git(checkout, "commit", "-qm", "fifteen unrelated broad-ranked pages")
        run_git(checkout, "push", "origin", "main")
    # Source facts are independently grounded fixtures, with the SAME current
    # verifier binding the production admission path requires. Continuations
    # reuse only those source-bound proofs, never raw editorial status flags.
    records = [
        Evidence(
            "manual",
            "Device manual",
            f"## Fact {i}",
            "quote",
            f"Device qualified fact {i} is {i + 100}.",
            quote=f"Device qualified fact {i} is {i + 100}.",
        )
        for i in range(count)
    ]
    source_text = "# Device manual\n" + "\n".join(record.text for record in records)
    state = store.create(
        JobState(
            job_id="",
            idempotency_key="",
            intent="Broad material" if crowded else "Device qualified facts",
        )
    )[0]
    source = SourceRecord("manual", "text", "Device manual", status="fetched", extraction_complete=True)
    state.sources = [source]
    state.stage = "place"
    state.status = "processing"
    pipeline = Pipeline(cfg, store)
    pipeline._store_extract(state, source, source_text)
    store.append_evidence(state.job_id, [record.to_dict() for record in records])
    for record in records:
        state.evidence_verification[curate.evidence_id(record)] = {
            "supported": True,
            "status": "supported",
            "excerpt": record.quote,
            "support_version": curate.SOURCE_SUPPORT_VERSION,
            "source_binding": curate.source_support_binding(record, {"manual": source_text}),
        }
    store.save(state)
    model = Planner()
    monkeypatch.setattr(Pipeline, "_model", lambda *args: model)
    published_ids = []
    canonical_facts = []
    no_sources_rechecked = []
    original_verify = curate.verify_source_claims

    async def verified(*args, **kwargs):
        no_sources_rechecked.append(args[1])
        return await original_verify(*args, **kwargs)

    monkeypatch.setattr(curate, "verify_source_claims", verified)
    actual_candidates = []
    original_plan = placement.plan_batch

    async def capture_candidates(*args, **kwargs):
        actual_candidates.append({path for path, _meta, _body in args[4]})
        return await original_plan(*args, **kwargs)

    monkeypatch.setattr(placement, "plan_batch", capture_candidates)
    for pass_number in range(20):
        if crowded and pass_number:
            candidates = pipeline._candidate_pages(state, checkout, run_git(checkout, "rev-parse", "origin/main"))
            assert "wiki/device.md" not in {path for path, _meta, _body in candidates}
            assert len(candidates) == 14
        await pipeline._stage_place(state)
        if crowded and pass_number:
            assert "wiki/device.md" in actual_candidates[-1]
            assert len(actual_candidates[-1]) >= 15
        changes = state.changes
        ids = [identity for change in changes for identity in change.claim_ids]
        assert len(ids) <= 12 and not (set(ids) & set(published_ids))
        assert ids, "continuation must advance beyond previously published batch"
        published_ids.extend(ids)
        canonical_facts.extend(record.text for record in records if curate.evidence_id(record) in ids)
        # Publish the actual required facts into this real Git canonical remote.
        # The continuation then verifies these bytes through canonical coverage,
        # rather than trusting the parent's status or a page-level boolean.
        target = checkout / "wiki/device.md"
        target.write_text(
            frontmatter.build(
                dict(valid_meta(cid="c-99aa0099"), title="Device"),
                "# Device\n" + "\n".join(canonical_facts) + "\n",
            )
        )
        run_git(checkout, "add", str(target))
        run_git(checkout, "commit", "-qm", f"publish required fact batch {pass_number}")
        run_git(checkout, "push", "origin", "main")
        for change in changes:
            change.status = "published"
        state.status = "partial"
        state.stage = "report"
        store.save(state)
        if len(published_ids) == count:
            break
        parent_before = copy.deepcopy(store.load(state.job_id).to_dict())
        result = _op_continue(
            {"job_id": state.job_id, "discover": False, "review_omitted": False},
            cfg,
            store,
        )
        assert store.load(state.job_id).to_dict() == parent_before
        child = store.load(result["job_id"])
        assert not child.changes and child.evidence_verification == state.evidence_verification
        assert child.evidence_selection["placement"]["ledger"]["claims"]
        state = child
        state.status = "processing"
        state.stage = "place"
        pipeline = Pipeline(cfg, store)
    assert len(published_ids) == len(set(published_ids)) == count
    assert set(published_ids) == {curate.evidence_id(record) for record in records}
    assert all(record.text in target.read_text() for record in records)
    assert not no_sources_rechecked, "matching bound current source proofs must survive continuation"
    assert any(call.startswith("CANONICAL CLAIM COVERAGE") for call in model.calls)


@pytest.mark.asyncio
async def test_canonical_noop_with_pending_source_is_partial_and_not_input_deduplicated(
    cfg, store, checkout, monkeypatch
):
    fact = "Device qualified fact 0 is 100."
    record = Evidence("manual", "manual", "# Manual", "quote", fact, quote=fact)
    unknown = Evidence(
        "unavailable",
        "missing manual",
        "# Manual",
        "quote",
        "Device qualified fact 1 is 101.",
        quote="Device qualified fact 1 is 101.",
    )
    source_text = "# Manual\n" + fact
    target = checkout / "wiki/device.md"
    target.write_text(
        frontmatter.build(
            dict(valid_meta(cid="c-88ff0088"), title="Device"),
            "# Device\n" + fact + "\n",
        )
    )
    run_git(checkout, "add", str(target))
    run_git(checkout, "commit", "-qm", "actual canonical no-op claim")
    run_git(checkout, "push", "origin", "main")
    state = store.create(JobState(job_id="", idempotency_key="", intent="Device", stage="place"))[0]
    state.input_digest = "same-pending-material-input"
    state.sources = [
        SourceRecord("manual", "text", "manual", status="fetched", extraction_complete=True),
        SourceRecord(
            "unavailable",
            "text",
            "missing manual",
            status="fetched",
            extraction_complete=True,
        ),
    ]
    store.append_evidence(state.job_id, [record.to_dict(), unknown.to_dict()])
    pipeline = Pipeline(cfg, store)
    pipeline._store_extract(state, state.sources[0], source_text)
    state.evidence_verification[curate.evidence_id(record)] = {
        "supported": True,
        "status": "supported",
        "excerpt": fact,
        "support_version": curate.SOURCE_SUPPORT_VERSION,
        "source_binding": curate.source_support_binding(record, {"manual": source_text}),
    }
    store.save(state)
    from doclayer.curator.state import Usage

    planner = Planner(covered=True)
    planner.usage = Usage()
    planner.max_calls = cfg.limits.max_model_calls
    monkeypatch.setattr(Pipeline, "_model", lambda *args: planner)
    done = await pipeline.run(state)
    assert done.status == "partial" and done.effective_coverage == "incomplete"
    assert not done.changes and not done.publication.merged
    selection = done.evidence_selection["placement"]
    assert curate.evidence_id(record) in selection["covered"]
    assert curate.evidence_id(unknown) in selection["remaining"]
    assert selection["ledger"]["claims"][curate.evidence_id(unknown)]["status"] == "pending"
    assert store.find_published_input(done.input_digest) is None
    assert done.evidence_verification[curate.evidence_id(unknown)]["status"] == "unknown"
