"""Whole qualified extraction replies are repaired before candidate admission."""

import copy
import json

import httpx
import pytest

from doclayer.curator import curate
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.state import BudgetExceeded, SourceRecord, Usage


class CandidateBoundary(Exception):
    pass


def wire_client(cfg, replies, *, max_calls=20):
    requests = []
    replies = iter(replies)

    def handler(request):
        messages = json.loads(request.content)["messages"]
        system, user = [m["content"] for m in messages]
        requests.append((system, user))
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    cfg.inference.retries = 0
    usage = Usage()
    return (
        ModelClient(cfg, usage, limits={"max_model_calls": max_calls}, transport=httpx.MockTransport(handler)),
        requests,
        usage,
    )


@pytest.mark.asyncio
async def test_real_http_repairs_whole_fields_before_persisting_any_candidate(cfg):
    assertion = "Device stores settings locally only for enterprise installations in region West."
    invalid = {
        "claims": [
            {
                "claim": "background " * 110 + assertion,
                "quote": "x" * 2001,
                "locator": "x" * 201,
                "important": "false",
                "excluded_topic": "false",
                "applicability": {"edition": ["enterprise"]},
            }
        ]
    }
    valid = {
        "doc_kind": "api-reference",
        "claims": [
            {
                "claim": assertion,
                "quote": assertion,
                "locator": "# Settings",
                "important": True,
                "excluded_topic": False,
                "applicability": {"edition": "enterprise", "region": "West"},
            }
        ],
    }
    client, requests, usage = wire_client(cfg, [invalid, valid])
    source = SourceRecord("source-settings", "text", "Settings")
    persisted = []

    def progress(rows):
        persisted.extend(copy.deepcopy(rows))
        raise CandidateBoundary

    with pytest.raises(CandidateBoundary):
        await curate.extract_claims(
            client, source, "# Settings\n" + assertion, "Device settings", [], char_budget=32768, progress=progress
        )
    assert usage.model_calls == 2
    assert len(persisted) == 1
    assert persisted[0]["claim"] == persisted[0]["original_quote"] == assertion
    assert persisted[0]["applicability"] == valid["claims"][0]["applicability"]
    assert persisted[0]["excluded_topic"] is False
    assert requests[0][1] == requests[1][1]
    assert assertion in requests[0][1]
    assert "maximum200" in requests[1][0]  # locator; claim and quote lengths are not limited
    assert "JSON boolean" in requests[1][0]
    assert "applicability.edition" in requests[1][0]
    assert source.reviewed_sections == []  # source verification has not run
    assert source.omitted_sections


@pytest.mark.asyncio
async def test_twelve_invalid_rows_get_bounded_actionable_retry(cfg):
    invalid = {
        "claims": [
            {"claim": "private-value-" * 100, "locator": "private-quote-" * 20, "important": "private-flag"}
            for _ in range(12)
        ]
    }
    valid = {"claims": [{"claim": "Device supports qualified mode only in laboratory installations."}]}
    client, requests, usage = wire_client(cfg, [invalid, valid])
    result = await curate._extract_json(client, "Extract complete facts.\n", "WHOLE SOURCE FRAME", 4500)
    assert result == valid and usage.model_calls == 2
    feedback = requests[1][0].removesuffix("Extract complete facts.\n")
    assert len(feedback) <= curate.EXTRACTION_FEEDBACK_RESERVE
    assert "24 issues total" in feedback and "omitted from feedback" in feedback
    assert "claims[0].locator" in feedback and "never truncate" in feedback
    assert "private-value" not in feedback and "private-quote" not in feedback
    assert requests[0][1] == requests[1][1] == "WHOLE SOURCE FRAME"


@pytest.mark.asyncio
async def test_exhausted_invalid_responses_never_review_or_persist_source(cfg):
    invalid = {"claims": [{"claim": "facts " * 200, "locator": "x" * 201, "important": 1}]}
    client, requests, usage = wire_client(cfg, [invalid] * 3)
    source = SourceRecord("source-settings", "text", "Settings")
    persisted = []
    with pytest.raises(ModelError) as failure:
        await curate.extract_claims(
            client,
            source,
            "# Settings\nComplete qualified source.",
            "Settings",
            [],
            char_budget=4500,
            progress=persisted.extend,
        )
    assert usage.model_calls == 3 and not persisted
    assert source.reviewed_sections == [] and source.omitted_sections
    assert failure.value.diagnostic["operation"] == "source-claim-extraction"
    assert failure.value.diagnostic["issue_count"] == 2
    assert all(len(s) + len(u) <= 4500 for s, u in requests)


@pytest.mark.asyncio
async def test_hard_budget_is_not_field_repair_or_candidate_admission(cfg):
    client, requests, usage = wire_client(cfg, [{"claims": [{"claim": "x", "important": "yes"}]}], max_calls=1)
    with pytest.raises(BudgetExceeded):
        await curate._extract_json(client, "System", "Complete original source", 4500)
    assert len(requests) == usage.model_calls == 1


@pytest.mark.parametrize(
    "row",
    [
        {"claim": 42},
        {"claim": ""},
        {"claim": "valid", "quote": []},
        {"claim": "valid", "locator": None},
        {"claim": "valid", "important": "true"},
        {"claim": "valid", "excluded_topic": 0},
        {"claim": "valid", "applicability": []},
        {"claim": "valid", "applicability": {"edition": True}},
        {"claim": "valid", "applicability": {"undeclared": "needed qualification"}},
        {"claim": "valid", "modality": []},
    ],
)
def test_supplied_fields_are_strict_and_optional_defaults_remain_compatible(row):
    assert curate._extraction_diagnostic({"claims": [row]})["issue_count"]
    assert curate._extraction_diagnostic({"claims": [{"claim": "A complete claim."}]})["issue_count"] == 0


def test_large_malformed_reply_is_fully_counted_without_large_diagnostics():
    diagnostic = curate._extraction_diagnostic({"claims": [None] * 10000})
    assert diagnostic["issue_count"] == 10000
    assert len(diagnostic["validation_issues"]) == 12
    assert diagnostic["omitted_issue_count"] == 9988
    assert len(json.dumps(diagnostic)) < 2000


def test_claim_count_and_claim_or_quote_length_are_not_limited():
    rows = [{"claim": f"Fact {n}: " + "qualified detail " * 100, "quote": "verbatim excerpt " * 200} for n in range(40)]
    assert curate._extraction_diagnostic({"claims": rows})["issue_count"] == 0
    prompt = curate._extraction_user(SourceRecord("s", "text", "Doc"), "body", "topic", [])
    assert "at most 12" not in prompt and "at most1000" not in prompt and "at most2000" not in prompt


@pytest.mark.asyncio
async def test_large_scope_frame_refused_before_call_without_clipping(cfg):
    client, requests, usage = wire_client(cfg, [])
    source = SourceRecord("source-scope", "text", "Manual")
    source.scope = {"scope": "required-condition " * 500}
    with pytest.raises(ModelError, match="frame and repair reserve"):
        await curate.extract_claims(
            client, source, "# Manual\nExact original condition.", "Manual", [], char_budget=4500
        )
    assert not requests and usage.model_calls == 0
    assert source.reviewed_sections == [] and source.omitted_sections


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [4500, 32768])
async def test_complete_selected_source_units_fit_frame_and_repair_reserve(cfg, budget):
    text = (
        "# Settings\n"
        + ("Background remains whole.\n" * (35 if budget == 4500 else 1000))
        + "Enterprise in West only.\n"
    )
    client, requests, usage = wire_client(cfg, [{"claims": []}] * 10)
    source = SourceRecord("source-settings", "text", "Settings")
    await curate.extract_claims(client, source, text, "Settings", [], char_budget=budget)
    extraction = [(s, u) for s, u in requests if "You extract durable" in s]
    assert extraction
    assert all(len(s) + len(u) + curate.EXTRACTION_FEEDBACK_RESERVE <= budget for s, u in extraction)
    assert all("[truncated]" not in u for _, u in extraction)
    assert "Enterprise in West only." in "\n".join(u for _, u in extraction)
    assert not source.omitted_sections


@pytest.mark.asyncio
async def test_changed_grouping_does_not_reuse_locator_review_for_changed_bytes(cfg):
    text = "# Settings\n" + "Whole source qualification.\n" * 100
    source = SourceRecord("source-settings", "text", "Settings " + "label" * 100)
    old = curate.map_source_structure(source, text, char_budget=4500)
    old_locator = old[0][0][0]
    source.reviewed_sections = [old_locator]
    client, requests, usage = wire_client(cfg, [{"claims": []}] * 10)
    await curate.extract_claims(client, source, text, "Settings", [], char_budget=4500)
    assert source.relevance["extraction_boundary_review"]
    assert source.relevance["extraction_boundary_review"][0]["locator"] == old_locator
    assert requests  # former same-name/different-byte reviewed unit was re-examined


@pytest.mark.asyncio
async def test_completed_regrouped_unit_byte_bindings_survive_same_source_resume(cfg):
    text = "# Settings\n" + "Whole source qualification.\n" * 100
    source = SourceRecord("source-settings", "text", "Settings " + "label" * 100)
    client, requests, usage = wire_client(cfg, [{"claims": []}] * 10)
    await curate.extract_claims(client, source, text, "Settings", [], char_budget=4500)
    assert source.relevance["extraction_reviewed_unit_hashes"] and not source.omitted_sections
    complete = copy.deepcopy(source)
    count = usage.model_calls
    await curate.extract_claims(client, source, text, "Settings", [], char_budget=4500)
    assert usage.model_calls == count
    assert source.reviewed_sections == complete.reviewed_sections
    assert source.relevance.get("extraction_boundary_review", []) == complete.relevance.get(
        "extraction_boundary_review", []
    )
