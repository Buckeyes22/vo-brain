"""A committed catalog subject names the question's subject by a complete phrase.

"Which options are overridden in the brain curator configuration?" says the
title of the committed subject brain-curator-config verbatim, but guess_subject
only knew sources/ directories and product/account pages, so the subject was
None and an unrelated gluetun page's "Overridden options" won the ranking.
"""

from __future__ import annotations

from doclayer import resolver
from doclayer.coverage import Catalog, SubjectEntry


def _repo(tmp_path):
    class FakeRepo:
        root = tmp_path

        def markdown_paths(self):
            return []

        def read_page(self, path):
            raise AssertionError("no pages")

    return FakeRepo()


def _catalog(*entries, status="ok"):
    return Catalog(status=status, revision="r", policy_version=1, subjects={entry.slug: entry for entry in entries})


CURATOR = SubjectEntry(slug="brain-curator-config", kind="service", title="Brain Curator Configuration")
RUNBOOK = SubjectEntry(slug="brain-curator-runbook", kind="procedure", title="Brain Curator Runbook")


def test_a_complete_catalog_title_names_the_subject(tmp_path):
    question = "Which options are overridden in the brain curator configuration?"
    assert resolver.guess_subject(_repo(tmp_path), question, None, _catalog(CURATOR, RUNBOOK)) == "brain-curator-config"


def test_a_title_fragment_does_not_name_the_subject(tmp_path):
    assert (
        resolver.guess_subject(_repo(tmp_path), "How is the brain curator deployed?", None, _catalog(CURATOR, RUNBOOK))
        is None
    )


def test_a_tie_between_catalog_subjects_names_none(tmp_path):
    twin = SubjectEntry(slug="curator-config-copy", kind="service", title="Brain Curator Configuration")
    question = "What is the brain curator configuration?"
    assert resolver.guess_subject(_repo(tmp_path), question, None, _catalog(CURATOR, twin)) is None


def test_only_a_current_catalog_and_committed_subjects_count(tmp_path):
    question = "Which options are overridden in the brain curator configuration?"
    assert resolver.guess_subject(_repo(tmp_path), question, None, _catalog(CURATOR, status="stale")) is None
    policy_only = SubjectEntry(
        slug="brain-curator-config", kind="service", title="Brain Curator Configuration", page_status="policy"
    )
    assert resolver.guess_subject(_repo(tmp_path), question, None, _catalog(policy_only)) is None


def test_a_single_word_catalog_name_does_not_name_the_subject(tmp_path):
    forgejo = SubjectEntry(slug="forgejo", kind="service", title="Forgejo")
    assert resolver.guess_subject(_repo(tmp_path), "Is Forgejo installed?", None, _catalog(forgejo)) is None


# A catalog phrase in a non-subject role, or a generic catalog alias, must
# not become the subject. Each case below has no subject and must keep none.

REAL = _catalog(
    CURATOR,
    RUNBOOK,
    SubjectEntry(slug="agent-runtime-gateway", kind="service", title="Agent-runtime Gateway"),
    SubjectEntry(
        slug="gluetun-custom-openvpn-configuration", kind="procedure", title="Gluetun Custom OpenVPN Configuration"
    ),
    SubjectEntry(
        slug="openai-model-lineup", kind="model-family", title="OpenAI Model Lineup", aliases=["model lineup"]
    ),
    SubjectEntry(slug="fleet-hosts", kind="host-inventory", title="Fleet hosts"),
    SubjectEntry(slug="openai-api-tools", kind="product", title="OpenAI API Tools", aliases=["api tools"]),
    SubjectEntry(
        slug="openai-api-performance-modes",
        kind="product",
        title="OpenAI API Performance Modes",
        aliases=["performance modes"],
    ),
    SubjectEntry(
        slug="gpt-6-prompting-guidance",
        kind="procedure",
        title="GPT-6 Prompting Guidance",
        aliases=["prompting guidance"],
    ),
    SubjectEntry(slug="litellm-hub", kind="service", title="LiteLLM Hub"),
    SubjectEntry(slug="forgejo", kind="service", title="Forgejo"),
    SubjectEntry(slug="Gpu1", kind="host", title="Gpu1", page_status="policy"),
    SubjectEntry(slug="laptop-1-gpu-verification-runtime", kind="general", title="Laptop 1 GPU Verification Runtime"),
    SubjectEntry(slug="vo-rag", kind="service", title="vo-rag", page_status="policy"),
    SubjectEntry(slug="vo-rag-retrieval-architecture", kind="general", title="vo-rag Retrieval Architecture"),
)


import pytest  # noqa: E402


@pytest.mark.parametrize(
    "question",
    [
        # Agent-runtime memory provider: the curator phrase is the object, Agent-runtime the subject.
        "Does the Agent-runtime Gateway memory provider read the brain curator configuration?",
        # Gluetun comparison: two named subjects, neither wins by length.
        "Compare the gluetun custom OpenVPN configuration with the brain curator configuration",
        # Qwen lineup: "model lineup" is a generic alias of the OpenAI lineup page.
        "What is the Qwen model lineup on Gpu1?",
        # Fleet hosts Docker: a two-word slug is too short to name the subject.
        "Which fleet hosts run Docker?",
        # UniFi API tools: "api tools" is a generic alias of the OpenAI tools page.
        "Which API tools does the UniFi MCP server expose?",
        # Dell performance modes: "performance modes" is a generic alias.
        "What performance modes does the Laptop 1 support?",
        # Anthropic prompting: "prompting guidance" is a generic alias.
        "What prompting guidance does Anthropic give for Claude Opus?",
        # Two-subject tie: each phrase names a different committed subject.
        "What is the brain curator runbook versus the brain curator config?",
        # Another named subject outside the phrase.
        "What port does Forgejo listen on, and which Forgejo URL does the brain curator configuration use?",
        "Which LiteLLM hub port does the brain curator configuration point at?",
        # The same subject named again outside the matched phrase.
        "Which vo-rag collection does the vo-rag retrieval architecture page describe?",
    ],
)
def test_a_catalog_phrase_in_a_non_subject_role_names_no_subject(tmp_path, question):
    assert resolver.guess_subject(_repo(tmp_path), question, None, REAL) is None


def test_the_curator_and_dell_runtime_questions_keep_their_subjects(tmp_path):
    curator = "Which options are overridden in the brain curator configuration?"
    assert resolver.guess_subject(_repo(tmp_path), curator, None, REAL) == "brain-curator-config"
    dell = "What is the Laptop 1 GPU verification runtime?"
    assert resolver.guess_subject(_repo(tmp_path), dell, None, REAL) == "laptop-1-gpu-verification-runtime"


def test_aliases_never_name_a_catalog_subject(tmp_path):
    lineup = SubjectEntry(
        slug="openai-model-lineup",
        kind="model-family",
        title="OpenAI Model Lineup",
        aliases=["the full openai model family lineup"],
    )
    question = "What is the full openai model family lineup?"
    assert resolver.guess_subject(_repo(tmp_path), question, None, _catalog(lineup)) is None
