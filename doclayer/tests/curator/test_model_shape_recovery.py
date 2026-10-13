"""Model-shape recovery from two live failures.

A. Place stage: a seven-claim coverage batch for a model page passed the
   introduction view, then failed twice on the "Preparedness Framework
   Ratings" view: first three replies with a wrong
   claim-ID set (feedback never said which IDs), then three whose
   verdict for one claim had a non-literal anchor and qualifier. That
   passage reads "- **High** for Biological and Chemical capability", so a
   quote without the Markdown markup was rejected. Only an anchor-only fault
   used to fall back to ordinary placement.
B. Validate stage (scope repair of a model page, section "Model Selection
   Context", two claims): disposition 0 was
   already_present with the 196-character bullet as it reads AFTER the reply's
   own citation-removing edit, so it occurs 0 times in the original.

The curator keeps no reply bytes (value-free diagnostics), so the replies here
are reconstructed from the retained diagnostics: field, length 196,
original_occurrences 0, edit_count 4, expected_claim_count 2.
"""

import json

import httpx
import pytest

from doclayer.curator import curate, editing, placement
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.state import Evidence, PageChange, Usage

# --------------------------------------------------------------------------- #
# Validate stage: bounded edit claims nonexistent prior knowledge
# --------------------------------------------------------------------------- #

LATEST = "https://developers.openai.com/api/docs/guides/latest-model"
SELECTION = "https://developers.openai.com/api/docs/guides/model-selection"
CHECKLIST = "https://developers.openai.com/api/docs/guides/deployment-checklist"
ASTRA_BULLET = (
    "*   **GPT-6 Astra:** Highest intelligence in the GPT-6 family; for the most demanding "
    "reasoning, coding, and professional work [source: " + LATEST + "]"
)
GPT56 = (
    "The GPT-5.6 family uses a tiered naming scheme: the `gpt-5.6` alias routes to `gpt-5.6-sol` "
    "(flagship capability), while `gpt-5.6-terra` targets strong performance at lower cost and "
    "`gpt-5.6-luna` targets efficient high-volume workloads [source: " + LATEST + "]."
)
SECTION = (
    "## Model Selection Context\n\n" + ASTRA_BULLET + " [source: " + SELECTION + "] [source: " + CHECKLIST + "].\n"
    "*   **GPT-6.1 Sol:** Optimized for complex tasks where time and cost management are required; "
    "near-Astra performance at lower cost [source: " + SELECTION + "] [source: " + LATEST + "].\n"
    "*   **GPT-6 Luna:** Smart and efficient model for scoped tasks, triage, and frequent automations "
    "[source: " + SELECTION + "] [source: " + CHECKLIST + "].\n\n"
    "Astra and GPT-6.1 Sol require the Responses API for tool calling [source: " + CHECKLIST + "].\n\n" + GPT56 + "\n"
)
ASTRA_ID, GPT56_ID = "claim-7ad9156fd487b7f6", "claim-a16d7f6e886fe4d8"
# The live rejected anchor: the Astra bullet after the reply removed its excluded citations.
REJECTED_ANCHOR = ASTRA_BULLET + "."


def live_section():
    return editing.page_units("# GPT-6 Astra\n\nIntro.\n\n" + SECTION)[1]


def scope_repair_reply(unit, dispositions):
    """Four citation-removing edits, as the live scope repair returned."""
    edits = [
        {
            "old": "work [source: " + LATEST + "] [source: " + SELECTION + "] [source: " + CHECKLIST + "].",
            "new": "work [source: " + LATEST + "].",
        },
        {"old": "cost [source: " + SELECTION + "] [source: " + LATEST + "].", "new": "cost [source: " + LATEST + "]."},
        {"old": "automations [source: " + SELECTION + "] [source: " + CHECKLIST + "].", "new": "automations."},
        {"old": "tool calling [source: " + CHECKLIST + "].", "new": "tool calling."},
    ]
    return {
        "section_id": unit.id,
        "edits": edits,
        "append": "",
        "handled_claim_ids": [item["id"] for item in dispositions],
        "claim_dispositions": dispositions,
        "notes": [],
    }


def test_scope_repair_reply_shape_matches_retained_diagnostic():
    unit = live_section()
    assert len(REJECTED_ANCHOR) == 196 and unit.text.count(REJECTED_ANCHOR) == 0
    reply = scope_repair_reply(
        unit,
        [
            {"id": ASTRA_ID, "status": "already_present", "anchor": REJECTED_ANCHOR},
            {"id": GPT56_ID, "status": "already_present", "anchor": GPT56},
        ],
    )
    with pytest.raises(ModelError, match="nonexistent prior knowledge") as caught:
        editing.apply_edits(unit, reply, {ASTRA_ID, GPT56_ID}, max_output_chars=6000)
    (issue,) = caught.value.diagnostic["issues"]
    assert issue["field"] == "claim_dispositions[0].anchor" and issue["length"] == 196
    assert issue["original_occurrences"] == 0 and issue["claim_id"] == ASTRA_ID
    assert caught.value.diagnostic["edit_count"] == 4


def test_scope_repair_absent_prior_anchor_is_not_handled_and_rest_is_accepted():
    unit = live_section()
    reply = scope_repair_reply(
        unit,
        [
            {"id": ASTRA_ID, "status": "already_present", "anchor": REJECTED_ANCHOR},
            {"id": GPT56_ID, "status": "already_present", "anchor": GPT56},
        ],
    )
    text = editing.apply_edits(unit, reply, {ASTRA_ID, GPT56_ID}, max_output_chars=6000, allow_partial=True)
    assert text.count(REJECTED_ANCHOR) == 1 and SELECTION not in text and CHECKLIST not in text
    # Only the proven claim is handled; the other is dispatched again.
    assert reply["handled_claim_ids"] == [GPT56_ID]
    assert [item["id"] for item in reply["claim_dispositions"]] == [GPT56_ID]


def test_scope_repair_no_claim_left_still_fails_with_named_value_feedback():
    unit = live_section()
    reply = scope_repair_reply(unit, [{"id": ASTRA_ID, "status": "already_present", "anchor": REJECTED_ANCHOR}])
    with pytest.raises(ModelError, match="nonexistent prior knowledge") as caught:
        editing.apply_edits(unit, reply, {ASTRA_ID}, max_output_chars=6000, allow_partial=True)
    feedback = editing._edit_feedback(caught.value, 1000, 6000, result=reply)
    assert len(feedback) <= 1000
    assert "claim_dispositions[0].anchor (claim " + ASTRA_ID + ")" in feedback
    assert json.dumps(REJECTED_ANCHOR[: editing.FEEDBACK_VALUE_CHARS] + "...") in feedback
    assert "not in the ORIGINAL section" in feedback
    # Persisted diagnostics never carry the value.
    assert REJECTED_ANCHOR[:40] not in json.dumps(caught.value.diagnostic)


@pytest.mark.parametrize(
    "anchor",
    [
        # whole sentences (the bullet line up to its citation), markup or whitespace differing
        "GPT-6 Astra: Highest intelligence in the GPT-6 family; for the most demanding reasoning, coding, and professional work",
        "GPT-6 Astra:  Highest intelligence in the GPT-6\nfamily; for the most demanding reasoning, coding, and professional work",
        (
            "The GPT-5.6 family uses a tiered naming scheme: the gpt-5.6 alias routes to gpt-5.6-sol (flagship capability), while "
            "gpt-5.6-terra targets strong performance at lower cost and gpt-5.6-luna targets efficient high-volume workloads"
        ),
        "- GPT-6.1 Sol: Optimized for complex tasks where time and cost management are required; near-Astra performance at lower cost",
    ],
)
def test_normalised_already_present_anchor_is_accepted(anchor):
    unit = live_section()
    reply = {
        "section_id": unit.id,
        "edits": [],
        "append": "",
        "handled_claim_ids": ["claim"],
        "claim_dispositions": [{"id": "claim", "status": "already_present", "anchor": anchor}],
    }
    assert editing.apply_edits(unit, reply, {"claim"}, max_output_chars=6000) == unit.text


@pytest.mark.parametrize(
    "anchor",
    [
        "[source: " + SELECTION + "]",  # ambiguous: occurs in several bullets
        "**",  # nothing left once normalised
        "GPT-6 Astra: Highest intelligence in the GPT-7 family",  # different fact
    ],
)
def test_ambiguous_or_different_already_present_anchor_is_still_rejected(anchor):
    unit = live_section()
    reply = {
        "section_id": unit.id,
        "edits": [],
        "append": "",
        "handled_claim_ids": ["claim"],
        "claim_dispositions": [{"id": "claim", "status": "already_present", "anchor": anchor}],
    }
    with pytest.raises(ModelError):
        editing.apply_edits(unit, reply, {"claim"}, max_output_chars=6000, allow_partial=True)


def test_normalisation_keeps_intraword_markers():
    assert editing.normalised_text("**Rate:** 2*3 `snake_case`") == "Rate: 2*3 snake_case"
    assert editing.normalised_text("**High**\n  for") == "High for"
    assert editing.normalised_text("treated as:\n- **Critical** in\n  1. x") == "treated as: Critical in x"


def test_literal_span_returns_exact_original_bytes():
    text = "treated as:\n- **Critical** in cybersecurity [s]\n- **High** for Biological and Chemical capability [s]\n"
    span = editing.proof_span("High for Biological and Chemical capability", text)
    assert span == "**High** for Biological and Chemical capability" and span in text
    assert (
        editing.proof_span("treated as: Critical in cybersecurity", text)
        == "treated as:\n- **Critical** in cybersecurity"
    )
    assert editing.proof_span("treated as: High for", text) is None  # not contiguous
    assert editing.proof_span("**", text) is None


def b_record(text):
    return Evidence(
        "src-0bc1ba3429", LATEST, "#Models", "paraphrase", text, url=LATEST, quote=text, support_status="supported"
    )


class ScopeRepairEditor:
    """First reply is the live B shape; the re-dispatched claim proves the cleaned bullet."""

    def __init__(self):
        self.edit_users = []

    async def chat_json(self, system, user):
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
            section = next(row for row in frame["sections"] if row["heading_path"].endswith("Model Selection Context"))
            ids = [line.split("[", 1)[1].split(" ", 1)[0] for line in user.splitlines() if line.startswith("- [claim-")]
            return {
                "routes": [
                    {
                        "id": identity,
                        "section_id": section["section_id"],
                        "new_heading": None,
                        "rationale": "fits model selection",
                    }
                    for identity in ids
                ]
            }
        self.edit_users.append(user)
        section_id = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
        listed = user.split("Claim IDs to integrate or prove already present: ", 1)[1].split("\n", 1)[0]
        if len(self.edit_users) == 1:
            unit = next(unit for unit in editing.page_units(PAGE) if unit.id == section_id)
            ids = sorted(json.loads(listed.replace("'", '"')))
            assert len(ids) == 2
            astra, gpt56 = ids if ASTRA_TEXT_ID == ids[0] else ids[::-1]
            return scope_repair_reply(
                unit,
                [
                    {"id": astra, "status": "already_present", "anchor": REJECTED_ANCHOR},
                    {"id": gpt56, "status": "already_present", "anchor": GPT56},
                ],
            )
        (identity,) = json.loads(listed.replace("'", '"'))
        return {
            "section_id": section_id,
            "edits": [],
            "append": "",
            "handled_claim_ids": [identity],
            "claim_dispositions": [{"id": identity, "status": "already_present", "anchor": REJECTED_ANCHOR}],
        }


PAGE = "# GPT-6 Astra\n\nIntro.\n\n" + SECTION
ASTRA_RECORD = b_record(
    "GPT-6 Astra is the highest-intelligence model in the GPT-6 family, for the most "
    "demanding reasoning, coding, and professional work."
)
GPT56_RECORD = b_record(
    "The GPT-5.6 family uses a tiered naming: the gpt-5.6 alias routes to gpt-5.6-sol, with "
    "gpt-5.6-terra for lower cost and gpt-5.6-luna for high volume."
)
ASTRA_TEXT_ID = curate.evidence_id(ASTRA_RECORD)


@pytest.mark.asyncio
async def test_scope_repair_edit_loop_redispatches_the_unproven_claim_instead_of_failing():
    records = [ASTRA_RECORD, GPT56_RECORD]
    ids = [curate.evidence_id(record) for record in records]
    change = PageChange("g", "wiki/gpt-6-astra.md", "update", title="GPT-6 Astra", claim_ids=ids)
    model = ScopeRepairEditor()
    result = await editing.edit_page(
        model, "GPT-6 model differences", change, records, PAGE, [], char_budget=16000, today="2026-10-06"
    )
    assert len(model.edit_users) == 2
    second = model.edit_users[1].split("Claim IDs to integrate or prove already present: ", 1)[1].split("\n", 1)[0]
    assert ASTRA_TEXT_ID in second and curate.evidence_id(GPT56_RECORD) not in second
    assert REJECTED_ANCHOR in model.edit_users[1]  # the redispatch sees the cleaned bullet
    assert set(result["draft_progress"]["handled_claim_ids"]) == set(ids)
    assert SELECTION not in result["body"] and CHECKLIST not in result["body"]


# --------------------------------------------------------------------------- #
# Place stage: invalid bounded placement response (coverage batch)
# --------------------------------------------------------------------------- #

SOL = "https://deploymentsafety.openai.com/gpt-6-1-sol"
SOL_CLAIMS = {
    "claim-123c1bc315f06213": "GPT-6.1 Sol delivers capabilities comparable to GPT-6 Astra (OpenAI's most powerful model) with an unmatched combination of speed and affordability.",
    "claim-7a9fe7e0ec454f8a": "This document is a system card addendum providing updates on baseline safety metrics and evaluation results for GPT-6.1 Sol.",
    "claim-cf6f35d56d892824": "Evaluations of GPT-6.1 Sol were performed in OpenAI's research environment or via its API, which may produce slightly different output from production ChatGPT due to differences in system prompts, tools available, and efforts.",
    "claim-989170baa6ee00fe": "Under OpenAI's Preparedness Framework, GPT-6.1 Sol is treated as Critical in cybersecurity.",
    "claim-018c3f3667f33704": "Under OpenAI's Preparedness Framework, GPT-6.1 Sol is treated as High for Biological and Chemical capability.",
    "claim-c3ec57801f7aa08a": "Based on its Preparedness Framework assessment, GPT-6.1 Sol uses the same safeguards stack as GPT-6 Astra.",
    "claim-74af8a542296923a": "The safeguards stack shared with GPT-6 Astra is described in detail in the GPT-6 Astra system card at deploymentsafety.openai.com/gpt-6-astra/safeguards.",
}
BIO = "claim-018c3f3667f33704"
# The live canonical introduction passage (wiki/gpt-6-1-sol.md at d6f7ac6).
SOL_INTRO = (
    "# GPT-6.1 Sol\n\nGPT-6.1 Sol is a model in the GPT-6.1 family, the latest model family in the GPT-6 series "
    "[source: " + SOL + "]. It delivers capabilities comparable to GPT-6 Astra, OpenAI's most powerful model, with "
    "an unmatched combination of speed and affordability [source: " + SOL + "]. The source document is a system card "
    "addendum providing updates on baseline safety metrics and evaluation results for GPT-6.1 Sol [source: "
    + SOL
    + "]. Based on its Preparedness Framework assessment, GPT-6.1 Sol uses the same safeguards stack as GPT-6 Astra, "
    "described in detail in the GPT-6 Astra system card [source: " + SOL + "].\n\n"
)
SOL_BODY = SOL_INTRO + (
    "## Preparedness Framework Ratings\n\nUnder OpenAI's Preparedness Framework, GPT-6.1 Sol is "
    "treated as:\n- **Critical** in cybersecurity [source: " + SOL + "]\n- **High** for Biological "
    "and Chemical capability [source: " + SOL + "]\n"
)
SUPPORT = {
    "claim-123c1bc315f06213": "It delivers capabilities comparable to GPT-6 Astra, OpenAI's most powerful model, with an unmatched combination of speed and affordability",
    "claim-7a9fe7e0ec454f8a": "The source document is a system card addendum providing updates on baseline safety metrics and evaluation results for GPT-6.1 Sol",
    "claim-c3ec57801f7aa08a": "Based on its Preparedness Framework assessment, GPT-6.1 Sol uses the same safeguards stack as GPT-6 Astra",
    "claim-74af8a542296923a": "described in detail in the GPT-6 Astra system card",
}


def sol_records():
    return [
        Evidence.from_dict(
            {
                "source_id": "src-3112645f16",
                "label": SOL,
                "locator": "introduction",
                "modality": "paraphrase",
                "text": text,
                "doc_kind": "system-card",
                "url": SOL,
                "claim_id": identity,
                "role": "primary",
                "applicability": {"model": "GPT-6.1 Sol", "version": "6.1", "provider": "OpenAI"},
                "quote": text,
                "support_status": "supported",
            }
        )
        for identity, text in SOL_CLAIMS.items()
    ]


def batch_client(cfg, respond):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        system, user = [message["content"] for message in payload["messages"]]
        requests.append((system, user))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(respond(system, user))}, "finish_reason": "stop"}]},
        )

    return ModelClient(cfg, Usage(), limits={"max_model_calls": 50}, transport=httpx.MockTransport(handler)), requests


def intro_verdicts(user, *, bio=None, drop=None):
    passage = user.split("Canonical passage:\n", 1)[1]
    rows = []
    for identity in SOL_CLAIMS:
        if identity == drop:
            continue
        anchor = SUPPORT.get(identity) if SUPPORT.get(identity, "\0") in passage else ""
        rows.append(
            {
                "id": identity,
                "covered": bool(anchor),
                "consistent": True,
                "anchor": anchor,
                "qualifiers": [],
                "reason": "canonical review",
            }
        )
        if identity == BIO and bio is not None:
            rows[-1].update(bio)
    return {"results": rows}


# A reply asserting coverage with text the passage never contains (not only markup).
BIO_HALLUCINATION = {
    "covered": True,
    "anchor": "GPT-6.1 Sol is treated as High for Biological and Chemical capability",
    "qualifiers": ["Under our Preparedness Framework, we are treating GPT-6.1 Sol as High"],
    "reason": "rating present",
}


@pytest.mark.asyncio
async def test_place_stage_nonliteral_anchor_and_qualifier_leave_batch_unproven_not_failed(cfg):
    records = sol_records()
    ledger = {"claims": {identity: {"status": "pending"} for identity in SOL_CLAIMS}}
    client, requests = batch_client(cfg, lambda system, user: intro_verdicts(user, bio=BIO_HALLUCINATION))
    await placement._covered_many(
        client,
        records,
        "wiki/gpt-6-1-sol.md",
        {"wiki/gpt-6-1-sol.md": ({"title": "GPT-6.1 Sol"}, SOL_BODY)},
        ledger,
        32768,
        lambda: None,
    )
    assert len(requests) == 3
    for row in ledger["claims"].values():
        assert row["status"] == "pending" and "coverage" not in row
        (attempt,) = row["unproven_coverage_attempts"].values()
        assert attempt["diagnostic"]["literal_issue_count"] == attempt["diagnostic"]["issue_count"] == 2
        assert "repair_hints" not in attempt["diagnostic"]
        assert BIO_HALLUCINATION["anchor"] not in json.dumps(attempt["diagnostic"])
    feedback = requests[1][0].split("MANDATORY JSON REPAIR: ", 1)[1]
    assert BIO + ": anchor " + json.dumps(BIO_HALLUCINATION["anchor"]) + " is not in the canonical passage" in feedback
    assert BIO + ": qualifiers[0] " + json.dumps(BIO_HALLUCINATION["qualifiers"][0]) in feedback


def preparedness_verdicts(user):
    """Run 2's view-1 shape: rating quotes without the passage's Markdown markup."""
    passage = user.split("Canonical passage:\n", 1)[1]
    if "## Preparedness Framework Ratings" not in passage:
        return intro_verdicts(user)
    rows = [
        {
            "id": identity,
            "covered": False,
            "consistent": True,
            "anchor": "",
            "qualifiers": [],
            "reason": "not in this region",
        }
        for identity in SOL_CLAIMS
    ]
    for row in rows:
        if row["id"] == BIO:
            row.update(
                covered=True,
                anchor="High for Biological and Chemical capability",
                qualifiers=["Under OpenAI's Preparedness Framework, GPT-6.1 Sol is treated as:"],
            )
        if row["id"] == "claim-989170baa6ee00fe":
            row.update(covered=True, anchor="treated as: Critical in cybersecurity")
    return {"results": rows}


@pytest.mark.asyncio
async def test_place_stage_markup_free_rating_quotes_are_accepted_as_exact_passage_spans(cfg):
    records = sol_records()
    ledger = {"claims": {identity: {"status": "pending"} for identity in SOL_CLAIMS}}
    client, requests = batch_client(cfg, lambda system, user: preparedness_verdicts(user))
    await placement._covered_many(
        client,
        records,
        "wiki/gpt-6-1-sol.md",
        {"wiki/gpt-6-1-sol.md": ({"title": "GPT-6.1 Sol"}, SOL_BODY)},
        ledger,
        32768,
        lambda: None,
    )
    assert len(requests) == 2  # one reply per view, no repair round
    bio = ledger["claims"][BIO]
    assert bio["status"] == "covered"
    assert bio["coverage"]["anchor"] == "**High** for Biological and Chemical capability"
    assert bio["coverage"]["anchor"] in SOL_BODY
    assert all(quote in SOL_BODY for quote in bio["coverage"]["qualifiers"])
    cyber = ledger["claims"]["claim-989170baa6ee00fe"]["coverage"]["anchor"]
    assert cyber == "treated as:\n- **Critical** in cybersecurity" and cyber in SOL_BODY


@pytest.mark.asyncio
async def test_place_stage_unproven_batch_claims_take_ordinary_placement(cfg):
    records = sol_records()

    def respond(system, user):
        if user.startswith("CANONICAL CLAIM COVERAGE BATCH"):
            return intro_verdicts(user, bio=BIO_HALLUCINATION)
        selected = json.loads(user.split("Claims JSON:\n")[1])
        return {
            "pages": [
                {
                    "action": "update",
                    "path": "wiki/gpt-6-1-sol.md",
                    "title": "GPT-6.1 Sol",
                    "subject_type": "model",
                    "rationale": "model facts",
                    "source_ids": ["src-3112645f16"],
                    "claim_ids": [item["id"] for item in selected],
                    "related": [],
                }
            ],
            "covered": [],
        }

    client, requests = batch_client(cfg, respond)
    ledger = {"claims": {identity: {"status": "pending", "target": "wiki/gpt-6-1-sol.md"} for identity in SOL_CLAIMS}}
    result = await placement.plan_batch(
        client,
        "GPT-6.1 Sol",
        [],
        records,
        [("wiki/gpt-6-1-sol.md", {"title": "GPT-6.1 Sol"}, SOL_BODY)],
        [],
        char_budget=32768,
        ledger=ledger,
    )
    assert len(requests) == 4
    assert set(result["planned"]) == set(SOL_CLAIMS) and result["covered"] == []


@pytest.mark.asyncio
async def test_place_stage_wrong_claim_id_set_feedback_names_missing_ids_and_still_stops(cfg):
    records = sol_records()
    ledger = {"claims": {identity: {"status": "pending"} for identity in SOL_CLAIMS}}
    client, requests = batch_client(cfg, lambda system, user: intro_verdicts(user, drop=BIO))
    with pytest.raises(ModelError, match="invalid bounded placement response") as caught:
        await placement._covered_many(
            client, records, "wiki/gpt-6-1-sol.md", {"wiki/gpt-6-1-sol.md": ({}, SOL_BODY)}, ledger, 32768, lambda: None
        )
    assert len(requests) == 3
    issue = 'expected 7 results, got 6; missing ["' + BIO + '"]'
    assert issue in requests[1][0] and issue in requests[2][0]
    assert caught.value.diagnostic["validation_issues"] == [
        "results must contain every supplied claim ID exactly once: " + issue
    ]
    assert not any(row.get("unproven_coverage_attempts") for row in ledger["claims"].values())


@pytest.mark.asyncio
async def test_place_stage_wrong_claim_id_set_recovers_when_feedback_is_followed(cfg):
    records = sol_records()
    ledger = {"claims": {identity: {"status": "pending"} for identity in SOL_CLAIMS}}

    def respond(system, user):
        missing = 'missing ["' + BIO + '"]' in system
        return intro_verdicts(user, drop=None if missing else BIO)

    client, requests = batch_client(cfg, respond)
    await placement._covered_many(
        client, records, "wiki/gpt-6-1-sol.md", {"wiki/gpt-6-1-sol.md": ({}, SOL_INTRO)}, ledger, 32768, lambda: None
    )
    assert len(requests) == 2
    assert {identity for identity, row in ledger["claims"].items() if row["status"] == "covered"} == set(SUPPORT)


@pytest.mark.parametrize(
    "reply,expected",
    [
        ({"pages": [], "covered": []}, "missing"),
        (
            {
                "pages": [
                    {
                        "action": "update",
                        "path": "wiki/device.md",
                        "title": "Device",
                        "subject_type": "gadget",
                        "rationale": "r",
                        "source_ids": ["manual"],
                        "claim_ids": ["ID"],
                        "related": [],
                    }
                ],
                "covered": [],
            },
            "pages[0].subject_type must be one of",
        ),
        (
            {
                "pages": [
                    {
                        "action": "update",
                        "path": "wiki/device.md",
                        "title": "Device",
                        "subject_type": "product",
                        "rationale": "r",
                        "source_ids": [],
                        "claim_ids": ["ID"],
                        "related": [],
                    }
                ],
                "covered": [],
            },
            'pages[0].source_ids must include the sources of its claims: missing ["manual"]',
        ),
    ],
)
@pytest.mark.asyncio
async def test_placement_plan_feedback_names_the_rejected_field(reply, expected):
    from test_placement import records as device_records

    evidence = device_records()
    identity = curate.evidence_id(evidence[0])
    calls = []

    class Client:
        async def chat_bounded_json(self, system, user, *, char_budget):
            calls.append(system)
            return json.loads(json.dumps(reply).replace('"ID"', json.dumps(identity)))

    with pytest.raises(ModelError, match="invalid bounded placement response"):
        await placement.plan_batch(Client(), "Device", [], evidence, [], [], char_budget=7000)
    assert len(calls) == 3 and expected in calls[1]
    if expected == "missing":
        assert 'missing ["' + identity + '"]' in calls[1]


# --------------------------------------------------------------------------- #
# Markup-only normalisation, feedback that always fits
# --------------------------------------------------------------------------- #

CONTENT_DIGIT_PROBES = [
    # (canonical passage, model anchor): digits and punctuation are content here
    (
        "The default limit is\n100. Requests above it are rejected.",
        "The default limit is\n500. Requests above it are rejected.",
    ),
    ("Retention\n30) days", "Retention\n90) days"),
    ("The default limit is\n100. Requests above it are rejected.", "500. Requests above it are rejected."),
    ("Steps:\n\n1. Enable SSO\n2. Enable MFA\n", "3. Enable MFA"),
    ("limit 5 GB per day", "- 5 GB per day"),
    ("Use `*` to match all hosts", "Use to match all hosts"),
]


@pytest.mark.parametrize("passage,anchor", CONTENT_DIGIT_PROBES)
def test_content_digits_and_code_span_content_never_normalise_away(passage, anchor):
    assert editing.proof_span(anchor, passage) is None
    diagnostic = placement._coverage_diagnostic(
        {"covered": True, "consistent": True, "anchor": anchor, "qualifiers": [anchor], "reason": "r"}, passage, []
    )
    assert diagnostic["issue_count"] == diagnostic["literal_issue_count"] == 2
    assert editing._prior_anchor(anchor, passage, passage) == "absent"


@pytest.mark.parametrize(
    "passage,anchor,span",
    [
        ("Steps:\n\n1. Enable SSO\n2. Enable MFA\n", "Enable SSO Enable MFA", "Enable SSO\n2. Enable MFA"),
        ("Steps:\n\n1. Enable SSO\n2. Enable MFA\n", "1. Enable SSO\n2. Enable MFA", "Enable SSO\n2. Enable MFA"),
        ("Rules:\n> **Never** share keys\n", "Never share keys", "**Never** share keys"),
        (
            "See the [guide](https://x.example/a) for details.",
            "See the guide for details.",
            "See the [guide](https://x.example/a) for details.",
        ),
        ("Use `*` to match all hosts", "Use `*` to match all hosts", "Use `*` to match all hosts"),
    ],
)
def test_markup_only_differences_match_exact_original_bytes(passage, anchor, span):
    assert editing.proof_span(anchor, passage) == span
    assert editing._prior_anchor(anchor, passage, passage) in ("exact", "normalised")


def test_diagnostics_cut_model_supplied_ids_and_keys():
    long = "x" * 200
    issue = placement._batch_identity_issue({"results": [{"id": long}]}, ["claim-a"])
    assert "x" * 64 in issue and "x" * 65 not in issue
    issue = placement._batch_identity_issue({long: []}, ["claim-a"])
    assert "x" * 64 in issue and "x" * 65 not in issue


def test_repair_feedback_keeps_whole_hints_then_counts_the_rest():
    issues = ["claim-a: anchor must be nonempty exact current passage text when covered=true"]
    hints = [
        "claim-" + str(i) + ": anchor " + json.dumps("v" * 100) + " is not in the canonical passage" for i in range(10)
    ]
    full = placement._repair_feedback(issues, hints, 100000)
    assert all(hint in full for hint in hints)
    tight = placement._repair_feedback(issues, hints, len(full) - 1)
    assert "... 1 more" in tight and hints[8] in tight and hints[9] not in tight
    generic = placement._repair_feedback(["i" * 500], [], 300)
    assert placement.GENERIC_REPAIR in generic and len(generic) <= 300
    assert placement._repair_feedback(issues, hints, 50) is None


def test_hint_excerpt_is_bounded_after_json_escaping():
    assert len(placement._excerpt("\x01" * 500)) <= placement.HINT_VALUE_CHARS + 5
    assert len(placement._excerpt("é" * 500)) <= placement.HINT_VALUE_CHARS + 5


def batch_claims(user):
    return json.loads(user.split("Claims JSON:\n")[1].split("\nPage metadata:")[0])


class Counting:
    def __init__(self, respond):
        self.respond, self.calls = respond, []

    async def chat_bounded_json(self, system, user, *, char_budget):
        assert len(system) + len(user) <= char_budget
        self.calls.append((system, user))
        return self.respond(system, user)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [12, 30])
async def test_plan_feedback_never_stops_a_retryable_fault_at_packed_budgets(count):
    # Regression: 63 of these 300 budgets once failed after 1 call with "cannot fit".
    from test_placement import records as device_records

    evidence = device_records(count)
    for budget in range(1500, 9000, 25):
        client = Counting(lambda system, user: {"pages": [], "covered": []})
        with pytest.raises(ModelError) as caught:
            await placement.plan_batch(client, "Device", [], evidence, [], [], char_budget=budget)
        if str(caught.value) == "whole placement claim cannot fit bounded context":
            assert not client.calls
            continue
        assert str(caught.value) == "invalid bounded placement response", budget
        assert len(client.calls) == 3, budget


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [3, 12])
@pytest.mark.parametrize("value", ["invented plain canonical text " * 3, "ctl\x01" * 40])
async def test_coverage_literal_hints_never_stop_the_fallback_at_packed_budgets(count, value):
    from test_placement import records as device_records

    evidence = device_records(count)
    ids = [curate.evidence_id(record) for record in evidence]
    body = "".join(f"## Region {i}\n\n" + "Unrelated canonical text. " * 30 + "\n\n" for i in range(3))

    def verdict(identity):
        return {"covered": True, "consistent": True, "anchor": value + identity, "qualifiers": [value], "reason": "r"}

    def respond(system, user):
        if user.startswith("CANONICAL CLAIM COVERAGE BATCH"):
            return {"results": [{"id": item["id"], **verdict(item["id"])} for item in batch_claims(user)]}
        return verdict(json.loads(user.split("Claim JSON:\n")[1].split("\nPage metadata:")[0])["id"])

    exercised = batched = 0
    for budget in range(4000, 16000, 100):
        ledger = {"claims": {identity: {"status": "pending"} for identity in ids}}
        client = Counting(respond)
        try:
            await placement._covered_many(
                client, evidence, "wiki/device.md", {"wiki/device.md": ({}, body)}, ledger, budget, lambda: None
            )
        except ModelError as exc:
            assert "exceed" in str(exc) and not client.calls, (budget, str(exc))
            continue
        exercised += 1
        batched += any(user.startswith("CANONICAL CLAIM COVERAGE BATCH") for _, user in client.calls)
        assert client.calls and len(client.calls) % 3 == 0, budget  # every request got all 3 attempts
        assert all("MANDATORY JSON REPAIR" in system for system, _ in client.calls[1::3])
        # Claims stay pending for ordinary placement; an unproven group ends the pass.
        assert all(row["status"] == "pending" and "coverage" not in row for row in ledger["claims"].values())
        assert any(row.get("unproven_coverage_attempts") for row in ledger["claims"].values())
    assert exercised > 50 and batched > 20


@pytest.mark.parametrize(
    "anchor,passage,span",
    [
        (
            "High for Biological and Critical",
            "- **High** for Biological and **Critical** in cybersecurity",
            "**High** for Biological and **Critical**",
        ),
        ("High and Critical", "Rated **High** and **Critical** overall", "**High** and **Critical**"),
    ],
)
def test_proof_span_pairs_dangling_delimiters_at_each_end(anchor, passage, span):
    assert editing.proof_span(anchor, passage) == span
    verdict = placement._canonical_verdict(
        {"covered": True, "consistent": True, "anchor": anchor, "qualifiers": [anchor], "reason": "r"}, passage, []
    )
    assert verdict["anchor"] == verdict["qualifiers"][0] == span


@pytest.mark.asyncio
async def test_retry_plan_feedback_fits_at_every_packed_budget():
    # Regression: 11 of these 300 budgets once stopped after 1 retry-plan attempt.
    from test_placement import records as device_records

    evidence = device_records(12)
    body = "## Facts\n\nUnrelated canonical text.\n"
    assert placement.PLAN_FEEDBACK_RESERVE >= len(placement.RETRY_PLAN_SUFFIX) + len(
        placement._repair_feedback([placement.GENERIC_REPAIR], [], 1 << 30)
    )

    class Client:
        def __init__(self):
            self.retry = 0

        async def chat_bounded_json(self, system, user, *, char_budget):
            assert len(system) + len(user) <= char_budget
            if placement.RETRY_PLAN_SUFFIX in system:
                self.retry += 1
                return {"pages": [], "covered": []}
            if user.startswith("CANONICAL CLAIM COVERAGE BATCH"):
                return {
                    "results": [
                        {
                            "id": item["id"],
                            "covered": False,
                            "consistent": True,
                            "anchor": "",
                            "qualifiers": [],
                            "reason": "r",
                        }
                        for item in batch_claims(user)
                    ]
                }
            if user.startswith("CANONICAL CLAIM COVERAGE"):
                return {"covered": False, "consistent": True, "anchor": "", "qualifiers": [], "reason": "r"}
            ids = [item["id"] for item in json.loads(user.split("Claims JSON:\n")[-1])]
            return {"pages": [], "covered": [{"claim_id": identity, "path": "wiki/device.md"} for identity in ids]}

    retried = 0
    for budget in range(1500, 9000, 25):
        client = Client()
        with pytest.raises(ModelError) as caught:
            await placement.plan_batch(
                client, "Device", [], evidence, [("wiki/device.md", {"title": "Device"}, body)], [], char_budget=budget
            )
        if str(caught.value) != "invalid bounded placement response":
            # only up-front non-transient sizing refusals, never a feedback-fit stop
            assert client.retry == 0 and not caught.value.transient and "repair" not in str(caught.value), budget
            continue
        retried += 1
        assert client.retry == 3, budget
    assert retried > 150


# --------------------------------------------------------------------------- #
# Draft stage, 170 calls:
# "bounded edit lacks unique actual claim anchor". New page
# wiki/openai-api-data-retention-controls.md; a two-claim task with one edit.
# claim_dispositions[0] was already_present with a 152-character anchor that
# occurs 0 times in the resulting AND the original section. The likeliest
# reply (exact length match) quotes the parent section's sentence while the
# task edits the MAM subsection, so the anchor is absent, not a markup issue.
# --------------------------------------------------------------------------- #

DATA = "https://developers.openai.com/api/docs/guides/your-data"
C_ANCHOR = (
    "These controls require prior OpenAI approval and acceptance of additional requirements [source: " + DATA + "]"
)
MAM = (
    "MAM excludes customer content from abuse monitoring logs across all API endpoints, with the exception of "
    "image and file inputs in rare cases, while retaining full platform capabilities [source: " + DATA + "]."
)
MAM_SENTENCE = MAM.split(" [source:")[0]
C_PAGE = (
    "# OpenAI API Data Retention Controls\n\n## Zero Data Retention (ZDR) and Modified Abuse Monitoring (MAM)\n\n"
    "Eligible customers may exclude customer content from abuse monitoring logs by obtaining approval for ZDR or MAM. "
    + C_ANCHOR
    + ".\n\n### Modified Abuse Monitoring (MAM)\n\n"
    + MAM
    + "\n\n### Zero Data Retention (ZDR)\n\nZDR text.\n"
)
APPROVAL_ID, MAM_ID = "claim-cbe7d0ef9aebaf2a", "claim-19ae633c5214014f"


def mam_section():
    return next(unit for unit in editing.page_units(C_PAGE) if unit.label.endswith("/ Modified Abuse Monitoring (MAM)"))


def c_reply(unit, dispositions):
    return {
        "section_id": unit.id,
        "append": "",
        "notes": [],
        "edits": [
            {
                "old": "capabilities [source: " + DATA + "].",
                "new": "capabilities [source: " + DATA + "]. See the MAM guide.",
            }
        ],
        "handled_claim_ids": [item["id"] for item in dispositions],
        "claim_dispositions": dispositions,
    }


def test_draft_stage_reply_shape_matches_retained_diagnostic():
    unit = mam_section()
    assert len(C_ANCHOR) == 152 and C_ANCHOR in C_PAGE and C_ANCHOR not in unit.text
    reply = c_reply(
        unit,
        [
            {"id": APPROVAL_ID, "status": "already_present", "anchor": C_ANCHOR},
            {"id": MAM_ID, "status": "already_present", "anchor": MAM_SENTENCE},
        ],
    )
    with pytest.raises(ModelError, match="lacks unique actual claim anchor") as caught:
        editing.apply_edits(unit, reply, {APPROVAL_ID, MAM_ID}, max_output_chars=6000)
    issues = caught.value.diagnostic["issues"]
    assert [
        (row["field"], row.get("occurrences"), row.get("original_occurrences"), row["length"]) for row in issues
    ] == [("claim_dispositions[0].anchor", 0, None, 152), ("claim_dispositions[0].anchor", None, 0, 152)]
    assert caught.value.diagnostic["edit_count"] == 1 and caught.value.diagnostic["expected_claim_count"] == 2


def test_draft_stage_absent_anchor_is_downgraded_and_the_proven_claim_kept():
    unit = mam_section()
    reply = c_reply(
        unit,
        [
            {"id": APPROVAL_ID, "status": "already_present", "anchor": C_ANCHOR},
            {"id": MAM_ID, "status": "already_present", "anchor": MAM_SENTENCE},
        ],
    )
    text = editing.apply_edits(unit, reply, {APPROVAL_ID, MAM_ID}, max_output_chars=6000, allow_partial=True)
    # The unattributed added sentence is not a proven claim's material, so it is not kept.
    assert "See the MAM guide." not in text and reply["handled_claim_ids"] == [MAM_ID]


def test_draft_stage_feedback_names_the_claim_and_the_absent_anchor():
    unit = mam_section()
    reply = c_reply(unit, [{"id": APPROVAL_ID, "status": "already_present", "anchor": C_ANCHOR}])
    with pytest.raises(ModelError, match="lacks unique actual claim anchor") as caught:
        editing.apply_edits(unit, reply, {APPROVAL_ID}, max_output_chars=6000, allow_partial=True)
    feedback = editing._edit_feedback(caught.value, 1000, 6000, result=reply)
    assert (
        "claim_dispositions[0].anchor (claim " + APPROVAL_ID + ') = "These controls require prior OpenAI approval'
        in feedback
    )
    assert "occurs nowhere in this section" in feedback and len(feedback) <= 1000


def disposition_reply(unit, edits, append, dispositions):
    return {
        "section_id": unit.id,
        "edits": edits,
        "append": append,
        "notes": [],
        "handled_claim_ids": [item["id"] for item in dispositions],
        "claim_dispositions": dispositions,
    }


def test_integrated_anchor_accepted_after_markup_normalisation_inside_added_material():
    unit = mam_section()
    reply = disposition_reply(
        unit,
        [],
        "- **Singapore** storage requires `MAM` or ZDR.",
        [{"id": "a", "status": "integrated", "anchor": "Singapore storage requires MAM or ZDR."}],
    )
    assert "**Singapore**" in editing.apply_edits(unit, reply, {"a"}, max_output_chars=6000)


@pytest.mark.parametrize(
    "dispositions,proven_kept",
    [
        # normalised match lies in OLD material, not in what the edit added
        ([{"id": "a", "status": "integrated", "anchor": MAM_SENTENCE}], True),
        # ambiguous: occurs twice in the result; b's paragraph adds only b's anchor sentence, so it stays
        ([{"id": "a", "status": "integrated", "anchor": "requires MAM"}], True),
        # missing anchor: the paragraph a added is not a proven anchor sentence, so it goes
        ([{"id": "a", "status": "integrated", "anchor": ""}], True),
    ],
)
def test_unproven_integrated_anchor_is_downgraded_never_accepted(dispositions, proven_kept):
    unit = mam_section()
    proven = {"id": "b", "status": "integrated", "anchor": "Seoul storage requires MAM or ZDR."}
    reply = disposition_reply(
        unit, [], "Singapore storage requires MAM.\n\nSeoul storage requires MAM or ZDR.", dispositions + [proven]
    )
    if proven_kept:
        text = editing.apply_edits(unit, reply, {"a", "b"}, max_output_chars=6000, allow_partial=True)
        assert reply["handled_claim_ids"] == ["b"] and "Seoul storage" in text and "Singapore" not in text
    else:
        # b's only edit carries part of a not-proven claim: dropped whole, b not handled either
        with pytest.raises(ModelError) as caught:
            editing.apply_edits(unit, reply, {"a", "b"}, max_output_chars=6000, allow_partial=True)
        assert caught.value.unproven_claims["b"] == "inseparable"
        assert "Singapore" not in caught.value.candidate and "Seoul" not in caught.value.candidate
    alone = disposition_reply(unit, [], "Singapore storage requires MAM.\n\nSeoul storage requires MAM.", dispositions)
    with pytest.raises(ModelError):
        editing.apply_edits(unit, alone, {"a"}, max_output_chars=6000, allow_partial=True)


def test_ambiguous_anchor_feedback_names_the_count():
    unit = mam_section()
    reply = disposition_reply(
        unit,
        [],
        "Seoul requires MAM.\n\nTokyo requires MAM.",
        [{"id": "a", "status": "integrated", "anchor": "requires MAM."}],
    )
    with pytest.raises(ModelError) as caught:
        editing.apply_edits(unit, reply, {"a"}, max_output_chars=6000, allow_partial=True)
    feedback = editing._edit_feedback(caught.value, 1000, 6000, result=reply)
    assert 'claim_dispositions[0].anchor (claim a) = "requires MAM." occurs 2 times' in feedback


# --------------------------------------------------------------------------- #
# Proof-span trim
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "span,trimmed",
    [
        ("High** for X", "High for X"),
        ("Alpha](u) and Beta", "Alpha and Beta"),
        ("Alpha](u) and [Beta", "Alpha and Beta"),
        ("for **Critical", "for Critical"),
        ("a `code` b", "a `code` b"),
        ("use `**kwargs` here", "use `**kwargs` here"),
        ("snake_case 2*3", "snake_case 2*3"),
        ("**High** for [guide](u) [source: https://a]", "**High** for [guide](u) [source: https://a]"),
    ],
)
def test_trim_markup_drops_only_unmatched_markup(span, trimmed):
    assert editing._trim_markup(span) == trimmed


@pytest.mark.parametrize(
    "fragment,passage,span",
    [
        ("High for X", "**Rating: High** for X", "High for X"),
        ("Alpha and Beta", "[Alpha](u) and Beta", "Alpha and Beta"),
        ("Alpha and Beta", "[Alpha](u) and [Beta](v)", "Alpha and Beta"),
        ("High for X", "- **High** for X", "**High** for X"),  # adjacent partner: exact bytes kept
    ],
)
def test_proof_span_is_trimmed_markup_normalised_substring(fragment, passage, span):
    assert editing.proof_span(fragment, passage) == span
    assert editing.proof_present(span, passage)


@pytest.mark.asyncio
async def test_trimmed_coverage_proof_is_reused_without_new_calls():
    from test_placement import records as device_records

    record = device_records()[0]
    passage = "# Device\n\n**Rating: " + record.text[:-1] + "** applies.\n"
    ledger = {"claims": {curate.evidence_id(record): {"status": "pending"}}}
    calls = []

    class Client:
        async def chat_bounded_json(self, system, user, *, char_budget):
            calls.append(user)
            return {
                "covered": True,
                "consistent": True,
                "anchor": record.text[:-1] + " applies.",
                "qualifiers": [],
                "reason": "r",
            }

    pages = {"wiki/device.md": ({}, passage)}
    assert await placement._covered(Client(), record, "wiki/device.md", pages, ledger, 32768, lambda: None)
    anchor = ledger["claims"][curate.evidence_id(record)]["coverage"]["anchor"]
    assert anchor == record.text[:-1] + " applies." and anchor not in passage and editing.proof_present(anchor, passage)
    assert await placement._covered(Client(), record, "wiki/device.md", pages, ledger, 32768, lambda: None)
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Concern 2: an already_present anchor found once elsewhere on the page
# --------------------------------------------------------------------------- #

PARENT_LABEL = "OpenAI API Data Retention Controls / Zero Data Retention (ZDR) and Modified Abuse Monitoring (MAM)"


def parent_section():
    return next(unit for unit in editing.page_units(C_PAGE) if unit.label == PARENT_LABEL)


def test_draft_stage_parent_section_sentence_is_accepted_as_already_present():
    page = editing.page_units(C_PAGE)
    unit = next(part for part in page if part.label.endswith("/ Modified Abuse Monitoring (MAM)"))
    reply = c_reply(
        unit,
        [
            {"id": APPROVAL_ID, "status": "already_present", "anchor": C_ANCHOR},
            {"id": MAM_ID, "status": "already_present", "anchor": MAM_SENTENCE},
        ],
    )
    editing.apply_edits(unit, reply, {APPROVAL_ID, MAM_ID}, max_output_chars=6000, allow_partial=True, page=page)
    assert reply["handled_claim_ids"] == [APPROVAL_ID, MAM_ID]
    assert reply["claim_dispositions"][0]["found_in_section"] == {
        "section_id": parent_section().id,
        "heading_path": PARENT_LABEL,
    }
    alone = c_reply(unit, [{"id": APPROVAL_ID, "status": "already_present", "anchor": C_ANCHOR}])
    editing.apply_edits(unit, alone, {APPROVAL_ID}, max_output_chars=6000, allow_partial=True, page=page)
    assert alone["handled_claim_ids"] == [APPROVAL_ID]


def test_markup_normalised_page_anchor_is_accepted():
    body = C_PAGE.replace(
        "These controls require prior OpenAI approval", "These controls require **prior OpenAI approval**"
    )
    page = editing.page_units(body)
    unit = next(part for part in page if part.label.endswith("/ Modified Abuse Monitoring (MAM)"))
    reply = c_reply(
        unit,
        [
            {
                "id": APPROVAL_ID,
                "status": "already_present",
                "anchor": "These controls require prior OpenAI approval and acceptance of additional requirements",
            }
        ],
    )
    editing.apply_edits(unit, reply, {APPROVAL_ID}, max_output_chars=6000, allow_partial=True, page=page)
    assert reply["claim_dispositions"][0]["found_in_section"]["section_id"] == parent_section().id


@pytest.mark.parametrize(
    "body,anchor,append",
    [
        (C_PAGE, "These controls require prior Anthropic approval", ""),  # nowhere on the page
        (C_PAGE.replace("ZDR text.", "ZDR text. " + C_ANCHOR + "."), C_ANCHOR, ""),  # twice on the page
        (C_PAGE, C_ANCHOR, C_ANCHOR + "."),  # the edit itself adds it to this section (a duplicate)
    ],
)
def test_absent_duplicated_or_self_added_page_anchor_is_still_downgraded(body, anchor, append):
    page = editing.page_units(body)
    unit = next(part for part in page if part.label.endswith("/ Modified Abuse Monitoring (MAM)"))
    reply = c_reply(
        unit,
        [
            {"id": APPROVAL_ID, "status": "already_present", "anchor": anchor},
            {"id": MAM_ID, "status": "already_present", "anchor": MAM_SENTENCE},
        ],
    )
    reply["append"] = append
    editing.apply_edits(unit, reply, {APPROVAL_ID, MAM_ID}, max_output_chars=6000, allow_partial=True, page=page)
    assert reply["handled_claim_ids"] == [MAM_ID]
    assert "found_in_section" not in reply["claim_dispositions"][0]


class ParentSentenceEditor:
    """Routes both claims to the MAM subsection; cites the parent's sentence."""

    def __init__(self, ids):
        self.ids, self.edit_calls = ids, 0

    async def chat_json(self, system, user):
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
            section = next(
                row for row in frame["sections"] if row["heading_path"].endswith("/ Modified Abuse Monitoring (MAM)")
            )
            return {
                "routes": [
                    {
                        "id": identity,
                        "section_id": section["section_id"],
                        "new_heading": None,
                        "rationale": "MAM controls",
                    }
                    for identity in self.ids
                ]
            }
        self.edit_calls += 1
        section_id = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
        return {
            "section_id": section_id,
            "edits": [],
            "append": "",
            "notes": [],
            "handled_claim_ids": list(self.ids),
            "claim_dispositions": [
                {"id": self.ids[0], "status": "already_present", "anchor": C_ANCHOR},
                {"id": self.ids[1], "status": "already_present", "anchor": MAM_SENTENCE},
            ],
        }


@pytest.mark.asyncio
async def test_edit_loop_records_the_section_where_the_anchor_was_found():
    records = [
        Evidence("src", DATA, "#Data", "paraphrase", text, url=DATA, quote=text, support_status="supported")
        for text in (
            "ZDR and MAM require prior OpenAI approval and acceptance of additional requirements.",
            "MAM excludes customer content from abuse monitoring logs while retaining full platform capabilities.",
        )
    ]
    ids = [curate.evidence_id(record) for record in records]
    change = PageChange(
        "g", "wiki/openai-api-data-retention-controls.md", "update", title="Data retention", claim_ids=ids
    )
    model = ParentSentenceEditor(ids)
    result = await editing.edit_page(
        model, "Data retention controls", change, records, C_PAGE, [], char_budget=16000, today="2026-10-06"
    )
    assert model.edit_calls == 1
    assert set(result["draft_progress"]["handled_claim_ids"]) == set(ids)
    assert result["draft_progress"]["present_elsewhere"] == {
        ids[0]: {"section_id": parent_section().id, "heading_path": PARENT_LABEL}
    }


# --------------------------------------------------------------------------- #
# already_present anchors are whole sentences that state the claim
# --------------------------------------------------------------------------- #


def two_section_page(other, mine="Section M body text here."):
    return f"# T\n\n## P\n\n{other}\n\n## M\n\n{mine}\n"


def run_probe(page_text, anchor, *, claim=None, append="", edits=None):
    """Reviewer harness: claim a is already_present elsewhere, claim b is integrated."""
    page = editing.page_units(page_text)
    unit = next(part for part in page if part.label.endswith("/ M"))
    reply = {
        "section_id": unit.id,
        "edits": edits or [],
        "notes": [],
        "append": (append + "\n\n" if append else "") + "Brand new sentence B.",
        "handled_claim_ids": ["a", "b"],
        "claim_dispositions": [
            {"id": "a", "status": "already_present", "anchor": anchor},
            {"id": "b", "status": "integrated", "anchor": "Brand new sentence B."},
        ],
    }
    claims = {"a": claim, "b": "Brand new sentence B."} if claim is not None else None
    editing.apply_edits(unit, reply, {"a", "b"}, max_output_chars=6000, allow_partial=True, page=page, claims=claims)
    return reply["handled_claim_ids"], reply["claim_dispositions"][0].get("found_in_section")


DOWNGRADED_PAGE_PROBES = [
    ("number prefix 30 in 300", two_section_page("Logs are retained for 300 days."), "Logs are retained for 30", None),
    ("number suffix 0 days in 30 days", two_section_page("Logs are retained for 30 days."), "0 days.", None),
    (
        "negation: no approval",
        two_section_page("No approval is required for MAM."),
        "approval is required for MAM.",
        None,
    ),
    ("negation: not", two_section_page("ZDR does not require prior approval."), "require prior approval.", None),
    (
        "qualifier dropped",
        two_section_page("MAM is available to enterprise customers only."),
        "MAM is available to enterprise customers",
        None,
    ),
    ("qualifier prefix", two_section_page("Only eligible customers may use ZDR."), "customers may use ZDR.", None),
    (
        "exact 90 vs claim 30",
        two_section_page("Logs are retained for 90 days."),
        "Logs are retained for 90 days.",
        "Logs are retained for 30 days.",
    ),
    ("digit differs", two_section_page("Logs are retained for 90 days."), "Logs are retained for 30 days.", None),
    (
        "twice on page",
        two_section_page("Logs are retained for 90 days. Logs are retained for 90 days."),
        "Logs are retained for 90 days.",
        None,
    ),
    ("long >1500 anchor", two_section_page("X" * 1600 + "."), "X" * 1600, None),
]


@pytest.mark.parametrize(
    "name,page_text,anchor,claim", DOWNGRADED_PAGE_PROBES, ids=[row[0] for row in DOWNGRADED_PAGE_PROBES]
)
def test_page_wide_probe_is_downgraded(name, page_text, anchor, claim):
    assert run_probe(page_text, anchor, claim=claim) == (["b"], None)


def test_page_wide_whole_sentence_stating_the_claim_is_accepted():
    for other in ("Logs are retained for 90 days.", "Logs are **retained** for 90 days."):
        handled, found = run_probe(
            two_section_page(other), "Logs are retained for 90 days.", claim="Logs are retained for 90 days by default."
        )
        assert handled == ["a", "b"] and found["heading_path"] == "T / P"


@pytest.mark.parametrize(
    "edits",
    [
        [{"old": "Section M body text here.", "new": "Section M body text here. Logs are retained for 90 days."}],
        [{"old": "Section M body text here.", "new": "Section M body text here. Logs are *retained* for 90 days."}],
    ],
)
def test_page_wide_self_added_anchor_is_downgraded(edits):
    assert run_probe(
        two_section_page("Logs are retained for 90 days."), "Logs are retained for 90 days.", edits=edits
    ) == (["b"], None)


def test_page_wide_anchor_removed_from_section_is_downgraded():
    page_text = two_section_page("Logs are retained for 90 days.", mine="Logs are retained for 90 days. Other.")
    assert run_probe(
        page_text,
        "Logs are retained for 90 days.",
        edits=[{"old": "Logs are retained for 90 days. Other.", "new": "Other."}],
    ) == (["b"], None)


IN_SECTION_PROBES = [
    # (section sentence, anchor, claim, expected _prior_anchor verdict)
    ("Logs are retained for 300 days.", "Logs are retained for 30", None, "partial"),
    ("No approval is required for MAM.", "approval is required for MAM.", None, "partial"),
    ("MAM is available to enterprise customers only.", "MAM is available to enterprise customers", None, "partial"),
    ("Logs are retained for 90 days.", "Logs are retained for 90 days.", "Logs are retained for 30 days.", "mismatch"),
    ("Logs are retained for 90 days.", "Logs are retained for 90 days.", "Logs are retained for 90 hours.", "mismatch"),
    ("Logs are retained for 90 days.", "Logs are retained for 90 days.", "Logs are retained for 9 days.", "mismatch"),
    (
        "No approval is required for MAM.",
        "No approval is required for MAM.",
        "Approval is required for MAM.",
        "mismatch",
    ),
    (
        "MAM is available to enterprise customers only.",
        "MAM is available to enterprise customers only.",
        "MAM is available to enterprise customers.",
        "mismatch",
    ),
    ("Logs are retained for 90 days.", "Logs are retained for 90 days.", "Logs are kept 90 days.", "exact"),
    (
        "Logs are **retained** for 90 days [source: https://x.example/a].",
        "Logs are retained for 90 days",
        "Logs are retained for 90 days.",
        "normalised",
    ),
    ("First fact. Second fact holds.", "First fact. Second fact holds.", None, "exact"),
]


@pytest.mark.parametrize("sentence,anchor,claim,verdict", IN_SECTION_PROBES)
def test_in_section_prior_anchor_requires_whole_sentences_stating_the_claim(sentence, anchor, claim, verdict):
    section = "## M\n\nIntro line. " + sentence + " Closing line.\n"
    assert editing._prior_anchor(anchor, section, section, claim) == verdict


def test_in_section_mismatch_and_partial_are_downgraded_with_named_feedback():
    unit = editing.page_units("# T\n\n## M\n\nNo approval is required for MAM.\n")[1]
    reply = {
        "section_id": unit.id,
        "edits": [],
        "append": "",
        "notes": [],
        "handled_claim_ids": ["a"],
        "claim_dispositions": [{"id": "a", "status": "already_present", "anchor": "No approval is required for MAM."}],
    }
    with pytest.raises(ModelError, match="differs from the claim") as caught:
        editing.apply_edits(
            unit, reply, {"a"}, max_output_chars=6000, allow_partial=True, claims={"a": "Approval is required for MAM."}
        )
    feedback = editing._edit_feedback(caught.value, 1000, 6000, result=reply)
    assert "does not state this claim: its negation/qualifier words differ from the claim" in feedback
    partial = {
        **reply,
        "handled_claim_ids": ["a"],
        "claim_dispositions": [{"id": "a", "status": "already_present", "anchor": "approval is required for MAM."}],
    }
    with pytest.raises(ModelError, match="not whole sentences") as caught:
        editing.apply_edits(unit, partial, {"a"}, max_output_chars=6000, allow_partial=True)
    assert "is not one or more whole sentences of this section" in editing._edit_feedback(
        caught.value, 1000, 6000, result=partial
    )


def test_removed_by_own_edit_feedback_says_so():
    unit = editing.page_units("# T\n\n## M\n\nLogs are retained for 90 days. Other.\n")[1]
    reply = {
        "section_id": unit.id,
        "edits": [{"old": "Logs are retained for 90 days. Other.", "new": "Other."}],
        "append": "",
        "notes": [],
        "handled_claim_ids": ["a"],
        "claim_dispositions": [{"id": "a", "status": "already_present", "anchor": "Logs are retained for 90 days."}],
    }
    with pytest.raises(ModelError) as caught:
        editing.apply_edits(unit, reply, {"a"}, max_output_chars=6000, allow_partial=True)
    feedback = editing._edit_feedback(caught.value, 1000, 6000, result=reply)
    assert "your edit removed this text from the section; keep already_present anchors unchanged" in feedback
    assert "occurs nowhere in this section" not in feedback


class DowngradeThenFollowUp:
    """First reply leaves claim A unproven; records what the follow-up is told."""

    def __init__(self, ids):
        self.ids, self.users = ids, []

    async def chat_json(self, system, user):
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
            return {
                "routes": [
                    {
                        "id": identity,
                        "section_id": frame["sections"][-1]["section_id"],
                        "new_heading": None,
                        "rationale": "r",
                    }
                    for identity in self.ids
                ]
            }
        self.users.append(user)
        section_id = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
        listed = user.split("Claim IDs to integrate or prove already present: ", 1)[1].split("\n", 1)[0]
        if len(self.users) == 1:
            return {
                "section_id": section_id,
                "edits": [],
                "append": "Logs are retained for 30 days.",
                "notes": [],
                "handled_claim_ids": list(self.ids),
                "claim_dispositions": [
                    {"id": self.ids[0], "status": "already_present", "anchor": "Retention is 30 days."},
                    {"id": self.ids[1], "status": "integrated", "anchor": "Logs are retained for 30 days."},
                ],
            }
        (identity,) = json.loads(listed.replace("'", '"'))
        return {
            "section_id": section_id,
            "edits": [],
            "append": "Exports run nightly.",
            "notes": [],
            "handled_claim_ids": [identity],
            "claim_dispositions": [{"id": identity, "status": "integrated", "anchor": "Exports run nightly."}],
        }


@pytest.mark.asyncio
async def test_redispatched_claim_gets_precise_feedback_on_its_first_try():
    records = [
        Evidence("src", DATA, "#Data", "paraphrase", text, url=DATA, quote=text, support_status="supported")
        for text in ("Exports run nightly.", "Logs are retained for 30 days.")
    ]
    ids = [curate.evidence_id(record) for record in records]
    change = PageChange("g", "wiki/t.md", "update", title="T", claim_ids=ids)
    model = DowngradeThenFollowUp(ids)
    result = await editing.edit_page(
        model, "T", change, records, "# T\n\n## M\n\nExisting text.\n", [], char_budget=16000, today="2026-10-06"
    )
    assert len(model.users) == 2 and set(result["draft_progress"]["handled_claim_ids"]) == set(ids)
    follow_up = model.users[1]
    assert "Previous reply left these claims unhandled: claim " + ids[0] in follow_up
    assert '"Retention is 30 days." was not accepted because it is not text of this section' in follow_up


# --------------------------------------------------------------------------- #
# Citations never bound a sentence; prose numbers; n't/cannot
# --------------------------------------------------------------------------- #


def probe_pagewide(sentence, anchor, claim):
    page = editing.page_units(two_section_page(sentence))
    unit = next(part for part in page if part.label.endswith("/ M"))
    reply = {
        "section_id": unit.id,
        "edits": [],
        "notes": [],
        "append": "Brand new sentence B.",
        "handled_claim_ids": ["a", "b"],
        "claim_dispositions": [
            {"id": "a", "status": "already_present", "anchor": anchor},
            {"id": "b", "status": "integrated", "anchor": "Brand new sentence B."},
        ],
    }
    editing.apply_edits(
        unit,
        reply,
        {"a", "b"},
        max_output_chars=6000,
        allow_partial=True,
        page=page,
        claims={"a": claim, "b": "Brand new sentence B."},
    )
    return "a" in reply["handled_claim_ids"]


def probe_insection(sentence, anchor, claim):
    section = "## M\n\nIntro line. " + sentence + " Closing line.\n"
    return editing._prior_anchor(anchor, section, section, claim)


LEGIT_PROBES = [
    (
        "markup bold",
        "Logs are **retained** for 30 days.",
        "Logs are retained for 30 days.",
        "Logs are retained for 30 days.",
    ),
    (
        "citation on page",
        "Logs are retained for 30 days [source: https://x.example/v2/2024-05].",
        "Logs are retained for 30 days",
        "Logs are retained for 30 days.",
    ),
    (
        "citation on page, anchor keeps it",
        "Logs are retained for 30 days [source: https://x.example/v2/2024-05].",
        "Logs are retained for 30 days [source: https://x.example/v2/2024-05].",
        "Logs are retained for 30 days.",
    ),
    (
        "citation in claim only",
        "Logs are retained for 30 days.",
        "Logs are retained for 30 days.",
        "Logs are retained for 30 days [source: https://x.example/v2/2024-05].",
    ),
    (
        "claim 30-day, page 30 days",
        "Logs are retained for 30 days.",
        "Logs are retained for 30 days.",
        "Logs have a 30-day retention period.",
    ),
    (
        "claim 30 days, page 30-day",
        "Logs have a 30-day retention period.",
        "Logs have a 30-day retention period.",
        "Logs are retained for 30 days.",
    ),
    (
        "code span",
        "Set `retention_days` to 30 days.",
        "Set retention_days to 30 days.",
        "Set retention_days to 30 days.",
    ),
    (
        "inline link https",
        "See the [guide](https://x.example/v3/a) for 30 days retention.",
        "See the guide for 30 days retention.",
        "Retention is 30 days.",
    ),
    (
        "inline link relative w/ digits",
        "Logs are kept 30 days per the [retention guide](/docs/v2/retention).",
        "Logs are kept 30 days per the [retention guide](/docs/v2/retention).",
        "Logs are kept 30 days per the retention guide.",
    ),
    (
        "wikilink w/ digits",
        "Logs are kept 30 days, see [[gateway-02-memory-plane-spec]].",
        "Logs are kept 30 days, see [[gateway-02-memory-plane-spec]].",
        "Logs are kept 30 days.",
    ),
    ("list item", "- Logs are retained for 30 days", "Logs are retained for 30 days", "Logs are retained for 30 days."),
    (
        "1,000 vs 1000",
        "The limit is 1,000 requests per minute.",
        "The limit is 1,000 requests per minute.",
        "The limit is 1000 requests per minute.",
    ),
    (
        "footnote reference",
        "Logs are retained for 30 days[^1].",
        "Logs are retained for 30 days",
        "Logs are retained for 30 days.",
    ),
]
ADVERSARIAL_PROBES = [
    (
        "doesn't vs claim positive",
        "ZDR doesn't require prior approval.",
        "ZDR doesn't require prior approval.",
        "ZDR requires prior approval.",
    ),
    (
        "can't vs claim positive",
        "Customers can't disable logging.",
        "Customers can't disable logging.",
        "Customers can disable logging.",
    ),
    (
        "cannot vs claim positive",
        "Customers cannot disable logging.",
        "Customers cannot disable logging.",
        "Customers can disable logging.",
    ),
    (
        "curly won’t vs claim positive",
        "Customers won’t see logs.",
        "Customers won’t see logs.",
        "Customers will see logs.",
    ),
    (
        "mid-sentence citation then unless",
        "Logs are retained for 30 days [source: https://x.example/a] unless ZDR is enabled.",
        "Logs are retained for 30 days",
        "Logs are retained for 30 days.",
    ),
    (
        "mid-sentence citation then except",
        "Logs are retained for 30 days [source: https://x.example/a], except under ZDR.",
        "Logs are retained for 30 days",
        "Logs are retained for 30 days.",
    ),
    (
        "anchor after mid-sentence citation",
        "Under ZDR [source: https://x.example/a] logs are not kept.",
        "logs are not kept.",
        "Logs are not kept.",
    ),
    ("decimal cut", "Logs are retained for 30.5 days.", "Logs are retained for 30.", "Logs are retained for 30."),
    (
        "30 days vs 30 hours",
        "Logs are retained for 30 hours.",
        "Logs are retained for 30 hours.",
        "Logs are retained for 30 days.",
    ),
    (
        "30 hours vs 3 hours",
        "Logs are retained for 30 hours.",
        "Logs are retained for 30 hours.",
        "Logs are retained for 3 hours.",
    ),
    (
        "only dropped (claim has none)",
        "Only enterprise customers may use ZDR.",
        "Only enterprise customers may use ZDR.",
        "Enterprise customers may use ZDR.",
    ),
    ("semicolon clause", "Logs are kept 30 days; not for ZDR.", "Logs are kept 30 days", "Logs are kept 30 days."),
    (
        "none vs claim positive",
        "None of the logs are retained.",
        "None of the logs are retained.",
        "The logs are retained.",
    ),
]


@pytest.mark.parametrize("name,sentence,anchor,claim", LEGIT_PROBES, ids=[row[0] for row in LEGIT_PROBES])
def test_legitimate_matches_are_accepted(name, sentence, anchor, claim):
    assert probe_pagewide(sentence, anchor, claim)
    if name != "list item":  # the in-section harness inlines the bullet mid-paragraph
        assert probe_insection(sentence, anchor, claim) in ("exact", "normalised")


@pytest.mark.parametrize("name,sentence,anchor,claim", ADVERSARIAL_PROBES, ids=[row[0] for row in ADVERSARIAL_PROBES])
def test_adversarial_matches_are_downgraded(name, sentence, anchor, claim):
    assert not probe_pagewide(sentence, anchor, claim)
    assert probe_insection(sentence, anchor, claim) not in ("exact", "normalised")


def test_citation_is_never_a_sentence_start_or_end():
    text = "Intro. First fact [source: https://x.example/a] Second fact holds."
    start = text.index("Second")
    assert not editing._sentence_bounded(text, start, len(text))
    assert not editing._sentence_bounded(text, text.index("First"), text.index(" [source"))
    assert editing._sentence_bounded(text, text.index("First"), len(text))


def test_negation_and_prose_number_extraction():
    for text in ("ZDR doesn't require it", "can't", "cannot", "won’t", "isn't", "aren't", "don't"):
        assert editing._polarity(text) == {"not"}, text
    assert editing._polarity("None apply, only without exceptions unless never") == {
        "none",
        "only",
        "without",
        "unless",
        "never",
    }
    assert editing._numbers("30-day") == editing._numbers("30 days") == ["30 day"]
    assert editing._numbers(
        "kept 30 days, see [[spec-02-v3|the spec]] and [guide](/docs/v2/x) `gpt-5.6` "
        "https://x.example/v9 [source: https://y.example/2024] [^7]"
    ) == ["30 day", "5.6"]
    # code-span digits count


@pytest.mark.parametrize(
    "text",
    [
        "First fact. [source: https://x.example/a] Second fact.",
        "First fact.[^1] Second fact.",
        "First fact. [3](#fn3) Second fact.",
    ],
)
def test_sentence_after_a_terminated_sentence_and_its_citation_is_whole(text):
    start = text.index("Second")
    assert editing._sentence_bounded(text, start, len(text))
    section = "## M\n\n" + text + "\n"
    assert editing._prior_anchor("Second fact.", section, section, "Second fact.") == "exact"
