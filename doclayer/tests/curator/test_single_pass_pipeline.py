"""Single-pass jobs end to end: one draft call and at most one grounding check
per page, deterministic contribution filtering, model-free publication."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import run_git
from test_pipeline import LONG_BODY, git_show, run_pipeline, submit_payload_job

from doclayer.curator import single_pass
from doclayer.curator.coverage import assess_coverage
from doclayer.curator.ops import handle

URL = "https://docs.test/model"
FOREIGN = "https://evil.test/x"
TARGET = "wiki/existing-model.md"
CLAIM_A = "The model supports a 128,000-token maximum output."
CLAIM_B = "Safety notes now include a 92 percent refusal rate."
# Model tasks of the multi-stage path; a single-pass job never makes them.
MULTI_STAGE_TASKS = {
    "support",
    "source_constraints",
    "relevance",
    "plan",
    "canonical_coverage",
    "check",
    "repair",
    "stale",
    "collision",
    "draft",
}
PAGES = {
    URL: (
        200,
        (
            "<html><head><title>Model X</title></head><body><article><h1>Model X</h1>"
            f"<p>{LONG_BODY}</p><p>{CLAIM_B}</p></article></body></html>"
        ),
    )
}


def extract_reply(*claims: tuple[str, str]) -> str:
    return json.dumps(
        {
            "doc_kind": "model-card",
            "claims": [
                {
                    "claim": text,
                    "locator": "(introduction)",
                    "modality": "quote",
                    "quote": quote,
                    "applicability": {},
                    "important": True,
                    "excluded_topic": False,
                }
                for text, quote in claims
            ],
        }
    )


def two_claims(qwen) -> None:
    """Extraction yields claim A (routed to the page root) and claim B (routed to Safety)."""
    reply = extract_reply((CLAIM_A, "128,000-token maximum output"), (CLAIM_B, "92 percent refusal rate"))
    qwen.overrides["extract"] = [reply] * 6


def sp_job(store, checkout, *, targets=(TARGET,), intent="build test-model knowledge"):
    state = submit_payload_job(store, checkout, urls=[URL], intent=intent)
    state.mode = "single-pass"
    if targets:
        state.evidence_selection["single_pass"] = {"version": 1, "targets": list(targets), "pages": {}}
    store.save(state)
    return state


def frame_of(user: str) -> dict:
    """The request frame; a JSON re-ask appends its repair note after it."""
    return json.JSONDecoder().raw_decode(user.split("\n", 1)[1])[0]


def draft_reply(user: str, sentence=lambda claim, url: f"{claim} [source: {url}]") -> dict:
    """Each claim appended to its routed section as one cited sentence."""
    frame = frame_of(user)
    evidence = {row["id"]: row for row in frame["evidence"]}
    sections = []
    for section in frame["sections"]:
        ids = section["claim_ids"]
        sections.append(
            {
                "section_id": section["section_id"],
                "edits": [],
                "append": "\n\n".join(sentence(evidence[i]["claim"], evidence[i]["url"]) for i in ids),
                "handled_claim_ids": ids,
                "claim_dispositions": [{"id": i, "status": "integrated", "anchor": evidence[i]["claim"]} for i in ids],
            }
        )
    return {"sections": sections, "notes": []}


def tasks(qwen) -> list[str]:
    return [call["task"] for call in qwen.calls]


def page_calls(state) -> dict[str, int]:
    return {path: row["calls"] for path, row in state.evidence_selection["single_pass"]["pages"].items()}


def push_canonical_edit(tmp_path: Path, origin: Path, path: str, old: str, new: str) -> None:
    """An operator edit landing on canonical main (a separate clone pushes it)."""
    clone = tmp_path / f"operator-{len(list(tmp_path.glob('operator-*')))}"
    run_git(tmp_path, "clone", "-q", str(origin), str(clone))
    run_git(clone, "config", "user.email", "op@test.invalid")
    run_git(clone, "config", "user.name", "operator")
    page = clone / path
    text = page.read_text()
    assert old in text
    page.write_text(text.replace(old, new, 1))
    run_git(clone, "commit", "-qam", "operator edit")
    run_git(clone, "push", "-q", "origin", "main")


def test_single_pass_publishes_update_within_call_caps(cfg, store, qwen, checkout, origin, forgejo):
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert state.status == "published", state.summary or state.error
    landed = git_show(origin, f"main:{TARGET}")
    assert CLAIM_A in landed and "Known facts." in landed and "Known safety notes." in landed
    assert page_calls(state) and all(calls <= 3 for calls in page_calls(state).values())
    assert not set(tasks(qwen)) & MULTI_STAGE_TASKS, tasks(qwen)
    assert tasks(qwen).count("sp_draft") == 1 and tasks(qwen).count("sp_ground") == 1
    assert state.usage.calls_by_stage.get("publish", 0) == 0
    assert state.usage.calls_by_stage.get("discover", 0) == 0 and state.usage.calls_by_stage.get("place", 0) == 0
    # sp-quote-1 support is accepted and never discarded by a multi-stage gate.
    assessment = assess_coverage(state)
    assert not [gap for gap in assessment.current_gaps if "placement claim" in gap or "source-support" in gap]
    assert "- **Mode:** single-pass" in state.summary
    assert state.limits["max_model_calls"] == cfg.single_pass.max_model_calls
    assert state.limits["max_repair_passes"] == 0


def test_foreign_citation_never_published(cfg, store, qwen, checkout, origin, forgejo):
    two_claims(qwen)
    qwen.overrides["sp_draft"] = [
        lambda user: draft_reply(
            user,
            lambda claim, url: (
                f"{claim} [source: {url}] Compare [details]({FOREIGN})."
                if claim == CLAIM_B
                else f"{claim} [source: {url}]"
            ),
        )
    ]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)  # grounding: all supported
    assert state.status == "published", state.summary or state.error
    landed = git_show(origin, f"main:{TARGET}")
    assert CLAIM_A in landed and FOREIGN not in landed and CLAIM_B not in landed
    assert any(FOREIGN in gap for gap in state.coverage_gaps)


def test_corrected_claim_not_reinstated(cfg, store, qwen, checkout, origin, forgejo):
    two_claims(qwen)
    store.add_correction({"page": TARGET, "claim": CLAIM_B, "instruction": "the refusal rate is wrong; remove it"})
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert state.status == "published", state.summary or state.error
    landed = git_show(origin, f"main:{TARGET}")
    assert CLAIM_A in landed and CLAIM_B not in landed
    assert any("operator correction" in gap for gap in state.coverage_gaps)


def test_unsupported_contribution_dropped_others_publish(cfg, store, qwen, checkout, origin, forgejo):
    two_claims(qwen)
    qwen.overrides["sp_ground"] = [
        lambda user: {
            "verdicts": [
                {"id": row["id"], "supported": CLAIM_B not in row["text"], "reason": "refusal rate not stated"}
                for row in frame_of(user)["contributions"]
            ]
        }
    ]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert state.status == "published", state.summary or state.error
    landed = git_show(origin, f"main:{TARGET}")
    assert CLAIM_A in landed and CLAIM_B not in landed
    assert any("not grounded: refusal rate not stated" in gap for gap in state.coverage_gaps)
    (change,) = state.changes
    assert change.status == "published" and len(change.claim_ids) == 1


def test_stale_canonical_merges_without_model(cfg, store, qwen, checkout, origin, forgejo, tmp_path):
    def ground_after_operator_edit(user):
        push_canonical_edit(
            tmp_path, origin, TARGET, "Known safety notes.", "Known safety notes, revised by the operator."
        )
        return {
            "verdicts": [
                {"id": row["id"], "supported": True, "reason": "ok"} for row in frame_of(user)["contributions"]
            ]
        }

    qwen.overrides["sp_ground"] = [ground_after_operator_edit]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert state.status == "published", state.summary or state.error
    landed = git_show(origin, f"main:{TARGET}")
    assert CLAIM_A in landed and "revised by the operator" in landed
    assert state.usage.calls_by_stage.get("publish", 0) == 0
    assert any("reconciled at publish" in note for note in state.changes[0].notes)


def test_overlap_keeps_fresh_and_records_superseded(cfg, store, qwen, checkout, origin, forgejo, tmp_path):
    """Claim A's addition overlaps an operator edit made after drafting: the
    fresh text stays, A is recorded as superseded; claim B still publishes."""
    two_claims(qwen)

    def ground_after_operator_edit(user):
        push_canonical_edit(
            tmp_path,
            origin,
            TARGET,
            "# Existing model\n\n",
            "# Existing model\n\nOperator introduction written after drafting.\n\n",
        )
        return {
            "verdicts": [
                {"id": row["id"], "supported": True, "reason": "ok"} for row in frame_of(user)["contributions"]
            ]
        }

    qwen.overrides["sp_ground"] = [ground_after_operator_edit]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert state.status == "published", state.summary or state.error
    landed = git_show(origin, f"main:{TARGET}")
    assert "Operator introduction written after drafting." in landed and CLAIM_A not in landed and CLAIM_B in landed
    (change,) = state.changes
    assert change.superseded_by_fresh and len(change.claim_ids) == 1
    assert any("superseded by fresh publication" in gap for gap in assess_coverage(state).current_gaps)
    assert state.usage.calls_by_stage.get("publish", 0) == 0


@pytest.mark.parametrize(
    "fresh_intro",
    [
        "Operator introduction written after drafting.",
        "Operator introduction written after drafting. [source: https://docs.test/model]",
    ],
)
def test_fully_superseded_page_is_skipped_not_published(
    cfg, store, qwen, checkout, origin, forgejo, tmp_path, fresh_intro
):
    """Every contribution overlaps an operator edit: the page ends skipped with
    a gap, and no frontmatter-only change is published."""

    def ground_after_operator_edit(user):
        push_canonical_edit(tmp_path, origin, TARGET, "# Existing model\n\n", f"# Existing model\n\n{fresh_intro}\n\n")
        return {
            "verdicts": [
                {"id": row["id"], "supported": True, "reason": "ok"} for row in frame_of(user)["contributions"]
            ]
        }

    qwen.overrides["sp_ground"] = [ground_after_operator_edit]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    (change,) = state.changes
    assert change.status == "skipped", (change.status, change.error)
    assert not forgejo.prs
    landed = git_show(origin, f"main:{TARGET}")
    assert CLAIM_A not in landed and "curator:" not in landed
    assert state.status in ("partial", "covered")
    gaps = assess_coverage(state).current_gaps
    assert any("superseded by fresh publication" in gap for gap in gaps)
    assert not any(gap.startswith("unpublished after reconciliation:") for gap in gaps)
    assert state.usage.calls_by_stage.get("publish", 0) == 0


def test_no_targets_uses_one_plan_pages_call(cfg, store, qwen, checkout, origin, forgejo):
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout, targets=()), forgejo)
    assert state.status == "published", state.summary or state.error
    assert tasks(qwen).count("plan_pages") == 1 and "plan" not in tasks(qwen)
    assert [change.path for change in state.changes] == ["wiki/test-model.md"]
    assert CLAIM_A in git_show(origin, "main:wiki/test-model.md")
    assert state.usage.calls_by_stage["place"] == 1


def test_intent_path_is_a_target_without_a_plan_call(cfg, store, qwen, checkout, origin, forgejo):
    state = sp_job(store, checkout, targets=(), intent=f"add the output limit to {TARGET}")
    state = run_pipeline(cfg, store, qwen, PAGES, state, forgejo)
    assert state.status == "published", state.summary or state.error
    assert "plan_pages" not in tasks(qwen) and [change.path for change in state.changes] == [TARGET]


def test_legacy_job_runs_multi_stage_path(cfg, store, qwen, checkout):
    """A state.json saved before the mode field existed loads, and runs, multi-stage."""
    state = submit_payload_job(store, checkout, urls=[URL], intent="build test-model knowledge")
    state.limits["max_model_calls"] = 50
    store.save(state)
    path = store.state_path(state.job_id)
    raw = json.loads(path.read_text())
    raw.pop("mode")
    path.write_text(json.dumps(raw))
    state = run_pipeline(cfg, store, qwen, PAGES, store.load(state.job_id))
    assert state.mode == "multi-stage" and "plan" in tasks(qwen)
    assert not {"sp_draft", "sp_ground", "plan_pages"} & set(tasks(qwen))
    assert "- **Mode:** multi-stage" in state.summary


def test_draft_too_large_marks_every_omitted_claim_unplaced(cfg, store, qwen, checkout, origin, forgejo, monkeypatch):
    two_claims(qwen)

    def too_large(topic, change, meta, units, records, corrections, *, char_budget, **kwargs):
        raise single_pass.DraftTooLarge("no record fits", [record.claim_id for record in records])

    monkeypatch.setattr(single_pass, "draft_request", too_large)
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    (change,) = state.changes
    assert change.status == "failed" and "sp_draft" not in tasks(qwen)
    assert any("single-pass draft: unit dropped (input exceeds bounded context" in gap for gap in state.coverage_gaps)
    unplaced = [gap for gap in state.coverage_gaps if "not integrated (unplaced: draft request budget" in gap]
    assert len(unplaced) == 2
    assert len(state.evidence_selection["single_pass"]["unplaced"]) == 2


def test_partially_omitted_draft_request_publishes_the_rest(cfg, store, qwen, checkout, origin, forgejo, monkeypatch):
    two_claims(qwen)
    real = single_pass.draft_request

    def shed_b(topic, change, meta, units, records, corrections, *, char_budget, **kwargs):
        kept = [record for record in records if record.text != CLAIM_B]
        system, user, kept, _ = real(topic, change, meta, units, kept, corrections, char_budget=char_budget, **kwargs)
        return system, user, kept, [record.claim_id for record in records if record.text == CLAIM_B]

    monkeypatch.setattr(single_pass, "draft_request", shed_b)
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert state.status == "published", state.summary or state.error
    landed = git_show(origin, f"main:{TARGET}")
    assert CLAIM_A in landed and CLAIM_B not in landed
    assert any("not integrated (unplaced: draft request budget" in gap for gap in state.coverage_gaps)


def test_base_text_fault_drops_the_one_contribution(cfg, store, qwen, checkout, origin, forgejo):
    """An insertion mid-sentence passes insertion_only but loses a base sentence;
    the contribution behind it is dropped and the rest publishes."""
    two_claims(qwen)

    def reply(user):
        result = draft_reply(user)
        safety = next(entry for entry in result["sections"] if CLAIM_B in entry["append"])
        safety["append"] = ""
        safety["edits"] = [{"old": "Known safety", "new": f"Known safety {CLAIM_B} [source: {URL}]"}]
        return result

    qwen.overrides["sp_draft"] = [reply]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert state.status == "published", state.summary or state.error
    landed = git_show(origin, f"main:{TARGET}")
    assert CLAIM_A in landed and CLAIM_B not in landed and "Known safety notes." in landed
    assert any("deterministic check: base-text" in gap for gap in state.coverage_gaps)


def test_blocker_left_after_the_drop_round_isolates_the_page(cfg, store, qwen, checkout, origin, forgejo):
    two_claims(qwen)

    def reply(user):
        result = draft_reply(
            user,
            lambda claim, url: (
                f"{claim} [source: {url}] See [[wiki/missing/page.md]]."
                if claim == CLAIM_A
                else f"{claim} [source: {url}]"
            ),
        )
        safety = next(entry for entry in result["sections"] if CLAIM_B in entry["append"])
        safety["append"] = ""
        safety["edits"] = [{"old": "Known safety", "new": f"Known safety {CLAIM_B} [source: {URL}]"}]
        return result

    qwen.overrides["sp_draft"] = [reply]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    (change,) = state.changes
    assert change.status == "failed" and "links" in change.error
    assert state.status == "failed" and not forgejo.prs
    assert any(gap.startswith(f"{TARGET}: editorial review unresolved") for gap in state.coverage_gaps)


def test_single_pass_continuation_gets_single_pass_setup(cfg, store, qwen, checkout, origin, forgejo):
    parent = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert parent.status == "published"
    parent.evidence_selection["placement"] = {"ledger": {"claims": {}, "version": 1}}
    store.save(parent)
    child = store.load(handle("continue", {"job_id": parent.job_id}, cfg, store)["job_id"])
    assert child.mode == "single-pass"
    assert child.evidence_selection["single_pass"] == {
        "version": 1,
        "targets": [TARGET],
        "pages": {},
        "published_claims": {identity: TARGET for identity in parent.changes[0].claim_ids},
    }
    assert "placement" not in child.evidence_selection

    parent.mode = "multi-stage"
    store.save(parent)
    child = store.load(handle("continue", {"job_id": parent.job_id, "mode": "single-pass"}, cfg, store)["job_id"])
    assert child.mode == "single-pass" and child.evidence_selection["single_pass"]["pages"] == {}
    assert "placement" not in child.evidence_selection
    legacy = store.load(
        handle("continue", {"job_id": parent.job_id, "idempotency_key": "legacy-child"}, cfg, store)["job_id"]
    )
    assert legacy.mode == "multi-stage" and "single_pass" not in legacy.evidence_selection
    assert legacy.evidence_selection["placement"]["ledger"]


def test_page_without_current_claims_spends_no_draft_call(cfg, store, qwen, checkout, origin, forgejo, monkeypatch):
    original = single_pass.STAGE_HANDLERS["place"]

    async def place_then_lose_support(p, state):
        result = await original(p, state)
        state.changes[0].claim_ids = ["claim-000000000000dead"]
        return result

    monkeypatch.setitem(single_pass.STAGE_HANDLERS, "place", place_then_lose_support)
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    (change,) = state.changes
    assert change.status == "skipped" and "sp_draft" not in tasks(qwen)
    assert any(
        "claim claim-000000000000dead not integrated (unplaced: source support no longer current" in gap
        for gap in state.coverage_gaps
    )
    assert state.status in ("covered", "partial") and not forgejo.prs


def test_planned_page_outside_curator_destinations_is_never_created(cfg, store, qwen, checkout, origin, forgejo):
    qwen.overrides["plan_pages"] = [
        json.dumps(
            {
                "already_covered": False,
                "explanation": "",
                "pages": [
                    {"action": "create", "path": path, "title": "X", "subject_type": "model", "rationale": "r"}
                    for path in ("log.md", "index.md", "../escape.md", "doclayer/recipes/x.md")
                ],
            }
        )
    ]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout, targets=()), forgejo)
    assert state.changes == [] and not forgejo.prs
    assert any("not integrated (unplaced: no target page)" in gap for gap in state.coverage_gaps)


def test_model_chosen_title_with_links_falls_back_to_the_slug(cfg, store, qwen, checkout, origin, forgejo):
    qwen.overrides["plan_pages"] = [
        json.dumps(
            {
                "already_covered": False,
                "explanation": "",
                "pages": [
                    {
                        "action": "create",
                        "path": "wiki/new-thing.md",
                        "subject_type": "model",
                        "rationale": "r",
                        "title": "Model [docs](https://evil.test/x) overview www.evil.test/y",
                    }
                ],
            }
        )
    ]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout, targets=()), forgejo)
    assert state.status == "published", state.summary or state.error
    landed = git_show(origin, "main:wiki/new-thing.md")
    assert "evil.test" not in landed and "# New thing\n" in landed and "title: New thing" in landed


def test_unsafe_title_strings_are_replaced():
    assert single_pass.safe_title("Model [docs](https://evil.test/x)", "wiki/new-thing.md") == "New thing"
    assert single_pass.safe_title("see evil.test/y", "wiki/new_thing.md") == "New thing"
    assert single_pass.safe_title("<b>x</b>", "wiki/a-b.md") == "A b"
    assert single_pass.safe_title("Claude Opus 5 (API)", "wiki/x.md") == "Claude Opus 5 (API)"


def test_two_bad_draft_replies_fail_the_page_at_two_calls(cfg, store, qwen, checkout, origin, forgejo):
    qwen.overrides["sp_draft"] = ["not JSON", "still not JSON"]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    (change,) = state.changes
    assert change.status == "failed" and page_calls(state) == {TARGET: 2}
    assert "sp_ground" not in tasks(qwen) and not forgejo.prs
    assert any(
        gap.startswith(f"{TARGET}: single-pass draft: unit dropped (model reply rejected")
        for gap in state.coverage_gaps
    )


def test_draft_that_deletes_or_renames_keeps_the_base_and_records_gaps(cfg, store, qwen, checkout, origin, forgejo):
    two_claims(qwen)

    def reply(user):
        result = draft_reply(user)
        for entry in result["sections"]:
            if CLAIM_A in entry["append"]:
                entry["edits"] = [{"old": "# Existing model", "new": "# Model"}]  # an H1 rename
            else:
                entry["edits"] = [
                    {"old": "Known safety notes.", "new": ""},  # a base-sentence deletion
                    {"old": "## Safety", "new": "## Safety notes"},
                ]  # a heading rename
        return result

    qwen.overrides["sp_draft"] = [reply]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert state.status == "published", state.summary or state.error
    landed = git_show(origin, f"main:{TARGET}")
    assert "# Existing model\n" in landed and "## Safety\n" in landed and "Known safety notes." in landed
    assert "## Safety notes" not in landed and CLAIM_A in landed
    assert any("single-pass edit" in gap and "remove or reword" in gap for gap in state.coverage_gaps)
    # The rejected section is re-asked once with its validation error; the
    # repair's insertion-only entry replaces it, so claim B lands too.
    assert tasks(qwen).count("sp_draft") == 2 and CLAIM_B in landed


def test_model_reask_in_placement_fails_closed(cfg, store, qwen, checkout, origin, forgejo):
    qwen.overrides["plan_pages"] = ["not JSON"]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout, targets=()), forgejo)
    assert tasks(qwen).count("plan_pages") == 1 and state.changes == []
    assert state.evidence_selection["single_pass"]["place"]["calls"] == 1
    assert any(
        gap.startswith("single-pass placement plan: unit dropped") and "page call cap" in gap
        for gap in state.coverage_gaps
    )


def test_stale_merge_onto_a_page_citing_the_same_source_publishes(
    cfg, store, qwen, checkout, origin, forgejo, tmp_path
):
    def ground_after_operator_edit(user):
        push_canonical_edit(
            tmp_path, origin, TARGET, "Known safety notes.", f"Known safety notes, revised. [source: {URL}]"
        )
        return {
            "verdicts": [
                {"id": row["id"], "supported": True, "reason": "ok"} for row in frame_of(user)["contributions"]
            ]
        }

    qwen.overrides["sp_ground"] = [ground_after_operator_edit]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert state.status == "published", (state.changes[0].status, state.changes[0].error)
    landed = git_show(origin, f"main:{TARGET}")
    assert CLAIM_A in landed and "revised. [source:" in landed


def test_correction_differing_only_by_case_drops_just_that_contribution(cfg, store, qwen, checkout, origin, forgejo):
    two_claims(qwen)
    store.add_correction({"page": TARGET, "claim": CLAIM_B.upper(), "instruction": "wrong; remove it"})
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert state.status == "published", (state.changes[0].status, state.changes[0].error)
    landed = git_show(origin, f"main:{TARGET}")
    assert CLAIM_A in landed and CLAIM_B not in landed


def test_continuation_with_no_new_material_drafts_nothing(cfg, store, qwen, checkout, origin, forgejo):
    parent = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert parent.status == "published"
    qwen.calls.clear()
    child = store.load(handle("continue", {"job_id": parent.job_id, "discover": False}, cfg, store)["job_id"])
    child = run_pipeline(cfg, store, qwen, PAGES, child, forgejo)
    assert "sp_draft" not in tasks(qwen) and "plan_pages" not in tasks(qwen)
    assert child.changes == [] and child.status == "covered", child.summary
    assert not [gap for gap in child.coverage_gaps if "not integrated" in gap]
    assert set(child.evidence_selection["single_pass"]["published_claims"]) == set(parent.changes[0].claim_ids)
