"""Accepted claim anchors survive every draft_progress rewrite.

Publication reconciliation attributes a dropped draft section to a claim only
through its accepted anchor, so each rewrite of the draft carries the anchors
forward: unchanged when their text stands; rebound only to text that is the
anchor (the rewrite's transform of it, or the one place the new page holds it
up to whitespace and Markdown markers); otherwise kept unchanged (stale, which
only over-reports a gap). An anchor never moves to different text.
"""

import hashlib

import pytest
from test_bounded_editing import response, routing_response, supported
from test_scope_repair_restores import page_job

from doclayer import frontmatter
from doclayer.curator import curate, editing, reconciliation
from doclayer.curator.editing import EDITOR_VERSION, rebind_claim_anchors
from doclayer.curator.pipeline import Pipeline, finalize_draft
from doclayer.curator.state import Evidence, PageChange

BASE = (
    "# Probe Page\n\nIntro words of this page.\n\n## Notes\n\nGeneral notes about the model family.\n\n"
    "## End\n\nFinal words of the page here.\n"
)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_rebind_keeps_standing_anchors_and_follows_rewritten_text():
    old = "Alpha line one.\r\nBeta line two.\r\n"
    new = old.replace("\r\n", "\n")
    anchors = {
        "kept": "Alpha line one.",
        "moved": "Alpha line one.\r\nBeta line two.",
        "unknown": "Not in either body.",
    }
    assert rebind_claim_anchors(anchors, old, new) == {
        "kept": "Alpha line one.",
        "moved": "Alpha line one.\nBeta line two.",
        "unknown": "Not in either body.",
    }


@pytest.mark.asyncio
async def test_anchors_survive_crlf_setext_normalisation(cfg, store, checkout, monkeypatch):
    draft = BASE.replace("\n", "\r\n")
    anchors = {
        "claim-a": "General notes about the model family.",
        "claim-b": "## End\r\n\r\nFinal words of the page here.",
    }
    state = page_job(cfg, store, checkout, monkeypatch, BASE, draft, progress={"claim_anchors": anchors})
    await Pipeline(cfg, store)._stage_validate(state)
    change = store.load(state.job_id).changes[0]
    assert "normalised CRLF line endings to LF" in change.notes
    assert change.draft_progress["claim_anchors"] == {
        "claim-a": "General notes about the model family.",
        "claim-b": "## End\n\nFinal words of the page here.",
    }


@pytest.mark.asyncio
async def test_anchors_survive_the_deterministic_structure_restore(cfg, store, checkout, monkeypatch):
    draft = BASE.replace("# Probe Page", "# Other Title").replace(
        "## Notes\n\nGeneral notes about the model family.\n\n", ""
    )
    anchors = {"claim-a": "Final words of the page here.", "claim-b": "# Other Title\n\nIntro words of this page."}
    state = page_job(cfg, store, checkout, monkeypatch, BASE, draft, progress={"claim_anchors": anchors})
    await Pipeline(cfg, store)._stage_validate(state)
    change = store.load(state.job_id).changes[0]
    assert any("restored dropped base section" in note for note in change.notes)
    assert change.draft_body.startswith("# Probe Page\n")
    assert change.draft_progress["claim_anchors"] == {
        "claim-a": "Final words of the page here.",  # moved by the restore, text unchanged
        "claim-b": "# Other Title\n\nIntro words of this page.",
    }  # its H1 text changed: kept, stale


@pytest.mark.asyncio
async def test_anchors_survive_a_stale_repair_context_reset(cfg, store, checkout, monkeypatch):
    anchors = {"claim-a": "General notes about the model family."}
    state = page_job(cfg, store, checkout, monkeypatch, BASE, BASE, progress={"claim_anchors": anchors})
    state.changes[0].repair_context = {"binding_hash": "stale", "body_hash": "stale", "failures": []}
    store.save(state)
    await Pipeline(cfg, store)._stage_validate(state)
    assert store.load(state.job_id).changes[0].draft_progress["claim_anchors"] == anchors


def test_anchors_follow_citation_hydration():
    raw = "# Page\nFact one about the device [src-0123456789].\n"
    change = PageChange("g", "wiki/page.md", "update")
    finalize_draft(
        change,
        {
            "body": raw,
            "draft_progress": {
                "body_hash": sha(raw),
                "claim_anchors": {"claim-a": "Fact one about the device [src-0123456789]."},
            },
        },
        [Evidence("src-0123456789", "manual", "#Manual", "quote", "Fact one", url="https://ex.test/a")],
    )
    assert change.draft_body == "# Page\nFact one about the device [Source](<https://ex.test/a>).\n"
    assert change.draft_progress["claim_anchors"] == {
        "claim-a": "Fact one about the device [Source](<https://ex.test/a>)."
    }


@pytest.mark.asyncio
async def test_anchors_follow_the_draft_onto_the_reconciled_body():
    base = "# Subject\n\n## A\nOriginal a.\n\n## B\nOriginal b.\n"
    fresh = base.replace("Original b.", "Operator b.")
    draft = base.replace("Original a.", "Drafted fact about subject a.")
    change = PageChange(
        "g",
        "wiki/subject.md",
        "update",
        title="Subject",
        draft_body=draft,
        draft_progress={"claim_anchors": {"claim-a": "Drafted fact about subject a."}},
    )

    class Never:
        async def chat_bounded_json(self, *args, **kwargs):
            raise AssertionError("a non-overlapping merge needs no model")

    meta = {"title": "Subject", "concept_id": "c-operator", "privacy": "internal"}
    result = await reconciliation.reconcile_stale(
        Never(),
        "topic",
        change,
        frontmatter.build(meta, fresh),
        [],
        base_page=frontmatter.build(meta, base),
        char_budget=7000,
    )
    assert "Operator b." in result["body"] and "Drafted fact about subject a." in result["body"]
    assert result["draft_progress"]["claim_anchors"] == {"claim-a": "Drafted fact about subject a."}


@pytest.mark.asyncio
async def test_editor_rebinds_an_existing_anchor_its_edit_rewrote():
    body = "# Device\nOperator fact about the device.\n"
    record = supported("New qualified device fact")
    new = "Operator  fact about the device. New qualified device fact [Source](https://device.example/manual)"

    class Client:
        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            return response(user, edits=[{"old": "Operator fact about the device.", "new": new}])

    change = PageChange(
        "g",
        "wiki/device.md",
        "update",
        draft_progress={
            "editor_version": EDITOR_VERSION,
            "body_hash": sha(body),
            "claim_anchors": {"claim-old": "Operator fact about the device."},
        },
    )
    result = await editing.edit_page(
        Client(), "Device", change, [record], body, [], char_budget=16000, today="2026-10-02"
    )
    assert result["body"] == "# Device\n" + new + "\n"
    assert result["draft_progress"]["claim_anchors"] == {
        "claim-old": "Operator  fact about the device.",
        curate.evidence_id(record): new,
    }


def _long_page(lines=1000):
    """About 75 KB of distinct paragraphs, each citing a source placeholder."""
    return "# Probe Page\n\n" + "".join(
        f"Paragraph {i} states a distinct qualified fact number {i} [src-0123456789].\n" for i in range(lines)
    )


@pytest.mark.parametrize("rewrite", ["crlf", "hydration", "crlf-aligned", "restore", "one-line"])
def test_rebinding_a_75kb_page_is_bounded(rewrite):
    import time

    from doclayer.curator.pipeline import hydrate_citations

    lf = _long_page()
    assert len(lf.encode()) >= 75_000
    evidence = [Evidence("src-0123456789", "manual", "#Manual", "quote", "fact", url="https://ex.test/a")]
    anchor_line = "Paragraph 500 states a distinct qualified fact number 500"
    if rewrite.startswith("crlf"):  # crlf-aligned: no transform, found up to whitespace
        old = lf.replace("\n", "\r\n")
        transform = (lambda a: a.replace("\r\n", "\n")) if rewrite == "crlf" else None
        new, anchor = lf, anchor_line + " [src-0123456789].\r\nParagraph 501"
        expected = anchor.replace("\r\n", "\n")
    elif rewrite == "hydration":
        old, transform = lf, (lambda a: hydrate_citations(a, evidence))
        new, anchor = hydrate_citations(lf, evidence), anchor_line + " [src-0123456789]."
        expected = anchor_line + " [Source](<https://ex.test/a>)."
    elif rewrite == "restore":  # a restored title and section: the changed anchor stays unchanged
        old, transform = lf.replace("# Probe Page", "# Other Title"), None
        new = lf + "\n## Restored\n\nRestored base section text.\n"
        anchor = expected = "# Other Title\n\nParagraph 0 states"
    else:  # an 11.9 KB single-line paragraph rewritten around a moved, re-emphasised anchor
        old = "# P\n\n" + " ".join(f"Sentence {i} states fact {i * 3}." for i in range(400)) + "\n"
        new = (
            "# P\n\nNew opening sentence. "
            + " ".join(f"Sentence {i} states fact {i * 3}." for i in range(400)).replace(
                "Sentence 200 states fact 600.", "Sentence 200 states **fact 600**."
            )
            + "\n"
        )
        assert len(old) >= 11_900 and "\n" not in old[5:-1]
        transform, anchor = None, "Sentence 200 states fact 600."
        expected = "Sentence 200 states **fact 600**."
    started = time.perf_counter()
    carried = rebind_claim_anchors({"claim-a": anchor}, old, new, transform)
    elapsed = time.perf_counter() - started
    assert carried == {"claim-a": expected}
    assert elapsed < 0.5, f"{rewrite} rebinding took {elapsed:.3f}s"


# --------------------------------------------------------------------------- #
# Large edits that move or reword an anchor: 11 adversarial
# cases plus a near-duplicate. A reworded anchor is never rebound to
# another claim's (or any differing) text; it stays unchanged.
# --------------------------------------------------------------------------- #


def _para(i):
    return f"Item {i} records a distinct measured fact {i * 7} about component {i}.\n"


def _reworded(i):
    return f"Item {i} now records a distinct measured fact {i * 7} about component {i} in detail.\n"


HEAD, TAIL = "# Page\n\n", "\n## End\n\nClosing words stay.\n"
OLD = HEAD + "".join(_para(i) for i in range(60)) + TAIL
INSERTED = "".join(f"Inserted line {k} with wholly new wording.\n" for k in range(40))
ANCHOR = "Item {0} records a distinct measured fact {1} about component {0}."
TWO_LINES = ANCHOR.format(20, 140) + "\n" + ANCHOR.format(21, 147)
MERGED = "".join(
    f"Item {2 * i} records a distinct measured fact {2 * i * 7} about component {2 * i}. "
    f"Item {2 * i + 1} records a distinct measured fact {(2 * i + 1) * 7} about component {2 * i + 1}!\n"
    for i in range(30)
)
ONE_LINE_OLD = "# P\n\n" + " ".join(f"Sentence {i} states fact {i * 3}." for i in range(400)) + "\n"
ONE_LINE_NEW = "# P\n\n" + " ".join(f"Sentence {i} now states the fact {i * 3} clearly." for i in range(400)) + "\n"
ADVERSARIAL = {
    "same-count reword": (ANCHOR.format(30, 210), OLD, HEAD + "".join(_reworded(i) for i in range(60)) + TAIL),
    "insert-40-before idx=5": (
        ANCHOR.format(5, 35),
        OLD,
        HEAD + INSERTED + "".join(_reworded(i) for i in range(60)) + TAIL,
    ),
    "insert-40-before idx=30": (
        ANCHOR.format(30, 210),
        OLD,
        HEAD + INSERTED + "".join(_reworded(i) for i in range(60)) + TAIL,
    ),
    "insert-40-before idx=55": (
        ANCHOR.format(55, 385),
        OLD,
        HEAD + INSERTED + "".join(_reworded(i) for i in range(60)) + TAIL,
    ),
    "delete-40-before idx=42": (
        ANCHOR.format(42, 294),
        OLD,
        HEAD + "".join(_reworded(i) for i in range(40, 60)) + TAIL,
    ),
    "delete-40-before idx=55": (
        ANCHOR.format(55, 385),
        OLD,
        HEAD + "".join(_reworded(i) for i in range(40, 60)) + TAIL,
    ),
    "reversed+reworded": (
        ANCHOR.format(30, 210),
        OLD,
        HEAD + "".join(_reworded(i) for i in reversed(range(60))) + TAIL,
    ),
    "one-line paragraph": ("Sentence 200 states fact 600. Sentence 201 states fact 603.", ONE_LINE_OLD, ONE_LINE_NEW),
    "multiline anchor same-count": (TWO_LINES, OLD, HEAD + "".join(_reworded(i) for i in range(60)) + TAIL),
    "multiline anchor insert-before": (
        TWO_LINES,
        OLD,
        HEAD + INSERTED + "".join(_reworded(i) for i in range(60)) + TAIL,
    ),
    "lines merged": (ANCHOR.format(41, 287), OLD, HEAD + MERGED + TAIL),
    "near-duplicate of the anchor elsewhere": (
        ANCHOR.format(30, 210),
        OLD,
        HEAD + "".join(_reworded(i) for i in range(60)) + ANCHOR.format(30, 210)[:-1] + "!\n" + TAIL,
    ),
}


@pytest.mark.parametrize("case", sorted(ADVERSARIAL))
def test_a_reworded_anchor_is_kept_never_moved_to_other_text(case):
    import time

    anchor, old, new = ADVERSARIAL[case]
    assert old.count(anchor) == 1 and anchor not in new
    started = time.perf_counter()
    carried = rebind_claim_anchors({"claim-a": anchor}, old, new)
    assert carried == {"claim-a": anchor}  # stale and unchanged: fails safe
    assert time.perf_counter() - started < 0.5


def test_an_anchor_held_twice_up_to_whitespace_is_not_rebound():
    old = "# P\nAlpha fact stated once here.\n"
    new = "# P\nAlpha  fact stated once here.\nAlpha fact\nstated once here.\n"
    assert rebind_claim_anchors({"c": "Alpha fact stated once here."}, old, new) == {
        "c": "Alpha fact stated once here."
    }


@pytest.mark.parametrize(
    "old, new, anchor",
    [
        # `#` inside a word is not a heading marker: C# never matches C.
        ("# P\nBuilt with C# tooling here.\n", "# P\nBuilt with C tooling here.\n", "Built with C# tooling here."),
        # An unpaired `*` is arithmetic, not emphasis: 2*3 never matches 23.
        ("# P\nThe product 2*3 equals six.\n", "# P\nThe product 23 equals six.\n", "The product 2*3 equals six"),
        # A match must end on a word boundary: "is 5" never matches inside "is 55".
        ("# P\nThe limit is 5\n", "# P\nThe limit  is 55 units.\n", "The limit is 5"),
    ],
)
def test_normalised_search_keeps_literal_symbols_and_word_boundaries(old, new, anchor):
    assert rebind_claim_anchors({"c": anchor}, old, new) == {"c": anchor}


def test_markdown_position_markers_are_still_ignored():
    old = "# P\nKeep the cache warm for speed.\n"
    new = "## P\nKeep the **cache** warm for _speed_.\n"
    assert rebind_claim_anchors({"c": "Keep the cache warm for speed."}, old, new) == {
        "c": "Keep the **cache** warm for _speed_."
    }
    heading = rebind_claim_anchors(
        {"h": "# Plan\nKeep the cache"}, "# Plan\nKeep the cache warm.\n", "## Plan\n\nKeep the cache cold.\n"
    )
    assert heading == {"h": "Plan\n\nKeep the cache"}  # a heading's leading hashes are markup
