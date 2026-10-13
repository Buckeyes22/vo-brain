"""Cross-subject import boundaries, durable continuation and evidence."""

import json
from pathlib import Path

import httpx
import pytest
from conftest import HttpFixture

from doclayer.curator import curate, intake, ops, policy
from doclayer.curator.model import ModelError, OutputTruncated
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import Evidence, JobState, PageChange, Publication


def job(store):
    return store.create(JobState(job_id="", idempotency_key="", intent="device firmware procedure"))[0]


def test_policy_exceptions_and_semantics_are_provider_independent():
    rules = {
        "exclude": [{"domain": "cloud.example", "path": "/managed/*"}],
        "exceptions": [
            {
                "url": "https://cloud.example/managed/card",
                "applicability": {"scope": "model-level"},
                "forbidden_claims": [r"\b(permission|quota|endpoint)\b"],
            }
        ],
    }
    assert not policy.url_decision("https://cloud.example/managed/conversation", rules)[0]
    assert policy.url_decision("https://cloud.example/managed/card", rules)[0]
    assert policy.url_decision("https://cloud.example.evil/managed/usage", rules)[0]
    assert not policy.claim_decision("https://cloud.example/managed/card", "Set endpoint permission", {}, rules)[0]
    assert not policy.claim_decision("https://cloud.example/managed/card", "Model fact", {"scope": "service"}, rules)[0]
    assert policy.claim_decision(
        "https://cloud.example/managed/card", "8B parameters", {"scope": "model-level"}, rules
    )[0]


@pytest.mark.asyncio
async def test_policy_blocks_discovery_and_redirect_before_fetch(cfg, store):
    state = job(store)
    seed = intake.make_url_source("https://device.example/manual")
    seed.status = "fetched"
    state.sources, state.usage.sources_fetched = [seed], 1
    state.source_policy = {"exclude": [{"domain": "cloud.example", "path": "/managed/*"}]}
    pipe = Pipeline(cfg, store)
    pipe._store_extract(state, seed, "#Device\n[Chat API](https://cloud.example/managed/conversation)")
    await pipe._stage_discover(state)
    assert state.sources[-1].status == "rejected" and state.sources[-1].parent_id == seed.id
    assert store.load(state.job_id).sources[-1].error
    seen = []

    def responder(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://cloud.example/managed/conversation"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        with pytest.raises(intake.FetchFailure, match="source policy"):
            await intake.fetch_url(
                client,
                "https://device.example/redirect",
                max_bytes=1000,
                url_policy=lambda url: policy.url_decision(url, state.source_policy)[:2],
            )
    assert seen == ["https://device.example/redirect"]


@pytest.mark.asyncio
async def test_whole_frontier_priority_persisted_across_cap_and_restart(cfg, store, monkeypatch):
    state = job(store)
    state.limits = {"max_sources": 2}
    source = intake.make_url_source("https://device.example/manual")
    source.status = "fetched"
    state.sources, state.usage.sources_fetched = [source], 1
    fixture = HttpFixture(
        {
            "https://device.example/z-important": (
                200,
                "<article><h1>Recovery</h1><p>" + "Device facts. " * 30 + "</p></article>",
            )
        }
    )
    pipe = Pipeline(cfg, store, fetch_transport=fixture.transport())
    links = "\n".join(f"[Peripheral {i}](https://device.example/a-{i:02})" for i in range(24))
    pipe._store_extract(state, source, links + "\n[Recovery](https://device.example/z-important)")

    async def score(client, topic, exclusions, candidates):
        return {
            url: {"relevant": True, "priority": "high" if url.endswith("z-important") else "low", "why": anchor}
            for url, anchor in candidates
        }

    monkeypatch.setattr(curate, "score_link_relevance", score)
    await pipe._stage_discover(state)
    assert fixture.requested == ["https://device.example/z-important"]
    loaded = store.load(state.job_id)
    assert len([s for s in loaded.sources if s.status == "scored"]) == 24 and loaded.coverage == "incomplete"
    await pipe._stage_discover(loaded)
    assert fixture.requested == ["https://device.example/z-important"] and len(loaded.sources) == 26


@pytest.mark.asyncio
async def test_failed_duplicate_rejected_candidates_survive(cfg, store, monkeypatch):
    state = job(store)
    seed = intake.make_url_source("https://device.example/start")
    seed.status = "fetched"
    state.sources, state.usage.sources_fetched = [seed], 1
    body = "<article><h1>Firmware</h1><p>" + "Firmware facts. " * 30 + "</p></article>"
    fixture = HttpFixture({"https://device.example/fail": (404, "missing"), "https://device.example/copy": (200, body)})
    pipe = Pipeline(cfg, store, fetch_transport=fixture.transport())
    seed.content_hash = intake.content_hash(intake.html_to_markdown(body))
    pipe._store_extract(
        state,
        seed,
        "[Fail](https://device.example/fail)\n[Copy](https://device.example/copy)\n[Unrelated](https://device.example/reject)",
    )

    async def score(client, topic, exclusions, candidates):
        return {
            url: {"relevant": not url.endswith("reject"), "priority": "normal", "why": "outside topic"}
            for url, _ in candidates
        }

    monkeypatch.setattr(curate, "score_link_relevance", score)
    await pipe._stage_discover(state)
    assert {s.url.rsplit("/", 1)[-1]: s.status for s in store.load(state.job_id).sources} == {
        "start": "fetched",
        "fail": "failed",
        "copy": "duplicate",
        "reject": "rejected",
    }


def test_continuation_owns_cache_filters_legacy_preserves_original(cfg, store):
    state = job(store)
    state.status, state.coverage = "published", "incomplete"
    state.publication = Publication(pr_number=70, merged=True, merge_commit="old-merge")
    allowed = intake.make_url_source("https://vendor.example/model")
    excluded = intake.make_url_source("https://vendor.example/service/chat")
    allowed.status = excluded.status = "fetched"
    allowed.extraction_complete = True
    allowed.reviewed_sections, allowed.omitted_sections = ["#Overview"], ["#Safety"]
    state.sources = [allowed, excluded]
    pipe = Pipeline(cfg, store)
    pipe._store_extract(state, allowed, "#Overview\nFacts\n#Safety\nRisks")
    pipe._store_extract(state, excluded, "#Chat\nUsage")
    store.append_evidence(
        state.job_id,
        [Evidence(s.id, "source", "#Overview", "data", "Fact", url=s.url).to_dict() for s in state.sources],
    )
    store.save(state)
    before = store.state_path(state.job_id).read_bytes()
    request = {
        "job_id": state.job_id,
        "source_policy": {"exclude": [{"domain": "vendor.example", "path": "/service/*"}]},
        "discover": False,
    }
    reply = ops.handle("continue", request, cfg, store)
    child = store.load(reply["job_id"])
    assert child.continuation_of == state.job_id and child.publication.pr_number is None
    assert store.state_path(state.job_id).read_bytes() == before and child.usage.sources_fetched == 0
    assert len(store.read_evidence(child.job_id)) == 1
    assert child.job_id in child.sources[0].extract_path
    Path(allowed.extract_path).unlink()
    assert pipe._extract_text(child, child.sources[0]) and child.sources[0].relevance["review_candidates"] == [
        "#Safety"
    ]
    assert ops.handle("continue", request, cfg, store)["job_id"] == child.job_id


def test_fences_hierarchy_duplicate_locators_stable():
    body = "# Manual\n## Setup\nFacts\n```sh\n# shell comment\n## not heading\n```\n## Setup\nMore facts\n### Recovery\nRepair steps\n"
    sections = intake.section_map(body)
    assert [loc for loc, _ in sections] == [
        "#Manual / Setup",
        "#Manual / Setup [occurrence 2]",
        "#Manual / Setup / Recovery",
    ]
    assert sections == intake.section_map(body) and "shell comment" in sections[0][1]


@pytest.mark.asyncio
async def test_hierarchical_selection_sees_late_regions():
    calls = []

    class Client:
        async def chat_json(self, system, user):
            calls.append(user)
            if user.startswith("SOURCE ANCESTOR CONSTRAINT CHECK"):
                item = json.loads(user.split("\n", 1)[1])
                context = item["ancestor_context"]
                quote = (
                    "Risk evaluation 19."
                    if "Risk evaluation 19." in context
                    else ("Useful." if "Useful." in context else "")
                )
                return {
                    "verdicts": [
                        {
                            "id": item["id"],
                            "supported": bool(quote),
                            "consistent": True,
                            "reason": "The actual selected local section documents this fact.",
                            "qualifiers": [],
                            "support_quotes": [quote] if quote else [],
                        }
                    ]
                }
            if "Verify selected claims against original source excerpts" in system:
                items = json.loads(user.split("\n", 1)[1])
                return {
                    "verdicts": [
                        {"id": item["id"], "supported": True, "reason": "supported source fixture"} for item in items
                    ]
                }
            if "Select the most useful" in system:
                import re

                indices = [int(i) for i in re.findall(r"^(\d+):", user, re.M)]
                return {"groups": [indices[-1]]}
            return {"claims": [{"claim": "Late risk documented", "locator": "#Chapter 19"}]}

    source = intake.make_url_source("https://vendor.example/report.pdf")
    text = "\n".join(f"## Chapter {i}\n" + ("Risk evaluation %d. " % i) * 120 for i in range(20))
    records = []
    await curate.extract_claims(
        Client(), source, text, "risk evaluations", [], char_budget=5000, progress=lambda rows: records.extend(rows)
    )
    # Every section group is reviewed (no selection call), so late regions are too.
    assert source.section_count == 20 and any("Chapter 19" in prompt for prompt in calls)
    assert any("Chapter 19" in loc for loc in source.reviewed_sections) and not source.omitted_sections and records
    assert not any("Section groups:" in prompt for prompt in calls)


@pytest.mark.asyncio
async def test_later_truncation_keeps_successful_checkpoint(cfg, store, monkeypatch):
    state = job(store)
    source = intake.make_url_source("https://device.example/manual")
    source.status = "fetched"
    state.sources = [source]
    cfg.limits.prompt_char_budget = 6000
    pipe = Pipeline(cfg, store)
    pipe._store_extract(state, source, "# First\n" + "Useful. " * 300 + "\n# Second\n" + "Other. " * 300)

    class Client:
        calls = 0
        from doclayer.curator.state import Usage

        usage = Usage()
        max_calls = cfg.limits.max_model_calls

        async def chat_bounded_json(self, system, user, *, char_budget):
            assert len(system) + len(user) <= char_budget
            return await self.chat_json(system, user)

        async def chat_verdicts(self, system, user, *, char_budget):
            assert len(system) + len(user) <= char_budget
            return await self.chat_json(system, user)

        async def chat_json(self, system, user):
            if user.startswith("SOURCE ANCESTOR CONSTRAINT CHECK"):
                item = json.loads(user.split("\n", 1)[1])
                context = item["ancestor_context"]
                quote = (
                    "Risk evaluation 19."
                    if "Risk evaluation 19." in context
                    else ("Useful." if "Useful." in context else "")
                )
                return {
                    "verdicts": [
                        {
                            "id": item["id"],
                            "supported": bool(quote),
                            "consistent": True,
                            "reason": "The actual selected local section documents this fact.",
                            "qualifiers": [],
                            "support_quotes": [quote] if quote else [],
                        }
                    ]
                }
            if "Verify selected claims against original source excerpts" in system:
                items = json.loads(user.split("\n", 1)[1])
                return {
                    "verdicts": [
                        {"id": item["id"], "supported": True, "reason": "supported source fixture"} for item in items
                    ]
                }
            self.calls += 1
            if self.calls == 2:
                raise OutputTruncated("later call")
            return {"claims": [{"claim": "Useful first fact", "locator": "#First"}]}

    monkeypatch.setattr(pipe, "_model", lambda _, stage="curation": Client())
    await pipe._stage_extract(state)
    loaded = store.load(state.job_id)
    assert len(store.read_evidence(state.job_id)) == 1
    assert loaded.sources[0].reviewed_sections == ["#First"] and loaded.sources[0].omitted_sections == ["#Second"]
    assert not loaded.sources[0].extraction_complete and loaded.coverage == "incomplete"


def test_page_evidence_targets_late_claims_and_different_subjects():
    noise = [
        Evidence("long", "vendor", "#Products", "paraphrase", f"Generic product description {i}") for i in range(150)
    ]
    needed = Evidence(
        "long",
        "vendor",
        "#Recovery",
        "data",
        "Firmware recovery requires disconnecting power supply before calibration",
    )
    account = Evidence("accounts", "account", "#Billing", "data", "The account renewal invoice closes monthly")
    records = noise + [needed, account]
    recovery = PageChange(
        "g1", "wiki/firmware-recovery.md", "create", title="Firmware recovery", rationale="Recovery calibration"
    )
    assert curate.select_page_evidence(records, recovery, limit=8)[0] == needed
    billing = PageChange("g2", "wiki/account-renewal.md", "create", title="Account renewal invoice")
    assert curate.select_page_evidence(records, billing, limit=8)[0] == account
    recovery.claim_ids = [curate.evidence_id(needed)]
    assert curate.select_page_evidence(records, recovery, limit=1) == [needed]


@pytest.mark.asyncio
async def test_no_page_changes_preserves_incomplete_source_coverage(cfg, store):
    state = job(store)
    state.coverage = "incomplete"
    state.coverage_gaps = ["discovery source limit", "late sections omitted"]
    pipe = Pipeline(cfg, store)
    state.stage = "place"
    await pipe.run(state)
    assert (
        state.status == "partial" and state.coverage == "incomplete" and "discovery source limit" in state.coverage_gaps
    )


@pytest.mark.asyncio
async def test_evidence_check_sees_unsupported_tail_inside_bounded_requests():
    checked = []

    class Client:
        async def chat_json(self, system, user):
            assert user.startswith("ARTICLE EDITORIAL REVIEW")
            assert len(system) + len(user) <= 6000
            frame = json.loads(user.split("\n", 1)[1])
            checked.append(frame)
            failures = []
            if "UNSUPPORTED_TAIL" in frame["body"]:
                section = next(
                    row for row in frame["sections"] if "UNSUPPORTED_TAIL" in frame["body"][row["start"] : row["end"]]
                )
                failures = [
                    {
                        "severity": "blocker",
                        "check": "evidence",
                        "detail": "Unsupported late claim",
                        "section_id": section["id"],
                        "anchor": "UNSUPPORTED_TAIL",
                    }
                ]
            return {"complete": True, "failures": failures, "conflicts": []}

    # Complete coherent sections retain all 700 original steps under the same
    # 6000-character bound; a single unsplittable section must budget-stop.
    body = (
        "# Procedure\n"
        + "".join(f"## Part {i}\n" + "Documented step. " * 100 + "\n" for i in range(7))
        + "## Last\nUNSUPPORTED_TAIL"
    )
    change = PageChange("g", "wiki/procedure.md", "create", draft_body=body)
    result = await curate.check_draft(Client(), "procedure", change, [], [], char_budget=6000)
    assert any(f["detail"] == "Unsupported late claim" for f in result["failures"])
    assert len(checked) > 1
    assert sum(frame["body"].count("Documented step.") for frame in checked) == 700
    assert any("UNSUPPORTED_TAIL" in frame["body"] for frame in checked)
    checked.clear()
    change.draft_body = "# Procedure\n" + "Documented step. " * 700 + "\n## Last\nUNSUPPORTED_TAIL"
    # Uncited split rule: the unsplittable section's new, uncited sentences are not
    # grounded against a partial view; each is reported as a new fact without a citation.
    result = await curate.check_draft(Client(), "procedure", change, [], [], char_budget=6000)
    assert checked == []  # no partial-section pass or hidden clipping
    details = " ".join(f["detail"] for f in result["failures"])
    # The tail section's governing context is the unsplittable section, so it is split too.
    assert "New fact without a citation: 'Documented step.'" in details
    assert "New fact without a citation: 'UNSUPPORTED_TAIL'" in details


def test_global_semantics_cover_local_text_and_pdf():
    restrictions = {"global": {"forbidden_claims": [r"\bmanaged-chat-endpoint\b"], "applicability": {"version": "2.0"}}}
    policy.validate_policy(restrictions)
    assert not policy.claim_decision(None, "Use managed-chat-endpoint for the model", {}, restrictions)[0]
    assert not policy.claim_decision(None, "Calibration interval changed", {"version": "1.0"}, restrictions)[0]
    assert policy.claim_decision(None, "Calibration interval changed", {"version": "2.0"}, restrictions)[0]


@pytest.mark.asyncio
async def test_legacy_pdf_locator_continuation_reviews_omitted_only():
    seen = []

    class Client:
        async def chat_json(self, system, user):
            seen.append(user)
            if "Verify selected claims against original source excerpts" in system:
                items = json.loads(user.split("\n", 1)[1])
                return {
                    "verdicts": [
                        {"id": item["id"], "supported": True, "reason": "supported source fixture"} for item in items
                    ]
                }
            return {"claims": [{"claim": "Late evaluation", "locator": "#PDF text (page map) / Page 2"}]}

    source = intake.make_url_source("https://vendor.example/card.pdf")
    source.reviewed_sections, source.omitted_sections = ["#Page 1"], ["#Page 2"]
    source.relevance["review_candidates"] = ["#Page 2"]
    text = "# PDF text (page map)\n## Page 1\nKnown facts.\n## Page 2\nLate evaluation."
    await curate.extract_claims(Client(), source, text, "late evaluation", [], char_budget=6000)
    assert len(seen) == 2 and "Known facts" not in seen[0] and "Late evaluation" in seen[0]
    assert source.section_count == 2 and not source.omitted_sections


def test_required_claim_outside_budget_is_explicit_missing(cfg, store):
    state = job(store)
    cfg.limits.prompt_char_budget = 4000
    evidence = [
        Evidence(
            "s",
            "source",
            "#Procedure",
            "data",
            "Recovery fact " + str(i) + " " + ("content " * 150),
            support_status="supported",
        )
        for i in range(5)
    ]
    change = PageChange(
        "g", "wiki/recovery.md", "create", title="Recovery", claim_ids=[curate.evidence_id(e) for e in evidence]
    )
    Pipeline(cfg, store)._page_evidence(state, change, evidence)
    assert (
        not change.missing_evidence and state.evidence_selection[change.path]["prompt_omitted"]
    )  # bounded additional drafting passes carry required support


def test_legacy_no_crawl_rebuilds_deferred_frontier(cfg, store):
    state = job(store)
    state.status, state.coverage = "published", "incomplete"
    source = intake.make_url_source("https://device.example/manual")
    source.status = "fetched"
    source.extraction_complete = True
    state.sources = [source]
    Pipeline(cfg, store)._store_extract(state, source, "# Device\n[Recovery](https://device.example/recovery)")
    store.save(state)
    reply = ops.handle("continue", {"job_id": state.job_id, "discover": False}, cfg, store)
    child = store.load(reply["job_id"])
    assert child.sources[-1].status == "deferred" and child.sources[-1].parent_id == source.id
    assert "reconstructed" in child.consolidation_notes[0]


def test_continue_cli_surface_accepts_targeted_policy_manifest():
    from doclayer.curator.cli import build_parser

    args = build_parser().parse_args(
        [
            "continue",
            "--job",
            "job-1234567890",
            "--source",
            "src-1",
            "--no-discover",
            "--policy-manifest",
            "/tmp/policy.yaml",
        ]
    )
    assert args.source == ["src-1"] and args.no_discover and args.policy_manifest == "/tmp/policy.yaml"


@pytest.mark.asyncio
async def test_collision_receives_draft_and_rename_preserves_content_identity():
    draft = PageChange(
        "g",
        "wiki/device.md",
        "create",
        title="Different device",
        draft_meta={"concept_id": "c-11112222", "privacy": "internal"},
        draft_body="# Different device\nImportant new claim [source: manual]",
    )

    class Client:
        async def chat_json(self, system, user):
            assert "Important new claim" in user and "c-11112222" in user and "Existing known fact" in user
            return {
                "resolution": "distinct-subject",
                "explanation": "Different model",
                "frontmatter": {"concept_id": "c-badbad00"},
                "body": "Hallucinated rewrite",
            }

    result = await curate.reconcile_collision(
        Client(), "device", draft, "# Current\nExisting known fact", char_budget=6000
    )
    assert result["body"] == draft.draft_body and result["frontmatter"] == draft.draft_meta


def test_queued_continuation_cache_survives_cleanup_cap_and_expiration(cfg, store):
    from doclayer.curator.worker import cleanup_cache

    state = job(store)
    path = cfg.cache_dir() / state.job_id
    path.mkdir(parents=True)
    (path / "extract.md").write_text("Cached evidence")
    cfg.cache.max_bytes = 1
    cleanup_cache(cfg, store, now=10**12)
    assert (path / "extract.md").read_text() == "Cached evidence"


@pytest.mark.asyncio
async def test_source_support_rejects_model_tool_misattribution_and_retains_excerpt(cfg, store, monkeypatch):
    state = job(store)
    source = intake.make_url_source("https://vendor.example/image-guide")
    source.status = "fetched"
    source.extraction_complete = True
    state.sources = [source]
    actual = "# API support\nVisionModel analyzes images and emits text through Chat API. ImageTool generates images through Response API only."
    pipeline = Pipeline(cfg, store)
    pipeline._store_extract(state, source, actual)
    false_claim = Evidence(
        source.id,
        "image guide",
        "#API support",
        "paraphrase",
        "VisionModel generates images through Chat API",
        quote="ImageTool generates images",
        url=source.url,
    )
    good_claim = Evidence(
        source.id,
        "image guide",
        "#API support",
        "paraphrase",
        "VisionModel analyzes images and emits text through Chat API",
        quote="VisionModel analyzes images and emits text through Chat API.",
        url=source.url,
    )
    store.append_evidence(state.job_id, [false_claim.to_dict(), good_claim.to_dict()])

    class Client:
        async def chat_json(self, system, user):
            assert "Verify selected claims against original source excerpts" in system
            records = json.loads(user.split("\n", 1)[1])
            verdicts = []
            for record in records:
                assert "ImageTool generates images through Response API only" in record["excerpt"]
                verdicts.append(
                    {
                        "id": record["id"],
                        "supported": record["claim"] == good_claim.text,
                        "reason": "Exact source model/tool applicability",
                    }
                )
            return {"verdicts": verdicts}

    change = PageChange(
        "g",
        "wiki/vision-model.md",
        "update",
        title="VisionModel image API",
        claim_ids=[curate.evidence_id(false_claim), curate.evidence_id(good_claim)],
    )
    await pipeline._verify_selected(state, change, Client())
    retained = pipeline._evidence(state)
    assert [record.text for record in retained] == [good_claim.text]
    assert retained[0].support_status == "supported" and "Response API only" in retained[0].quote
    assert store.load(state.job_id).evidence_verification[curate.evidence_id(false_claim)]["supported"] is False
    assert change.missing_evidence == [curate.evidence_id(false_claim)]


@pytest.mark.asyncio
async def test_malformed_source_verifier_cannot_authorize_claim():

    class Client:
        async def chat_json(self, system, user):
            return {"verdicts": []}

    record = Evidence("source", "manual", "#Setup", "data", "Unsupported model claim")
    with pytest.raises(ModelError, match="source-support"):
        await curate.verify_source_claims(Client(), [record], {"source": "# Setup\nUnrelated text"}, char_budget=6000)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["duplicate", "foreign", "missing", "unhashable"])
async def test_source_verifier_rejects_ambiguous_ids(mode):

    record = Evidence("source", "manual", "#Setup", "data", "Model fact")
    identity = curate.evidence_id(record)

    class Client:
        async def chat_json(self, system, user):
            good = {"id": identity, "supported": True, "reason": "supported"}
            responses = {
                "duplicate": [{"id": identity, "supported": False, "reason": "unsupported"}, good],
                "foreign": [dict(good, id="other")],
                "missing": [],
                "unhashable": [dict(good, id=[])],
            }
            return {"verdicts": responses[mode]}

    with pytest.raises(ModelError, match="source-support"):
        await curate.verify_source_claims(Client(), [record], {"source": "# Setup\nModel fact"}, char_budget=6000)


@pytest.mark.asyncio
async def test_verifier_outage_checkpoints_previous_batch(cfg, store):

    state = job(store)
    records = [Evidence("source", "manual", "#Setup", "data", f"Model fact {i}") for i in range(3)]

    class Client:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            if self.calls == 2:
                raise ModelError("inference outage", transient=True)
            items = json.loads(user.split("\n", 1)[1])
            return {"verdicts": [{"id": item["id"], "supported": True, "reason": "source supported"} for item in items]}

    pipeline = Pipeline(cfg, store)
    with pytest.raises(ModelError):
        await curate.verify_source_claims(
            Client(),
            records,
            {"source": "# Setup\nModel facts"},
            char_budget=6000,
            progress=lambda results: pipeline._retain_verification(state, results),
        )
    persisted = store.load(state.job_id)
    assert len(persisted.evidence_verification) == 3
    assert persisted.evidence_verification[curate.evidence_id(records[0])]["supported"]
    assert all(
        persisted.evidence_verification[curate.evidence_id(record)]["verification_pending"] for record in records[1:]
    )


@pytest.mark.asyncio
async def test_rejected_top_claims_verify_replacements_before_editorial_use(cfg, store, monkeypatch):
    state = job(store)
    source = intake.make_url_source("https://vendor.example/manual")
    source.status = "fetched"
    state.sources = [source]
    pipeline = Pipeline(cfg, store)
    pipeline._store_extract(state, source, "# Model\nCorrect final model fact")
    records = [
        Evidence(source.id, "manual", "#Model", "data", text, url=source.url)
        for text in ["Wrong first model fact", "Wrong second model fact", "Correct final model fact"]
    ]
    store.append_evidence(state.job_id, [e.to_dict() for e in records])
    monkeypatch.setattr(curate, "select_page_evidence", lambda records, change, limit=80: records[:1])
    verified = []

    class Client:
        async def chat_json(self, system, user):
            items = json.loads(user.split("\n", 1)[1])
            verified.extend(item["claim"] for item in items)
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": item["claim"].startswith("Correct"),
                        "reason": "Source applicability",
                    }
                    for item in items
                ]
            }

    change = PageChange("g", "wiki/model.md", "create", claim_ids=[curate.evidence_id(e) for e in records])
    await pipeline._verify_selected(state, change, Client())
    assert verified == [e.text for e in records]
    assert [e.text for e in pipeline._evidence(state)] == ["Correct final model fact"]
    assert all(e.support_status == "supported" for e in pipeline._evidence(state))
    assert set(change.missing_evidence) == {curate.evidence_id(e) for e in records[:2]}


@pytest.mark.asyncio
async def test_source_verification_resume_skips_checkpointed_batches():

    records = [Evidence("s", "manual", "#Model", "data", f"Model fact {i}") for i in range(3)]
    saved = {}

    class First:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            if self.calls == 2:
                raise ModelError("outage", transient=True)
            return {
                "verdicts": [
                    {"id": item["id"], "supported": True, "reason": "Supported"}
                    for item in json.loads(user.split("\n", 1)[1])
                ]
            }

    with pytest.raises(ModelError):
        await curate.verify_source_claims(
            First(), records, {"s": "# Model\nModel facts"}, char_budget=6000, progress=saved.update
        )
    resumed = []

    class Next:
        async def chat_json(self, system, user):
            items = json.loads(user.split("\n", 1)[1])
            resumed.extend(item["id"] for item in items)
            return {"verdicts": [{"id": item["id"], "supported": True, "reason": "Supported"} for item in items]}

    result = await curate.verify_source_claims(
        Next(), records, {"s": "# Model\nModel facts"}, char_budget=6000, known=saved, progress=saved.update
    )
    assert curate.evidence_id(records[0]) not in resumed and len(resumed) == 2 and len(result) == 3


@pytest.mark.asyncio
async def test_required_evidence_spans_bounded_drafting_passes_without_dropping_claims():
    import re

    records = [
        Evidence(
            "s",
            "manual",
            "#Facts",
            "data",
            f"Firmware documented fact {i}",
            quote="Original support. " * 12,
            support_status="supported",
        )
        for i in range(12)
    ]
    change = PageChange(
        "g", "wiki/firmware.md", "create", title="Firmware", claim_ids=[curate.evidence_id(e) for e in records]
    )
    prompts, included, checkpoints = [], set(), []

    class Client:
        async def chat_json(self, system, user):
            prompts.append(user)
            included.update(re.findall(r"claim-[0-9a-f]{16}", user))
            return {"frontmatter": {"schema_version": 1}, "body": "# Firmware\n" + "\n".join(sorted(included))}

    result = await curate.draft_page(
        Client(),
        "firmware",
        change,
        records,
        None,
        [],
        char_budget=6000,
        today="2026-10-02",
        progress=checkpoints.append,
    )
    assert len(prompts) > 1 and len(checkpoints) == len(prompts)
    assert all(identity in result["body"] for identity in change.claim_ids)
    assert all(len(prompt) < 6000 for prompt in prompts)


def test_long_tables_retain_columns_and_fenced_examples_remain_coherent():
    table = "## API matrix\n| Model | Input | Output |\n| --- | --- | --- |\n" + "".join(
        f"| Model-{i} | image | text |\n" for i in range(40)
    )
    chunks = intake.group_sections(intake.section_map(table), 200)
    for chunk in chunks:
        for locator, text in chunk:
            assert len(text) <= 200
            if "| Model-" in text:
                assert "| Model | Input | Output |" in text
    code = "## Procedure\n```python\n" + 'print("a")\n' * 40 + "```\n"
    chunks = intake.group_sections(intake.section_map(code), 200)
    assert len(chunks) > 1
    for chunk in chunks:
        for _, text in chunk:
            if "print(" in text:
                assert text.count("```") == 2


@pytest.mark.asyncio
async def test_single_batch_never_rewrites_truncated_operator_page():
    class Client:
        async def chat_json(self, system, user):
            import ast
            import json
            import re

            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
                identities = re.findall(r"^- \[([a-z]+-[0-9a-f]+) /", user.split("\nEvidence:\n", 1)[1], re.M)
                return {
                    "routes": [
                        {
                            "id": identity,
                            "section_id": frame["root_section_id"],
                            "new_heading": None,
                            "rationale": "General manual fact.",
                        }
                        for identity in identities
                    ]
                }
            assert "Never return the complete page" in user
            identities = ast.literal_eval(
                re.search(r"Claim IDs to (?:consider|integrate or prove already present): (.+)", user)[1]
            )
            return {
                "section_id": re.search(r"Section ID: (.+)", user)[1],
                "edits": [],
                "append": "New fact. [Source](https://manual.example)",
                "handled_claim_ids": identities,
                "claim_dispositions": [
                    {"id": identity, "status": "integrated", "anchor": "New fact."} for identity in identities
                ],
            }

    record = Evidence("s", "manual", "#Facts", "data", "New fact", support_status="supported")
    existing = "# Manual\n" + "Operator content. " * 500 + "\nLATE_OPERATOR_SENTINEL"
    change = PageChange("g", "wiki/manual.md", "update")
    result = await curate.draft_page(
        Client(), "manual", change, [record], existing, [], char_budget=6000, today="2026-10-02"
    )
    assert "LATE_OPERATOR_SENTINEL" in result["body"] and existing.rstrip("\n") in result["body"]


@pytest.mark.asyncio
async def test_structure_selection_previews_each_page_inside_group_and_bounds_every_request():
    import re

    prompts = []

    class Client:
        async def chat_json(self, system, user):
            prompts.append(user)
            assert len(user) <= 6000
            if "Select the most useful" in system:
                useful = [
                    int(line.split(":", 1)[0])
                    for line in user.splitlines()
                    if re.match(r"^\d+:", line) and "calibration-critical" in line
                ]
                return {"groups": useful[:1]}
            if "Verify selected claims" in system:
                items = json.loads(user.split("\n", 1)[1])
                return {
                    "verdicts": [
                        {"id": item["id"], "supported": True, "reason": "Actual section support"} for item in items
                    ]
                }
            return {"claims": [{"claim": "Calibration procedure is critical", "locator": "#Page 6"}]}

    source = intake.make_url_source("https://device.example/report.pdf")
    text = "\n".join(
        f"## Page {i}\n"
        + "Generic boilerplate. " * 12
        + ("calibration-critical detail in a late page of the group." if i == 6 else "Unrelated matter.")
        for i in range(50)
    )
    await curate.extract_claims(Client(), source, text, "calibration-critical details", [], char_budget=6000)
    # No selection request: every page group is extracted, each request bounded.
    assert not [prompt for prompt in prompts if "Section groups:" in prompt]
    assert any("Page 6" in prompt and "calibration-critical" in prompt for prompt in prompts)
    assert any("Page 6" in locator for locator in source.reviewed_sections) and not source.omitted_sections


def test_legacy_parent_marker_does_not_count_as_reviewed_child_content():
    source = intake.make_url_source("https://vendor.example/report.pdf")
    source.extraction_complete = True
    source.reviewed_sections = ["#PDF text (page map)", "#Page 1"]
    source.omitted_sections = ["#Page 2"]
    original = list(source.reviewed_sections)
    curate.map_source_structure(
        source, "# PDF text (page map)\n## Page 1\nKnown facts\n## Page 2\nUnreviewed risks", char_budget=6000
    )
    assert source.section_count == source.review_unit_count == 2
    assert source.reviewed_sections == ["#PDF text (page map) / Page 1"]
    assert source.omitted_sections == ["#PDF text (page map) / Page 2"]
    assert source.obsolete_reviewed_sections == ["#PDF text (page map)"]
    assert source.relevance["historical_reviewed_sections"] == original and not source.unknown_reviewed_sections


def test_unmapped_legacy_reviews_and_missing_maps_remain_unknown():
    source = intake.make_url_source("https://vendor.example/manual")
    source.extraction_complete = True
    source.reviewed_sections = ["#Removed code comment"]
    curate.map_source_structure(source, "# Manual\nFacts\n```sh\n# Removed code comment\n```", char_budget=6000)
    assert not source.reviewed_sections and source.unknown_reviewed_sections == ["#Removed code comment"]
    assert len(source.omitted_sections) == source.review_unit_count == 1
    assert source.omitted_categories["historical-reviewed"]
    empty = intake.make_url_source("https://vendor.example/guide")
    empty.extraction_complete = True
    curate.map_source_structure(empty, "# Procedure\nFacts", char_budget=6000)
    assert empty.unknown_reviewed_sections == ["(historical review map unavailable)"]
    assert empty.omitted_sections == ["#Procedure"]


def test_continuation_reports_known_gaps_immediately_before_worker_claim(cfg, store):
    state = job(store)
    state.status = "published"
    state.coverage = "incomplete"
    source = intake.make_url_source("https://device.example/report")
    source.status = "fetched"
    source.extraction_complete = True
    source.reviewed_sections = ["#PDF text (page map)", "#Page 1"]
    source.omitted_sections = ["#Page 2"]
    state.sources = [source]
    Pipeline(cfg, store)._store_extract(
        state,
        source,
        "# PDF text (page map)\n## Page 1\nKnown facts\n## Page 2\nUnreviewed risks\n[Next](https://device.example/next)",
    )
    store.save(state)
    parent = store.state_path(state.job_id).read_bytes()
    result = ops.handle("continue", {"job_id": state.job_id, "discover": False}, cfg, store)
    child = store.load(result["job_id"])
    status = ops.handle("status", {"job_id": child.job_id}, cfg, store)["job"]
    assert child.status == "queued" and status["coverage"] == "incomplete"
    assert status["sources"]["sections_mapped"] == status["sources"]["review_units_mapped"] == 2
    assert status["sources"]["sections_reviewed"] == 1 and status["sources"]["sections_omitted"] == 1
    assert status["sources"]["obsolete_parent_reviews"] == 1 and status["sources"]["pending"] == 0
    assert child.sources[-1].status == "deferred"
    assert store.state_path(state.job_id).read_bytes() == parent


@pytest.mark.asyncio
async def test_malformed_source_verdict_is_repaired_with_same_inputs_and_exact_ids():
    record = Evidence("s", "manual", "#Model", "data", "Model supports documented feature")

    class Client:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            inputs = json.loads(user.split("\n", 1)[1])
            assert [item["id"] for item in inputs] == [curate.evidence_id(record)]
            if self.calls == 1:
                return {"verdicts": [{"id": inputs[0]["id"], "supported": "yes", "reason": "wrong boolean type"}]}
            assert "previous response violated" in system
            return {
                "verdicts": [{"id": inputs[0]["id"], "supported": True, "reason": "Actual source supports the claim"}]
            }

    client = Client()
    result = await curate.verify_source_claims(client, [record], {"s": "# Model\nDocumented feature"}, char_budget=6000)
    assert client.calls == 2 and result[curate.evidence_id(record)]["supported"]


@pytest.mark.asyncio
async def test_repeated_schema_failure_is_bounded_and_retryable():

    record = Evidence("s", "manual", "#Model", "data", "Model fact")

    class Client:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            return {"verdicts": []}

    client = Client()
    with pytest.raises(ModelError) as error:
        await curate.verify_source_claims(client, [record], {"s": "# Model\nModel fact"}, char_budget=6000)
    assert client.calls == 3 and error.value.transient


@pytest.mark.asyncio
async def test_duplicate_input_claims_are_deduplicated_before_verification():
    record = Evidence("s", "manual", "#Model", "data", "Model fact")

    class Client:
        async def chat_json(self, system, user):
            items = json.loads(user.split("\n", 1)[1])
            assert len(items) == 1
            return {"verdicts": [{"id": items[0]["id"], "supported": True, "reason": "Supported"}]}

    result = await curate.verify_source_claims(
        Client(), [record, record], {"s": "# Model\nModel fact"}, char_budget=6000
    )
    assert len(result) == 1


def test_unfinished_failure_can_resume_but_completed_report_is_immutable(cfg, store):
    state = job(store)
    state.status = "failed"
    state.stage = "extract"
    state.error = "inference: invalid source-support verification response"
    state.evidence_verification = {"claim-retained": {"supported": True, "reason": "Previously verified"}}
    store.save(state)
    response = ops.handle("resume", {"job_id": state.job_id}, cfg, store)
    assert (
        response["status"] == "queued" and store.load(state.job_id).evidence_verification == state.evidence_verification
    )
    rejected = job(store)
    rejected.status = "failed"
    rejected.stage = "report"
    rejected.stages_done = ["report"]
    rejected.error = "publication: rejected changes"
    store.save(rejected)
    response = ops.handle("resume", {"job_id": rejected.job_id}, cfg, store)
    assert response["status"] == "failed" and store.load(rejected.job_id).status == "failed"


@pytest.mark.asyncio
async def test_source_verdict_repairs_obey_real_model_call_budget(cfg):
    from doclayer.curator.model import ModelClient
    from doclayer.curator.state import BudgetExceeded, Usage

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"verdicts":[]}'}, "finish_reason": "stop"}]}
        )

    usage = Usage()
    client = ModelClient(cfg, usage, limits={"max_model_calls": 1}, transport=httpx.MockTransport(respond))
    record = Evidence("s", "manual", "#Model", "data", "Model fact")
    with pytest.raises(BudgetExceeded):
        await curate.verify_source_claims(client, [record], {"s": "# Model\nModel fact"}, char_budget=6000)
    assert usage.model_calls == len(requests) == 1


def test_unfinished_review_reports_incomplete_in_status_and_failure_digest(cfg, store):
    state = job(store)
    source = intake.make_url_source("https://vendor.example/model")
    source.status = "fetched"
    state.sources = [source]
    state.status = "retryable"
    state.stage = "extract"
    store.save(state)
    assert ops.handle("status", {"job_id": state.job_id}, cfg, store)["job"]["coverage"] == "incomplete"
    Pipeline(cfg, store)._finish_report(state)
    assert state.coverage == "incomplete" and "**Coverage:** incomplete" in state.summary
    # A completed, explicitly mapped review can honestly report complete;
    # the transient processing state alone does not permanently set a gap.
    state.coverage = "complete"
    source.extraction_complete = True
    source.review_unit_count = 1
    source.reviewed_sections = ["#Model"]
    assert state.effective_coverage == "complete"


@pytest.mark.asyncio
async def test_exact_quote_tail_survives_verification_and_stale_rejection_is_rechecked():
    quote = "Background context. " * 90 + "The device requires firmware version 7."
    assert len(quote) < 2000
    record = Evidence("s", "manual", "#Model", "data", "Device requires firmware version 7", quote=quote)
    text = "# Model\n" + "Earlier unrelated context. " * 20 + quote + "\nEnd."
    excerpt = curate.support_excerpt(record, text, 2000)
    assert quote in excerpt and len(excerpt) <= 2000
    identity = curate.evidence_id(record)

    class Client:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            item = json.loads(user.split("\n", 1)[1])
            if user.startswith("SOURCE ANCESTOR CONSTRAINT CHECK"):
                context = item["ancestor_context"] + item["local_excerpt"]
                tail = "The device requires firmware version 7."
                return {
                    "verdicts": [
                        {
                            "id": item["id"],
                            "supported": tail in context,
                            "consistent": True,
                            "reason": "Actual source tail establishes firmware qualification.",
                            "qualifiers": [tail] if tail in item["ancestor_context"] else [],
                            "support_quotes": [tail] if tail in context else [],
                        }
                    ]
                }
            assert quote in item[0]["excerpt"]
            return {
                "verdicts": [
                    {"id": identity, "supported": True, "reason": "Exact source tail supports firmware requirement"}
                ]
            }

    client = Client()
    known = {
        identity: {
            "supported": False,
            "reason": "Previous clipped excerpt lacked the required version",
            "excerpt": quote[:-100],
        }
    }
    result = await curate.verify_source_claims(client, [record], {"s": text}, char_budget=6000, known=known)
    assert result[identity]["supported"] and result[identity]["support_version"] == curate.SOURCE_SUPPORT_VERSION
    completed_calls = client.calls
    assert completed_calls > 0 and "firmware version 7" in result[identity]["excerpt"]
    await curate.verify_source_claims(client, [record], {"s": text}, char_budget=6000, known=result)
    assert client.calls == completed_calls  # current-version progress survives restart


@pytest.mark.asyncio
async def test_oversized_exact_support_is_unreviewed_not_semantically_rejected():
    record = Evidence("s", "manual", "#Model", "data", "Qualified fact", quote="x" * 2100)

    class NoModel:
        async def chat_json(self, *args):
            pytest.fail("Incomplete source support must not be judged as a false claim")

    result = await curate.verify_source_claims(NoModel(), [record], {"s": "# Model\n" + "x" * 2100}, char_budget=6000)
    assert result[curate.evidence_id(record)]["reason"] == "selected source support exceeds bounded context"


@pytest.mark.asyncio
async def test_original_quote_still_rejects_model_tool_misattribution():
    quote = "Chat completions analyzes input images and generates text. The image tool generates images using the separate image model."
    record = Evidence(
        "s", "manual", "#API", "data", "Language model generates images through chat completions", quote=quote
    )

    class Client:
        async def chat_json(self, system, user):
            item = json.loads(user.split("\n", 1)[1])[0]
            assert quote in item["excerpt"]
            return {
                "verdicts": [
                    {
                        "id": item["id"],
                        "supported": False,
                        "reason": "Image generation belongs to the image model and tool",
                    }
                ]
            }

    result = await curate.verify_source_claims(Client(), [record], {"s": "# API\n" + quote}, char_budget=6000)
    assert not result[curate.evidence_id(record)]["supported"]


@pytest.mark.asyncio
async def test_schema_repair_feedback_fits_combined_prompt_budget():
    quote = "A documented source qualification. " * 55
    record = Evidence("s", "manual", "#Model", "data", "A qualified claim. " * 130, quote=quote)

    class Client:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            assert len(system) + len(user) <= 6000
            item = json.loads(user.split("\n", 1)[1])[0]
            assert item["claim"] == record.text and quote in item["excerpt"]
            if self.calls == 1:
                return {"verdicts": [], "extra": "untrusted previous reply " * 1000}
            return {"verdicts": [{"id": item["id"], "supported": True, "reason": "Supported"}]}

    client = Client()
    result = await curate.verify_source_claims(client, [record], {"s": "# Model\n" + quote}, char_budget=6000)
    assert client.calls == 2 and result[curate.evidence_id(record)]["supported"]


@pytest.mark.asyncio
async def test_complete_four_verdict_root_array_preserves_every_claim(cfg):
    from doclayer.curator.model import ModelClient
    from doclayer.curator.state import Usage

    records = [Evidence("s", "manual", "#API", "data", f"Feature {i}") for i in range(4)]

    def respond(request):
        user = json.loads(request.content)["messages"][1]["content"]
        items = json.loads(user.split("\n", 1)[1])
        assert len(items) == 4
        verdicts = [{"id": item["id"], "supported": True, "reason": "Supported"} for item in items]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(verdicts)}, "finish_reason": "stop"}]}
        )

    client = ModelClient(cfg, Usage(), transport=httpx.MockTransport(respond))
    result = await curate.verify_source_claims(client, records, {"s": "# API\nDocumented features"}, char_budget=16000)
    assert set(result) == {curate.evidence_id(r) for r in records} and client.usage.model_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["duplicate", "missing", "foreign", "boolean"])
async def test_invalid_root_arrays_keep_strict_guards_and_bounded_diagnostics(cfg, defect):
    from doclayer.curator.model import ModelClient
    from doclayer.curator.state import Usage

    records = [Evidence("s", "manual", "#API", "data", f"Feature {i}") for i in range(4)]

    def respond(request):
        items = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        verdicts = [{"id": item["id"], "supported": True, "reason": "Supported"} for item in items]
        if defect == "duplicate":
            verdicts[-1] = dict(verdicts[0])
        elif defect == "missing":
            verdicts.pop()
        elif defect == "foreign":
            verdicts[-1]["id"] = "foreign"
        else:
            verdicts[-1]["supported"] = "true"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(verdicts)}, "finish_reason": "stop"}]}
        )

    client = ModelClient(cfg, Usage(), transport=httpx.MockTransport(respond))
    with pytest.raises(ModelError) as error:
        await curate.verify_source_claims(client, records, {"s": "# API\nDocumented features"}, char_budget=16000)
    assert error.value.transient and client.usage.model_calls == 3
    diagnostic = error.value.diagnostic
    assert diagnostic["operation"] == "source-support" and diagnostic["attempts"] == 3
    assert len(json.dumps(diagnostic)) < 2000 and "reason" not in diagnostic
    if defect == "duplicate":
        assert diagnostic["duplicate_ids"] and diagnostic["missing_ids"]
    if defect == "missing":
        assert diagnostic["verdict_count"] == 3 and diagnostic["missing_ids"]
    if defect == "foreign":
        assert diagnostic["foreign_ids"] == ["foreign"]
    if defect == "boolean":
        assert diagnostic["item_field_types"][-1]["supported"] == "str"


@pytest.mark.asyncio
async def test_failure_shape_diagnostics_survive_supported_resume(cfg, store):

    class Broken(Pipeline):
        async def _run_stage(self, state):
            raise ModelError(
                "invalid source-support verification response",
                diagnostic={"operation": "source-support", "verdict_type": "dict"},
            )

    state = job(store)
    state.stage = "extract"
    state.inference_diagnostics = [{"operation": "previous"}] * 3
    result = await Broken(cfg, store).run(state)
    assert result.status == "retryable" and len(result.inference_diagnostics) == 3
    ops.handle("resume", {"job_id": state.job_id}, cfg, store)
    reloaded = store.load(state.job_id)
    assert reloaded.status == "queued" and reloaded.inference_diagnostics[-1]["verdict_type"] == "dict"


def test_pdf_whitespace_quote_retains_exact_true_model_source_not_similar_other_model():
    source = "# PDF text (page map)\n## Page 7\nInasimulationusingmorethan\n54,000internaltasks,HeliosreceivedroughlyhalfasmanyflagsasSol.\n## Page 133\nSol produced 36% fewer flags in a simulation using 54,000 internal tasks.\n"
    record = Evidence(
        "s",
        "system card",
        "Page 7, Section 1 Overview",
        "paraphrase",
        "Helios received roughly half as many flags in a simulation of 54,000 internal tasks.",
        quote="In a simulation using more than 54,000 internal tasks, Helios received roughly half as many flags as Sol.",
    )
    excerpt = curate.support_excerpt(record, source, 180)
    assert "HeliosreceivedroughlyhalfasmanyflagsasSol." in excerpt
    assert "36% fewer" not in excerpt
    assert excerpt in source and len(excerpt) <= 180


def test_nonmatching_pdf_quote_fallback_respects_locator_and_does_not_borrow_other_model():
    source = "# PDF text (page map)\n## Page 21\nHelios HealthBench score 36.6.\n## Page 126\nSol HealthBench score 30.1 and an increase of 3.5 points; lengthy benchmark explanation.\n"
    wrong = Evidence(
        "s",
        "card",
        "#Page 21",
        "paraphrase",
        "Helios HealthBench score is 30.1 with an increase of 3.5 points.",
        quote="fabricated HealthBench quote 30.1 with an increase of 3.5",
    )
    excerpt = curate.support_excerpt(wrong, source, 1000)
    assert "Helios HealthBench score 36.6." in excerpt and "Sol HealthBench" not in excerpt
    assert "fabricated" not in excerpt


@pytest.mark.asyncio
async def test_rechecks_obsolete_false_verdict_with_actual_normalized_quote():
    source = "# Card\n## Page 21\nHelios HealthBenchHardscoreof36.6(+3.5).\n## Page 126\nSol HealthBench score 30.1.\n"
    record = Evidence(
        "s",
        "card",
        "#Page 21",
        "paraphrase",
        "Helios HealthBench Hard score is 36.6, an increase of 3.5 points.",
        quote="HealthBench Hard score of 36.6 (+3.5)",
    )
    identity = curate.evidence_id(record)

    class Client:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            assert "HealthBenchHardscoreof36.6(+3.5)" in user
            return {
                "verdicts": [
                    {"id": identity, "supported": True, "reason": "Correct target model source context supports value."}
                ]
            }

    client = Client()
    result = await curate.verify_source_claims(
        client,
        [record],
        {"s": source},
        char_budget=6000,
        known={identity: {"supported": False, "reason": "Wrong model table was selected", "support_version": 2}},
    )
    assert (
        client.calls == 1
        and result[identity]["supported"]
        and result[identity]["support_version"] == curate.SOURCE_SUPPORT_VERSION
    )


def test_support_missing_pdf_page_and_ambiguous_model_quote_are_unknown():
    source = "# Card\n## Page 2\nWrongModel score 90%.\n## Page 7\nTargetModel score 90%.\n"
    missing = Evidence("s", "card", "Page 99", "paraphrase", "TargetModel score 90%.", quote="score 90%.")
    assert curate.support_excerpt(missing, source) == ""
    target = Evidence("s", "card", "Page 7", "paraphrase", "TargetModel score 90%.", quote="score 90%.")
    assert "TargetModel" in curate.support_excerpt(target, source) and "WrongModel" not in curate.support_excerpt(
        target, source
    )
    unknown = Evidence("s", "card", "unmapped", "paraphrase", "TargetModel score 90%.", quote="score 90%.")
    assert curate.support_excerpt(unknown, source) == ""


def test_quote_context_uses_fence_aware_boundaries_not_code_comments():
    source = '# Manual\nQualified example is test-only.\n```python\n# Comment looks like a heading\nresult = "qualified"\n```\nNever use this example in production.\n## Other\nOther context.\n'
    record = Evidence("s", "manual", "#Manual", "quote", "Example is test-only.", quote='result = "qualified"')
    excerpt = curate.support_excerpt(record, source)
    assert "Qualified example is test-only." in excerpt and "Never use this example in production." in excerpt
    assert "Other context." not in excerpt


def test_pdf_quote_handles_line_wrap_hyphenation_but_returns_original_qualified_bytes():
    source = "# Card\n## Page 8\nHelios is less likely to include incrimi-\nnating information; this is an adversarial evaluation only.\n## Page 68\nSol uses a different monitoring method.\n"
    record = Evidence(
        "s",
        "card",
        "Page 8",
        "paraphrase",
        "Helios includes less incriminating information in adversarial evaluations.",
        quote="less likely to include incriminating information",
    )
    excerpt = curate.support_excerpt(record, source)
    assert "incrimi-\nnating" in excerpt and "adversarial evaluation only" in excerpt
    assert "Sol uses" not in excerpt and excerpt in source
    negative = Evidence("s", "card", "Page 8", "quote", "Score is -1.", quote="Score is 1.")
    assert curate._quote_span(negative.quote, "Score is -1.") is None


def test_source_quote_ambiguity_is_union_of_literal_and_linewrap_matches():
    source = "# Card\n## Model A\nNative image generation is unavailable.\n## Model B\nNative image gene-\nration is unavailable.\n"
    quote = "Native image generation is unavailable."
    assert curate._quote_span(quote, source) is None
    unknown = Evidence("s", "card", "unmapped", "quote", quote, quote=quote)
    assert curate.support_excerpt(unknown, source) == ""
    for name in ["Model A", "Model B"]:
        record = Evidence("s", "card", name, "quote", quote, quote=quote)
        excerpt = curate.support_excerpt(record, source)
        assert name in excerpt and ("Model B" if name == "Model A" else "Model A") not in excerpt


def test_support_excerpt_retains_ancestor_hypothetical_provider_qualifications():
    source = (
        "# Model card\n## Hypothetical stress-test setup only\n### Model A\nNative image generation is available.\n"
    )
    record = Evidence(
        "s",
        "card",
        "Model A",
        "quote",
        "Model A natively generates images.",
        quote="Native image generation is available.",
    )
    excerpt = curate.support_excerpt(record, source)
    assert "Hypothetical stress-test setup only" in excerpt and "Native image generation is available." in excerpt
    assert len(excerpt) <= 2000
    assert curate.support_excerpt(record, source, 40) == ""  # cannot discard binding context to fit


def test_source_support_preserves_parent_prose_conditions_and_global_preamble():
    source = "This manual describes a hypothetical experiment only.\n# Provider manual\nThese statements apply only to the test edition.\n## Device\nNative images are available.\n"
    record = Evidence(
        "s", "manual", "Device", "quote", "Native images are available.", quote="Native images are available."
    )
    excerpt = curate.support_excerpt(record, source)
    assert (
        "hypothetical experiment only" in excerpt and "only to the test edition" in excerpt and record.quote in excerpt
    )
    oversized = source.replace(
        "These statements apply only to the test edition.",
        "Background. " * 250 + "These statements apply only to the test edition.",
    )
    assert curate.support_excerpt(record, oversized) == ""
