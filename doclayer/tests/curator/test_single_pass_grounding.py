"""Single-pass grounding: bounded chunk packing and fail-closed verdicts."""

import json

import pytest

from doclayer.curator import degrade, single_pass
from doclayer.curator.model import ReplyRejected
from doclayer.curator.state import Evidence, PageChange

URL = "https://docs.test/model"
OTHER = "https://docs.test/other"
BUDGET = 6000


def record(claim_id, url=URL, quote="Verbatim source support. " * 30):
    return Evidence(
        "src-0123456789",
        "Model docs",
        "## Limits",
        "quote",
        f"Claim {claim_id} is documented.",
        claim_id=claim_id,
        url=url,
        quote=quote,
        support_status="supported",
    )


def contribution(number, *, claim_ids=None, cite=URL, text=None):
    return {
        "id": f"k{number}",
        "section_id": "section-1",
        "kind": "append",
        "index": -1,
        "text": text or f"Claim c{number} is documented. [source: {cite}]",
        "claim_ids": [f"c{number}"] if claim_ids is None else claim_ids,
    }


class StubClient:
    """Answers each chunk through `reply`, recording every call."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def chat_bounded_json(self, system, user, *, char_budget, **kwargs):
        self.calls.append({"system": system, "user": user, "char_budget": char_budget, **kwargs})
        outcome = self.reply(json.loads(user.split("\n", 1)[1]))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def all_supported(frame):
    return {"verdicts": [{"id": row["id"], "supported": True, "reason": "stated"} for row in frame["contributions"]]}


CHANGE = PageChange("g", "wiki/existing-model.md", "update")


def test_chunks_respect_budget_and_cap():
    records = [record(f"c{number}") for number in range(1, 6)]
    contributions = [contribution(number) for number in range(1, 6)]
    one_size = len(json.dumps(single_pass._evidence_row(records[0])))
    budget = 2 * one_size + 2500  # two contributions per chunk, three do not fit
    chunks, overflow = single_pass.grounding_chunks(contributions, records, [], char_budget=budget, max_chunks=2)
    assert [[row["id"] for row in chunk["contributions"]] for chunk in chunks] == [["k1", "k2"], ["k3", "k4"]]
    assert overflow == ["k5"]
    for chunk in chunks:
        prompt = single_pass.GROUND_SYSTEM + "SINGLE-PASS GROUNDING\n" + json.dumps({"page": CHANGE.path, **chunk})
        assert len(prompt) + 1000 <= budget
        own = {row["id"] for row in chunk["contributions"]}
        assert {row["id"] for row in chunk["evidence"]} == {"c" + identity[1:] for identity in own}
    # A contribution too large for a chunk alone overflows without blocking the others.
    huge = contribution(9, text="Claim c9 is documented. [source: " + URL + "] " + "x" * budget)
    chunks, overflow = single_pass.grounding_chunks(
        [contributions[0], huge, contributions[1]], records, [], char_budget=budget, max_chunks=2
    )
    assert overflow == ["k9"] and [row["id"] for row in chunks[0]["contributions"]] == ["k1", "k2"]
    # Evidence comes from the contribution's own claims and its cited URL.
    chunks, _ = single_pass.grounding_chunks(
        [contribution(1, claim_ids=[], cite=OTHER)],
        records + [record("c7", url=OTHER)],
        [],
        char_budget=BUDGET,
        max_chunks=1,
    )
    assert [row["id"] for row in chunks[0]["evidence"]] == ["c7"]
    assert single_pass.GROUND_SYSTEM.startswith("You are the single-pass grounding reviewer.")


async def test_missing_verdict_is_unsupported():
    records = [record(f"c{number}") for number in range(1, 4)]
    chunks, _ = single_pass.grounding_chunks(
        [contribution(number) for number in range(1, 4)], records, [], char_budget=BUDGET, max_chunks=1
    )
    client = StubClient(
        lambda frame: {
            "verdicts": [
                {"id": "k1", "supported": True, "reason": "stated"},
                {"id": "k2", "supported": "yes", "reason": "not a boolean"},
                {"id": "foreign", "supported": True, "reason": "not in this chunk"},
            ]
        }
    )
    verdicts = await single_pass.ground(client, CHANGE, chunks, char_budget=BUDGET, saved={}, progress=lambda: None)
    assert verdicts["k1"]["supported"] is True
    assert verdicts["k2"]["supported"] is False
    assert verdicts["k3"] == {"supported": False, "reason": "no verdict returned"}
    assert set(verdicts) == {"k1", "k2", "k3"}
    call = client.calls[0]
    assert call["json_object"] is True and call["char_budget"] == BUDGET
    assert call["system"] == single_pass.GROUND_SYSTEM and call["user"].startswith("SINGLE-PASS GROUNDING\n")


async def test_rejected_chunk_reply_drops_chunk_and_records_gap():
    records = [record(f"c{number}") for number in range(1, 5)]
    one_size = len(json.dumps(single_pass._evidence_row(records[0])))
    chunks, overflow = single_pass.grounding_chunks(
        [contribution(number) for number in range(1, 5)], records, [], char_budget=2 * one_size + 2500, max_chunks=2
    )
    assert len(chunks) == 2 and overflow == []

    def reply(frame):
        if frame["contributions"][0]["id"] == "k1":
            return ReplyRejected("unparseable grounding reply", transient=True)
        return all_supported(frame)

    gaps, saved, saves = [], {}, []
    with degrade.recording(gaps):
        verdicts = await single_pass.ground(
            StubClient(reply),
            CHANGE,
            chunks,
            char_budget=BUDGET,
            saved=saved,
            progress=lambda: saves.append(len(saved)),
        )
    assert [verdicts[key]["supported"] for key in ("k1", "k2", "k3", "k4")] == [False, False, True, True]
    assert len(gaps) == 1 and "wiki/existing-model.md: grounding chunk 1" in gaps[0] and degrade.GAP_MARK in gaps[0]
    assert len(saved) == 1 and saves == [1]  # the rejected chunk is not saved: a resume may ask it again
    with pytest.raises(ReplyRejected):  # no job to record the gap on: never dropped silently
        await single_pass.ground(StubClient(reply), CHANGE, chunks[:1], char_budget=BUDGET, saved={}, progress=None)


async def test_saved_chunk_not_reasked():
    records = [record("c1"), record("c2")]
    chunks, _ = single_pass.grounding_chunks(
        [contribution(1), contribution(2)], records, [], char_budget=BUDGET, max_chunks=2
    )
    client, saved = StubClient(all_supported), {}
    first = await single_pass.ground(client, CHANGE, chunks, char_budget=BUDGET, saved=saved, progress=lambda: None)
    assert len(client.calls) == 1 and all(row["supported"] for row in first.values())
    assert json.loads(json.dumps(saved)) == saved  # persistable
    again = StubClient(lambda frame: AssertionError("asked again"))
    second = await single_pass.ground(again, CHANGE, chunks, char_budget=BUDGET, saved=saved, progress=None)
    assert again.calls == [] and second == first


async def test_duplicate_verdicts_fail_closed():
    records = [record("c1"), record("c2")]
    chunks, _ = single_pass.grounding_chunks(
        [contribution(1), contribution(2)], records, [], char_budget=BUDGET, max_chunks=1
    )
    for order in ((False, True), (True, False)):
        rows = [{"id": "k1", "supported": value, "reason": f"{value}"} for value in order]
        rows.append({"id": "k2", "supported": True, "reason": "stated"})
        rows.append({"id": "k2", "supported": True, "reason": "stated again"})
        verdicts = await single_pass.ground(
            StubClient(lambda frame, rows=rows: {"verdicts": rows}),
            CHANGE,
            chunks,
            char_budget=BUDGET,
            saved={},
            progress=None,
        )
        assert verdicts["k1"]["supported"] is False and verdicts["k1"]["reason"] == "False"
        assert verdicts["k2"]["supported"] is True


async def test_saved_verdicts_are_completed_against_the_chunk():
    records = [record("c1"), record("c2")]
    chunks, _ = single_pass.grounding_chunks(
        [contribution(1), contribution(2)], records, [], char_budget=BUDGET, max_chunks=1
    )
    key = single_pass._chunk_hash(chunks[0])
    saved = {
        key: {
            "k1": {"supported": True, "reason": "stated"},
            "foreign": {"supported": True, "reason": "x"},
            "k2": {"supported": "yes", "reason": "not a boolean"},
        }
    }
    client = StubClient(lambda frame: AssertionError("asked again"))
    verdicts = await single_pass.ground(client, CHANGE, chunks, char_budget=BUDGET, saved=saved, progress=None)
    assert client.calls == []
    assert verdicts == {
        "k1": {"supported": True, "reason": "stated"},
        "k2": {"supported": False, "reason": "not a boolean"},
    }
    saved[key] = {}
    verdicts = await single_pass.ground(client, CHANGE, chunks, char_budget=BUDGET, saved=saved, progress=None)
    assert verdicts == {
        "k1": {"supported": False, "reason": "no verdict returned"},
        "k2": {"supported": False, "reason": "no verdict returned"},
    }


def test_reserve_accounts_for_the_page_path():
    records = [record(f"c{number}") for number in range(1, 8)]
    contributions = [contribution(number) for number in range(1, 8)]
    path = "wiki/" + "deep/" * 60 + "page.md"
    assert len(path) > 300
    one_size = len(json.dumps(single_pass._evidence_row(records[0])))
    budget = 3 * one_size + 2500
    chunks, _ = single_pass.grounding_chunks(
        contributions, records, [], char_budget=budget, max_chunks=7, page_path=path
    )
    assert chunks
    for chunk in chunks:
        prompt = single_pass.GROUND_SYSTEM + "SINGLE-PASS GROUNDING\n" + json.dumps({"page": path, **chunk})
        assert len(prompt) + single_pass.REPLY_RESERVE <= budget
