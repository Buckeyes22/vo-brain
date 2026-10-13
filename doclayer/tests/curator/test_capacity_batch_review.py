"""capacity-batch review (job-cb8bf0deac): pages whose assigned claims exceed
one review together are drafted and reviewed in batches.

- Grounding splits one source's complete records into in-order chunks of
  complete records; a sentence is supported if ANY chunk supports it, and only
  one record that cannot fit beside the sentence fails (named).
- The single-claim precheck measures the smallest real review frame, not the
  conservative whole-page planner stand-in.
"""

import json
import re

import pytest

from doclayer.curator import capacity, curate, editorial
from doclayer.curator.editorial import REVIEW_PACKING_SHARE, EditorialContextOverflow
from doclayer.curator.state import Evidence, PageChange

BUDGET = 32768
FILLER = "reasoning effort tokens latency budget model "
URL = "https://example.com/manual"
SMALL = "# P\n\n## Performance\n\nOld text.\n"
LONG = (
    SMALL
    + "\n## Streaming\n\n"
    + "Streaming events arrive in order. " * 280
    + "\n"
    + "".join(f"\n## Section {k}\n\nSome text about section {k}.\n" for k in range(60))
)


def rec(n, text_len=900, quote_len=388, source="manual"):
    text = (f"Fact {n}: " + FILLER * 2000)[:text_len]
    quote = (f"Source sentence {n}: " + "x" * 40000)[:quote_len]
    return Evidence(
        source, "Performance modes", f"# Fact {n}", "quote", text, url=URL, quote=quote, support_status="supported"
    )


def page(records, extra=""):
    ids = [curate.evidence_id(r) for r in records]
    change = PageChange("g", "wiki/p.md", "update", title="P", source_ids=["manual"], claim_ids=ids)
    change.draft_body = (
        "# P\n\n## Performance\n\n"
        + "".join(f"Fact {i} is stated here [source: {URL}].\n\n" for i in range(len(records)))
        + extra
    )
    change.draft_meta = {}
    return change


def plan(change, records, mode, original_body):
    """Run the review planner up to its first model call (no model is called)."""
    try:
        capacity._drive_without_suspension(
            editorial.review(
                capacity._ReviewTripwire(),
                "intent",
                change,
                records,
                [],
                char_budget=BUDGET,
                original_body=original_body,
                original_meta={},
                mode=mode,
                context={"source_policy": {}, "source_binding": None},
            )
        )
        return "returned"
    except capacity._ReviewReached:
        return "fits"
    except EditorialContextOverflow as exc:
        return "OVERFLOW: " + str(exc)


# --------------------------------------------------------------------------- #
# Critical: one source's records in chunks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["grounding", "required", "preservation"])
@pytest.mark.parametrize("count", [11, 19, 20, 30, 40])
def test_single_source_pages_of_many_claims_plan_every_review(count, mode):
    records = [rec(i) for i in range(count)]
    assert plan(page(records), records, mode, SMALL) == "fits"


class ChunkModel:
    """Supports a sentence 'Fact N ...' only from a chunk holding record 'Fact N:'."""

    def __init__(self):
        self.frames = []

    async def chat_json(self, system, user):
        assert len(system) + len(user) <= BUDGET * REVIEW_PACKING_SHARE
        frame = json.loads(user.split("\n", 1)[1])
        self.frames.append(frame)
        failures = []
        for sentence in frame.get("review_only_sentences", []):
            number = re.match(r"Fact (\d+)", sentence)
            shown = {re.match(r"Fact (\d+):", row["text"]).group(1) for row in frame["evidence"]}
            if number and number.group(1) not in shown:
                failures.append(
                    {
                        "severity": "blocker",
                        "check": "article-grounding",
                        "detail": f"Unsupported by the shown records: {sentence}",
                        "section_id": frame["target_section_ids"][0],
                        "anchor": sentence,
                    }
                )
        return {"complete": True, "failures": failures, "conflicts": []}


@pytest.mark.asyncio
async def test_a_sentence_is_supported_if_any_chunk_of_its_source_supports_it():
    records = [rec(i) for i in range(30)]
    canonical = f"Fact 998 is stated here [source: {URL}]."
    change = page(records, extra=f"Fact 999 is stated here [source: {URL}].\n\n{canonical}\n")
    model = ChunkModel()
    result = await editorial.review(
        model,
        "intent",
        change,
        records,
        [],
        char_budget=BUDGET,
        original_body=SMALL + "\n" + canonical + "\n",
        original_meta={},
        mode="grounding",
        context={"source_policy": {}, "source_binding": None},
    )
    chunked = [f for f in model.frames if "evidence_chunk" in f]
    assert chunked and len({f["evidence_chunk"]["first_record"] for f in chunked}) > 1
    # Every chunk holds complete records, in order, and together they cover the source.
    first = [f for f in chunked if f["review_only_sentences"] == ["Fact 0 is stated here [source: " + URL + "]."]]
    assert sum(len(f["evidence"]) for f in first) == 30
    assert [f["evidence_chunk"]["first_record"] for f in first] == sorted(
        f["evidence_chunk"]["first_record"] for f in first
    )
    # Facts 0-29 are each supported by one chunk only: none is reported.
    reported = [f["anchor"] or f["detail"] for f in result["failures"]]
    assert not any(re.search(r"Fact (\d|[12]\d) is", text) for text in reported)
    # No chunk supports Fact 999 (new): reported. Fact 998 is canonical: retained.
    assert any("Fact 999" in text for text in reported)
    assert not any("Fact 998" in text for text in reported)
    assert any("Fact 998" in note for note in result.get("notes", []))


def test_only_a_single_record_too_large_beside_the_sentence_fails_and_is_named():
    records = [rec(i) for i in range(20)]
    records[7] = rec(7, text_len=40000)
    message = plan(page(records), records, "grounding", SMALL)
    assert message.startswith("OVERFLOW") and curate.evidence_id(records[7]) in message
    assert "this one record of the source" in message


# --------------------------------------------------------------------------- #
# Important: the single-claim precheck uses the smallest real frame
# --------------------------------------------------------------------------- #


def single(text_len, quote_len=388):
    record = rec(0, text_len=text_len, quote_len=quote_len)
    change = PageChange(
        "g", "wiki/p.md", "update", title="P", source_ids=["manual"], claim_ids=[curate.evidence_id(record)]
    )
    return change, record


def boundary(cap, original):
    low, high = 100, 40000
    while high - low > 1:
        middle = (low + high) // 2
        change, record = single(middle)
        low, high = (middle, high) if cap.claim_fits(change, original, record) else (low, middle)
    return low


def test_the_single_claim_boundary_includes_the_review_frame():
    cap = capacity.ReviewCapacity(BUDGET, "intent", {})
    largest = boundary(cap, ({}, LONG))
    change, record = single(largest)
    cap.require_assigned(change, ({}, LONG), [record])  # just under: accepted
    change, record = single(largest + 1)
    # It fits the budget by itself, but not with the review frame: refused and named.
    assert len(record.text) + len(record.quote) <= BUDGET
    with pytest.raises(EditorialContextOverflow) as caught:
        cap.require_assigned(change, ({}, LONG), [record])
    assert curate.evidence_id(record) in str(caught.value)
    assert f"its claim text ({largest + 1} chars) and source excerpt (388 chars) together" in str(caught.value)


@pytest.mark.parametrize(
    "text_len, quote_len, wording",
    [
        (30000, 388, "its claim text (30000 chars) alone"),
        (900, 34000, "its source excerpt (34000 chars) alone"),
    ],
)
def test_the_error_names_the_part_that_overflows(text_len, quote_len, wording):
    cap = capacity.ReviewCapacity(BUDGET, "intent", {})
    change, record = single(text_len, quote_len=quote_len)
    with pytest.raises(EditorialContextOverflow) as caught:
        cap.require_assigned(change, ({}, SMALL), [record])
    assert wording in str(caught.value) and curate.evidence_id(record) in str(caught.value)


@pytest.mark.parametrize("length", [6000, 9000, 12000])
def test_claims_that_real_reviews_accept_pass_the_precheck(length):
    cap = capacity.ReviewCapacity(BUDGET, "intent", {})
    change, record = single(length)
    cap.require_assigned(change, ({}, LONG), [record])  # the old grow stand-in refused these
    change.draft_body = LONG + f"\n## New\n\n{record.text} [source: {URL}].\n"
    for mode in ("grounding", "required", "preservation"):
        assert plan(change, [record], mode, LONG) == "fits", mode


# --------------------------------------------------------------------------- #
# A chunk supports a sentence only if its reply has no finding it failed to
# locate (a finding that names the wrong sentence does not count as support).
# --------------------------------------------------------------------------- #


class Faithful:
    """Supports a sentence only if EVERY 'Fact N' it names is in the shown records."""

    def __init__(self):
        self.frames = []

    async def chat_json(self, system, user):
        frame = json.loads(user.split("\n", 1)[1])
        self.frames.append(frame)
        shown = {m.group(1) for row in frame.get("evidence", []) for m in [re.match(r"Fact (\d+):", row["text"])] if m}
        failures = []
        for sentence in frame.get("review_only_sentences", []):
            needed = set(re.findall(r"Fact (\d+)", sentence))
            if needed and not needed <= shown:
                failures.append(
                    {
                        "severity": "blocker",
                        "check": "article-grounding",
                        "detail": f"Unsupported: {sentence}",
                        "section_id": frame["target_section_ids"][0],
                        "anchor": sentence,
                    }
                )
        return {"complete": True, "failures": failures, "conflicts": []}


class Misanchor(Faithful):
    """In the first chunk, the flag names ANOTHER sentence of the section."""

    async def chat_json(self, system, user):
        out = await super().chat_json(system, user)
        frame = self.frames[-1]
        if "evidence_chunk" in frame and frame["evidence_chunk"]["first_record"] == 1:
            for failure in out["failures"]:
                failure["anchor"] = f"Fact 7 is stated here [source: {URL}]."
                failure["detail"] = "Unsupported claim."
        return out


async def chunked_review(model, extra, original=SMALL):
    records = [rec(i) for i in range(30)]
    change = page(records, extra=extra)
    return await editorial.review(
        model,
        "intent",
        change,
        records,
        [],
        char_budget=BUDGET,
        original_body=original,
        original_meta={},
        mode="grounding",
        context={"source_policy": {}, "source_binding": None},
    )


COMBO = f"Fact 1 and Fact 28 together hold [source: {URL}]."  # records in different chunks


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [Faithful, Misanchor])
async def test_a_chunk_with_a_finding_it_cannot_attach_supports_nothing(model):
    result = await chunked_review(model(), COMBO + "\n")
    reported = [f["anchor"] or f["detail"] for f in result["failures"]]
    assert any("Fact 1 and Fact 28" in text for text in reported)  # no chunk alone supports it
    assert not any(re.match(r"Fact 7 is stated", text or "") for text in reported)  # never misattributed


@pytest.mark.asyncio
async def test_a_misanchored_flag_on_a_canonical_sentence_is_still_retained():
    result = await chunked_review(Misanchor(), COMBO + "\n", original=SMALL + "\n" + COMBO + "\n")
    assert not any("Fact 1 and Fact 28" in (f["anchor"] or f["detail"]) for f in result["failures"])
    assert any("Fact 1 and Fact 28" in note for note in result.get("notes", []))


@pytest.mark.asyncio
async def test_a_sentence_one_clean_chunk_supports_stays_supported():
    same_chunk = f"Fact 2 and Fact 3 together hold [source: {URL}]."
    result = await chunked_review(Misanchor(), same_chunk + "\n")
    assert not any("Fact 2 and Fact 3" in (f["anchor"] or f["detail"]) for f in result["failures"])


def test_chunk_prompts_never_claim_complete_records():
    records = [rec(i) for i in range(30)]
    seen = []

    class Recorder:
        async def chat_json(self, system, user):
            seen.append((system, json.loads(user.split("\n", 1)[1])))
            return {"complete": True, "failures": [], "conflicts": []}

    import asyncio

    asyncio.run(
        editorial.review(
            Recorder(),
            "intent",
            page(records),
            records,
            [],
            char_budget=BUDGET,
            original_body=SMALL,
            original_meta={},
            mode="grounding",
            context={"source_policy": {}, "source_binding": None},
        )
    )
    chunks = [(system, frame) for system, frame in seen if "evidence_chunk" in frame]
    assert chunks
    for system, frame in chunks:
        assert "COMPLETE" not in system and "complete records" not in system
        assert "complete" not in frame["evidence_scope"]
        assert system.endswith(editorial.SOURCE_CHUNK_GUIDANCE)


# --------------------------------------------------------------------------- #
# The single-claim check uses THIS page's outline and routing
# frame; a claim that passes it also passes drafting routing on that page.
# --------------------------------------------------------------------------- #


class Editor:
    """Routes like the editor fakes; integrates each claim as one brief sentence."""

    def __init__(self):
        import sys

        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
        from test_bounded_editing import routing_response

        self.routing_response = routing_response

    async def chat_json(self, system, user):
        import ast

        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            return self.routing_response(user)
        ids = ast.literal_eval(re.search(r"Claim IDs to integrate or prove already present: (.+)", user)[1])
        lines = [f"Brief statement of {identity} [source: {URL}]." for identity in ids]
        return {
            "section_id": re.search(r"Section ID: (.+)", user)[1],
            "edits": [],
            "append": "\n".join(lines),
            "handled_claim_ids": ids,
            "notes": [],
            "claim_dispositions": [{"id": i, "status": "integrated", "anchor": line} for i, line in zip(ids, lines)],
        }


async def draft(record, original_body):
    change = PageChange(
        "g", "wiki/p.md", "update", title="P", source_ids=["manual"], claim_ids=[curate.evidence_id(record)]
    )
    change.draft_meta = {}
    return await curate.draft_page(
        Editor(), "intent", change, [record], original_body, [], char_budget=BUDGET, today="2026-10-06"
    )


@pytest.mark.asyncio
async def test_on_the_63_section_page_the_precheck_matches_drafting():
    from doclayer.curator.editing import page_units
    from doclayer.curator.model import ModelError

    assert len(page_units(LONG)) == 63
    cap = capacity.ReviewCapacity(BUDGET, "intent", {})
    largest = boundary(cap, ({}, LONG))
    assert largest < boundary(capacity.ReviewCapacity(BUDGET, "intent", {}), ({}, SMALL))  # page-specific
    for length in (24000, 25000, 27000):  # passed the page-independent check, then failed routing
        change, record = single(length)
        with pytest.raises(EditorialContextOverflow):
            cap.require_assigned(change, ({}, LONG), [record])
    change, record = single(largest)
    cap.require_assigned(change, ({}, LONG), [record])
    result = await draft(record, LONG)  # passes routing and editing on that page
    assert f"Brief statement of {curate.evidence_id(record)}" in result["body"]
    change, record = single(largest + 1)
    with pytest.raises(EditorialContextOverflow):
        cap.require_assigned(change, ({}, LONG), [record])
    with pytest.raises(ModelError, match="cannot fit bounded"):
        await draft(record, LONG)


def test_claim_fits_cache_is_keyed_by_page_title():
    cap = capacity.ReviewCapacity(BUDGET, "intent", {})
    _, record = single(900)
    cap.claim_fits(PageChange("g", "wiki/p.md", "update", title="P"), ({}, SMALL), record)
    cap.claim_fits(PageChange("g", "wiki/p.md", "update", title="Another title"), ({}, SMALL), record)
    titles = {key[2] for key in cap.cache if key[0] == "claim"}
    assert titles == {"P", "Another title"}
