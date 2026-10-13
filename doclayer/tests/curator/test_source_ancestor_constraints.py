"""Complete governing context, minimal durable support, bounded source checks."""

import json

import pytest

from doclayer.curator import curate
from doclayer.curator.model import ModelError
from doclayer.curator.state import Evidence, JobState, SourceRecord


class Constraints:
    def __init__(self, fail_after=None):
        self.calls = 0
        self.fail_after = fail_after
        self.requests = []

    async def chat_json(self, system, user):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ModelError("inference outage")
        assert len(system) + len(user) <= 12000
        if user.startswith("SOURCE ANCESTOR CONSTRAINT CHECK"):
            item = json.loads(user.split("\n", 1)[1])
            self.requests.append(item)
            context = item["ancestor_context"]
            previous = item["previous_qualification_quotes"]
            quotes = []
            for quote in (
                "Only the test edition supports these features.",
                "Only local installations support these features.",
            ):
                if quote in context:
                    quotes.append(quote)
            restrictions = quotes + previous
            consistent = all(
                ("test edition" not in quote or "test edition" in item["claim"])
                and ("local installations" not in quote or "local installations" in item["claim"])
                for quote in quotes
            )
            supported = (
                "outputs images" in (item["local_excerpt"] + context + " ".join(item["previous_support_quotes"]))
                and "imaginary" not in item["claim"]
                and ("test edition" not in item["claim"] or any("test edition" in quote for quote in restrictions))
                and (
                    "local installations" not in item["claim"]
                    or any("local installations" in quote for quote in restrictions)
                )
            )
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": supported,
                        "consistent": consistent,
                        "reason": "Complete local support and applicable ancestor restrictions.",
                        "qualifiers": quotes,
                        "support_quotes": ["The device outputs images."] if supported else [],
                    }
                ]
            }
        items = json.loads(user.split("\n", 1)[1])
        return {
            "verdicts": [
                {
                    "id": item["id"],
                    "supported": item["claim"] in item["excerpt"],
                    "reason": "Actual complete source supports the claim.",
                }
                for item in items
            ]
        }


def source():
    return (
        "# Manual\n"
        + ("Unrelated setup guidance.\n" * 180)
        + "Only the test edition supports these features.\n"
        + ("Additional setup guidance.\n" * 180)
        + "Only local installations support these features.\n## Device\nThe device outputs images.\n"
    )


def evidence(claim):
    return Evidence("s", "manual", "Device", "quote", claim, quote="The device outputs images.")


@pytest.mark.asyncio
async def test_long_ancestor_preserves_real_facts_and_rejects_unqualified_or_invented_conditions():
    records = [
        evidence("In the test edition and local installations, the device outputs images."),
        evidence("The device outputs images."),
        evidence("In an imaginary edition, the device outputs images."),
    ]
    client = Constraints()
    results = await curate.verify_source_claims(client, records, {"s": source()}, char_budget=12000)
    assert results[curate.evidence_id(records[0])]["supported"]
    assert all(results[curate.evidence_id(record)]["status"] == "unsupported" for record in records[1:])
    excerpt = results[curate.evidence_id(records[0])]["excerpt"]
    assert len(excerpt) <= 2000 and "Only the test edition" in excerpt and "Only local installations" in excerpt
    assert "Unrelated setup" not in excerpt and client.requests
    assert any(
        "test edition" in quote for request in client.requests for quote in request["previous_qualification_quotes"]
    )


@pytest.mark.asyncio
async def test_source_constraint_outage_preserves_completed_chunks_and_original_quote(cfg, store):
    from doclayer.curator.pipeline import Pipeline

    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    record = evidence("In the test edition and local installations, the device outputs images.")
    state.sources = [SourceRecord("s", "text", "manual")]
    store.append_evidence(state.job_id, [record.to_dict()])
    runner = Pipeline(cfg, store)
    runner._store_extract(state, state.sources[0], source())
    store.save(state)
    client = Constraints(fail_after=1)
    with pytest.raises(ModelError):
        await curate.verify_source_claims(
            client,
            [record],
            {"s": source()},
            char_budget=12000,
            progress=lambda values: runner._retain_verification(state, values),
        )
    recovered = store.load(state.job_id)
    saved = recovered.evidence_verification[curate.evidence_id(record)]
    assert saved["verification_pending"] and len(saved["ancestor_verdicts"]) == 1
    assert runner._evidence(recovered)[0].quote == record.quote and not any(
        "ancestor verification pending" in gap for gap in recovered.coverage_gaps
    )
    resumed = Constraints()
    results = await curate.verify_source_claims(
        resumed, runner._evidence(recovered), {"s": source()}, char_budget=12000, known=recovered.evidence_verification
    )
    assert (
        results[curate.evidence_id(record)]["supported"]
        and resumed.calls == len(results[curate.evidence_id(record)]["ancestor_verdicts"]) - 1
    )
    altered = Constraints()
    await curate.verify_source_claims(
        altered,
        [record],
        {"s": source().replace("setup guidance", "different setup guidance")},
        char_budget=12000,
        known=results,
    )
    assert altered.calls > resumed.calls


def test_only_active_ancestors_govern_repeated_heading_occurrences():
    text = "# Runtime\nArchived service requires manual approval.\n## Settings\nArchived setting.\n# Runtime\nCurrent service is unattended.\n## Settings\nCurrent setting is automatic.\n"
    record = Evidence(
        "s", "manual", "Settings", "quote", "Current setting is automatic.", quote="Current setting is automatic."
    )
    excerpt = curate.support_excerpt(record, text)
    assert "Current service is unattended" in excerpt and "Archived" not in excerpt


def test_cross_unit_quote_and_ambiguous_paraphrase_cannot_bypass_scope():
    text = "# Card\nThis card is hypothetical only.\n## First\nFirst assertion.\n## Second\nSecond assertion.\n"
    record = Evidence(
        "s", "manual", "", "quote", "General assertion.", quote="First assertion.\n## Second\nSecond assertion."
    )
    assert curate.support_excerpt(record, text) == ""
    ambiguous = "# Provider A\n## Settings\nA settings.\n# Provider B\n## Settings\nB settings.\n"
    record = Evidence("s", "manual", "Settings", "paraphrase", "Settings are supported.")
    assert curate.support_excerpt(record, ambiguous) == ""


def test_actual_child_heading_remains_visible_when_quote_occurs_late():
    text = "# Card\n## Provider B\n" + ("Setup. " * 120) + "The device outputs images.\n"
    record = Evidence(
        "s", "manual", "Provider A", "quote", "Provider A outputs images.", quote="The device outputs images."
    )
    assert "## Provider B" in curate.support_excerpt(record, text)


@pytest.mark.asyncio
async def test_source_verdict_cache_binds_semantic_applicability_and_role():
    record = Evidence(
        "s",
        "manual",
        "Device",
        "quote",
        "The device outputs text.",
        quote="The device outputs text.",
        applicability={"version": "1"},
        role="primary",
    )
    text = "# Device\nThe device outputs text.\n"
    first = Constraints()
    known = await curate.verify_source_claims(first, [record], {"s": text}, char_budget=12000)
    assert first.calls == 1
    cached = Constraints()
    await curate.verify_source_claims(cached, [record], {"s": text}, char_budget=12000, known=known)
    assert cached.calls == 0
    for change in (
        lambda: record.applicability.update(version="2"),
        lambda: record.applicability.update(provider="different"),
        lambda: setattr(record, "role", "supplementary"),
    ):
        change()
        client = Constraints()
        known = await curate.verify_source_claims(client, [record], {"s": text}, char_budget=12000, known=known)
        assert client.calls == 1


@pytest.mark.asyncio
async def test_pipeline_effective_metadata_verdict_cache_is_stable_then_invalidates(cfg, store):
    from doclayer.curator.pipeline import Pipeline
    from doclayer.curator.state import PageChange

    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    source = SourceRecord("s", "text", "manual", scope={"version": "1"}, role="supplementary")
    state.sources = [source]
    record = Evidence("s", "manual", "Device", "quote", "The device outputs text.", quote="The device outputs text.")
    store.append_evidence(state.job_id, [record.to_dict()])
    runner = Pipeline(cfg, store)
    runner._store_extract(state, source, "# Device\nThe device outputs text.\n")
    store.save(state)
    change = PageChange("g", "wiki/device.md", "create", claim_ids=[curate.evidence_id(record)])
    client = Constraints()
    await runner._verify_selected(state, change, client)
    assert client.calls == 1 and runner._evidence(state)[0].support_status == "supported"
    assert (
        runner._evidence(state)[0].applicability == {"version": "1"}
        and runner._evidence(state)[0].role == "supplementary"
    )
    await runner._verify_selected(state, change, client)
    assert client.calls == 1
    source.scope["version"] = "2"
    await runner._verify_selected(state, change, client)
    assert client.calls == 2
    state.source_policy = {"global": {"applicability": {"environment": "local"}}}
    await runner._verify_selected(state, change, client)
    assert client.calls == 3


@pytest.mark.asyncio
async def test_late_same_unit_and_headingless_conditions_cannot_hide_outside_quote_window():
    condition = "Only the test edition supports these features."
    for prefix in ("# Device\n", ""):
        text = prefix + condition + "\n" + ("Unrelated setup guidance.\n" * 220) + "The device outputs images.\n"
        record = evidence("The device outputs images.")
        record.locator = "Device" if prefix else "(introduction)"
        client = Constraints()
        results = await curate.verify_source_claims(client, [record], {"s": text}, char_budget=12000)
        assert results[curate.evidence_id(record)]["status"] == "unsupported"
        assert any(condition in request["ancestor_context"] for request in client.requests)
        assert any("The device outputs images." in request["ancestor_context"] for request in client.requests)


@pytest.mark.asyncio
async def test_repaired_declared_quote_selection_invalidates_unsupported_cache_without_source_change():
    text = "# Manual\nThe device outputs text.\n" + ("Irrelevant setup.\n" * 200) + "The device outputs images.\n"
    record = Evidence("s", "manual", "Manual", "quote", "The device outputs images.", quote="The device outputs text.")
    first = Constraints()
    known = await curate.verify_source_claims(first, [record], {"s": text}, char_budget=12000)
    # Full-unit checking can recover support beyond an incorrect initial quote.
    assert known[curate.evidence_id(record)]["supported"]
    previous = curate.source_support_binding(record, {"s": text})
    record.quote = "The device outputs images."
    assert curate.source_support_binding(record, {"s": text}) != previous
    client = Constraints()
    await curate.verify_source_claims(client, [record], {"s": text}, char_budget=12000, known=known)
    assert client.calls > 0


@pytest.mark.asyncio
async def test_fake_or_duplicate_constraint_verdicts_are_schema_repaired_not_accepted():
    class Malformed(Constraints):
        async def chat_json(self, system, user):
            self.calls += 1
            item = json.loads(user.split("\n", 1)[1])
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": True,
                        "consistent": True,
                        "reason": "guess",
                        "qualifiers": [],
                        "support_quotes": ["invented text"],
                    }
                ]
            }

    record = evidence("The device outputs images.")
    client = Malformed()
    result = await curate.verify_source_claims(client, [record], {"s": source()}, char_budget=12000)
    failed = result[curate.evidence_id(record)]
    assert failed["status"] == "unknown" and failed["supported"] is False and failed["excerpt"] == ""
    assert "invalid source quotation repair" in failed["review_error"]["message"]
    assert failed["review_error"]["diagnostic"]["validation_issues"]
    assert client.calls == 3


@pytest.mark.asyncio
async def test_changed_material_invalidates_cached_positive_before_first_request(cfg, store):
    from doclayer.curator.pipeline import Pipeline

    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    state.sources = [SourceRecord("s", "text", "manual")]
    record = Evidence("s", "manual", "Device", "quote", "The device outputs text.", quote="The device outputs text.")
    store.append_evidence(state.job_id, [record.to_dict()])
    runner = Pipeline(cfg, store)
    runner._store_extract(state, state.sources[0], "# Device\nThe device outputs text.\n")
    store.save(state)

    def progress(results):
        return runner._retain_verification(state, results)

    await curate.verify_source_claims(
        Constraints(),
        runner._evidence(state),
        {"s": runner._extract_text(state, state.sources[0])},
        char_budget=12000,
        progress=progress,
    )
    old = state.evidence_verification[curate.evidence_id(record)]["source_binding"]
    runner._store_extract(
        state, state.sources[0], "# Device\nOnly the test edition supports these features.\nThe device outputs text.\n"
    )

    class Outage:
        async def chat_json(self, *args):
            persisted = store.load(state.job_id).evidence_verification[curate.evidence_id(record)]
            assert (
                persisted["verification_pending"] and not persisted["supported"] and persisted["source_binding"] != old
            )
            raise ModelError("inference outage")

    with pytest.raises(ModelError, match="inference outage"):
        await curate.verify_source_claims(
            Outage(),
            runner._evidence(state),
            {"s": runner._extract_text(state, state.sources[0])},
            char_budget=12000,
            known=state.evidence_verification,
            progress=progress,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["duplicate", "missing", "foreign", "nonboolean"])
async def test_constraint_schema_requires_exact_unique_identity_and_boolean_status(kind):
    class Malformed:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            item = json.loads(user.split("\n", 1)[1])
            value = {
                "id": item["id"],
                "supported": False,
                "consistent": True,
                "reason": "sparse",
                "qualifiers": [],
                "support_quotes": [],
            }
            if kind == "duplicate":
                values = [value, value]
            elif kind == "missing":
                values = []
            elif kind == "foreign":
                values = [dict(value, id="foreign")]
            else:
                values = [dict(value, consistent="yes")]
            return {"verdicts": values}

    client = Malformed()
    record = evidence("The device outputs images.")
    result = await curate.verify_source_claims(client, [record], {"s": source()}, char_budget=12000)
    failed = result[curate.evidence_id(record)]
    assert failed["status"] == "unknown" and failed["supported"] is False and failed["excerpt"] == ""
    assert failed["review_error"]["message"] == "invalid source ancestor constraint response"
    assert failed["review_error"]["diagnostic"]["attempts"] == 3
    assert failed["review_error"]["diagnostic"]["validation_issues"]
    assert client.calls == 3


@pytest.mark.asyncio
async def test_extraction_keeps_declared_quote_and_reuses_effective_support_cache(cfg, store, monkeypatch):
    from doclayer.curator.pipeline import Pipeline
    from doclayer.curator.state import PageChange

    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    source = SourceRecord("s", "text", "manual", status="fetched", scope={"version": "1"}, role="supplementary")
    state.sources = [source]
    runner = Pipeline(cfg, store)
    original = "The device outputs text."
    runner._store_extract(state, source, "# Device\nA local installation is required.\n" + original + "\n")
    store.save(state)

    class Client(Constraints):
        async def chat_bounded_json(self, system, user, *, char_budget):
            assert len(system) + len(user) <= char_budget
            return await self.chat_json(system, user)

        async def chat_verdicts(self, system, user, *, char_budget):
            assert len(system) + len(user) <= char_budget
            return await self.chat_json(system, user)

        async def chat_json(self, system, user):
            if "extract durable, checkable claims" in system:
                return {
                    "doc_kind": "api-reference",
                    "claims": [{"claim": original, "locator": "Device", "quote": original}],
                }
            return await super().chat_json(system, user)

    from doclayer.curator.state import Usage

    client = Client()
    client.usage = Usage()
    client.max_calls = cfg.limits.max_model_calls
    monkeypatch.setattr(runner, "_model", lambda _, stage="curation": client)
    await runner._stage_extract(state)
    records = runner._evidence(state)
    assert records and records[0].support_status == "supported" and records[0].original_quote == original
    assert records[0].quote != original  # hydrated contextual support is not quote identity
    calls = client.calls
    change = PageChange("g", "wiki/device.md", "create", claim_ids=[curate.evidence_id(records[0])])
    await runner._verify_selected(state, change, client)
    assert client.calls == calls


@pytest.mark.asyncio
async def test_literal_qualification_from_supplied_local_excerpt_is_valid_during_ancestor_review():
    text = (
        "# Manual\n"
        + ("Preparation guidance.\n" * 160)
        + "## Limits\nThe device outputs images only in the test edition.\n"
    )
    record = Evidence(
        "s",
        "manual",
        "Limits",
        "quote",
        "The device outputs images only in the test edition.",
        quote="The device outputs images only in the test edition.",
    )

    class Client:
        async def chat_json(self, system, user):
            item = json.loads(user.split("\n", 1)[1])
            quote = "The device outputs images only in the test edition."
            present = quote in item["ancestor_context"]
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": present,
                        "consistent": True,
                        "reason": "Actual complete local context supports the qualification.",
                        "qualifiers": [quote] if present else [],
                        "support_quotes": [quote] if present else [],
                    }
                ]
            }

    result = await curate.verify_source_claims(Client(), [record], {"s": text}, char_budget=12000)
    assert result[curate.evidence_id(record)]["supported"]


@pytest.mark.asyncio
async def test_normalized_pdf_source_quotes_restore_actual_literal_offsets():
    text = "# Manual\n" + ("Preparation guidance.\n" * 160) + "## Device\nThedevice outputsimages onlyintest edition.\n"
    record = Evidence(
        "s",
        "manual",
        "Device",
        "quote",
        "The device outputs images only in test edition.",
        quote="The device outputs images only in test edition.",
    )

    class Client:
        async def chat_json(self, system, user):
            item = json.loads(user.split("\n", 1)[1])
            quote = "The device outputs images only in test edition."
            present = "Thedevice outputsimages onlyintest edition." in item["ancestor_context"]
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": present,
                        "consistent": True,
                        "reason": "Actual supplied PDF source supports the qualification.",
                        "qualifiers": [quote] if present else [],
                        "support_quotes": [quote] if present else [],
                    }
                ]
            }

    result = await curate.verify_source_claims(Client(), [record], {"s": text}, char_budget=12000)
    assert result[curate.evidence_id(record)]["supported"]
    assert "Thedevice outputsimages onlyintest edition." in result[curate.evidence_id(record)]["excerpt"]


@pytest.mark.asyncio
async def test_consistent_context_without_complete_positive_support_is_unknown_not_rejected():
    class Missing:
        async def chat_json(self, system, user):
            item = json.loads(user.split("\n", 1)[1])
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": False,
                        "consistent": True,
                        "reason": "The specific model version is not established here.",
                        "qualifiers": [],
                        "support_quotes": [],
                    }
                ]
            }

    record = evidence("In the test edition and local installations, version 9 outputs images.")
    result = await curate.verify_source_claims(Missing(), [record], {"s": source()}, char_budget=12000)
    assert result[curate.evidence_id(record)]["status"] == "unknown"


@pytest.mark.asyncio
async def test_ancestor_review_does_not_repeat_positive_child_before_scope_is_reviewed():
    class Focused(Constraints):
        async def chat_json(self, system, user):
            if user.startswith("SOURCE ANCESTOR CONSTRAINT CHECK"):
                item = json.loads(user.split("\n", 1)[1])
                assert item["local_excerpt"] == ""
                if item["ancestor_locator"] == "Manual":
                    assert "The device outputs images." not in item["ancestor_context"]
            return await super().chat_json(system, user)

    record = evidence("The device outputs images.")
    result = await curate.verify_source_claims(Focused(), [record], {"s": source()}, char_budget=12000)
    assert result[curate.evidence_id(record)]["status"] == "unsupported"


@pytest.mark.asyncio
async def test_raw_supported_marker_without_current_verifier_proof_is_rechecked(cfg, store):
    from doclayer.curator.pipeline import Pipeline
    from doclayer.curator.state import PageChange

    state = store.create(JobState(job_id="", idempotency_key="", intent="Device outputs"))[0]
    source = SourceRecord("s", "text", "manual", status="fetched")
    state.sources = [source]
    record = Evidence(
        "s",
        "manual",
        "Device",
        "quote",
        "The device outputs images.",
        quote="The device outputs text only.",
        support_status="supported",
    )
    store.append_evidence(state.job_id, [record.to_dict()])
    runner = Pipeline(cfg, store)
    runner._store_extract(
        state, source, "# Device\nThe device outputs text only. Image creation requires a separate tool.\n"
    )
    store.save(state)
    assert runner._evidence(state)[0].support_status == "unknown"

    class SourceCheck:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            items = json.loads(user.split("\n", 1)[1])
            assert "Image creation requires a separate tool." in items[0]["excerpt"]
            return {
                "verdicts": [
                    {"id": item["id"], "supported": False, "reason": "Source explicitly limits this device to text."}
                    for item in items
                ]
            }

    client = SourceCheck()
    change = PageChange("g", "wiki/device.md", "update", claim_ids=[curate.evidence_id(record)])
    await runner._verify_selected(state, change, client)
    assert client.calls == 1 and not runner._evidence(state)
    assert state.evidence_verification[curate.evidence_id(record)]["status"] == "unsupported"
    # The page-specific retained pool cannot silently re-admit the raw marker.
    eligible = runner._page_evidence(state, change, runner._evidence(state))
    assert eligible == [] and change.missing_evidence == [curate.evidence_id(record)]
    # The new article reviewer gets no phantom supported record and blocks the
    # assigned contribution before inference; resolver is fingerprint-only.
    change.draft_body = "# Device\nThe device outputs images.\n"
    outcome = await curate.check_draft(
        client,
        "Device",
        change,
        eligible,
        [],
        char_budget=12000,
        support_resolver=runner._support_resolver(state, client),
    )
    assert outcome["failures"][0]["check"] == "required-evidence"
    assert client.calls == 1


def test_raw_rejected_marker_without_current_proof_is_unknown_not_ignored(cfg, store):
    from doclayer.curator.pipeline import Pipeline

    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    record = Evidence(
        "s",
        "manual",
        "Device",
        "quote",
        "The device outputs text.",
        quote="The device outputs text.",
        support_status="unsupported",
    )
    store.append_evidence(state.job_id, [record.to_dict()])
    runner = Pipeline(cfg, store)
    state.sources = [SourceRecord("s", "text", "manual")]
    runner._store_extract(state, state.sources[0], "# Device\nThe device outputs text.\n")
    assert runner._evidence(state)[0].support_status == "unknown"


@pytest.mark.asyncio
async def test_actual_excerpt_budget_routes_short_unit_with_late_condition_into_complete_context():
    quote = "The device outputs images."
    condition = "Only laboratory installations support these features."
    text = "# Device\n" + quote + "\n" + ("Preparation guidance.\n" * 85) + condition + "\n"
    assert 1700 < len(text) < 2000
    record = Evidence("s", "manual", "Device", "quote", quote, quote=quote)

    class Client:
        saw_condition = False

        async def chat_json(self, system, user):
            assert len(system) + len(user) <= 4500
            assert user.startswith("SOURCE ANCESTOR CONSTRAINT CHECK")
            item = json.loads(user.split("\n", 1)[1])
            context = item["ancestor_context"]
            found = condition in context
            self.saw_condition = self.saw_condition or found
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": quote in context,
                        "consistent": not found,
                        "reason": "A necessary installation restriction is omitted."
                        if found
                        else "This partial source region provides no restriction.",
                        "qualifiers": [condition] if found else [],
                        "support_quotes": [quote] if quote in context else [],
                    }
                ]
            }

    client = Client()
    result = await curate.verify_source_claims(client, [record], {"s": text}, char_budget=4500)
    assert client.saw_condition and result[curate.evidence_id(record)]["status"] == "unsupported"


@pytest.mark.asyncio
async def test_partial_source_fact_survives_outage_until_later_scope_completes_claim():
    quote = "The device outputs images."
    condition = "Only laboratory installations support these features."
    text = "# Device\n" + quote + "\n" + ("Preparation guidance.\n" * 85) + condition + "\n"
    record = Evidence(
        "s", "manual", "Device", "quote", "In laboratory installations, the device outputs images.", quote=quote
    )
    saved = {}

    class Client:
        def __init__(self, outage=False):
            self.outage = outage
            self.calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            if self.outage and self.calls == 2:
                raise ModelError("inference outage")
            item = json.loads(user.split("\n", 1)[1])
            context = item["ancestor_context"]
            previous = item["previous_support_quotes"]
            supported = condition in context and quote in previous
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": supported,
                        "consistent": True,
                        "reason": "Partial source fact and later condition jointly establish the qualified claim.",
                        "qualifiers": [condition] if condition in context else [],
                        "support_quotes": [quote] if quote in context or supported else [],
                    }
                ]
            }

    with pytest.raises(ModelError):
        await curate.verify_source_claims(
            Client(outage=True), [record], {"s": text}, char_budget=4500, progress=lambda values: saved.update(values)
        )
    identity = curate.evidence_id(record)
    assert saved[identity]["verification_pending"]
    assert any(
        not v["supported"] and v["support_quotes"] == [quote] for v in saved[identity]["ancestor_verdicts"].values()
    )
    client = Client()
    result = await curate.verify_source_claims(client, [record], {"s": text}, char_budget=4500, known=saved)
    assert (
        result[identity]["supported"]
        and quote in result[identity]["excerpt"]
        and condition in result[identity]["excerpt"]
    )
    assert client.calls == 2  # first partial fact resumes from its durable checkpoint


def test_quote_typography_restores_unique_actual_bytes_and_preserves_ambiguity():
    quote = 'The device\'s output is "text only".'
    actual = "The device’s output is “text only”."
    text = "# Card\n## Page 7\n" + actual + "\n"
    span = curate._quote_span(quote, text, "Page 7")
    assert span and text[span[0] : span[1]] == actual
    duplicate = text + "## Page 8\n" + quote + "\n"
    assert curate._quote_span(quote, duplicate) is None
    record = Evidence("s", "card", "", "quote", quote, quote=quote)
    assert curate.support_excerpt(record, duplicate) == ""
    for locator, expected in (("Page 7", actual), ("Page 8", quote)):
        span = curate._quote_span(quote, duplicate, locator)
        assert span and duplicate[span[0] : span[1]] == expected
    assert not curate._quote_spans("The device produces images.", text, "Page 7")
    assert not curate._quote_spans(
        "The device’s output is text only.", text, "Page 7"
    )  # punctuation remains meaningful


@pytest.mark.asyncio
async def test_source_context_quotes_restore_typography_without_accepting_unseen_paraphrase():
    actual = "The device’s output is “text only” in enterprise installations."
    alias = 'The device\'s output is "text only" in enterprise installations.'
    text = "# Manual\n" + ("Preparation guidance.\n" * 160) + "## Device\n" + actual + "\n"
    record = Evidence("s", "manual", "Device", "quote", alias, quote=alias)

    class Client:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            item = json.loads(user.split("\n", 1)[1])
            present = actual in item["ancestor_context"]
            assert "at most 8 quotes" in system and "at most 600 characters" in system
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": present,
                        "consistent": True,
                        "reason": "Actual source wording.",
                        "qualifiers": ["enterprise installations"] if present else [],
                        "support_quotes": [alias] if present else [],
                    }
                ]
            }

    result = await curate.verify_source_claims(Client(), [record], {"s": text}, char_budget=12000)
    verdict = result[curate.evidence_id(record)]
    assert verdict["supported"] and actual in verdict["excerpt"] and alias not in verdict["excerpt"]

    class Unseen(Client):
        async def chat_json(self, system, user):
            item = json.loads(user.split("\n", 1)[1])
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": True,
                        "consistent": True,
                        "reason": "Invented scope.",
                        "qualifiers": ["consumer installations"],
                        "support_quotes": ["The device outputs images."],
                    }
                ]
            }

    result = await curate.verify_source_claims(Unseen(), [record], {"s": text}, char_budget=12000)
    failed = result[curate.evidence_id(record)]
    assert failed["status"] == "unknown" and failed["supported"] is False and failed["excerpt"] == ""
    assert any("literal source span" in issue for issue in failed["review_error"]["diagnostic"]["validation_issues"])


@pytest.mark.asyncio
@pytest.mark.parametrize("malformation", ["too_long", "too_many", "not_literal"])
async def test_invalid_source_quote_contract_has_actionable_bounded_diagnostic_and_repairs(malformation):
    from doclayer.curator.source_constraints import verify

    text = "# Manual\n" + ("Preparation guidance.\n" * 170) + "## Device\nThe device outputs text.\n"
    record = Evidence("s", "manual", "Device", "quote", "The device outputs text.", quote="The device outputs text.")

    class Client:
        calls = 0
        prompts = []

        async def chat_json(self, system, user):
            self.calls += 1
            self.prompts.append(system)
            assert len(system) + len(user) <= 6000
            item = json.loads(user.split("\n", 1)[1])
            quote = (
                "A" * 601
                if malformation == "too_long"
                else ("missing assertion" if malformation == "not_literal" else "# Manual")
            )
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": False,
                        "consistent": True,
                        "reason": "sparse",
                        "qualifiers": [quote] * 9 if malformation == "too_many" else [quote],
                        "support_quotes": [],
                    }
                ]
            }

    client = Client()
    progress = []
    with pytest.raises(ModelError) as exc:
        await verify(
            client,
            record,
            text,
            char_budget=6000,
            excerpt_budget=2000,
            binding="fixture",
            progress=lambda value: progress.append(value),
        )
    assert client.calls == 3
    diagnostic = exc.value.diagnostic
    assert diagnostic["operation"] in ("source-ancestor-constraints", "source-quotation-repair")
    assert diagnostic["attempts"] == (3 if malformation == "too_many" else 2)
    expected = {
        "too_long": "exceeds 600 characters",
        "too_many": "exceeds 8 quotes",
        "not_literal": "not a literal source span",
    }[malformation]
    assert any(expected in issue for issue in diagnostic["validation_issues"])
    if malformation == "too_many":
        assert expected in client.prompts[1] and expected in client.prompts[2]
    else:
        assert "Repair ONLY the identified invalid quotation fields" in client.prompts[1]
        assert "id/edits" in client.prompts[2]
    assert len(json.dumps(diagnostic)) < 1800 and "ancestor_context" not in diagnostic
    assert progress[-1][curate.evidence_id(record)]["verification_pending"]
    assert not progress[-1][curate.evidence_id(record)]["supported"]


@pytest.mark.asyncio
async def test_old_positive_proof_is_invalidated_when_typography_adds_an_ambiguous_candidate():
    quote = "The device's output is text only."
    text = "# Provider A\n" + quote + "\n# Provider B\nThe device’s output is text only.\n"
    record = Evidence("s", "card", "", "quote", quote, quote=quote)
    identity = curate.evidence_id(record)
    legacy = {
        identity: {
            "support_version": 10,
            "source_binding": curate.source_support_binding(record, {"s": text}),
            "supported": True,
            "status": "supported",
            "verification_pending": False,
            "excerpt": "# Provider A\n" + quote,
        }
    }

    class NoModel:
        async def chat_json(self, *args):
            pytest.fail("Ambiguous quote cannot become source support")

    result = await curate.verify_source_claims(NoModel(), [record], {"s": text}, char_budget=12000, known=legacy)
    assert curate.SOURCE_SUPPORT_VERSION == 12
    assert result[identity]["status"] == "unknown" and not result[identity]["supported"]
    assert not result[identity]["excerpt"]


@pytest.mark.asyncio
async def test_quote_contract_repair_is_prioritized_without_discarding_unsupported_scope():
    from doclayer.curator.source_constraints import verify

    text = "# Manual\n" + ("Preparation guidance.\n" * 160) + "## Device\nOnly local deployments support images.\n"
    record = Evidence(
        "s", "manual", "Device", "quote", "The device outputs images.", quote="Only local deployments support images."
    )

    class Client:
        repaired = False

        async def chat_json(self, system, user):
            item = json.loads(user.split("\n", 1)[1])
            present = "Only local deployments support images." in item["ancestor_context"]
            if not present:
                return {
                    "verdicts": [
                        {
                            "id": item["id"],
                            "supported": False,
                            "consistent": True,
                            "reason": "sparse",
                            "qualifiers": [],
                            "support_quotes": [],
                        }
                    ]
                }
            if user.startswith("SOURCE LITERAL QUOTATION REPAIR"):
                self.repaired = True
                assert "qualifiers[0] is not a literal source span" in user
                return {
                    "verdicts": [
                        {
                            "id": item["id"],
                            "edits": [
                                {
                                    "field": "qualifiers",
                                    "index": 0,
                                    "replacement": "Only local deployments support images.",
                                }
                            ],
                        }
                    ]
                }
            if not user.startswith("SOURCE REPAIRED QUOTATION SEMANTIC CHECK"):
                return {
                    "verdicts": [
                        {
                            "id": item["id"],
                            "supported": False,
                            "consistent": False,
                            "reason": "Restriction omitted.",
                            "qualifiers": ["local deployment requirement"],
                            "support_quotes": [],
                        }
                    ]
                }
            self.repaired = True
            assert list(item["retained_quotes"]["qualifiers"].values()) == ["Only local deployments support images."]
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": False,
                        "consistent": False,
                        "reason": "Restriction omitted.",
                        "retained_quote_ids": ["q0"],
                        "additional_qualifiers": [],
                        "additional_support_quotes": [],
                    }
                ]
            }

    client = Client()
    result = await verify(client, record, text, char_budget=12000, excerpt_budget=2000, binding="fixture")
    assert client.repaired and result["status"] == "unsupported" and not result["supported"]
    assert "Only local deployments support images." in result["excerpt"]
