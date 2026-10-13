"""Unknown capture labels cannot filter out real published answer evidence."""

from types import SimpleNamespace

import pytest
from test_verification_contract import client, positive
from test_verification_contract import published as published

from doclayer import resolver, verification
from doclayer.coverage import Catalog, SubjectEntry
from doclayer.gap_budget import ResourceBudget


@pytest.mark.parametrize(
    "identity,canonical,question,known,expected",
    [
        (
            "unknown:unidentified",
            None,
            "How does Widget use custom configuration?",
            None,
            "widget",
        ),
        (
            "unknown:sort of",
            None,
            'can you have luna subagents look for any code or documentation about any sort of "code rag" system',
            None,
            None,
        ),
        ("unknown:unidentified", None, "What reports are available?", None, None),
        ("unknown:brain", None, "Which model handles wiki curation?", "brain", "brain"),
        ("unknown:unidentified", "widget", "What options exist?", None, "widget"),
    ],
)
def test_hint_uses_validated_subject_not_placeholder(monkeypatch, identity, canonical, question, known, expected):
    catalog = Catalog(status="ok", revision="a" * 40, policy_version=1)
    if known:
        catalog.subjects[known] = SubjectEntry(slug=known, title=known, kind="procedure", local_first=True)
    monkeypatch.setattr("doclayer.coverage.build_catalog", lambda *a, **kw: catalog)
    record = SimpleNamespace(identity=identity, canonical_subject=canonical, representative_question=question)
    record.to_dict = lambda: dict(record.__dict__)
    repo = SimpleNamespace(root="unused", head_commit=lambda: "a" * 40)
    assert verification._resolver_product_hint(record, repo) == expected


def test_unknown_public_capture_uses_ordinary_published_evidence(published, tmp_path, monkeypatch):
    root, path, commit, ledger, record, reply, index = published
    record.identity = "unknown:unidentified"
    record.canonical_subject = None
    record.representative_question = "How does Nimbus expose its rate limit?"
    ledger.save(record)
    original = resolver.resolve
    calls = []

    async def observe(question, **kwargs):
        calls.append(kwargs["product"])
        assert kwargs["capture"] is False and kwargs["force"] is True
        return await original(question, **kwargs)

    monkeypatch.setattr(resolver, "resolve", observe)
    model, usage = client(tmp_path, positive(path))
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    assert calls == [None], result
    assert result.outcome == "resolved" and usage.model_calls == 2
    assert ledger.load(record.gap_id).identity == "unknown:unidentified"
