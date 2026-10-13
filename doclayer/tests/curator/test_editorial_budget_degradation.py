"""Over-budget editorial review tasks degrade instead of failing the page.

Shape of job-e6353422ea (wiki/openai-responses-api.md): the preservation
review shows the complete candidate body; with binding operator corrections a
single original section plus one source record no longer fit, and the page
failed at report with "editorial-context".
"""

import hashlib
import json

import pytest

from doclayer.curator import editorial
from doclayer.curator.editing import page_units
from doclayer.curator.state import Evidence, PageChange

RESERVE = editorial.FORMAT_REPAIR_RESERVE


def source(text, identity):
    return Evidence(
        "s",
        "Responses guide",
        "## Guide",
        "quote",
        text,
        claim_id=identity,
        quote=text,
        url="https://api.test/responses",
        support_status="supported",
    )


def page(sections, intro="Responses API overview."):
    return "# Responses API\n" + intro + "\n" + "".join(f"## {title}\n{text}\n" for title, text in sections)


def change(body, ids=()):
    return PageChange(
        "g",
        "wiki/openai-responses-api.md",
        "update",
        title="Responses API",
        draft_body=body,
        draft_meta={"title": "Responses API"},
        claim_ids=list(ids),
    )


class Recorder:
    def __init__(self, reply=None):
        self.prompts = []
        self.frames = []
        self.reply = reply or (lambda frame: {"complete": True, "failures": [], "conflicts": []})

    async def chat_bounded_json(self, system, user, *, char_budget):
        assert len(system) + len(user) + RESERVE <= char_budget
        self.prompts.append(system + "\x00" + user)
        frame = json.loads(user.split("\n", 1)[1])
        self.frames.append((system, frame))
        return self.reply(frame)


def digest(prompts):
    return hashlib.sha256("\x01".join(prompts).encode()).hexdigest()


def filler(name, count):
    return " ".join(f"{name} detail {index}." for index in range(count))


UNDER_BUDGET_DIGEST = "aaf1159da8ecff2e2e0b943ced046b66398c9ec8d865a112a54bd8cccaea2387"


@pytest.mark.asyncio
async def test_under_budget_review_prompts_are_byte_identical():
    original = page([("Tools", "Built-in tools include shell."), ("State", "Conversation state is stored.")])
    body = original.replace("shell.", "shell and web search.")
    records = [source("Built-in tools include shell and web search.", "claim-tools")]
    corrections = [
        {
            "page": "wiki/openai-responses-api.md",
            "claim": "Conversation state is stored.",
            "instruction": "Say state is stored for 30 days.",
            "correction_id": "corr-1",
        }
    ]
    prompts = []
    for mode in ("grounding", "preservation"):
        model = Recorder()
        target = change(body, ["claim-tools"] if mode == "grounding" else [])
        await editorial.review(
            model,
            "Responses API",
            target,
            records,
            corrections,
            char_budget=12000,
            mode=mode,
            original_body=original,
            original_meta=target.draft_meta,
            namespace="review-" + mode,
        )
        prompts += model.prompts
    assert len(prompts) == 4
    assert digest(prompts) == UNDER_BUDGET_DIGEST


def responses_page(changed=False, size=110):
    sections = [(f"Feature {index}", filler(f"feature{index}", size)) for index in range(8)]
    original = page(sections)
    body = original.replace("feature3 detail 5.", "feature3 detail five, revised.") if changed else original
    return original, body


def operator_corrections(size=25):
    return [
        {
            "page": "wiki/openai-responses-api.md",
            "claim": f"feature{index} detail 1.",
            "instruction": "Keep the documented qualification. " * size,
            "correction_id": f"corr-{index}",
        }
        for index in range(2)
    ]


@pytest.mark.asyncio
async def test_preservation_section_over_budget_uses_fail_closed_partial_candidate_view():
    """Job-e6353422ea: one original section plus one record overflowed the full-candidate frame."""
    original, body = responses_page(changed=True)
    records = [source("feature3 detail five, revised, per the guide. " * 4, f"claim-{index}") for index in range(3)]
    target = change(body)
    model = Recorder()
    budget = 12000
    result = await editorial.review(
        model,
        "Responses API",
        target,
        records,
        operator_corrections(),
        char_budget=budget,
        mode="preservation",
        original_body=original,
        original_meta=target.draft_meta,
        namespace="existing_preservation",
    )
    assert result["complete"] and len(model.frames) == 1
    system, frame = model.frames[0]
    changed = next(unit for unit in page_units(body) if "revised" in unit.text)
    assert frame["mode"] == "preservation" and frame["target_section_ids"] == [changed.id]
    assert system.endswith(editorial.PARTIAL_VIEW_GUIDANCE)
    assert frame["candidate_view_policy"] == editorial.PARTIAL_VIEW_POLICY
    assert [row["id"] for row in frame["candidate_outline"]] == [unit.id for unit in page_units(body)]
    shown = [row["id"] for row in frame["sections"]]
    assert changed.id in shown and len(shown) < len(page_units(body))
    assert frame["body"] == "".join(unit.text for unit in page_units(body) if unit.id in shown)
    assert frame["evidence"] and len(frame["corrections"]) == 2  # binding corrections stay complete
    assert set(frame["retained_canonical_sections"]) <= set(shown)


@pytest.mark.asyncio
async def test_preservation_single_section_at_minimal_view_names_section_and_sizes():
    original, body = responses_page(changed=True, size=900)
    target = change(body)

    class Never(Recorder):
        async def chat_bounded_json(self, *args, **kwargs):
            pytest.fail("an over-budget review cannot dispatch")

    with pytest.raises(editorial.EditorialContextOverflow) as raised:
        await editorial.review(
            Never(),
            "Responses API",
            target,
            [],
            [],
            char_budget=12000,
            mode="preservation",
            original_body=original,
            original_meta=target.draft_meta,
        )
    changed = next(unit for unit in page_units(body) if "revised" in unit.text)
    message = str(raised.value)
    assert message.startswith("complete editorial section/context/source exceeds bounded input")
    assert f"section_id {changed.id}" in message and "budget 12000" in message and "needs " in message
    assert raised.value.diagnostic["section_id"] == changed.id and not raised.value.transient


@pytest.mark.asyncio
async def test_required_claim_over_budget_uses_partial_view_of_the_article():
    original, body = responses_page(size=200)
    record = source("feature6 detail 7 is documented with its qualification.", "claim-feature6")
    target = change(body, ["claim-feature6"])
    model = Recorder()
    await editorial.review(
        model,
        "Responses API",
        target,
        [record],
        [],
        char_budget=12000,
        original_body=original,
        original_meta=target.draft_meta,
    )
    system, frame = model.frames[-1]
    assert frame["mode"] == "required" and frame["required_claim_ids"] == ["claim-feature6"]
    assert system.endswith(editorial.PARTIAL_VIEW_GUIDANCE) and "candidate_outline" in frame
    feature6 = next(unit for unit in page_units(body) if unit.label.endswith("Feature 6"))
    assert feature6.id in [row["id"] for row in frame["sections"]]
    assert len(frame["body"]) < len(body)


@pytest.mark.asyncio
async def test_required_single_claim_at_minimal_view_names_claim_and_sizes():
    original, body = responses_page(size=20)
    record = source("feature2 " + filler("huge", 1500), "claim-huge")

    class Never(Recorder):
        async def chat_bounded_json(self, *args, **kwargs):
            pytest.fail("an over-budget review cannot dispatch")

    target = change(body, ["claim-huge"])
    with pytest.raises(editorial.EditorialContextOverflow) as raised:
        await editorial.review(
            Never(),
            "Responses API",
            target,
            [record],
            [],
            char_budget=12000,
            original_body=original,
            original_meta=target.draft_meta,
        )
    message = str(raised.value)
    assert message.startswith("complete required contribution and article exceed bounded context")
    assert "claim_id claim-huge" in message and "budget 12000" in message
    assert raised.value.diagnostic["claim_id"] == "claim-huge"


@pytest.mark.asyncio
async def test_operator_corrections_over_budget_are_batched_against_the_complete_article():
    original, body = responses_page(size=30)
    corrections = [
        {
            "page": "wiki/openai-responses-api.md",
            "claim": f"feature{index} detail 1.",
            "instruction": f"Correction {index}: keep the documented qualification. " * 25,
            "correction_id": f"corr-{index}",
        }
        for index in range(3)
    ]
    target = change(body)
    model = Recorder()
    await editorial.review(
        model,
        "Responses API",
        target,
        [],
        corrections,
        char_budget=12000,
        original_body=original,
        original_meta=target.draft_meta,
    )
    completions = [frame for _, frame in model.frames if frame["mode"] == "correction"]
    assert len(completions) >= 2
    assert sorted(i for frame in completions for i in frame["required_claim_ids"]) == ["corr-0", "corr-1", "corr-2"]
    assert all(frame["body"] == body and "candidate_outline" not in frame for frame in completions)
    for frame in completions:
        assert [row["correction_id"] for row in frame["corrections"]] == frame["required_claim_ids"]


MOVED = "Alpha sessions retain conversation state for thirty days."
DELETED = "Gamma exports require an enterprise administrator approval."


def moved_page():
    others = [(f"Feature {index}", filler(f"feature{index}", 40)) for index in range(4)]
    # MOVED ends its paragraph: a sentence left right after a moved one keeps the
    # finding (it may be a detached qualifier), see test_presence_with_qualifier_left_behind.
    alpha = "Alpha overview paragraph. " + MOVED + "\nAlpha supports streaming."
    gamma = "Gamma overview paragraph. " + DELETED + " Gamma supports batching."
    original = page([("Alpha", alpha), ("Beta", "Beta overview."), ("Gamma", gamma)] + others)
    # Beta grows: beside the narrowed Alpha view it no longer fits.
    body = (
        original.replace(" " + MOVED, "")
        .replace(" " + DELETED, "")
        .replace("Beta overview.", "Beta overview. " + filler("beta", 260) + " " + MOVED)
    )
    return original, body


class FailClosed(Recorder):
    """Flags every listed original fact of a target it cannot see, quoting it."""

    def __init__(self, facts):
        super().__init__()
        self.facts = facts  # target section ID -> original facts

    async def chat_bounded_json(self, system, user, *, char_budget):
        result = await super().chat_bounded_json(system, user, char_budget=char_budget)
        frame = self.frames[-1][1]
        if frame["mode"] != "preservation" or len(frame["target_section_ids"]) != 1:
            return result
        target = frame["target_section_ids"][0]
        return {
            "complete": True,
            "conflicts": [],
            "failures": [
                {
                    "severity": "blocker",
                    "check": "preservation",
                    "section_id": target,
                    "anchor": None,
                    "detail": f"Original section stated '{fact}' and it is not represented.",
                }
                for fact in self.facts.get(target, [])
                if fact not in frame["body"]
            ],
        }


@pytest.mark.asyncio
async def test_narrowed_preservation_review_does_not_flag_or_duplicate_a_moved_fact():
    original, body = moved_page()
    alpha, beta, gamma = (
        next(unit for unit in page_units(body) if unit.label.endswith(name)) for name in ("Alpha", "Beta", "Gamma")
    )
    target = change(body)
    model = FailClosed({alpha.id: [MOVED], gamma.id: [DELETED]})
    budget = 9000
    result = await editorial.review(
        model,
        "Responses API",
        target,
        [],
        [],
        char_budget=budget,
        mode="preservation",
        original_body=original,
        original_meta=target.draft_meta,
        namespace="existing_preservation",
    )
    narrowed = next(frame for _, frame in model.frames if frame["target_section_ids"] == [alpha.id])
    assert "candidate_outline" in narrowed and beta.id not in [row["id"] for row in narrowed["sections"]]
    assert narrowed["represented_outside_view"] == [{"text": MOVED, "section_id": beta.id}]
    details = [failure["detail"] for failure in result["failures"]]
    # A duplicate is preferable to a loss. The overview sentence left in
    # place lost its following neighbour, so the moved fact's finding stands (fail closed) ...
    assert (
        len(details) == 2
        and any(DELETED in detail for detail in details)
        and any(MOVED in detail for detail in details)
    )

    # Repair restores only what the review still flags: the deleted fact.
    state = target.check_progress["existing_preservation"]
    state.update(
        original_body=original,
        original_meta=target.draft_meta,
        canonical_binding={"commit": "c" * 40, "blob": "b" * 40, "path": target.path},
    )

    class Restore:
        async def chat_json(self, system, user):
            rows = json.loads(user.split("Article review repairs for this exact section: ", 1)[1].split("\n", 1)[0])
            facts = [
                next(group for group in match if group)
                for row in rows
                for match in editorial._QUOTED.findall(row["detail"])
            ]
            return {
                "section_id": user.split("Section ID: ", 1)[1].split("\n", 1)[0],
                "edits": [],
                "append": " ".join(facts),
                "handled_claim_ids": [],
                "claim_dispositions": [],
                "notes": [],
            }

    from doclayer.curator import editing

    repaired = await editing.edit_page(
        Restore(), "Responses API", target, [], body, [], char_budget=12000, today="2026-10-06", repair=True
    )
    # ... and repair restores it beside its original neighbours. Its line did not move intact
    # (it left a neighbour behind), so it is not MOVED and the span rule keeps the duplicate:
    # removing it could detach a qualifier.
    assert repaired["body"].count(MOVED) == 2 and repaired["body"].count(DELETED) == 1


@pytest.mark.asyncio
async def test_narrowed_required_review_does_not_flag_a_claim_present_elsewhere_on_the_page():
    present = "Gamma tokens expire after ninety minutes idle."
    absent = "Gamma tokens refresh through the rotation endpoint."
    # Lookalike precedes the real owner, so it is the narrowed view's core.
    sections = [
        ("Lookalike", "Idle gamma minutes: ninety tokens expire after use. Rotation endpoint refresh notes."),
        ("Owner", filler("owner", 260) + " " + present),
    ]
    sections += [(f"Feature {index}", filler(f"feature{index}", 40)) for index in range(3)]
    body = page(sections)
    records = [source(present, "claim-present"), source(absent, "claim-absent")]
    target = change(body, ["claim-present", "claim-absent"])

    class FailClosed(Recorder):
        async def chat_bounded_json(self, system, user, *, char_budget):
            await super().chat_bounded_json(system, user, char_budget=char_budget)
            frame = self.frames[-1][1]
            failures = []
            if frame["mode"] == "required":
                texts = {"claim-present": present, "claim-absent": absent}
                failures = [
                    {
                        "severity": "blocker",
                        "check": "required-contribution",
                        "claim_id": identity,
                        "section_id": frame["sections"][0]["id"],
                        "anchor": None,
                        "detail": "Claim not represented.",
                    }
                    for identity in frame["required_claim_ids"]
                    if texts[identity] not in frame["body"]
                ]
            return {"complete": True, "failures": failures, "conflicts": []}

    model = FailClosed()
    result = await editorial.review(
        model,
        "Responses API",
        target,
        records,
        [],
        char_budget=9000,
        original_body=body,
        original_meta=target.draft_meta,
    )
    required = [frame for _, frame in model.frames if frame["mode"] == "required"]
    hidden = [frame for frame in required if "claim-present" in frame["required_claim_ids"]]
    assert hidden and "candidate_outline" in hidden[0] and present not in hidden[0]["body"]
    assert [f["claim_id"] for f in result["failures"]] == ["claim-absent"]


def narrowed_task(mode, shown, target_ids=()):
    return {
        "mode": mode,
        "candidate_outline": [],
        "sections": [{"id": i} for i in shown],
        "target_section_ids": list(target_ids),
        "body": "",
    }


def test_required_claim_is_not_present_because_its_short_quote_is():
    """Only the quote '400,000 tokens' occurs in a hidden section."""
    claim = "Model Astra supports a context window of 400,000 tokens."
    record = Evidence(
        "s",
        "Guide",
        "## Astra",
        "quote",
        claim,
        claim_id="claim-astra",
        quote="400,000 tokens",
        support_status="supported",
    )
    sections = [
        {"id": "astra", "text": "## Astra\nAstra overview.\n"},
        {"id": "luna", "text": "## Luna\nModel Luna supports a context window of 400,000 tokens.\n"},
    ]
    failure = {"claim_id": "claim-astra", "detail": "Claim not represented."}
    task = narrowed_task("required", ["astra"])
    assert not editorial._moved_out_of_view(failure, task, sections, {}, {"claim-astra": record})
    sections[1]["text"] += claim + "\n"  # the complete claim sentence, hidden: moved
    assert editorial._moved_out_of_view(failure, task, sections, {}, {"claim-astra": record})


def test_preservation_fragment_quote_does_not_prove_presence():
    """A 12+ character fragment of a deleted sentence occurs in a qualified hidden sentence."""
    original = {"alpha": {"text": "## Alpha\nAlpha exports require enterprise administrator approval.\n"}}
    sections = [
        {"id": "alpha", "text": "## Alpha\nAlpha overview.\n"},
        {
            "id": "beta",
            "text": "## Beta\nBeta exports require enterprise administrator approval only for audit logs.\n",
        },
    ]
    task = narrowed_task("preservation", ["alpha"], ["alpha"])
    fragment = {"detail": "The original said 'exports require enterprise administrator approval' and it is gone."}
    assert not editorial._moved_out_of_view(fragment, task, sections, original, {})
    sections[1]["text"] += "Alpha exports require enterprise administrator approval.\n"
    assert editorial._moved_out_of_view(fragment, task, sections, original, {})  # whole sentence moved
    weakened = {
        "detail": "Original 'Alpha exports require enterprise administrator approval.' is weakened by the move."
    }
    assert not editorial._moved_out_of_view(weakened, task, sections, original, {})
    candidate = {
        "detail": "Original 'Alpha exports require enterprise administrator approval.' became 'Alpha overview.'"
    }
    assert not editorial._moved_out_of_view(candidate, task, sections, original, {})


def test_presence_covers_every_sentence():
    """Short qualifier sentences and unquoted losses keep the finding."""
    claim = "Model Astra supports a context window of 400,000 tokens. Only in beta."
    record = Evidence(
        "s", "Guide", "## Astra", "quote", claim, claim_id="claim-astra", quote=claim, support_status="supported"
    )
    sections = [
        {"id": "astra", "text": "## Astra\nAstra overview.\n"},
        {"id": "luna", "text": "## Luna\nModel Astra supports a context window of 400,000 tokens.\n"},
    ]
    failure = {"claim_id": "claim-astra", "detail": "Claim not represented."}
    task = narrowed_task("required", ["astra"])
    assert not editorial._moved_out_of_view(failure, task, sections, {}, {"claim-astra": record})
    sections[1]["text"] = "## Luna\n" + claim + "\n"
    assert editorial._moved_out_of_view(failure, task, sections, {}, {"claim-astra": record})

    hidden_first = {"id": "beta", "text": "## Beta\nAlpha exports require enterprise administrator approval.\n"}
    alpha_now = {"id": "alpha", "text": "## Alpha\nAlpha overview.\n"}
    task = narrowed_task("preservation", ["alpha"], ["alpha"])
    finding = {
        "detail": "Original 'Alpha exports require enterprise administrator approval.' and the retention "
        "period sentence are absent."
    }
    retention = {
        "alpha": {
            "text": "## Alpha\nAlpha overview.\nAlpha exports require enterprise administrator approval. "
            "Alpha retains export logs for ninety days.\n"
        }
    }
    assert not editorial._moved_out_of_view(finding, task, [alpha_now, hidden_first], retention, {})
    exclusion = {
        "alpha": {
            "text": "## Alpha\nAlpha overview.\nAlpha exports require enterprise administrator approval. Not in EU.\n"
        }
    }
    lost_eu = {"detail": "Original 'Alpha exports require enterprise administrator approval.' lost its EU exclusion."}
    assert not editorial._moved_out_of_view(lost_eu, task, [alpha_now, hidden_first], exclusion, {})
    moved_all = {"id": "beta", "text": "## Beta\nAlpha exports require enterprise administrator approval. Not in EU.\n"}
    plain = {"detail": "Original 'Alpha exports require enterprise administrator approval.' is not represented."}
    assert editorial._moved_out_of_view(plain, task, [alpha_now, moved_all], exclusion, {})


# One section holds two adjacent facts: the first is moved to another section and the second is dropped outright.
SAME_MOVED = "Alpha sessions retain conversation state for thirty days."
SAME_DELETED = "Alpha exports require an enterprise administrator approval."


def same_section_moved_page():
    others = [(f"Feature {index}", filler(f"feature{index}", 40)) for index in range(4)]
    alpha = "Alpha overview paragraph. " + SAME_MOVED + " " + SAME_DELETED + " Alpha supports streaming."
    original = page([("Alpha", alpha), ("Beta", "Beta overview.")] + others)
    # Beta grows: beside the narrowed Alpha view it no longer fits.
    body = original.replace(" " + SAME_MOVED + " " + SAME_DELETED, "").replace(
        "Beta overview.", "Beta overview. " + filler("beta", 260) + " " + SAME_MOVED
    )
    return original, body


class SameSectionFailClosed(Recorder):
    """Flags every original Alpha fact it cannot see, quoting the original text."""

    def __init__(self, alpha_id):
        super().__init__()
        self.alpha_id = alpha_id

    async def chat_bounded_json(self, system, user, *, char_budget):
        result = await super().chat_bounded_json(system, user, char_budget=char_budget)
        frame = self.frames[-1][1]
        if frame["mode"] != "preservation" or frame["target_section_ids"] != [self.alpha_id]:
            return result
        return {
            "complete": True,
            "conflicts": [],
            "failures": [
                {
                    "severity": "blocker",
                    "check": "preservation",
                    "section_id": self.alpha_id,
                    "anchor": None,
                    "detail": f"Original Alpha section stated '{fact}' and it is not represented.",
                }
                for fact in (SAME_MOVED, SAME_DELETED)
                if fact not in frame["body"]
            ],
        }


@pytest.mark.asyncio
async def test_same_section_moved_and_deleted_facts_restore_only_the_deleted_one():
    original, body = same_section_moved_page()
    alpha = next(unit for unit in page_units(body) if unit.label.endswith("Alpha"))
    beta = next(unit for unit in page_units(body) if unit.label.endswith("Beta"))
    target = change(body)
    model = SameSectionFailClosed(alpha.id)
    budget = 9000
    result = await editorial.review(
        model,
        "Responses API",
        target,
        [],
        [],
        char_budget=budget,
        mode="preservation",
        original_body=original,
        original_meta=target.draft_meta,
        namespace="existing_preservation",
    )
    narrowed = next(frame for _, frame in model.frames if frame["target_section_ids"] == [alpha.id])
    assert "candidate_outline" in narrowed and beta.id not in [row["id"] for row in narrowed["sections"]]
    assert narrowed["represented_outside_view"] == [{"text": SAME_MOVED, "section_id": beta.id}]
    details = [failure["detail"] for failure in result["failures"]]
    # A duplicate is preferable to a loss. The overview sentence left in
    # place lost its following neighbour, so the moved fact's finding stands (fail closed) ...
    assert (
        len(details) == 2
        and any(SAME_DELETED in detail for detail in details)
        and any(SAME_MOVED in detail for detail in details)
    )

    # Repair restores only what the review still flags: the deleted fact.
    state = target.check_progress["existing_preservation"]
    state.update(
        original_body=original,
        original_meta=target.draft_meta,
        canonical_binding={"commit": "c" * 40, "blob": "b" * 40, "path": target.path},
    )

    class Restore:
        async def chat_json(self, system, user):
            rows = json.loads(user.split("Article review repairs for this exact section: ", 1)[1].split("\n", 1)[0])
            facts = [
                next(group for group in match if group)
                for row in rows
                for match in editorial._QUOTED.findall(row["detail"])
            ]
            return {
                "section_id": user.split("Section ID: ", 1)[1].split("\n", 1)[0],
                "edits": [],
                "append": " ".join(facts),
                "handled_claim_ids": [],
                "claim_dispositions": [],
                "notes": [],
            }

    from doclayer.curator import editing

    repaired = await editing.edit_page(
        Restore(), "Responses API", target, [], body, [], char_budget=12000, today="2026-10-06", repair=True
    )
    # ... and repair restores it beside its original neighbours. Its line did not move intact
    # (it left a neighbour behind), so it is not MOVED and the span rule keeps the duplicate:
    # removing it could detach a qualifier.
    assert repaired["body"].count(SAME_MOVED) == 2 and repaired["body"].count(SAME_DELETED) == 1


def test_presence_with_qualifier_left_behind():
    """Presence of moved sentences, including a qualifier left behind."""
    M = editorial._moved_out_of_view

    def task(mode, shown, targets=()):
        return {
            "mode": mode,
            "candidate_outline": [],
            "sections": [{"id": i} for i in shown],
            "target_section_ids": list(targets),
            "body": "",
        }

    def rec(text):
        return Evidence("s", "Guide", "## Astra", "quote", text, claim_id="c", quote="", support_status="supported")

    long = "Model Astra supports a context window of 400,000 tokens."
    astra = {"id": "astra", "text": "## Astra\nAstra overview.\n"}

    def req(claim, sections):
        return M(
            {"claim_id": "c", "detail": "Claim not represented."},
            task("required", ["astra"]),
            sections,
            {},
            {"c": rec(claim)},
        )

    beta = long + " Only in beta."
    assert not req(beta, [astra, {"id": "luna", "text": "## Luna\n" + long + "\n"}])
    assert req(beta, [astra, {"id": "luna", "text": "## Luna\n" + beta + "\n"}])  # control
    assert not req(
        beta,
        [
            astra,
            {"id": "luna", "text": "## Luna\n" + long + "\n"},
            {"id": "sol", "text": "## Sol\nSol is new. Only in beta.\n"},
        ],
    )
    mid = long + " Only in beta. Requires the v2 endpoint for all requests."
    assert not req(
        mid, [astra, {"id": "luna", "text": "## Luna\n" + long + " Requires the v2 endpoint for all requests.\n"}]
    )
    assert not req(beta, [astra, {"id": "luna", "text": "## Luna\n" + long + " Not only in beta.\n"}])
    assert req(beta, [astra, {"id": "luna", "text": "## Luna\n- " + long + "\n- Only in beta.\n"}])

    approval = "Alpha exports require enterprise administrator approval."
    orig = {"alpha": {"text": f"## Alpha\nAlpha overview.\n{approval} Not in EU.\n"}}
    alpha = {"id": "alpha", "text": "## Alpha\nAlpha overview.\n"}
    pt = task("preservation", ["alpha"], ["alpha"])
    lost_eu = {"detail": f"Original '{approval}' lost its EU exclusion."}
    plain = {"detail": f"Original '{approval}' is not represented."}
    assert not M(lost_eu, pt, [alpha, {"id": "beta", "text": f"## Beta\n{approval}\n"}], orig, {})
    assert M(plain, pt, [alpha, {"id": "beta", "text": f"## Beta\n{approval} Not in EU.\n"}], orig, {})  # control
    assert not M(
        lost_eu,
        pt,
        [
            alpha,
            {"id": "beta", "text": f"## Beta\n{approval}\n"},
            {"id": "gamma", "text": "## Gamma\nGamma streaming is generally available. Not in EU.\n"},
        ],
        orig,
        {},
    )
    imports = "Alpha imports require enterprise administrator approval."
    orig2 = {"alpha": {"text": f"## Alpha\nAlpha overview.\n{approval} Not in EU.\n{imports} Not in EU.\n"}}
    lost_imports = {"detail": f"Original '{imports}' lost its EU exclusion."}
    assert not M(
        lost_imports, pt, [alpha, {"id": "beta", "text": f"## Beta\n{approval} Not in EU.\n{imports}\n"}], orig2, {}
    )
    assert not M(
        lost_eu, pt, [alpha, {"id": "beta", "text": f"## Beta\nBeta overview. Not in EU.\n{approval}\n"}], orig, {}
    )
    left_behind = {"id": "alpha", "text": "## Alpha\nAlpha overview.\nNot in EU.\n"}
    assert not M(
        plain, pt, [left_behind, {"id": "beta", "text": f"## Beta\n{approval}\n"}], orig, {}
    )  # detached qualifier

    retention = "Alpha retains export logs for ninety days."
    orig3 = {"alpha": {"text": f"## Alpha\nAlpha overview.\n{approval} {retention}\n"}}
    both = {"detail": f"Original '{approval}' and the retention period sentence are absent."}
    moved_beta = {"id": "beta", "text": f"## Beta\n{approval}\n"}
    assert not M(both, pt, [alpha, moved_beta], orig3, {})
    assert not M(
        both, pt, [alpha, moved_beta, {"id": "delta", "text": f"## Delta\n{retention}\n"}], orig3, {}
    )  # fail closed
    orig4 = {"alpha": {"text": f"## Alpha\nAlpha overview.\n{approval}\n{retention} Deleted on request.\n"}}
    gone = {"detail": f"Original '{approval}' is not represented; the deletion-on-request sentence is gone."}
    assert not M(
        gone, pt, [{"id": "alpha", "text": f"## Alpha\nAlpha overview.\n{retention}\n"}, moved_beta], orig4, {}
    )


# --------------------------------------------------------------------------- #
# Review plus repair of moved and deleted sentences, end to end.
# --------------------------------------------------------------------------- #

SHAPE_MOVED = "Alpha sessions retain conversation state for thirty days."
SHAPE_DELETED = "Alpha exports require an enterprise administrator approval."
SHAPE_A = "Alpha exports require enterprise administrator approval."
SHAPE_B = "Alpha imports require enterprise administrator approval."
SHAPE_OTHERS = [(f"Feature {index}", filler(f"feature{index}", 40)) for index in range(4)]


def shape_grow(text):
    return "Beta overview. " + filler("beta", 260) + " " + text


class ShapeFlags(Recorder):
    """Fail-closed narrowed Alpha reviewer; `shape` decides how unseen original facts are reported."""

    def __init__(self, alpha_id, facts, shape):
        super().__init__()
        self.alpha_id, self.facts, self.shape = alpha_id, facts, shape

    async def chat_bounded_json(self, system, user, *, char_budget):
        result = await super().chat_bounded_json(system, user, char_budget=char_budget)
        frame = self.frames[-1][1]
        if frame["mode"] != "preservation" or frame["target_section_ids"] != [self.alpha_id]:
            return result
        gone = [fact for fact in self.facts if fact not in frame["body"]]

        def row(detail):
            return {
                "severity": "blocker",
                "check": "preservation",
                "section_id": self.alpha_id,
                "anchor": None,
                "detail": detail,
            }

        if self.shape == "combined":
            rows = (
                [
                    row(
                        "Original Alpha section stated "
                        + " and ".join(f"'{fact}'" for fact in gone)
                        + " and they are not represented."
                    )
                ]
                if gone
                else []
            )
        elif self.shape == "paraphrase":
            rows = [
                row(f"Original Alpha section stated '{fact}' and it is not represented.")
                if fact != SHAPE_DELETED
                else row("The original Alpha export approval requirement is not represented.")
                for fact in gone
            ]
        else:
            rows = [row(f"Original Alpha section stated '{fact}' and it is not represented.") for fact in gone]
        return {"complete": True, "conflicts": [], "failures": rows}


class ShapeRestore:
    async def chat_json(self, system, user):
        rows = json.loads(user.split("Article review repairs for this exact section: ", 1)[1].split("\n", 1)[0])
        facts = [
            next(group for group in match if group)
            for row in rows
            for match in editorial._QUOTED.findall(row["detail"])
        ]
        if any("export approval requirement" in row["detail"] for row in rows):
            facts.append(SHAPE_DELETED)
        return {
            "section_id": user.split("Section ID: ", 1)[1].split("\n", 1)[0],
            "edits": [],
            "append": " ".join(facts),
            "handled_claim_ids": [],
            "claim_dispositions": [],
            "notes": [],
        }


def shape_cases():
    alpha = "Alpha overview paragraph. " + SHAPE_MOVED + " " + SHAPE_DELETED + " Alpha supports streaming."
    o = page([("Alpha", alpha), ("Beta", "Beta overview.")] + SHAPE_OTHERS)
    b = o.replace(" " + SHAPE_MOVED + " " + SHAPE_DELETED, "").replace("Beta overview.", shape_grow(SHAPE_MOVED))
    # The moved fact left neighbours behind, so it is not MOVED intact: the duplicate is allowed.
    yield "V2a", o, b, [SHAPE_MOVED, SHAPE_DELETED], "combined", {SHAPE_MOVED: 2, SHAPE_DELETED: 1}
    yield "V2c", o, b, [SHAPE_MOVED, SHAPE_DELETED], "paraphrase", {SHAPE_MOVED: 2, SHAPE_DELETED: 1}
    alpha = "Alpha overview paragraph. " + SHAPE_DELETED + " " + SHAPE_MOVED + " Alpha supports streaming."
    o = page([("Alpha", alpha), ("Beta", "Beta overview.")] + SHAPE_OTHERS)
    b = o.replace(" " + SHAPE_DELETED + " " + SHAPE_MOVED, "").replace("Beta overview.", shape_grow(SHAPE_MOVED))
    yield "V2b", o, b, [SHAPE_MOVED, SHAPE_DELETED], "separate", {SHAPE_MOVED: 2, SHAPE_DELETED: 1}
    # E1: the fact is also stated in Gamma; Alpha's copy moved to Beta, Gamma's removed. The original
    # page held it twice, so restoring Alpha's copy is not a duplicate beyond the base count.
    o = page(
        [
            ("Alpha", "Alpha overview paragraph. " + SHAPE_MOVED),
            ("Beta", "Beta overview."),
            ("Gamma", "Gamma overview paragraph. " + SHAPE_MOVED),
        ]
        + SHAPE_OTHERS
    )
    b = (
        o.replace("Alpha overview paragraph. " + SHAPE_MOVED, "Alpha overview paragraph.")
        .replace("Gamma overview paragraph. " + SHAPE_MOVED, "Gamma overview paragraph.")
        .replace("Beta overview.", shape_grow(SHAPE_MOVED))
    )
    yield "E1", o, b, [SHAPE_MOVED], "separate", {SHAPE_MOVED: 2}
    # E2: a dedupe; the fact was in Alpha and Beta, the Alpha copy was removed.
    o = page(
        [("Alpha", "Alpha overview paragraph. " + SHAPE_MOVED), ("Beta", "Beta overview. " + SHAPE_MOVED)]
        + SHAPE_OTHERS
    )
    b = o.replace("Alpha overview paragraph. " + SHAPE_MOVED, "Alpha overview paragraph.").replace(
        "Beta overview. " + SHAPE_MOVED, shape_grow(SHAPE_MOVED)
    )
    yield "E2", o, b, [SHAPE_MOVED], "separate", {SHAPE_MOVED: 1}
    # E3: exports line moved whole, imports' repeated qualifier removed in view. Repair output is
    # never removed, so the re-inserted exports sentence stays (a duplicate, never a loss).
    o = page(
        [("Alpha", f"Alpha overview.\n{SHAPE_A} Not in EU.\n{SHAPE_B} Not in EU."), ("Beta", "Beta overview.")]
        + SHAPE_OTHERS
    )
    b = (
        o.replace(f"\n{SHAPE_A} Not in EU.", "")
        .replace(f"{SHAPE_B} Not in EU.", SHAPE_B)
        .replace("Beta overview.", shape_grow(SHAPE_A + " Not in EU."))
    )
    yield "E3", o, b, [SHAPE_A], "separate", {SHAPE_A: 2}


async def shape_run(original, body, facts, shape):
    alpha = next(unit for unit in page_units(body) if unit.label.endswith("Alpha"))
    target = change(body)
    model = ShapeFlags(alpha.id, facts, shape)
    # 9000 before review packing stopped growing views at 97% of the budget:
    # 9280 keeps the same packed partial views, so each shape stays the one tested.
    result = await editorial.review(
        model,
        "Responses API",
        target,
        [],
        [],
        char_budget=9280,
        mode="preservation",
        original_body=original,
        original_meta=target.draft_meta,
        namespace="existing_preservation",
    )
    kept = [failure["detail"] for failure in result["failures"]]
    if not kept:
        return kept, body
    state = target.check_progress["existing_preservation"]
    state.update(
        original_body=original,
        original_meta=target.draft_meta,
        canonical_binding={"commit": "c" * 40, "blob": "b" * 40, "path": target.path},
    )
    from doclayer.curator import editing

    repaired = await editing.edit_page(
        ShapeRestore(), "Responses API", target, [], body, [], char_budget=12000, today="2026-10-06", repair=True
    )
    return kept, repaired["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(shape_cases()), ids=lambda case: case[0])
async def test_repair_counts_after_review_and_repair(case):
    name, original, body, facts, shape, expected = case
    kept, repaired = await shape_run(original, body, facts, shape)
    for fact, count in expected.items():
        assert repaired.count(fact) == count, (name, fact, repaired.count(fact))
        assert repaired.count(fact) >= min(original.count(fact), 1)  # never a loss


def test_leading_qualifier_presence():
    """A leading qualifier sentence stays attached to the sentence it qualifies (plus a control)."""
    M = editorial._moved_out_of_view
    pt = {
        "mode": "preservation",
        "candidate_outline": [],
        "sections": [{"id": "alpha"}],
        "target_section_ids": ["alpha"],
        "body": "",
    }
    alpha = {"id": "alpha", "text": "## Alpha\nAlpha overview.\n"}
    plain = {"detail": f"Original '{SHAPE_A}' is not represented."}
    o = {"alpha": {"text": f"## Alpha\nAlpha overview.\n{SHAPE_A} Not in EU.\n{SHAPE_B} Only in US.\n"}}
    both = {"detail": f"Original '{SHAPE_A}' and '{SHAPE_B}' are not represented."}
    assert not M(
        both, pt, [alpha, {"id": "beta", "text": f"## Beta\n{SHAPE_A} Only in US.\n{SHAPE_B} Not in EU.\n"}], o, {}
    )
    o = {"alpha": {"text": f"## Alpha\nAlpha overview.\nApplies to enterprise plans only. {SHAPE_A}\n"}}
    assert not M(
        plain,
        pt,
        [
            {"id": "alpha", "text": "## Alpha\nAlpha overview.\nApplies to enterprise plans only.\n"},
            {"id": "beta", "text": f"## Beta\n{SHAPE_A}\n"},
        ],
        o,
        {},
    )
    assert not M(
        plain,
        pt,
        [
            {"id": "alpha", "text": "## Alpha\nAlpha overview. Applies to enterprise plans only.\n"},
            {"id": "beta", "text": f"## Beta\n{SHAPE_A}\n"},
        ],
        o,
        {},
    )
    assert M(
        plain, pt, [alpha, {"id": "beta", "text": f"## Beta\nApplies to enterprise plans only. {SHAPE_A}\n"}], o, {}
    )  # control


# --------------------------------------------------------------------------- #
# Span-diff rule for re-inserted MOVED sentences.
# --------------------------------------------------------------------------- #

from doclayer.curator import editing as _editing  # noqa: E402

LEAD = "Applies to enterprise plans only."


def sound(original, draft, out, bound):
    """Every bound fact present with its qualifiers, no fragments, no new statements.

    A bound fact is present when its sentence sequence is contiguous within one
    section (a repair may attach it across a line break)."""
    streams = [[s for line in unit.text.splitlines() for s in editorial._sentences(line)] for unit in page_units(out)]
    for text in bound:
        assert any(editorial._holds(stream, editorial._sentences(text)) for stream in streams), text
    sentences = {s for text in (original, draft) for line in text.splitlines() for s in editorial._sentences(line)}
    for line in out.splitlines():
        assert not line.startswith(" ") and "  " not in line and not line.endswith(" "), repr(line)
        for sentence in editorial._sentences(line):
            assert sentence in sentences, sentence  # nothing new or truncated


class QuoteFlags(Recorder):
    """Fail-closed narrowed Alpha reviewer quoting one original text."""

    def __init__(self, alpha_id, quote):
        super().__init__()
        self.alpha_id, self.quote = alpha_id, quote

    async def chat_bounded_json(self, system, user, *, char_budget):
        result = await super().chat_bounded_json(system, user, char_budget=char_budget)
        frame = self.frames[-1][1]
        if frame["mode"] != "preservation" or frame["target_section_ids"] != [self.alpha_id]:
            return result
        return {
            "complete": True,
            "conflicts": [],
            "failures": [
                {
                    "severity": "blocker",
                    "check": "preservation",
                    "section_id": self.alpha_id,
                    "anchor": None,
                    "detail": f"Original Alpha section stated '{self.quote}' and it is not represented.",
                }
            ],
        }


class QuoteRestore:
    """Restores quoted original text beside an anchor (reattach) or by append."""

    def __init__(self, anchor=None):
        self.anchor = anchor
        self.prompts = []

    async def chat_json(self, system, user):
        self.prompts.append(user)
        rows = json.loads(user.split("Article review repairs for this exact section: ", 1)[1].split("\n", 1)[0])
        facts = [
            next(group for group in match if group)
            for row in rows
            for match in editorial._QUOTED.findall(row["detail"])
        ]
        section = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
        if self.anchor:
            return {
                "section_id": section,
                "edits": [{"old": self.anchor, "new": self.anchor + " " + " ".join(facts)}],
                "append": "",
                "handled_claim_ids": [],
                "claim_dispositions": [],
                "notes": [],
            }
        return {
            "section_id": section,
            "edits": [],
            "append": " ".join(facts),
            "handled_claim_ids": [],
            "claim_dispositions": [],
            "notes": [],
        }


def quote_cases():
    o = page([("Alpha", f"Alpha overview paragraph.\n{LEAD} {SHAPE_A}"), ("Beta", "Beta overview.")] + SHAPE_OTHERS)
    b = o.replace(f"{LEAD} {SHAPE_A}", LEAD).replace("Beta overview.", shape_grow(SHAPE_A))
    yield "V1b-append", o, b, SHAPE_A, None, [f"{LEAD} {SHAPE_A}"]
    yield "V1b-reattach", o, b, SHAPE_A, LEAD, [f"{LEAD} {SHAPE_A}"]
    b2 = o.replace(f"Alpha overview paragraph.\n{LEAD} {SHAPE_A}", f"Alpha overview paragraph. {LEAD}").replace(
        "Beta overview.", shape_grow(SHAPE_A)
    )
    yield "V1b2", o, b2, SHAPE_A, LEAD, [f"{LEAD} {SHAPE_A}"]
    o = page([("Alpha", f"Alpha overview.\n{SHAPE_A} Not in EU."), ("Beta", "Beta overview.")] + SHAPE_OTHERS)
    b = o.replace(f"\n{SHAPE_A} Not in EU.", "").replace("Beta overview.", shape_grow(SHAPE_A))
    yield "P0", o, b, f"{SHAPE_A} Not in EU.", None, [f"{SHAPE_A} Not in EU."]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(quote_cases()), ids=lambda case: case[0])
async def test_detached_qualifier_shapes_end_with_the_qualifier_attached(case):
    name, original, body, quote, anchor, bound = case
    alpha = next(unit for unit in page_units(body) if unit.label.endswith("Alpha"))
    target = change(body)
    result = await editorial.review(
        QuoteFlags(alpha.id, quote),
        "Responses API",
        target,
        [],
        [],
        char_budget=9000,
        mode="preservation",
        original_body=original,
        original_meta=target.draft_meta,
        namespace="existing_preservation",
    )
    assert result["failures"]  # the fail-closed finding stands
    assert "moved_sentences" not in target.check_progress["existing_preservation"]  # not moved intact
    target.check_progress["existing_preservation"].update(
        original_body=original,
        original_meta=target.draft_meta,
        canonical_binding={"commit": "c" * 40, "blob": "b" * 40, "path": target.path},
    )
    out = (
        await _editing.edit_page(
            QuoteRestore(anchor),
            "Responses API",
            target,
            [],
            body,
            [],
            char_budget=12000,
            today="2026-10-06",
            repair=True,
        )
    )["body"]
    sound(original, body, out, bound)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(shape_cases()), ids=lambda case: case[0])
async def test_shapes_end_sound(case):
    name, original, body, facts, shape, expected = case
    kept, repaired = await shape_run(original, body, facts, shape)
    bound = [SHAPE_A + " Not in EU."] if name == "E3" else facts
    sound(original, body, repaired, bound)


@pytest.mark.asyncio
async def test_lines_elsewhere_are_information_for_repair_and_never_removed():
    """W1: a whole line moved intact; repair is informed, and its output is never cut."""
    original = page(
        [("Alpha", f"Alpha overview paragraph.\n{SHAPE_MOVED}\n{SHAPE_DELETED}"), ("Beta", "Beta overview.")]
        + SHAPE_OTHERS
    )
    body = original.replace(f"\n{SHAPE_MOVED}\n{SHAPE_DELETED}", "").replace(
        "Beta overview.", "Beta overview. " + filler("beta", 260) + f"\n{SHAPE_MOVED}"
    )
    alpha = next(unit for unit in page_units(body) if unit.label.endswith("Alpha"))
    target = change(body)
    model = ShapeFlags(alpha.id, [SHAPE_MOVED, SHAPE_DELETED], "separate")
    result = await editorial.review(
        model,
        "Responses API",
        target,
        [],
        [],
        char_budget=9000,
        mode="preservation",
        original_body=original,
        original_meta=target.draft_meta,
        namespace="existing_preservation",
    )
    assert [f["detail"] for f in result["failures"]] == [
        f"Original Alpha section stated '{SHAPE_DELETED}' and it is not represented."
    ]
    assert target.check_progress["existing_preservation"]["lines_elsewhere"] == [
        {"section_id": alpha.id, "line": SHAPE_MOVED}
    ]
    target.check_progress["existing_preservation"].update(
        original_body=original,
        original_meta=target.draft_meta,
        canonical_binding={"commit": "c" * 40, "blob": "b" * 40, "path": target.path},
    )

    class Overeager(QuoteRestore):
        """Re-inserts the moved sentence anyway."""

        async def chat_json(self, system, user):
            self.prompts.append(user)
            section = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
            return {
                "section_id": section,
                "edits": [{"old": "Alpha overview paragraph.", "new": f"Alpha overview paragraph.\n{SHAPE_MOVED}"}],
                "append": SHAPE_DELETED,
                "handled_claim_ids": [],
                "claim_dispositions": [],
                "notes": [],
            }

    model = Overeager()
    out = (
        await _editing.edit_page(
            model, "Responses API", target, [], body, [], char_budget=12000, today="2026-10-06", repair=True
        )
    )["body"]
    assert (
        f'These sentences already exist elsewhere on the page: ["{SHAPE_MOVED}"]. Do not re-insert them unless '
        "a sentence is needed to keep a fact's qualifier attached."
    ) in model.prompts[0]
    assert out.count(SHAPE_MOVED) == 2 and out.count(SHAPE_DELETED) == 1  # output kept as the repair wrote it
    sound(original, body, out, [SHAPE_MOVED, SHAPE_DELETED])


@pytest.mark.asyncio
async def test_a_line_quoted_by_a_kept_finding_is_never_listed_elsewhere():
    """Minor 4 (V6a shape): A's line moved intact, but a kept finding quotes it."""
    original = page(
        [("Alpha", f"Alpha overview.\n{SHAPE_A} Not in EU.\n{SHAPE_B} Not in EU."), ("Beta", "Beta overview.")]
        + SHAPE_OTHERS
    )
    body = (
        original.replace(f"\n{SHAPE_A} Not in EU.", "")
        .replace(f"{SHAPE_B} Not in EU.", SHAPE_B)
        .replace("Beta overview.", shape_grow(f"{SHAPE_A} Not in EU."))
    )
    alpha = next(unit for unit in page_units(body) if unit.label.endswith("Alpha"))
    target = change(body)
    model = ShapeFlags(alpha.id, [f"{SHAPE_A} Not in EU.", f"{SHAPE_B} Not in EU."], "separate")
    result = await editorial.review(
        model,
        "Responses API",
        target,
        [],
        [],
        char_budget=9000,
        mode="preservation",
        original_body=original,
        original_meta=target.draft_meta,
        namespace="existing_preservation",
    )
    assert any(SHAPE_A in f["detail"] for f in result["failures"])
    assert "lines_elsewhere" not in target.check_progress["existing_preservation"]


@pytest.mark.asyncio
async def test_new_page_repair_gets_no_elsewhere_information():
    """A create never consults lines found elsewhere, even if a review listed some."""
    original = page([("Alpha", "Alpha overview."), ("Gamma", "Gamma imports are available. Not in EU.")])
    target = PageChange(
        "g",
        "wiki/openai-responses-api.md",
        "create",
        title="Responses API",
        draft_body=original,
        draft_meta={"title": "Responses API"},
    )
    review = editorial_review_for(original, [(1, "Alpha overview.", "Add the imports availability.", "preservation")])
    review.update(
        original_body=original,
        original_meta={"title": "Responses API"},
        canonical_binding={"commit": "c" * 40, "blob": "b" * 40, "path": target.path},
        lines_elsewhere=[{"section_id": page_units(original)[1].id, "line": "Not in EU."}],
    )
    target.check_progress = {"existing_preservation": review}
    prompts = []

    class Client:
        async def chat_json(self, system, user):
            prompts.append(user)
            section = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
            return {
                "section_id": section,
                "edits": [],
                "append": "Alpha imports are available. Not in EU.",
                "handled_claim_ids": [],
                "claim_dispositions": [],
                "notes": [],
            }

    out = (
        await _editing.edit_page(
            Client(), "Responses API", target, [], original, [], char_budget=12000, today="2026-10-06", repair=True
        )
    )["body"]
    assert "Alpha imports are available. Not in EU." in out and "already exist elsewhere" not in prompts[0]


def editorial_review_for(body, rows):
    sections = []
    offset = 0
    for unit in page_units(body):
        sections.append({"id": unit.id, "label": unit.label, "start": offset, "end": offset + len(unit.text)})
        offset += len(unit.text)
    return {
        "editorial_version": 1,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "sections": sections,
        "failures": [
            {
                "severity": "blocker",
                "check": check,
                "detail": detail,
                "section_id": sections[index]["id"],
                "anchor": anchor,
            }
            for index, anchor, detail, check in rows
        ],
    }


# --------------------------------------------------------------------------- #
# Acceptance: every case ends with zero losses, zero detached
# qualifiers, zero fragments and zero joined sentences. Duplicates are fine.
# --------------------------------------------------------------------------- #


def accept(original, draft, out, bound):
    vocabulary = {s for text in (original, draft) for line in text.splitlines() for s in editorial._sentences(line)}
    streams = [[s for line in unit.text.splitlines() for s in editorial._sentences(line)] for unit in page_units(out)]
    present = {s for stream in streams for s in stream}
    lost = [
        s
        for line in original.splitlines()
        if not line.startswith("#")
        for s in editorial._sentences(line)
        if s not in present
    ]
    assert not lost, ("loss", lost)
    detached = [
        text for text in bound if not any(editorial._holds(stream, editorial._sentences(text)) for stream in streams)
    ]
    assert not detached, ("detached qualifier", detached)
    joined = [
        s
        for line in out.splitlines()
        if not line.startswith("#")
        for s in editorial._sentences(line)
        if s not in vocabulary
    ]
    assert not joined, ("joined or fragment sentence", joined)
    residue = [line for line in out.splitlines() if line.startswith(" ") or line.endswith(" ") or "  " in line]
    assert not residue and "\n\n\n" not in out, ("whitespace residue", residue)


class RowFlags(Recorder):
    def __init__(self, alpha_id, rows):
        super().__init__()
        self.alpha_id, self.rows = alpha_id, rows

    async def chat_bounded_json(self, system, user, *, char_budget):
        result = await super().chat_bounded_json(system, user, char_budget=char_budget)
        frame = self.frames[-1][1]
        if frame["mode"] != "preservation" or frame["target_section_ids"] != [self.alpha_id]:
            return result
        return {
            "complete": True,
            "conflicts": [],
            "failures": [
                {"severity": "blocker", "check": check, "section_id": self.alpha_id, "anchor": anchor, "detail": detail}
                for check, anchor, detail in self.rows
            ],
        }


class Fix:
    def __init__(self, edits, append=""):
        self.edits, self.append, self.prompts = edits, append, []

    async def chat_json(self, system, user):
        self.prompts.append(user)
        section = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
        return {
            "section_id": section,
            "edits": [{"old": old, "new": new} for old, new in self.edits],
            "append": self.append,
            "handled_claim_ids": [],
            "claim_dispositions": [],
            "notes": [],
        }


def reply_cases():
    a, b, q, imp = SHAPE_A, SHAPE_B, "Not in EU.", "Alpha imports are available."
    o = page([("Alpha", f"Alpha overview.\n{a} {q}\n{b} {q}"), ("Beta", "Beta overview.")] + SHAPE_OTHERS)
    d = o.replace(f"\n{a} {q}", "").replace(f"{b} {q}", b).replace("Beta overview.", shape_grow(f"{a} {q}"))
    rows = [
        ("preservation", None, f"Original Alpha section stated '{a} {q}' and it is not represented."),
        ("preservation", None, f"Original Alpha section stated '{b} {q}' and it is not represented."),
    ]
    yield "V6a-edit", o, d, rows, [], Fix([(b, f"{b} {q}")]), [f"{a} {q}", f"{b} {q}"]
    yield "V6a-append", o, d, rows, [], Fix([], f"{b} {q}"), [f"{a} {q}", f"{b} {q}"]
    o = page([("Alpha", f"Alpha overview.\n{a} {q}\n{imp}"), ("Beta", "Beta overview.")] + SHAPE_OTHERS)
    d = o.replace(f"\n{a} {q}", "").replace("Beta overview.", shape_grow(f"{a} {q}"))
    rows = [("grounding", imp, f"'{imp}' omits the source's restriction that imports are not available in the EU.")]
    yield (
        "V6b-edit",
        o,
        d,
        rows,
        [source("Alpha imports are available, but not in the EU.", "claim-imports")],
        Fix([(imp, f"{imp} {q}")]),
        [f"{imp} {q}", f"{a} {q}"],
    )
    im, bat, rl = "Imports need approval.", "Batch needs approval.", "Batch is rate limited."
    o = page([("Alpha", f"{im} {q}\n\n{a} {q}\n\n{bat} {q} {rl}"), ("Beta", "Beta overview.")] + SHAPE_OTHERS)
    d = o.replace(f"\n\n{a} {q}", "").replace(f" {rl}", "").replace("Beta overview.", shape_grow(f"{a} {q}"))
    rows = [
        ("preservation", None, f"Original Alpha section stated '{a} {q}' and it is not represented."),
        ("preservation", None, f"Original Alpha section stated '{rl}' and it is not represented."),
    ]
    yield (
        "V6c-overeager",
        o,
        d,
        rows,
        [],
        Fix([(f"{im} {q}", f"{im} {q}\n\n{a} {q}"), (f"{bat} {q}", f"{bat} {q} {rl}")]),
        [f"{im} {q}", f"{a} {q}", f"{bat} {q} {rl}"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(reply_cases()), ids=lambda case: case[0])
async def test_every_reply_case_ends_without_loss_end_to_end(case):
    name, original, body, rows, records, fix, bound = case
    alpha = next(unit for unit in page_units(body) if unit.label.endswith("Alpha"))
    target = change(body)
    await editorial.review(
        RowFlags(alpha.id, rows),
        "Responses API",
        target,
        [],
        [],
        char_budget=9000,
        mode="preservation",
        original_body=original,
        original_meta=target.draft_meta,
        namespace="existing_preservation",
    )
    target.check_progress["existing_preservation"].update(
        original_body=original,
        original_meta=target.draft_meta,
        canonical_binding={"commit": "c" * 40, "blob": "b" * 40, "path": target.path},
    )
    out = (
        await _editing.edit_page(
            fix, "Responses API", target, records, body, [], char_budget=12000, today="2026-10-06", repair=True
        )
    )["body"]
    vocabulary_draft = body + "\n" + "\n".join(new for _, new in fix.edits) + "\n" + fix.append
    accept(original, vocabulary_draft, out, bound)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(quote_cases()), ids=lambda case: case[0])
async def test_every_quote_case_ends_without_loss_end_to_end(case):
    name, original, body, quote, anchor, bound = case
    alpha = next(unit for unit in page_units(body) if unit.label.endswith("Alpha"))
    target = change(body)
    await editorial.review(
        QuoteFlags(alpha.id, quote),
        "Responses API",
        target,
        [],
        [],
        char_budget=9000,
        mode="preservation",
        original_body=original,
        original_meta=target.draft_meta,
        namespace="existing_preservation",
    )
    target.check_progress["existing_preservation"].update(
        original_body=original,
        original_meta=target.draft_meta,
        canonical_binding={"commit": "c" * 40, "blob": "b" * 40, "path": target.path},
    )
    out = (
        await _editing.edit_page(
            QuoteRestore(anchor),
            "Responses API",
            target,
            [],
            body,
            [],
            char_budget=12000,
            today="2026-10-06",
            repair=True,
        )
    )["body"]
    accept(original, body, out, bound)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(shape_cases()), ids=lambda case: case[0])
async def test_acceptance_shape_end_to_end(case):
    name, original, body, facts, shape, expected = case
    kept, out = await shape_run(original, body, facts, shape)
    bound = [SHAPE_A + " Not in EU."] if name == "E3" else []
    if name == "E3":
        original = original.replace(f"{SHAPE_B} Not in EU.", SHAPE_B)  # the draft removed this qualifier by evidence
    if name == "E2":
        original = original.replace(
            "Alpha overview paragraph. " + SHAPE_MOVED, "Alpha overview paragraph."
        )  # dedupe edit
    accept(original, body, out, bound)


@pytest.mark.asyncio
async def test_saved_review_state_drops_the_obsolete_moved_sentences_key():
    original = page([("Tools", "Built-in tools include shell."), ("State", "Conversation state is stored.")])
    body = original.replace("shell.", "shell and web search.")
    target = change(body)
    await editorial.review(
        Recorder(),
        "Responses API",
        target,
        [],
        [],
        char_budget=12000,
        mode="preservation",
        original_body=original,
        original_meta=target.draft_meta,
        namespace="existing_preservation",
    )
    saved = target.check_progress["existing_preservation"]
    assert saved["complete"] and saved["results"]
    saved["moved_sentences"] = [
        {"section_id": "section-x", "sentence": "Built-in tools include shell."}
    ]  # in-flight job
    model = Recorder()
    await editorial.review(
        model,
        "Responses API",
        target,
        [],
        [],
        char_budget=12000,
        mode="preservation",
        original_body=original,
        original_meta=target.draft_meta,
        namespace="existing_preservation",
    )
    assert model.prompts == []  # the saved state was resumed, not rebuilt
    assert "moved_sentences" not in target.check_progress["existing_preservation"]
