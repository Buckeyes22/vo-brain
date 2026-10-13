"""Classification regressions from the assumption audit (2026-10-10, lane-c causes G and H).

Public library/product questions must reach public discovery, never the private
route; infrastructure documentation needs qualify whatever word form they use.
"""

import pytest

from doclayer.gaps import GapLedger
from doclayer.local_material import is_local_need
from doclayer.question_scope import (
    infrastructure_documentation_need,
    private_question,
    public_discovery_allowed,
    public_subject,
)

# Verbatim questions of gaps 017a4e02 and 05b522a6 (both captured as unknown:unidentified).
PYPDF = (
    "What does official pypdf documentation state about image-only scanned PDFs without OCR, "
    "OCR text layers, and why pypdf cannot extract text from images? Return exact source "
    "references and applicability."
)
GLUETUN = (
    "How does Gluetun use an existing custom OpenVPN .ovpn profile with OPENVPN_USER and "
    "OPENVPN_PASSWORD and OPENVPN_CUSTOM_CONFIG?"
)


@pytest.mark.parametrize(
    "question,need_class,subject",
    [(PYPDF, "infrastructure", "pypdf"), (GLUETUN, "missing_coverage", "gluetun")],
)
def test_public_library_and_product_questions_are_not_routed_private(tmp_path, question, need_class, subject):
    gap = {
        "identity": "unknown:unidentified",
        "canonical_subject": None,
        "representative_question": question,
        "need_class": need_class,
        "source_candidates": [],
    }
    assert not private_question(question)
    assert public_subject(gap) == subject
    assert public_discovery_allowed(gap)
    record, _ = GapLedger(tmp_path / "gaps").submit_event(
        need_class=need_class,
        subject="unidentified",
        representative_question=question,
    )
    assert record.identity == "unknown:unidentified"
    assert not is_local_need(record)


@pytest.mark.parametrize(
    "question",
    [
        "What documented capabilities does the local example-tui platform expose through its HTTP API and PWA?",
        "What documentation covers the example-tui HTTP API and PWA?",
        "What docs describe the example-tui API?",
        "What documented operator recovery procedure applies to app-db when /mnt/fast exhausts free space?",
        "Where is acli installed on SVC1?",
        "How do I install acli on SVC1?",
        "What is the installation path of acli on SVC1?",
        "How is app-coach deployed?",
        "Which deployments run on prod?",
        "What configuration does Caddy use on prod?",
        "How is Caddy configured on prod?",
    ],
)
def test_infrastructure_documentation_word_forms_qualify(question):
    assert infrastructure_documentation_need(question)


@pytest.mark.parametrize(
    "question",
    [
        "Why is app-db down right now?",
        "Is App1 reachable?",
        "Prometheus alert fired for high CPU on SVC1",
    ],
)
def test_availability_incidents_do_not_qualify(question):
    assert not infrastructure_documentation_need(question)
