"""Exact no-span locator fallback retains governing context and old proofs."""

import json

import pytest

from doclayer.curator import curate, source_constraints
from doclayer.curator.state import BudgetExceeded, Evidence, JobState


def material(condition=False):
    root = "Only enterprise installations support editing.\n" if condition else "Media endpoint guide.\n"
    local = "## Edit Media\nThe edits endpoint supports:\n- Edit existing images\n- Create reference-based images\n- Edit masked regions\n"
    text = (
        "# Provider Guide\n"
        + root
        + "Unrelated setup guidance.\n" * 170
        + local
        + "### Reference images\n"
        + "Reference example.\n" * 150
        + "### Mask editing\n"
        + "Mask example.\n" * 150
    )
    claim = (
        "Enterprise installations: " if condition else ""
    ) + "The edits endpoint can edit existing images, create reference-based images, and edit masked regions."
    quote = "Edit existing images, Create reference-based images, Edit masked regions"
    record = Evidence(
        "s",
        "manual",
        "#Edit Media",
        "quote",
        claim,
        quote=quote,
        applicability={"provider": "ProviderZ"},
    )
    return record, text, local, root.strip()


def cached(record, text, status="unknown"):
    return {
        "supported": status == "supported",
        "status": status,
        "verification_pending": False,
        "reason": "selected source support exceeds bounded context" if status == "unknown" else "Previously verified.",
        "excerpt": "Existing literal proof." if status == "supported" else "",
        "support_version": curate.SOURCE_SUPPORT_VERSION,
        "source_binding": curate.source_support_binding(record, {"s": text}),
    }


class SourceClient:
    def __init__(self, local, condition=None):
        self.local = local
        self.condition = condition
        self.requests = []

    async def chat_verdicts(self, system, user, *, char_budget):
        assert len(system) + len(user) <= char_budget
        frame = json.loads(user.split("\n", 1)[1])
        self.requests.append(frame)
        if "ancestor_context" not in frame:
            pytest.fail("Long governing context must be fully streamed")
        body = frame["ancestor_context"]
        qualified = not self.condition or "Enterprise installations:" in frame["claim"]
        return {
            "verdicts": [
                dict(
                    id=frame["id"],
                    supported=self.local in body,
                    consistent=qualified,
                    reason="Qualified support." if qualified else "Claim omits enterprise installation restriction.",
                    qualifiers=[self.condition] if self.condition and self.condition in body else [],
                    support_quotes=[self.local] if self.local in body else [],
                )
            ]
        }


@pytest.mark.asyncio
async def test_exact_parent_bullets_recover_old_unknown_with_complete_ancestors():
    record, text, local, _ = material()
    regions = curate._source_regions(text)
    assert len(curate._located_regions(record.locator, regions)[0]) == 3
    selected, _ = curate._located_source_units(record.locator, regions)
    assert len(selected) == 1 and text[selected[0][0] : selected[0][1]] == local
    assert not curate._quote_spans(record.quote, text, record.locator)
    client = SourceClient(local)
    identity = curate.evidence_id(record)
    result = await curate.verify_source_claims(
        client,
        [record],
        {"s": text},
        char_budget=6000,
        known={identity: cached(record, text)},
    )
    assert result[identity]["supported"] and client.requests
    assert any("Media endpoint guide." in request["ancestor_context"] for request in client.requests)
    assert any(local in request["ancestor_context"] for request in client.requests)
    assert all("Reference example." not in request["ancestor_context"] for request in client.requests)


@pytest.mark.asyncio
async def test_valid_current_positive_cache_untouched():
    record, text, _, _ = material()
    proof = cached(record, text, "supported")

    class Never:
        async def chat_verdicts(self, *args, **kwargs):
            pytest.fail("Valid positive proof cannot be gratuitously invalidated")

    result = await curate.verify_source_claims(
        Never(),
        [record],
        {"s": text},
        char_budget=6000,
        known={curate.evidence_id(record): proof},
    )
    assert result[curate.evidence_id(record)] == proof


@pytest.mark.asyncio
async def test_fallback_preserves_governing_environment_restriction():
    record, text, local, condition = material(True)
    record.text = record.text.replace("Enterprise installations: ", "")
    result = await curate.verify_source_claims(SourceClient(local, condition), [record], {"s": text}, char_budget=6000)
    assert result[curate.evidence_id(record)]["status"] == "unsupported"
    assert condition in result[curate.evidence_id(record)]["excerpt"]


def test_exact_hierarchical_path_resolves_repeated_leaf_but_unqualified_leaf_is_ambiguous():
    text = "# Edition A\nEnterprise only.\n## Edit Media\nFirst endpoint.\n### Examples\nExample A.\n# Edition B\nConsumer only.\n## Edit Media\nSecond endpoint.\n"
    regions = curate._source_regions(text)
    assert len(curate._located_source_units("#Edit Media", regions)[0]) == 2
    selected, _ = curate._located_source_units("#Edition A / Edit Media", regions)
    assert len(selected) == 1 and selected[0][2] == "Edition A / Edit Media"
    record = Evidence(
        "s",
        "manual",
        "#Edit Media",
        "quote",
        "Editing is supported.",
        quote="Nonliteral combined summary.",
    )
    assert source_constraints.material(record, text, 50) is None
    assert not curate.support_excerpt(record, text, 2000)


@pytest.mark.asyncio
async def test_repeated_unresolved_locator_old_unknown_stays_unknown_without_model():
    text = "# A\n## Options\nOne.\n# B\n## Options\nTwo.\n"
    record = Evidence(
        "s",
        "manual",
        "#Options",
        "quote",
        "Both options are available.",
        quote="One, Two",
    )

    class Never:
        async def chat_verdicts(self, *args, **kwargs):
            pytest.fail("Ambiguity cannot grant semantic source admission")

    result = await curate.verify_source_claims(
        Never(),
        [record],
        {"s": text},
        char_budget=6000,
        known={curate.evidence_id(record): cached(record, text)},
    )
    assert result[curate.evidence_id(record)]["status"] == "unknown"


def test_literal_cross_unit_quote_and_explicit_pdf_page_keep_old_refusal():
    text = "# A\nOne.\n# B\nTwo.\n"
    record = Evidence(
        "s",
        "manual",
        "",
        "quote",
        "A and B support the feature.",
        quote="One.\n# B\nTwo.",
    )
    assert not curate.support_excerpt(record, text, 2000)
    assert source_constraints.material(record, text, 100) is None
    regions = curate._source_regions("# Page 8\nIntro.\n## Details\nFacts.\n# Page 9\nOther.\n")
    assert curate._located_source_units("Page 8", regions) == curate._located_regions("Page 8", regions)


@pytest.mark.asyncio
async def test_wrapper_checkpoint_preserves_pending_quote_stage_through_real_store_restart(
    store,
):
    record, text, local, _ = material()
    identity = curate.evidence_id(record)
    binding = curate.source_support_binding(record, {"s": text})
    snapshots = []

    class Stops:
        async def chat_verdicts(self, system, user, *, char_budget):
            request = json.loads(user.split("\n", 1)[1])
            if user.startswith("SOURCE LITERAL QUOTATION REPAIR"):
                raise BudgetExceeded("model_calls")
            return {
                "verdicts": [
                    dict(
                        id=request["id"],
                        supported=False,
                        consistent=True,
                        reason="Scope only.",
                        qualifiers=["Media endpoint guide.", "Invented Provider"],
                        support_quotes=[],
                    )
                ]
            }

    with pytest.raises(BudgetExceeded):
        await curate.verify_source_claims(
            Stops(),
            [record],
            {"s": text},
            char_budget=6000,
            progress=lambda rows: snapshots.append(json.loads(json.dumps(rows))),
        )
    proof = snapshots[-1][identity]
    assert proof["pending_quote_repairs"]
    state, _ = store.create(
        JobState(
            job_id="job-quotewrap1",
            idempotency_key="wrapper-checkpoint",
            intent="Media",
        )
    )
    state.evidence_verification = {identity: proof}
    store.save(state)

    def interrupted(rows):
        state.evidence_verification.update(rows)
        store.save(state)
        raise BudgetExceeded("model_calls")

    with pytest.raises(BudgetExceeded):
        await curate.verify_source_claims(
            Stops(),
            [record],
            {"s": text},
            char_budget=6000,
            known=store.load(state.job_id).evidence_verification,
            progress=interrupted,
        )
    restored = store.load(state.job_id).evidence_verification[identity]
    assert restored["source_binding"] == binding and restored["pending_quote_repairs"] == proof["pending_quote_repairs"]
    assert not restored["supported"] and restored["verification_pending"]

    class Resume(SourceClient):
        async def chat_verdicts(self, system, user, *, char_budget):
            request = json.loads(user.split("\n", 1)[1])
            if user.startswith("SOURCE LITERAL QUOTATION REPAIR"):
                self.requests.append(request)
                return {
                    "verdicts": [
                        {
                            "id": request["id"],
                            "edits": [{"field": "qualifiers", "index": 1, "replacement": None}],
                        }
                    ]
                }
            return await super().chat_verdicts(system, user, char_budget=char_budget)

    client = Resume(local)
    result = await curate.verify_source_claims(
        client, [record], {"s": text}, char_budget=6000, known={identity: restored}
    )
    assert client.requests[0]["fixed_semantic_verdict"]["supported"] is False
    assert result[identity]["supported"]
