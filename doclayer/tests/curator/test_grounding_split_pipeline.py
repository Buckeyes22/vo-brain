"""The validation loop clears an obsolete "unverifiable under budget, retained" note."""

import pytest
from test_pipeline_capacity import FILLER, PAGE, make_job

from doclayer.curator import curate, editorial, gittools
from doclayer.curator.pipeline import CAPACITY_GENERATION, Pipeline

CANONICAL = "Existing model was benchmarked on internal suites."


@pytest.mark.asyncio
async def test_retained_note_from_pass_one_is_gone_after_pass_two(cfg, store, monkeypatch):
    import doclayer.curator.pipeline as module

    state, records, pipeline, wire = make_job(cfg, store, count=3, assigned=1)
    change = state.changes[0]
    change.status = "drafted"
    change.base_blob = gittools.blob_hash(cfg.paths.brain_checkout, f"{state.base_commit}:{PAGE}")
    change.draft_body = "# Existing model\n\n## Performance\n\n" + FILLER * 5 + "\n"
    change.notes = ["Drafted from the performance guide."]
    state.evidence_selection["drafted:" + PAGE] = {"capacity_generation": CAPACITY_GENERATION}
    state.stage = "validate"
    note = f"{editorial.RETAINED_NOTE}: '{CANONICAL}'"
    passes = []

    async def check(client, topic, change, evidence, *args, **kwargs):
        passes.append(change.draft_body)
        if len(passes) == 1:
            # Pass 1: the canonical sentence is retained, and a new sentence is unsupported.
            return {
                "failures": [
                    {
                        "severity": "blocker",
                        "check": "grounding",
                        "section_id": "s",
                        "anchor": None,
                        "detail": "Unsupported new sentence.",
                    }
                ],
                "conflicts": [],
                "notes": [note],
            }
        return {"failures": [], "conflicts": []}  # pass 2: the repaired draft needs no note

    async def repair(client, topic, change, failures, evidence, corrections, *, char_budget, progress=None):
        return {"frontmatter": change.draft_meta, "body": change.draft_body.replace(FILLER * 5, FILLER * 4)}

    async def preserved(*args, **kwargs):
        return {"failures": [], "conflicts": []}

    monkeypatch.setattr(curate, "check_draft", check)
    monkeypatch.setattr(curate, "repair_draft", repair)
    monkeypatch.setattr(Pipeline, "_preserve_existing", preserved)
    monkeypatch.setattr(Pipeline, "_scope_failures", lambda *args: [])
    monkeypatch.setattr(module, "run_deterministic_checks", lambda *args: [])
    seen = []
    original_persist = Pipeline._persist

    def persist(self, state):
        seen.append(list(state.changes[0].notes))
        return original_persist(self, state)

    monkeypatch.setattr(Pipeline, "_persist", persist)
    await pipeline._stage_validate(state)
    assert len(passes) == 2 and passes[1] != passes[0]  # the loop reviewed the repaired draft again
    assert any(note in notes for notes in seen)  # pass 1 recorded the retained note
    assert change.status == "validated" and change.repair_passes == 1
    assert note not in change.notes and "Drafted from the performance guide." in change.notes  # pass 2 cleared it
    assert wire == []
