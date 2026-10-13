"""Presentation aliases retain actual source destinations and provenance."""

import pytest

from doclayer.curator import curate
from doclayer.curator.state import Evidence


def test_rendered_link_restores_entire_actual_source():
    text = "# Card\nOnly with access, our [Framework](/rules?q=3) permits testing.\n"
    quote = "Only with access, our Framework permits testing."
    span = curate._quote_span(quote, text)
    assert span and text[slice(*span)] == "Only with access, our [Framework](/rules?q=3) permits testing."
    label = curate._quote_span("Framework", text)
    assert label and text[slice(*label)] == "[Framework](/rules?q=3)"
    assert not curate._quote_span("our [Framework](/wrong) permits testing.", text)
    assert not curate._quote_span("Only without access, our Framework permits testing.", text)


def test_display_ambiguity_and_locator():
    text = "# Page 1\nUse [Framework](/first).\n# Page 2\nUse Framework.\n"
    assert curate._quote_span("Use Framework.", text) is None
    span = curate._quote_span("Use Framework.", text, "Page 1")
    assert span and "/first" in text[slice(*span)]
    links = "# Page 1\nUse [Framework](/first).\n# Page 2\nUse [Framework](/second).\n"
    assert curate._quote_span("Use Framework.", links) is None


@pytest.mark.parametrize(
    "text",
    [
        "```markdown\nUse [Framework](/rules).\n```\n",
        "~~~markdown\nUse [Framework](/rules).\n~~~~\n",
        "`Use [Framework](/rules).`\n",
        "# Manual\n\n    Use [Framework](/rules).\n",
        "# Manual\n\n\tUse [Framework](/rules).\n",
    ],
)
def test_code_is_not_rendered_as_prose(text):
    assert curate._quote_span("Use Framework.", text) is None
    assert curate._quote_span("Use [Framework](/rules).", text)


def test_balanced_destination_and_numerics():
    text = "Only version 3 permits [test](/rules(v3)).\n"
    span = curate._quote_span("Only version 3 permits test.", text)
    assert span and text[slice(*span)] == text.strip()
    assert not curate._quote_span("Only version 4 permits test.", text)


def test_governing_qualifiers_and_cross_unit_refusal():
    text = "# Manual\nLaboratory only.\n## Device\nUse [Framework](/rules).\n"
    record = Evidence("s", "manual", "Device", "quote", "Use Framework.", quote="Use Framework.")
    excerpt = curate.support_excerpt(record, text)
    assert "Laboratory only." in excerpt and "[Framework](/rules)" in excerpt
    record.quote = "Laboratory only. ## Device Use Framework."
    record.locator = ""
    assert not curate.support_excerpt(record, text)


@pytest.mark.asyncio
async def test_previous_positive_does_not_bypass_new_ambiguity():
    text = "# Card\nUse [Framework](/one).\nUse [Framework](/two).\n"
    record = Evidence("s", "card", "Card", "quote", "Use Framework.", quote="Use Framework.")
    key = curate.evidence_id(record)
    known = {
        key: {
            "support_version": 11,
            "source_binding": curate.source_support_binding(record, {"s": text}),
            "supported": True,
            "status": "supported",
            "verification_pending": False,
        }
    }

    class NoModel:
        async def chat_json(self, *args, **kwargs):
            pytest.fail("ambiguous support must not be guessed")

    result = await curate.verify_source_claims(NoModel(), [record], {"s": text}, char_budget=12000, known=known)
    assert curate.SOURCE_SUPPORT_VERSION == 12
    assert result[key]["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition,claim",
    [
        ("Only laboratory deployments with signed access may output images.", "Device outputs images."),
        ("Provider B enterprise version 3 alone supports exports.", "Provider A supports exports."),
        ("Evaluations only: the simulated device outputs images.", "Device outputs images."),
    ],
)
async def test_short_source_gate_explicitly_binds_governing_conditions(condition, claim):
    text = "# Scope\n" + condition + "\n## Device\n" + claim + "\n"
    record = Evidence("s", "manual", "Device", "quote", claim, quote=claim)

    class Client:
        async def chat_json(self, system, user):
            import json

            assert "child statement or literal matching sentence does NOT establish" in system
            assert "hypothetical/evaluation-only" in system
            item = json.loads(user.split("\n", 1)[1])[0]
            assert condition in item["excerpt"]
            return {"verdicts": [{"id": item["id"], "supported": False, "reason": "Missing governing condition."}]}

    result = await curate.verify_source_claims(Client(), [record], {"s": text}, char_budget=12000)
    assert result[curate.evidence_id(record)]["status"] == "unsupported"
