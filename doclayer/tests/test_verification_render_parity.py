"""Whole returned source survives renderer whitespace; omitted facts do not."""

import pytest

from doclayer.resolver import EVIDENCE_CHARS, Answer, answer_evidence
from doclayer.verification import _delivered_pages

PATH = "wiki/document-extractor.md"
REVISION = "a" * 40
BODY = "# Document Extractor\n\nLab edition only.\n## Image documents\nExtractor reads an existing text layer, cannot detect OCR failures, and does not perform OCR."


def delivery(context):
    answer = Answer(question="What does Document Extractor support?", subject="document-extractor")
    answer.git_facts = [
        {
            "fact": "Image documents",
            "value": "Extractor reads an existing text layer and does not perform OCR.",
            "source": PATH + "@" + REVISION[:8] + " · page text",
            "evidence_context": context,
        }
    ]
    pages = [
        {
            "path": PATH,
            "revision": REVISION,
            "body": BODY,
            "page_metadata": {"product": "document-extractor", "edition": "Lab"},
            "authority": {"path_area": "wiki"},
        }
    ]
    return answer, pages


@pytest.mark.parametrize("suffix", ["\n", "\n\n", "\n\n\n", "\n \n"])
def test_whole_context_with_trailing_blank_lines_keeps_original_qualifiers(suffix):
    context = BODY + suffix
    answer, pages = delivery(context)
    assert len(answer_evidence(answer)) == 1
    result = _delivered_pages(answer, pages)
    assert len(result) == 1
    assert result[0]["body"] == context  # Do not trim retained evidence.
    assert result[0]["page_metadata"] == pages[0]["page_metadata"]
    assert result[0]["revision"] == REVISION
    assert "Lab edition only." in result[0]["body"]
    assert "cannot detect OCR failures" in result[0]["body"]


@pytest.mark.parametrize(
    "omitted",
    ["Lab edition only.", "cannot detect OCR failures, and ", "does not perform OCR."],
)
def test_missing_substantive_rendered_bytes_cannot_become_delivered_evidence(monkeypatch, omitted):
    answer, pages = delivery(BODY + "\n\n")
    rendered = answer._prose()
    assert omitted in rendered
    monkeypatch.setattr(answer, "_prose", lambda: rendered.replace(omitted, "", 1))
    assert _delivered_pages(answer, pages) == []


@pytest.mark.parametrize("context", [None, "", " \n\n", "x" * (EVIDENCE_CHARS + 1)])
def test_empty_or_oversized_evidence_remains_withheld(context):
    answer, pages = delivery(context)
    assert _delivered_pages(answer, pages) == []


@pytest.mark.parametrize("source", ["wiki/other.md@" + REVISION[:8], PATH + "@" + "b" * 8, PATH])
def test_render_parity_does_not_relax_canonical_path_or_revision(source):
    answer, pages = delivery(BODY + "\n\n")
    answer.git_facts[0]["source"] = source
    assert _delivered_pages(answer, pages) == []
