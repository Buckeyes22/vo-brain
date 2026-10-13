"""Explicit public products recover discovery without rewriting stored identity."""

import httpx
import pytest

from doclayer import discovery, gap_worker
from doclayer.coverage import Catalog
from doclayer.gaps import GapLedger
from doclayer.question_scope import public_discovery_allowed, public_subject

GLUETUN = "How does Gluetun use an existing custom OpenVPN .ovpn profile with OPENVPN_USER and OPENVPN_PASSWORD and OPENVPN_CUSTOM_CONFIG?"


def gap(question):
    return {
        "gap_id": "gap-public",
        "identity": "unknown:unidentified",
        "canonical_subject": None,
        "need_class": "missing_coverage",
        "representative_question": question,
        "source_candidates": [],
    }


@pytest.mark.parametrize(
    "question,subject",
    [
        (GLUETUN, "gluetun"),
        ("What does official pypdf documentation say about OCR?", "pypdf"),
    ],
)
def test_named_public_question_does_not_require_seed(question, subject):
    record = gap(question)
    assert public_subject(record) == subject
    assert public_discovery_allowed(record, Catalog(status="ok", revision="a" * 40, policy_version=1))
    assert record["identity"] == "unknown:unidentified"


@pytest.mark.parametrize(
    "question",
    [
        "What reports are available?",
        "How does the operator's UniFi configuration work?",
        "How does the operator’s UniFi configuration work?",
        "How does Gluetun use /home/user/private-vpn/profile.ovpn?",
    ],
)
def test_ambiguous_or_private_owner_never_searches(question, monkeypatch):
    record = gap(question)
    assert not public_discovery_allowed(record, Catalog(status="ok", revision="a" * 40, policy_version=1))
    monkeypatch.setattr(
        discovery,
        "_search_candidates",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("public search")),
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(AssertionError("external HTTP")))
    ) as client:
        result = discovery.discover(
            record,
            Catalog(status="ok", revision="a" * 40, policy_version=1),
            client=client,
            search_client=client,
        )
    assert result.requests_used == result.model_calls == 0
    assert result.seeds == []


def test_discovery_search_uses_extracted_product_without_mutating_input(monkeypatch):
    calls = []
    record = gap(GLUETUN)
    monkeypatch.setattr(
        discovery,
        "_search_candidates",
        lambda subject, question, **kwargs: calls.append((subject, question)) or [],
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(AssertionError("unexpected HTTP")))
    ) as client:
        result = discovery.discover(
            record,
            Catalog(status="ok", revision="a" * 40, policy_version=1),
            client=client,
            search_client=client,
        )
    assert calls == [("gluetun", GLUETUN)]
    assert result.requests_used == 1 and not result.seeds
    assert record["identity"] == "unknown:unidentified" and record["canonical_subject"] is None


def test_worker_reaches_real_discovery_with_derived_product_without_ledger_rewrite(tmp_path, monkeypatch):
    ledger = GapLedger(tmp_path / "gaps")
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject=None,
        representative_question=GLUETUN,
        identity_override="unknown:unidentified",
    )
    original_identity = row.identity
    calls = []
    monkeypatch.setattr(
        discovery,
        "_search_candidates",
        lambda subject, question, **kwargs: calls.append((subject, question)) or [],
    )
    actual = discovery.discover
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(AssertionError("unexpected HTTP")))
    ) as client:
        monkeypatch.setattr(
            gap_worker,
            "discover",
            lambda record, catalog, **kwargs: actual(record, catalog, client=client, search_client=client),
        )
        gap_worker._research_one(
            ledger,
            row,
            Catalog(status="ok", revision="a" * 40, policy_version=1, subjects={}),
            {},
            [],
        )
    current = ledger.load(row.gap_id)
    assert calls == [("gluetun", GLUETUN)]
    assert current.identity == original_identity and current.canonical_subject is None
    assert current.status == "retry_wait" and not current.job_links


def test_github_login_query_is_never_fetched(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "_search_candidates",
        lambda *a, **k: ["https://github.com/login?return_to=%2Fqdm12%2Fgluetun"],
    )
    fetched = []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: fetched.append(str(request.url)) or httpx.Response(200, text="Gluetun documentation")
        )
    ) as client:
        outcome = discovery.discover(
            gap(GLUETUN),
            Catalog(status="ok", revision="a" * 40, policy_version=1),
            client=client,
            search_client=client,
        )
    assert fetched == [] and outcome.seeds == [] and outcome.model_calls == 0


def test_github_family_cannot_transfer_official_authority_to_other_project():
    assert discovery._same_family(
        "https://github.com/qdm12/gluetun/wiki/OpenVPN",
        "https://github.com/qdm12/gluetun",
    )
    assert not discovery._same_family(
        "https://github.com/other/project/wiki/OpenVPN",
        "https://github.com/qdm12/gluetun",
    )
    assert not discovery._same_family(
        "https://raw.githubusercontent.com/qdm12/other/main/README.md",
        "https://raw.githubusercontent.com/qdm12/gluetun/main/README.md",
    )


@pytest.mark.parametrize("official", [False, True])
def test_github_blob_reads_source_bytes_but_still_requires_authority(monkeypatch, official):
    blob = "https://github.com/qdm12/gluetun/blob/main/README.md"
    raw = "https://raw.githubusercontent.com/qdm12/gluetun/main/README.md"
    fetched, reviews = [], []
    monkeypatch.setattr(discovery, "_search_candidates", lambda *a, **k: [blob])
    monkeypatch.setattr(discovery, "_qwen_available", lambda: True)
    monkeypatch.setattr(
        discovery,
        "_qwen_authoritative",
        lambda url, title, subject, question, body, **kw: reviews.append((url, subject, body)) or official,
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (
                fetched.append(str(request.url))
                or httpx.Response(
                    200,
                    text="Gluetun OpenVPN official documentation profile configuration",
                )
            )
        )
    ) as client:
        outcome = discovery.discover(
            gap(GLUETUN),
            Catalog(status="ok", revision="a" * 40, policy_version=1),
            client=client,
            search_client=client,
        )
    assert fetched == [raw]
    assert reviews == [(raw, "gluetun", "Gluetun OpenVPN official documentation profile configuration")]
    assert outcome.model_calls == 1
    assert bool(outcome.seeds) is official
    if official:
        assert outcome.seeds[0].url == raw and outcome.seeds[0].authority == "official"
