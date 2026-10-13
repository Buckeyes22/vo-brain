"""Question fragments never become strict platform filters.

The questions are the real gap questions recorded in the ledger; previously each produced a
platform scope that no wiki page could satisfy.
"""

import pytest

from doclayer import applicability
from doclayer.applicability import effective_question_scopes, requested_scopes


@pytest.fixture(autouse=True)
def known(identities):
    """Recognized identities: declared hosts plus catalog subjects."""
    identities(
        hosts=["App1", "Gpu1", "worker1", "prod"],
        subjects=["vo-rag", "Nimbus Weather App", "nimbus-metar-taf-application-app-1", "openai-responses-api"],
    )


def platforms(scopes):
    return [s["value"] for s in scopes if s["dimension"] == "platform"]


GAP_QUESTIONS = {
    "0b8f978a": (
        "What are the documented read-only access routes and deployment locations for app-intel on App1, including app-db, app_bulk and the coach app?",
        ["App1"],
    ),
    "19c48300": (
        "Nimbus METAR product deployment: App1 installed checkout, API/frontend access and current release workflow",
        [],
    ),
    "4031a309": (
        "For Nimbus METAR product deployment on App1, what are the installed checkout, API/frontend access and existing release workflow? Read-only evidence lookup; preserve current TAF release.",
        ["App1"],
    ),
    "609c098f": (
        "Which existing Nimbus TAF acquisition and stored-source paths on App1 provide cutoff-bound sensor or station-move transition evidence for Columbus at Dayton (station 72429, 2026 season)? I need to distinguish existing but unjoined evidence from missing evidence for the current station_change reason; do not infer unchanged stations or METAR source authority.",
        ["App1"],
    ),
    "6430ad2d": (
        "Which existing authorized stored TAF NWS sweep-grain station tables or sources provide true station target, route, and precipitation sample denominators, keyed to NWS sweep/station IDs, with ingestion receipt clocks? This is for the selected Columbus–Dayton nowcast handoff on App1. Distinguish season aggregates and gauge sums from authoritative sweep/station denominator records; do not infer absence from catalog coverage.",
        ["App1"],
    ),
    "f08bec1c": (
        "What existing deployment and rollback route serves Nimbus METAR API, Dagster producer, and frontend on App1?",
        ["App1"],
    ),
    "fb90182e": (
        "What are the documented Nimbus TAF schedule and live station coverage access routes on App1 for selecting the next upcoming storm, and its Redis publication memory configuration?",
        ["App1"],
    ),
    "1f683284": (
        "What is the canonical vo-rag production deployment topology and storage/cache override configuration on App1 as of now? Read only; do not investigate or close existing Doclayer gaps.",
        ["App1"],
    ),
}


@pytest.mark.parametrize(("gap", "question", "expected"), [(g, q, e) for g, (q, e) in GAP_QUESTIONS.items()])
def test_gap_question_platform_scopes_are_recognized_identities_only(gap, question, expected):
    assert platforms(requested_scopes(question)) == expected


def test_a_gap_question_keeps_no_subject_or_purpose_platform_scope():
    question = "What are the existing Nimbus production deployment host, API/UI endpoints, deployment and rollback procedures for METAR board changes?"
    assert platforms(requested_scopes(question)) == []


def test_known_host_and_explicit_on_platform_are_both_kept():
    question = "What prod deployment of Grafana runs on OneCloud for the prod cluster?"
    assert platforms(requested_scopes(question)) == ["OneCloud", "prod"]


@pytest.mark.parametrize(
    "question",
    [
        "What runs on Nimbus in production?",
        "Which service is hosted on Nimbus Weather App?",
    ],
)
def test_catalog_subject_token_is_the_subject_not_a_platform(question):
    assert platforms(requested_scopes(question)) == []


def test_weak_for_fragment_is_dropped_when_a_platform_is_stated():
    assert platforms(requested_scopes("What is the deployment workflow for Foo board changes on App1?")) == ["App1"]


def test_fragment_outside_any_platform_phrase_is_dropped():
    assert platforms(requested_scopes("Does Foo board matter?", "x")) == []


@pytest.mark.parametrize("question", ["What is the rate hosted on OneCloud?", "What is the rate running on OneCloud?"])
def test_hosted_and_running_on_phrases_keep_unknown_platform(question):
    assert platforms(requested_scopes(question)) == ["OneCloud"]


@pytest.mark.parametrize(
    "question",
    [
        "What existing deployment and rollback route serves Nimbus METAR API?",
        "What existing deployment is current?",
    ],
)
def test_filler_word_is_never_a_scope(question):
    assert platforms(requested_scopes(question)) == []


@pytest.mark.parametrize(
    ("stored", "question"),
    [
        ([{"dimension": "platform", "value": "App1. Distinguish season"}], GAP_QUESTIONS["6430ad2d"][0]),
        ([{"dimension": "platform", "value": "App1 provide cutoff-bound"}], GAP_QUESTIONS["609c098f"][0]),
        (
            [{"dimension": "platform", "value": "App1"}, {"dimension": "platform", "value": "Nimbus METAR product"}],
            GAP_QUESTIONS["4031a309"][0],
        ),
        (
            [
                {"dimension": "platform", "value": "App1 as of"},
                {"dimension": "platform", "value": "canonical vo-rag production"},
            ],
            GAP_QUESTIONS["1f683284"][0],
        ),
    ],
)
def test_stored_applicability_fragments_repair_to_the_recognized_identity(stored, question):
    assert platforms(effective_question_scopes(question, stored)) == ["App1"]


def test_unrecognized_stored_platform_outside_an_explicit_phrase_drops_out():
    stored = [{"dimension": "platform", "value": "app-intel"}, {"dimension": "version", "value": "v3"}]
    effective = effective_question_scopes(GAP_QUESTIONS["0b8f978a"][0], stored)
    assert effective == [{"dimension": "version", "value": "v3"}]


def test_catalog_subject_is_the_subject_not_a_platform_filter():
    stored = [{"dimension": "platform", "value": "vo-rag production"}]
    assert platforms(effective_question_scopes("Where does the vo-rag production stack run on App1?", stored)) == []


def test_unknown_platform_in_an_unanchored_question_is_not_regressed():
    assert platforms(requested_scopes("What is the rate on OneCloud?")) == ["OneCloud"]


def test_typed_scopes_are_untouched_by_identity_filtering():
    stored = [{"dimension": "edition", "value": "Enterprise"}, {"dimension": "region", "value": "east"}]
    assert effective_question_scopes(GAP_QUESTIONS["0b8f978a"][0], stored) == stored


def test_platform_fragment_of_a_longer_catalog_subject_name_drops_out(identities):
    identities(hosts=["App1"], subjects=["Responses API", "openai-responses-api"])
    question = "For GPT-6 Responses API, distinguish mid-turn steering acceptance from application, async results and compaction restrictions."
    assert platforms(requested_scopes(question)) == []
    assert platforms(effective_question_scopes(question, [{"dimension": "platform", "value": "GPT-6 Responses"}])) == []


def test_loader_splits_catalog_hosts_from_subjects(monkeypatch, tmp_path):
    from doclayer import coverage, routing

    catalog = coverage.Catalog(
        "ok",
        "rev",
        1,
        subjects={
            "ubuntu": coverage.SubjectEntry("ubuntu", "host", "App1", aliases=["ubuntu box"]),
            "vo-rag": coverage.SubjectEntry("vo-rag", "service", "vo-rag", aliases=["vo rag"]),
        },
    )
    monkeypatch.setattr(routing, "load_catalog", lambda *args, **kwargs: catalog)
    monkeypatch.setattr(coverage, "ssh_host_aliases", lambda *args, **kwargs: ["prod"])
    found = applicability._load_identities()
    assert {"prod", "ubuntu box", "App1"} <= found.hosts
    assert found.subjects == {"vo-rag", "vo rag"}


def test_possessive_determiner_does_not_hide_an_explicit_platform():
    assert platforms(requested_scopes("What storage is configured in our Example Track installation?")) == [
        "Example Track"
    ]
