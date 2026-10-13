"""Validate-stage repair never dies on an unproven claim (live failure).

A continuation went retryable at VALIDATE with "bounded edit
already_present sentence differs from the claim". The scope-repair redraft of
wiki/gpt-6-astra.md (curate.repair_draft -> editing.edit_page, repair=True)
dispatched claim-095ebd03793eb6af alone to "## Rate Limits". The reply cited
the whole 322-character rate-limit table as already_present (edit_count 0).
The claim states the same numbers as "500K TPM / 1.5M batch queue", the table
as "500,000 | 1,500,000", so the number check read them as different values.
With one claim in the task, a downgrade had nothing left to accept, so the
bounded retries raised and the whole job stopped.
"""

import json

import pytest

from doclayer.curator import curate, editing
from doclayer.curator.state import Evidence, PageChange

MODEL = "https://developers.openai.com/api/docs/models/gpt-6-astra"
EXCLUDED = "https://developers.openai.com/api/docs/guides/model-selection"
LIVE_CLAIM = (
    "Rate limits by tier: Free not supported; Tier 1: 500 RPM / 500K TPM / 1.5M batch queue; "
    "Tier 2: 5,000 RPM / 1M TPM / 3M batch queue; Tier 3: 5,000 RPM / 2M TPM / 100M batch queue; "
    "Tier 4: 10,000 RPM / 4M TPM / 200M batch queue; Tier 5: 15,000 RPM / 40M TPM / 15B batch queue."
)
TABLE = (
    "| Tier | RPM | TPM | Batch queue limit |\n| --- | --- | --- | --- |\n| Free | Not supported | — | — |\n"
    "| Tier 1 | 500 | 500,000 | 1,500,000 |\n| Tier 2 | 5,000 | 1,000,000 | 3,000,000 |\n"
    "| Tier 3 | 5,000 | 2,000,000 | 100,000,000 |\n| Tier 4 | 10,000 | 4,000,000 | 200,000,000 |\n"
    "| Tier 5 | 15,000 | 40,000,000 | 15,000,000,000 |"
)
INTRO = "Rate limits by tier for GPT-6 Astra [source: " + MODEL + "] [source: " + EXCLUDED + "]:"
PAGE = (
    "# GPT-6 Astra\n\n## Rate Limits\n\n" + INTRO + "\n\n" + TABLE + "\n\n## Notes\n\n" + "Operator fact. " * 400 + "\n"
)


def record(text):
    return Evidence(
        "src-65c5c61b5f", MODEL, "#Rate limits", "paraphrase", text, url=MODEL, quote=text, support_status="supported"
    )


LOWER_TABLE = TABLE.replace("Batch queue limit", "batch queue limit")
LOWER_PAGE = PAGE.replace(TABLE, LOWER_TABLE)


def test_live_table_states_the_live_claim():
    assert len(TABLE) == 322
    # Header labels ("Batch") compare case-insensitively; unit tokens after a number do not.
    assert editing._claim_mismatch(TABLE, LIVE_CLAIM) is None
    section = "## Rate Limits\n\n" + INTRO + "\n\n" + TABLE + "\n"
    assert editing._prior_anchor(TABLE, section, section, LIVE_CLAIM) == "exact"


@pytest.mark.parametrize(
    "left,right,differs",
    [
        ("| Tier | Batch |\n| --- | --- |\n| x | 3 |", "x: 3 batch", False),  # a header label ignores case
        ("| Tier | BATCH |\n| --- | --- |\n| x | 3 |", "x: 3 Batches", False),
        (
            "| Model | Gb |\n| --- | --- |\n| x | 2 |",
            "Model x has 2 gb.",
            True,
        ),  # a unit token keeps its case, also in a header
        ("| Model | Gb |\n| --- | --- |\n| x | 2 |", "Model x has 2 Gb.", False),
        ("| Tier | Batch |\n| --- | --- |\n| x | 3 |", "x: 3 queue", True),  # still a different word
        ("| Tier | Batch |\n| --- | --- |\n| x | 3 |", "x: 4 batch", True),
        ("100 Mb/s", "100 MB/s", True),  # a unit token after a number stays case-sensitive
        ("2 Gb of memory", "2 GB of memory", True),
        ("3 Batch", "3 batch", True),
    ],
)
def test_header_labels_ignore_case_but_unit_tokens_do_not(left, right, differs):
    assert editing._numbers_differ(left, right) is differs


@pytest.mark.parametrize(
    "left,right,differs",
    [
        ("500K", "500,000", False),
        ("500K TPM", "500,000 TPM", False),
        ("1.5M", "1,500,000", False),
        ("1.5M batch queue", "1,500,000 batch queue", False),
        ("1.5M batch queue", "1,500,000", True),  # the unit word differs
        ("15B", "15,000,000,000", False),
        ("2 T", "2,000,000,000,000", False),
        ("500 RPM", "| RPM |\n| --- |\n| 500 |", False),
        ("500 RPM", "| 500 |", True),  # no header names the unit
        ("90 days", "90 days", False),
        ("90 days", "90-day", False),
        ("1 day", "1 days", False),  # singular and plural are one unit
        ("90 days", "90 hours", True),
        ("90 days", "90%", True),
        ("90 days", "90", True),
        ("10ms", "10 ms", False),
        ("Tier 1: 500 RPM", "| Tier | RPM |\n| --- | --- |\n| Tier 1 | 500 |", False),
        ("H100 GPUs", "H200 GPUs", True),  # letters and digits are one token
        ("Q3 release", "Q4 release", True),
        ("v2", "v3", True),
        ("1.500M tokens", "1.500B tokens", True),
        ("1.500K", "1.500", True),
        ("1,5M", "1,5B", True),  # locale form: compared as written
        ("15 B parameters", "15,000,000,000 parameters", True),  # in doubt, not expanded
        ("500B tokens", "500 billion tokens", True),  # number words are not normalised
        ("2 million", "2,000,000", True),
        ("5m", "5,000,000", True),  # lowercase is never a magnitude
        ("5 m", "5,000,000", True),
        ("500B bytes", "500,000,000,000 bytes", True),  # byte word follows: not expanded
        ("50% discount", "50 seats", True),
        ("50%", "50 %", True),
        ("3x faster", "3 faster", True),
        ("3 x faster", "3 faster", True),
        ("3× faster", "3x faster", False),
        ("1.500 tokens", "1.5 tokens", True),
        ("1.500 tokens", "1500 tokens", True),
        ("1.50", "1.5", True),
        ("Requires Python 3.12.1 or later.", "Requires Python 3.12.1 or later.", False),
        ("Requires Python 3.12.1 or later.", "Requires Python 3.12.2 or later.", True),
        ("Host 192.0.2.1 only", "Host 192.0.2.2 only", True),
        ("Released 2026.10.06", "Released 2026.10.06", False),
        ("1" * 30, "1" * 30, False),
        ("1" * 30 + "K", "1" * 30 + "000", True),  # beyond 28 digits: never expanded
        ("500K TPM", "600,000", True),
        ("1.5M", "1,500", True),
    ],
)
def test_number_values_ignore_notation_but_not_value_or_unit(left, right, differs):
    assert editing._numbers_differ(left, right) is differs


class RepairClient:
    """Scope-repair editor: removes the excluded citation and cites the table."""

    def __init__(self, claims, cite):
        self.claims, self.cite, self.edit_users = claims, cite, []

    async def chat_json(self, system, user):
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
            section = next(row for row in frame["sections"] if row["heading_path"].endswith("/ Rate Limits"))
            ids = [line.split("[", 1)[1].split(" ", 1)[0] for line in user.splitlines() if line.startswith("- [claim-")]
            return {
                "routes": [
                    {
                        "id": identity,
                        "section_id": section["section_id"],
                        "new_heading": None,
                        "rationale": "rate limits",
                    }
                    for identity in ids
                ]
            }
        self.edit_users.append(user)
        section_id = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
        listed = json.loads(
            user.split("Claim IDs to integrate or prove already present: ", 1)[1].split("\n", 1)[0].replace("'", '"')
        )
        view = user.split("Current section view:\n", 1)[1]
        edits = [{"old": " [source: " + EXCLUDED + "]", "new": ""}] if "[source: " + EXCLUDED + "]" in view else []
        dispositions = []
        for identity in listed:
            if self.claims[identity] == "table":
                dispositions.append({"id": identity, "status": "already_present", "anchor": self.cite})
            else:
                dispositions.append({"id": identity, "status": "already_present", "anchor": self.claims[identity]})
        if not listed:
            dispositions = []
        return {
            "section_id": section_id,
            "edits": edits,
            "append": "",
            "notes": [],
            "handled_claim_ids": listed,
            "claim_dispositions": dispositions,
        }


def listed(user):
    return user.split("Claim IDs to integrate or prove already present: ", 1)[1].split("\n", 1)[0]


def repair_change(records, page=None):
    return PageChange(
        "g",
        "wiki/gpt-6-astra.md",
        "update",
        title="GPT-6 Astra",
        draft_body=page or PAGE,
        draft_meta={"concept_id": "c-1", "updated": "2026-10-06"},
        claim_ids=[curate.evidence_id(item) for item in records],
    )


SCOPE = [
    {
        "severity": "blocker",
        "check": "scope",
        "detail": "Scope repair: " + EXCLUDED + " is excluded; remove the citation.",
    }
]


@pytest.mark.asyncio
async def test_validate_repair_replay_proves_the_table():
    live = record(LIVE_CLAIM)
    identity = curate.evidence_id(live)
    client = RepairClient({identity: "table"}, cite=TABLE)
    result = await curate.repair_draft(
        client, "GPT-6 model differences", repair_change([live]), SCOPE, [live], [], char_budget=12000
    )
    assert identity in result["draft_progress"]["handled_claim_ids"]
    assert "not_integrated" not in result["draft_progress"]
    assert EXCLUDED not in result["body"] and result["body"].count(TABLE) == 1


@pytest.mark.asyncio
async def test_replay_with_matching_units_proves_the_table_and_lands_the_scope_edit():
    live = record(LIVE_CLAIM)
    identity = curate.evidence_id(live)
    client = RepairClient({identity: "table"}, cite=LOWER_TABLE)
    result = await curate.repair_draft(
        client, "GPT-6 model differences", repair_change([live], LOWER_PAGE), SCOPE, [live], [], char_budget=12000
    )
    assert identity in result["draft_progress"]["handled_claim_ids"]
    assert "not_integrated" not in result["draft_progress"]
    assert EXCLUDED not in result["body"] and LOWER_TABLE in result["body"]


@pytest.mark.asyncio
async def test_validate_repair_records_an_unprovable_claim_and_keeps_the_page_going():
    wrong = record("Tier 1 allows 600 RPM.")  # the table says 500: a real mismatch
    right = record(LIVE_CLAIM)
    ids = [curate.evidence_id(wrong), curate.evidence_id(right)]
    client = RepairClient({ids[0]: "table", ids[1]: "table"}, cite=LOWER_TABLE)
    progress = []
    result = await curate.repair_draft(
        client,
        "GPT-6 model differences",
        repair_change([wrong, right], LOWER_PAGE),
        SCOPE,
        [wrong, right],
        [],
        char_budget=12000,
        progress=progress.append,
    )
    draft = result["draft_progress"]
    assert ids[1] in draft["handled_claim_ids"] and ids[0] not in draft["handled_claim_ids"]
    assert draft["not_integrated"][ids[0]]["reason"] == "mismatch"
    assert EXCLUDED not in result["body"]  # the repair edit itself still landed
    assert result["body"].count(LOWER_TABLE) == 1 and "600" not in result["body"]
    # one partial reply, then the downgraded claim's follow-up, bounded by the same-failure guard
    claim_calls = [user for user in client.edit_users if ids[0] in listed(user)]
    assert len(claim_calls) == 3
    follow_up = claim_calls[1]
    assert "Previous reply left these claims unhandled: claim " + ids[0] in follow_up
    assert any("not integrated" in note for note in result["notes"])


@pytest.mark.asyncio
async def test_single_unprovable_claim_task_is_recorded_not_raised_and_not_redispatched():
    wrong = record("Tier 1 allows 600 RPM.")
    identity = curate.evidence_id(wrong)
    client = RepairClient({identity: "table"}, cite=TABLE)
    change = repair_change([wrong])
    result = await curate.repair_draft(client, "GPT-6 model differences", change, SCOPE, [wrong], [], char_budget=12000)
    assert result["draft_progress"]["not_integrated"][identity] == {
        "section_id": next(unit.id for unit in editing.page_units(PAGE) if unit.label.endswith("/ Rate Limits")),
        "reason": "mismatch",
    }
    # first try, then the same deterministic failure ends it
    assert len([user for user in client.edit_users if identity in listed(user)]) == 2
    assert EXCLUDED in result["body"]  # nothing of an all-unproven reply lands; the next repair pass retries it
    # A resume over the same progress does not spend the claim's attempts again.
    change.draft_body = result["body"]
    change.draft_progress = result["draft_progress"]
    calls = len(client.edit_users)
    again = await curate.repair_draft(client, "GPT-6 model differences", change, SCOPE, [wrong], [], char_budget=12000)
    assert again["draft_progress"]["not_integrated"] == result["draft_progress"]["not_integrated"]
    assert all(identity not in listed(user) for user in client.edit_users[calls:])


def test_pipeline_records_not_integrated_claims_as_coverage_gaps():
    from types import SimpleNamespace

    from doclayer.curator.pipeline import Pipeline

    state = SimpleNamespace(coverage_gaps=[])
    change = PageChange(
        "g",
        "wiki/gpt-6-astra.md",
        "update",
        draft_progress={"not_integrated": {"claim-095ebd03793eb6af": {"section_id": "s", "reason": "mismatch"}}},
    )
    Pipeline._record_not_integrated(state, change)
    Pipeline._record_not_integrated(state, change)
    assert state.coverage_gaps == ["wiki/gpt-6-astra.md: claim claim-095ebd03793eb6af not integrated (mismatch)"]


# --------------------------------------------------------------------------- #
# Draft stage, 176 calls, "bounded edit lacks unique
# actual claim anchor". A single-claim follow-up (left over by an earlier
# partial reply) for claim-0011ceb8793bf62d in "## Zero Data Retention (ZDR)
# and Modified Abuse Monitoring (MAM)", edit_count 0: an already_present
# anchor of 219 characters with 0 occurrences in the section. The section's
# sentence is exactly 219 characters but writes "OpenAI’s"; the reply wrote
# "OpenAI's". With nothing left to accept, the follow-up raised.
# --------------------------------------------------------------------------- #

DATA = "https://developers.openai.com/api/docs/guides/your-data"
C2_SENTENCE = (
    "Customers enabling these controls remain responsible for ensuring user compliance with OpenAI’s safety "
    "policies and applicable moderation/reporting laws [source: " + DATA + "]."
)
C2_ANCHOR = C2_SENTENCE.replace("’", "'")
C2_CLAIM = (
    "Customers enabling MAM or ZDR are responsible for ensuring their users abide by OpenAI's safe and responsible "
    "use policies and comply with moderation and reporting requirements under applicable law."
)
C2_PAGE = (
    "# OpenAI API Data Retention Controls\n\n## Zero Data Retention (ZDR) and Modified Abuse Monitoring (MAM)\n\n"
    "Eligible customers may exclude customer content from abuse monitoring logs by obtaining approval for ZDR or MAM. "
    + C2_SENTENCE
    + "\n\n### Modified Abuse Monitoring (MAM)\n\nMAM text.\n"
)


def zdr_section(page=C2_PAGE):
    return next(
        unit
        for unit in editing.page_units(page)
        if unit.label.endswith("/ Zero Data Retention (ZDR) and Modified Abuse Monitoring (MAM)")
    )


def test_typographic_apostrophe_anchor_is_proven():
    assert len(C2_ANCHOR) == 219 and C2_ANCHOR not in C2_PAGE
    unit = zdr_section()
    reply = {
        "section_id": unit.id,
        "edits": [],
        "append": "",
        "notes": [],
        "handled_claim_ids": ["claim-0011ceb8793bf62d"],
        "claim_dispositions": [{"id": "claim-0011ceb8793bf62d", "status": "already_present", "anchor": C2_ANCHOR}],
    }
    assert (
        editing.apply_edits(
            unit,
            reply,
            {"claim-0011ceb8793bf62d"},
            max_output_chars=6000,
            allow_partial=True,
            claims={"claim-0011ceb8793bf62d": C2_CLAIM},
        )
        == unit.text
    )


class DraftEditor:
    """Draft editor whose reply for one claim never proves it."""

    def __init__(self, replies):
        self.replies, self.users = replies, []

    async def chat_json(self, system, user):
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
            section = next(
                row
                for row in frame["sections"]
                if row["heading_path"].endswith("(MAM)") and "Zero Data" in row["heading_path"]
            )
            ids = [line.split("[", 1)[1].split(" ", 1)[0] for line in user.splitlines() if line.startswith("- [claim-")]
            return {
                "routes": [
                    {
                        "id": identity,
                        "section_id": section["section_id"],
                        "new_heading": None,
                        "rationale": "joint controls",
                    }
                    for identity in ids
                ]
            }
        self.users.append(user)
        section_id = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
        ids = json.loads(listed(user).replace("'", '"'))
        append, dispositions = "", []
        for identity in ids:
            text, row = self.replies[identity]
            append += text
            dispositions.append({"id": identity, **row})
        return {
            "section_id": section_id,
            "edits": [],
            "append": append,
            "notes": [],
            "handled_claim_ids": ids,
            "claim_dispositions": dispositions,
        }


def c2_record(text):
    return Evidence("src", DATA, "#Data", "paraphrase", text, url=DATA, quote=text, support_status="supported")


@pytest.mark.asyncio
async def test_draft_single_claim_follow_up_records_gap_instead_of_raising():
    bad, good = c2_record("Absent fact about retention windows."), c2_record("Exports are encrypted at rest.")
    ids = [curate.evidence_id(bad), curate.evidence_id(good)]
    client = DraftEditor(
        {
            ids[0]: ("", {"status": "already_present", "anchor": "This sentence exists nowhere at all."}),
            ids[1]: (
                "\nExports are encrypted at rest.",
                {"status": "integrated", "anchor": "Exports are encrypted at rest."},
            ),
        }
    )
    change = PageChange(
        "g", "wiki/openai-api-data-retention-controls.md", "update", title="Data retention", claim_ids=ids
    )
    result = await editing.edit_page(
        client, "Data retention", change, [bad, good], C2_PAGE, [], char_budget=16000, today="2026-10-06"
    )
    assert set(result["draft_progress"]["handled_claim_ids"]) == {ids[1]}
    assert result["draft_progress"]["not_integrated"][ids[0]]["reason"] == "absent"
    assert "Exports are encrypted at rest." in result["body"]
    assert len([user for user in client.users if ids[0] in listed(user)]) == 3  # partial, follow-up, one retry
    assert result["body"].count("Exports are encrypted at rest.") == 1


@pytest.mark.parametrize(
    "append,anchor,accepted",
    [
        ("**Exports** are encrypted at rest.", "Exports are encrypted at rest.", True),  # unique after normalisation
        (
            "Exports are encrypted at rest. Exports are encrypted at rest.",
            "Exports are encrypted at rest.",
            False,
        ),  # twice added
        ("Backups are kept.", "Eligible customers may exclude customer content", False),  # old material only
    ],
)
def test_integrated_anchor_must_be_unique_in_added_material(append, anchor, accepted):
    unit = zdr_section()
    reply = {
        "section_id": unit.id,
        "edits": [],
        "append": append + "\n\nSeoul storage requires MAM.",
        "notes": [],
        "handled_claim_ids": ["a", "b"],
        "claim_dispositions": [
            {"id": "a", "status": "integrated", "anchor": anchor},
            {"id": "b", "status": "integrated", "anchor": "Seoul storage requires MAM."},
        ],
    }
    editing.apply_edits(unit, reply, {"a", "b"}, max_output_chars=6000, allow_partial=True)
    assert ("a" in reply["handled_claim_ids"]) is accepted
    if not accepted:
        feedback = editing._downgrade_feedback(reply["_downgraded"])
        assert "claim a: the previous integrated anchor" in feedback
        assert "times in the material your edit added or changed" in feedback


@pytest.mark.asyncio
async def test_draft_unprovable_integrated_claim_is_bounded_then_recorded():
    bad, good = c2_record("Exports are encrypted at rest."), c2_record("Backups are kept for 7 days.")
    ids = [curate.evidence_id(bad), curate.evidence_id(good)]
    client = DraftEditor(
        {
            ids[0]: (
                "\nExports are encrypted. Exports are encrypted.",
                {"status": "integrated", "anchor": "Exports are encrypted."},
            ),
            ids[1]: (
                "\nBackups are kept for 7 days.",
                {"status": "integrated", "anchor": "Backups are kept for 7 days."},
            ),
        }
    )
    change = PageChange(
        "g", "wiki/openai-api-data-retention-controls.md", "update", title="Data retention", claim_ids=ids
    )
    result = await editing.edit_page(
        client, "Data retention", change, [bad, good], C2_PAGE, [], char_budget=16000, today="2026-10-06"
    )
    # Both claims' text shares one appended paragraph: it is dropped whole, the
    # unprovable claim becomes a gap and the proven one is re-dispatched alone.
    assert set(result["draft_progress"]["handled_claim_ids"]) == {ids[1]}
    assert result["draft_progress"]["not_integrated"] == {
        ids[0]: {"section_id": zdr_section().id, "reason": "not-unique"}
    }
    follow_up = next(user for user in client.users if listed(user) == str([ids[1]]))
    assert (
        "claim " + ids[1] + ': the previous integrated anchor "Backups are kept for 7 days." was not accepted because '
        "its text shared an edit with text of a claim that was not proven"
    ) in follow_up
    assert result["body"].count("Exports are encrypted") == 0
    assert result["body"].count("Backups are kept for 7 days.") == 1


# --------------------------------------------------------------------------- #
# Coverage, proven-only material, quotes
# --------------------------------------------------------------------------- #


def coverage_job(store, change):
    from doclayer.curator.state import JobState, SourceRecord

    state = store.create(JobState(job_id="", idempotency_key="", intent="GPT-6 model differences"))[0]
    state.sources = [
        SourceRecord(
            "manual",
            "text",
            "Rate limits",
            status="fetched",
            extraction_complete=True,
            review_unit_count=1,
            reviewed_sections=["Complete source unit"],
        )
    ]
    state.changes = [change]
    state.evidence_selection = {
        "placement": {
            "ledger": {
                "claims": {identity: {"status": "planned", "target": change.path} for identity in change.claim_ids}
            }
        }
    }
    return state


def test_not_integrated_claim_is_a_current_gap_and_never_published(store):
    from doclayer.curator.coverage import assess_coverage
    from doclayer.curator.pipeline import Pipeline

    missing, kept = "claim-095ebd03793eb6af", "claim-0de22c0e7802511a"
    change = PageChange(
        "g",
        "wiki/gpt-6-astra.md",
        "update",
        claim_ids=[missing, kept],
        draft_progress={"not_integrated": {missing: {"section_id": "s", "reason": "mismatch"}}},
    )
    state = coverage_job(store, change)
    Pipeline._record_not_integrated(state, change)
    assert change.claim_ids == [kept]
    change.status = "published"
    store.save(state)
    assessment = assess_coverage(store.load(state.job_id))
    gap = "wiki/gpt-6-astra.md: claim " + missing + " not integrated (mismatch)"
    assert assessment.coverage == "incomplete"
    assert gap in assessment.current_gaps and gap not in assessment.historical_gaps
    assert "placement claim " + missing + ": assigned page not published" in assessment.current_gaps
    assert not any(kept in item for item in assessment.current_gaps)
    # The recorded note alone (progress since reset by a redraft) stays a current gap.
    state.changes[0].draft_progress = {}
    store.save(state)
    assert gap in assess_coverage(store.load(state.job_id)).current_gaps


def test_quote_variants_fold():
    assert editing.normalised_text("OpenAIʼs „x“ «y» ‹z›") == 'OpenAI\'s "x" "y" \'z\''
    assert editing.normalised_text("‚x‘ ‛y’ ‟z″") == "'x' 'y' \"z\""
    assert editing._polarity("Customers canʼt opt out") == {"not"}


@pytest.mark.asyncio
async def test_mixed_reply_publishes_only_the_proven_claims_material():
    bad, good = c2_record("Exports are encrypted at rest."), c2_record("Backups are kept for 7 days.")
    ids = [curate.evidence_id(bad), curate.evidence_id(good)]
    client = DraftEditor(
        {
            ids[0]: (
                "\nExports are encrypted. Exports are encrypted.",
                {"status": "integrated", "anchor": "Exports are encrypted."},
            ),
            ids[1]: (
                "\nBackups are kept for 7 days.",
                {"status": "integrated", "anchor": "Backups are kept for 7 days."},
            ),
        }
    )
    change = PageChange(
        "g", "wiki/openai-api-data-retention-controls.md", "update", title="Data retention", claim_ids=ids
    )
    result = await editing.edit_page(
        client, "Data retention", change, [bad, good], C2_PAGE, [], char_budget=16000, today="2026-10-06"
    )
    assert result["body"].count("Exports are encrypted") == 0  # nothing unproven, no duplicates
    assert result["body"].count("Backups are kept for 7 days.") == 1
    assert result["draft_progress"]["not_integrated"][ids[0]]["reason"] == "not-unique"


def test_proven_claim_sharing_an_edit_with_unproven_text_is_not_handled():
    unit = zdr_section()
    reply = {
        "section_id": unit.id,
        "edits": [],
        "notes": [],
        "handled_claim_ids": ["a", "b"],
        "append": "Backups are kept for 7 days. Exports are encrypted. Exports are encrypted.",
        "claim_dispositions": [
            {"id": "a", "status": "integrated", "anchor": "Exports are encrypted."},
            {"id": "b", "status": "integrated", "anchor": "Backups are kept for 7 days."},
        ],
    }
    with pytest.raises(editing.ModelError) as caught:
        editing.apply_edits(unit, reply, {"a", "b"}, max_output_chars=6000, allow_partial=True)
    assert caught.value.unproven_claims == {"a": "not-unique", "b": "inseparable"}
    assert "Exports are encrypted" not in caught.value.candidate and "Backups" not in caught.value.candidate


P_BACKUPS = "Backups are kept for 7 days."


@pytest.mark.parametrize(
    "name,append,anchor",
    [
        (
            "absent anchor, same paragraph",
            P_BACKUPS + " Exports are encrypted at rest daily.",
            "Exports are encrypted at rest.",
        ),
        ("empty anchor, same paragraph", P_BACKUPS + " Exports are encrypted at rest.", ""),
    ],
)
def test_absent_or_empty_anchor_publishes_no_unproven_text(name, append, anchor):
    unit = zdr_section()
    reply = {
        "section_id": unit.id,
        "edits": [],
        "append": append,
        "notes": [],
        "handled_claim_ids": ["a", "b"],
        "claim_dispositions": [
            {"id": "a", "status": "integrated", "anchor": anchor},
            {"id": "b", "status": "integrated", "anchor": P_BACKUPS},
        ],
    }
    with pytest.raises(editing.ModelError) as caught:
        editing.apply_edits(unit, reply, {"a", "b"}, max_output_chars=6000, allow_partial=True)
    assert caught.value.unproven_claims["b"] == "inseparable"
    assert caught.value.candidate.count("Exports are encrypted") == 0


def test_claim_evidence_text_identifies_unproven_material():
    unit = zdr_section()
    reply = {
        "section_id": unit.id,
        "edits": [],
        "notes": [],
        "handled_claim_ids": ["a", "b"],
        "append": P_BACKUPS + "\n\nAll exports are encrypted at rest with AES-256.",
        "claim_dispositions": [
            {"id": "a", "status": "integrated", "anchor": "Nothing like this."},
            {"id": "b", "status": "integrated", "anchor": P_BACKUPS},
        ],
    }
    text = editing.apply_edits(
        unit,
        reply,
        {"a", "b"},
        max_output_chars=6000,
        allow_partial=True,
        claims={"a": "Exports are encrypted at rest with AES-256.", "b": P_BACKUPS},
    )
    assert reply["handled_claim_ids"] == ["b"]
    assert "AES-256" not in text and text.count(P_BACKUPS) == 1


@pytest.mark.parametrize(
    "append",
    [
        "Backups are kept for 7 days.\n\nExports are encrypted. Exports are encrypted.",  # separate paragraphs
    ],
)
def test_downgraded_claims_material_is_removed_from_a_partial_reply(append):
    unit = zdr_section()
    reply = {
        "section_id": unit.id,
        "edits": [],
        "append": append,
        "notes": [],
        "handled_claim_ids": ["a", "b"],
        "claim_dispositions": [
            {"id": "a", "status": "integrated", "anchor": "Exports are encrypted."},
            {"id": "b", "status": "integrated", "anchor": "Backups are kept for 7 days."},
        ],
    }
    text = editing.apply_edits(unit, reply, {"a", "b"}, max_output_chars=6000, allow_partial=True)
    assert reply["handled_claim_ids"] == ["b"]
    assert text.count("Exports are encrypted") == 0 and text.count("Backups are kept for 7 days.") == 1


def test_abandoned_reply_publishes_nothing():
    unit = zdr_section()
    reply = {
        "section_id": unit.id,
        "notes": [],
        "handled_claim_ids": ["a"],
        "edits": [{"old": " [source: " + DATA + "].", "new": "."}],  # a citation removal adds nothing
        "append": "Exports are encrypted. Exports are encrypted.",
        "claim_dispositions": [{"id": "a", "status": "integrated", "anchor": "Exports are encrypted."}],
    }
    with pytest.raises(editing.ModelError) as caught:
        editing.apply_edits(unit, reply, {"a"}, max_output_chars=6000, allow_partial=True)
    assert caught.value.unproven_claims == {"a": "not-unique"}
    assert caught.value.candidate == unit.text  # the original section, unchanged


# --------------------------------------------------------------------------- #
# When unsure, MISMATCH
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "left,right",
    [
        ("100 Mb/s", "100 MB/s"),  # units are case-sensitive
        ("100Mb", "100MB"),
        ("1 Gbps", "1 GBps"),
        ("5 mW", "5 MW"),
        ("10 ms", "10 Ms"),
        ("2 Gb of memory", "2 GB of memory"),
        ("1.500K", "1500"),  # an ambiguous decimal is never expanded
        ("1.500K", "1,500"),
        ("1.500M", "1,500,000"),
        ("Limit ５００ RPM", "Limit ６００ RPM"),  # fullwidth digits are digits
        ("-5%", "5%"),  # signs are kept
        ("−5 °C", "5 °C"),
        ("+5 points", "-5 points"),
        ("Tier 1 gets 500 RPM and Tier 2 gets 1,000 RPM.", "Tier 1 gets 1,000 RPM and Tier 2 gets 500 RPM."),  # order
        (
            "| Tier | RPM |\n| --- | --- |\n| Tier 1 | 500 |\n| Tier 2 | 1,000 |",
            "| Tier | RPM |\n| --- | --- |\n| Tier 1 | 1,000 |\n| Tier 2 | 500 |",
        ),
        ("The default model is `gpt-5.6`.", "The default model is `gpt-5.5`."),  # code-span digits count
    ],
)
def test_unsure_forms_are_mismatches(left, right):
    assert editing._numbers_differ(left, right)
    sentence, claim = f"Throughput is {left} per link.", f"Throughput is {right} per link."
    assert editing._claim_mismatch(sentence, claim) == "numbers"
    assert editing._prior_anchor(sentence, sentence, sentence, claim) == "mismatch"


@pytest.mark.parametrize(
    "left,right",
    [
        ("Limit ５００ RPM", "Limit 500 RPM"),  # NFKC
        ("1,500 K", "1,500,000"),
        ("1.25M", "1,250,000"),  # two decimal places expand
        ("90 days", "90-day"),
        ("The default model is `gpt-5.6`.", "The default model is gpt-5.6."),
    ],
)
def test_equal_forms_still_match(left, right):
    assert not editing._numbers_differ(left, right)


@pytest.mark.parametrize(
    "name,append,anchor,claims",
    [
        (
            "one-word drift, claims",
            P_BACKUPS + " Exports get encrypted.",
            "Exports are encrypted.",
            {"a": "Exports are encrypted at rest.", "b": P_BACKUPS},
        ),
        (
            "paraphrase, claims",
            P_BACKUPS + " Exported files use AES-256 encryption when stored.",
            "Exports use AES-256 at rest.",
            {"a": "Exports are encrypted at rest with AES-256.", "b": P_BACKUPS},
        ),
        ("one-word drift, no claims", P_BACKUPS + " Exports get encrypted.", "Exports are encrypted.", None),
    ],
)
def test_drifted_unproven_text_is_never_published(name, append, anchor, claims):
    unit = zdr_section()
    reply = {
        "section_id": unit.id,
        "edits": [],
        "append": append,
        "notes": [],
        "handled_claim_ids": ["a", "b"],
        "claim_dispositions": [
            {"id": "a", "status": "integrated", "anchor": anchor},
            {"id": "b", "status": "integrated", "anchor": P_BACKUPS},
        ],
    }
    with pytest.raises(editing.ModelError) as caught:
        editing.apply_edits(unit, reply, {"a", "b"}, max_output_chars=6000, allow_partial=True, claims=claims)
    assert caught.value.unproven_claims["b"] == "inseparable"
    assert caught.value.candidate.count("ncrypt") == 0 and P_BACKUPS not in caught.value.candidate


def test_drift_in_its_own_paragraph_keeps_the_proven_paragraph():
    unit = zdr_section()
    reply = {
        "section_id": unit.id,
        "edits": [],
        "append": P_BACKUPS + "\n\nExports get encrypted.",
        "notes": [],
        "handled_claim_ids": ["a", "b"],
        "claim_dispositions": [
            {"id": "a", "status": "integrated", "anchor": "Exports are encrypted."},
            {"id": "b", "status": "integrated", "anchor": P_BACKUPS},
        ],
    }
    text = editing.apply_edits(unit, reply, {"a", "b"}, max_output_chars=6000, allow_partial=True)
    assert reply["handled_claim_ids"] == ["b"] and text.count("ncrypt") == 0 and text.count(P_BACKUPS) == 1


def test_heading_and_citation_only_additions_stay_with_a_proven_anchor():
    unit = zdr_section()
    reply = {
        "section_id": unit.id,
        "notes": [],
        "handled_claim_ids": ["a", "b"],
        "edits": [{"old": " [source: " + DATA + "].", "new": "."}],
        "append": "### Backups\n\n" + P_BACKUPS + " [source: " + DATA + "]\n\nExports get encrypted.",
        "claim_dispositions": [
            {"id": "a", "status": "integrated", "anchor": "Exports are encrypted."},
            {"id": "b", "status": "integrated", "anchor": P_BACKUPS},
        ],
    }
    text = editing.apply_edits(unit, reply, {"a", "b"}, max_output_chars=6000, allow_partial=True)
    assert reply["handled_claim_ids"] == ["b"]
    assert "### Backups" in text and text.count(P_BACKUPS) == 1 and "ncrypt" not in text
    assert text.count("[source: " + DATA + "]") == 1  # the removal landed, the new citation stayed


# --------------------------------------------------------------------------- #
# Adversarial partial replies: unproven text, number pairs and markup-only anchors
# --------------------------------------------------------------------------- #


def partial_reply(append, dispositions, claims=None, edits=(), page=None):
    unit = zdr_section(page) if page else zdr_section()
    reply = {
        "section_id": unit.id,
        "edits": list(edits),
        "append": append,
        "notes": [],
        "handled_claim_ids": [item["id"] for item in dispositions],
        "claim_dispositions": dispositions,
    }
    try:
        text = editing.apply_edits(
            unit, reply, {item["id"] for item in dispositions}, max_output_chars=6000, allow_partial=True, claims=claims
        )
        return reply["handled_claim_ids"], text
    except editing.ModelError as exc:  # a structural failure carries neither attribute
        return getattr(exc, "unproven_claims", None), getattr(exc, "candidate", unit.text)


A_ANCHOR = {"id": "a", "status": "integrated", "anchor": "Exports are encrypted."}
B2 = "Backups are encrypted, but exports are not encrypted."
TIERS = "Tier 1 gets 500 RPM and Tier 2 gets 1000 RPM."
REORDER_PAGE = C2_PAGE.replace("MAM. ", "MAM. Backups are kept for 7 days and exports for 30 days. ", 1)
REORDER = [
    {
        "old": "Backups are kept for 7 days and exports for 30 days.",
        "new": "Backups are kept for 30 days and exports for 7 days.",
    }
]
QUALIFIER = [
    {
        "old": "Eligible customers may exclude customer content from abuse monitoring logs by obtaining approval for ZDR or MAM.",
        "new": "Customers may exclude customer content from abuse monitoring logs.",
    }
]


@pytest.mark.parametrize(
    "name,append,dispositions,claims,edits,page,needle",
    [
        (
            "word subset drops a negation",
            B2 + " Exports are encrypted.",
            [
                {"id": "a", "status": "integrated", "anchor": "Exports are encrypted at rest."},
                {"id": "b", "status": "integrated", "anchor": B2},
            ],
            {"a": "Exports are encrypted at rest.", "b": B2},
            (),
            None,
            "Exports are encrypted.",
        ),
        (
            "word subset moves a number",
            TIERS + " Tier 2 gets 500 RPM.",
            [
                {"id": "a", "status": "integrated", "anchor": "Tier 3 gets 2000 RPM."},
                {"id": "b", "status": "integrated", "anchor": TIERS},
            ],
            {"a": "Tier 3 gets 2000 RPM.", "b": TIERS},
            (),
            None,
            "Tier 2 gets 500 RPM.",
        ),
        (
            'inline "# " is not a heading',
            P_BACKUPS + " # Exports get encrypted.",
            [A_ANCHOR, {"id": "b", "status": "integrated", "anchor": P_BACKUPS}],
            None,
            (),
            None,
            "Exports get encrypted",
        ),
        (
            "[source: prose] is not a citation",
            P_BACKUPS + " [source: Exports get encrypted at rest]",
            [A_ANCHOR, {"id": "b", "status": "integrated", "anchor": P_BACKUPS}],
            None,
            (),
            None,
            "Exports get encrypted",
        ),
        (
            "reorder beside a proven append",
            P_BACKUPS,
            [A_ANCHOR, {"id": "b", "status": "integrated", "anchor": P_BACKUPS}],
            None,
            REORDER,
            REORDER_PAGE,
            "Backups are kept for 30 days and exports for 7 days.",
        ),
        (
            "reorder, all unproven",
            "",
            [A_ANCHOR],
            None,
            REORDER,
            REORDER_PAGE,
            "Backups are kept for 30 days and exports for 7 days.",
        ),
        (
            "qualifier removal, all unproven",
            "",
            [A_ANCHOR],
            None,
            QUALIFIER,
            None,
            "Customers may exclude customer content from abuse monitoring logs.",
        ),
    ],
)
def test_unproven_text_is_never_published(name, append, dispositions, claims, edits, page, needle):
    _, text = partial_reply(append, dispositions, claims, edits, page)
    assert text.count(needle) == 0


def test_heading_line_anchor_and_citation_removal_are_kept():
    handled, text = partial_reply(
        "### Backups\n\n" + P_BACKUPS + " [source: " + DATA + "]",
        [A_ANCHOR, {"id": "b", "status": "integrated", "anchor": P_BACKUPS}],
        {"a": "Exports are encrypted at rest.", "b": P_BACKUPS},
        [{"old": " [source: " + DATA + "].", "new": "."}],
    )
    assert handled == ["b"] and text.count("### Backups") == 1 and text.count(P_BACKUPS) == 1


def test_sentence_removal_is_never_kept_beside_a_proven_claim():
    handled, text = partial_reply(
        P_BACKUPS,
        [A_ANCHOR, {"id": "b", "status": "integrated", "anchor": P_BACKUPS}],
        None,
        [
            {
                "old": "Eligible customers may exclude customer content from abuse monitoring logs by "
                "obtaining approval for ZDR or MAM. ",
                "new": "",
            }
        ],
    )
    assert handled == ["b"] and "Eligible customers may exclude" in text


@pytest.mark.parametrize(
    "left,right",
    [
        ("10² requests", "102 requests"),  # superscripts are never folded into digits
        ("10⁶ tokens", "106 tokens"),
        ("–5 °C", "5 °C"),  # every minus-like dash is a sign
        ("—5 °C", "5 °C"),
        ("‐5 °C", "5 °C"),
        ("$-5 credit", "-5 credit"),  # currency is kept with its sign
        ("Costs -$5.", "Costs $-5."),
        ("1.5000K", "1500"),
        ("1.500 K", "1500"),
        ("Limit ٥٠٠ RPM", "Limit 500 RPM"),
        ("-５", "5"),
        ("a `-5` b", "a 5 b"),
        ("```\nlimit = 500\n```", "limit = 600"),
        ("5 to 10", "10 to 5"),
        ("5 KB", "5 kB"),
    ],
)
def test_number_pairs_differ(left, right):
    assert editing._numbers_differ(left, right)


@pytest.mark.parametrize(
    "left,right",
    [
        ("10 FLOPs", "10 FLOP"),
        ("1,500K", "1,500,000"),
        ("| Model | Gb |\n| --- | --- |\n| x | 2 |", "Model x has 2 Gb."),  # a header unit token, same case
        ("| Model | Ms |\n| --- | --- |\n| x | 10 |", "Model x 10 Ms."),
        ("1.50K", "1500"),
        ("1.5K", "1500"),
        ("５００", "500"),
    ],
)
def test_equal_number_forms(left, right):
    assert not editing._numbers_differ(left, right)


@pytest.mark.parametrize(
    "sentence,anchor,claim",
    [
        (
            "**Exports** are encrypted with `AES-256` for 30 days.",
            "Exports are encrypted with AES-256 for 30 days.",
            "Exports are encrypted with AES-256 for 30 days.",
        ),
        (
            "Customers can’t opt out of “Zero Data Retention” after 90 days.",
            'Customers can\'t opt out of "Zero Data Retention" after 90 days.',
            'Customers can\'t opt out of "Zero Data Retention" after 90 days.',
        ),
        (
            "The default model is `gpt-5.6` with 1,000 RPM.",
            "The default model is gpt-5.6 with 1,000 RPM.",
            "The default model is gpt-5.6 with 1,000 RPM.",
        ),
    ],
)
def test_markup_only_already_present_passes(sentence, anchor, claim):
    page = C2_PAGE.replace("MAM. ", "MAM. " + sentence + " ", 1)
    unit = zdr_section(page)
    reply = {
        "section_id": unit.id,
        "edits": [],
        "append": "",
        "notes": [],
        "handled_claim_ids": ["a"],
        "claim_dispositions": [{"id": "a", "status": "already_present", "anchor": anchor}],
    }
    assert (
        editing.apply_edits(unit, reply, {"a"}, max_output_chars=6000, allow_partial=True, claims={"a": claim})
        == unit.text
    )
    assert editing._prior_anchor(anchor, unit.text, unit.text, claim) == "normalised"


def test_differing_claim_fails():
    page = C2_PAGE.replace("MAM. ", "MAM. Exports are encrypted for 30 days. ", 1)
    unit = zdr_section(page)
    reply = {
        "section_id": unit.id,
        "edits": [],
        "append": "",
        "notes": [],
        "handled_claim_ids": ["a"],
        "claim_dispositions": [
            {"id": "a", "status": "already_present", "anchor": "Exports are encrypted for 30 days."}
        ],
    }
    with pytest.raises(editing.ModelError, match="differs from the claim") as caught:
        editing.apply_edits(
            unit,
            reply,
            {"a"},
            max_output_chars=6000,
            allow_partial=True,
            claims={"a": "Exports are encrypted for 31 days."},
        )
    assert caught.value.unproven_claims == {"a": "mismatch"}


def test_number_comparison_fuzz_never_raises():
    import random

    for seed, alphabet in [
        (7, "0123456789.,  KMBTkmbx%×$€-"),
        (11, "0123456789.,:;  KMBTkmbxX%×$€£-|\n\tabcdHQvms²５٣/()[]`"),
        (13, "0123456789 .,-−+–`|#\n²⁶¹½KMBTmsGbB%$abcXY[]()source:^"),
    ]:
        rng = random.Random(seed)
        for _ in range(5000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40)))
            editing._numbers(text)
            editing._numbers_differ(text, text[::-1])
            editing._claim_mismatch(text, text)
            for unit, heading in editing._added_sentences(text[::-1], text):
                editing._publishable(unit, [text[::-1]], heading)


# --------------------------------------------------------------------------- #
# Indented hash lines, footnotes and number-token variants
# --------------------------------------------------------------------------- #

PROVEN_B = {"id": "b", "status": "integrated", "anchor": P_BACKUPS}
CLAIMS_AB = {"a": "Exports are encrypted at rest.", "b": P_BACKUPS}


@pytest.mark.parametrize(
    "append",
    [
        P_BACKUPS + "\n    # Exports are never encrypted.",  # four spaces: paragraph text
        P_BACKUPS + "\n\t# Exports are never encrypted.",  # a tab
        P_BACKUPS + "\n\n    # Exports are never encrypted.",  # an indented code block
    ],
)
def test_indented_hash_line_is_not_a_heading(append):
    _, text = partial_reply(append, [A_ANCHOR, PROVEN_B], CLAIMS_AB)
    assert text.count("Exports are never encrypted") == 0


def test_heading_with_up_to_three_spaces_is_a_heading():
    assert editing._units_of("   ### Backups") == [("   ### Backups", True)]
    assert editing._units_of("    ### Backups") == [("    ### Backups", False)]
    assert editing._units_of("\t### Backups") == [("\t### Backups", False)]


def test_editorial_grounds_an_indented_hash_line():
    from doclayer.curator import editorial

    units = editorial._section_units("Backups are kept for 7 days.\n    # Exports are never encrypted.\n")
    assert any("Exports are never encrypted" in unit for _, unit, _ in units)
    assert not any("Real heading" in unit for _, unit, _ in editorial._section_units("  # Real heading\nText.\n"))


@pytest.mark.parametrize(
    "append",
    [
        P_BACKUPS + " [^Exports-are-never-encrypted]",  # attached: the label is prose
        P_BACKUPS + "\n\n[^Exports-are-never-encrypted]",  # alone
    ],
)
def test_unresolved_footnote_label_is_prose(append):
    _, text = partial_reply(append, [A_ANCHOR, PROVEN_B], CLAIMS_AB)
    assert "Exports-are-never-encrypted" not in text


def test_resolved_footnote_is_citation_only():
    page = C2_PAGE.replace("\n\n### Modified", "\n\n[^7]: https://x.example/retention\n\n### Modified", 1)
    handled, text = partial_reply(P_BACKUPS + "[^7]", [A_ANCHOR, PROVEN_B], CLAIMS_AB, page=page)
    assert handled == ["b"] and (P_BACKUPS + "[^7]") in text
    handled, text = partial_reply(P_BACKUPS + "\n\n[^8]: src-65c5c61b5f", [A_ANCHOR, PROVEN_B], CLAIMS_AB)
    assert handled == ["b"] and "[^8]: src-65c5c61b5f" in text  # a definition whose content is a source ID


@pytest.mark.parametrize(
    "left,right",
    [
        ("10⁻⁶ errors", "10⁶ errors"),  # the superscript minus is kept
        ("10⁻⁶ errors", "10^6 errors"),
        ("2⁴ GPUs", "2-4 GPUs"),  # the exponent stays in the token
        ("2⁴ GPUs", "2 4 GPUs"),
        ("10₋₃", "10₃"),
        ("| Model | MB/s |\n| --- | --- |\n| x | 100 |", "x does 100 Mb/s."),  # header unit tokens keep case
        ("| Model | Mb/s |\n| --- | --- |\n| x | 100 |", "x does 100 MB/s."),
        ("| Device | mW |\n| --- | --- |\n| x | 5 |", "x uses 5 MW."),
        ("| Model | Gb |\n| --- | --- |\n| x | 2 |", "x has 2 GB."),
        ("| Model | GB |\n| --- | --- |\n| x | 2 |", "x has 2 Gb."),
        ("| Size | kB |\n| --- | --- |\n| x | 5 |", "x is 5 KB."),
        ("| Latency | ms |\n| --- | --- |\n| x | 10 |", "x takes 10 Ms."),
        ("¥500", "₹500"),  # every currency symbol is part of the token
        ("¥500", "500"),
        ("− 5 °C", "5 °C"),  # a spaced Unicode minus is a sign
    ],
)
def test_number_pairs_differ_for_exponents_units_and_currency(left, right):
    assert editing._numbers_differ(left, right)


@pytest.mark.parametrize(
    "left,right",
    [
        ("2⁴ GPUs", "2^4 GPUs"),
        ("10⁶ errors", "10^6 errors"),
        ("| Model | MB/s |\n| --- | --- |\n| x | 100 |", "x does 100 MB/s."),
        ("| Tier | Batch |\n| --- | --- |\n| x | 3 |", "x: 3 batch"),  # a non-unit header label folds case
        ("| Tier | RPM |\n| --- | --- |\n| x | 3 |", "x: 3 rpm"),
    ],
)
def test_equal_exponent_and_header_forms(left, right):
    assert not editing._numbers_differ(left, right)


@pytest.mark.parametrize(
    "sentence,claim",
    [
        ("The error rate is below 10⁻⁶ per request.", "The error rate is below 10⁶ per request."),
        ("| Model | MB/s |\n| --- | --- |\n| x | 100 |", "Model x does 100 Mb/s."),
        ("| Model | Gb |\n| --- | --- |\n| x | 2 |", "Model x has 2 GB."),
        ("It holds at − 5 °C.", "It holds at 5 °C."),
    ],
)
def test_already_present_numeric_variants_are_mismatches(sentence, claim):
    section = "## M\n\nIntro line.\n\n" + sentence + "\n\nClosing line.\n"
    assert editing._prior_anchor(sentence, section, section, claim) == "mismatch"
