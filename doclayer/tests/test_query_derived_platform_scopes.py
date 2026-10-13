"""Old query parser artifacts cannot become invented hosting constraints."""

import hashlib
import json

import httpx
import pytest
from test_verification_contract import (  # noqa: F401
    ModelClient,
    client,
    git,
    positive,
    published,
)

from doclayer import applicability, verification
from doclayer.applicability import (
    effective_question_scopes,
    matches_scope,
    requested_scopes,
    source_scope,
)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What is the canonical Nimbus production deployment topology on OneCloud as of now?", "OneCloud"),
        ("Which is the current Nimbus deployment configuration on One Cloud as of now?", "One Cloud"),
        (
            "For authorized repairs on OneCloud after deployed abcdef123, what is the embedding identity route?",
            "OneCloud",
        ),
        ("What was installed on OneCloud before release v2?", "OneCloud"),
    ],
)
def test_temporal_and_interrogative_words_are_not_platform_identities(question, expected):
    scopes = requested_scopes(question, "Nimbus")
    assert [s["value"] for s in scopes if s["dimension"] == "platform"] == [expected]


def test_real_leading_host_and_edition_version_region_constraints_remain():
    assert requested_scopes("OneCloud deployment of Model A", "Model A") == [
        {"dimension": "platform", "value": "OneCloud"}
    ]
    question = "What is the canonical Nimbus production deployment topology on OneCloud as of now?"
    original = [
        {"dimension": "platform", "value": "OneCloud as of"},
        {"dimension": "platform", "value": "canonical Nimbus production"},
        {"dimension": "platform", "value": "OtherHost"},
        {"dimension": "edition", "value": "Enterprise"},
        {"dimension": "version", "value": "v3"},
        {"dimension": "region", "value": "east"},
    ]
    effective = effective_question_scopes(question, original)
    assert effective == [{"dimension": "platform", "value": "OneCloud"}, *original[2:]]
    assert original[0]["value"] == "OneCloud as of"
    assert not matches_scope(
        source_scope({"platform": "OneCloud", "edition": "Enterprise", "version": "v3", "region": "east"}), effective
    )


def test_unrelated_independently_supplied_constraint_is_not_reparsed_or_discarded():
    scopes = [
        {"dimension": "platform", "value": "OneCloud after deployed"},
        {"dimension": "provider", "value": "PrivateProvider"},
    ]
    assert effective_question_scopes("What is the Nimbus rate?", scopes) == scopes
    assert effective_question_scopes("OneCloud deployment of Nimbus", scopes) == scopes


def test_actual_publication_verification_preserves_original_constraints_and_policy_binding(
    published, tmp_path, monkeypatch
):  # noqa: F811
    root, path, _, ledger, record, reply, index = published
    source = root / path
    source.write_text(source.read_text().replace("product: nimbus", "product: nimbus\nplatform: OneCloud"))
    git(root, "add", path)
    git(root, "commit", "-m", "Establish exact hosting metadata")
    commit = git(root, "rev-parse", "HEAD")
    reply["job"]["publication"]["merge_commit"] = commit
    index["last_commit"] = commit
    index["files"][path] = hashlib.sha256(source.read_bytes()).hexdigest()
    record.representative_question = (
        "What is the canonical Nimbus production deployment topology and rate on OneCloud as of now?"
    )
    record.applicability = [
        {"dimension": "platform", "value": "OneCloud as of"},
        {"dimension": "platform", "value": "canonical Nimbus production"},
    ]
    ledger.save(record)
    before = list(record.applicability)
    assert not matches_scope(source_scope({"platform": "OneCloud"}), before)
    model, usage = client(tmp_path, positive(path))
    frames = []

    def http(request):
        frames.append(json.loads(json.loads(request.content)["messages"][-1]["content"]))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(positive(path))}, "finish_reason": "stop"}]}
        )

    model = ModelClient(model.cfg, usage, transport=httpx.MockTransport(http))
    result = verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=model)
    assert result.outcome == "resolved" and usage.model_calls == 2
    assert len(frames) == 2
    assert all(
        frame["question"] == record.representative_question
        and frame["applicability"] == [{"dimension": "platform", "value": "OneCloud"}]
        for frame in frames
    )
    saved = ledger.load(record.gap_id)
    assert saved.applicability == before and saved.representative_question == record.representative_question
    first_binding = saved.resolution_verification["canonical_checkpoint"]["binding"]
    monkeypatch.setattr(applicability, "QUERY_SCOPE_POLICY", applicability.QUERY_SCOPE_POLICY + 1)
    fresh, new_usage = client(tmp_path, positive(path))
    assert verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=fresh).outcome == "resolved"
    assert new_usage.model_calls == 2
    assert ledger.load(record.gap_id).resolution_verification["canonical_checkpoint"]["binding"] != first_binding


@pytest.mark.parametrize("owner", ["Alex", "Morgan"])
def test_named_workstation_owner_is_not_a_platform_but_named_machine_disk_remains(owner):
    question = f"What OS and desktop GPU are installed on {owner}'s current local desktop workstation?"
    assert requested_scopes(question) == []
    original = [{"dimension": "platform", "value": owner}, {"dimension": "edition", "value": "Enterprise"}]
    assert effective_question_scopes(question, original) == [original[1]]
    machine_question = f"What is installed on {owner}'s root disk?"
    assert requested_scopes(machine_question) == [{"dimension": "platform", "value": owner}]
    assert effective_question_scopes(machine_question, original) == original


@pytest.mark.parametrize("machine", ["App1", "ws1", "SVC1", "Gpu1"])
def test_machine_identifiers_workstation_clause_remain_host_constraints(machine):
    question = f"What is installed on {machine}'s current workstation?"
    original = [{"dimension": "platform", "value": machine}]
    assert requested_scopes(question) == original
    assert effective_question_scopes(question, original) == original


@pytest.mark.parametrize(
    "question",
    [
        "Status of the Responses API steering events",
        "Overview of the Responses API steering events",
        "current state of the Responses API migration from Chat Completions",
    ],
)
def test_filler_stripped_api_captures_never_become_platform_filters(question):
    assert [s for s in requested_scopes(question) if s["dimension"] == "platform"] == []


def test_every_platform_capture_is_literal_in_the_question():
    question = "How do I deploy the vo-rag production deployment on App1?"
    flat = " ".join(question.split())
    scopes = requested_scopes(question)
    assert [s["value"] for s in scopes] == ["App1"]
    assert all(s["value"] in flat for s in scopes)


@pytest.mark.parametrize(
    "question",
    [
        "How is the proxy configured on the Azure api gateway?",
        "What is the limit on the Foundry the api layer for chat?",
        "Status of the Responses API steering events",
    ],
)
def test_lowercase_on_captures_keep_the_questions_own_words(question):
    flat = " ".join(question.split())
    assert all(s["value"] in flat for s in requested_scopes(question) if s["dimension"] == "platform")
