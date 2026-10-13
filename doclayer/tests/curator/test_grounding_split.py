"""An over-budget grounding section splits by sentence group and cited source.

Shape of a real grounding job (section "Model Selection Context" of a model
page): the section's complete relevant evidence (22420 characters) did
not fit, but every sentence cites one admitted source whose complete records
do. A sentence is judged only against its own cited sources' complete records.
"""

import hashlib
import json
import re

import pytest

from doclayer.curator import editorial
from doclayer.curator.editing import page_units
from doclayer.curator.state import Evidence, PageChange

RESERVE = editorial.FORMAT_REPAIR_RESERVE
CHECKLIST = "https://developers.openai.com/api/docs/guides/deployment-checklist"
DATA = "https://developers.openai.com/api/docs/guides/your-data"
SELECTION = "https://developers.openai.com/api/docs/guides/model-selection"  # cited, never admitted


def record(source_id, url, text, identity):
    return Evidence(
        source_id,
        "Guide",
        "## Guide",
        "quote",
        text,
        url=url,
        quote=text,
        claim_id=identity,
        support_status="supported",
    )


def filler(name, count):
    return " ".join(f"{name} model guidance detail {index}." for index in range(count))


def page(sections):
    return "# GPT-6 Astra\nAstra overview.\n" + "".join(f"## {title}\n{text}\n" for title, text in sections)


def change(body, ids=()):
    return PageChange(
        "g",
        "wiki/gpt-6-astra.md",
        "update",
        title="GPT-6 Astra",
        draft_body=body,
        draft_meta={"title": "GPT-6 Astra"},
        claim_ids=list(ids),
    )


def strip_citations(sentence):
    return " ".join(re.sub(r"\[source:[^\]]*\]|\[\^[^\]]*\]", "", sentence).split()).replace(" .", ".")


class Grounder:
    """Flags each reviewed sentence no supplied record states verbatim (its citation removed)."""

    def __init__(self):
        self.frames = []
        self.prompts = []

    async def chat_bounded_json(self, system, user, *, char_budget):
        assert len(system) + len(user) + RESERVE <= char_budget
        self.prompts.append(system + "\x00" + user)
        frame = json.loads(user.split("\n", 1)[1])
        self.frames.append((system, frame))
        if frame["mode"] != "grounding":
            return {"complete": True, "failures": [], "conflicts": []}
        texts = {" ".join(row["text"].split()) for row in frame["evidence"]}
        section = frame["sections"][0]["id"]
        reviewed = frame.get("review_only_sentences") or [
            s for line in frame["body"].splitlines() if not line.startswith("#") for s in editorial._sentences(line)
        ]
        failures = [
            {
                "severity": "blocker",
                "check": "grounding",
                "section_id": section,
                "anchor": None,
                "detail": f"Unsupported: '{strip_citations(sentence).split(':** ')[-1]}'",
            }
            for sentence in reviewed
            if not any(text in strip_citations(sentence) for text in texts)
        ]
        return {"complete": True, "failures": failures, "conflicts": []}


SUPPORTED = "GPT-6 Astra is intended for ambiguous problems and deep analysis."
SUPPORTED_2 = "Choose the model that performs well on representative tasks."
UNSUPPORTED = "GPT-6 Astra is free for every organization."
CARRIED = "Astra requires the Responses API for tool calling."
CARRIED_2 = "GPT-6 Luna supports function calling in Chat Completions."


def astra_records():
    checklist = [
        record("checklist", CHECKLIST, text, f"claim-checklist-{index}")
        for index, text in enumerate(
            [SUPPORTED, SUPPORTED_2, CARRIED, CARRIED_2, "Unrelated rollout checklist wording about canaries."]
        )
    ]
    # A large, highly relevant second source the section never cites.
    data = [
        record(
            "data",
            DATA,
            f"GPT-6 Astra model guidance retention detail {index}. " + filler(f"data{index}", 25),
            f"claim-data-{index}",
        )
        for index in range(12)
    ]
    return checklist, data


def selection_body(extra=""):
    text = (
        f"*   **GPT-6 Astra:** {SUPPORTED[:-1]} [source: {SELECTION}] [source: {CHECKLIST}].\n\n"
        f"{SUPPORTED_2[:-1]} [source: {CHECKLIST}].\n\n"
        f"{CARRIED} {CARRIED_2[:-1]} [source: {CHECKLIST}].\n" + extra
    )
    return page([("Specifications", "Astra specifications."), ("Model Selection Context", text)])


async def run(model, body, records, budget, original=None):
    target = change(body)
    return await editorial.review(
        model,
        "GPT-6 Astra",
        target,
        records,
        [],
        char_budget=budget,
        original_body=original if original is not None else page([("Specifications", "Astra specifications.")]),
        original_meta=target.draft_meta,
    )


@pytest.mark.asyncio
async def test_a_large_section_grounds_each_sentence_on_its_cited_sources_complete_records():
    checklist, data = astra_records()
    body = selection_body()
    model = Grounder()
    result = await run(model, body, checklist + data, 9000)
    split = [frame for _, frame in model.frames if "review_only_sentences" in frame]
    assert len(split) == 1 and split[0]["evidence_scope"] == "complete records of the cited sources checklist"
    # Every record of the cited source, including one no sentence shares words with; none of the uncited source.
    assert sorted(row["id"] for row in split[0]["evidence"]) == sorted(r.claim_id for r in checklist)
    assert len(split[0]["review_only_sentences"]) == 4
    assert result["failures"] == []  # supported sentences are never flagged after the split
    system = next(system for system, frame in model.frames if "review_only_sentences" in frame)
    assert system.endswith(editorial.SENTENCE_SCOPE_GUIDANCE)


@pytest.mark.asyncio
async def test_unsupported_sentence_is_still_flagged_after_the_split():
    checklist, data = astra_records()
    body = selection_body(f"\n{UNSUPPORTED[:-1]} [source: {CHECKLIST}].\n")
    result = await run(Grounder(), body, checklist + data, 9000)
    assert [f["detail"] for f in result["failures"]] == [f"Unsupported: '{UNSUPPORTED}'"]


@pytest.mark.asyncio
async def test_footnote_citations_resolve_to_their_source():
    checklist, data = astra_records()
    text = f"{SUPPORTED[:-1]}[^1].\n\n[^1]: Deployment checklist, {CHECKLIST}\n"
    body = page([("Specifications", "Astra specifications."), ("Model Selection Context", text)])
    model = Grounder()
    result = await run(model, body, checklist + data, 9000)
    split = [frame for _, frame in model.frames if "review_only_sentences" in frame]
    assert split and split[0]["evidence_scope"] == "complete records of the cited sources checklist"
    assert result["failures"] == []


def big_two_source_records():
    a = [record("a", CHECKLIST, SUPPORTED, "claim-a-0")] + [
        record("a", CHECKLIST, f"Checklist detail {i}. " + filler(f"a{i}", 6), f"claim-a-{i + 1}") for i in range(5)
    ]
    b = [record("b", DATA, f"Data detail {i}. " + filler(f"b{i}", 6), f"claim-b-{i}") for i in range(6)]
    return a, b


@pytest.mark.asyncio
@pytest.mark.parametrize("sentence,flagged", [(SUPPORTED, False), (UNSUPPORTED, True)])
async def test_one_source_at_a_time_when_a_sentences_cited_sources_overflow(sentence, flagged):
    a, b = big_two_source_records()
    body = page(
        [
            ("Specifications", "Astra specifications."),
            ("Model Selection Context", f"{sentence[:-1]} [source: {CHECKLIST}] [source: {DATA}]."),
        ]
    )
    model = Grounder()
    result = await run(model, body, a + b, 9000)
    alternatives = [frame for _, frame in model.frames if "support_alternative" in frame]
    assert len(alternatives) == 2  # one task per cited source, each with that source's complete records
    assert {tuple(sorted(row["id"] for row in frame["evidence"])) for frame in alternatives} == {
        tuple(sorted(r.claim_id for r in a)),
        tuple(sorted(r.claim_id for r in b)),
    }
    assert [f["detail"] for f in result["failures"]] == ([f"Unsupported: '{UNSUPPORTED}'"] if flagged else [])


@pytest.mark.asyncio
async def test_single_source_overflow_for_one_sentence_is_grounded_in_record_chunks():
    """One source's complete records that overflow for a
    sentence are split, in order, into chunks of complete records; the sentence
    is unsupported only if every chunk flags it (here no record states it)."""
    a = [record("a", CHECKLIST, f"Checklist detail {i}. " + filler(f"a{i}", 40), f"claim-a-{i}") for i in range(12)]
    body = page(
        [
            ("Specifications", "Astra specifications."),
            ("Model Selection Context", f"{SUPPORTED[:-1]} [source: {CHECKLIST}]."),
        ]
    )
    model = Grounder()
    result = await run(model, body, a, 9000)
    chunks = [frame for _, frame in model.frames if "evidence_chunk" in frame]
    assert len(chunks) > 1 and all(
        system.endswith(editorial.SOURCE_CHUNK_GUIDANCE) for system, frame in model.frames if "evidence_chunk" in frame
    )
    shown = [row["id"] for frame in chunks for row in frame["evidence"]]
    assert shown == [f"claim-a-{i}" for i in range(12)]  # complete records, in order, each once
    assert [(c["evidence_chunk"]["first_record"], c["evidence_chunk"]["last_record"]) for c in chunks][-1][1] == 12
    assert [f["anchor"] for f in result["failures"] if f["check"] == "grounding"] == [
        f"{SUPPORTED[:-1]} [source: {CHECKLIST}]."
    ]


@pytest.mark.asyncio
async def test_new_uncited_sentence_is_flagged_without_an_evidence_call():
    checklist, data = astra_records()
    body = page(
        [
            ("Specifications", "Astra specifications."),
            ("Model Selection Context", f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\nBilling invoices arrive monthly."),
        ]
    )
    model = Grounder()
    result = await run(model, body, checklist + data, 9000)
    assert all(
        "Billing invoices arrive monthly." not in frame.get("review_only_sentences", []) for _, frame in model.frames
    )
    assert [(f["check"], f["detail"], f["anchor"]) for f in result["failures"]] == [
        (
            "grounding",
            "New fact without a citation: 'Billing invoices arrive monthly.'. Cite the source that states it, or remove it.",
            "Billing invoices arrive monthly.",
        )
    ]


@pytest.mark.asyncio
async def test_canonical_uncited_sentences_are_retained_and_never_grounded():
    checklist, data = astra_records()
    canonical = "Astra keeps its benchmark notes in the appendix."
    base_text = f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{canonical}\n"
    original, body = base_and_draft(base_text, base_text + f"\n{SUPPORTED_2[:-1]} [source: {CHECKLIST}].\n")
    model = Grounder()
    _, result = await review(model, body, original, checklist + data)
    assert all(canonical not in frame.get("review_only_sentences", []) for _, frame in model.frames)
    assert result["failures"] == []
    assert result["notes"] == [f"{editorial.RETAINED_NOTE}: '{canonical}'"]


@pytest.mark.asyncio
async def test_many_uncited_sentences_fit_without_their_evidence():
    """job-ce36c8b003: sections with 22, 12 and 2 uncited sentences needed up to 39436 characters."""
    checklist, data = astra_records()
    old = [f"Astra canonical behaviour note {i} stays documented." for i in range(12)]
    new = [f"Astra new behaviour note {i} is described here." for i in range(10)]
    base_text = "\n".join(old) + "\n"
    original, body = base_and_draft(base_text, base_text + "\n".join(new) + "\n")
    model = Grounder()
    _, result = await review(model, body, original, checklist + data)
    assert all(not frame.get("review_only_sentences") for _, frame in model.frames)  # nothing uncited is grounded
    assert sorted(f["detail"] for f in result["failures"]) == sorted(
        f"New fact without a citation: '{sentence}'. Cite the source that states it, or remove it." for sentence in new
    )
    assert sorted(result["notes"]) == sorted(f"{editorial.RETAINED_NOTE}: '{sentence}'" for sentence in old)


UNDER_BUDGET_DIGEST = "52b967efecce99601ad3dc2b195803dba8a97f4fe6abb9727fec53860a59d6fa"


@pytest.mark.asyncio
async def test_under_budget_grounding_prompts_are_byte_identical():
    checklist, data = astra_records()
    model = Grounder()
    await run(model, selection_body(), checklist + data[:2], 32768)
    assert all("review_only_sentences" not in frame for _, frame in model.frames)
    assert hashlib.sha256("\x01".join(model.prompts).encode()).hexdigest() == UNDER_BUDGET_DIGEST


ELSEWHERE = "GPT-6 Astra keeps prompts for thirty days by default."
UNADMITTED = "GPT-6 Astra suits ambiguous research problems."


def recheck_records():
    checklist, _ = astra_records()
    # The uncited source: many relevant records, more than one re-check chunk holds.
    data = [
        record("data", DATA, f"GPT-6 Astra retention note {i}. " + filler(f"r{i}", 18), f"claim-data-{i}")
        for i in range(10)
    ]
    data += [
        record("data", DATA, ELSEWHERE, "claim-data-support"),
        record("data", DATA, UNADMITTED, "claim-data-unadmitted"),
    ]
    return checklist, data


BASE_UNVERIFIED = "GPT-6 Astra was benchmarked on internal suites."


# --------------------------------------------------------------------------- #
# Cited-source grounding only; canonical text is retained.
# --------------------------------------------------------------------------- #

BASE_UNVERIFIED = "GPT-6 Astra was benchmarked on internal suites."


def base_and_draft(base_text, draft_text):
    original = page([("Specifications", "Astra specifications."), ("Model Selection Context", base_text)])
    return original, original.replace(base_text, draft_text)


async def review(model, body, original, records):
    target = change(body)
    result = await editorial.review(
        model,
        "GPT-6 Astra",
        target,
        records,
        [],
        char_budget=9000,
        original_body=original,
        original_meta=target.draft_meta,
    )
    return target, result


@pytest.mark.asyncio
async def test_unsupported_canonical_sentence_is_retained_with_a_note_and_new_one_flagged():
    checklist, data = recheck_records()
    base_text = f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}].\n"
    original, body = base_and_draft(base_text, base_text + f"\n{UNSUPPORTED[:-1]} [source: {CHECKLIST}].\n")
    target, result = await review(Grounder(), body, original, checklist + data)
    assert [f["detail"] for f in result["failures"]] == [f"Unsupported: '{UNSUPPORTED}'"]
    assert result["notes"] == [f"{editorial.RETAINED_NOTE}: '{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}].'"]
    assert target.check_progress["article_review"]["notes"] == result["notes"]


@pytest.mark.asyncio
async def test_canonical_sentence_whose_citation_was_touched_is_still_canonical():
    """The editor only adds a citation to canonical text."""
    checklist, data = recheck_records()
    base_text = f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}].\n"
    original, body = base_and_draft(
        base_text,
        base_text.replace(
            f"{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}].",
            f"{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}] [source: {SELECTION}].",
        ),
    )
    _, result = await review(Grounder(), body, original, checklist + data)
    assert result["failures"] == []
    assert len(result["notes"]) == 1 and BASE_UNVERIFIED[:-1] in result["notes"][0]
    for touched in (
        f"{BASE_UNVERIFIED[:-1]}[^7].",
        f"{BASE_UNVERIFIED[:-1]} ([model selection guide]({SELECTION})).",
        f"{BASE_UNVERIFIED[:-1]} [guide]({SELECTION}).",
        f"{BASE_UNVERIFIED[:-1]} [docs]({SELECTION}).",
        f"- {BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}].",
        f"> {BASE_UNVERIFIED[:-1]}.",
    ):
        assert editorial._core(touched) == editorial._core(f"{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}].")


class Vague(Grounder):
    """Flags like Grounder, but with a paraphrased detail and no anchor."""

    async def chat_bounded_json(self, system, user, *, char_budget):
        out = await super().chat_bounded_json(system, user, char_budget=char_budget)
        for failure in out["failures"]:
            failure["detail"] = "The pricing claim is not in the evidence."
        return out


@pytest.mark.asyncio
async def test_unlocated_finding_in_a_mixed_group_is_resolved_per_sentence():
    """Base SUPPORTED, new SUPPORTED_2 (supported) and new UNSUPPORTED, vague finding."""
    checklist, data = recheck_records()
    base_text = f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n"
    original, body = base_and_draft(
        base_text,
        base_text + f"\n{SUPPORTED_2[:-1]} [source: {CHECKLIST}].\n\n{UNSUPPORTED[:-1]} [source: {CHECKLIST}].\n",
    )
    model = Vague()
    _, result = await review(model, body, original, checklist + data)
    singles = [frame for _, frame in model.frames if len(frame.get("review_only_sentences", [])) == 1]
    assert len(singles) == 3  # each reviewed sentence grounded alone against its own cited source
    assert all(
        sorted(row["id"] for row in frame["evidence"]) == sorted(r.claim_id for r in checklist) for frame in singles
    )
    # Attached to that one sentence and retargeted to its exact text.
    assert result["failures"] == [
        {
            "severity": "blocker",
            "check": "grounding",
            "section_id": page_units(body)[-1].id,
            "anchor": f"{UNSUPPORTED[:-1]} [source: {CHECKLIST}].",
            "detail": "The pricing claim is not in the evidence.",
        }
    ]
    assert "notes" not in result  # the supported base sentence gets no false note


@pytest.mark.asyncio
async def test_unlocated_finding_in_an_all_new_group_is_reported_once():
    checklist, data = recheck_records()
    text = f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{UNSUPPORTED[:-1]} [source: {CHECKLIST}].\n"
    body = page([("Specifications", "Astra specifications."), ("Model Selection Context", text)])
    result = await run(Vague(), body, checklist + data, 9000)
    assert [(f["check"], f["detail"]) for f in result["failures"]] == [
        ("grounding", "The pricing claim is not in the evidence.")
    ]


class Scaffolding(Grounder):
    async def chat_bounded_json(self, system, user, *, char_budget):
        out = await super().chat_bounded_json(system, user, char_budget=char_budget)
        frame = json.loads(user.split("\n", 1)[1])
        if frame.get("review_only_sentences") and frame["evidence_scope"].startswith("complete records"):
            out["failures"].append(
                {
                    "severity": "blocker",
                    "check": "task-scaffolding",
                    "section_id": frame["sections"][0]["id"],
                    "anchor": None,
                    "detail": "New unrelated reminder to check the dashboard weekly.",
                }
            )
        return out


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", [True, False])
async def test_non_grounding_findings_pass_through_untouched(canonical):
    """A task-scaffolding blocker survives, in base or new groups."""
    checklist, data = recheck_records()
    text = f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{SUPPORTED_2[:-1]} [source: {CHECKLIST}].\n"
    original = (
        page([("Specifications", "Astra specifications."), ("Model Selection Context", text)])
        if canonical
        else page([("Specifications", "Astra specifications.")])
    )
    # The section changes (a supported line is added), so its canonical sentences are reviewed too.
    body = page(
        [
            ("Specifications", "Astra specifications."),
            ("Model Selection Context", text + f"\n{CARRIED[:-1]} [source: {CHECKLIST}].\n"),
        ]
    )
    result = await run(Scaffolding(), body, checklist + data, 9000, original=original)
    assert [(f["check"], f["detail"]) for f in result["failures"]] == [
        ("task-scaffolding", "New unrelated reminder to check the dashboard weekly.")
    ]
    assert "notes" not in result


@pytest.mark.asyncio
async def test_new_sentence_unsupported_by_its_cited_sources_is_reported():
    """No re-check against uncited sources; a new sentence citing the wrong source is reported."""
    checklist, data = recheck_records()
    text = f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{ELSEWHERE[:-1]} [source: {CHECKLIST}].\n"
    body = page([("Specifications", "Astra specifications."), ("Model Selection Context", text)])
    model = Grounder()
    result = await run(model, body, checklist + data, 9000)
    assert [f["detail"] for f in result["failures"]] == [f"Unsupported: '{ELSEWHERE}'"]
    assert not any(frame.get("recheck") for _, frame in model.frames)


@pytest.mark.asyncio
async def test_each_review_of_a_changed_draft_grounds_it_again():
    """The review binding hashes the body: a repaired draft gets a fresh grounding review."""
    checklist, data = recheck_records()
    text = f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{UNSUPPORTED[:-1]} [source: {CHECKLIST}].\n"
    original = page([("Specifications", "Astra specifications.")])
    body = page([("Specifications", "Astra specifications."), ("Model Selection Context", text)])
    target = change(body)
    first = await editorial.review(
        Grounder(),
        "GPT-6 Astra",
        target,
        checklist + data,
        [],
        char_budget=9000,
        original_body=original,
        original_meta=target.draft_meta,
    )
    assert len(first["failures"]) == 1
    target.draft_body = body.replace(
        f"{UNSUPPORTED[:-1]} [source: {CHECKLIST}].", f"{SUPPORTED_2[:-1]} [source: {CHECKLIST}]."
    )
    model = Grounder()
    second = await editorial.review(
        model,
        "GPT-6 Astra",
        target,
        checklist + data,
        [],
        char_budget=9000,
        original_body=original,
        original_meta=target.draft_meta,
    )
    assert model.frames and second["failures"] == []


def test_citation_key_lowercases_only_scheme_and_host():
    assert editorial._citation_key("HTTPS://Developers.OpenAI.com/api/Docs/#x") == editorial._citation_key(
        "https://developers.openai.com/api/Docs"
    )
    assert editorial._citation_key("https://example.test/Guide") != editorial._citation_key(
        "https://example.test/guide"
    )
    assert editorial._citation_key("https://example.test/guide@2026-10-04/") == "https://example.test/guide"


@pytest.mark.asyncio
async def test_section_text_alone_overflowing_names_that_cause_not_a_source():
    big = " ".join(f"Astra fact {i}." for i in range(900))
    body = page(
        [("Specifications", "Astra specifications."), ("Model Selection Context", f"{big} [source: {CHECKLIST}].")]
    )
    checklist, data = astra_records()
    with pytest.raises(editorial.EditorialContextOverflow) as raised:
        await run(Grounder(), body, checklist + data, 9000)
    message = str(raised.value)
    assert "cause the section text and context alone" in message and "source_id" not in message


# --------------------------------------------------------------------------- #
# Scaffold findings, line anchors and canonical text whose form changed.
# --------------------------------------------------------------------------- #


def cited_task(frame):
    return bool(frame.get("review_only_sentences")) and frame.get("evidence_scope", "").startswith("complete records")


class AltScaffold(Grounder):
    """Adds a non-grounding finding only to the per-source task whose evidence is source `on`."""

    def __init__(self, on):
        super().__init__()
        self.on = on

    async def chat_bounded_json(self, system, user, *, char_budget):
        out = await super().chat_bounded_json(system, user, char_budget=char_budget)
        frame = json.loads(user.split("\n", 1)[1])
        if "support_alternative" in frame and frame["evidence_scope"].endswith(" " + self.on):
            out["failures"].append(
                {
                    "severity": "blocker",
                    "check": "task-scaffolding",
                    "section_id": frame["sections"][0]["id"],
                    "anchor": None,
                    "detail": "New unrelated reminder to check the dashboard weekly.",
                }
            )
        return out


SCAFFOLD = ("task-scaffolding", "New unrelated reminder to check the dashboard weekly.")


@pytest.mark.asyncio
async def test_scaffold_on_the_flagging_source_survives_when_another_supports():
    a, b = big_two_source_records()  # SUPPORTED is stated by a only
    body = page(
        [
            ("Specifications", "Astra specifications."),
            ("Model Selection Context", f"{SUPPORTED[:-1]} [source: {CHECKLIST}] [source: {DATA}]."),
        ]
    )
    model = AltScaffold("b")
    result = await run(model, body, a + b, 9000)
    assert len([f for _, f in model.frames if "support_alternative" in f]) == 2
    assert [(f["check"], f["detail"]) for f in result["failures"]] == [SCAFFOLD]


@pytest.mark.asyncio
async def test_a_sentence_supported_by_one_cited_source_is_never_reported():
    a, b = big_two_source_records()
    b = b + [record("b", DATA, ELSEWHERE, "claim-b-support")]  # supported by b only; a flags it
    body = page(
        [
            ("Specifications", "Astra specifications."),
            ("Model Selection Context", f"{ELSEWHERE[:-1]} [source: {CHECKLIST}] [source: {DATA}]."),
        ]
    )
    model = AltScaffold("b")
    result = await run(model, body, a + b, 9000)
    assert len([f for _, f in model.frames if "support_alternative" in f]) == 2
    assert [(f["check"], f["detail"]) for f in result["failures"]] == [SCAFFOLD]


class LineAnchor(Grounder):
    """Flags like Grounder, anchoring each finding on one whole (exact, unique) section line."""

    def __init__(self, line):
        super().__init__()
        self.line = line

    async def chat_bounded_json(self, system, user, *, char_budget):
        out = await super().chat_bounded_json(system, user, char_budget=char_budget)
        for failure in out["failures"]:
            failure["anchor"] = self.line
        return out


@pytest.mark.asyncio
async def test_a_line_anchor_never_transfers_a_canonical_finding_to_a_new_sentence():
    checklist, data = recheck_records()
    canonical = f"{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}]."
    original, body = base_and_draft(
        f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{canonical}\n",
        f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{canonical} {SUPPORTED_2[:-1]} [source: {CHECKLIST}].\n",
    )
    line = f"{canonical} {SUPPORTED_2[:-1]} [source: {CHECKLIST}]."
    assert body.count(line) == 1
    _, result = await review(LineAnchor(line), body, original, checklist + data)
    assert result["failures"] == []  # the new SUPPORTED_2 is judged on its own (supported) finding
    assert result["notes"] == [f"{editorial.RETAINED_NOTE}: '{canonical}'"]


@pytest.mark.asyncio
async def test_a_new_unsupported_sentence_on_a_canonical_line_is_still_reported():
    checklist, data = recheck_records()
    canonical = f"{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}]."
    original, body = base_and_draft(
        f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{canonical}\n",
        f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{canonical} {UNSUPPORTED[:-1]} [source: {CHECKLIST}].\n",
    )
    line = f"{canonical} {UNSUPPORTED[:-1]} [source: {CHECKLIST}]."
    _, result = await review(LineAnchor(line), body, original, checklist + data)
    assert [f["detail"] for f in result["failures"]] == [f"Unsupported: '{UNSUPPORTED}'"]
    # Repair targets only the new sentence, never the canonical one on the same line.
    assert [f["anchor"] for f in result["failures"]] == [f"{UNSUPPORTED[:-1]} [source: {CHECKLIST}]."]
    assert result["notes"] == [f"{editorial.RETAINED_NOTE}: '{canonical}'"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["reflow", "link-citation", "bare-link-citation"])
async def test_canonical_text_whose_form_changed_is_still_canonical(kind):
    checklist, data = recheck_records()
    if kind == "reflow":
        base_line = f"GPT-6 Astra was benchmarked\non internal suites [source: {CHECKLIST}]."
        draft_line = f"GPT-6 Astra was benchmarked on internal suites [source: {CHECKLIST}]."
    elif kind == "link-citation":
        base_line = f"{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}]."
        draft_line = f"{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}] ([model selection guide]({SELECTION}))."
    else:
        base_line = f"{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}]."
        draft_line = f"{BASE_UNVERIFIED[:-1]} [guide]({SELECTION})."  # a citation link: text 'guide'
    original, body = base_and_draft(
        f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{base_line}\n",
        f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{draft_line}\n",
    )
    _, result = await review(Grounder(), body, original, checklist + data)
    assert result["failures"] == []
    assert len(result["notes"]) == 1 and editorial.RETAINED_NOTE in result["notes"][0]


@pytest.mark.asyncio
async def test_required_and_scaffold_findings_survive():
    checklist, data = recheck_records()

    class Required(Grounder):
        async def chat_bounded_json(self, system, user, *, char_budget):
            out = await super().chat_bounded_json(system, user, char_budget=char_budget)
            frame = json.loads(user.split("\n", 1)[1])
            if cited_task(frame):
                out["failures"].append(
                    {
                        "severity": "blocker",
                        "check": SCAFFOLD[0],
                        "section_id": frame["sections"][0]["id"],
                        "anchor": None,
                        "detail": SCAFFOLD[1],
                    }
                )
            return out

    base = f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n"
    original = page([("Specifications", "Astra specifications."), ("Model Selection Context", base)])
    body = original.replace(base, base + f"\n{SUPPORTED_2[:-1]} [source: {CHECKLIST}].\n")
    _, result = await review(Required(), body, original, checklist + data)
    assert [(f["check"], f["detail"]) for f in result["failures"]] == [SCAFFOLD] and "notes" not in result


# --------------------------------------------------------------------------- #
# Wrapped list items, link-text swaps, multi-source support and retargeted findings.
# --------------------------------------------------------------------------- #

THIRD = "https://developers.openai.com/api/docs/guides/third-source"


@pytest.mark.asyncio
async def test_wrapped_list_item_not_first_in_its_list_is_canonical():
    checklist, data = recheck_records()
    base = (
        f"- {SUPPORTED[:-1]} [source: {CHECKLIST}].\n"
        f"- GPT-6 Astra was benchmarked\n  on internal suites [source: {CHECKLIST}].\n"
    )
    draft = (
        f"- {SUPPORTED[:-1]} [source: {CHECKLIST}].\n"
        f"- GPT-6 Astra was benchmarked on internal suites [source: {CHECKLIST}].\n"
    )
    original, body = base_and_draft(base, draft)
    assert "GPT-6 Astra was benchmarked on internal suites" in editorial._base_cores(original)
    _, result = await review(Grounder(), body, original, checklist + data)
    assert result["failures"] == [] and len(result["notes"]) == 1


@pytest.mark.asyncio
async def test_inline_link_text_swap_is_new_text_and_reported():
    checklist, data = recheck_records()
    base = (
        f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n"
        f"GPT-6 Astra traffic is routed through [LiteLLM](https://docs.litellm.ai/docs/proxy) on port 4000 [source: {CHECKLIST}].\n"
    )
    draft = base.replace("[LiteLLM](https://docs.litellm.ai/docs/proxy)", "[Envoy](https://www.envoyproxy.io/docs)")
    original, body = base_and_draft(base, draft)
    assert editorial._core(
        "routed through [LiteLLM](https://docs.litellm.ai/docs/proxy) on port 4000"
    ) != editorial._core("routed through [Envoy](https://www.envoyproxy.io/docs) on port 4000")
    _, result = await review(Grounder(), body, original, checklist + data)
    assert len(result["failures"]) == 1 and "Envoy" in result["failures"][0]["detail"] and "notes" not in result


@pytest.mark.asyncio
async def test_three_sources_only_the_third_supports():
    a, b = big_two_source_records()
    c = [record("c", THIRD, f"Third detail {i}. " + filler(f"c{i}", 6), f"claim-c-{i}") for i in range(5)]
    c += [record("c", THIRD, ELSEWHERE, "claim-c-support")]
    body = page(
        [
            ("Specifications", "Astra specifications."),
            ("Model Selection Context", f"{ELSEWHERE[:-1]} [source: {CHECKLIST}] [source: {DATA}] [source: {THIRD}]."),
        ]
    )
    model = AltScaffold("a")
    result = await run(model, body, a + b + c, 9000)
    assert len([f for _, f in model.frames if "support_alternative" in f]) == 3
    assert [(f["check"], f["detail"]) for f in result["failures"]] == [SCAFFOLD] and "notes" not in result


@pytest.mark.asyncio
async def test_canonical_sentence_every_source_flags_is_retained_and_scaffold_kept():
    a, b = big_two_source_records()
    canonical = f"{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}] [source: {DATA}]."
    original, body = base_and_draft(f"{canonical}\n", f"{canonical}\n\n{SUPPORTED[:-1]} [source: {CHECKLIST}].\n")
    model = AltScaffold("b")
    _, result = await review(model, body, original, a + b)
    assert len([f for _, f in model.frames if "support_alternative" in f]) == 2
    assert [(f["check"], f["detail"]) for f in result["failures"]] == [SCAFFOLD]
    assert result["notes"] == [f"{editorial.RETAINED_NOTE}: '{canonical}'"]


class Disobedient(Grounder):
    """In a per-source task, also flags the canonical 'benchmarked' sentence shown as context (anchor None)."""

    async def chat_bounded_json(self, system, user, *, char_budget):
        out = await super().chat_bounded_json(system, user, char_budget=char_budget)
        frame = json.loads(user.split("\n", 1)[1])
        if "support_alternative" in frame:
            texts = {" ".join(row["text"].split()) for row in frame["evidence"]}
            others = [
                s
                for line in frame["body"].splitlines()
                if not line.startswith("#")
                for s in editorial._sentences(line)
                if s not in frame["review_only_sentences"]
            ]
            out["failures"] += [
                {
                    "severity": "blocker",
                    "check": "grounding",
                    "section_id": frame["sections"][0]["id"],
                    "anchor": None,
                    "detail": f"Unsupported: '{strip_citations(s)}'",
                }
                for s in others
                if not any(t in strip_citations(s) for t in texts) and "benchmarked" in s
            ]
        return out


@pytest.mark.asyncio
async def test_a_finding_about_another_sentence_neither_attaches_nor_counts_as_flagging():
    a, b = big_two_source_records()  # SUPPORTED is stated by a
    canonical = f"{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}]."
    original, body = base_and_draft(
        f"{canonical}\n", f"{canonical} {SUPPORTED[:-1]} [source: {CHECKLIST}] [source: {DATA}].\n"
    )
    model = Disobedient()
    _, result = await review(model, body, original, a + b)
    assert len([f for _, f in model.frames if "support_alternative" in f]) == 2
    assert all(UNSUPPORTED not in f["detail"] and "benchmarked" not in f["detail"] for f in result["failures"])
    assert result["failures"] == []  # SUPPORTED is supported by a; the canonical sentence's finding was ignored


@pytest.mark.asyncio
async def test_a_new_sentence_finding_is_retargeted_off_the_canonical_sentence():
    a, b = big_two_source_records()
    canonical = f"{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}]."
    line = f"{canonical} {UNSUPPORTED[:-1]} [source: {CHECKLIST}] [source: {DATA}]."
    original, body = base_and_draft(f"{canonical}\n", f"{line}\n")
    _, result = await review(LineAnchor(line), body, original, a + b)
    assert [f["detail"] for f in result["failures"]] == [f"Unsupported: '{UNSUPPORTED}'"]
    assert [f["anchor"] for f in result["failures"]] == [f"{UNSUPPORTED[:-1]} [source: {CHECKLIST}] [source: {DATA}]."]
    assert all(canonical not in (f["anchor"] or "") for f in result["failures"])


class VagueLineAnchor(LineAnchor):
    """Anchors on the whole line and says nothing that names a sentence."""

    async def chat_bounded_json(self, system, user, *, char_budget):
        out = await super().chat_bounded_json(system, user, char_budget=char_budget)
        for failure in out["failures"]:
            failure["detail"] = "The pricing claim is not in the evidence."
        return out


@pytest.mark.asyncio
async def test_a_vague_line_anchor_in_a_narrowed_task_attaches_to_its_new_sentence():
    a, b = big_two_source_records()
    canonical = f"{BASE_UNVERIFIED[:-1]} [source: {CHECKLIST}]."
    line = f"{canonical} {UNSUPPORTED[:-1]} [source: {CHECKLIST}] [source: {DATA}]."
    original, body = base_and_draft(f"{canonical}\n", f"{line}\n")
    model = VagueLineAnchor(line)
    _, result = await review(model, body, original, a + b)
    assert len([f for _, f in model.frames if "support_alternative" in f]) == 2  # the new sentence is narrowed
    assert [(f["detail"], f["anchor"]) for f in result["failures"]] == [
        ("The pricing claim is not in the evidence.", f"{UNSUPPORTED[:-1]} [source: {CHECKLIST}] [source: {DATA}].")
    ]
    assert result["notes"] == [f"{editorial.RETAINED_NOTE}: '{canonical}'"]  # the canonical sentence stays retained


def test_only_end_parenthesised_or_source_text_links_are_citations():
    """A domain-text link is ordinary text: swapping it changes the sentence."""
    assert editorial._core("Code is hosted on [github.com](https://github.com/x) today.") != editorial._core(
        "Code is hosted on [gitlab.com](https://gitlab.com/x) today."
    )
    assert editorial._core("Code is hosted on GitHub ([docs](https://github.com/x)).") == editorial._core(
        "Code is hosted on GitHub."
    )
    # A parenthesised link mid-sentence is prose, not a citation.
    assert "Envoy" in editorial._core("Traffic goes through a proxy ([Envoy](https://envoyproxy.io)) on port 4000.")


def test_stacked_trailing_citations_normalise_away():
    """Citations stripped first, then trailing parenthesised links until stable."""
    plain = editorial._core("X.")
    assert editorial._core("X ([a](https://a.test)) ([b](https://b.test)).") == plain
    assert editorial._core("X ([a](https://a.test)) [source: https://s.test].") == plain
    assert editorial._core("X [source: https://s.test] ([a](https://a.test))[^2].") == plain
    assert plain == "X"


def test_live_citation_forms_are_recognised():
    """Forms in job-ce36c8b003 drafts: a dated [source: ...] and an angle-bracket, dated link."""
    dated = "Prices are listed [source: https://openai.com/index/practical-guide-building-gpt-6/ @ 2026-10-04]."
    linked = (
        "Playwright controls browsers [Source](<https://openai.com/index/practical-guide-building-gpt-6/>@2026-10-04)."
    )
    for sentence in (dated, linked):
        assert editorial._cited_urls(sentence, {}) == ["https://openai.com/index/practical-guide-building-gpt-6/"]
    assert editorial._core(dated) == "Prices are listed" and editorial._core(linked) == "Playwright controls browsers"


# --------------------------------------------------------------------------- #
# Uncited sentences: unit boundaries, table rows, empty citations and ambiguous labels.
# --------------------------------------------------------------------------- #


def reviewed_units(model):
    return [unit for _, frame in model.frames for unit in frame.get("review_only_sentences", [])]


@pytest.mark.asyncio
async def test_fences_header_label_and_image_lines_are_never_units_code_content_is_one():
    checklist, data = astra_records()
    text = (
        f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n```bash\npip install openai\n```\n\n"
        "| Model | Context window |\n|---|---|\n| Astra | 400k |\n\n**Notes**\n\n![diagram](https://x.test/a.png)\n"
    )
    body = page([("Specifications", "Astra specifications."), ("Model Selection Context", text)])
    model = Grounder()
    result = await run(model, body, checklist + data, 9000)
    units = reviewed_units(model)
    assert "pip install openai" in units  # the new code block is one unit, grounded against the citation above it
    assert not any(token in unit for unit in units for token in ("```", "| Model |", "|---|", "**Notes**", "diagram"))
    details = [f["detail"] for f in result["failures"]]
    assert "Unsupported: 'pip install openai'" in details
    # The body row follows a code block, so no paragraph citation reaches it: a new row without a citation.
    assert "New fact without a citation: '| Astra | 400k |'. Cite the source that states it, or remove it." in details
    assert not any("```" in detail for detail in details)  # fences are never findings


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["row", "header", "caption", "paragraph"])
async def test_table_body_rows_inherit_their_tables_citation(where):
    checklist, data = astra_records()
    cite = f" [source: {CHECKLIST}]"
    before = {"caption": f"**Context windows{cite}**\n", "paragraph": f"The models compare as follows{cite}.\n"}.get(
        where, ""
    )
    table = (
        f"| Model | Context window{cite if where == 'header' else ''} |\n|---|---|\n"
        f"| Astra | 400k{cite if where == 'row' else ''} |\n| Luna | 128k{cite if where == 'row' else ''} |\n"
    )
    text = f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{before}{table}"
    body = page([("Specifications", "Astra specifications."), ("Model Selection Context", text)])
    model = Grounder()
    result = await run(model, body, checklist + data, 9000)
    row_frames = [
        frame for _, frame in model.frames if any(u.startswith("| ") for u in frame.get("review_only_sentences", []))
    ]
    rows = {u for frame in row_frames for u in frame["review_only_sentences"] if u.startswith("| ")}
    assert len(rows) == 2  # both body rows grounded, against the inherited citation's complete records
    assert all(frame["evidence_scope"] == "complete records of the cited sources checklist" for frame in row_frames)
    assert all("without a citation" not in f["detail"] for f in result["failures"])


@pytest.mark.asyncio
async def test_uncited_rows_without_letters_are_flagged_when_new_and_retained_when_canonical():
    checklist, data = astra_records()
    table = "```\ncode\n```\n\n| Context | Output |\n|---|---|\n| 32k | 128k |\n"
    base_text = f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{table}"
    original, body = base_and_draft(base_text, base_text + "| 2026 | 5.00 |\n")
    _, result = await review(Grounder(), body, original, checklist + data)
    assert [f["detail"] for f in result["failures"]] == [
        "New fact without a citation: '| 2026 | 5.00 |'. Cite the source that states it, or remove it."
    ]
    assert result["notes"] == [f"{editorial.RETAINED_NOTE}: '| 32k | 128k |'"]


@pytest.mark.asyncio
async def test_a_canonical_sentence_rewrapped_by_the_draft_is_retained_whole():
    checklist, data = astra_records()
    canonical = "Astra keeps its benchmark notes in the appendix for every release."
    base_text = f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{canonical}\n"
    draft_text = (
        f"{SUPPORTED[:-1]} [source: {CHECKLIST}].\n\n{canonical.replace('notes in', 'notes' + chr(10) + 'in')}\n\n"
        f"{SUPPORTED_2[:-1]} [source: {CHECKLIST}].\n"
    )
    original, body = base_and_draft(base_text, draft_text)
    _, result = await review(Grounder(), body, original, checklist + data)
    assert result["failures"] == []
    assert result["notes"] == [f"{editorial.RETAINED_NOTE}: '{canonical}'"]


@pytest.mark.asyncio
async def test_empty_citations_are_no_citation_and_unknown_labels_are_grounded():
    checklist, data = astra_records()
    text = (
        f"{UNSUPPORTED[:-1]} [source: Totally made up manual].\n\n"
        "Astra costs nothing at all [source: ].\n\nAstra runs on the moon [source:   @ 2026-10-04].\n"
    )
    body = page([("Specifications", "Astra specifications."), ("Model Selection Context", text)])
    model = Grounder()
    result = await run(model, body, checklist + data, 9000)
    assert editorial._cited_urls("Astra costs nothing at all [source: ].", {}) == []
    assert editorial._cited_urls("Astra runs on the moon [source:   @ 2026-10-04].", {}) == []
    details = [f["detail"] for f in result["failures"]]
    assert f"Unsupported: '{UNSUPPORTED}'" in details  # the unknown label was grounded (no admitted records)
    assert (
        sum(d.startswith("New fact without a citation") for d in details) == 2
    )  # the empty citations, deterministically
    assert ["Astra costs nothing at all [source: ]."] not in [
        frame.get("review_only_sentences") for _, frame in model.frames
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fits_union", [True, False])
async def test_an_ambiguous_citation_label_uses_the_union_or_is_flagged_never_an_overflow(fits_union):
    a, b = big_two_source_records()
    # Both sources carry the label 'Guide'; a label citation matches both.
    for r in a + b:
        r.label = "Guide"
    if fits_union:
        a, b = a[:2], b[:2]
    # A large relevant source the sentence does not cite forces split mode.
    noise = [
        record(
            "notes",
            "https://example.test/notes",
            f"Deep analysis note {i}. " + " ".join(f"zeta{i}q{w}" for w in range(60)),
            f"claim-notes-{i}",
        )
        for i in range(8)
    ]
    for r in noise:
        r.label = "Notes"
    body = page(
        [("Specifications", "Astra specifications."), ("Model Selection Context", f"{SUPPORTED[:-1]} [source: Guide].")]
    )
    model = Grounder()
    result = await run(model, body, a + b + noise, 9000)
    if fits_union:
        frame = next(frame for _, frame in model.frames if frame.get("review_only_sentences"))
        assert frame["evidence_scope"] == "complete records of the cited sources a, b"
        assert result["failures"] == []
    else:
        assert not any(frame.get("review_only_sentences") for _, frame in model.frames)
        assert [f["detail"] for f in result["failures"]] == [
            "Citation label is ambiguous: '[source: Guide]' matches sources a, b; cite the URL of the source that states it."
        ]


# --------------------------------------------------------------------------- #
# Uncited sentences: wrapped canonical blocks, code blocks and nearest-citation grounding.
# --------------------------------------------------------------------------- #

C = f" [source: {CHECKLIST}]"
CANON = "Astra keeps its benchmark notes in the appendix for every release."
HEAD = f"{SUPPORTED[:-1]}{C}.\n\n"


async def draft_only(text, records=None):
    checklist, data = astra_records()
    body = page([("Specifications", "Astra specifications."), ("Model Selection Context", text)])
    model = Grounder()
    result = await run(model, body, records if records is not None else checklist + data, 9000)
    return model, result


async def base_draft(base_text, draft_text):
    checklist, data = astra_records()
    original, body = base_and_draft(base_text, draft_text)
    model = Grounder()
    _, result = await review(model, body, original, checklist + data)
    return model, result


@pytest.mark.asyncio
async def test_list_items_wraps_bold_labels_and_blockquotes_are_retained_whole():
    for base_text, draft_text in [
        (
            HEAD + f"- {CANON}\n- Other item here.\n",
            HEAD + f"- {CANON.replace('notes in', 'notes' + chr(10) + '  in')}\n- Other item here.\n",
        ),
        (HEAD + f"**Notes**\n{CANON}\n", HEAD + f"**Notes**\n{CANON.replace('notes in', 'notes' + chr(10) + 'in')}\n"),
        (HEAD + f"> {CANON}\n", HEAD + f"> {CANON.replace('notes in', 'notes' + chr(10) + '> in')}\n"),
    ]:
        _, result = await base_draft(base_text, draft_text)
        assert result["failures"] == [] and f"{editorial.RETAINED_NOTE}: '{CANON}'" in result["notes"]


@pytest.mark.asyncio
async def test_a_new_sentence_in_a_wrapped_canonical_paragraph_is_flagged_alone():
    wrapped = CANON.replace("its ", "its\n").replace("for ", "for\n")
    _, result = await base_draft(HEAD + f"{CANON}\n", HEAD + f"{wrapped} Astra ships a brand new turbo mode.\n")
    assert [(f["detail"], f["anchor"]) for f in result["failures"]] == [
        (
            "New fact without a citation: 'Astra ships a brand new turbo mode.'. Cite the source that states it, or remove it.",
            "Astra ships a brand new turbo mode.",
        )
    ]
    assert result["notes"] == [f"{editorial.RETAINED_NOTE}: '{CANON}'"]


@pytest.mark.asyncio
async def test_a_new_code_block_with_a_fabricated_comment_is_grounded_and_flagged():
    code = '# GPT-6 Astra is free for every organization and has a 10M context window.\nclient.responses.create(model="gpt-6-astra")'
    model, result = await draft_only(HEAD + f"```python\n{code}\n```\n")
    frame = next(frame for _, frame in model.frames if code in frame.get("review_only_sentences", []))
    assert frame["evidence_scope"] == "complete records of the cited sources checklist"  # the nearest citation above
    assert [f["detail"] for f in result["failures"]] == [f"Unsupported: '{' '.join(code.split())}'"]


@pytest.mark.asyncio
async def test_tilde_and_list_nested_four_space_fences_are_code_units_never_findings():
    text = (
        HEAD
        + "~~~\n# Astra runs on the moon.\n~~~\n\n- Install it:\n\n    ```bash\n    # Astra costs nothing at all.\n"
        "    pip install openai\n    ```\n"
    )
    model, result = await draft_only(text)
    units = reviewed_units(model)
    assert "# Astra runs on the moon." in units
    assert any("pip install openai" in unit and "```" not in unit for unit in units)
    assert not any("```" in f["detail"] or "~~~" in f["detail"] for f in result["failures"])
    assert not any(f["detail"].startswith("New fact without a citation") for f in result["failures"])


@pytest.mark.asyncio
async def test_canonical_code_is_retained_and_new_code_is_grounded_on_the_nearest_citation():
    code_old = "pip install openai"
    base_text = f"Install the SDK.\n\n```bash\n{code_old}\n```\n"
    original, body = base_and_draft(
        base_text,
        base_text.replace("Install the SDK.", HEAD + "Install the SDK.") + "\n```bash\nexport ASTRA_TIER=free\n```\n",
    )
    checklist, data = astra_records()
    model = Grounder()
    _, result = await review(model, body, original, checklist + data)
    units = reviewed_units(model)
    assert code_old not in units  # canonical code, matched whole: never grounded
    assert "export ASTRA_TIER=free" in units  # new code: grounded against the nearest citation above it
    assert not any(code_old in f["detail"] for f in result["failures"])


@pytest.mark.asyncio
async def test_new_code_without_any_inherited_citation_is_flagged():
    model, result = await draft_only("```bash\nexport ASTRA_TIER=free\n```\n\n" + HEAD)
    assert "export ASTRA_TIER=free" not in reviewed_units(model)
    assert "New code without a citation: 'export ASTRA_TIER=free'. Cite the source it comes from, or remove it." in [
        f["detail"] for f in result["failures"]
    ]


@pytest.mark.asyncio
async def test_pipeless_tables_underscore_labels_and_empty_rows():
    model, result = await draft_only(HEAD + "Model | Window\n--- | ---\nAstra | 400k\n|  |  |\n\n**max_tokens**\n")
    units = reviewed_units(model)
    assert "Astra | 400k" in units
    assert not any(unit.startswith("Model | Window") or "---" in unit or "max_tokens" in unit for unit in units)
    assert not any("max_tokens" in f["detail"] or "'|  |  |'" in f["detail"] for f in result["failures"])


@pytest.mark.asyncio
async def test_an_ambiguous_label_combined_with_a_url_grounds_the_overflowing_source_in_chunks():
    a, b = big_two_source_records()
    for r in a + b:
        r.label = "Guide"
    a, b = a[:2], b[:2]
    noise = [
        record(
            "notes",
            "https://example.test/notes",
            f"Deep analysis note {i}. " + " ".join(f"zeta{i}q{w}" for w in range(60)),
            f"claim-notes-{i}",
        )
        for i in range(8)
    ]
    for r in noise:
        r.label = "Notes"
    # The URL-cited source that overflows is grounded in chunks:
    # no overflow, and the label is not misreported as ambiguous.
    model, result = await draft_only(
        f"{SUPPORTED[:-1]} [source: Guide] [source: https://example.test/notes].\n", a + b + noise
    )
    chunks = [frame for _, frame in model.frames if "evidence_chunk" in frame]
    assert chunks and {frame["evidence_chunk"]["source_id"] for frame in chunks} == {"notes"}
    assert not any("Citation label is ambiguous" in f["detail"] for f in result["failures"])


# --------------------------------------------------------------------------- #
# Uncited sentences: fences in blockquotes, inline backticks, separators and bold labels.
# --------------------------------------------------------------------------- #

FAB = "# GPT-6 Astra is free for every organization and has a 10M context window."


@pytest.mark.asyncio
async def test_a_fence_inside_a_blockquote_is_code_and_never_a_finding():
    model, result = await draft_only(HEAD + "> Run:\n>\n> ```bash\n> pip install openai\n> ```\n")
    assert "pip install openai" in reviewed_units(model)
    assert not any("```" in f["detail"] for f in result["failures"])
    assert not any(f["detail"].startswith("New fact without a citation") for f in result["failures"])


@pytest.mark.asyncio
async def test_an_inline_triple_backtick_span_is_prose_not_a_fence():
    model, result = await draft_only(
        HEAD + "```pip``` installs Astra for free.\n\nAstra ships a brand new turbo mode.\n"
    )
    details = [f["detail"] for f in result["failures"]]
    assert (
        "New fact without a citation: '```pip``` installs Astra for free.'. Cite the source that states it, or remove it."
        in details
    )
    assert (
        "New fact without a citation: 'Astra ships a brand new turbo mode.'. Cite the source that states it, or remove it."
        in details
    )
    assert not any(unit.startswith("installs") or "turbo" in unit for unit in reviewed_units(model))


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["-|-", ":-|-:", "|-|-|"])
async def test_single_dash_gfm_separators_are_separators(separator):
    header = "Model | Window" if not separator.startswith("|") else "| Model | Window |"
    row = "Astra | 400k" if not separator.startswith("|") else "| Astra | 400k |"
    model, result = await draft_only(HEAD + f"{header}\n{separator}\n{row}\n")
    units = reviewed_units(model)
    assert row in units and not any("Model" in unit or "-|" in unit for unit in units)
    assert not any("Window" in f["detail"] for f in result["failures"])


@pytest.mark.asyncio
async def test_bold_lines_are_labels_only_without_a_sentence_end_and_under_five_words():
    model, result = await draft_only(
        HEAD + "**`max_tokens`**:\n\n__max_output_tokens__\n\n- **reasoning.effort**\n\n**a*b**\n\n"
        "**Astra is free for *every* organization.**\n\n**Astra supports five new regional deployments**\n"
    )
    details = [f["detail"] for f in result["failures"]]
    assert not any(
        label in detail
        for detail in details
        for label in ("max_tokens", "max_output_tokens", "reasoning.effort", "a*b")
    )
    assert (
        "New fact without a citation: '**Astra is free for *every* organization.**'. Cite the source that states it, or remove it."
        in details
    )
    assert any("five new regional deployments" in detail for detail in details)  # 5+ words: prose


@pytest.mark.asyncio
async def test_new_code_above_any_citation_is_flagged_and_a_changed_canonical_block_is_grounded():
    model, result = await draft_only(f"```python\n{FAB}\n```\n\n" + HEAD)
    assert f"New code without a citation: '{FAB}'. Cite the source it comes from, or remove it." in [
        f["detail"] for f in result["failures"]
    ]
    base = HEAD + "```bash\npip install openai\n```\n"
    model, result = await base_draft(base, HEAD + f"```bash\n{FAB}\npip install openai\n```\n")
    assert any(FAB in unit for unit in reviewed_units(model))  # no longer canonical: grounded, and flagged
    assert any(FAB[2:30] in f["detail"] for f in result["failures"])
