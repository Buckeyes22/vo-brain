"""A storage location or output modality is not a capacity measurement."""

import hashlib

import pytest
from test_verification_contract import client, git, published  # noqa: F401

from doclayer import verification
from doclayer.verification import _validate_semantic


@pytest.mark.parametrize(
    "question,facet,quote",
    [
        (
            "Which device and filesystem back Nimbus storage?",
            "storage",
            "Nimbus storage uses /dev/sdb1 with an ext4 filesystem.",
        ),
        ("Where are Nimbus durable jobs stored?", "storage", "Nimbus stores durable jobs in /var/lib/nimbus/jobs."),
        (
            "Does Nimbus produce pictures natively or through a tool?",
            "output",
            "Nimbus produces text output; picture generation uses a separate tool.",
        ),
        (
            "How does Nimbus preserve conversation context?",
            "context",
            "Nimbus preserves context through a compacted continuation record.",
        ),
    ],
)
def test_literal_nonnumeric_fact_is_valid_semantic_evidence(question, facet, quote):
    page = {"path": "wiki/nimbus.md", "body": quote, "page_metadata": {}}
    verdict = {
        "answers": True,
        "facets": [
            {
                "facet": facet,
                "answered": True,
                "scope_preserved": True,
                "evidence": [{"path": page["path"], "quote": quote}],
            }
        ],
        "reason": "The literal source answers the requested nonnumeric fact.",
    }
    assert _validate_semantic(verdict, [facet], [], [page], question=question) == verdict


@pytest.mark.parametrize(
    "question,facet,quote",
    [
        (
            "How much storage capacity does Nimbus have?",
            "storage",
            "Nimbus storage uses /dev/sdb1 with an ext4 filesystem.",
        ),
        (
            "How many output tokens can Nimbus generate?",
            "output",
            "Nimbus produces text output through its native interface.",
        ),
        (
            "What is Nimbus context window size?",
            "context",
            "Nimbus preserves context through a compacted continuation record.",
        ),
        ("What is Nimbus rate limit?", "rate", "Nimbus request rate limits are documented."),
    ],
)
def test_nonnumeric_fact_cannot_answer_a_quantity(question, facet, quote):
    page = {"path": "wiki/nimbus.md", "body": quote, "page_metadata": {}}
    verdict = {
        "answers": True,
        "facets": [
            {
                "facet": facet,
                "answered": True,
                "scope_preserved": True,
                "evidence": [{"path": page["path"], "quote": quote}],
            }
        ],
        "reason": "An unsupported positive model verdict cannot replace a quantity.",
    }
    # the row without a quantity is downgraded to unanswered; the reply is not rejected
    out = _validate_semantic(verdict, [facet], [], [page], question=question)
    assert out is not None and out["answers"] is False
    assert out["facets"][0]["answered"] is False and out["facets"][0]["evidence"] == []


def test_nonnumeric_scope_still_rejects_nonliteral_and_foreign_quotes():
    quote = "Nimbus stores durable jobs in /var/lib/nimbus/jobs."
    page = {"path": "wiki/nimbus.md", "body": quote, "page_metadata": {"applicability": {"platform": "OneCloud"}}}
    verdict = {
        "answers": True,
        "facets": [
            {
                "facet": "storage",
                "answered": True,
                "scope_preserved": True,
                "evidence": [{"path": page["path"], "quote": quote}],
            }
        ],
        "reason": "Actual scope remains required.",
    }
    assert (
        _validate_semantic(
            verdict,
            ["storage"],
            [{"dimension": "platform", "value": "OtherCloud"}],
            [page],
            question="Where are jobs stored on OtherCloud?",
        )
        is None
    )
    verdict["facets"][0]["evidence"][0]["quote"] = "Nimbus uses a database for jobs."
    assert _validate_semantic(verdict, ["storage"], [], [page], question="Where are jobs stored?") is None


def test_storage_location_closes_through_both_charged_frames_and_cached_revalidation(
    published,
    tmp_path,  # noqa: F811
):
    root, path, _, ledger, record, reply, index = published
    quote = "Nimbus storage persists durable jobs in /var/lib/nimbus/jobs."
    (root / path).write_text("---\ntitle: Nimbus\nproduct: nimbus\n---\n# Nimbus\n## Storage\n" + quote + "\n")
    git(root, "add", path)
    git(root, "commit", "-m", "Document durable storage location")
    index["last_commit"] = git(root, "rev-parse", "HEAD")
    index["files"][path] = hashlib.sha256((root / path).read_bytes()).hexdigest()
    record.representative_question = "Where are Nimbus durable jobs stored?"
    record.facets = ["storage"]
    ledger.save(record)
    verdict = {
        "answers": True,
        "facets": [
            {
                "facet": "storage",
                "answered": True,
                "scope_preserved": True,
                "evidence": [{"path": path, "quote": quote}],
            }
        ],
        "reason": "The source gives the actual durable location.",
    }
    model, usage = client(tmp_path, verdict)
    result = verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=model)
    assert result.outcome == "resolved" and usage.model_calls == 2
    assert ledger.load(record.gap_id).status == "resolved"
    fresh, new_usage = client(tmp_path, verdict)
    result = verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=fresh)
    assert result.outcome == "resolved" and new_usage.model_calls == 1
