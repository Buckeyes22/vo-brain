"""Quote matching ignores markdown markers and whitespace only."""

from test_verification_contract import client, positive, published  # noqa: F401

from doclayer import verification

BODY = (
    "# Host\n\n"
    "- **Available:** 33,073,586,176 bytes\n\n"
    "Install `xdotool` and run `command -v xdotool`.\n\n"
    "The pool is _fast_ and\nspans two lines.\n\n"
    "Plain sentence about the scratch volume.\n"
)
PATH = "wiki/host.md"
PAGES = [dict(path=PATH, body=BODY, page_metadata={})]


def row(facet, quote=None, *, answered=True):
    evidence = [] if quote is None else [dict(path=PATH, quote=quote)]
    return dict(facet=facet, answered=answered, scope_preserved=True, evidence=evidence)


def reply(*rows, answers=None):
    if answers is None:
        answers = all(r["answered"] for r in rows)
    return dict(answers=answers, facets=list(rows), reason="r")


def validate(rep, facets):
    return verification._validate_semantic(rep, facets, [], PAGES, question="where?")


def test_markdown_markers_and_whitespace_are_normalised_on_both_sides():
    for quote in (
        "Available: 33,073,586,176 bytes",
        "Install xdotool and run command -v xdotool.",
        "The pool is fast and spans two lines.",
        "The pool is *fast* and spans   two lines.",
        "Available:\n33,073,586,176 bytes",
    ):
        out = validate(reply(row("f", quote)), ["f"])
        assert out is not None and out["answers"] is True, quote
        assert out["facets"][0]["evidence"][0]["quote"] == quote


def test_quote_differing_in_words_is_not_accepted():
    # Claimed answered with nothing citable: no verdict (the caller retries).
    assert validate(reply(row("f", "Available: 33,073,586,177 bytes")), ["f"]) is None


def test_underscores_inside_identifiers_are_words_not_formatting():
    body = "Set `DOC_LAYER_GAP_OWNER` and max_memory in the config file.\nA _plain emphasis_ here and _x_ too.\n"
    pages = [dict(path=PATH, body=body, page_metadata={})]

    def check(quote):
        rep = reply(row("f", quote))
        return verification._validate_semantic(rep, ["f"], [], pages, question="where?")

    assert check("Set DOC_LAYER_GAP_OWNER and max_memory in the config file.")["answers"] is True
    assert check("Set DOCLAYERGAPOWNER and max_memory in the config file.") is None
    assert check("Set DOC_LAYER_GAP_OWNER and maxmemory in the config file.") is None
    assert check("A plain emphasis here and x too.")["answers"] is True


def test_ellipsis_quote_matches_each_fragment_in_order():
    body = "The scratch volume is mounted at /mnt/fast on App1 and is not backed up nightly.\n"
    pages = [dict(path=PATH, body=body, page_metadata={})]

    def check(quote):
        return verification._validate_semantic(reply(row("f", quote)), ["f"], [], pages, question="where?")

    assert check("scratch volume is mounted ... and is not backed up")["answers"] is True
    assert check("scratch volume is mounted \u2026 not backed up nightly.")["answers"] is True
    assert check("not backed up nightly ... scratch volume is mounted") is None  # order
    assert check("scratch volume is mounted ... and is always backed up") is None
    assert check("scratch volume ... and") is None  # fragments too small to mean anything


def test_one_bad_quote_rejects_only_its_claim():
    rep = reply(
        row("good", "Available: 33,073,586,176 bytes"),
        row("bad", "this sentence is not on the page at all", answered=False),
    )
    out = validate(rep, ["good", "bad"])
    assert out is not None
    by = {r["facet"]: r for r in out["facets"]}
    assert by["good"]["answered"] is True and by["good"]["evidence"]
    assert by["bad"]["answered"] is False and by["bad"]["evidence"] == []
    assert out["answers"] is False


def test_bad_quote_beside_good_quote_keeps_the_facet_answered():
    rep = reply(
        dict(
            facet="f",
            answered=True,
            scope_preserved=True,
            evidence=[
                dict(path=PATH, quote="Available: 33,073,586,176 bytes"),
                dict(path=PATH, quote="invented words that are absent"),
            ],
        )
    )
    out = validate(rep, ["f"])
    assert out["answers"] is True
    assert [c["quote"] for c in out["facets"][0]["evidence"]] == ["Available: 33,073,586,176 bytes"]


def test_negative_verdict_survives_a_bad_quote():
    rep = reply(row("f", "totally made up quote text", answered=False))
    out = validate(rep, ["f"])
    assert out is not None and out["answers"] is False


def test_answers_true_with_unanswered_row_is_derived_false_not_rejected():
    rep = reply(
        row("a", "Plain sentence about the scratch volume."),
        row("b", None, answered=False),
        answers=True,
    )
    out = validate(rep, ["a", "b"])
    assert out is not None and out["answers"] is False


def test_structural_defects_still_reject_the_whole_reply():
    assert validate(reply(row("f", "Available: 33,073,586,176 bytes")), ["other"]) is None
    assert validate({"answers": True}, ["f"]) is None
    bad = reply(row("f", "Available: 33,073,586,176 bytes"))
    bad["facets"][0]["evidence"][0]["extra"] = 1
    assert validate(bad, ["f"]) is None


def test_validation_is_idempotent_and_does_not_mutate_its_input():
    rep = reply(
        row("good", "Available: 33,073,586,176 bytes"),
        row("bad", "this sentence is not on the page at all", answered=False),
    )
    import copy

    before = copy.deepcopy(rep)
    once = validate(rep, ["good", "bad"])
    assert rep == before
    assert validate(once, ["good", "bad"]) == once


def test_answered_facet_whose_every_quote_fails_stays_verifying(published, tmp_path):
    """The model claimed an answer and only its citations failed: retry, no negative."""
    from doclayer.gap_budget import ResourceBudget

    root, path, commit, ledger, record, reply_, index = published
    bad = positive(path)
    bad["facets"][0]["evidence"][0]["quote"] = "Nimbus request rate limit is 41 RPM."
    model, usage = client(tmp_path, bad)
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply_,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    assert result.outcome == "verifying" and usage.model_calls == 1
    assert ledger.load(record.gap_id).status == "verifying"


def test_facet_the_model_marked_unanswered_still_gives_a_final_negative(published, tmp_path):
    from doclayer.gap_budget import ResourceBudget

    root, path, commit, ledger, record, reply_, index = published
    neg = dict(
        answers=False,
        facets=[
            dict(
                facet="rate",
                answered=False,
                scope_preserved=True,
                evidence=[dict(path=path, quote="a sentence that is not on the page")],
            )
        ],
        reason="not answered",
    )
    model, usage = client(tmp_path, neg)
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply_,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    assert result.outcome == "failed" and usage.model_calls == 1


def test_citation_of_an_unsupplied_page_is_retryable_not_a_negative(published, tmp_path):
    from doclayer.gap_budget import ResourceBudget

    root, path, commit, ledger, record, reply_, index = published
    bad = positive(path)
    bad["facets"][0]["evidence"][0]["path"] = "wiki/unrelated.md"
    model, usage = client(tmp_path, bad)
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply_,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    assert result.outcome == "verifying" and usage.model_calls == 1
    assert ledger.load(record.gap_id).status != "resolved"


def test_validation_that_changes_a_verdict_means_it_is_not_reusable_as_stored():
    """verify_gap reuses a checkpoint only if validation returns it unchanged."""
    rep = reply(
        dict(
            facet="f",
            answered=True,
            scope_preserved=True,
            evidence=[
                dict(path=PATH, quote="Available: 33,073,586,176 bytes"),
                dict(path=PATH, quote="invented words that are absent"),
            ],
        )
    )
    out = validate(rep, ["f"])
    assert out is not None and out != rep


def test_dunder_names_and_cross_line_underscores_are_not_emphasis():
    q = verification._quote_in
    assert not q("edit init.py", "edit __init__.py")
    assert q("edit __init__.py", "edit `__init__.py`")
    assert not q("Set PREFIX = 1", "Mount /_data here.\nSet PREFIX_ = 1")
    assert not q("Mount /data here.", "Mount /_data here.")
    assert not q("a plain remark", "a _plain\nremark_")  # no pair across lines
    assert q("a plain remark", "a _plain remark_")
    assert q("Available: 33,073,586,176 bytes", "- **Available:** 33,073,586,176 bytes")
    assert not q("an emphasis here", "an _ emphasis _ here")  # inner starts/ends with space


def test_ellipsis_fragments_must_share_one_paragraph_or_section():
    body = (
        "## Limits\n\nThe rate limit is 40 req per key.\n\n"
        "## Totals\n\nUnrelated RPM total is reported nightly.\n\n"
        "The rate limit is 40 req and burst RPM total is 90 per key.\n"
    )
    q = verification._quote_in
    # fragments from different sections are never joined
    assert not q("limit is 40 req per key ... RPM total is reported nightly", body)
    # the same fragments inside one paragraph match
    assert q("rate limit is 40 req ... RPM total is 90", body)
