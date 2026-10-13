"""Retained grammar fragments cannot identify a public discovery target."""

import httpx
import pytest

from doclayer import discovery
from doclayer.coverage import Catalog
from doclayer.question_scope import public_discovery_allowed, public_subject

QUESTION = 'can you have luna subagents look for any code or documentation about any sort of "code rag" system'


@pytest.mark.parametrize("identity", ["unknown:sort of", "unknown:unidentified"])
def test_observed_generic_code_rag_question_never_requests_public_sources(identity, monkeypatch):
    record = {
        "identity": identity,
        "representative_question": QUESTION,
        "need_class": "missing_coverage",
        "source_candidates": [],
    }
    catalog = Catalog(status="ok", revision="a" * 40, policy_version=1)
    assert public_subject(record) == ""
    assert not public_discovery_allowed(record, catalog)
    monkeypatch.setattr(
        discovery,
        "_search_candidates",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("placeholder search")),
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(AssertionError("external HTTP")))
    ) as client:
        result = discovery.discover(record, catalog, client=client, search_client=client)
    assert result.requests_used == result.model_calls == 0 and not result.seeds
    assert record["identity"] == identity


@pytest.mark.parametrize(
    "question,expected",
    [
        ("How does Gluetun use an existing custom OpenVPN profile?", "gluetun"),
        ("What does official pypdf documentation say about OCR?", "pypdf"),
        ("docs for Widget", "widget"),
    ],
)
def test_explicit_named_product_can_replace_placeholder_target_without_identity_rewrite(question, expected):
    record = {
        "identity": "unknown:sort of",
        "representative_question": question,
        "need_class": "missing_coverage",
    }
    assert public_subject(record) == expected
    assert public_discovery_allowed(record)
    assert record["identity"] == "unknown:sort of"


@pytest.mark.parametrize(
    "identity,question",
    [
        (
            "unknown:laptop 1",
            "What is the verified Laptop-1 Android adb/capture access route?",
        ),
        ("unknown:widget", "What reports are available?"),
    ],
)
def test_unknown_identity_alone_cannot_validate_public_question(identity, question):
    record = {
        "identity": identity,
        "representative_question": question,
        "need_class": "missing_coverage",
        "source_candidates": [{"url": "https://docs.example/", "authority": "official"}],
    }
    assert public_subject(record) == ""
    assert not public_discovery_allowed(record)


def test_known_public_catalog_subject_validates_unknown_capture():
    from doclayer.coverage import SubjectEntry

    catalog = Catalog(status="ok", revision="a" * 40, policy_version=1)
    catalog.subjects["widget"] = SubjectEntry(slug="widget", title="Widget", kind="product")
    record = {
        "identity": "unknown:widget",
        "representative_question": "What retry options are available?",
        "need_class": "missing_coverage",
    }
    assert public_subject(record, catalog) == "widget"
    assert public_discovery_allowed(record, catalog)


def test_unrelated_official_seed_cannot_recognize_invalid_extracted_subject(
    monkeypatch,
):
    record = {
        "identity": "unknown:sort of",
        "representative_question": QUESTION,
        "need_class": "missing_coverage",
        "source_candidates": [{"url": "https://docs.keystatic.example/", "authority": "official"}],
    }
    catalog = Catalog(status="ok", revision="a" * 40, policy_version=1)
    assert not public_discovery_allowed(record, catalog)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError("unrelated official source fetched"))
        )
    ) as client:
        result = discovery.discover(record, catalog, client=client, search_client=client)
    assert result.requests_used == result.model_calls == 0
    assert result.seeds == []
