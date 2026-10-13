"""Unproved canonical skip proposals retain work and use ordinary placement."""

import copy
import json

import pytest
from test_placement import Planner, records

from doclayer.curator import curate, placement
from doclayer.curator.model import ModelError


class MissingAnchor(Planner):
    def __init__(self, anchor="", extra=None):
        super().__init__(covered=True)
        self.anchor = anchor
        self.extra = extra or {}
        self.coverage_requests = []

    async def chat_bounded_json(self, system, user, *, char_budget):
        if user.startswith("CANONICAL CLAIM COVERAGE"):
            assert len(system) + len(user) <= char_budget
            self.coverage_requests.append((system, user))
            return {
                "covered": True,
                "consistent": True,
                "anchor": self.anchor,
                "qualifiers": [],
                "reason": "proposed coverage without canonical support",
                **self.extra,
            }
        return await super().chat_bounded_json(system, user, char_budget=char_budget)


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor", ["", "invented canonical supporting text"])
async def test_exhausted_anchor_only_error_plans_claim_without_coverage(anchor):
    evidence = records()
    identity = curate.evidence_id(evidence[0])
    client = MissingAnchor(anchor)
    saved = []
    page = ("wiki/device.md", {}, "Device introduction without the incoming fact.")
    result = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        [page],
        [],
        char_budget=7000,
        progress=lambda ledger: saved.append(copy.deepcopy(ledger)),
    )
    assert len(client.coverage_requests) == 3
    assert "MANDATORY JSON REPAIR" not in client.coverage_requests[0][0]
    assert all("MANDATORY JSON REPAIR" in s for s, _ in client.coverage_requests[1:])
    assert len({u for _, u in client.coverage_requests}) == 1
    assert result["covered"] == [] and not result["already_covered"]
    assert result["pages"][0]["claim_ids"] == [identity]
    row = result["ledger"]["claims"][identity]
    assert row["status"] == "planned" and "coverage" not in row
    unproven = list(row["unproven_coverage_attempts"].values())
    assert len(unproven) == 1
    assert unproven[0]["status"] == "unproven"
    assert unproven[0]["diagnostic"]["attempts"] == 3
    pending = next(s for s in saved if s["claims"][identity].get("unproven_coverage_attempts"))
    assert pending["claims"][identity]["status"] == "pending"
    retry = next(u for u in client.calls if "Claims JSON:" in u and evidence[0].text in u)
    assert json.loads(retry.split("Claims JSON:\n")[1])[0]["claim"] == evidence[0].text

    calls = (len(client.calls), len(client.coverage_requests))
    replay = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        [page],
        [],
        char_budget=7000,
        ledger=result["ledger"],
    )
    assert replay["pages"] == result["pages"] and replay["covered"] == []
    assert (len(client.calls), len(client.coverage_requests)) == calls
    resumed = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        [page],
        [],
        char_budget=7000,
        ledger=pending,
    )
    assert resumed["pages"] and resumed["covered"] == []
    assert len(client.coverage_requests) == 3
    assert resumed["ledger"]["claims"][identity]["unproven_coverage_attempts"] == row["unproven_coverage_attempts"]

    # The retained failed attempt is exact-page bound, never an eternal skip.
    fresh = Planner(covered=True)
    changed = await placement.plan_batch(
        fresh,
        "Device",
        [],
        evidence,
        [("wiki/device.md", {}, evidence[0].text)],
        [],
        char_budget=7000,
        ledger=pending,
    )
    assert changed["covered"] == [identity] and changed["already_covered"]
    assert len(fresh.calls) == 1 and fresh.calls[0].startswith("CANONICAL CLAIM COVERAGE")
    updated = changed["ledger"]["claims"][identity]
    assert updated["coverage"]["anchor"] == evidence[0].text
    assert updated["coverage"]["binding"] != unproven[0]["binding"]
    assert updated["unproven_coverage_attempts"] == row["unproven_coverage_attempts"]


@pytest.mark.asyncio
async def test_exhausted_nonliteral_anchor_and_qualifier_plans_claim_without_coverage():
    # Live job-0d55cdaaf2: an empty anchor plus an invented qualifier is still
    # only non-literal canonical text, so it proves no coverage either.
    evidence = records()
    identity = curate.evidence_id(evidence[0])
    client = MissingAnchor(extra={"qualifiers": ["invented qualification"]})
    result = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        [("wiki/device.md", {}, "Device introduction")],
        [],
        char_budget=7000,
    )
    assert len(client.coverage_requests) == 3
    assert '"invented qualification"' in client.coverage_requests[1][0]
    row = result["ledger"]["claims"][identity]
    assert row["status"] == "planned" and "coverage" not in row
    (diagnostic,) = [attempt["diagnostic"] for attempt in row["unproven_coverage_attempts"].values()]
    assert diagnostic["issue_count"] == diagnostic["literal_issue_count"] == 2
    assert "invented qualification" not in json.dumps(diagnostic)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"covered": "true"},
        {"consistent": "true"},
        {"anchor": None},
        {"reason": None},
    ],
)
async def test_other_invalid_coverage_fields_still_stop(extra):
    evidence = records()
    client = MissingAnchor(extra=extra)
    saved = []
    with pytest.raises(ModelError, match="invalid bounded placement response"):
        await placement.plan_batch(
            client,
            "Device",
            [],
            evidence,
            [("wiki/device.md", {}, "Device introduction")],
            [],
            char_budget=7000,
            progress=lambda ledger: saved.append(copy.deepcopy(ledger)),
        )
    assert len(client.coverage_requests) == 3
    assert not any(
        r.get("unproven_coverage_attempts") or r.get("coverage") for state in saved for r in state["claims"].values()
    )


@pytest.mark.asyncio
async def test_real_charged_http_recovery_and_durable_restart_keep_original_allowance(tmp_path):
    import httpx

    from doclayer.curator.config import CuratorConfig
    from doclayer.curator.model import ModelClient
    from doclayer.curator.state import Usage

    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        system, user = [m["content"] for m in payload["messages"]]
        if user.startswith("CANONICAL CLAIM COVERAGE"):
            result = {
                "covered": True,
                "consistent": True,
                "anchor": "",
                "qualifiers": [],
                "reason": "coverage without supporting anchor",
            }
        else:
            selected = json.loads(user.split("Claims JSON:\n")[1])
            if "FAILED independent" not in system:
                result = {"pages": [], "covered": [{"claim_id": s["id"], "path": "wiki/device.md"} for s in selected]}
            else:
                result = {
                    "pages": [
                        {
                            "action": "update",
                            "path": "wiki/device.md",
                            "title": "Device",
                            "subject_type": "product",
                            "rationale": "retain supported work",
                            "source_ids": ["manual"],
                            "claim_ids": [s["id"] for s in selected],
                            "related": [],
                        }
                    ],
                    "covered": [],
                }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )

    cfg = CuratorConfig()
    cfg.inference.url = "http://model.fixture/v1"
    cfg.inference.retries = 0
    usage = Usage(model_calls=34)
    client = ModelClient(cfg, usage, limits={"max_model_calls": 50}, transport=httpx.MockTransport(handler))
    checkpoint = tmp_path / "placement.json"
    evidence = records()
    pages = [("wiki/device.md", {}, "A different current fact.")]
    result = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        pages,
        [],
        char_budget=7000,
        progress=lambda ledger: checkpoint.write_text(json.dumps(ledger)),
    )
    assert len(requests) == 5 and usage.model_calls == 39 and client.max_calls == 50
    assert not result["covered"] and result["pages"][0]["claim_ids"] == [curate.evidence_id(evidence[0])]
    saved = json.loads(checkpoint.read_text())
    restarted = ModelClient(cfg, usage, limits={"max_model_calls": 50}, transport=httpx.MockTransport(handler))
    replay = await placement.plan_batch(
        restarted,
        "Device",
        [],
        evidence,
        pages,
        [],
        char_budget=7000,
        ledger=saved,
    )
    assert replay["pages"] == result["pages"] and not replay["covered"]
    assert len(requests) == 5 and usage.model_calls == 39 and restarted.max_calls == 50
