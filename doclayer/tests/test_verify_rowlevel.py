"""A failed quantity requirement downgrades one row, never the whole reply."""

import pytest

from doclayer import verification

PATH = "wiki/app-db-disk-full-and-recovery-mode.md"
QUESTION = (
    "Where is App1 app-db PostgreSQL data mounted, and are there documented storage limits or "
    "recovery procedures for No space left on device and recovery mode?"
)
QUALITATIVE_LIMIT = (
    "Limits observed: the Docker volume `app-data_pgdata` is driver `local` with `options=map[]` and `app-db` has "
    "`HostConfig.StorageOpt` `map[]`, and `tune2fs -l /dev/sdb1` lists no `quota` or `project` feature, so the only size "
    "limit on the data directory is the free space of the shared `/mnt/fast` filesystem."
)
QUANTITY = "`/mnt/fast` is `/dev/sdb1`, ext4, `rw,noatime`, 750G virtual disk, shared with `ai-db-postgres`."
QUALITATIVE = "The recovery procedure is to stop the writers, free space on `/mnt/fast`, then restart PostgreSQL."
FREE = "`/mnt/fast` has 16,374,161,408 bytes free."
BODY = f"# App DB\n\n{QUALITATIVE_LIMIT}\n\n{QUANTITY}\n\n{QUALITATIVE}\n\n{FREE}\n"
PAGES = [dict(path=PATH, body=BODY, page_metadata={})]


def reply(*rows, answers=True):
    return dict(
        answers=answers,
        reason="r",
        facets=[
            dict(facet=f, answered=True, scope_preserved=True, evidence=[dict(path=PATH, quote=q)]) for f, q in rows
        ],
    )


def validate(rep, facets, question=QUESTION):
    why = []
    out = verification._validate_semantic(rep, facets, [], PAGES, {PATH: BODY}, question=question, why=why)
    return out, why


def test_a_quantity_citation_resolves_the_gap_2869_storage_facet():
    out, why = validate(reply(("storage", QUANTITY)), ["storage"])
    assert why == [] and out["answers"] is True and out["facets"][0]["answered"] is True


def test_a_qualitative_citation_downgrades_the_row_instead_of_rejecting_the_reply():
    out, why = validate(reply(("storage", QUALITATIVE_LIMIT)), ["storage"])
    assert why == [] and out is not None and out["answers"] is False
    assert out["facets"][0]["answered"] is False and out["facets"][0]["evidence"] == []


@pytest.mark.parametrize("facet", ["storage capacity", "free space", "used", "size", "usage", "available"])
def test_measurement_facets_require_a_quantity(facet):
    question = "How much storage is there?"
    out, why = validate(reply((facet, QUALITATIVE_LIMIT)), [facet], question=question)
    assert why == [] and out["answers"] is False and out["facets"][0]["answered"] is False
    ok, _ = validate(reply((facet, FREE)), [facet], question=question)
    assert ok["answers"] is True


def test_one_failing_row_does_not_reject_the_reply_or_sink_the_others():
    out, why = validate(
        reply(("storage", QUALITATIVE), ("recovery", QUALITATIVE), answers=True), ["storage", "recovery"]
    )
    assert why == [] and out is not None
    assert {r["facet"]: r["answered"] for r in out["facets"]} == {"storage": False, "recovery": True}
    assert out["answers"] is False


def test_the_flag_false_with_a_downgraded_row_is_final_not_a_contradiction_retry():
    out, why = validate(
        reply(("storage", QUALITATIVE), ("recovery", QUALITATIVE), answers=False), ["storage", "recovery"]
    )
    assert why == [] and out["answers"] is False
    assert {r["facet"]: r["answered"] for r in out["facets"]} == {"storage": False, "recovery": True}


def test_a_downgraded_verdict_revalidates_unchanged():
    out, _ = validate(reply(("storage", QUANTITY), ("recovery", QUALITATIVE)), ["storage", "recovery"])
    again, why = validate(out, ["storage", "recovery"])
    assert why == [] and again == out


@pytest.mark.parametrize(
    "quote",
    [
        "This is a path a task intended to use, not a recorded approval of it as scratch.",
        "The approval is not recorded.",
    ],
)
def test_a_not_a_recorded_statement_is_a_marker_for_approval_facets(quote):
    assert verification._states_not_established(quote, facet="approval") is True
    page = [dict(path=PATH, body="# P\n\n" + quote + "\n", page_metadata={})]
    out = verification._validate_semantic(reply(("approval", quote)), ["approval"], [], page, {PATH: page[0]["body"]})
    assert out["answers"] is False and out["facets"][0]["answered"] is False


def test_not_a_recorded_is_scoped_to_approval_authorization_and_permission_facets():
    quote = "The ledger is at `/data/bk1`, not a recorded approval of it."
    for facet in ("location", "storage", "need", "version", "backup location"):
        assert verification._states_not_established(quote, facet=facet) is False, facet
    for facet in ("approval", "authorization", "write permission", "Authorised use", "consent"):
        assert verification._states_not_established(quote, facet=facet) is True, facet
    assert verification._states_not_established("The location is not recorded.", facet="location") is True


# -- a row that dropped a non-literal citation is a retry, never a final negative -------------------- #

NOT_ON_PAGE = "The scratch volume has 500 GB free on the fast disk."
LITERAL_QUALITATIVE = "The scratch volume is on the fast disk today."
UNKNOWN_QUOTE = "Free space on the scratch volume is unknown as of 2026-10-06."
DIRTY_BODY = f"# H\n\n{LITERAL_QUALITATIVE}\n\n{UNKNOWN_QUOTE}\n"


@pytest.mark.parametrize("literal", [LITERAL_QUALITATIVE, UNKNOWN_QUOTE])
@pytest.mark.parametrize("flag", [True, False])
def test_a_row_with_a_dropped_non_literal_citation_is_a_retry_whether_downgraded_for_quantity_or_unknown(literal, flag):
    page = [dict(path=PATH, body=DIRTY_BODY, page_metadata={})]
    rep = dict(
        answers=flag,
        reason="r",
        facets=[
            dict(
                facet="storage",
                answered=True,
                scope_preserved=True,
                evidence=[dict(path=PATH, quote=NOT_ON_PAGE), dict(path=PATH, quote=literal)],
            )
        ],
    )
    why = []
    out = verification._validate_semantic(
        rep, ["storage"], [], page, {PATH: DIRTY_BODY}, question="How much storage is there?", why=why
    )
    assert out is None and why, (literal, flag)


def test_with_every_citation_literal_the_same_rows_are_downgraded_not_retried():
    page = [dict(path=PATH, body=DIRTY_BODY, page_metadata={})]
    for literal in (LITERAL_QUALITATIVE, UNKNOWN_QUOTE):
        rep = dict(
            answers=True,
            reason="r",
            facets=[
                dict(facet="storage", answered=True, scope_preserved=True, evidence=[dict(path=PATH, quote=literal)])
            ],
        )
        out = verification._validate_semantic(
            rep, ["storage"], [], page, {PATH: DIRTY_BODY}, question="How much storage is there?"
        )
        assert out["answers"] is False and out["facets"][0]["answered"] is False


# -- the restored gate: only measure aspects and measurement facets need a quantity -------------------- #


@pytest.mark.parametrize(
    "facet, question, quote",
    [
        ("connection limit", "What is the connection limit of app-db?", "Postgres runs with max_connections = 100."),
        ("backup window", "What is the backup window?", "Backups run nightly from 01:00 for 3 hours."),
        ("maintenance window", "What is the maintenance window?", "Maintenance runs Sundays 02:00-04:00 for 2 hours."),
    ],
)
def test_limit_and_window_facets_are_not_measurement_facets_and_stay_answered(facet, question, quote):
    body = f"# P\n\n{quote}\n"
    why = []
    out = verification._validate_semantic(
        reply((facet, quote)),
        [facet],
        [],
        [dict(path=PATH, body=body, page_metadata={})],
        {PATH: body},
        question=question,
        why=why,
    )
    assert why == [] and out["answers"] is True and out["facets"][0]["answered"] is True


@pytest.mark.parametrize(
    "facet, question, quote",
    [
        (
            "memory",
            "Is there a memory limit, and how much does ollama use?",
            "The ollama container has no memory limit set.",
        ),
        (
            "storage",
            "What are the storage limits and free space for app-db?",
            "The volume has `options=map[]` and no quota.",
        ),
        (
            "storage",
            "Are there storage limits on app-db, and how big is it?",
            "The volume has `options=map[]` and no quota.",
        ),
    ],
)
def test_elided_how_much_questions_need_a_quantity_not_a_no_limit_statement(facet, question, quote):
    body = f"# P\n\n{quote}\n"
    why = []
    out = verification._validate_semantic(
        reply((facet, quote)),
        [facet],
        [],
        [dict(path=PATH, body=body, page_metadata={})],
        {PATH: body},
        question=question,
        why=why,
    )
    assert why == [] and out["answers"] is False and out["facets"][0]["answered"] is False
