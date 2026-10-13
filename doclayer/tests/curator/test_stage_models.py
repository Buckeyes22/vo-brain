"""Per-stage model selection: stage overrides on the shared inference block."""

import json

import httpx
import pytest

from doclayer import discovery, private_writer, verification
from doclayer.curator import publish
from doclayer.curator.config import STAGES, CuratorConfig, load_config, stage_config
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import JobState


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_no_stages_means_every_stage_uses_the_global_inference(tmp_path):
    cfg = load_config(write(tmp_path, "inference:\n  model: qwen3.8-max\n"))
    assert STAGES == ("discovery", "planning", "curation", "verification", "extraction")
    for stage in STAGES:
        assert stage_config(cfg, stage).inference.model == "qwen3.8-max"


def test_stage_override_applies_only_to_that_stage(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            (
                "inference:\n  model: qwen3.8-max\n"
                "stages:\n  discovery:\n    model: qwen3.8-flash\n  verification:\n    model: deepseek-v4.1-flash\n"
            ),
        )
    )
    assert stage_config(cfg, "discovery").inference.model == "qwen3.8-flash"
    assert stage_config(cfg, "verification").inference.model == "deepseek-v4.1-flash"
    assert stage_config(cfg, "curation").inference.model == "qwen3.8-max"
    assert cfg.inference.model == "qwen3.8-max"  # global untouched


def test_stage_config_never_mutates_the_global(tmp_path):
    cfg = load_config(write(tmp_path, "stages:\n  verification:\n    model: deepseek-v4.1-flash\n    timeout_s: 30\n"))
    before = (cfg.inference.model, cfg.inference.timeout_s)
    stage_config(cfg, "verification")
    assert (cfg.inference.model, cfg.inference.timeout_s) == before


def test_unknown_stage_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown model stage 'drafting'"):
        load_config(write(tmp_path, "stages:\n  drafting:\n    model: x\n"))
    with pytest.raises(ValueError, match="unknown model stage"):
        stage_config(CuratorConfig(), "drafting")


def test_unknown_override_key_is_rejected(tmp_path):
    with pytest.raises(ValueError, match=r"stages.verification: unknown inference keys \['modle'\]"):
        load_config(write(tmp_path, "stages:\n  verification:\n    modle: deepseek-v4.1-flash\n"))


def test_stage_api_key_env_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("CURATOR_INFERENCE_API_KEY", "global-key")
    monkeypatch.setenv("VERIFIER_API_KEY", "verifier-key")
    cfg = load_config(write(tmp_path, "stages:\n  verification:\n    api_key_env: VERIFIER_API_KEY\n"))
    assert stage_config(cfg, "verification").inference_api_key() == "verifier-key"
    assert stage_config(cfg, "curation").inference_api_key() == "global-key"


def test_stage_prompt_override_rederives_char_budget_unless_explicit(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            (
                "inference:\n  serving_context_tokens: 262144\n  prompt_token_target: 16384\n"
                "stages:\n  curation:\n    prompt_token_target: 65536\n"
            ),
        )
    )
    staged = stage_config(cfg, "curation")
    expected = int(staged.inference.effective_prompt_target * staged.inference.chars_per_token)
    assert staged.limits.prompt_char_budget == expected
    assert cfg.limits.prompt_char_budget != expected  # global keeps its own budget
    explicit = load_config(
        write(
            tmp_path, ("limits:\n  prompt_char_budget: 50000\nstages:\n  curation:\n    prompt_token_target: 65536\n")
        )
    )
    assert stage_config(explicit, "curation").limits.prompt_char_budget == 50000


STAGED = (
    "inference:\n  model: qwen3.8-max\n"
    "stages:\n  discovery:\n    model: qwen3.8-flash\n"
    "  planning:\n    model: plan-model\n  verification:\n    model: deepseek-v4.1-flash\n"
    "  curation:\n    model: curate-model\n"
)


def test_private_writer_plans_with_the_planning_stage(tmp_path):
    cfg = load_config(write(tmp_path, STAGED))
    assert private_writer._model_client(cfg).cfg.inference.model == "plan-model"


def test_verifier_defaults_to_the_verification_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_CURATOR_CONFIG", str(write(tmp_path, STAGED)))
    assert verification._default_client().cfg.inference.model == "deepseek-v4.1-flash"


def test_discovery_judges_with_the_discovery_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_CURATOR_CONFIG", str(write(tmp_path, STAGED)))
    assert discovery._discovery_config().inference.model == "qwen3.8-flash"


def test_discovery_request_carries_the_discovery_model(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_CURATOR_CONFIG", str(write(tmp_path, STAGED)))
    monkeypatch.setenv("CURATOR_INFERENCE_API_KEY", "k")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": '{"relevant": true}'}}], "usage": {}},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        discovery.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    discovery._qwen_judgment("https://x.test/doc", "Doc", "need", "body", state_dir=tmp_path)
    assert seen and seen[0]["model"] == "qwen3.8-flash"


@pytest.mark.asyncio
async def test_curation_calls_use_the_curation_stage(cfg, store, tmp_path):
    cfg.inference.model = "qwen3.8-max"
    cfg.stages = {"curation": {"model": "curate-model"}}
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}], "usage": {}}
        )

    pipeline = Pipeline(cfg, store, inference_transport=httpx.MockTransport(handler))
    state = store.create(JobState("", "stage-curation", "Device"))[0]
    client = pipeline._model(state)
    assert client.cfg.inference.model == "curate-model"
    await client.chat("system", "user", json_object=True)
    assert seen == ["curate-model"]


def test_publication_text_names_the_curation_model(cfg):
    cfg.inference.model = "qwen3.8-max"
    cfg.stages = {"curation": {"model": "curate-model"}}
    state = JobState("job-x", "k", "intent")
    assert "model curate-model" in publish.commit_message(state, stage_config(cfg, "curation").inference.model)


def test_changing_the_verifier_model_changes_the_verification_binding(tmp_path, monkeypatch):
    a = load_config(write(tmp_path, "stages:\n  verification:\n    model: deepseek-v4.1-flash\n"))
    b = load_config(write(tmp_path, "stages:\n  verification:\n    model: qwen3.8-max\n"))
    assert verification._policy_binding(stage_config(a, "verification")) != verification._policy_binding(
        stage_config(b, "verification")
    )


def test_pipeline_sizes_prompts_from_the_curation_stage(cfg, store):
    cfg.inference.serving_context_tokens = 262144
    cfg.inference.prompt_token_target = 16384
    cfg.limits.prompt_char_budget = cfg.inference.prompt_char_budget
    cfg.stages = {"curation": {"prompt_token_target": 65536}}
    pipeline = Pipeline(cfg, store)
    assert pipeline.cfg.inference.prompt_token_target == 65536
    assert pipeline.cfg.limits.prompt_char_budget == pipeline.cfg.inference.prompt_char_budget
    assert cfg.inference.prompt_token_target == 16384  # global untouched
