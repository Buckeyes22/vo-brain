import pytest
from test_pipeline import git_show, run_pipeline, submit_payload_job
from test_single_pass_pipeline import draft_reply, extract_reply

from doclayer.curator.ops import OpsError, handle

LABEL = "live check ws1 dpkg-query #1 @2026-10-07T17:30Z"
TEXT = "host: ws1\ncommand: dpkg-query -W solaar\nexit: 0\nstdout:\nsolaar\t1.1.19-1\nstderr:\n(no output)\n"
PAGE = "wiki/solaar-ws1.md"


def private_payload(**over):
    payload = {
        "intent": "Document from live read-only evidence: How is Solaar installed on ws1?",
        "texts": [{"label": LABEL, "text": TEXT}],
        "private": True,
        "mode": "single-pass",
        "submitted_by": "gap-worker-private",
        "source_policy": {"exclude": [{"domain": "*"}]},
    }
    payload.update(over)
    return payload


@pytest.mark.parametrize(
    "over",
    [
        {"submitted_by": "agent"},
        {"submitted_by": "doclayer-gap-dispatcher"},
        {"urls": ["https://example.org/x"]},
        {"mode": "multi-stage"},
        {"texts": []},
        {"files": [{"name": "x", "size": 1}]},
        {"directory": "/tmp"},
        {"manifest": {"sources": []}},
        {"manifest_file": "/tmp/m"},
    ],
)
def test_private_submit_refused_outside_its_contract(cfg, store, over):
    with pytest.raises(OpsError):
        handle("submit", private_payload(**over), cfg, store)


def test_private_flag_persists_and_shows_in_status(cfg, store):
    reply = handle("submit", private_payload(), cfg, store)
    state = store.load(reply["job_id"])
    assert state.private is True and state.submitted_by == "gap-worker-private" and state.mode == "single-pass"
    assert handle("status", {"job_id": reply["job_id"]}, cfg, store)["job"]["private"] is True


def _private_job(store, checkout):
    state = submit_payload_job(
        store,
        checkout,
        texts=[(LABEL, TEXT)],
        intent="Document from live read-only evidence: How is Solaar installed on ws1?",
    )
    state.mode, state.private, state.submitted_by = "single-pass", True, "gap-worker-private"
    store.save(state)
    return state


def _script(qwen, citation):
    qwen.overrides["extract"] = [extract_reply(("Solaar 1.1.19-1 is installed on ws1", "solaar\t1.1.19-1"))] * 6
    qwen.overrides["plan_pages"] = [
        {
            "pages": [
                {
                    "path": PAGE,
                    "title": "Solaar on ws1",
                    "subject_type": "product",
                    "rationale": "private live evidence",
                }
            ]
        }
    ]
    qwen.overrides["sp_draft"] = [
        lambda user: draft_reply(user, sentence=lambda claim, url: f"{claim} [source: {citation}]")
    ] * 4


def test_private_job_publishes_agent_page_with_live_check_lineage(cfg, store, checkout, origin, qwen, forgejo):
    state = _private_job(store, checkout)
    _script(qwen, LABEL)
    run_pipeline(cfg, store, qwen, {}, state, forgejo=forgejo)
    page = git_show(origin, f"main:{PAGE}")
    assert "privacy: internal" in page and "source_origin: agent" in page
    assert "live-check:ws1@2026-10-07" in page and f"curator:{state.job_id}" in page
    assert f"[source: {LABEL}]" in page


def test_private_job_never_publishes_a_foreign_url_citation(cfg, store, checkout, origin, qwen, forgejo):
    state = _private_job(store, checkout)
    _script(qwen, "https://example.org/x")
    run_pipeline(cfg, store, qwen, {}, state, forgejo=forgejo)
    landed = git_show(origin, f"main:{PAGE}")
    assert landed is None or "https://example.org/x" not in landed


def test_continuation_of_a_private_job_stays_private_and_refuses_urls(cfg, store):
    reply = handle("submit", private_payload(), cfg, store)
    state = store.load(reply["job_id"])
    state.status = "partial"
    store.save(state)
    with pytest.raises(OpsError):
        handle("continue", {"job_id": state.job_id, "urls": ["https://example.org/x"]}, cfg, store)
    with pytest.raises(OpsError):
        handle("continue", {"job_id": state.job_id, "source_policy": {"exclude": []}}, cfg, store)
    with pytest.raises(OpsError):
        handle("continue", {"job_id": state.job_id, "mode": "multi-stage"}, cfg, store)
    child = store.load(handle("continue", {"job_id": state.job_id}, cfg, store)["job_id"])
    assert child.private is True and child.submitted_by == "gap-worker-private" and child.mode == "single-pass"


def test_private_job_skips_url_discovery(cfg, store, checkout, origin, qwen, forgejo, monkeypatch):
    import doclayer.curator.pipeline as pipeline_mod

    expanded = []
    monkeypatch.setattr(pipeline_mod.intake, "extract_links", lambda text, base: expanded.append(base) or [])
    state = _private_job(store, checkout)
    state.sources[0].url = "https://example.org/page-with-links"
    store.save(state)
    _script(qwen, LABEL)
    run_pipeline(cfg, store, qwen, {}, state, forgejo=forgejo)
    assert expanded == []
