"""Evidence admission matches the real renderer's trailing whitespace only."""

import pytest

from doclayer import resolver


def answer(context):
    result = resolver.Answer("How much output can Nimbus generate?", None)
    result.git_facts = [
        {
            "fact": "Nimbus",
            "value": "Nimbus produces at most 100 tokens of output.",
            "source": "wiki/nimbus.md@aaaaaaaa",
            "evidence_context": context,
        }
    ]
    return result


@pytest.mark.parametrize("suffix", ["\n", "\n\n", "\n\n\n", "  \n\n"])
def test_complete_qualified_quote_with_final_blank_lines_is_admitted(suffix):
    context = "# Nimbus\nEnterprise edition only.\nNimbus produces at most 100 tokens of output." + suffix
    result = answer(context)
    rendered = result._prose()
    assert "> Enterprise edition only." in rendered
    evidence = resolver.answer_evidence(result)
    assert len(evidence) == 1 and evidence[0]["context"] == context
    assert evidence[0]["facets"] == ["output"]
    assert "answered" in resolver._dispositions(result, lag_flags=[], called={"brain_git": True})


@pytest.mark.parametrize("context", ["", "\n \n", "x" * (resolver.EVIDENCE_CHARS + 1)])
def test_empty_or_oversized_context_remains_withheld(context):
    assert not resolver.answer_evidence(answer(context))


def test_missing_middle_qualification_is_not_render_parity(monkeypatch):
    context = "# Nimbus\nEnterprise edition only.\nNimbus produces at most 100 tokens of output.\n\n"
    result = answer(context)
    monkeypatch.setattr(result, "_prose", lambda: "> # Nimbus\n> Nimbus produces at most 100 tokens of output.")
    assert not resolver.answer_evidence(result)


def test_missing_final_fact_is_not_render_parity(monkeypatch):
    result = answer("# Nimbus\nEnterprise edition only.\nNimbus produces at most 100 tokens of output.\n\n")
    monkeypatch.setattr(result, "_prose", lambda: "> # Nimbus\n> Enterprise edition only.")
    assert not resolver.answer_evidence(result)


def test_nonnumeric_native_output_distinction_does_not_require_token_quantity():
    context = "# Nimbus\nEnterprise edition only.\nNimbus accepts image input and produces text output natively. Picture generation uses a separate image tool.\n\n"
    result = resolver.Answer("Can Nimbus generate pictures itself, or use a separate tool?", None)
    result.git_facts = [
        {
            "fact": "Nimbus",
            "value": context.splitlines()[2],
            "source": "wiki/nimbus.md@aaaaaaaa",
            "evidence_context": context,
        }
    ]
    evidence = resolver.answer_evidence(result)
    assert len(evidence) == 1 and evidence[0]["context"] == context
    assert evidence[0]["facets"] == ["output"]


def test_numeric_output_question_still_requires_actual_quantity():
    result = answer("# Nimbus\nNimbus produces text output.\n\n")
    result.git_facts[0]["value"] = "Nimbus produces text output."
    assert not resolver.answer_evidence(result)
    assert "answered" not in resolver._dispositions(result, lag_flags=[], called={"brain_git": True})
