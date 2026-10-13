"""Source strings shown in either retained quote array retain their subspans."""

import hashlib
import json

import httpx
import pytest

from doclayer.curator import curate, source_constraints
from doclayer.curator.model import ModelClient
from doclayer.curator.state import BudgetExceeded, Evidence, JobState

WHOLE = 'response = client.create({\n  type: "input_image",\n  image_url: "https://example.invalid/image.png",\n  detail: "auto",\n})'
PART = 'type: "input_image",\n  image_url: "https://example.invalid/image.png",\n  detail: "auto",'


def item(qualifiers=None, support=None):
    return dict(
        id="scope",
        supported=True,
        consistent=True,
        reason="Literal source.",
        qualifiers=qualifiers or [],
        support_quotes=support or [],
    )


@pytest.mark.parametrize("previous_field", ["qualifiers", "support_quotes"])
@pytest.mark.parametrize("output_field", ["qualifiers", "support_quotes"])
def test_exact_subspan_is_visible_across_both_shown_quote_arrays(previous_field, output_field):
    values = item(support=["Current literal fact."])
    values[output_field] = [PART]
    previous = {"qualifiers": [], "support_quotes": []}
    previous[previous_field] = [WHOLE]
    assert not source_constraints._validation_issues(
        [values],
        "scope",
        [("Manual", WHOLE), ("Local", "Current literal fact.")],
        "Current literal fact.",
        "",
        previous["qualifiers"],
        previous["support_quotes"],
    )


@pytest.mark.parametrize("hidden", ["whole-source", "metadata", "claim", "foreign-source"])
def test_unshown_or_foreign_source_cannot_establish_visible_literal_proof(hidden):
    quote = "Secret conditional fact."
    contexts = [("Manual", "Current literal fact.")]
    if hidden != "foreign-source":
        contexts.append(("Later", quote))
    previous = [quote] if hidden == "foreign-source" else []
    issues = source_constraints._validation_issues(
        [item(support=[quote])],
        "scope",
        contexts,
        "Current literal fact.",
        "",
        [],
        previous,
        metadata={"applicability": {"edition": quote}, "claim": quote},
    )
    expected = "not a literal source span" if hidden == "foreign-source" else "was not shown"
    assert any(expected in issue for issue in issues)


def test_normalized_cross_field_span_restores_original_code_bytes_and_stays_literal():
    values = [item(support=['type: "input_image", image_url: "https://example.invalid/image.png", detail: "auto",'])]
    source_constraints._restore_quote_spans(values, "Current literal fact.", "", [WHOLE], [])
    assert values[0]["support_quotes"] == [PART]
    assert not source_constraints._validation_issues(
        values, "scope", [("Manual", WHOLE)], "Current literal fact.", "", [WHOLE], []
    )
    # Distinct repeated punctuation variants remain ambiguous, not rewritten.
    variants = ["The device’s output is text.", "The device's output is text."]
    values = [item(support=["The device‘s output is text."])]
    source_constraints._restore_quote_spans(values, "", "", variants, [])
    assert values[0]["support_quotes"] == ["The device‘s output is text."]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["valid", "source", "request", "repair-binding", "id", "boolean"])
async def test_actual_wrapper_zero_inference_recovers_bound_original_after_store_restart(cfg, store, mutation):
    fact = "The device accepts input_image and detail parameters."
    text = "# Manual\n" + WHOLE + "\n" + "Preparation guidance.\n" * 170 + "## Device\n" + fact + "\n"
    record = Evidence("s", "manual", "Device", "quote", fact, quote=fact)
    identity = curate.evidence_id(record)
    frames = []

    class Initial:
        async def chat_verdicts(self, system, user, *, char_budget):
            frame = json.loads(user.split("\n", 1)[1])
            frames.append(frame)
            body = frame["ancestor_context"]
            return {
                "verdicts": [
                    dict(
                        id=frame["id"],
                        supported=fact in body,
                        consistent=True,
                        reason="Actual source fields.",
                        qualifiers=[],
                        support_quotes=[PART] if fact in body else ([WHOLE] if WHOLE in body else []),
                    )
                ]
            }

    proof = (await curate.verify_source_claims(Initial(), [record], {"s": text}, char_budget=6000))[identity]
    assert proof["supported"]
    request = frames[-1]
    key = hashlib.sha256((request["ancestor_locator"] + "\0" + request["ancestor_context"]).encode()).hexdigest()
    original = proof["ancestor_verdicts"].pop(key)
    assert original["support_quotes"] == [PART] and any(WHOLE in part for part in request["previous_support_quotes"])
    repair_binding = hashlib.sha256(
        json.dumps(
            {"source_binding": proof["source_binding"], "request": request},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    proof.update(
        supported=False,
        status="unknown",
        verification_pending=True,
        excerpt="",
        pending_quote_repairs={
            key: {
                "binding": repair_binding,
                "original": original,
                "targets": [
                    {
                        "field": "support_quotes",
                        "index": 0,
                        "invalid_quote": PART,
                        "error": "support_quotes[0] was not shown in current or retained context",
                    }
                ],
            }
        },
    )
    state, _ = store.create(
        JobState(
            job_id="job-retained01",
            idempotency_key="retained-recovery",
            intent="Device",
        )
    )
    if mutation == "source":
        text += "\nChanged source revision.\n"
    elif mutation == "request":
        record.applicability = {"edition": "Enterprise"}
    elif mutation == "repair-binding":
        proof["pending_quote_repairs"][key]["binding"] = "wrong-request"
    elif mutation == "id":
        proof["pending_quote_repairs"][key]["original"]["id"] = "wrong-id"
    elif mutation == "boolean":
        proof["pending_quote_repairs"][key]["original"]["supported"] = 1
    state.evidence_verification = {identity: proof}
    store.save(state)
    state = store.load(state.job_id)

    def forbidden(request):
        pytest.fail("Bound earlier complete semantic decision needs no repeated inference")

    client = ModelClient(
        cfg,
        state.usage,
        limits={"max_model_calls": 0},
        transport=httpx.MockTransport(forbidden),
    )

    def checkpoint(rows):
        state.evidence_verification.update(rows)
        store.save(state)

    if mutation != "valid":
        with pytest.raises(BudgetExceeded):
            await curate.verify_source_claims(
                client,
                [record],
                {"s": text},
                char_budget=6000,
                known=state.evidence_verification,
                progress=checkpoint,
            )
        assert not state.evidence_verification[identity]["supported"] and state.usage.model_calls == 0
        return
    result = await curate.verify_source_claims(
        client,
        [record],
        {"s": text},
        char_budget=6000,
        known=state.evidence_verification,
        progress=checkpoint,
    )
    assert result[identity]["supported"] and not result[identity]["pending_quote_repairs"]
    assert state.usage.model_calls == 0 and store.load(state.job_id).evidence_verification[identity]["supported"]
