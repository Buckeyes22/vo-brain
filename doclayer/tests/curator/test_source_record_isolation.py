"""One exhausted source quotation is unresolved, never sibling fact authority."""

import copy
import json

import httpx
import pytest
from conftest import run_git, valid_meta
from test_source_admission_pipeline import PipelineWire

from doclayer.curator import curate
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.pipeline import CancelledByRequest, Pipeline
from doclayer.curator.publish import Publisher
from doclayer.curator.state import (
    BudgetExceeded,
    Evidence,
    JobState,
    SourceRecord,
    Usage,
)

CONDITION = "Only laboratory installations support these facts."
BAD = "Device factual detail zero is 10 in laboratory installations."
GOOD = "Device factual detail one is 11 in laboratory installations."
TEXT = "# Manual\n" + CONDITION + "\n" + "Preparation guidance.\n" * 170 + "## Device\n" + BAD + "\n" + GOOD + "\n"


def records():
    return [Evidence("manual", "Device manual", "Device", "quote", value, quote=value) for value in (BAD, GOOD)]


class IsolationWire(PipelineWire):
    def transport(self):
        ordinary = super().transport()

        def handler(request):
            system, user = [row["content"] for row in json.loads(request.content)["messages"]]
            if user.startswith("SOURCE "):
                frame = json.loads(user.split("\n", 1)[1])
                self.requests.append((system, user))
                if user.startswith("SOURCE LITERAL QUOTATION REPAIR"):
                    value = {
                        "id": frame["id"],
                        "edits": [
                            {
                                "field": item["field"],
                                "index": item["index"],
                                "replacement": CONDITION,
                            }
                            for item in frame["invalid_quotation_fields"]
                        ],
                    }
                elif user.startswith("SOURCE REPAIRED QUOTATION SEMANTIC CHECK"):
                    value = dict(
                        id=frame["id"],
                        supported=True,
                        consistent=True,
                        reason="Untrusted attempted positive.",
                        retained_quote_ids=frame["required_retained_quote_ids"],
                        additional_qualifiers=["This invented qualification is absent."],
                        additional_support_quotes=[],
                    )
                else:
                    assert user.startswith("SOURCE ANCESTOR CONSTRAINT CHECK")
                    actual = frame["ancestor_context"]
                    if frame["claim"] == BAD:
                        value = dict(
                            id=frame["id"],
                            supported=True,
                            consistent=True,
                            reason="Untrusted bad quotation.",
                            qualifiers=["Invented laboratory label"],
                            support_quotes=[CONDITION],
                        )
                    else:
                        assert frame["claim"] == GOOD
                        value = dict(
                            id=frame["id"],
                            supported=GOOD in actual,
                            consistent=True,
                            reason="Exact qualified fact.",
                            qualifiers=[CONDITION] if CONDITION in actual else [],
                            support_quotes=[GOOD] if GOOD in actual else [],
                        )
                reply = {"verdicts": [value]}
            elif "You extract durable, checkable claims" in system:
                self.requests.append((system, user))
                reply = {
                    "doc_kind": "api-reference",
                    "claims": [
                        dict(
                            claim=value,
                            quote=value,
                            locator="Device",
                            modality="quote",
                            important=True,
                            excluded_topic=False,
                            applicability={},
                        )
                        for value in (BAD, GOOD)
                    ],
                }
            elif "You write one wiki page" in system:
                self.requests.append((system, user))
                assert GOOD in user and BAD not in user
                reply = {
                    "frontmatter": dict(valid_meta(cid="c-aa998877"), title="Device"),
                    "body": "# Device\n\n" + GOOD + " [source: Device manual]\n",
                    "notes": [],
                }
            elif user.startswith("ARTICLE EDITORIAL REVIEW"):
                frame = json.loads(user.split("\n", 1)[1])
                assert BAD not in frame["body"] and GOOD in frame["body"]
                return ordinary.handler(request)
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
async def test_real_charged_invalid_record_does_not_block_independent_git_publication(cfg, store, origin, forgejo):
    source = SourceRecord("manual", "text", "Device manual", status="fetched", role="primary")
    state = store.create(
        JobState(
            "",
            "",
            "Device laboratory facts",
            sources=[source],
            limits={"max_model_calls": 50},
        )
    )[0]
    wire = IsolationWire()
    pipeline = Pipeline(
        cfg,
        store,
        publisher=Publisher(cfg, store, forgejo=forgejo),
        inference_transport=wire.transport(),
    )
    pipeline._store_extract(state, source, TEXT)
    state.stage = "extract"
    await pipeline._stage_extract(state)
    bad, good = [curate.evidence_id(record) for record in records()]
    rejected = state.evidence_verification[bad]
    assert rejected["status"] == "unknown" and rejected["supported"] is False
    assert rejected["verification_pending"] is False and rejected["excerpt"] == ""
    assert rejected["review_error"]["diagnostic"]["operation"] == "source-semantic-recheck"
    assert rejected["review_error"]["diagnostic"]["attempts"] == 2
    assert rejected["pending_quote_repairs"]  # Original/provisional rows remain audit data.
    assert state.evidence_verification[good]["status"] == "supported"
    assert any(bad in gap for gap in state.coverage_gaps)
    await pipeline._stage_place(state)
    assert all(change.claim_ids == [good] for change in state.changes)
    await pipeline._stage_draft(state)
    await pipeline._stage_validate(state)
    await pipeline._stage_publish(state)
    landed = run_git(origin, "show", "main:wiki/extracted.md")
    assert GOOD in landed and BAD not in landed
    assert all(change.status == "published" for change in state.changes)
    assert state.usage.model_calls <= 50
    assert store.load(state.job_id).evidence_verification[bad] == rejected


@pytest.mark.asyncio
async def test_store_restart_reuses_unresolved_record_without_replaying_or_losing_history(cfg, store):
    state = store.create(JobState("", "", "Device laboratory facts", limits={"max_model_calls": 50}))[0]
    wire = IsolationWire()
    client = ModelClient(cfg, state.usage, limits=state.limits, transport=wire.transport())
    bad, good = [curate.evidence_id(record) for record in records()]

    def interrupt(delta):
        state.evidence_verification.update(copy.deepcopy(delta))
        store.save(state)
        if delta.get(bad, {}).get("review_error"):
            raise RuntimeError("power loss after durable unresolved record")

    with pytest.raises(RuntimeError, match="power loss"):
        await curate.verify_source_claims(client, records(), {"manual": TEXT}, char_budget=32768, progress=interrupt)
    saved = store.load(state.job_id)
    original = copy.deepcopy(saved.evidence_verification[bad])
    charged = saved.usage.model_calls
    assert charged == 4
    resumed = ModelClient(cfg, saved.usage, limits=saved.limits, transport=wire.transport())

    def retain(delta):
        saved.evidence_verification.update(copy.deepcopy(delta))
        store.save(saved)

    result = await curate.verify_source_claims(
        resumed,
        records(),
        {"manual": TEXT},
        char_budget=32768,
        known=saved.evidence_verification,
        progress=retain,
    )
    assert result[bad] == original and result[good]["status"] == "supported"
    assert saved.usage.model_calls == charged + 2
    count = len(wire.requests)
    await curate.verify_source_claims(
        resumed,
        records(),
        {"manual": TEXT},
        char_budget=32768,
        known=saved.evidence_verification,
        progress=retain,
    )
    assert len(wire.requests) == count
    assert saved.evidence_verification[bad] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("char_budget", [6000, 32768])
async def test_changed_source_binding_rechecks_unresolved_record_without_rewriting_prior_proof(cfg, char_budget):
    wire = IsolationWire()
    usage = Usage()
    client = ModelClient(cfg, usage, limits={"max_model_calls": 50}, transport=wire.transport())
    record = records()[0]
    identity = curate.evidence_id(record)
    original = await curate.verify_source_claims(client, [record], {"manual": TEXT}, char_budget=char_budget)
    old = copy.deepcopy(original)
    assert original[identity]["status"] == "unknown"
    charged = usage.model_calls
    resumed = await curate.verify_source_claims(
        client, [record], {"manual": TEXT}, char_budget=char_budget, known=original
    )
    assert resumed == original and usage.model_calls == charged
    changed = await curate.verify_source_claims(
        client,
        [record],
        {"manual": TEXT + "\nCurrent source revision.\n"},
        char_budget=char_budget,
        known=original,
    )
    assert usage.model_calls > charged
    assert changed[identity]["source_binding"] != original[identity]["source_binding"]
    assert changed[identity]["status"] == "unknown" and changed[identity]["supported"] is False
    assert original == old
    for system, user in wire.requests:
        assert len(system) + len(user) <= char_budget


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["outage", "budget", "cancel"])
async def test_operational_interruptions_are_not_isolated_as_record_schema(cfg, stop):
    usage = Usage()
    wire = IsolationWire()
    calls = 0

    def admit():
        nonlocal calls
        calls += 1
        if stop == "cancel" and calls == 2:
            raise CancelledByRequest("operator cancel")

    transport = wire.transport()
    if stop == "outage":
        transport = httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable"))
    client = ModelClient(
        cfg,
        usage,
        limits={"max_model_calls": 2 if stop == "budget" else 50},
        transport=transport,
        before_call=admit,
    )
    proofs = {}
    expected = {
        "outage": ModelError,
        "budget": BudgetExceeded,
        "cancel": CancelledByRequest,
    }[stop]
    with pytest.raises(expected):
        await curate.verify_source_claims(
            client,
            records(),
            {"manual": TEXT},
            char_budget=32768,
            progress=lambda delta: proofs.update(copy.deepcopy(delta)),
        )
    assert usage.model_calls > 0
    assert all(not proof.get("review_error") for proof in proofs.values())
    assert all(proof.get("verification_pending") for proof in proofs.values())
