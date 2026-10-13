"""Actual charged model requests yield source work without hiding backlog."""

import copy
import json
import re

import httpx
import pytest

from doclayer.curator import curate, source_admission
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.state import BudgetExceeded, Evidence, Usage


def proof(record, text, status="supported"):
    return {
        "supported": status == "supported",
        "status": status,
        "support_version": curate.SOURCE_SUPPORT_VERSION,
        "source_binding": curate.source_support_binding(record, {record.source_id: text}),
        "excerpt": record.quote if status == "supported" else "",
    }


def fact(number):
    text = f"Device factual detail {number} is {number + 10}."
    return Evidence(
        f"source-{number}",
        f"Manual {number}",
        "# Manual",
        "quote",
        text,
        quote=text,
        claim_id=f"claim-{number:08d}",
    )


class Wire:
    def __init__(self):
        self.requests = []

    def transport(self):
        def handler(request):
            messages = json.loads(request.content)["messages"]
            system, user = [message["content"] for message in messages]
            self.requests.append((system, user))
            if "Section groups:" in user:
                listing = user.split("Section groups:\n", 1)[1].split("\nReturn", 1)[0]
                reply = {"groups": [int(value) for value in re.findall(r"^(\d+):", listing, re.M)][:3]}
            elif "You extract durable, checkable claims" in system:
                body = user.split("### locator: ", 1)[1].split("\n\nReturn ", 1)[0]
                locator = body.split("\n", 1)[0]
                facts = re.findall(r"Device factual detail \d+ is \d+\.", body)
                reply = {
                    "doc_kind": "api-reference",
                    "claims": [
                        {
                            "claim": value,
                            "quote": value,
                            "locator": locator,
                            "modality": "quote",
                            "important": True,
                            "excluded_topic": False,
                            "applicability": {},
                        }
                        for value in facts
                    ],
                }
            elif user.startswith("SOURCE ANCESTOR"):
                item = json.loads(user.split("\n", 1)[1])
                shown = item["ancestor_context"]
                known = item.get("previous_support_quotes", [])
                assertion = item["claim"]
                positive = assertion in shown or assertion in known
                reply = {
                    "verdicts": [
                        {
                            "id": item["id"],
                            "supported": positive,
                            "consistent": True,
                            "reason": "literal source assertion",
                            "qualifiers": [],
                            "support_quotes": [assertion] if positive else [],
                        }
                    ]
                }
            elif user.startswith("CANONICAL CLAIM COVERAGE"):
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
            elif user.startswith("ARTICLE EDITORIAL REVIEW"):
                frame = json.loads(user.split("\n", 1)[1])
                assert frame["body"] and frame["evidence"]
                reply = {"complete": True, "failures": [], "conflicts": []}
            elif "Verify selected claims" in system:
                items = json.loads(user.split("\n", 1)[1])
                reply = {
                    "verdicts": [
                        {
                            "id": item["id"],
                            "supported": item["claim"] in item["excerpt"],
                            "reason": "whole literal fixture claim",
                        }
                        for item in items
                    ]
                }
            else:
                reply = {"failures": [], "conflicts": []}
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
async def test_137_mixed_records_long_first_source_yields_and_next_invocation_resumes_real_charged_proof(
    cfg, store, checkout, origin, forgejo
):
    known_facts = [fact(number) for number in range(4)]
    long = Evidence(
        "critical",
        "Long manual",
        "# Manual",
        "quote",
        "Device critical factual detail is 99.",
        quote="Device critical factual detail is 99.",
        claim_id="claim-00010000",
    )
    rest = [fact(number) for number in range(4, 136)]
    records = known_facts + [long] + rest
    texts = {record.source_id: "# Manual\n" + record.text for record in records}
    texts[long.source_id] += "\n" + "".join(f"Additional original context {number}.\n" for number in range(900))
    known = {curate.evidence_id(record): proof(record, texts[record.source_id]) for record in known_facts}
    original = copy.deepcopy(known)
    first_usage = Usage()
    wire = Wire()
    client = ModelClient(cfg, first_usage, limits={"max_model_calls": 12}, transport=wire.transport())
    saved = copy.deepcopy(known)
    first = await source_admission.review_sources(
        client,
        "critical Device",
        records,
        texts,
        char_budget=8000,
        known=saved,
        progress=lambda delta: saved.update(copy.deepcopy(delta)),
    )
    assert first["yielded"] and first_usage.model_calls == 4 and len(wire.requests) == 4
    assert first["reserve"] == 8 and len(first["selected"]) == 12
    assert len(first["deferred"]) == 133 and curate.evidence_id(long) in first["deferred"]
    assert saved[curate.evidence_id(long)]["verification_pending"]
    checked = copy.deepcopy(saved[curate.evidence_id(long)]["ancestor_verdicts"])
    assert checked and all(saved[identity] == old for identity, old in original.items())
    # A distinct bounded editorial job validates cached complete source facts.
    # The source counter stays unchanged; the cap16 pipeline test separately
    # proves source review and publication within the same charged job.
    from conftest import run_git, valid_meta

    from doclayer.curator.pipeline import Pipeline
    from doclayer.curator.publish import Publisher
    from doclayer.curator.state import JobState, PageChange, SourceRecord

    state = store.create(
        JobState(
            job_id="",
            idempotency_key="",
            intent="Device",
            base_commit=run_git(checkout, "rev-parse", "HEAD"),
        )
    )[0]
    state.status = "validating"
    state.stage = "validate"
    state.limits = {"max_model_calls": 12}
    pipeline = Pipeline(cfg, store, publisher=Publisher(cfg, store, forgejo=forgejo))
    editorial_usage = state.usage
    editorial = ModelClient(cfg, editorial_usage, limits={"max_model_calls": 12}, transport=wire.transport())
    pipeline._model = lambda current: editorial
    state.sources = [
        SourceRecord(
            record.source_id,
            "text",
            record.label,
            status="fetched",
            extraction_complete=True,
        )
        for record in known_facts
    ]
    for source, record in zip(state.sources, known_facts):
        pipeline._store_extract(state, source, texts[record.source_id])
    store.append_evidence(state.job_id, [record.to_dict() for record in known_facts])
    state.evidence_verification = copy.deepcopy(original)
    published_facts = known_facts[:1]  # bounded coherent group; other known facts remain retained
    body = "# Device\n" + "\n".join(record.text + f" [source: {record.label}]" for record in published_facts)
    state.changes = [
        PageChange(
            "g",
            "wiki/coherent.md",
            "create",
            title="Device",
            status="drafted",
            draft_body=body,
            draft_meta=dict(valid_meta(cid="c-9988aabb"), title="Device"),
            claim_ids=[curate.evidence_id(record) for record in published_facts],
        )
    ]
    store.save(state)
    await pipeline._stage_validate(state)
    assert state.changes[0].status == "validated", (
        state.changes[0].error,
        state.changes[0].validation,
    )
    await pipeline._stage_publish(state)
    assert state.changes[0].status == "published" and state.publication.merged
    assert all(record.text in run_git(origin, "show", "main:wiki/coherent.md") for record in published_facts)
    assert state.usage is editorial.usage and state.usage.model_calls <= 12
    assert first_usage.model_calls == 4
    used_first = first_usage.model_calls
    first_requests = len(wire.requests)
    # A separate source-function invocation has its own scoped counter and
    # carries exact pending proofs without resetting the original counter.
    second_usage = Usage()
    second = ModelClient(cfg, second_usage, limits={"max_model_calls": 12}, transport=wire.transport())
    second_result = await source_admission.review_sources(
        second,
        "critical Device",
        records,
        texts,
        char_budget=8000,
        known=saved,
        progress=lambda delta: saved.update(copy.deepcopy(delta)),
        settled_ids=set(original),
    )
    assert second_usage.model_calls <= 4
    assert saved[curate.evidence_id(long)]["ancestor_verdicts"].keys() >= checked.keys()
    assert first_usage.model_calls == used_first
    assert len(second_result["deferred"]) > 120
    assert not any(user in {old for _, old in wire.requests[:used_first]} for _, user in wire.requests[first_requests:])


@pytest.mark.asyncio
async def test_current_complete_unknown_does_not_starve_fresh_records_and_binding_change_reopens(
    cfg,
):
    records = [fact(number) for number in range(140)]
    texts = {record.source_id: "# Manual\n" + record.text for record in records}
    known = {curate.evidence_id(record): proof(record, texts[record.source_id], "unknown") for record in records[:130]}
    wire = Wire()
    usage = Usage()
    client = ModelClient(cfg, usage, limits={"max_model_calls": 40}, transport=wire.transport())
    result = await source_admission.review_sources(client, "Device", records, texts, char_budget=12000, known=known)
    assert result["selected"] == [curate.evidence_id(record) for record in records[130:]]
    assert len(result["cached_unknown"]) == 130 and len(result["deferred"]) == 130
    texts[records[0].source_id] += "\nCorrected original source context."
    changed = await source_admission.review_sources(client, "Device", records, texts, char_budget=12000, known=known)
    assert curate.evidence_id(records[0]) in changed["selected"]


@pytest.mark.asyncio
async def test_known_pending_12_yields_but_settled_hints_advance_more_than_two_continuations(
    cfg,
):
    records = [fact(number) for number in range(37)]
    texts = {record.source_id: "# Manual\n" + record.text for record in records}
    known = {curate.evidence_id(record): proof(record, texts[record.source_id]) for record in records[:12]}
    wire = Wire()
    client = ModelClient(cfg, Usage(), limits={"max_model_calls": 40}, transport=wire.transport())
    first = await source_admission.review_sources(client, "Device", records, texts, char_budget=12000, known=known)
    assert first["selected"] == [] and not wire.requests
    settled = set(known)
    # Pure source-function invocations: each scoped Usage is independent; this
    # test does not claim a persisted job or reset an owning parent counter.
    for _ in range(3):
        child = ModelClient(cfg, Usage(), limits={"max_model_calls": 40}, transport=wire.transport())
        result = await source_admission.review_sources(
            child,
            "Device",
            records,
            texts,
            char_budget=12000,
            known=known,
            progress=known.update,
            settled_ids=settled,
        )
        assert result["selected"]
        settled.update(identity for identity, value in known.items() if value.get("status") == "supported")
    assert len(settled) == 37 and not result["deferred"]


@pytest.mark.asyncio
async def test_source_outage_and_cancel_propagate_without_becoming_soft_defer(cfg):
    record = fact(0)

    def unavailable(request):
        if request.method == "GET":  # reachability probe: the server answers, only the POST drops
            return httpx.Response(404)
        raise httpx.ConnectError("real inference outage")

    cfg.inference.retries = 0
    client = ModelClient(
        cfg,
        Usage(),
        limits={"max_model_calls": 12},
        transport=httpx.MockTransport(unavailable),
    )
    with pytest.raises(ModelError, match="inference"):
        await source_admission.review_sources(
            client,
            "Device",
            [record],
            {record.source_id: "# Manual\n" + record.text},
            char_budget=12000,
        )
    assert client.usage.model_calls == 1

    def cancelled():
        raise RuntimeError("caller cancellation")

    client = ModelClient(
        cfg,
        Usage(),
        limits={"max_model_calls": 12},
        transport=Wire().transport(),
        before_call=cancelled,
    )
    with pytest.raises(RuntimeError, match="caller cancellation"):
        await source_admission.review_sources(
            client,
            "Device",
            [record],
            {record.source_id: "# Manual\n" + record.text},
            char_budget=12000,
        )
    assert client.usage.model_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed", "transient"])
async def test_reserve_applies_to_actual_schema_repair_and_http_retry_attempts(cfg, failure):
    record = fact(0)
    dispatches = []
    admissions = []

    def handler(request):
        dispatches.append(request)
        if failure == "transient":
            return httpx.Response(429, json={"error": "transient"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "malformed"}}]})

    def callback():
        admissions.append("original")

    usage = Usage(model_calls=3)
    client = ModelClient(
        cfg,
        usage,
        limits={"max_model_calls": 12},
        transport=httpx.MockTransport(handler),
        before_call=callback,
    )
    result = await source_admission.review_sources(
        client,
        "Device",
        [record],
        {record.source_id: "# Manual\n" + record.text},
        char_budget=12000,
    )
    assert result["yielded"] and result["deferred"] == [curate.evidence_id(record)]
    assert usage.model_calls == 4 and len(dispatches) == 1
    assert len(admissions) == 2  # original callback also sees deferred retry admission
    assert client._before_call is callback
    assert result["proofs"][curate.evidence_id(record)]["verification_pending"]


@pytest.mark.asyncio
async def test_initial_extraction_yields_with_durable_units_and_continues_remaining_material(
    cfg, store, checkout, origin, forgejo
):
    from conftest import run_git, valid_meta

    from doclayer.curator.pipeline import Pipeline
    from doclayer.curator.publish import Publisher
    from doclayer.curator.state import JobState, PageChange, SourceRecord

    text = "\n\n".join(
        f"# Section {number}\n{fact(number).text}\n" + "Ordinary contextual background. " * 68 for number in range(4)
    )
    source = SourceRecord(id="manual", kind="text", label="Device manual", role="primary")
    state = store.create(JobState(job_id="", idempotency_key="", intent="Device", sources=[source]))[0]
    pipeline = Pipeline(cfg, store, publisher=Publisher(cfg, store, forgejo=forgejo))
    pipeline._store_extract(state, source, text)
    retained = {}
    proofs = {}
    wire = Wire()
    client = ModelClient(cfg, state.usage, limits={"max_model_calls": 12}, transport=wire.transport())
    original = client._before_call

    def persist(rows):
        for row in rows:
            key = curate.claim_identity(source.id, row["locator"], row["claim"])
            retained[key] = copy.deepcopy(row)
        state.evidence_verification = copy.deepcopy(proofs)
        if rows:
            store.append_evidence(
                state.job_id,
                [
                    Evidence(
                        source.id,
                        source.label,
                        row["locator"],
                        row["modality"],
                        row["claim"],
                        applicability=row["applicability"],
                        doc_kind=source.doc_kind,
                        role=source.role,
                        quote=row["quote"],
                        original_quote=row["original_quote"],
                    ).to_dict()
                    for row in rows
                ],
            )
        store.save(state)

    def verified(rows):
        proofs.update(copy.deepcopy(rows))
        persist([])

    with pytest.raises(source_admission.SourceAdmissionDeferred):
        with source_admission.source_budget(client) as admitted:
            await curate.extract_claims(
                admitted,
                source,
                text,
                "Device",
                [],
                char_budget=6000,
                progress=persist,
                verification_progress=verified,
            )
    persist([])
    assert state.usage.model_calls == 4 and client._before_call is original
    assert source.omitted_sections
    assert source.review_unit_count == 4 and source.section_count == 4
    assert source.doc_kind == "api-reference"
    assert proofs and any(value.get("verification_pending") for value in proofs.values())
    assert len(retained) < 4
    saved = store.load(state.job_id)
    parent_snapshot = saved.to_dict()
    assert saved.usage.model_calls == 4
    assert pipeline._extract_text(saved, saved.sources[0]) == text
    assert saved.sources[0].reviewed_sections == source.reviewed_sections
    assert saved.sources[0].omitted_sections == source.omitted_sections
    previous_reviewed = set(source.reviewed_sections)
    child_usages = []
    # Each bounded continuation owns a distinct state and its charged Usage.
    # The persistence closures follow that owning state, never the parent.
    for _ in range(8):
        if not source.omitted_sections:
            break
        state = store.create(
            JobState(
                job_id="",
                idempotency_key="",
                intent="Device",
                continuation_of=state.job_id,
                sources=[copy.deepcopy(source)],
                evidence_verification=copy.deepcopy(proofs),
                limits={"max_model_calls": 12},
            )
        )[0]
        source = state.sources[0]
        pipeline._store_extract(state, source, text)
        source.relevance["review_candidates"] = list(source.omitted_sections)
        child_usages.append(state.usage)
        child = ModelClient(cfg, state.usage, limits=state.limits, transport=wire.transport())
        try:
            with source_admission.source_budget(child) as admitted:
                await curate.extract_claims(
                    admitted,
                    source,
                    text,
                    "Device",
                    [],
                    char_budget=6000,
                    progress=persist,
                    verification_progress=verified,
                    verification_cache=proofs,
                )
        except source_admission.SourceAdmissionDeferred:
            pass
        persist([])
        assert store.load(state.job_id).usage.model_calls == child.usage.model_calls
        assert pipeline._extract_text(state, source) == text
    assert set(source.reviewed_sections) > previous_reviewed and not source.omitted_sections
    assert len(retained) == 4 and all(value.get("supported") for value in proofs.values())
    assert all(usage.model_calls <= 4 for usage in child_usages)
    assert store.load(saved.job_id).to_dict() == parent_snapshot
    source_job = store.load(state.job_id)
    source_snapshot = source_job.to_dict()
    # Publication is a separate linked editorial job with its own matching
    # state/client Usage; all source-job counters and checkpoints stay immutable.
    state = store.create(
        JobState(
            job_id="",
            idempotency_key="",
            intent="Device",
            continuation_of=source_job.job_id,
            sources=[copy.deepcopy(source)],
            evidence_verification=copy.deepcopy(proofs),
            limits={"max_model_calls": 12},
        )
    )[0]
    source = state.sources[0]
    store.append_evidence(
        state.job_id,
        [
            Evidence(
                source.id,
                source.label,
                row["locator"],
                row["modality"],
                row["claim"],
                applicability=row["applicability"],
                doc_kind=source.doc_kind,
                role=source.role,
                quote=row["quote"],
                original_quote=row["original_quote"],
            ).to_dict()
            for row in retained.values()
        ],
    )
    # All four complete source facts are checked and published through Git.
    pipeline = Pipeline(cfg, store, publisher=Publisher(cfg, store, forgejo=forgejo))
    editorial_usage = state.usage
    editorial = ModelClient(cfg, editorial_usage, limits={"max_model_calls": 12}, transport=wire.transport())
    pipeline._model = lambda current: editorial
    state.base_commit = run_git(checkout, "rev-parse", "HEAD")
    state.limits = {"max_model_calls": 12}
    state.evidence_verification = copy.deepcopy(proofs)
    pipeline._store_extract(state, source, text)
    supported = [identity for identity, value in proofs.items() if value.get("supported")]
    body = "# Device\n" + "\n".join(retained[identity]["claim"] + " [source: Device manual]" for identity in supported)
    state.changes = [
        PageChange(
            "g",
            "wiki/extracted.md",
            "create",
            title="Device",
            status="drafted",
            draft_body=body,
            draft_meta=dict(valid_meta(cid="c-99bb88aa"), title="Device"),
            claim_ids=supported,
            source_ids=[source.id],
        )
    ]
    await pipeline._stage_validate(state)
    assert state.changes[0].status == "validated", (
        state.changes[0].error,
        state.changes[0].validation,
    )
    await pipeline._stage_publish(state)
    assert state.changes[0].status == "published"
    assert all(
        retained[identity]["claim"] in run_git(origin, "show", "main:wiki/extracted.md") for identity in supported
    )
    assert state.usage.model_calls <= 12
    assert state.usage is editorial.usage
    assert store.load(saved.job_id).to_dict() == parent_snapshot
    assert store.load(source_job.job_id).to_dict() == source_snapshot


@pytest.mark.asyncio
async def test_source_budget_preserves_hard_exhaustion_and_cancel_at_soft_threshold(
    cfg,
):
    client = ModelClient(
        cfg,
        Usage(model_calls=12),
        limits={"max_model_calls": 12},
        transport=Wire().transport(),
    )
    with pytest.raises(BudgetExceeded):
        with source_admission.source_budget(client) as admitted:
            await admitted.chat_json("system", "user")
    assert client.usage.model_calls == 12

    def cancelled():
        raise RuntimeError("actual cancellation at soft threshold")

    client = ModelClient(
        cfg,
        Usage(model_calls=6),
        limits={"max_model_calls": 12},
        transport=Wire().transport(),
        before_call=cancelled,
    )
    with pytest.raises(RuntimeError, match="actual cancellation"):
        with source_admission.source_budget(client) as admitted:
            await admitted.chat_json("system", "user")
    assert client._before_call is cancelled and client.usage.model_calls == 6
