"""Durable repair generations and observed cancellation survive worker restarts."""

import httpx
import pytest
from conftest import run_git

from doclayer.curator import curate
from doclayer.curator.model import ModelError
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import JobState, PageChange
from doclayer.curator.validate import Failure


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary,charged", [("before", 0), ("admitted", 1)])
async def test_cancel_written_during_admission_persistence_never_dispatches(cfg, store, boundary, charged):
    state = store.create(JobState(job_id="", idempotency_key="", intent="device"))[0]
    state.stage = "extract"
    state.limits = {"max_model_calls": 10, "max_active_seconds": 100}
    store.save(state)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})

    class Calls(Pipeline):
        injected = False

        def _persist(self, current, *, check_budget=True):
            # A separate operator has written cancellation exactly between the
            # preceding check and the persistence merge, using the real store.
            if (
                not self.injected
                and current.status == "processing"
                and (
                    (boundary == "before" and current.usage.model_calls == 0 and self.entered)
                    or (boundary == "admitted" and current.usage.model_calls == 1)
                )
            ):
                external = store.load(current.job_id)
                external.cancel_requested = True
                store.save(external)
                self.injected = True
            super()._persist(current, check_budget=check_budget)

        entered = False

        async def _run_stage(self, current):
            self.entered = True
            await self._model(current).chat("contract", "source")
            pytest.fail("cancelled request returned")

    result = await Calls(cfg, store, inference_transport=httpx.MockTransport(respond)).run(state)
    assert result.status == "cancelled" and result.cancel_requested
    assert result.usage.model_calls == charged
    assert store.load(state.job_id).usage.model_calls == charged
    assert not requests


def validation_job(store, checkout):
    state = store.create(
        JobState(job_id="", idempotency_key="", intent="device", base_commit=run_git(checkout, "rev-parse", "HEAD"))
    )[0]
    state.stage = "validate"
    state.status = "processing"
    state.limits = {"max_repair_passes": 2}
    state.changes = [PageChange("g", "wiki/new.md", "create", draft_body="# Device\nWRONG\n", status="drafted")]
    store.save(state)
    return state


def test_stale_reconciliation_reads_recorded_blob_not_current_operator_edit(cfg, store, checkout):
    from doclayer.curator import gittools

    state = validation_job(store, checkout)
    path = "wiki/existing-model.md"
    original = (checkout / path).read_text()
    change = PageChange(
        "g",
        path,
        "update",
        base_commit=state.base_commit,
        base_blob=gittools.blob_hash(checkout, f"{state.base_commit}:{path}"),
    )
    (checkout / path).write_text(original + "\nOperator later edit.\n")
    run_git(checkout, "add", path)
    run_git(checkout, "commit", "-qm", "operator later edit")
    runner = Pipeline(cfg, store)
    assert runner._reconciliation_base(state, change) == original
    change.base_blob = gittools.blob_hash(checkout, f"HEAD:{path}")
    with pytest.raises(ModelError, match="does not match"):
        runner._reconciliation_base(state, change)


def fake_validation(monkeypatch, checks):
    import doclayer.curator.pipeline as module

    async def verify(*args):
        pass

    async def check(*args, **kwargs):
        return {"failures": [], "conflicts": []}

    def deterministic(change, *args):
        checks.append(change.draft_body)
        return [Failure("test", "still wrong")]

    monkeypatch.setattr(Pipeline, "_verify_selected", verify)
    monkeypatch.setattr(Pipeline, "_evidence", lambda *args: [])
    monkeypatch.setattr(Pipeline, "_scope_failures", lambda *args: [])
    monkeypatch.setattr(module, "run_deterministic_checks", deterministic)
    monkeypatch.setattr(curate, "check_draft", check)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["body", "intent", "base", "correction", "source"])
async def test_changed_generation_rechecks_material_without_resetting_repair_cap(
    cfg, store, checkout, monkeypatch, mutation
):
    checks = []
    fake_validation(monkeypatch, checks)
    state = validation_job(store, checkout)
    calls = []
    if mutation == "source":
        from doclayer.curator.state import Evidence, SourceRecord

        records = [
            Evidence(
                "s",
                "manual",
                "#Manual",
                "quote",
                "Documented fact",
                quote="Documented fact",
                support_status="supported",
            )
        ]
        state.sources = [SourceRecord("s", "text", "manual", content_hash="original")]
        monkeypatch.setattr(Pipeline, "_evidence", lambda *args: records)
        store.save(state)

    async def repair(client, topic, change, failures, *args, progress=None, **kwargs):
        calls.append((topic, change.repair_passes, dict(change.draft_progress)))
        if len(calls) == 1:
            progress(
                {"body": change.draft_body + "partial\n", "frontmatter": {}, "draft_progress": {"old_generation": True}}
            )
            raise ModelError("outage")
        return {"body": change.draft_body, "frontmatter": {}, "draft_progress": {}}

    monkeypatch.setattr(curate, "repair_draft", repair)
    with pytest.raises(ModelError):
        await Pipeline(cfg, store)._stage_validate(state)
    state = store.load(state.job_id)
    if mutation == "body":
        state.changes[0].draft_body += "external edit\n"
    elif mutation == "intent":
        state.intent = "new intent"
    elif mutation == "base":
        state.changes[0].base_blob = "new canonical blob"
    elif mutation == "source":
        state.sources[0].content_hash = "changed-source-revision"
    else:
        monkeypatch.setattr(store, "corrections", lambda: [{"page": "wiki/new.md", "correction": "new constraint"}])
    store.save(state)
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert change.status == "failed" and change.repair_passes == 2
    assert len(calls) == 2 and calls[1][2] == {}
    assert len(checks) == 3  # initial, changed generation, completed second pass
    # A further worker resume cannot silently admit a third pass.
    await Pipeline(cfg, store)._stage_validate(store.load(state.job_id))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_saved_repair_context_tampering_is_rejected_before_model_call():
    from test_bounded_editing import response

    body = "# Device\n" + "".join(f"line{i:03d} operator fact.\n" for i in range(240))
    change = PageChange("g", "wiki/device.md", "update", draft_body=body)

    class Interrupted:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            if self.calls == 2:
                raise ModelError("outage")
            return response(user)

    def checkpoint(result):
        change.draft_body = result["body"]
        change.draft_meta = result["frontmatter"]
        change.draft_progress = result["draft_progress"]

    with pytest.raises(ModelError, match="outage"):
        await curate.repair_draft(Interrupted(), "device", change, [], [], [], char_budget=12000, progress=checkpoint)
    change.draft_progress["repair_views"][1]["text"] = "untrusted replacement of unseen operator facts"

    class Forbidden:
        async def chat_json(self, *args):
            pytest.fail("content-unbound saved view reached model")

    with pytest.raises(ModelError, match="content binding mismatch"):
        await curate.repair_draft(Forbidden(), "device", change, [], [], [], char_budget=12000)
    assert change.draft_body == body


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["outage", "cancel"])
async def test_pipeline_check_progress_is_durable_and_honors_cancel(cfg, store, checkout, monkeypatch, stop):
    from test_draft_claim_grounding import ArticleModel, record

    import doclayer.curator.pipeline as module
    from doclayer.curator.pipeline import CancelledByRequest

    records = [record(i) for i in range(20)]
    # Twenty genuinely distinct section facts: generic same-subject vocabulary
    # would make all twenty records relevant to every 6000-character task and
    # correctly stop before inference under the complete-evidence contract.
    for i, item in enumerate(records):
        item.text = item.quote = f"Facet{i} is value{i}."
    state = validation_job(store, checkout)
    state.changes[0].draft_body = "# Device\n" + "\n".join(item.text for item in records)
    state.changes[0].claim_ids = []
    store.save(state)

    async def verify(*args):
        pass

    monkeypatch.setattr(Pipeline, "_verify_selected", verify)
    monkeypatch.setattr(Pipeline, "_evidence", lambda *args: records)
    monkeypatch.setattr(Pipeline, "_scope_failures", lambda *args: [])
    monkeypatch.setattr(module, "run_deterministic_checks", lambda *args: [])
    # Force multiple bounded whole-section tasks, not a synthetic claim batch.
    cfg.limits.prompt_char_budget = 6000
    state.changes[0].draft_body = (
        "# Device\n"
        + records[0].text
        + "\n"
        + "\n".join(
            f"## Fact {i}\n{item.text}\n" + "Ordinary contextual background. " * 30
            for i, item in enumerate(records[1:], 1)
        )
    )
    store.save(state)

    class Interrupted(ArticleModel):
        async def chat_json(self, system, user):
            reply = await super().chat_json(system, user)
            frame = self.frames[-1]
            assert frame["mode"] == "grounding"
            supplied = {row["id"]: row for row in frame["evidence"]}
            for item in records:
                if item.text in frame["body"]:
                    row = supplied[curate.evidence_id(item)]
                    assert frame["source_excerpts"][row["quote_ref"]] == item.quote
            if stop == "cancel" and self.calls == 1:
                external = store.load(state.job_id)
                external.cancel_requested = True
                store.save(external)
            return reply

    client = Interrupted(fail_after=1 if stop == "outage" else None)
    monkeypatch.setattr(Pipeline, "_model", lambda *args: client)
    with pytest.raises(
        ModelError if stop == "outage" else CancelledByRequest, match="inference outage" if stop == "outage" else None
    ):
        await Pipeline(cfg, store)._stage_validate(state)
    recovered = store.load(state.job_id)
    saved = recovered.changes[0].check_progress["article_review"]
    assert len(saved["results"]) == 1
    assert next(iter(saved["results"].values()))["complete"]
    first_frame = client.frames[0]
    if stop == "cancel":
        assert recovered.cancel_requested and client.calls == 1
        return
    assert client.calls == 2  # one complete task, then the intended model outage
    resumed = Interrupted()
    monkeypatch.setattr(Pipeline, "_model", lambda *args: resumed)
    await Pipeline(cfg, store)._stage_validate(recovered)
    assert recovered.changes[0].status == "validated"
    assert first_frame not in resumed.frames  # completed exact section is not replayed


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_pipeline_reviews_fact_beyond_80_and_blocks_missing_current_backing(
    cfg, store, checkout, monkeypatch, missing
):
    from test_draft_claim_grounding import ArticleModel

    import doclayer.curator.pipeline as module
    from doclayer.curator.state import Evidence, SourceRecord

    cfg.limits.prompt_char_budget = 12000
    state = validation_job(store, checkout)
    change = state.changes[0]
    change.source_ids = ["primary", "old"]
    change.draft_body = "# Device\nDevice qualified fact 99.\n"
    original = change.draft_body
    state.sources = [SourceRecord("primary", "text", "manual"), SourceRecord("old", "text", "old manual")]
    primary = [
        Evidence(
            "primary",
            "manual",
            "#Manual",
            "quote",
            f"Device qualified fact {i}.",
            quote=f"Device qualified fact {i}.",
            support_status="supported",
        )
        for i in range(80)
    ]
    old = Evidence(
        "old",
        "old manual",
        "#Manual",
        "quote",
        "Device qualified fact 99.",
        quote="Device qualified fact 99.",
        support_status="unknown" if missing else "supported",
    )
    store.append_evidence(state.job_id, [item.to_dict() for item in primary + [old]])
    runner = Pipeline(cfg, store)
    runner._store_extract(state, state.sources[0], "# Manual\n" + "\n".join(item.text for item in primary))
    if not missing:
        runner._store_extract(state, state.sources[1], "# Manual\n" + old.text)
        state.evidence_verification[curate.evidence_id(old)] = {
            "supported": True,
            "status": "supported",
            "support_version": curate.SOURCE_SUPPORT_VERSION,
            "source_binding": curate.source_support_binding(old, {"old": "# Manual\n" + old.text}),
        }
    store.save(state)

    class Model(ArticleModel):
        async def chat_json(self, system, user):
            import json

            reply = await super().chat_json(system, user)
            frame = json.loads(user.split("\n", 1)[1])
            if missing:
                reply["failures"] = [
                    {
                        "severity": "blocker",
                        "check": "article-grounding",
                        "detail": "No current source backs the new fact 99 assertion.",
                        "section_id": frame["target_section_ids"][0],
                        "anchor": old.text,
                    }
                ]
            else:
                assert any(row["text"] == old.text for row in frame["evidence"])
            return reply

    async def unchanged(client, intent, target, *args, **kwargs):
        return {"body": target.draft_body, "frontmatter": target.draft_meta}

    monkeypatch.setattr(curate, "repair_draft", unchanged)
    monkeypatch.setattr(module, "run_deterministic_checks", lambda *args: [])
    monkeypatch.setattr(Pipeline, "_scope_failures", lambda *args: [])
    client = Model()
    monkeypatch.setattr(Pipeline, "_model", lambda *args: client)
    await runner._stage_validate(state)
    assert change.draft_body == original
    assert len(runner._evidence(state)) == 81
    assert old.support_status == ("unknown" if missing else "supported")
    assert change.status == ("failed" if missing else "validated")
    assert change.repair_passes == (2 if missing else 0)
    assert not state.publication.merged
    assert bool(change.check_progress["article_review"]["failures"]) == missing


def test_retained_support_binding_tracks_scope_role_source_content_and_canonical_revision(cfg, store, checkout):
    from doclayer.curator.state import SourceRecord

    state = validation_job(store, checkout)
    state.sources = [SourceRecord("s", "text", "manual")]
    runner = Pipeline(cfg, store)
    runner._store_extract(state, state.sources[0], "# Manual\nOriginal source statement.\n")
    store.save(state)
    previous = runner._support_resolver(state, object()).binding
    for change in [
        lambda: state.sources[0].scope.update(model="different"),
        lambda: setattr(state.sources[0], "role", "reference"),
        lambda: setattr(state, "base_commit", "new canonical revision"),
        lambda: runner._store_extract(state, state.sources[0], "# Manual\nChanged source statement.\n"),
    ]:
        change()
        current = runner._support_resolver(state, object()).binding
        assert current != previous
        previous = current


@pytest.mark.asyncio
async def test_post_reconcile_missing_current_support_blocks_candidate_without_altering_draft(
    cfg, store, checkout, monkeypatch
):
    from test_draft_claim_grounding import ArticleModel

    from doclayer.curator import reconciliation
    from doclayer.curator.publish import ReconcileFailed
    from doclayer.curator.state import Evidence, SourceRecord

    state = validation_job(store, checkout)
    state.stage = "publish"
    change = state.changes[0]
    change.status = "validated"
    change.path = "wiki/existing-model.md"
    change.action = "update"
    change.base_blob = run_git(checkout, "rev-parse", f"{state.base_commit}:{change.path}")
    change.source_ids = ["primary"]
    change.draft_body = "# Device\nDevice qualified fact 0.\n"
    original = change.draft_body
    state.sources = [SourceRecord("primary", "text", "manual"), SourceRecord("old", "text", "old manual")]
    records = [
        Evidence(
            "primary",
            "manual",
            "#Manual",
            "quote",
            "Device qualified fact 0.",
            quote="Device qualified fact 0.",
            support_status="supported",
        ),
        Evidence(
            "old",
            "old manual",
            "#Manual",
            "quote",
            "Device qualified fact 99.",
            quote="Device qualified fact 99.",
            support_status="unknown",
        ),
    ]
    store.append_evidence(state.job_id, [item.to_dict() for item in records])
    Pipeline(cfg, store)._store_extract(state, state.sources[0], "# Manual\nDevice qualified fact 0.")
    store.save(state)
    cfg.limits.prompt_char_budget = 12000

    class GroundedModel(ArticleModel):
        async def chat_json(self, system, user):
            import json

            reply = await super().chat_json(system, user)
            frame = json.loads(user.split("\n", 1)[1])
            if frame["mode"] == "grounding" and records[1].text in frame["body"]:
                reply["failures"] = [
                    {
                        "severity": "blocker",
                        "check": "article-grounding",
                        "detail": "Reconciled addition has no current backing.",
                        "section_id": frame["target_section_ids"][0],
                        "anchor": records[1].text,
                    }
                ]
            return reply

    model = GroundedModel()
    monkeypatch.setattr(Pipeline, "_model", lambda *args: model)

    async def reconcile(*args, **kwargs):
        return {"body": original + records[1].text + "\n", "frontmatter": change.draft_meta}

    monkeypatch.setattr(reconciliation, "reconcile_stale", reconcile)
    accepted = []

    class Publisher:
        def publish_changes(self, current, changes, reconciler, revalidate, persist):
            reconciler(changes[0], "# Fresh canonical operator version\n", False)
            accepted.append("candidate accepted")
            pytest.fail("unavailable source candidate reached publication")

    runner = Pipeline(cfg, store, publisher=Publisher())
    with pytest.raises(ReconcileFailed, match="evidence/applicability"):
        await runner._stage_publish(state)
    saved = store.load(state.job_id)
    assert saved.changes[0].draft_body == original and saved.changes[0].status == "validated"
    assert saved.changes[0].reconcile_check_progress["article_review"]["failures"] and not accepted
    assert runner._evidence(saved)[1].support_status == "unknown"
