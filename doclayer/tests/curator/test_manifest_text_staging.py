"""Inline manifest evidence survives actual submit/move/intake and replay."""

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml

from doclayer.curator.intake import content_hash
from doclayer.curator.jobstore import JobStore
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.transport import LocalTransport


@pytest.mark.parametrize("input_key", ["sources", "inputs"])
def test_manifest_text_is_durable_exact_local_evidence(cfg, tmp_path, input_key):
    text = "# Observed configuration\n\nCache limit is 5 GiB — only on host Harbor.\n"
    manifest = tmp_path / "private.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "topic": "private installation",
                input_key: [
                    {
                        "label": "../../outside/host facts",
                        "text": text,
                        "role": "supplementary",
                        "applicability": {"host": "Harbor"},
                    }
                ],
                "source_policy": {"exclude": [{"domain": "*", "reason": "private facts"}]},
            }
        )
    )
    transport = LocalTransport(cfg)
    payload = {
        "intent": "private installation",
        "manifest_file": manifest.name,
        "manifest_digest": content_hash(manifest.read_text())[:16],
        "limits": {"max_model_calls": 50},
    }
    reply = transport.call("submit", payload, files=[manifest])
    assert reply["ok"] and reply["created"]
    store = JobStore(cfg.jobs_dir())
    state = store.load(reply["job_id"])
    source = state.sources[0]
    path = Path(source.local_path)
    assert path.parent == store.staging_dir(state.job_id)
    assert path.read_bytes() == text.encode("utf-8")
    assert source.kind == "text" and source.role == "supplementary"
    assert source.label == "../../outside/host facts"
    assert source.scope == {"host": "Harbor"}
    assert source.content_hash == content_hash(text)
    assert not (tmp_path / "outside").exists()

    requested = []

    def fail_external(request):
        requested.append(str(request.url))
        raise AssertionError("local evidence must not fetch public sources")

    pipeline = Pipeline(cfg, store, fetch_transport=httpx.MockTransport(fail_external))
    asyncio.run(pipeline._stage_intake(state))
    after = store.load(state.job_id)
    assert after.sources[0].status == "fetched"
    assert after.sources[0].content_hash == content_hash(text)
    assert after.usage.sources_fetched == 1
    assert after.usage.model_calls == 0 and requested == []
    assert after.limits["max_model_calls"] == 50

    # Replaying a lost reply must not recreate inputs or overwrite progressed
    # native state; the same job carries the original source bytes and usage.
    state_bytes = store.state_path(state.job_id).read_bytes()
    replay = transport.call("submit", payload, files=[manifest])
    assert replay["job_id"] == state.job_id and not replay["created"]
    assert store.state_path(state.job_id).read_bytes() == state_bytes
    assert path.read_bytes() == text.encode("utf-8")
