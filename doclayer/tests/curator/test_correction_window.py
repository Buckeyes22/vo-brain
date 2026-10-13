"""Binding corrections are checked against a windowed article, never clipped.

Shape of job-a001c3c736 (wiki/openai-responses-api.md): one correction of
1.5k characters plus the complete 22.6k-character article needed 33921
characters against 32768. The correction names the sections it concerns.
"""

import json

import pytest

from doclayer.curator import editorial
from doclayer.curator.state import PageChange

RESERVE = editorial.FORMAT_REPAIR_RESERVE


def filler(name, count):
    return " ".join(f"{name} operational detail {index}." for index in range(count))


def article(extra=None):
    sections = [
        ("Prompting and Skills", "For Codex and coding agents using GPT-6 Astra, keep skills short."),
        ("Core Architecture", filler("core", 60)),
        ("State Management", "Continue a conversation with previous_response_id."),
        ("Built-in Tools", filler("tools", 60)),
        ("Server-side Compaction", filler("compaction", 60)),
        ("Mid-Turn Steering", filler("steering", 60)),
        ("SDKs and Access", filler("sdk", 60)),
    ]
    sections += extra or []
    return "# OpenAI Responses API\nResponses API overview.\n" + "".join(f"## {t}\n{x}\n" for t, x in sections)


def change(body):
    return PageChange(
        "g",
        "wiki/openai-responses-api.md",
        "update",
        title="OpenAI Responses API",
        draft_body=body,
        draft_meta={"title": "OpenAI Responses API"},
    )


REQUIRED = "previous_response_id does not restore browser sessions"
FORBIDDEN = "When using the Responses API with GPT-6 Astra, overly specific guidance in skills"


def correction(instruction, claim=FORBIDDEN, cid="corr-260d6c5f"):
    return {"page": "wiki/openai-responses-api.md", "claim": claim, "instruction": instruction, "correction_id": cid}


# An assertion-only correction: names no corrected claim and asks for no removal.
LOCATED = (
    "In State Management add that continuing via previous_response_id does not restore browser sessions, "
    "login state or runtime variables. " + "Keep every unrelated fact and citation exactly as it is. " * 18
)
# A removal: the corrected claim must be gone everywhere, with its replacement scoped.
REMOVAL = (
    "In Prompting and Skills, replace the Responses-API-specific framing with For Codex and coding agents. "
    + "Keep every unrelated fact and citation exactly as it is. " * 18
)


class Judge:
    """Correction completion by literal text: required present, forbidden absent."""

    def __init__(self, required=REQUIRED, forbidden=FORBIDDEN):
        self.required, self.forbidden, self.frames = required, forbidden, []

    async def chat_bounded_json(self, system, user, *, char_budget):
        assert len(system) + len(user) + RESERVE <= char_budget
        frame = json.loads(user.split("\n", 1)[1])
        self.frames.append((system, frame))
        if frame["mode"] != "correction":
            return {"complete": True, "failures": [], "conflicts": []}
        cid, section = frame["required_claim_ids"][0], frame["sections"][0]["id"]

        def row(check, detail):
            return {
                "severity": "blocker",
                "check": check,
                "section_id": section,
                "anchor": None,
                "claim_id": cid,
                "detail": detail,
            }

        body = frame["body"]
        if "correction_chunk" in frame:
            rows = []
            if self.forbidden in body:
                rows.append(row("correction-forbidden-present", "forbidden claim present"))
            if self.required not in body:
                rows.append(row("correction-required-absent", "required assertion absent"))
            return {"complete": True, "failures": rows, "conflicts": []}
        rows = []
        if self.required not in body:
            rows.append(row("correction-completion", "required assertion missing"))
        if self.forbidden in body:
            rows.append(row("correction-completion", "forbidden claim present"))
        return {"complete": True, "failures": rows, "conflicts": []}


async def review(model, body, corrections, budget=9000):
    target = change(body)
    return await editorial.review(
        model,
        "Responses API",
        target,
        [],
        corrections,
        char_budget=budget,
        original_body=body,
        original_meta=target.draft_meta,
    )


def correction_frames(model):
    return [frame for _, frame in model.frames if frame["mode"] == "correction"]


@pytest.mark.asyncio
async def test_every_over_budget_correction_is_checked_against_the_whole_article_in_chunks():
    body = article().replace(
        "previous_response_id.", "previous_response_id; previous_response_id does not restore browser sessions."
    )
    model = Judge()
    result = await review(model, body, [correction(LOCATED, claim="")])
    frames = correction_frames(model)
    assert len(frames) > 1 and all("correction_chunk" in frame for frame in frames)
    assert all(
        [row["label"] for row in frame["sections"]][:1] == ["OpenAI Responses API"] for frame in frames
    )  # the H1
    covered = {row["label"] for frame in frames for row in frame["sections"]}
    assert covered == {row["label"] for row in frames[0]["candidate_outline"]}  # the WHOLE article
    assert all(frame["corrections"] == [correction(LOCATED, claim="")] for frame in frames)  # never clipped
    assert result["failures"] == []


@pytest.mark.asyncio
async def test_an_assertion_correction_not_applied_is_reported():
    model = Judge()
    result = await review(model, article(), [correction(LOCATED, claim="")])
    assert [(f["check"], f["claim_id"]) for f in result["failures"]] == [("correction-completion", "corr-260d6c5f")]


@pytest.mark.asyncio
async def test_a_correction_sharing_too_few_words_with_any_section_uses_whole_article_chunks():
    loose = "Mention runtime variables. " + "Bind exactly. " * 60  # shares fewer than 3 content words with each section
    model = Judge(required="runtime variables", forbidden="zzz-never")
    await review(model, article(), [correction(loose, claim="")])
    chunks = [frame for frame in correction_frames(model) if "correction_chunk" in frame]
    covered = {row["label"] for frame in chunks for row in frame["sections"]}
    assert len(chunks) > 1 and covered == {row["label"] for row in chunks[0]["candidate_outline"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remnant,expected", [(None, []), ("reworded", ["correction-completion"]), ("literal", ["correction-completion"])]
)
async def test_a_removal_is_checked_against_the_whole_article_in_chunks(remnant, expected):
    body = article()
    if remnant == "reworded":  # judged per chunk by the model
        body = body.replace(
            "sdk operational detail 2.", "sdk operational detail 2. Skills guidance for the Responses API is risky."
        )
    if remnant == "literal":  # also caught by the literal scan
        body = body.replace("sdk operational detail 2.", f"sdk operational detail 2. {FORBIDDEN} remains.")
    model = Judge(
        required="For Codex and coding agents",
        forbidden="Skills guidance for the Responses API" if remnant == "reworded" else FORBIDDEN,
    )
    result = await review(model, body, [correction(REMOVAL)])
    chunks = [frame for frame in correction_frames(model) if "correction_chunk" in frame]
    covered = {row["label"] for frame in chunks for row in frame["sections"]}
    assert len(chunks) > 1 and covered == {row["label"] for row in chunks[0]["candidate_outline"]}  # the WHOLE article
    assert sorted({f["check"] for f in result["failures"]}) == expected


@pytest.mark.asyncio
async def test_correction_text_too_large_even_with_its_minimal_section_names_it():
    huge = "In State Management add the full previous_response_id contract. " + "Binding qualification text. " * 300

    class Never(Judge):
        async def chat_bounded_json(self, system, user, *, char_budget):
            frame = json.loads(user.split("\n", 1)[1])
            assert frame["mode"] != "correction"
            return await super().chat_bounded_json(system, user, char_budget=char_budget)

    with pytest.raises(editorial.EditorialContextOverflow) as raised:
        await review(Never(), article(), [correction(huge, claim="", cid="corr-huge")])
    message = str(raised.value)
    assert message.startswith("complete operator corrections and article exceed bounded context")
    assert "correction_id corr-huge" in message and "cause the correction text with" in message


REQUIRED_U = "Retention lasts thirty days."
# Names no section and shares at most one term with any section: checked in chunks.
UNLOCATED = "Restate the retention guarantee exactly as approved by the operator. " + "Bind exactly. " * 60


@pytest.mark.asyncio
@pytest.mark.parametrize("where,expected", [("compaction", []), (None, ["correction-completion"])])
async def test_an_unlocated_correction_is_checked_in_chunks(where, expected):
    body = article()
    if where:
        body = body.replace("compaction operational detail 3.", f"compaction operational detail 3. {REQUIRED_U}")
    model = Judge(required=REQUIRED_U)
    result = await review(model, body, [correction(UNLOCATED, claim="")])
    chunks = [frame for frame in correction_frames(model) if "correction_chunk" in frame]
    assert len(chunks) > 1 and all(frame["corrections"][0]["instruction"] == UNLOCATED for frame in chunks)
    assert {chunk["correction_chunk"]["of"] for chunk in chunks} == {len(chunks)}
    assert [f["check"] for f in result["failures"]] == expected


@pytest.mark.asyncio
async def test_an_unlocated_correction_whose_forbidden_claim_survives_in_one_chunk_is_reported():
    body = article().replace("compaction operational detail 3.", f"compaction operational detail 3. {REQUIRED_U}")
    body = body.replace("sdk operational detail 2.", "sdk operational detail 2. Gamma keeps all state forever.")
    model = Judge(required=REQUIRED_U, forbidden="Gamma keeps all state forever.")
    result = await review(model, body, [correction(UNLOCATED, claim="")])
    assert [f["check"] for f in result["failures"]] == ["correction-completion"]
    assert "forbidden" in result["failures"][0]["detail"]


# --------------------------------------------------------------------------- #
# Correction-window cases.
# --------------------------------------------------------------------------- #

BOILER = "Keep every unrelated fact and citation exactly as it is. " * 18
APPLIED = "previous_response_id conversations are retained for 30 days."
REMNANT = "Stored conversations never expire."


def page6(state_extra="", sdk_extra="", intro_extra=""):
    body = article()
    if state_extra:
        body = body.replace(
            "Continue a conversation with previous_response_id.",
            f"Continue a conversation with previous_response_id. {state_extra}",
        )
    if sdk_extra:
        body = body.replace("sdk operational detail 2.", f"sdk operational detail 2. {sdk_extra}")
    if intro_extra:
        body = body.replace("Responses API overview.", f"Responses API overview. {intro_extra}")
    return body


def chunked(model):
    frames = correction_frames(model)
    return [frame for frame in frames if "correction_chunk" in frame], [
        frame for frame in frames if "correction_chunk" not in frame
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wording",
    [
        "In State Management, correct the retention: previous_response_id conversations are retained for 30 days, not forever. ",
        "In State Management, change the stated retention of previous_response_id conversations from forever to 30 days. ",
        "In State Management, the claim that previous_response_id conversations are kept forever is wrong; they are retained for 30 days. ",
        "In State Management, state that previous_response_id conversations are retained for 30 days instead of indefinitely. ",
    ],
)
async def test_substitution_wording_with_a_remnant_elsewhere_is_reported(wording):
    model = Judge(required=APPLIED, forbidden=REMNANT)
    result = await review(
        model, page6(state_extra=APPLIED, sdk_extra=REMNANT), [correction(wording + BOILER, claim="", cid="corr-sub")]
    )
    chunks, windows = chunked(model)
    assert len(chunks) > 1 and not windows
    assert [(f["check"], f["claim_id"]) for f in result["failures"]] == [("correction-completion", "corr-sub")]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["sdk", "intro"])
async def test_a_reworded_remnant_anywhere_is_reported_once(where):
    wording = "In State Management, do not state that previous_response_id conversations are kept forever; say they are retained for 30 days. "
    body = page6(
        state_extra=APPLIED,
        sdk_extra=REMNANT if where == "sdk" else "",
        intro_extra=REMNANT if where == "intro" else "",
    )
    model = Judge(required=APPLIED, forbidden=REMNANT)
    result = await review(model, body, [correction(wording + BOILER, claim="", cid="corr-rem")])
    assert len(chunked(model)[0]) > 1
    # A remnant in the H1 is shown in every chunk but reported once.
    assert [(f["check"], f["detail"]) for f in result["failures"]] == [
        ("correction-completion", "forbidden claim present")
    ]


@pytest.mark.asyncio
async def test_a_named_claim_on_a_clean_page_is_preserved():
    wording = "In State Management, replace the forever-retention claim with: conversations are retained for 30 days. "
    model = Judge(required=APPLIED, forbidden=REMNANT)
    result = await review(
        model,
        page6(state_extra=APPLIED),
        [correction(wording + BOILER, claim="previous_response_id conversations are kept forever.", cid="corr-claim")],
    )
    assert result["failures"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["state", "sdk", "intro"])
async def test_a_required_assertion_present_anywhere_is_preserved(where):
    body = page6(
        state_extra=REQUIRED + "." if where == "state" else "",
        sdk_extra=REQUIRED + "." if where == "sdk" else "",
        intro_extra=REQUIRED + "." if where == "intro" else "",
    )
    model = Judge()
    result = await review(model, body, [correction(LOCATED, claim="")])
    assert len(chunked(model)[0]) > 1 and result["failures"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Mention compaction thresholds. " + "Bind exactly. " * 60,
        "Mention compaction operational thresholds. " + "Bind exactly. " * 60,
        "In the Responses API page mention compaction. " + "Bind exactly. " * 60,
    ],
)
async def test_no_section_matching_remains_every_correction_is_chunked(text):
    model = Judge(required="zzz-required", forbidden="zzz-never")
    await review(model, article(), [correction(text, claim="")])
    chunks, windows = chunked(model)
    assert len(chunks) > 1 and not windows
