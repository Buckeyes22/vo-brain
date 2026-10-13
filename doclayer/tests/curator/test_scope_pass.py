"""A draft citing an excluded source goes to the model repair in a
dedicated scope pass. Scope passes do not spend the general repair passes, at
most MAX_SCOPE_REPAIR_PASSES run per page (within the job's model-call limit),
and the instruction lists each rejected URL with every new sentence citing it.
Also: the no-progress guard of the deterministic section/title restore.
"""

import json
import re

import pytest
from test_scope_repair_restores import OK, POLICY, REJ, page_job

from doclayer.curator import curate
from doclayer.curator.pipeline import MAX_DETERMINISTIC_PASSES, MAX_SCOPE_REPAIR_PASSES, Pipeline
from doclayer.curator.state import SourceRecord

OTHER = "https://ex.com/pricing"
BASE = f"# Probe Page\n\nIntro words of this page [source: {OK}].\n\n## Limits\n\nFive hundred per minute [source: {OK}].\n"
DRAFT = (
    f"# Probe Page\n\nIntro words of this page [source: {OK}]. A new tier exists [source: {REJ}].\n\n"
    f"## Limits\n\nFive hundred per minute [source: {OK}]. Bursts double it [source: {REJ}] [source: {OK}]. "
    f"Batch costs half [source: {OTHER}].\n"
)


def scoped(state, store):
    state.source_policy = POLICY
    state.sources = [
        SourceRecord("ok", "url", "ok", url=OK),
        SourceRecord("rej", "url", "rej", url=REJ, status="rejected"),
        SourceRecord("other", "url", "other", url=OTHER, status="rejected"),
    ]
    store.save(state)
    return state


def follow(body: str, instruction: str) -> str:
    """A model reply that does what the scope instruction says."""
    for row in re.findall(r"^- (\S+) is cited by: (.*)$", instruction, re.M):
        url, listed = row
        for sentence in re.findall(r'"((?:[^"\\]|\\.)*)"', listed):
            sentence = json.loads(f'"{sentence}"')
            stripped = re.sub(r"\s*\[source:\s*" + re.escape(url) + r"\s*\]", "", sentence)
            body = body.replace(sentence, stripped if "[source:" in stripped else "")
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"  +", " ", body))


def capture(monkeypatch, reply):
    calls = []

    async def model_repair(client, topic, change, failures, *args, **kwargs):
        calls.append(
            {"topic": topic, "failures": failures, "body": change.draft_body, "kind": change.repair_context.get("kind")}
        )
        return {"body": reply(change.draft_body, failures[0]["detail"], len(calls)), "frontmatter": change.draft_meta}

    monkeypatch.setattr(curate, "repair_draft", model_repair)
    return calls


@pytest.mark.asyncio
async def test_a_scope_pass_does_not_spend_the_general_repair_passes(cfg, store, checkout, monkeypatch):
    state = scoped(page_job(cfg, store, checkout, monkeypatch, BASE, DRAFT), store)
    state.changes[0].repair_passes = 2  # the general passes are exhausted (job-d45ed8a3f5)
    state.changes[0].status = "failed"
    store.save(state)
    calls = capture(monkeypatch, lambda body, instruction, n: follow(body, instruction))
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert change.status == "validated", change.validation
    assert change.repair_passes == 2 and change.scope_repair_passes == 1 and len(calls) == 1
    assert calls[0]["kind"] == "scope"
    assert REJ not in change.draft_body and OTHER not in change.draft_body
    assert f"Bursts double it [source: {OK}]." in change.draft_body  # kept on its accepted citation
    assert "A new tier exists" not in change.draft_body and "Batch costs half" not in change.draft_body


@pytest.mark.asyncio
async def test_the_instruction_lists_every_rejected_url_and_every_sentence_citing_it(cfg, store, checkout, monkeypatch):
    state = scoped(page_job(cfg, store, checkout, monkeypatch, BASE, DRAFT), store)
    calls = capture(monkeypatch, lambda body, instruction, n: follow(body, instruction))
    await Pipeline(cfg, store)._stage_validate(state)
    instruction = calls[0]["failures"][0]["detail"]
    assert instruction in calls[0]["topic"]
    assert instruction.endswith(
        "Remove these citations; keep the sentence only if another accepted citation "
        "supports it; never remove base text."
    )
    for url, sentences in {REJ: ["A new tier exists", "Bursts double it"], OTHER: ["Batch costs half"]}.items():
        row = next(line for line in instruction.splitlines() if line.startswith(f"- {url} is cited by:"))
        assert all(sentence in row for sentence in sentences)
    assert "Intro words of this page" not in instruction and "Five hundred per minute" not in instruction  # base text
    assert [f["check"] for f in calls[0]["failures"]] == ["scope"] * len(calls[0]["failures"])


@pytest.mark.asyncio
async def test_scope_passes_are_bounded_at_two_then_the_general_passes_apply(cfg, store, checkout, monkeypatch):
    state = scoped(page_job(cfg, store, checkout, monkeypatch, BASE, DRAFT), store)
    # The model never fixes it and cites a new excluded claim each time (no stall).
    calls = capture(monkeypatch, lambda body, instruction, n: body + f"\nNew claim {n} [source: {REJ}].\n")
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert MAX_SCOPE_REPAIR_PASSES == 2
    assert [call["kind"] for call in calls] == ["scope", "scope", None, None]
    assert all("Scope repair" in call["topic"] for call in calls[:2])
    assert not any("Scope repair" in call["topic"] for call in calls[2:])
    assert change.scope_repair_passes == 2 and change.repair_passes == 2
    assert change.status == "failed"  # the scope validator still blocks it: nothing unsafe publishes


@pytest.mark.asyncio
async def test_a_correction_job_gets_no_scope_pass(cfg, store, checkout, monkeypatch):
    state = scoped(
        page_job(
            cfg,
            store,
            checkout,
            monkeypatch,
            BASE,
            DRAFT,
            plan="Correction: x",
            correction={"claim": "", "instruction": "fix it"},
        ),
        store,
    )
    calls = capture(monkeypatch, lambda body, instruction, n: BASE)
    await Pipeline(cfg, store)._stage_validate(state)
    assert calls[0]["kind"] is None and "Scope repair" not in calls[0]["topic"]
    assert state.changes[0].repair_passes == 1 and state.changes[0].scope_repair_passes == 0


# --------------------------------------------------------------------------- #
# the no-progress guard of the deterministic section/title restore
# --------------------------------------------------------------------------- #

SECTIONS = "# Probe Page\n\nIntro words.\n\n## Notes\n\nNotes words here.\n\n## End\n\nEnd words here.\n"
NO_NOTES = "# Probe Page\n\nIntro words.\n\n## End\n\nEnd words here.\n"


@pytest.mark.asyncio
async def test_a_restore_cycling_between_two_states_ends_the_loop(cfg, store, checkout, monkeypatch):
    import doclayer.curator.pipeline as module

    state = page_job(cfg, store, checkout, monkeypatch, SECTIONS, NO_NOTES)
    flips = [0]

    def cycling(original, body, renames=None):
        flips[0] += 1
        return (NO_NOTES + "Alt A.\n" if "Alt A" not in body else NO_NOTES + "Alt B.\n"), ["Notes"], []

    monkeypatch.setattr(module, "restore_dropped_sections", cycling)
    calls = capture(monkeypatch, lambda body, instruction, n: SECTIONS)
    await Pipeline(cfg, store)._stage_validate(state)
    assert flips[0] <= 4 and len(calls) == 1
    assert calls[0]["body"] in (NO_NOTES + "Alt A.\n", NO_NOTES + "Alt B.\n")  # the cut pass was undone
    assert state.changes[0].status == "validated"


@pytest.mark.asyncio
async def test_a_growing_restore_is_capped_and_the_cut_pass_undone(cfg, store, checkout, monkeypatch):
    import doclayer.curator.pipeline as module

    state = page_job(cfg, store, checkout, monkeypatch, SECTIONS, NO_NOTES)
    grown = [0]

    def growing(original, body, renames=None):
        grown[0] += 1
        return body + f"x{grown[0]}.\n", ["Notes"], []

    monkeypatch.setattr(module, "restore_dropped_sections", growing)
    calls = capture(monkeypatch, lambda body, instruction, n: SECTIONS)
    await Pipeline(cfg, store)._stage_validate(state)
    assert grown[0] <= MAX_DETERMINISTIC_PASSES + 2 and len(calls) == 1
    assert (
        f"x{MAX_DETERMINISTIC_PASSES}." in calls[0]["body"]
        and f"x{MAX_DETERMINISTIC_PASSES + 1}." not in calls[0]["body"]
    )
    assert sum("restored dropped base section" in note for note in state.changes[0].notes) == MAX_DETERMINISTIC_PASSES


@pytest.mark.asyncio
async def test_a_scope_pass_that_changes_nothing_fails_with_an_accurate_message(cfg, store, checkout, monkeypatch):
    state = scoped(page_job(cfg, store, checkout, monkeypatch, BASE, DRAFT), store)
    calls = capture(monkeypatch, lambda body, instruction, n: body)  # the model returns the page unchanged
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert len(calls) == 1 and change.status == "failed"
    assert change.error.startswith(
        "repair-stalled: scope pass 1 left the identical scope blockers; no further pass spent"
    )
    assert change.repair_passes == 0 and change.scope_repair_passes == 1


def test_the_scope_pass_trigger_is_the_typed_failure_code_not_the_message():
    from doclayer.curator import capacity
    from doclayer.curator.validate import Failure

    failures = capacity.scope_failures(POLICY, [OK, REJ], f"# P\n\nNew claim [source: {REJ}].\n", "")
    assert [failure.code for failure in failures] == [capacity.SCOPE_CITATION]
    assert Pipeline._cites_excluded(failures[0])
    reworded = Failure("scope", "a reworded message about an excluded source", code=capacity.SCOPE_CITATION)
    assert Pipeline._cites_excluded(reworded)
    assert not Pipeline._cites_excluded(Failure("scope", failures[0].detail))  # same text, no code
    assert failures[0].as_dict() == {"severity": "blocker", "check": "scope", "detail": failures[0].detail}
