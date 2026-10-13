from test_pipeline import LONG_BODY, run_pipeline, submit_payload_job

from doclayer.curator import curate
from doclayer.curator.state import Evidence

SOURCE = "# Card\n\nThe model supports a 128,000-token maximum output.\n"


def record(quote):
    return Evidence(
        "src-0000000001",
        "card",
        "(introduction)",
        "quote",
        "The model supports a 128,000-token maximum output.",
        url="https://docs.test/model",
        quote=quote,
        original_quote=quote,
    )


def test_locate_support_requires_verbatim_quote():
    found = curate.locate_support([record("128,000-token maximum output")], {"src-0000000001": SOURCE})
    missing = curate.locate_support([record("256,000-token maximum output")], {"src-0000000001": SOURCE})
    (ok,), (bad,) = found.values(), missing.values()
    assert ok["status"] == "supported" and ok["support_version"] == curate.SP_QUOTE_SUPPORT_VERSION
    assert bad["status"] == "unknown" and not bad["supported"]


def test_locate_support_ambiguous_quote_is_unknown():
    twice = SOURCE + "\nThe model supports a 128,000-token maximum output.\n"
    (verdict,) = curate.locate_support([record("128,000-token maximum output")], {"src-0000000001": twice}).values()
    assert verdict["status"] == "unknown" and not verdict["supported"] and verdict["excerpt"] == ""
    assert verdict["support_version"] == curate.SP_QUOTE_SUPPORT_VERSION


def many_sources(cfg, store, checkout):
    """More sources than the extract cap allows, so the cap ends extraction.
    The cap is lowered below the default source limit to keep the fixture small."""
    cfg.single_pass.max_extract_calls = 8
    urls = [f"https://docs.test/model-{index}" for index in range(cfg.single_pass.max_extract_calls + 2)]
    pages = {
        url: (
            200,
            f"<html><head><title>M{index}</title></head><body><article><h1>M{index}</h1><p>{LONG_BODY}</p></article></body></html>",
        )
        for index, url in enumerate(urls)
    }
    state = submit_payload_job(store, checkout, urls=urls, intent="build test-model knowledge")
    state.mode = "single-pass"
    return state, pages


def test_single_pass_extract_makes_no_support_calls(cfg, store, qwen, checkout):
    state, pages = many_sources(cfg, store, checkout)
    # The job-wide limit equals the extract cap, so the run stops once
    # extraction has spent it, before placement can make a call.
    state.limits["max_model_calls"] = cfg.single_pass.max_extract_calls
    store.save(state)
    state = run_pipeline(cfg, store, qwen, pages, state)
    assert (state.stage, state.status) == ("extract", "retryable")
    assert state.stages_done == ["intake", "discover"]
    assert state.error == "limit reached: model_calls"
    assert not [c for c in qwen.calls if "Verify selected claims" in c["system"]]
    assert state.usage.calls_by_stage.get("extract", 0) <= cfg.single_pass.max_extract_calls
    assert state.usage.calls_by_stage == {"extract": state.usage.model_calls}
    verdicts = list(state.evidence_verification.values())
    assert verdicts and all(v["support_version"] == curate.SP_QUOTE_SUPPORT_VERSION for v in verdicts)
    assert all(v["status"] == "supported" for v in verdicts)


def test_single_pass_extract_cap_yields_to_placement(cfg, store, qwen, checkout):
    state, pages = many_sources(cfg, store, checkout)
    state.limits["max_model_calls"] = cfg.single_pass.max_model_calls
    store.save(state)
    state = run_pipeline(cfg, store, qwen, pages, state)
    assert state.usage.calls_by_stage["extract"] == cfg.single_pass.max_extract_calls
    assert "extract" in state.stages_done and state.usage.calls_by_stage.get("place", 0) >= 1
    assert any(source.omitted_categories.get("source-budget") for source in state.sources)
    assert not [c for c in qwen.calls if "Verify selected claims" in c["system"]]


def test_evidence_accepts_sp_quote_verdict_only_in_single_pass(cfg, store, qwen, checkout):
    from doclayer.curator.pipeline import Pipeline

    state, pages = many_sources(cfg, store, checkout)
    state.limits["max_model_calls"] = cfg.single_pass.max_extract_calls
    store.save(state)
    state = run_pipeline(cfg, store, qwen, pages, state)
    assert state.evidence_verification
    assert all(v["support_version"] == curate.SP_QUOTE_SUPPORT_VERSION for v in state.evidence_verification.values())
    pipeline = Pipeline(cfg, store)
    single = pipeline._evidence(state)
    assert single and all(r.support_status == "supported" for r in single)
    state.mode = "multi-stage"
    multi = pipeline._evidence(state)
    assert len(multi) == len(single) and all(r.support_status == "unknown" for r in multi)
