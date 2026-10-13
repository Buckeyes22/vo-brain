"""Actual pipeline stages retain source frontier while publishing bounded work."""

import copy
import json

import httpx
import pytest
from conftest import run_git, valid_meta
from test_source_admission import Wire, fact, proof

from doclayer.curator import curate
from doclayer.curator.ops import _op_continue
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.publish import Publisher
from doclayer.curator.state import JobState, SourceRecord


class PipelineWire(Wire):
    def transport(self):
        ordinary = super().transport()

        def handler(request):
            system, user = [row["content"] for row in json.loads(request.content)["messages"]]
            if user.startswith("CANONICAL CLAIM COVERAGE"):
                self.requests.append((system, user))
                item = json.loads(user.split("Claim JSON:\n")[1].split("\nPage metadata:")[0])
                body = user.split("Canonical passage:\n")[1]
                covered = item["claim"] in body
                reply = {
                    "covered": covered,
                    "consistent": True,
                    "qualifiers": [],
                    "anchor": item["claim"] if covered else "",
                    "reason": "actual scoped passage",
                }
            elif user.startswith("BOUNDED CLAIM PLACEMENT"):
                self.requests.append((system, user))
                rows = json.loads(user.split("Claims JSON:\n")[1])
                reply = {
                    "covered": [],
                    "pages": [
                        {
                            "path": "wiki/extracted.md",
                            "action": "create",
                            "title": "Device",
                            "subject_type": "general",
                            "rationale": "verified source facts",
                            "source_ids": sorted({row["source_id"] for row in rows}),
                            "claim_ids": [row["id"] for row in rows],
                            "related": [],
                        }
                    ],
                }
            else:
                return ordinary.handler(request)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": json.dumps(reply)},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_actual_extract_soft_yield_publishes_known_facts_and_continues_omitted_units(
    cfg, store, checkout, origin, forgejo
):
    cfg.limits.prompt_char_budget = 6000
    cfg.limits.max_model_calls = 16
    source = SourceRecord("manual", "text", "Device manual", status="fetched", role="primary")
    text = "\n\n".join(
        f"# Section {number}\n{fact(number).text}\n" + "Ordinary contextual background. " * 68 for number in range(4)
    )
    state = store.create(JobState("", "", "Device", sources=[source], limits={"max_model_calls": 16}))[0]
    wire = PipelineWire()
    pipeline = Pipeline(
        cfg,
        store,
        publisher=Publisher(cfg, store, forgejo=forgejo),
        inference_transport=wire.transport(),
    )
    pipeline._store_extract(state, source, text)
    state.stage = "extract"
    assert await pipeline._stage_extract(state) == "place"
    assert state.usage.model_calls == 6
    assert source.reviewed_sections and source.omitted_sections and "source-budget" in source.omitted_categories
    assert source.review_unit_count == source.section_count == 4
    assert source.doc_kind == "api-reference" and not source.extraction_complete
    saved = store.load(state.job_id)
    assert saved.sources[0].to_dict() == source.to_dict()
    assert saved.evidence_verification == state.evidence_verification
    await pipeline._stage_place(state)
    assert state.changes
    # Earlier source yield leaves unreviewed original units, even when every
    # already-supported fact fits the first coherent placement.
    assert source.omitted_sections
    records = {curate.evidence_id(record): record for record in pipeline._evidence(state)}
    for change in state.changes:
        change.draft_meta = dict(valid_meta(cid="c-aa998877"), title="Device")
        change.draft_body = "# Device\n" + "\n".join(
            records[identity].text + " [source: Device manual]" for identity in change.claim_ids
        )
        change.status = "drafted"
    await pipeline._stage_validate(state)
    assert all(change.status == "validated" for change in state.changes), [
        change.validation for change in state.changes
    ]
    await pipeline._stage_publish(state)
    assert all(change.status == "published" for change in state.changes)
    assert state.usage.model_calls <= 16
    assert all(
        records[identity].text in run_git(origin, "show", "main:wiki/extracted.md")
        for change in state.changes
        for identity in change.claim_ids
    )
    state.status = "partial"
    state.stage = "report"
    store.save(state)
    parent = copy.deepcopy(state.to_dict())
    child_id = _op_continue({"job_id": state.job_id, "discover": False, "review_omitted": True}, cfg, store)["job_id"]
    assert store.load(state.job_id).to_dict() == parent
    child = store.load(child_id)
    child_pipeline = Pipeline(cfg, store, inference_transport=wire.transport())
    previous = set(child.sources[0].reviewed_sections)
    await child_pipeline._stage_extract(child)
    assert set(child.sources[0].reviewed_sections) > previous
    assert child.usage.model_calls <= 8
    for _ in range(3):
        if not child.sources[0].omitted_sections:
            break
        child.status, child.stage = "partial", "report"
        store.save(child)
        prior = copy.deepcopy(child.to_dict())
        next_id = _op_continue(
            {"job_id": child.job_id, "discover": False, "review_omitted": True},
            cfg,
            store,
        )["job_id"]
        assert store.load(child.job_id).to_dict() == prior
        child = store.load(next_id)
        child_pipeline = Pipeline(cfg, store, inference_transport=wire.transport())
        await child_pipeline._stage_extract(child)
        assert child.usage.model_calls <= 8
    assert not child.sources[0].omitted_sections
    assert {row["text"] for row in store.read_evidence(child.job_id)} == {fact(number).text for number in range(4)}
    assert store.load(state.job_id).to_dict() == parent


@pytest.mark.asyncio
async def test_actual_place_skips_complete_unknown_prefix_and_advances_fresh_ids(cfg, store):
    records = [fact(number) for number in range(140)]
    sources = [
        SourceRecord(
            record.source_id,
            "text",
            record.label,
            status="fetched",
            extraction_complete=True,
        )
        for record in records
    ]
    state = store.create(JobState("", "", "Device", sources=sources, limits={"max_model_calls": 60}))[0]
    wire = PipelineWire()
    pipeline = Pipeline(cfg, store, inference_transport=wire.transport())
    for source, record in zip(sources, records):
        text = "# Manual\n" + record.text
        pipeline._store_extract(state, source, text)
        if int(source.id.split("-")[-1]) < 130:
            state.evidence_verification[curate.evidence_id(record)] = proof(record, text, "unknown")
    store.append_evidence(state.job_id, [record.to_dict() for record in records])
    await pipeline._stage_place(state)
    admission = state.evidence_selection["source_admission"]
    assert admission["selected"] == [curate.evidence_id(record) for record in records[130:]]
    assert len(admission["cached_unknown"]) == 130
    selected = {identity for change in state.changes for identity in change.claim_ids}
    assert selected == {curate.evidence_id(record) for record in records[130:]}
    assert set(admission["cached_unknown"]) <= set(state.evidence_selection["placement"]["remaining"])
    assert not state.evidence_selection["placement"]["ledger"].get("complete", False)
