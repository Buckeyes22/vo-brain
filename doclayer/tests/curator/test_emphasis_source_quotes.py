"""Rendered emphasis quotations retain exact original, scoped source bytes."""

import json

import pytest

from doclayer.curator import curate, source_constraints
from doclayer.curator.model import ModelError
from doclayer.curator.state import Evidence, JobState


@pytest.mark.parametrize(
    "marked",
    [
        "**Explicit refusals:**",
        "__Explicit refusals:__",
        "*Explicit refusals:*",
        "_Explicit refusals:_",
        "***Explicit refusals:***",
    ],
)
def test_emphasis_display_restores_complete_original_markup(marked):
    body = marked + " Safety-based model refusals are programmatically detectable."
    quote = "Explicit refusals: Safety-based model refusals are programmatically detectable."
    spans = curate._quote_spans(quote, body)
    assert spans == [(0, len(body))]
    values = [dict(id="s", supported=True, consistent=True, reason="Source", qualifiers=[], support_quotes=[quote])]
    source_constraints._restore_quote_spans(values, body, "", [], [])
    assert values[0]["support_quotes"] == [body]
    assert not source_constraints._validation_issues(values, "s", [("intro", body)], body, "", [], [])


@pytest.mark.parametrize(
    "body",
    [
        "`**Enabled:** Always available.`",
        "````\n**Enabled:** Always available.\n````",
        "~~~\n**Enabled:** Always available.\n~~~",
        "    **Enabled:** Always available.",
        "\t**Enabled:** Always available.",
        r"\**Enabled:** Always available.",
        "~~Enabled:~~ Always available.",
        "prefix_Enabled:_suffix Always available.",
    ],
)
def test_code_escape_unmatched_semantic_and_intraword_markers_stay_literal(body):
    assert not curate._quote_spans("Enabled: Always available.", body)
    assert curate._quote_spans(body, body) == [(len(body) - len(body.lstrip()), len(body))]


def test_nested_emphasis_and_links_preserve_destination_and_all_original_bytes():
    body = "**Use [the *enterprise* endpoint](https://example.invalid/API):** only in version 3."
    quote = "Use the enterprise endpoint: only in version 3."
    spans = curate._quote_spans(quote, body)
    assert spans == [(0, len(body))]
    assert not curate._quote_spans("Use the enterprise endpoint (https://wrong.invalid/API): only in version 3.", body)


def test_ambiguity_scope_and_hidden_source_remain_strict():
    first = "**Enabled:** Always available."
    second = "Enabled: Always available."
    body = "# Alpha\n" + first + "\n# Beta\n" + second + "\n"
    quote = "Enabled: Always available."
    assert len(curate._quote_spans(quote, body)) == 2
    assert curate._quote_span(quote, body) is None
    left, right = curate._quote_span(quote, body, "#Alpha")
    assert body[left:right] == first
    values = [dict(id="s", supported=True, consistent=True, reason="Source", qualifiers=[], support_quotes=[quote])]
    source_constraints._restore_quote_spans(values, body, "", [], [])
    assert values[0]["support_quotes"] == [quote]  # raw literal already valid; no ambiguous rewrite
    hidden = "**Required:** Enterprise version 3 only."
    values[0]["support_quotes"] = ["Required: Enterprise version 3 only."]
    source_constraints._restore_quote_spans(values, second, "", [], [])
    assert source_constraints._validation_issues(
        values, "s", [("visible", second), ("hidden", hidden)], second, "", [], []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_pending", [False, True])
async def test_real_store_restart_repairs_emphasis_but_never_blesses_metadata_quotes(store, legacy_pending):
    intro = "**Explicit refusals:** Safety-based model refusals are now programmatically detectable."
    rendered = "Explicit refusals: Safety-based model refusals are now programmatically detectable"
    fact = "A refusal is represented as a content item with type refusal within the message output."
    text = intro + "\n" + "General preparation guidance.\n" * 90 + "# Refusals\n" + fact + "\n"
    record = Evidence("s", "manual", "Refusals", "quote", fact, quote=fact, applicability={"scope": "Responses API"})
    identity = curate.evidence_id(record)
    state = JobState("job-emphasis123", "emphasis-proof", "Refusals")
    store.create(state)
    requests = []

    class Initial:
        async def chat_verdicts(self, system, user, *, char_budget):
            frame = json.loads(user.split("\n", 1)[1])
            requests.append(frame)
            if user.startswith("SOURCE LITERAL QUOTATION REPAIR"):
                raise ModelError("offline", transient=True)
            return {
                "verdicts": [
                    dict(
                        id=frame["id"],
                        supported=False,
                        consistent=True,
                        reason="Intro alone not enough.",
                        qualifiers=["Structured Outputs feature", "Responses API"],
                        support_quotes=[rendered],
                    )
                ]
            }

    def retain(rows):
        state.evidence_verification.update(rows)
        store.save(state)

    with pytest.raises(ModelError, match="offline"):
        await curate.verify_source_claims(Initial(), [record], {"s": text}, char_budget=12000, progress=retain)
    state = store.load(state.job_id)
    proof = state.evidence_verification[identity]
    assert proof["verification_pending"] and proof["pending_quote_repairs"]
    pending = next(iter(proof["pending_quote_repairs"].values()))
    assert pending["original"]["support_quotes"] == [intro.rstrip(".")]
    # The display mapping fixed the actual quotation; metadata remains invalid
    # and must be explicitly changed by the model on this same bound restart.
    assert {t["field"] for t in pending["targets"]} == {"qualifiers"}
    if legacy_pending:
        # Exact historical representation: old source mapping left the rendered
        # support quote invalid, alongside metadata targets, in this SAME bound
        # request generation. It must survive actual store restart unchanged.
        pending["original"]["support_quotes"] = [rendered]
        pending["targets"].append(
            {
                "field": "support_quotes",
                "index": 0,
                "invalid_quote": rendered,
                "error": "support_quotes[0] is not a literal source span",
            }
        )
        store.save(state)
        state = store.load(state.job_id)

    class Resume:
        async def chat_verdicts(self, system, user, *, char_budget):
            frame = json.loads(user.split("\n", 1)[1])
            requests.append(frame)
            if user.startswith("SOURCE LITERAL QUOTATION REPAIR"):
                assert frame["fixed_semantic_verdict"]["supported"] is False
                return {
                    "verdicts": [
                        {
                            "id": frame["id"],
                            "edits": [
                                {
                                    "field": t["field"],
                                    "index": t["index"],
                                    "replacement": rendered if t["field"] == "support_quotes" else None,
                                }
                                for t in frame["invalid_quotation_fields"]
                            ],
                        }
                    ]
                }
            supported = fact in frame["ancestor_context"]
            return {
                "verdicts": [
                    dict(
                        id=frame["id"],
                        supported=supported,
                        consistent=True,
                        reason="Actual current source.",
                        qualifiers=[],
                        support_quotes=[fact] if supported else [],
                    )
                ]
            }

    result = (
        await curate.verify_source_claims(
            Resume(), [record], {"s": text}, char_budget=12000, known=state.evidence_verification, progress=retain
        )
    )[identity]
    assert result["supported"] and not result.get("pending_quote_repairs")
    assert intro.rstrip(".") in result["excerpt"] and fact in result["excerpt"]
    assert "Responses API" not in result["excerpt"] and "Structured Outputs feature" not in result["excerpt"]
    assert len(requests) >= 4  # semantic, outage, explicit repair, remaining complete regions
    assert (
        sum(
            frame.get("ancestor_context") == requests[0]["ancestor_context"] and "fixed_semantic_verdict" not in frame
            for frame in requests
        )
        == 1
    )


@pytest.mark.parametrize(
    "body", ["**Unclosed emphasis.", "**Outer *crossed** markers*", "*cross\nline*", "cost_a_b_value"]
)
def test_unmatched_crossed_multiline_and_identifier_markup_is_not_erased(body):
    _, characters = curate._display_links(body)
    assert "".join(c for _, c in characters) == body


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.invalid/**admin**",
        "https://example.invalid/_enterprise_",
        "s3://bucket/**artifact**",
        "<https://example.invalid/_enterprise_>",
    ],
)
def test_bare_and_autolink_uri_identifiers_never_get_emphasis_rewritten(uri):
    _, characters = curate._display_links(uri)
    assert "".join(c for _, c in characters) == uri
    altered = uri.replace("**", "").replace("_", "")
    assert not curate._quote_spans(altered, uri)
    prose = "**Required:** use " + uri + " only."
    rendered = "Required: use " + uri + " only."
    assert curate._quote_spans(rendered, prose) == [(0, len(prose))]
