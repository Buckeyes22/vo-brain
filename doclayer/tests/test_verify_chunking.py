"""Oversized evidence is judged in chunks; every unusable result names its cause."""

import asyncio
import json

import httpx
import pytest
from test_verification_contract import positive, published  # noqa: F401

from doclayer import verification
from doclayer.curator.config import CuratorConfig
from doclayer.curator.model import ModelClient
from doclayer.curator.state import Usage
from doclayer.gap_budget import ResourceBudget

PATH = "wiki/fleet-fast-storage-observations.md"


class Budget:
    def consume_model(self, **kwargs):
        pass


class FrameClient:
    """Answers when a frame carries the sentinel; records every frame it sees."""

    def __init__(self, cfg, reply=None, sentinel="ANSWER-need-is-stored-here-today"):
        self.cfg = cfg
        self._before_call = None
        self.max_calls = 50
        self.sentinel = sentinel
        self.reply = reply
        self.frames = []

        class U:
            model_calls = 0

        self.usage = U()

    async def chat_json(self, system, user, **kwargs):
        assert len(system) + len(user) <= kwargs["prompt_char_budget"]
        frame = json.loads(user)
        self.frames.append(frame)
        if self.reply is not None:
            return self.reply
        page = next((p for p in frame["pages"] if self.sentinel in p["body"]), None)
        row = dict(
            facet="need",
            answered=page is not None,
            scope_preserved=True,
            evidence=[dict(path=page["path"], quote=self.sentinel)] if page else [],
        )
        return dict(answers=page is not None, facets=[row], reason="chunk")


def big_page(cap, *, sections=10, sentinel_section=7):
    """A page several times the frame cap, in headed sections of about cap/3 chars."""
    parts = ["# Fleet fast storage\n\nIntro line.\n\n"]
    for i in range(sections):
        paragraphs = "\n\n".join(f"Paragraph {j} of section {i}. " + "filler " * 200 for j in range(cap // 3 // 1450))
        extra = "\n\nANSWER-need-is-stored-here-today\n" if i == sentinel_section else ""
        parts.append(f"## Section {i}\n\n{paragraphs}{extra}\n\n")
    return dict(path=PATH, body="".join(parts), page_metadata={}, authority={})


def run(pages, client, **kwargs):
    causes = []
    out = asyncio.run(
        verification._semantic_evidence(
            "q", ["need"], [], pages, model_client=client, budget=Budget(), causes=causes, **kwargs
        )
    )
    return out, causes


def test_oversized_page_is_chunked_at_headings_and_resolves():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    page = big_page(cap)
    assert len(page["body"]) > 2 * cap
    client = FrameClient(cfg)
    out, causes = run([page], client)
    assert out is not None and out["answers"] is True and causes == []
    assert len(client.frames) >= 2
    sent = [p for frame in client.frames for p in frame["pages"]]
    assert all(p["path"] == PATH and p["heading_trail"] and p["chunk"] for p in sent)
    holder = next(p for p in sent if "ANSWER-need" in p["body"])
    assert "## Section 7" in holder["body"] and " through " in holder["heading_trail"]
    assert out["facets"][0]["evidence"][0]["path"] == PATH


def test_chunks_cover_the_page_exactly_in_order():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    page = big_page(cap)
    groups = verification._fitting_groups([page], lambda group: len(json.dumps(group, default=str)), cap)
    chunks = [c for g in groups for c in g]
    assert "".join(c["body"] for c in chunks) == page["body"]
    assert all(len(json.dumps(g, default=str)) <= cap for g in groups)


def test_one_section_larger_than_the_frame_splits_at_paragraphs():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    paragraphs = "\n\n".join("Paragraph %d. %s" % (i, "word " * 400) for i in range(int(cap * 2.5 / 2000)))
    page = dict(
        path=PATH,
        body="# Only\n\n## One section\n\n" + paragraphs + "\n\nANSWER-need-is-stored-here-today\n",
        page_metadata={},
        authority={},
    )
    assert len(page["body"]) > 2 * cap
    client = FrameClient(cfg)
    out, causes = run([page], client)
    assert out["answers"] is True
    sent = [p for frame in client.frames for p in frame["pages"]]
    assert len(sent) >= 3 and all(p["heading_trail"].startswith("Only") for p in sent)
    assert "".join(p["body"] for p in sent) == page["body"]


def test_quote_from_a_chunk_validates_against_the_full_page_text():
    full = "# A\n\nalpha alpha alpha.\n\n## B\n\nThe scratch volume is /mnt/fast-scratch today.\n\n## C\n\nOther text entirely.\n"
    chunks = [
        dict(path=PATH, body="# A\n\nalpha alpha alpha.\n\n", page_metadata={}, heading_trail="A", chunk="1 of 3"),
        dict(
            path=PATH,
            body="## B\n\nThe scratch volume is /mnt/fast-scratch today.\n\n",
            page_metadata={},
            heading_trail="A > B",
            chunk="2 of 3",
        ),
        dict(path=PATH, body="## C\n\nOther text entirely.\n", page_metadata={}, heading_trail="A > C", chunk="3 of 3"),
    ]

    def reply(quote):
        return dict(
            answers=True,
            reason="r",
            facets=[dict(facet="need", answered=True, scope_preserved=True, evidence=[dict(path=PATH, quote=quote)])],
        )

    ok = verification._validate_semantic(
        reply("The scratch volume is /mnt/fast-scratch today."), ["need"], [], chunks[:2], {PATH: full}
    )
    assert ok is not None and ok["answers"] is True
    # a quote that is in the full page but in a chunk the model was not shown is not accepted
    why = []
    gone = verification._validate_semantic(
        reply("Other text entirely."), ["need"], [], chunks[:2], {PATH: full}, why=why
    )
    assert gone is None and why == ["answered facet cites no literal source quote"]
    # a quote the full page does not contain is never accepted, chunked or not
    assert (
        verification._validate_semantic(
            reply("The scratch volume is /mnt/invented today."), ["need"], [], chunks, {PATH: full}
        )
        is None
    )


def test_chunk_frames_carry_path_and_heading_trail():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    client = FrameClient(cfg)
    run([big_page(cap)], client)
    first = client.frames[0]["pages"][0]
    assert (
        first["path"] == PATH
        and first["heading_trail"].startswith("Fleet fast storage")
        and first["chunk"].startswith("1 of ")
    )


def test_a_single_paragraph_that_cannot_fit_names_the_page_and_sizes():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    page = dict(path=PATH, body="# H\n\n" + "x" * (cap + 500), page_metadata={}, authority={})
    client = FrameClient(cfg)
    out, causes = run([page], client)
    assert out is None and client.frames == []
    assert len(causes) == 1 and causes[0].startswith(f"evidence exceeds bound: {PATH} ")
    size, limit = causes[0].rsplit(" ", 3)[-3], causes[0].rsplit(" ", 1)[-1]
    assert int(size) > int(limit) == cap


def test_malformed_reply_note_is_distinct_from_the_bound_note():
    cfg = CuratorConfig()
    page = dict(path=PATH, body="# H\n\nsmall page\n", page_metadata={}, authority={})
    out, causes = run([page], FrameClient(cfg, reply={"answers": True}))
    assert out is None and causes == ["malformed reply: reply is not the verdict object"]


def stray(reply):
    """answers=False plus a citation that is not on the page: more than the flag is wrong,
    so the flag cannot be derived from the rows and the reply still triggers the re-ask."""
    row = reply["facets"][0]
    extra = dict(path=row["evidence"][0]["path"], quote="a quote that is not on the page at all")
    return dict(reply, answers=False, facets=[dict(row, evidence=row["evidence"] + [extra])])


def test_contradictory_reply_gets_one_correction_then_resolves():
    cfg = CuratorConfig()
    page = dict(path=PATH, body="# H\n\nANSWER-need-is-stored-here-today\n", page_metadata={}, authority={})
    seen = []
    good = positive_for("ANSWER-need-is-stored-here-today")
    contradiction = stray(good)

    class Client(FrameClient):
        async def chat_json(self, system, user, **kwargs):
            seen.append(system)
            return contradiction if len(seen) == 1 else good

    out, causes = run([page], Client(cfg))
    assert out["answers"] is True and causes == [] and len(seen) == 2
    assert "Re-judge" in seen[1] and "Re-judge" not in seen[0]


def test_persistent_contradiction_reports_its_cause_after_one_correction():
    cfg = CuratorConfig()
    page = dict(path=PATH, body="# H\n\nANSWER-need-is-stored-here-today\n", page_metadata={}, authority={})
    contradiction = stray(positive_for("ANSWER-need-is-stored-here-today"))
    client = FrameClient(cfg, reply=contradiction)
    out, causes = run([page], client)
    assert out is None and len(client.frames) == 2
    assert causes == ["malformed reply: " + verification._CONTRADICTION]


# -- verify_gap: the notes the caller records, always a retry ---------------------------------- #


def sequence_client(tmp_path, replies, *, status=200):
    cfg = CuratorConfig()
    cfg.credentials_file = str(tmp_path / "no-credentials")
    cfg.inference.retries = 0
    queue = list(replies)

    def respond(request):
        if status != 200:
            return httpx.Response(status, json={"error": "down"})
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(body)}, "finish_reason": "stop"}]}
        )

    usage = Usage()
    return ModelClient(cfg, usage, transport=httpx.MockTransport(respond)), usage


def contradiction_for(path):
    return stray(positive(path))


def test_delivered_judgement_resolves_after_a_correction(published, tmp_path):
    root, path, commit, ledger, record, reply_, index = published
    model, usage = sequence_client(tmp_path, [positive(path), contradiction_for(path), positive(path)])
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply_, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "resolved" and usage.model_calls == 3


def test_delivered_judgement_blocker_is_named_and_stays_a_retry(published, tmp_path):
    root, path, commit, ledger, record, reply_, index = published
    model, usage = sequence_client(tmp_path, [positive(path), contradiction_for(path)])
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply_, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "verifying"
    assert result.notes == ["delivered evidence judgement pending: malformed reply: " + verification._CONTRADICTION]
    assert ledger.load(record.gap_id).status == "verifying"


def test_model_unavailable_note_is_distinct_and_stays_a_retry(published, tmp_path):
    root, path, commit, ledger, record, reply_, index = published
    model, usage = sequence_client(tmp_path, [positive(path)], status=503)
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply_, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "verifying"
    assert len(result.notes) == 1 and result.notes[0].startswith("verification pending: model unavailable: ")


def test_a_missing_quantity_downgrades_only_that_row_and_never_rejects_the_reply():
    page = [dict(path=PATH, body="# H\n\nThe scratch volume is on the fast disk today.\n", page_metadata={})]
    rep = dict(
        answers=True,
        reason="r",
        facets=[
            dict(
                facet="storage",
                answered=True,
                scope_preserved=True,
                evidence=[dict(path=PATH, quote="The scratch volume is on the fast disk today.")],
            )
        ],
    )
    why = []
    out = verification._validate_semantic(rep, ["storage"], [], page, question="how much storage is there?", why=why)
    assert out is not None and why == [] and out["answers"] is False
    assert out["facets"][0]["answered"] is False and out["facets"][0]["evidence"] == []


# -- oversize units, fences, chunk scope and corrections -------------------------------------------------------- #


def test_a_unit_that_cannot_fit_alone_raises_instead_of_sending_an_oversize_group(monkeypatch):
    page = dict(path=PATH, body="# H\n\n" + "y" * 700, page_metadata={})
    big = dict(page, body="z" * 900, heading_trail="H", chunk="1 of 1")
    monkeypatch.setattr(verification, "_chunk_page", lambda page, fits, size: [big])
    with pytest.raises(verification._EvidenceTooLarge) as caught:
        verification._fitting_groups([page], lambda group: sum(len(p["body"]) for p in group), 600)
    assert caught.value.path == PATH and caught.value.frame_chars == 900


def test_paragraph_split_never_cuts_a_fenced_block_and_fences_close_only_themselves():
    text = "Intro\n\n```\nline one\n\nline two\n~~~\n\nstill code\n```\n\nafter\n\n~~~\nx\n\n```\ny\n~~~\n\nend\n"
    paragraphs = verification._paragraphs(text)
    assert "".join(paragraphs) == text
    assert paragraphs == [
        "Intro\n\n",
        "```\nline one\n\nline two\n~~~\n\nstill code\n```\n\n",
        "after\n\n",
        "~~~\nx\n\n```\ny\n~~~\n\n",
        "end\n",
    ]


def test_headings_inside_a_fence_are_not_sections():
    body = "# A\n\n```\n# not a heading\n```\n\n## B\n\ntext\n"
    assert [trail for trail, _ in verification._heading_pieces(body)] == ["A", "A > B"]
    mixed = "# A\n\n~~~\n```\n# still code\n~~~\n\n## B\n"
    assert [trail for trail, _ in verification._heading_pieces(mixed)] == ["A", "A > B"]


def paragraph_page(cap, lead="Measured 2026-09-01 on SVC1:"):
    """One section whose lead paragraph and table caption sit before later chunks."""
    paragraphs = [lead + " the checks below.\n\n"]
    for i in range(int(cap * 2.2 / 1450)):
        paragraphs.append(f"Paragraph {i}. " + "filler " * 200 + "\n\n")
    return dict(path=PATH, body="# Host\n\n## Disks\n\n" + "".join(paragraphs), page_metadata={}, authority={})


def test_chunks_after_the_first_carry_the_section_lead_as_context():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    page = paragraph_page(cap)
    groups = verification._fitting_groups([page], lambda g: len(json.dumps(g, default=str)), cap)
    chunks = [c for g in groups for c in g]
    assert len(chunks) >= 2 and "context" not in chunks[0]
    assert all("Measured 2026-09-01 on SVC1" in c["context"] for c in chunks[1:])
    assert all(len(json.dumps(g, default=str)) <= cap for g in groups)
    assert "".join(c["body"] for c in chunks) == page["body"]


def test_table_caption_travels_with_a_table_chunk():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    filler = "".join(f"Paragraph {i}. " + "filler " * 200 + "\n\n" for i in range(int(cap * 1.2 / 1450)))
    table = "| Mount | Free (bytes) |\n|---|---|\n| /data | 42 |\n\n"
    page = dict(
        path=PATH,
        body="# H\n\nLead line.\n\n" + filler + "Measured on SVC1 2026-09-01:\n\n" + table + filler,
        page_metadata={},
        authority={},
    )
    groups = verification._fitting_groups([page], lambda g: len(json.dumps(g, default=str)), cap)
    holder = next(c for g in groups for c in g if "| /data | 42 |" in c["body"] and not c["body"].startswith("# H"))
    assert "Measured on SVC1 2026-09-01:" in holder["body"] or "Measured on SVC1 2026-09-01:" in holder.get(
        "context", ""
    )


def test_system_prompt_states_that_an_entry_may_be_a_chunk_only_when_chunked():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    systems = []

    class Spy(FrameClient):
        async def chat_json(self, system, user, **kwargs):
            systems.append(system)
            return await super().chat_json(system, user, **kwargs)

    run([big_page(cap)], Spy(cfg))
    assert systems and all("heading_trail locates it" in s for s in systems)
    systems.clear()
    run([dict(path=PATH, body="# H\n\nANSWER-need-is-stored-here-today\n", page_metadata={})], Spy(cfg))
    assert systems and all("heading_trail locates it" not in s for s in systems)


def scope_chunk(context="", trail="Disks", body="The scratch volume is /mnt/fast-scratch today.\n"):
    page = dict(path=PATH, body=body, page_metadata={}, heading_trail=trail, chunk="2 of 3")
    if context:
        page["context"] = context
    return page


def positive_for(quote):
    return dict(
        answers=True,
        reason="r",
        facets=[dict(facet="need", answered=True, scope_preserved=True, evidence=[dict(path=PATH, quote=quote)])],
    )


SCOPES = [dict(dimension="platform", value="SVC1")]
QUOTE = "The scratch volume is /mnt/fast-scratch today."


def test_a_chunk_whose_text_does_not_establish_the_scope_does_not_answer_the_facet():
    chunk = scope_chunk()
    out = verification._validate_semantic(positive_for(QUOTE), ["need"], SCOPES, [chunk], {PATH: chunk["body"]})
    assert out["answers"] is False
    assert out["facets"][0]["answered"] is False and out["facets"][0]["evidence"] == []


FULL = {
    "context": "# Fleet\n\n## Disks\n\nMeasured on SVC1:\n\nThe scratch volume is /mnt/fast-scratch today.\n",
    "trail": "# Fleet\n\n## Disks on SVC1\n\nThe scratch volume is /mnt/fast-scratch today.\n",
    "quote": "# Fleet\n\n## Disks\n\nOn SVC1 the scratch volume is /mnt/fast-scratch today.\n",
}


@pytest.mark.parametrize("where", ["context", "trail", "quote"])
def test_a_chunk_establishes_the_scope_through_the_holding_sections_context_trail_or_quote(where):
    full = FULL[where]
    quote = "On SVC1 the scratch volume is /mnt/fast-scratch today." if where == "quote" else QUOTE
    chunk = scope_chunk(
        body=full.split("## Disks", 1)[1].split("\n\n", 1)[1] if where != "quote" else full.split("\n\n", 2)[2],
        trail="Fleet through Fleet > Elsewhere",
    )
    out = verification._validate_semantic(positive_for(quote), ["need"], SCOPES, [chunk], {PATH: full})
    assert out["answers"] is True


def test_the_chunks_own_context_and_merged_trail_do_not_establish_scope():
    full = "# Fleet\n\n## Elsewhere\n\nThe scratch volume is /mnt/fast-scratch today.\n"
    chunk = scope_chunk(
        body="The scratch volume is /mnt/fast-scratch today.\n",
        trail="Fleet > SVC1 through Fleet > Elsewhere",
        context="Measured on SVC1:",
    )
    out = verification._validate_semantic(positive_for(QUOTE), ["need"], SCOPES, [chunk], {PATH: full})
    assert out["answers"] is False


def test_a_host_heading_that_does_not_govern_the_quote_does_not_satisfy_a_merged_chunk():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)

    def fill(name):
        return "".join(f"{name} {i}. " + "filler " * 200 + "\n\n" for i in range(int(cap * 0.4 / 1450)))

    body = (
        "## SVC1\n\n"
        + fill("a")
        + "## worker1\n\nworker1 has 512 GB free on its data disk.\n\n"
        + fill("b")
        + "## prod\n\n"
        + fill("c")
    )
    page = dict(path=PATH, body=body, page_metadata={}, authority={})
    chunks = [
        c for g in verification._fitting_groups([page], lambda g: len(json.dumps(g, default=str)), cap) for c in g
    ]
    holder = next(c for c in chunks if "worker1 has 512 GB" in c["body"])
    assert "SVC1" in holder["heading_trail"] and " through " in holder["heading_trail"]
    quote = "worker1 has 512 GB free on its data disk."
    out = verification._validate_semantic(positive_for(quote), ["need"], SCOPES, [holder], {PATH: body})
    assert out["answers"] is False
    ok = verification._validate_semantic(
        positive_for(quote), ["need"], [dict(dimension="platform", value="worker1")], [holder], {PATH: body}
    )
    assert ok["answers"] is True


def test_boundary_window_starts_with_the_lead_context_and_trail_of_its_section():
    full = (
        "# Fleet\n\n## Disks\n\nMeasured on SVC1:\n\nPara one.\n\nPara two ends here.\n\n"
        "Para three begins.\n\n## Next\n\nSection text.\n\n"
    )
    cut = full.index("Para three")
    left = verification._Chunk(
        path=PATH, body=full[:cut], page_metadata={}, heading_trail="Fleet through Fleet > Disks", chunk="1 of 2"
    )
    right = verification._Chunk(
        path=PATH, body=full[cut:], page_metadata={}, heading_trail="Fleet > Disks through Fleet > Next", chunk="2 of 2"
    )
    right.start = cut
    window = verification._boundary_window(left, right, lambda g: sum(len(p["body"]) for p in g) <= 45, full)
    assert window["body"] == "Para two ends here.\n\nPara three begins.\n\n"
    assert window["context"] == "Measured on SVC1:"
    assert window["heading_trail"].startswith("Fleet > Disks")
    assert window.start == full.index("Para two")


def test_a_whole_page_is_not_subject_to_the_chunk_scope_check():
    page = dict(path=PATH, body="The scratch volume is /mnt/fast-scratch today.\n", page_metadata={})
    out = verification._validate_semantic(positive_for(QUOTE), ["need"], SCOPES, [page], {PATH: page["body"]})
    assert out["answers"] is True


def test_correction_reply_with_an_invented_quote_is_still_rejected():
    cfg = CuratorConfig()
    page = dict(path=PATH, body="# H\n\nANSWER-need-is-stored-here-today\n", page_metadata={}, authority={})
    first = stray(positive_for("ANSWER-need-is-stored-here-today"))
    invented = positive_for("ANSWER-need-is-invented-elsewhere")

    class Client(FrameClient):
        calls = 0

        async def chat_json(self, system, user, **kwargs):
            Client.calls += 1
            return first if Client.calls == 1 else invented

    out, causes = run([page], Client(cfg))
    assert out is None and causes == ["malformed reply: answered facet cites no literal source quote"]


def test_correction_frame_repeats_the_reason_and_a_consistent_corrected_reply_is_final_even_negative():
    cfg = CuratorConfig()
    page = dict(path=PATH, body="# H\n\nANSWER-need-is-stored-here-today\n", page_metadata={}, authority={})
    first = stray(positive_for("ANSWER-need-is-stored-here-today"))
    first.update(reason="Unsure the host matches.")
    flipped = dict(first, answers=False, facets=[dict(first["facets"][0], answered=False, evidence=[])])
    frames = []

    class Client(FrameClient):
        async def chat_json(self, system, user, **kwargs):
            frames.append(json.loads(user))
            return first if len(frames) == 1 else flipped

    out, causes = run([page], Client(cfg))
    assert "previous_reply" not in frames[0]
    assert frames[1]["previous_reply"] == dict(reason="Unsure the host matches.", scope_preserved={"need": True})
    # the corrected reply changed the row and is consistent and valid: final, not a retry
    assert out is not None and out["answers"] is False and out["facets"][0]["answered"] is False
    assert causes == [] and len(frames) == 2


def test_a_corrected_reply_that_is_itself_invalid_or_contradictory_is_a_retry():
    cfg = CuratorConfig()
    page = dict(path=PATH, body="# H\n\nANSWER-need-is-stored-here-today\n", page_metadata={}, authority={})
    first = stray(positive_for("ANSWER-need-is-stored-here-today"))

    class Invalid(FrameClient):
        n = 0

        async def chat_json(self, system, user, **kwargs):
            Invalid.n += 1
            return first if Invalid.n == 1 else {"answers": True}

    out, causes = run([page], Invalid(cfg))
    assert out is None and causes == ["malformed reply: reply is not the verdict object"]


def test_the_scope_check_reads_the_quote_occurrence_inside_the_shown_chunk():
    sentence = "The scratch volume is /mnt/fast-scratch today."
    full = f"## SVC1\n\n{sentence}\n\n## prod\n\nfiller\n\n{sentence}\n"
    prod_start = full.rindex(sentence)
    shown = verification._Chunk(
        path=PATH, body=full[prod_start - len("filler\n\n") :], page_metadata={}, heading_trail="prod", chunk="2 of 2"
    )
    shown.start = prod_start - len("filler\n\n")
    # only the prod copy was shown: the SVC1 copy on the page must not satisfy scope SVC1
    out = verification._validate_semantic(positive_for(sentence), ["need"], SCOPES, [shown], {PATH: full})
    assert out["answers"] is False
    assert verification._locate(full, sentence, shown.start, shown.start + len(shown["body"])) == prod_start
    assert verification._locate(full, sentence) == full.index(sentence)
    svc1 = verification._Chunk(
        path=PATH, body=full[: full.index("## prod")], page_metadata={}, heading_trail="SVC1", chunk="1 of 2"
    )
    ok = verification._validate_semantic(positive_for(sentence), ["need"], SCOPES, [svc1], {PATH: full})
    assert ok["answers"] is True


def split_page(cap):
    """Two heading sections that never share a chunk, with one fact straddling their cut."""
    first = "".join(f"Paragraph {i}. " + "filler " * 200 + "\n\n" for i in range(int(cap * 0.6 / 1450)))
    second = "".join(f"Other {i}. " + "filler " * 200 + "\n\n" for i in range(int(cap * 0.6 / 1450)))
    body = (
        "# Disks\n\n## First\n\n"
        + first
        + "The ledger lives under\n\n"
        + "## Second\n\n/data/ledger-fast-scratch holds it today.\n\n"
        + second
    )
    return dict(path=PATH, body=body, page_metadata={}, authority={})


class StraddleClient(FrameClient):
    """Answers only when one entry holds both halves of the straddling fact."""

    async def chat_json(self, system, user, **kwargs):
        frame = json.loads(user)
        self.frames.append(frame)
        wanted = frame["facets"]
        joined = any(
            "The ledger lives under" in p["body"] and "/data/ledger-fast-scratch holds it today." in p["body"]
            for p in frame["pages"]
        )
        rows = [
            dict(
                facet=f,
                answered=joined,
                scope_preserved=True,
                evidence=[dict(path=PATH, quote="/data/ledger-fast-scratch holds it today.")] if joined else [],
            )
            for f in wanted
        ]
        return dict(answers=joined, facets=rows, reason="straddle")


def test_a_fact_straddling_a_chunk_cut_is_found_before_any_negative_is_final():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    page = split_page(cap)
    chunks = [
        c for g in verification._fitting_groups([page], lambda g: len(json.dumps(g, default=str)), cap) for c in g
    ]
    assert not any("The ledger lives under" in c["body"] and "holds it today" in c["body"] for c in chunks)
    client = StraddleClient(cfg)
    out, causes = run([page], client)
    assert out is not None and out["answers"] is True and causes == []
    window = client.frames[-1]["pages"][0]
    assert window["chunk"].startswith("boundary of ") and "The ledger lives under" in window["body"]
    assert len(json.dumps(client.frames[-1])) <= cap


def test_a_chunked_negative_is_final_only_after_every_boundary_was_judged():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    page = big_page(cap, sentinel_section=99)  # the answer is nowhere
    client = FrameClient(cfg)
    out, causes = run([page], client)
    assert out is not None and out["answers"] is False and causes == []
    boundaries = [f for f in client.frames if f["pages"][0]["chunk"].startswith("boundary of ")]
    groups = [f for f in client.frames if not f["pages"][0]["chunk"].startswith("boundary of ")]
    assert len(groups) >= 2 and len(boundaries) == len(groups) - 1
    assert all(len(json.dumps(f)) <= cap for f in client.frames)


def test_an_unusable_boundary_reply_keeps_a_negative_a_retry():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    page = big_page(cap, sentinel_section=99)

    class Client(FrameClient):
        async def chat_json(self, system, user, **kwargs):
            frame = json.loads(user)
            if frame["pages"][0]["chunk"].startswith("boundary of "):
                return {"answers": True}
            return await super().chat_json(system, user, **kwargs)

    out, causes = run([page], Client(cfg))
    assert out is None and causes == ["malformed reply: reply is not the verdict object"]


# -- over-cap units, fence matching and chunk notes ------------------------------------------------ #


def test_an_over_cap_unit_raises_even_after_a_non_empty_group(monkeypatch):
    page = dict(path=PATH, body="# H\n\n" + "y" * 700, page_metadata={})
    small = dict(page, body="z" * 100, heading_trail="H", chunk="1 of 2")
    big = dict(page, body="z" * 900, heading_trail="H", chunk="2 of 2")
    monkeypatch.setattr(verification, "_chunk_page", lambda page, fits, size: [small, big])
    with pytest.raises(verification._EvidenceTooLarge) as caught:
        verification._fitting_groups([page], lambda group: sum(len(p["body"]) for p in group), 600)
    assert caught.value.frame_chars == 900


@pytest.mark.parametrize(
    "text, expected",
    [
        # a longer fence is closed only by an equal or longer fence of the same character
        (
            "a\n\n````md\n```\ninner one\n\ninner two\n```\n````\n\nb\n",
            ["a\n\n", "````md\n```\ninner one\n\ninner two\n```\n````\n\n", "b\n"],
        ),
        ("a\n\n~~~~\nx\n\n~~~\ny\n~~~~\n\nb\n", ["a\n\n", "~~~~\nx\n\n~~~\ny\n~~~~\n\n", "b\n"]),
        ("a\n\n```python\nx\n\ny\n```\n\nb\n", ["a\n\n", "```python\nx\n\ny\n```\n\n", "b\n"]),
    ],
)
def test_fences_of_three_or_more_match_by_count(text, expected):
    assert verification._paragraphs(text) == expected
    assert "".join(verification._paragraphs(text)) == text


def test_headings_inside_a_four_backtick_fence_are_not_sections():
    body = "# A\n\n````\n```\n# inner\n```\n# still code\n````\n\n## B\n"
    assert [trail for trail, _ in verification._heading_pieces(body)] == ["A", "A > B"]


def test_a_skipped_boundary_window_is_logged_in_the_notes(monkeypatch):
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    monkeypatch.setattr(verification, "_boundary_window", lambda *args, **kwargs: None)
    client, notes = FrameClient(cfg), []
    asyncio.run(
        verification._semantic_evidence(
            "q", ["need"], [], [big_page(cap, sentinel_section=99)], model_client=client, budget=Budget(), notes=notes
        )
    )
    assert notes and all(n.startswith(f"boundary window skipped: {PATH} ") for n in notes)


def test_chunk_note_is_only_added_when_something_was_chunked():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    half = int(cap * 0.6)
    pages = [dict(path=f"wiki/p{i}.md", body="filler " * (half // 7), page_metadata={}, authority={}) for i in range(2)]
    systems = []

    class Spy(FrameClient):
        async def chat_json(self, system, user, **kwargs):
            systems.append(system)
            return await super().chat_json(system, user, **kwargs)

    run(pages, Spy(cfg))
    assert len(systems) == 2 and all("heading_trail locates it" not in s for s in systems)


# -- the answers flag is derived when it is the only inconsistency ------------------------------- #

CEF_PAGE = (
    "# Fleet fast storage\n\n"
    "Approved fast scratch location for the audit ledger: unknown as of 2026-10-06\n\n"
    "| Mount | Disk | Free | Used |\n|---|---|---|---|\n"
    "| App1 `/data` (`/dev/sdd1`, ext4) | virtual `QEMU HARDDISK` 953.9G, `ROTA=0` | 115,984,068,608 | 89% | no |\n\n"
    "Version of the audit tooling for that location: runner `bk1-full-audit-runner-resumed-fb18718-nvme-v10.py` (builder commit `fb18718`, profile v10).\n"
)
CEF_QUOTES = {
    "approval": "Approved fast scratch location for the audit ledger: unknown as of 2026-10-06",
    "storage": "App1 `/data` (`/dev/sdd1`, ext4) | virtual `QEMU HARDDISK` 953.9G, `ROTA=0` | 115,984,068,608 | 89% | no",
    "version": "Version of the audit tooling for that location: runner `bk1-full-audit-runner-resumed-fb18718-nvme-v10.py` (builder commit `fb18718`, profile v10).",
}


def cef_run2_reply():
    """The shape of the gap-cef3 run-2 reply: every row answered with literal quotes, answers=false."""
    return dict(
        answers=False,
        reason="storage and version are given",
        facets=[
            dict(facet=f, answered=True, scope_preserved=True, evidence=[dict(path=PATH, quote=q)])
            for f, q in CEF_QUOTES.items()
        ],
    )


def real_value_reply(facets=("storage", "version")):
    """Rows that quote real values, with answers=false (the cef3 run-2 flag mistake)."""
    return dict(
        answers=False,
        reason="storage and version are given",
        facets=[
            dict(facet=f, answered=True, scope_preserved=True, evidence=[dict(path=PATH, quote=CEF_QUOTES[f])])
            for f in facets
        ],
    )


def test_the_flag_alone_disagreeing_with_fully_valid_rows_is_derived_not_rejected():
    page = [dict(path=PATH, body=CEF_PAGE, page_metadata={})]
    names = ["storage", "version"]
    why = []
    out = verification._validate_semantic(
        real_value_reply(), names, [], page, question="what capacity exists?", why=why
    )
    assert out is not None and why == []
    assert out["answers"] is True and all(r["answered"] for r in out["facets"])
    # the derived verdict re-validates unchanged, so a stored checkpoint stays reusable
    assert verification._validate_semantic(out, names, [], page, question="what capacity exists?") == out


def test_a_real_value_reply_with_a_wrong_flag_resolves_without_a_re_ask():
    cfg = CuratorConfig()
    page = dict(path=PATH, body=CEF_PAGE, page_metadata={}, authority={})
    client = FrameClient(cfg, reply=real_value_reply())
    causes = []
    out = asyncio.run(
        verification._semantic_evidence(
            "what capacity exists?",
            ["storage", "version"],
            [],
            [page],
            model_client=client,
            budget=Budget(),
            causes=causes,
        )
    )
    assert out["answers"] is True and causes == [] and len(client.frames) == 1


def test_a_row_quoting_an_unknown_statement_is_unanswered_and_real_value_rows_still_count():
    page = [dict(path=PATH, body=CEF_PAGE, page_metadata={})]
    names = list(CEF_QUOTES)
    for flag in (False, True):
        reply = cef_run2_reply()
        reply["answers"] = flag
        out = verification._validate_semantic(reply, names, [], page, question="what capacity exists?")
        assert out is not None and out["answers"] is False
        by_facet = {r["facet"]: r for r in out["facets"]}
        assert by_facet["approval"]["answered"] is False and by_facet["approval"]["evidence"] == []
        assert by_facet["storage"]["answered"] and by_facet["version"]["answered"]


def one_quote(quote, body):
    page = [dict(path=PATH, body=body, page_metadata={})]
    return verification._validate_semantic(positive_for(quote), ["need"], [], page, {PATH: body})


@pytest.mark.parametrize(
    "quote",
    [
        "Unknown as of 2026-10-06: an authoritative sweep/station record of station targets",
        "Authority status. Unknown as of 2026-10-06: an authoritative sweep/station record of station targets",
        "Deletion safety is not established; nothing was deleted.",
        "The ledger size is unmeasured on this host.",
        "Disk errors were not observed during the window.",
        "The approval was not recorded anywhere.",
    ],
)
def test_a_quote_with_an_unknown_marker_and_no_value_never_establishes_a_facet(quote):
    out = one_quote(quote, "# P\n\n" + quote + "\n")
    assert out is not None and out["answers"] is False and out["facets"][0]["answered"] is False


SURROUNDED = [
    # (full page text, quote): the marker is in the surrounding sentence/row/line, never in the quote
    ("Free space was 23 GB; headroom unknown as of 2026-10-06.", "Free space was 23 GB"),
    ("Free space was 23 GB, headroom unknown as of 2026-10-06.", "Free space was 23 GB"),
    ("Free space was 23 GB (headroom unknown as of 2026-10-06).", "Free space was 23 GB"),
    ("Free space was 23 GB. headroom unknown as of 2026-10-06.", "Free space was 23 GB"),
    ("Free space was 23 GB,\nheadroom unknown as of 2026-10-06.", "Free space was 23 GB,"),
    ("Unknown as of 2026-10-06: the approval; free space was 23 GB.", "free space was 23 GB"),
    ("| /data | 953.9 GB | headroom unknown as of 2026-10-06 |", "/data | 953.9 GB"),
    ("The unknown-vendor disk holds 931.5 GB.", "The unknown-vendor disk holds 931.5 GB."),
    ("The pool has 2 TB free (SMART not verified).", "The pool has 2 TB free (SMART not verified)."),
    ("The pool has 2 TB free, no evidence of wear.", "The pool has 2 TB free, no evidence of wear."),
    ("The disk is 931.5 GB; its age is not known.", "The disk is 931.5 GB; its age is not known."),
]


@pytest.mark.parametrize("text, quote", SURROUNDED)
def test_the_unknown_rule_never_looks_at_the_surrounding_text(text, quote):
    out = one_quote(quote, "# P\n\n" + text + "\n")
    assert out is not None and out["answers"] is True


CEF3_APPROVAL = "Approved fast scratch location for the roughly 232 GB audit ledger: unknown as of 2026-10-06"


def test_the_cef3_approval_quote_is_downgraded_the_head_is_the_subject_of_the_unknown_statement():
    assert verification._states_not_established(CEF3_APPROVAL) is True
    out = one_quote(CEF3_APPROVAL, "# P\n\n" + CEF3_APPROVAL + "\n")
    assert out["answers"] is False and out["facets"][0]["answered"] is False
    long_form = (
        CEF3_APPROVAL + ' (commands and results are in "Current answers" above) [source: live check ws1 2026-10-05].'
    )
    assert verification._states_not_established(long_form) is True


@pytest.mark.parametrize(
    "quote, downgraded",
    [
        ("Free space was 23 GB; headroom unknown as of 2026-10-06", False),
        ("Headroom unknown as of 2026-10-06; free space was 23 GB", False),
        ("Free space was 23 GB. The headroom is not recorded.", False),
        ("The disk holds 931.5 GB, its wear is not observed.", False),
        ("Free space was 23 GB, headroom unknown as of 2026-10-06.", False),
        ("Free space was 23 GB (headroom unknown as of 2026-10-06).", False),
        ("Approved scratch for the 232 GB ledger (see Sec. 4): unknown as of 2026-10-06", True),
        ("Location for the 232 GB ledger, i.e. the audit DB: unknown as of 2026-10-06", True),
        ("Approved fast scratch location for the roughly 232 GB audit ledger ... unknown as of 2026-10-06", True),
        ("Approved scratch for the 232 GB ledger, e.g. the audit DB: unknown as of 2026-10-06", True),
        ("The audit ledger is roughly 232 GB: unknown as of 2026-10-06", True),
        ("Unknown as of 2026-10-06: 23 GB free", True),
        ("Deletion safety is not established; nothing was deleted.", True),
        ("The pool has 2 TB free (SMART not verified).", False),
    ],
)
def test_a_marker_quote_is_kept_only_when_a_clause_without_the_marker_holds_a_value(quote, downgraded):
    assert verification._states_not_established(quote) is downgraded


def test_a_row_is_downgraded_only_when_all_its_quotes_are():
    body = "# P\n\nUnknown as of 2026-10-06: the record.\n\nThe table carries `fetched_at` per fetch.\n"
    page = [dict(path=PATH, body=body, page_metadata={})]
    real = "The table carries `fetched_at` per fetch."
    mixed = dict(
        answers=True,
        reason="r",
        facets=[
            dict(
                facet="need",
                answered=True,
                scope_preserved=True,
                evidence=[dict(path=PATH, quote="Unknown as of 2026-10-06: the record."), dict(path=PATH, quote=real)],
            )
        ],
    )
    out = verification._validate_semantic(mixed, ["need"], [], page, {PATH: body})
    assert out["answers"] is True and [c["quote"] for c in out["facets"][0]["evidence"]] == [real]


LIVE_SHARD = (
    "Unknown as of 2026-10-05: `grep -rniE 'retention|delet' docs/` returned 15 lines, none of which is a retention period "
    '(the one relevant line says "No unrelated cache or data was deleted"). Deletion safety is not established; nothing was '
    "deleted. These directories total about 58M and SHARD9 root has 374G free (`df -h` 2026-10-05).\n"
)
LIVE_FLEET = (
    "The audit inputs and the current ledger size were not measured by any command run for this page, so whether ledger plus "
    "inputs plus reserves fits on Gpu1 (333,824,475,136 bytes free on 2026-10-06) is Unknown as of 2026-10-06.\n"
)
LIVE_WS1 = (
    "- Cause of that GDM failure: unknown as of 2026-10-05.\n"
    "- Boot -3: its last journal lines are `earlyoom[2434]: mem avail: 2075 of 50888 MiB ( 4.08%), swap free: 0 of 8191 MiB`; "
    "why the machine went down at that point: unknown as of 2026-10-05.\n\n"
    "| 2026-10-04 00:45:15 | the kernel killed `git` pid 367626 (`anon-rss:8963396kB`, global OOM). Cause for this logout: unknown as of 2026-10-05. |\n"
)


@pytest.mark.parametrize(
    "text, quote",
    [
        (LIVE_SHARD, "These directories total about 58M and SHARD9 root has 374G free"),
        (LIVE_FLEET, "Gpu1 (333,824,475,136 bytes free on 2026-10-06)"),
        (LIVE_WS1, "mem avail: 2075 of 50888 MiB ( 4.08%)"),
        (LIVE_WS1, "the kernel killed `git` pid 367626 (`anon-rss:8963396kB`, global OOM)"),
        (LIVE_WS1, "(`anon-rss:8963396kB`, global OOM). Cause for this logout: unknown as of 2026-10-05."),
    ],
)
def test_the_four_live_vault_cases_are_kept(text, quote):
    out = one_quote(quote, "# P\n\n" + text)
    assert out is not None and out["answers"] is True and out["facets"][0]["evidence"][0]["quote"] == quote


def test_a_cef3_run2_shaped_reply_is_a_final_partial_when_one_row_quotes_an_unknown():
    cfg = CuratorConfig()
    page = dict(path=PATH, body=CEF_PAGE, page_metadata={}, authority={})
    client = FrameClient(cfg, reply=cef_run2_reply())
    causes = []
    out = asyncio.run(
        verification._semantic_evidence(
            "what capacity exists?", list(CEF_QUOTES), [], [page], model_client=client, budget=Budget(), causes=causes
        )
    )
    assert out["answers"] is False and causes == [] and len(client.frames) == 1
    assert {r["facet"]: r["answered"] for r in out["facets"]} == {"approval": False, "storage": True, "version": True}


def test_other_contradictions_are_not_derived():
    page = [dict(path=PATH, body=CEF_PAGE, page_metadata={})]
    names = list(CEF_QUOTES)
    # a dropped (non-literal) citation beside the valid one
    dropped = cef_run2_reply()
    dropped["facets"][1]["evidence"].append(dict(path=PATH, quote="a quote that is not on the page at all"))
    why = []
    assert verification._validate_semantic(dropped, names, [], page, question="q?", why=why) is None
    assert why == [verification._CONTRADICTION]
    # a row downgraded by the chunk scope check
    chunk = verification._Chunk(path=PATH, body=CEF_PAGE, page_metadata={}, heading_trail="Elsewhere", chunk="2 of 2")
    why = []
    assert (
        verification._validate_semantic(
            cef_run2_reply(),
            names,
            [dict(dimension="platform", value="SVC1")],
            [chunk],
            {PATH: CEF_PAGE},
            question="q?",
            why=why,
        )
        is None
    )
    assert why == [verification._CONTRADICTION]
    # one row not answered: the flag is simply right
    partial = cef_run2_reply()
    partial["facets"][0].update(answered=False)
    assert verification._validate_semantic(partial, names, [], page, question="q?")["answers"] is False


def test_locate_never_falls_back_outside_the_shown_chunk_for_formatted_quotes():
    sentence = "**The scratch volume** is /mnt/fast-scratch today."
    quote = "The scratch volume is /mnt/fast-scratch today."
    full = f"## prod\n\n{sentence}\n\nfillerA\n\n## SVC1\n\n{sentence}\n"
    low = full.index("fillerA")
    svc1 = full.rindex(sentence)
    assert verification._locate(full, quote, low, len(full)) == svc1  # not the prod copy before low
    assert verification._locate(full, quote, 0, low) == full.index(
        sentence
    )  # the prod copy, when that is what was shown
    assert verification._locate(full, quote, low, svc1) is None  # not in the shown span: not placed
    prod_chunk = verification._Chunk(path=PATH, body=full[:low], page_metadata={}, heading_trail="prod", chunk="1 of 2")
    out = verification._validate_semantic(positive_for(quote), ["need"], SCOPES, [prod_chunk], {PATH: full})
    assert out["answers"] is False  # only the prod copy was shown: the SVC1 copy must not satisfy scope SVC1
    svc1_chunk = verification._Chunk(
        path=PATH, body=full[low:], page_metadata={}, heading_trail="fillerA through SVC1", chunk="2 of 2"
    )
    svc1_chunk.start = low
    assert (
        verification._validate_semantic(positive_for(quote), ["need"], SCOPES, [svc1_chunk], {PATH: full})["answers"]
        is True
    )


# -- a gap without facets: the model lists the facets and the constraints ------------------------- #

Q6430 = (
    "Which existing authorized stored TAF NWS sweep-grain station tables or sources provide true station target, route, "
    "and precipitation sample denominators, keyed to NWS sweep/station IDs, with ingestion receipt clocks? This is for the "
    "selected Columbus\u2013Dayton nowcast handoff on App1. Distinguish season aggregates and "
    "gauge sums from authoritative sweep/station denominator records; do not infer absence from catalog coverage."
)
RECEIPT = "the existing ingestion receipt clock of this source is `fetched_at`; the table has no `loaded_at`"
PAGE6430 = (
    "# NWS\n\nFor the selected Columbus at Dayton nowcast handoff, " + RECEIPT + "\n\n"
    "Unknown as of 2026-10-06: an authoritative sweep/station record of station targets, routes and precipitation samples.\n"
)
NAMES6430 = ("target", "route", "precipitation sample", "keying", "receipt clocks")


def facet_rows(answered_names, names=NAMES6430):
    return dict(
        answers=False,
        reason="r",
        facets=[
            dict(
                facet=n,
                answered=n in answered_names,
                scope_preserved=True,
                evidence=[dict(path=PATH, quote=RECEIPT)] if n in answered_names else [],
            )
            for n in names
        ],
    )


def derive(reply, question=Q6430, body=PAGE6430):
    cfg = CuratorConfig()
    client = FrameClient(cfg, reply=reply)
    causes = []
    out = asyncio.run(
        verification._semantic_evidence(
            question,
            [],
            [],
            [dict(path=PATH, body=body, page_metadata={}, authority={})],
            model_client=client,
            budget=Budget(),
            causes=causes,
        )
    )
    return out, causes, client


def test_a_receipt_clock_only_quote_does_not_resolve_the_6430_question():
    out, causes, client = derive(facet_rows({"receipt clocks"}))
    assert causes == [] and out["answers"] is False
    assert {r["facet"]: r["answered"] for r in out["facets"]} == {
        "target": False,
        "route": False,
        "precipitation sample": False,
        "keying": False,
        "receipt clocks": True,
    }
    frame = client.frames[0]
    assert frame["facets"] == [] and "minimum_facets" not in frame


def test_a_model_listed_facet_list_of_any_length_is_accepted_no_splitter_floor():
    one = dict(
        answers=True,
        reason="r",
        facets=[
            dict(
                facet="track and field schedule",
                answered=True,
                scope_preserved=True,
                evidence=[dict(path=PATH, quote=RECEIPT)],
            )
        ],
    )
    out, causes, client = derive(one, question="Who publishes the track and field schedule, and where?")
    assert causes == [] and out["answers"] is True and len(client.frames) == 1
    # an empty list or blank names are still not a facet list
    page = [dict(path=PATH, body=PAGE6430, page_metadata={})]
    for bad in ([], [dict(facet="  ", answered=False, scope_preserved=True, evidence=[])]):
        why = []
        assert (
            verification._validate_semantic(dict(answers=False, reason="r", facets=bad), None, [], page, why=why)
            is None
        )
        assert why == ["facet rows do not match the requested facets"]


def test_a_listed_facet_set_that_answers_every_row_resolves_and_is_stored_in_the_verdict():
    quote = "Station target, route and sample counts come from `station_sweep_denominators` keyed by nws_sweep_id and nws_station_id."
    body = PAGE6430 + "\n" + quote + "\n"
    rows = [
        dict(
            facet=n,
            answered=True,
            scope_preserved=True,
            evidence=[dict(path=PATH, quote=RECEIPT if n == "receipt clocks" else quote)],
        )
        for n in NAMES6430
    ]
    out, causes, _ = derive(dict(answers=True, reason="r", facets=rows), body=body)
    assert out["answers"] is True and [r["facet"] for r in out["facets"]] == list(NAMES6430)
    # re-validating the stored verdict derives the same facet list from its own rows
    again = verification._validate_semantic(
        out, None, [], [dict(path=PATH, body=body, page_metadata={})], question=Q6430
    )
    assert again == out


def test_derived_facet_names_must_be_distinct_strings():
    page = [dict(path=PATH, body=PAGE6430, page_metadata={})]
    rows = facet_rows({"receipt clocks"}, names=("a", "a", "b", "c", "d"))
    why = []
    assert verification._validate_semantic(rows, None, [], page, question=Q6430, why=why) is None
    assert why == ["facet rows do not match the requested facets"]


def test_verify_gap_with_no_facets_judges_the_delivered_evidence_with_the_derived_list(published, tmp_path):
    root, path, commit, ledger, record, reply_, index = published
    ledger.update(record.gap_id, lambda r: setattr(r, "facets", []))
    frames = []
    cfg = CuratorConfig()
    cfg.credentials_file = str(tmp_path / "no-credentials")
    cfg.inference.retries = 0

    def respond(request):
        frames.append(json.loads(json.loads(request.content)["messages"][-1]["content"]))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(positive(path))}, "finish_reason": "stop"}]}
        )

    usage = Usage()
    model = ModelClient(cfg, usage, transport=httpx.MockTransport(respond))
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply_, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "resolved" and usage.model_calls == 2
    assert frames[0]["facets"] == [] and "minimum_facets" not in frames[0]
    assert frames[1]["facets"] == ["rate"]


# -- constraints come only from the model's list --------------------------------------------------- #


@pytest.mark.parametrize(
    "name", ["Do not disturb schedule", "Never-scrubbed disks", "No SSH access hosts", "Without-root accounts"]
)
def test_constraint_like_facet_names_stay_ordinary_facets(name):
    page = [dict(path=PATH, body=PAGE6430, page_metadata={})]
    reply = dict(
        answers=True,
        reason="r",
        facets=[dict(facet=name, answered=True, scope_preserved=True, evidence=[dict(path=PATH, quote=RECEIPT)])],
    )
    out = verification._validate_semantic(reply, None, [], page)
    assert out is not None and [r["facet"] for r in out["facets"]] == [name] and "constraints" not in out


def test_model_listed_constraints_are_kept_and_must_be_strings():
    page = [dict(path=PATH, body=PAGE6430, page_metadata={})]
    ok = facet_rows({"receipt clocks"})
    ok["constraints"] = ["do not infer absence from catalog coverage"]
    out = verification._validate_semantic(ok, None, [], page, question=Q6430)
    assert out["constraints"] == ["do not infer absence from catalog coverage"] and out["answers"] is False
    for bad in ("do not", ["ok", 3]):
        reply = dict(facet_rows({"receipt clocks"}), constraints=bad)
        why = []
        assert verification._validate_semantic(reply, None, [], page, question=Q6430, why=why) is None
        assert why == ["reply is not the verdict object"]


def test_the_derive_prompt_lists_constraints_separately_and_is_dropped_once_facets_are_fixed():
    cfg = CuratorConfig()
    cap = verification._frame_char_cap(cfg)
    systems, frames = [], []

    class Spy(FrameClient):
        async def chat_json(self, system, user, **kwargs):
            systems.append(system)
            frames.append(json.loads(user))
            first = len(systems) == 1
            frame = frames[-1]
            names = frame["facets"] or list(NAMES6430)
            rows = [dict(facet=n, answered=False, scope_preserved=True, evidence=[]) for n in names]
            reply = dict(answers=False, reason="r", facets=rows)
            if first:
                reply["constraints"] = ["never invent a source"]
            elif len(systems) == 2:
                reply["constraints"] = ["avoid the catalog"]
            return reply

    out = asyncio.run(
        verification._semantic_evidence(
            Q6430, [], [], [big_page(cap, sentinel_section=99)], model_client=Spy(cfg), budget=Budget()
        )
    )
    assert '"constraints"' in systems[0] and "never give them a row" in systems[0]
    assert "facets list is empty" in systems[0]
    # later groups and boundary windows carry the fixed facet list and no listing instruction
    assert len(systems) > 2 and all("facets list is empty" not in s for s in systems[1:])
    assert all(f["facets"] == list(NAMES6430) for f in frames[1:])
    # merging the chunk verdicts keeps the model's constraints (union, in order)
    assert out["answers"] is False and out["constraints"] == ["never invent a source", "avoid the catalog"]


CONTINUED = "# P\n\nApproved fast scratch location for the roughly 232 GB audit ledger: unknown as of 2026-10-06 (see Sec. 4).\n"


@pytest.mark.parametrize(
    "quote",
    [
        "Approved fast scratch location for the roughly 232 GB audit ledger",
        "Approved fast scratch location for the roughly 232 GB",
    ],
)
def test_a_quote_that_stops_before_a_colon_marker_tail_in_its_clause_is_downgraded(quote):
    assert verification._states_not_established(quote, CONTINUED) is True
    out = one_quote(quote, CONTINUED)
    assert out["answers"] is False and out["facets"][0]["answered"] is False


@pytest.mark.parametrize(
    "text, quote",
    [
        ("Free space was 23 GB; headroom unknown as of 2026-10-06.", "Free space was 23 GB"),
        ("Approved scratch is 232 GB. It is unknown as of 2026-10-06 whether it fits.", "Approved scratch is 232 GB."),
        (
            "It fits on Gpu1 (333,824,475,136 bytes free on 2026-10-06) is Unknown as of 2026-10-06.",
            "Gpu1 (333,824,475,136 bytes free on 2026-10-06)",
        ),
        ("Note: the ledger is about 232 GB. Cause: unknown as of 2026-10-05.", "the ledger is about 232 GB."),
    ],
)
def test_the_clause_continuation_needs_a_colon_marker_tail_in_the_same_clause(text, quote):
    assert verification._states_not_established(quote, "# P\n\n" + text + "\n") is False


def test_the_sentence_split_does_not_end_after_abbreviations_or_ellipses():
    assert verification._sentences("See Sec. 4 for details. Next one.") == ["See Sec. 4 for details.", "Next one."]
    assert verification._sentences("The DB, i.e. the audit one, e.g. prod. Done.") == [
        "The DB, i.e. the audit one, e.g. prod.",
        "Done.",
    ]
    assert verification._sentences("It is unclear... still unknown. Ok?") == ["It is unclear... still unknown.", "Ok?"]


# -- the continuation tail is bounded; colon markers act on their own clause; masks need a lowercase continuation -- #

LIVE_277G = (
    "Gpu1 also mounts `App1:/mnt/tank/bulk` at `/mnt/tank/bulk` over sshfs (read-only, `ro,nosuid,nodev,relatime` "
    "on 2026-10-06; 277G available per `df -h` on 2026-10-05); that is the App1 `/mnt/tank` disk (`ROTA=1`) and not fast "
    "storage. [source: live check Gpu1 2026-10-05, 2026-10-06]"
)
LIVE_277G_PAGE = (
    "# Fleet\n\n" + LIVE_277G + "\n\n## Approved fast scratch locations\n\n"
    "Approved fast scratch location for the roughly 232 GB audit ledger: unknown as of 2026-10-06 (commands and results are in "
    '"Current answers" above) [source: live check ws1 2026-10-05, 2026-10-06; brain base `8041fdc`].\n'
)


def test_the_live_277g_whole_line_quote_is_kept():
    # the tail ends at the first newline: it never reaches the later "232 GB ...: unknown as of" paragraph
    assert verification._states_not_established(LIVE_277G, LIVE_277G_PAGE) is False
    assert verification._states_not_established(LIVE_277G[:-1], LIVE_277G_PAGE) is False
    out = one_quote(LIVE_277G, LIVE_277G_PAGE)
    assert out["answers"] is True and out["facets"][0]["evidence"][0]["quote"] == LIVE_277G


@pytest.mark.parametrize(
    "text, quote",
    [
        ("- Free space: 23 GB\n- Approval: unknown as of 2026-10-06\n", "Free space: 23 GB"),
        ("- Free space: 23 GB\n\n- Approval: unknown as of 2026-10-06\n", "Free space: 23 GB"),
        ("- Free space: 23 GB\n\n## Next\n\nApproval: unknown as of 2026-10-06\n", "Free space: 23 GB"),
        (
            "Free space: 23 GB [source: live check 2026-10-05]\nApproval: unknown as of 2026-10-06\n",
            "Free space: 23 GB [source: live check 2026-10-05]",
        ),
        ("| /data | 953.9 GB | ok |\n| /scratch | n/a | approval: unknown as of 2026-10-06 |\n", "/data | 953.9 GB"),
        ("| /data | 953.9 GB | approval: unknown as of 2026-10-06 |\n", "/data | 953.9 GB"),
    ],
)
def test_the_continuation_tail_ends_at_the_line_and_at_the_next_pipe(text, quote):
    assert verification._states_not_established(quote, "# P\n\n" + text) is False


@pytest.mark.parametrize(
    "quote, downgraded",
    [
        ("Free space was 23 GB, headroom: unknown as of 2026-10-06.", False),
        ("Free space was 23 GB (headroom: unknown as of 2026-10-06).", False),
        ("Free space was 23 GB (approved: unknown as of 2026-10-06).", False),
        ("Free space was 23 GB; approval: unknown as of 2026-10-06", False),
        ("Location for the 232 GB ledger, e.g. the audit DB: unknown as of 2026-10-06", True),
        ("Approved scratch for the 232 GB ledger (see Sec. 4): unknown as of 2026-10-06", True),
    ],
)
def test_a_colon_marker_in_a_later_clause_takes_only_that_clause(quote, downgraded):
    assert verification._states_not_established(quote) is downgraded


def test_a_colon_marker_in_a_later_comma_clause_does_not_downgrade_the_head_quote():
    page = "# P\n\nFree space was 23 GB on /data, see note: headroom unknown as of 2026-10-06.\n"
    assert verification._states_not_established("Free space was 23 GB on /data", page) is False
    page = "# P\n\nFree space was 23 GB, headroom: unknown as of 2026-10-06.\n"
    assert verification._states_not_established("Free space was 23 GB", page) is False


@pytest.mark.parametrize(
    "text, quote",
    [
        (
            "Approval: unknown as of 2026-10-06 for logs etc. Free space was 23 GB.",
            "Approval: unknown as of 2026-10-06 for logs etc. Free space was 23 GB.",
        ),
        (
            "Approval: unknown as of 2026-10-06... Free space was 23 GB.",
            "Approval: unknown as of 2026-10-06... Free space was 23 GB.",
        ),
    ],
)
def test_etc_and_ellipsis_before_an_uppercase_word_end_the_sentence(text, quote):
    assert verification._states_not_established(quote) is False
    assert verification._sentences("Logs etc. Free space.") == ["Logs etc.", "Free space."]
    assert verification._sentences("Wait... Free space.") == ["Wait...", "Free space."]
    assert verification._sentences("Logs etc. and more. Done.") == ["Logs etc. and more.", "Done."]
    assert verification._sentences("Wait... still more. Done.") == ["Wait... still more.", "Done."]


@pytest.mark.parametrize("appositive", ["i.e.", "e.g."])
def test_the_tail_cut_keeps_an_appositive_with_its_head_like_the_in_quote_split(appositive):
    page = f"# P\n\nLocation for the 232 GB ledger, {appositive} the audit DB: unknown as of 2026-10-06\n"
    head = "Location for the 232 GB ledger"
    assert verification._states_not_established(head, page) is True
    assert (
        verification._states_not_established(page.split("\n\n", 1)[1].strip()) is True
    )  # the whole line, in-quote path
    # a plain comma clause is still its own clause: the head keeps its value
    other = "# P\n\nLocation for the 232 GB ledger, see note: unknown as of 2026-10-06\n"
    assert verification._states_not_established(head, other) is False
    out = one_quote(head, page)
    assert out["answers"] is False and out["facets"][0]["answered"] is False
