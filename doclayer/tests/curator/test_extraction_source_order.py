"""Declared primary seeds precede hash order; budget yields retain all seeds."""

import copy

import pytest
from test_source_admission import Wire, fact

from doclayer.curator.coverage import assess_coverage
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import JobState, SourceRecord


@pytest.mark.asyncio
async def test_real_extraction_keeps_declared_primary_order_before_other_sources(cfg, store):
    cfg.limits.prompt_char_budget = 6000
    cfg.limits.max_model_calls = 24
    sources = [
        SourceRecord("b-supplement", "text", "Supplement", role="supplementary", status="fetched"),
        SourceRecord("z-first-primary", "text", "First declared seed", status="fetched"),
        SourceRecord("c-discovered", "text", "Discovered", submitted=False, status="fetched"),
        SourceRecord("a-second-primary", "text", "Second declared seed", status="fetched"),
    ]
    state = store.create(JobState("", "", "Device", sources=sources, limits={"max_model_calls": 24}))[0]
    wire = Wire()
    pipeline = Pipeline(cfg, store, inference_transport=wire.transport())
    for i, source in enumerate(sources):
        pipeline._store_extract(state, source, "# Manual\n" + fact(i).text)
    assert await pipeline._stage_extract(state) == "place"
    requests = [user for system, user in wire.requests if "You extract durable, checkable claims" in system]
    assert [
        next(source.label for source in sources if f"Source: {source.label} (kind:" in user) for user in requests
    ] == ["First declared seed", "Second declared seed", "Supplement", "Discovered"]
    assert all(source.extraction_complete for source in sources)
    assert len(wire.requests) == state.usage.model_calls == 8
    assert {row["source_id"] for row in store.read_evidence(state.job_id)} == {source.id for source in sources}


@pytest.mark.asyncio
async def test_soft_budget_yield_maps_unvisited_primary_and_supplement_frontier_durably(cfg, store):
    cfg.limits.prompt_char_budget = 6000
    cfg.limits.max_model_calls = 16
    sources = [
        SourceRecord("z-first", "text", "First declared long seed", status="fetched"),
        SourceRecord("a-second", "text", "Later declared primary", status="fetched"),
        SourceRecord("b-extra", "text", "Supplement", role="supplementary", status="fetched"),
        SourceRecord(
            "c-excluded",
            "url",
            "Excluded",
            url="https://excluded.invalid/manual",
            status="fetched",
        ),
        SourceRecord("d-missing", "text", "Uncached primary", status="fetched"),
    ]
    state = store.create(
        JobState(
            "",
            "",
            "Device",
            sources=sources,
            limits={"max_model_calls": 16},
            source_policy={"exclude": [{"domain": "excluded.invalid"}]},
        )
    )[0]
    wire = Wire()
    pipeline = Pipeline(cfg, store, inference_transport=wire.transport())
    long = "\n\n".join(f"# Section {i}\n{fact(i).text}\n" + "Ordinary contextual background. " * 68 for i in range(4))
    for source, text in zip(
        sources,
        [
            long,
            "# Manual\n" + fact(10).text,
            "# Manual\n" + fact(11).text,
            "# Manual\n" + fact(12).text,
        ],
    ):
        pipeline._store_extract(state, source, text)
    assert await pipeline._stage_extract(state) == "place"
    assert state.usage.model_calls == len(wire.requests) == 6
    assert all(
        "Source: First declared long seed (kind:" in user
        for system, user in wire.requests
        if "You extract durable, checkable claims" in system
    )
    for source in sources[1:3]:
        assert not source.extraction_complete and source.reviewed_sections == []
        assert source.review_unit_count == source.section_count == 1
        assert source.omitted_sections and "source-budget" in source.omitted_categories
    assert "source-budget" not in sources[3].omitted_categories
    assert sources[4].section_count == sources[4].review_unit_count == 0
    assert "cached-extract" in sources[4].omitted_categories
    assert "source-budget" in sources[4].omitted_categories
    saved = store.load(state.job_id)
    assert [source.to_dict() for source in saved.sources] == [source.to_dict() for source in sources]
    old_omissions = copy.deepcopy([source.to_dict() for source in sources[:3] + sources[4:]])
    gaps = assess_coverage(saved).current_gaps
    assert any("Later declared primary" in gap and "source-budget" in gap for gap in gaps)
    assert any("Supplement" in gap and "source-budget" in gap for gap in gaps)
    assert any("Uncached primary" in gap and "cached-extract" in gap for gap in gaps)
    # An unchanged source-stage restart cannot spend another source call or
    # duplicate omission records/gaps when the source allowance is exhausted.
    resumed = Pipeline(cfg, store, inference_transport=wire.transport())
    await resumed._stage_extract(saved)
    assert saved.usage.model_calls == len(wire.requests) == 6
    assert [source.to_dict() for source in saved.sources[:3] + saved.sources[4:]] == old_omissions
    assert assess_coverage(saved).current_gaps == gaps
