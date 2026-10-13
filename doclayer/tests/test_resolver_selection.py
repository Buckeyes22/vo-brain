from pathlib import Path

import pytest
import yaml

from doclayer import resolver
from doclayer.brainrepo import BrainPage
from doclayer.livechecks import LiveResult


class Repo:
    def __init__(self, root, pages):
        self.root = root
        self.pages = {p.path: p for p in pages}

    def markdown_paths(self):
        return list(self.pages)

    def read_page(self, path):
        return self.pages[path]

    def source_paths(self, subject):
        return []

    def has_path(self, path):
        return False


def page(path, body, title=None, updated="2026-10-02", **meta):
    return BrainPage(path, {"title": title or path, "updated": updated, **meta}, body, "a" * 40)


def select(pages, question):
    answer = resolver.Answer(question, None)
    for p in pages:
        resolver._append_page_facts(answer, p, question)
    return resolver._rank_facts(answer.git_facts, question, 8)


@pytest.fixture
def sources(monkeypatch):
    async def query(*args):
        return [], "offline"

    async def docs(*args):
        return {"error": "offline"}

    monkeypatch.setattr(resolver, "query_vo_rag", query)
    monkeypatch.setattr(resolver, "query_context7", docs)
    monkeypatch.setattr(resolver, "_safe_index_lag", lambda repo: [])
    monkeypatch.setattr(resolver, "stale_observation_flags", lambda repo: [])


def test_subject_distinctive_aliases_and_boundaries(tmp_path):
    repo = Repo(
        tmp_path,
        [
            page(
                "wiki/alibaba-model-studio.md",
                "",
                "Alibaba Model Studio",
                tags=["product"],
            ),
            page(
                "wiki/vertex-ai.md",
                "",
                "Vertex AI",
                tags=["product"],
                aliases=["Google Vertex"],
            ),
        ],
    )
    assert resolver.guess_subject(repo, "Which model handles curation?", None) is None
    assert resolver.guess_subject(repo, "How does the planning model work?", None) is None
    assert resolver.guess_subject(repo, "Model Studio base URL?", None) == "alibaba-model-studio"
    assert resolver.guess_subject(repo, "Google Vertex limits?", None) == "vertex-ai"
    assert resolver.guess_subject(repo, "vertexical operations?", None) is None
    assert resolver.guess_subject(repo, "anything", "explicit") == "explicit"


def test_relevant_fact_row_without_account_dump():
    p = page(
        "wiki/vertex.md",
        "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Base URL | https://vertex.test | — |\n| Quota | 200 requests | — |\n| Models | model-a | — |",
        "Vertex",
    )
    facts = select([p], "What is the Vertex base URL?")
    assert len(facts) == 1 and "vertex.test" in facts[0]["value"]


def test_current_host_beats_archive_but_archive_remains_discoverable():
    old = page(
        "inbox/old-investigation.md",
        "## Historical investigation 2026-09-30\nworker1 address was 10.0.0.2 during initial investigation.",
        updated="2026-09-30",
    )
    current = page("infra/fleet.md", "## Hosts\n| worker1 | address 192.0.2.160 |", "Fleet")
    facts = select([old, current], "What IP address is worker1 at?")
    assert "192.0.2.160" in facts[0]["value"]
    facts = select([old], "What was the historical worker1 address?")
    assert "historical/proposed (2026-09-30)" in facts[0]["source"]


def test_newest_observation_retains_declaration():
    template = "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| API version | v1 | {date}: {version} |"
    facts = select(
        [
            page(
                "wiki/old.md",
                template.format(date="2026-09-30", version="v2"),
                "Acme API",
            ),
            page(
                "wiki/new.md",
                template.format(date="2026-10-02", version="v3"),
                "Acme API",
            ),
        ],
        "What Acme API version is current?",
    )
    assert facts[0]["value"].startswith("2026-10-02: v3")
    assert "declared: v1" in facts[0]["value"]


def test_policy_and_design_relevance():
    pages = [
        page(
            "wiki/curation.md",
            "## Publication\nThe wiki curator uses local Qwen for curation. Operator approval is not required for publishing imported pages.\n## Accounts\nToken Plan costs 50 credits.",
            "Wiki curation",
        ),
        page(
            "wiki/storage.md",
            "## Design\nStorage uses durable file checkpoints and atomic replacement for recovery.\n## Old proposal\nThe proposed storage design used a new database.",
            "Storage design",
        ),
    ]
    policy = select(
        pages,
        "Which model handles wiki curation, and do imports require operator review before publication?",
    )
    assert any("local Qwen" in f["value"] for f in policy)
    assert not any("50 credits" in f["value"] for f in policy)
    assert "atomic replacement" in select(pages, "How does storage recovery work?")[0]["value"]


@pytest.mark.asyncio
async def test_other_questions_never_read_account_routes(tmp_path, monkeypatch, sources):
    def routes():
        raise AssertionError("irrelevant account route read")

    monkeypatch.setattr(resolver, "route_facts", routes)
    repo = Repo(
        tmp_path,
        [
            page(
                "wiki/curation.md",
                "Wiki curation uses local Qwen. Operator approval is not required.",
                "Wiki curation",
            )
        ],
    )
    answer = await resolver.resolve(
        "Which model handles wiki curation and is operator approval required?",
        repo=repo,
        live=False,
    )
    assert answer.subject is None and answer.git_facts and not answer.route_facts


@pytest.mark.asyncio
async def test_unknown_and_outage_remain_honest(tmp_path, sources):
    answer = await resolver.resolve("What is the frobnicator bandwidth?", repo=Repo(tmp_path, []), live=False)
    assert not answer.git_facts and answer.search_error == "offline" and answer.unknowns


def test_live_observation_leads_answer():
    live = LiveResult(
        name="test",
        checked_at="2026-10-02T04:00:00Z",
        checks=[{"what": "deployed commit", "value": "new123"}],
    )
    answer = resolver.Answer(
        "What release is current?",
        "Acme",
        git_facts=[
            {
                "fact": "declared release",
                "value": "old123",
                "source": "wiki/acme@abc123",
            }
        ],
        live_results=[live],
    )
    assert answer._prose().index("new123") < answer._prose().index("old123")


def test_raw_host_bullet_beats_question_echo_and_unrelated_host():
    facts = select(
        [
            page(
                "infra/fleet.md",
                "- **worker1** — `192.0.2.160`. Agent runtime host.\n- App1 — 192.0.2.228 app host.",
                "Fleet",
            ),
            page(
                "doclayer/as-built.md",
                'The natural question "what address is worker1 at?" was hard for embeddings.',
                "Inventory",
            ),
        ],
        "What IP address is worker1 at?",
    )
    assert "192.0.2.160" in facts[0]["value"]
    assert not any("228" in f["value"] for f in facts)


def test_historical_declarations_not_reported_as_current_split(tmp_path):
    p = tmp_path / "wiki"
    p.mkdir()
    (p / "api.md").write_text(
        "# Acme\n## Historical\nvo-rag release `aaaaaaa` ran previously.\n## Current\nvo-rag release `bbbbbbb`\n"
    )

    class TextRepo:
        root = tmp_path

        def markdown_paths(self):
            return ["wiki/api.md"]

        def read_text(self, path):
            return (self.root / path).read_text()

    live = LiveResult(
        name="vo-rag-release",
        checked_at="2026-10-02",
        checks=[{"what": "deployed commit", "value": "bbbbbbb"}],
    )
    flags = resolver.declared_vs_running_flags(TextRepo(), [live])
    assert not any("aaaaaaa" in f for f in flags)


def test_model_sections_cannot_borrow_other_models_context():
    p = page(
        "wiki/model-lineup.md",
        "## Orion X\nContext window is 10000 tokens.\n## Orion Y\nContext window is 20000 tokens.",
        "Model lineup",
    )
    answer = resolver.Answer("What is Orion X context window?", "orion-x")
    resolver._append_page_facts(answer, p, answer.question)
    assert len(answer.git_facts) == 1 and "10000" in answer.git_facts[0]["value"]
    # Distinctive long names and single-letter variants both retain scope.
    p = page(
        "wiki/model-lineup.md",
        "## Orion Astra\nContext window is 10000 tokens.\n## Orion Sol\nContext window is 20000 tokens.",
        "Model lineup",
    )
    answer = resolver.Answer("What is Orion Astra context window?", "orion-astra")
    resolver._append_page_facts(answer, p, answer.question)
    assert len(answer.git_facts) == 1 and "10000" in answer.git_facts[0]["value"]


def test_pricing_prompt_mentions_do_not_answer_prompting_question():
    p = page(
        "wiki/orion.md",
        "## Prompting\nOrion Astra prompts benefit from clear completion criteria.\n## Pricing\nOrion Astra context and prompts cost 100 credits.",
        "Orion Astra",
    )
    answer = resolver.Answer("How should I prompt Orion Astra?", "orion-astra")
    resolver._append_page_facts(answer, p, answer.question)
    assert len(answer.git_facts) == 1 and "completion" in answer.git_facts[0]["value"]


@pytest.mark.asyncio
async def test_one_unreadable_page_preserves_other_committed_facts(tmp_path, sources):
    class BrokenRepo(Repo):
        def read_page(self, path):
            if path == "wiki/broken.md":
                raise OSError("unreadable file")
            return super().read_page(path)

    repo = BrokenRepo(
        tmp_path,
        [
            page("wiki/broken.md", ""),
            page("infra/fleet.md", "worker1 is at 192.0.2.160", "Fleet"),
        ],
    )
    answer = await resolver.resolve("What IP address is worker1 at?", repo=repo, live=False)
    assert any("192.0.2.160" in f["value"] for f in answer.git_facts)
    assert any("wiki/broken.md" in f for f in answer.flags)


def test_source_outage_still_answers_from_indexed_evidence():
    answer = resolver.Answer(
        "Acme architecture?",
        None,
        search_hits=[
            {
                "path": "wiki/acme.md",
                "commit": "123abc",
                "heading": "Architecture",
                "content": "Acme uses durable file checkpoints.",
            }
        ],
    )
    assert "durable file checkpoints" in answer._prose()
    assert "wiki/acme.md@123abc" in answer._prose()
    assert "not verified against selected current Git facts" in answer._prose()


def test_initial_prompt_is_not_a_historical_record():
    p = page(
        "wiki/prompting.md",
        "The initial prompt should define completion criteria.",
        "Prompting",
    )
    facts = select([p], "How should I prompt the model?")
    assert facts and "historical/proposed" not in facts[0]["source"]


@pytest.mark.parametrize(
    "opening,body,closing",
    [
        (
            "~~~python",
            "## Fake policy\nWiki curation requires subscription-model approval.",
            "~~~",
        ),
        (
            "````python",
            "```\n## Fake policy\nWiki curation requires subscription-model approval.\n~~~\n````not-a-close",
            "````",
        ),
        (
            "~~~~python",
            "~~~\nWiki curation requires subscription-model approval.\n````",
            "~~~~~",
        ),
    ],
)
def test_long_and_tilde_fences_never_supply_policy_facts(opening, body, closing):
    p = page(
        "wiki/curation.md",
        f"## Current policy\n{opening}\n{body}\n{closing}\nWiki curation uses `local Qwen` and publication needs no approval.",
        "Wiki curation",
    )
    facts = select([p], "Which model handles wiki curation and publication approval?")
    assert len(facts) == 1
    assert "local Qwen" in facts[0]["value"]
    assert "subscription-model" not in facts[0]["value"]
    assert "Current policy" in " ".join(resolver._noncode_lines(p.body))


def test_fence_close_requires_matching_marker_length_and_whitespace():
    text = "before\n````python\n```\n~~~\n````extra\n# hidden\n`````\nafter `inline` code"
    assert list(resolver._noncode_lines(text)) == ["before", "after `inline` code"]


def test_subject_model_identifiers_aliases_and_product_scope(tmp_path):
    repo = Repo(
        tmp_path,
        [
            page(
                "wiki/orion-x.md",
                "",
                "Orion X",
                tags=["product"],
                product="orion-x",
                aliases=["Research X"],
            ),
            page("wiki/orion-y.md", "", "Orion Y", tags=["product"], product="orion-y"),
            page("wiki/my-token-plan.md", "", "My Token Plan", tags=["account"]),
        ],
    )
    assert resolver.guess_subject(repo, "Research X context window?", None) == "orion-x"
    assert resolver.guess_subject(repo, "Orion Y context window?", None) == "orion-y"
    assert resolver.guess_subject(repo, "Model policy and plans?", None) is None
    assert resolver.guess_subject(repo, "Token Plan endpoint?", None) == "my-token-plan"
    p = page(
        "wiki/lineup.md",
        "## Orion X\nContext window is 10000 tokens.\n## Orion Y\nContext window is 20000 tokens.",
        "Model lineup",
        tags=["product"],
    )
    answer = resolver.Answer("What Orion X context window?", "orion-x")
    resolver._append_page_facts(answer, p, answer.question)
    assert len(answer.git_facts) == 1 and "10000" in answer.git_facts[0]["value"]


def test_indexed_fallback_distinguishes_unavailable_git():
    hits = [
        {
            "path": "wiki/acme.md",
            "commit": "123abc",
            "heading": "Design",
            "content": "Acme uses file checkpoints.",
        }
    ]
    available = resolver.Answer("Acme design?", None, search_hits=hits)
    unavailable = resolver.Answer("Acme design?", None, search_hits=hits, git_source_available=False)
    assert "not verified against selected" in available._prose()
    assert "source unavailable" not in available._prose()
    assert "current Git source unavailable" in unavailable._prose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,product",
    [
        ("What is the current Orion Astra context window?", "orion-astra"),
        ("Which model currently handles wiki curation?", None),
        ("What is the latest wiki publication approval policy?", None),
        ("What is the current IP address of App1?", None),
    ],
)
async def test_auto_live_unrelated_subjects_never_probe_rag(tmp_path, monkeypatch, sources, question, product):
    async def probes(*args, **kwargs):
        raise AssertionError("unrelated service probed")

    monkeypatch.setattr(resolver, "run_checks", probes)
    answer = await resolver.resolve(question, product=product, repo=Repo(tmp_path, []))
    assert not answer.live_results
    assert any("no applicable implemented live check" in flag for flag in answer.flags)
    assert not any("port :8001" in line for line in answer._prose().splitlines())


@pytest.mark.asyncio
async def test_auto_live_current_rag_host_uses_applicable_checks(tmp_path, monkeypatch, sources):
    received = []

    async def probes(*, names):
        received.extend(names)
        return [
            LiveResult(
                name="vo-rag-release",
                checked_at="2026-10-02",
                checks=[{"what": "deployed commit", "value": "new123"}],
            ),
            LiveResult(
                name="vo-rag-api",
                checked_at="2026-10-02",
                checks=[{"what": "port :8001", "value": "answers"}],
            ),
            LiveResult(
                name="litellm-container",
                checked_at="2026-10-02",
                checks=[{"what": "unrelated gateway", "value": "active"}],
            ),
        ]

    monkeypatch.setattr(resolver, "run_checks", probes)
    answer = await resolver.resolve("What vo-rag release and host port are current?", repo=Repo(tmp_path, []))
    assert received == ["vo-rag-release", "vo-rag-api"]
    assert {r.name for r in answer.live_results} == set(received)
    assert "new123" in answer._prose() and "unrelated gateway" not in answer._prose()
    assert not any("no applicable" in flag for flag in answer.flags)


@pytest.mark.parametrize(
    "question,subject,expected",
    [
        ("What is current LiteLLM container status?", "litellm", ["litellm-container"]),
        ("Is the Token Plan endpoint reachable now?", None, ["token-plan-endpoint"]),
        ("Which models are currently in Token Plan?", None, []),
        ("What vo-rag embedding model runs here?", None, []),
        (
            "What is the current Qdrant brain collection point count?",
            None,
            ["vo-rag-brain-collection"],
        ),
    ],
)
def test_live_capabilities_match_observed_resource(question, subject, expected):
    assert resolver._applicable_live_checks(question, subject) == expected


def test_concurrent_limit_does_not_request_current_live_state():
    assert not resolver._wants_live_state("What are the concurrent request limits?")
    assert resolver._wants_live_state("Which model currently handles curation?")


def test_policy_framing_does_not_invent_gap_in_stated_approval_rule():
    answer = resolver.Answer(
        "What is the publication approval policy?",
        None,
        git_facts=[
            {
                "fact": "Publication",
                "value": "Approval is optional and retrospective.",
                "source": "wiki/policy@123abc",
            }
        ],
    )
    assert not resolver.unknown_flags(answer, [])


@pytest.mark.parametrize(
    "question",
    [
        "What is the current Qdrant raw collection point count?",
        "What is the current vo-rag sterile collection point count?",
    ],
)
def test_brain_collection_probe_never_claims_other_collection_health(question):
    assert resolver._applicable_live_checks(question, None) == []


@pytest.mark.parametrize(
    "question",
    [
        "Which vo-rag brain sync version is current?",
        "Which vo-rag embedding model version is current?",
        "What is the current vo-rag brain collection writer version?",
    ],
)
def test_api_release_is_not_indexer_or_collection_writer_revision(question):
    assert resolver._applicable_live_checks(question, None) == []


def test_named_brain_collection_health_does_not_probe_api():
    assert resolver._applicable_live_checks("What is current vo-rag brain collection status?", None) == [
        "vo-rag-brain-collection"
    ]


def test_model_question_keeps_identity_and_constraints_not_curator_file_handles():
    p = page(
        "doclayer/operations.md",
        "## Curation\n| Component | Details |\n|---|---|\n| Curator design | `/home/user/git/brain/inbox/wiki-curator-design.md` |\n| Curator implementation | `curator/model.py` |\n| Editorial model | Local Nimbus on worker-2 handles wiki curation. |\n| Serving | Wiki curation uses local Nimbus on worker-2; no paid fallback. |\n| Fallback | Wiki curation never uses subscription or paid fallback. |",
        "Wiki curation operations",
    )
    facts = select([p], "Which model handles wiki curation?")
    values = " ".join(f["value"] for f in facts)
    assert "Nimbus" in values and "paid fallback" in values
    assert "wiki-curator-design.md" not in values and "curator/model.py" not in values


def test_model_question_does_not_borrow_identity_from_page_title_or_heading():
    p = page(
        "wiki/model-services.md",
        "## Models for curation\nSee `/home/user/git/curator/model.py` for implementation.\n## Curation serving\nCuration runs atlas4.2-small on worker-3.",
        "Model services",
    )
    facts = select([p], "Which model handles curation?")
    assert len(facts) == 1 and "atlas4.2-small" in facts[0]["value"]


def test_model_facet_also_filters_indexed_fallback():
    answer = resolver.Answer("Which model handles wiki curation?", None)
    answer.search_hits = [
        {"content": "Wiki curator implementation: `/home/user/git/curator/model.py`", "heading": "Models"},
        {"content": "Wiki curation uses local Nimbus on worker-2; no paid fallback.", "heading": "Serving"},
    ]
    assert "Nimbus" in answer._prose() and "model.py" not in answer._prose()


def test_model_category_and_investigation_mentions_are_not_serving_identities():
    p = page(
        "doclayer/curation.md",
        '## Curation\nThe LLM-wiki brain is a curated knowledge base.\nGeneric "model" overlap selects the wrong product for curation-policy questions.\nThe editorial model is Nimbus.\nCurator model calls are pinned to local Nimbus; outages never fall back to subscription models.',
        "Curation",
    )
    facts = select([p], "Which model handles curation?")
    values = " ".join(f["value"] for f in facts)
    assert "editorial model is Nimbus" in values and "local Nimbus" in values
    assert "LLM-wiki brain" not in values and "wrong product" not in values


def test_named_model_fact_table_remains_a_serving_identity():
    p = page(
        "wiki/curation.md",
        "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Editorial model | Nimbus | — |",
        "Wiki curation",
    )
    facts = select([p], "Which model handles wiki curation?")
    assert len(facts) == 1 and "Nimbus" in facts[0]["value"]


@pytest.mark.parametrize(
    "body",
    [
        "The editorial model is nimbus.",
        "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Editorial model | nimbus | — |",
        "The editorial model is `nimbus`.",
    ],
)
def test_explicit_lowercase_model_identifiers_are_not_degraded_to_unknown(body):
    facts = select([page("wiki/curation.md", body, "Wiki curation")], "Which model handles wiki curation?")
    assert len(facts) == 1 and "nimbus" in facts[0]["value"]


@pytest.mark.parametrize(
    "body", ["The editorial model is available.", "The editorial model is local.", "Wiki curation uses a local Model."]
)
def test_model_predicates_and_generic_categories_are_not_identifiers(body):
    assert not select([page("wiki/curation.md", body, "Wiki curation")], "Which model handles wiki curation?")


def test_numeric_multifacet_keeps_both_original_model_quantities():
    p = page(
        "wiki/astra.md",
        "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Context window | 1,050,000 tokens | — |\n| Maximum output | 128K tokens | — |\n\n## Context management\nAstra context management keeps instructions concise.",
        "Astra",
    )
    q = "What is the current Astra context window and maximum output size?"
    facts = select([p], q)
    assert {f["fact"] for f in facts} == {"Context window", "Maximum output"}
    assert all(f["source"].startswith("wiki/astra.md@") for f in facts)
    assert not resolver.unknown_flags(resolver.Answer(q, None, git_facts=facts), [])


def test_three_numeric_facets_on_one_capacity_page_survive_diversity_cap():
    p = page(
        "wiki/nimbus.md",
        "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Storage capacity | 8 TiB | — |\n| Memory capacity | 64 GB | — |\n| Maximum connections | 256 | — |",
        "Nimbus",
    )
    facts = select([p], "What are Nimbus storage and memory capacities and maximum connections?")
    assert {f["fact"] for f in facts} == {"Storage capacity", "Memory capacity", "Maximum connections"}
    assert any("8 TiB" in f["value"] for f in facts)


@pytest.mark.parametrize(
    "body",
    [
        "Storage capacity is expandable.",
        "Storage capacity is 64.",
        "Storage capacity uses version 64.2.",
        "Storage capacity is 64 tokens.",
    ],
)
def test_missing_or_wrong_units_do_not_establish_capacity(body):
    q = "What is Nimbus storage capacity?"
    facts = select([page("wiki/nimbus.md", body, "Nimbus")], q)
    assert not facts
    flags = resolver.unknown_flags(resolver.Answer(q, None), [{"heading": "Nimbus", "content": body}])
    assert any("storage" in f for f in flags)


def test_missing_second_facet_remains_unknown_despite_nearest_neighbor_echo():
    q = "What are Nimbus storage capacity and maximum connections?"
    facts = select([page("wiki/nimbus.md", "Nimbus storage capacity is 8 TB.", "Nimbus")], q)
    flags = resolver.unknown_flags(
        resolver.Answer(q, None, git_facts=facts),
        [{"heading": "Connections", "content": "Maximum connections are configurable."}],
    )
    assert any("connection" in flag for flag in flags)


def test_current_numeric_fact_precedes_historical_quantity():
    old = page("inbox/nimbus-old.md", "Nimbus storage capacity was 4 TB.", "Nimbus", updated="2026-01-01")
    new = page("wiki/nimbus.md", "Nimbus storage capacity is 8 TB.", "Nimbus")
    facts = select([old, new], "What is the current Nimbus storage capacity?")
    assert "8 TB" in facts[0]["value"]


def test_endpoint_intent_selects_existing_live_api_capability():
    assert resolver._applicable_live_checks(
        "What vo-rag version is current and which endpoint serves retrieval?", "vo-rag"
    ) == ["vo-rag-release", "vo-rag-api"]


def test_neighboring_quantity_does_not_fill_unspecified_output_limit():
    text = "Context window is 64K tokens, but maximum output is configurable."
    q = "What is the context window and maximum output size?"
    assert resolver._matching_aspects(text, q) == {"context"}


def test_unrelated_version_number_does_not_fill_storage_capacity():
    text = "Nimbus version is v2.4; storage capacity is configurable."
    q = "What is Nimbus version and storage capacity?"
    assert resolver._matching_aspects(text, q) == {"version"}


def test_version_and_capacity_require_each_concrete_value():
    p = page(
        "wiki/nimbus.md",
        "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Version | v2.4 | — |\n| Storage capacity | 8 TiB | — |\n\nThis architecture is the current version.\nInspect the writer revision for stale data.",
        "Nimbus",
    )
    facts = select([p], "What version and storage capacity does Nimbus use?")
    assert {f["fact"] for f in facts} == {"Version", "Storage capacity"}


def test_requested_capacity_keeps_source_units_without_inventing_conversion():
    facts = select(
        [page("wiki/nimbus.md", "Nimbus storage capacity is 8 TiB.", "Nimbus")],
        "What is Nimbus storage capacity in GB?",
    )
    assert facts and "8 TiB" in facts[0]["value"] and "GB" not in facts[0]["value"]


def test_unstated_requested_unit_is_honestly_flagged_without_conversion():
    q = "What is Nimbus storage capacity in GB?"
    facts = select([page("wiki/nimbus.md", "Nimbus storage capacity is 8 TiB.", "Nimbus")], q)
    flags = resolver.unknown_flags(resolver.Answer(q, "Nimbus", git_facts=facts), [])
    assert any("requested unit GB" in f and "no conversion established" in f for f in flags)
    assert "8 TiB" in facts[0]["value"]


def test_document_version_not_attributed_to_neighboring_service():
    answer = resolver.Answer("What Search Harbor version currently runs?", "search-harbor")
    unrelated = page(
        "doclayer/index.md",
        "**Version 2.2.** This document explains operations. Search Harbor performs retrieval.",
        "Operations manual",
    )
    real = page(
        "wiki/search-harbor.md",
        "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Version | v4.1 | — |",
        "Search Harbor",
    )
    for p in [unrelated, real]:
        resolver._append_page_facts(answer, p, answer.question)
    facts = resolver._rank_facts(answer.git_facts, answer.question, 8)
    assert len(facts) == 1 and "v4.1" in facts[0]["value"]


def test_shared_component_table_versions_require_subject_ownership():
    answer = resolver.Answer("What Worker version currently runs?", "worker")
    p = page(
        "infra/fleet.md",
        "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Gateway version | v2.0; Worker supports TLS | — |\n| Worker version | v3.4 | — |",
        "Shared fleet",
    )
    resolver._append_page_facts(answer, p, answer.question)
    assert {f["fact"] for f in answer.git_facts} == {"Worker version"}


def test_owned_subsection_retains_compact_component_version():
    answer = resolver.Answer("What Worker version currently runs?", "worker")
    p = page("infra/fleet.md", "## Worker\nVersion is v3.4.\n## Gateway\nVersion is v2.0.", "Shared fleet")
    resolver._append_page_facts(answer, p, answer.question)
    assert len(answer.git_facts) == 1 and "v3.4" in answer.git_facts[0]["value"]


def test_stored_observation_is_qualified_when_fresher_live_probe_exists():
    answer = resolver.Answer(
        "What Worker version currently runs?",
        "worker",
        live_results=[
            LiveResult("worker-release", "2026-10-02T09:00:00Z", [{"what": "deployed commit", "value": "abc12345"}])
        ],
    )
    old = page(
        "infra/worker.md", "Worker release observed by a live check 2026-10-02T08:00:00Z was def67890.", "Worker"
    )
    resolver._append_page_facts(answer, old, answer.question)
    assert "historical/proposed (2026-10-02T08:00:00Z)" in answer.git_facts[0]["source"]
    assert answer._prose().index("abc12345") < answer._prose().index("def67890")


def test_dated_policy_remains_current_design_without_becoming_observed_state():
    answer = resolver.Answer(
        "What Worker publication approval policy is current?",
        "worker",
        live_results=[
            LiveResult("worker-release", "2026-10-02T09:00:00Z", [{"what": "deployed commit", "value": "abc12345"}])
        ],
    )
    p = page(
        "wiki/worker-policy.md",
        "Worker publication approval is optional and retrospective.",
        "Worker policy",
        updated="2026-10-01",
    )
    resolver._append_page_facts(answer, p, answer.question)
    assert answer.git_facts and "historical" not in answer.git_facts[0]["source"]


def test_dated_observation_retained_without_claiming_freshness_on_live_outage():
    answer = resolver.Answer(
        "What Worker version currently runs?",
        "worker",
        live_results=[LiveResult("worker-release", "2026-10-02T09:00:00Z", error="unreachable")],
    )
    p = page("infra/worker.md", "Worker release observed 2026-10-02T08:00:00Z was def67890.", "Worker")
    resolver._append_page_facts(answer, p, answer.question)
    assert answer.git_facts and "2026-10-02T08:00:00Z" in answer.git_facts[0]["source"]


def test_different_component_label_overrides_shared_page_subject_context():
    answer = resolver.Answer("What Worker version currently runs?", "worker")
    p = page(
        "infra/components.md",
        "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Gateway version | v2.0 | — |\n| Worker version | v3.4 | — |",
        "Worker and Gateway",
    )
    resolver._append_page_facts(answer, p, answer.question)
    assert {f["fact"] for f in answer.git_facts} == {"Worker version"}


def test_exact_endpoint_question_requires_actual_locator_not_design_label():
    q = "What Worker version currently runs and which endpoint serves requests?"
    assert not resolver._matching_aspects("The Worker endpoint port is stale.", q)
    assert resolver._matching_aspects("Worker API endpoint https://worker.test/api", q) == {"endpoint"}
    assert resolver._matching_aspects("Worker API listens on port 8012.", q) == {"endpoint"}
    assert resolver._matches_requested_aspects(
        "Worker endpoint discovery uses DNS.", "How does Worker endpoint discovery work?"
    )


@pytest.mark.parametrize("meta", [{"aliases": ["Nimbus"]}, {"product": "Nimbus"}])
def test_declared_owner_alias_preserves_compact_version_fact(meta):
    answer = resolver.Answer("What Nimbus version currently runs?", "Nimbus")
    p = page(
        "wiki/control-plane.md",
        "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| API version | v3.4 | — |",
        "Control Plane",
        **meta,
    )
    resolver._append_page_facts(answer, p, answer.question)
    assert answer.git_facts and "v3.4" in answer.git_facts[0]["value"]


def test_citation_url_does_not_supply_missing_service_endpoint():
    text = "Worker version is v3.4; endpoint is undocumented. [source: https://docs.worker.test/api]"
    assert resolver._matching_aspects(text, "What Worker version runs and which endpoint serves requests?") == {
        "version"
    }


def test_hypothetical_test_release_not_returned_as_current_observation():
    p = page(
        "docs/acceptance.md",
        "**Given:** a brain page declares Worker release `f21bd74`; the live system has moved.",
        "Worker acceptance",
    )
    current = resolver.Answer("What Worker version currently runs?", "worker")
    resolver._append_page_facts(current, p, current.question)
    assert not current.git_facts
    design = resolver.Answer("What Worker release does the current acceptance scenario assume?", "worker")
    resolver._append_page_facts(design, p, design.question)
    assert design.git_facts and "f21bd74" in design.git_facts[0]["value"]


@pytest.mark.parametrize(
    "body",
    [
        "Gateway version is v2.0.",
        "## Gateway\nVersion is v2.0.",
        "## Gateway\n### Facts\nVersion is v2.0.",
        "Worker uses Gateway version v2.0.",
    ],
)
def test_specific_other_prose_or_subsection_owner_overrides_shared_title(body):
    answer = resolver.Answer("What Worker version currently runs?", "worker")
    p = page("infra/components.md", body, "Worker and Gateway")
    resolver._append_page_facts(answer, p, answer.question)
    assert not answer.git_facts


@pytest.mark.parametrize("body", ["Sol version is v2.0.", "## Sol\nVersion is v2.0."])
def test_multiple_model_page_does_not_borrow_other_model_version(body):
    answer = resolver.Answer("What Astra version currently runs?", "astra")
    p = page("wiki/models.md", body, "Astra and Sol")
    resolver._append_page_facts(answer, p, answer.question)
    assert not answer.git_facts


def test_generic_facts_subsection_preserves_true_owned_page_version():
    answer = resolver.Answer("What Worker version currently runs?", "worker")
    p = page("wiki/worker.md", "## Facts\nVersion is v3.4.", "Worker")
    resolver._append_page_facts(answer, p, answer.question)
    assert answer.git_facts and "v3.4" in answer.git_facts[0]["value"]


def test_real_dated_deployment_clause_survives_generic_owner_words():
    answer = resolver.Answer("What Search Harbor version currently runs?", "search-harbor")
    p = page(
        "docs/as-built.md",
        "Search Harbor API | Normal deployment and live revision check 2026-10-02T06:23:00Z: abc12345 | active",
        "As built",
    )
    resolver._append_page_facts(answer, p, answer.question)
    assert answer.git_facts and "abc12345" in answer.git_facts[0]["value"]


@pytest.mark.parametrize(
    "recorded,older",
    [("2026-10-02 06:23 UTC", True), ("2026-10-02 10:23 UTC", False), ("2026-10-02", False), ("2026-10-01", True)],
)
def test_observation_timestamp_order_does_not_drop_plain_utc_wall_time(recorded, older):
    answer = resolver.Answer(
        "What Worker version currently runs?",
        "worker",
        live_results=[
            LiveResult("worker-release", "2026-10-02T09:00:00Z", [{"what": "deployed commit", "value": "abc12345"}])
        ],
    )
    assert (
        resolver._older_observation(answer, "Worker release observed " + recorded + " was def67890.", "page text")
        is older
    )


def test_plain_utc_source_timestamp_preserved_in_fact_qualification():
    answer = resolver.Answer(
        "What Worker version currently runs?",
        "worker",
        live_results=[
            LiveResult("worker-release", "2026-10-02T09:00:00Z", [{"what": "deployed commit", "value": "abc12345"}])
        ],
    )
    p = page("wiki/worker.md", "Worker release observed 2026-10-02 06:23 UTC was def67890.", "Worker")
    resolver._append_page_facts(answer, p, answer.question)
    assert "06:23 UTC" in answer.git_facts[0]["value"]
    assert "2026-10-02T06:23Z" in answer.git_facts[0]["source"]


def test_explicit_subject_version_under_document_heading_not_lost():
    answer = resolver.Answer("What Search Harbor version currently runs?", "search-harbor")
    p = page(
        "docs/as-built.md",
        "# Documentation as-built facts\n## Observed deployment\nSearch Harbor API | Normal deployment and live revision check 2026-10-02T06:23:00Z: abc12345 | active",
        "As built",
    )
    resolver._append_page_facts(answer, p, answer.question)
    assert answer.git_facts and "abc12345" in answer.git_facts[0]["value"]


def test_wants_routes_fires_for_the_routing_policys_token_plan_subject():
    # The question avoids the phrase "token plan", so only the subject check can fire.
    policy = yaml.safe_load((Path(__file__).resolve().parents[1] / "routing-policy.yaml").read_text())
    assert "token-plan" in policy["subjects"]
    assert resolver._wants_routes("Which endpoint serves tier S?", "token-plan")
    assert resolver._wants_routes("Which endpoint do I use?", "token-plan")
    assert not resolver._wants_routes("Which endpoint do I use?", "my-token-plan")
    assert not resolver._wants_routes("Which endpoint do I use?", None)
