"""Route repair identifies actual rejected fields without changing whole evidence."""

import copy
import json
import re

import httpx
import pytest

from doclayer.curator import editing
from doclayer.curator.model import ModelClient
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import Evidence, JobState, PageChange, Usage

BODY = "# Device\nOperator introduction.\n## Advanced Features\nExisting qualified behavior.\n"


def records(count=1):
    return [
        Evidence(
            "manual",
            "Official manual",
            "Device",
            "quote",
            f"On enterprise Linux gateways, operation {index} requires operator-approved debug access.",
            quote=f"On enterprise Linux gateways, operation {index} requires operator-approved debug access.",
            claim_id=f"claim-{index}",
            support_status="supported",
            applicability={"edition": "enterprise", "platform": "Linux gateways"},
        )
        for index in range(count)
    ]


def frame(user):
    return json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])


def ids(user):
    return re.findall(r"^- \[([^ ]+) /", user, re.M)


def valid_reply(user):
    f = frame(user)
    return {
        "routes": [
            {
                "id": identity,
                "section_id": f["root_section_id"],
                "new_heading": "## Operational Monitoring",
                "rationale": "The qualified monitoring contract belongs in a root peer.",
            }
            for identity in ids(user)
        ]
    }


class HttpRoutes:
    def __init__(self, replies):
        self.replies = replies
        self.requests = []

    def transport(self):
        def handler(request):
            messages = json.loads(request.content)["messages"]
            system, user = [m["content"] for m in messages]
            self.requests.append((system, user))
            reply = self.replies.pop(0)
            if callable(reply):
                reply = reply(user)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]}
            )

        return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_child_heading_failure_repairs_exact_field_with_same_whole_claim_context(cfg):
    supplied = records()

    def invalid(user):
        reply = valid_reply(user)
        reply["routes"][0].update(
            section_id=frame(user)["sections"][1]["section_id"], new_heading="### Operational Monitoring"
        )
        return reply

    http = HttpRoutes([invalid, valid_reply])
    usage = Usage()
    model = ModelClient(cfg, usage, transport=http.transport())
    routes = {}
    await editing.route_claims(
        model,
        "Integrate operational scope",
        PageChange("g", "wiki/device.md", "update"),
        supplied,
        editing.page_units(BODY),
        routes,
        char_budget=12000,
        progress=lambda: None,
    )
    assert usage.model_calls == 2 and len(routes) == 1
    first, second = http.requests
    assert first[1].split("\nInvalid section routes:")[0] == second[1].split("\nInvalid section routes:")[0]
    assert "routes[0].new_heading:" in second[1] and "complete Markdown heading beginning '## '" in second[1]
    for system, user in http.requests:
        assert len(system) + len(user) <= 12000
        assert supplied[0].text in user and "enterprise Linux gateways" in user
        assert "at most 600 characters" in system and "at most 200 characters" in system
        assert "root_section_id" in system
    assert routes["claim-0"]["section_id"] == frame(second[1])["root_section_id"]
    assert routes["claim-0"]["new_heading"] == "## Operational Monitoring"


@pytest.mark.parametrize(
    "mutation,field,reason",
    [
        (lambda r, u: r["routes"][0].update(rationale="x" * 601), ".rationale", "600"),
        (lambda r, u: r["routes"][0].update(new_heading="## " + "x" * 198), ".new_heading", "200"),
        (lambda r, u: r["routes"][0].update(section_id=u[1].id), ".new_heading", "root_section_id"),
        (lambda r, u: r["routes"][0].update(new_heading="## Advanced Features"), ".new_heading", "duplicates"),
        (lambda r, u: r["routes"][0].update(section_id="invented"), ".section_id", "supplied structure"),
        (lambda r, u: r["routes"].append(copy.deepcopy(r["routes"][0])), "routes", "count"),
        (lambda r, u: r["routes"][0].update(id=["foreign"]), "routes.id", "exact supplied"),
        (lambda r, u: r["routes"][0].pop("new_heading"), ".new_heading", "field required"),
    ],
)
def test_strict_route_contract_has_bounded_field_diagnostics(mutation, field, reason):
    units = editing.page_units(BODY)
    reply = {
        "routes": [
            {
                "id": "claim-0",
                "section_id": units[0].id,
                "new_heading": "## Monitoring",
                "rationale": "Scoped behavior.",
            }
        ]
    }
    mutation(reply, units)
    diagnostic = editing._route_diagnostic(reply, {"claim-0"}, units, units[0])
    assert any(issue["field"].endswith(field) and reason in issue["reason"] for issue in diagnostic["issues"])
    assert len(diagnostic["issues"]) <= 8
    assert "Existing qualified behavior" not in json.dumps(diagnostic)
    assert "Scoped behavior." not in json.dumps(diagnostic)


@pytest.mark.asyncio
async def test_rejected_later_batch_is_durable_and_restart_keeps_prior_routes(cfg, store):
    supplied = records(8)
    state = store.create(JobState("", "route-checkpoint", "Integrate operational scope"))[0]
    state.stage = "draft"
    state.status = "queued"
    units = editing.page_units(BODY)
    first = {
        "id": "claim-0",
        "section_id": units[0].id,
        "new_heading": "## Operational Monitoring",
        "rationale": "Saved earlier.",
    }
    state.changes = [
        PageChange(
            "g",
            "wiki/device.md",
            "update",
            draft_body=BODY,
            claim_ids=[r.claim_id for r in supplied],
            draft_progress={"claim_routes": {"claim-0": first}},
        )
    ]
    store.save(state)

    def invalid(user):
        reply = valid_reply(user)
        reply["routes"][0]["rationale"] = "unbounded explanation " * 40
        return reply

    http = HttpRoutes([valid_reply, invalid, invalid, invalid])

    class RoutePipeline(Pipeline):
        async def _run_stage(self, current):
            change = current.changes[0]
            routes = change.draft_progress["claim_routes"]
            await editing.route_claims(
                self._model(current),
                current.intent,
                change,
                supplied,
                units,
                routes,
                char_budget=12000,
                progress=lambda: self._persist(current),
            )
            return None

    pipe = RoutePipeline(cfg, store, inference_transport=http.transport())
    await pipe.run(state)
    saved = store.load(state.job_id)
    assert saved.status == "retryable" and saved.usage.model_calls == 4
    assert set(saved.changes[0].draft_progress["claim_routes"]) == {f"claim-{i}" for i in range(7)}
    assert saved.changes[0].draft_progress["claim_routes"]["claim-0"] == first
    diagnostic = saved.inference_diagnostics[-1]
    assert diagnostic["operation"] == "bounded-section-routing"
    assert diagnostic["expected_count"] == 1 and diagnostic["route_count"] == 1
    assert diagnostic["issues"][0]["field"] == "routes[0].rationale"
    assert diagnostic["issues"][0]["length"] > 600
    assert all("claim-0" not in ids(user) for _, user in http.requests)
    replay = HttpRoutes([valid_reply])
    saved.status = "queued"
    store.save(saved)
    await RoutePipeline(cfg, store, inference_transport=replay.transport()).run(saved)
    assert len(replay.requests) == 1 and ids(replay.requests[0][1]) == ["claim-7"]
    assert set(store.load(state.job_id).changes[0].draft_progress["claim_routes"]) == {f"claim-{i}" for i in range(8)}
    assert store.load(state.job_id).usage.model_calls == 5
    assert saved.changes[0].claim_ids == [r.claim_id for r in supplied]


@pytest.mark.asyncio
async def test_prefix_and_parent_repairs_are_reported_together_not_hidden(cfg):
    supplied = records()
    units = editing.page_units(BODY)
    root = units[0]

    def invalid(user):
        reply = valid_reply(user)
        reply["routes"][0].update(section_id=units[1].id, new_heading="### Operational Monitoring")
        return reply

    def corrected(user):
        assert "complete Markdown heading beginning" in user
        assert "new subsections must use root_section_id" in user
        assert f"set section_id={root.id!r}" in user
        assert "heading beginning '## '" in user
        assert ids(user) == ["claim-0"] and supplied[0].text in user
        return valid_reply(user)

    http = HttpRoutes([invalid, corrected])
    usage = Usage()
    routes = {}
    await editing.route_claims(
        ModelClient(cfg, usage, transport=http.transport()),
        "Integrate qualified monitoring",
        PageChange("g", "wiki/device.md", "update"),
        supplied,
        units,
        routes,
        char_budget=6000,
        progress=lambda: None,
    )
    assert usage.model_calls == 2
    assert all(len(system) + len(user) <= 6000 for system, user in http.requests)
    assert routes["claim-0"]["section_id"] == root.id
    assert routes["claim-0"]["new_heading"] == "## Operational Monitoring"


def test_wrong_parent_and_prefix_diagnostics_retain_only_structural_values():
    units = editing.page_units(BODY)
    reply = {
        "routes": [
            {
                "id": "claim-0",
                "section_id": units[1].id,
                "new_heading": "### Private operational title",
                "rationale": "Private source-derived rationale.",
            }
        ]
    }
    diagnostic = editing._route_diagnostic(reply, {"claim-0"}, units, units[0])
    issues = diagnostic["issues"]
    assert len(issues) == 2
    assert issues[0]["expected_prefix"] == "## " and issues[0]["supplied_prefix"] == "### "
    assert issues[1]["expected_section_id"] == units[0].id
    assert issues[1]["supplied_section_id"] == units[1].id
    assert "Private operational title" not in json.dumps(diagnostic)
    assert "Private source-derived rationale" not in json.dumps(diagnostic)


@pytest.mark.asyncio
async def test_maximum_diagnostics_fit_reserved_feedback_without_clipping_claims(cfg):
    supplied = records()
    units = editing.page_units(BODY)
    requests = []
    supplied[0].quote += " The operator-approved access condition applies to every invocation." * 10

    class Capture:
        async def chat_bounded_json(self, system, user, **kwargs):
            requests.append((system, user))
            return valid_reply(user)

    await editing.route_claims(
        Capture(),
        "Scope",
        PageChange("g", "wiki/device.md", "update"),
        supplied,
        units,
        {},
        char_budget=12000,
        progress=lambda: None,
    )
    bound = sum(map(len, requests[0])) + 701  # whole-record packer reserves one separator

    def invalid(user):
        row = valid_reply(user)["routes"][0]
        row.update(section_id=units[1].id, new_heading="### Monitoring", rationale="x" * 601)
        return {"routes": [row, copy.deepcopy(row)]}

    def corrected(user):
        assert len(user) - len(requests[0][1]) <= 700
        assert supplied[0].text in user and requests[0][1] in user
        assert f"set section_id={units[0].id!r}" in user
        assert "heading beginning '## '" in user
        return valid_reply(user)

    http = HttpRoutes([invalid, corrected])
    usage = Usage()
    await editing.route_claims(
        ModelClient(cfg, usage, transport=http.transport()),
        "Scope",
        PageChange("g", "wiki/device.md", "update"),
        supplied,
        units,
        {},
        char_budget=bound,
        progress=lambda: None,
    )
    assert usage.model_calls == 2
    assert sum(map(len, http.requests[1])) == bound - 1
