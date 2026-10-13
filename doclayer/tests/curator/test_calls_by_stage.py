from test_pipeline import LONG_BODY, run_pipeline, submit_payload_job

from doclayer.curator.state import Usage


def test_usage_round_trips_calls_by_stage():
    usage = Usage.from_dict({"model_calls": 3, "calls_by_stage": {"extract": 2, "draft": 1}})
    assert usage.to_dict()["calls_by_stage"] == {"extract": 2, "draft": 1}
    assert Usage.from_dict({}).calls_by_stage == {}


def test_calls_by_stage_sums_to_model_calls(cfg, store, qwen, checkout, forgejo):
    pages = {
        "https://docs.test/model": (
            200,
            f"<html><head><title>M</title></head><body><article><h1>M</h1><p>{LONG_BODY}</p></article></body></html>",
        )
    }
    state = submit_payload_job(store, checkout, urls=["https://docs.test/model"], intent="build test-model knowledge")
    state.limits["max_model_calls"] = 50
    store.save(state)
    state = run_pipeline(cfg, store, qwen, pages, state, forgejo)
    assert sum(state.usage.calls_by_stage.values()) == state.usage.model_calls
    assert "calls by stage" in (store.read_summary(state.job_id) or "")
