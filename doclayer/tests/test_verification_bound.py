"""The frame bound follows the configured model context."""

import asyncio
import json

from doclayer import verification
from doclayer.curator.config import CuratorConfig


class Budget:
    def consume_model(self, **kwargs):
        pass


class FakeClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self._before_call = None
        self.calls = 0
        self.max_calls = 10

        class U:
            model_calls = 0

        self.usage = U()

    async def chat_json(self, system, user, **kwargs):
        self.calls += 1
        assert len(system) + len(user) <= kwargs["prompt_char_budget"]
        return dict(
            answers=False, facets=[dict(facet="f", answered=False, scope_preserved=True, evidence=[])], reason="no"
        )


def _cfg():
    """The fixtures are sized against a 32768-token serving context."""
    cfg = CuratorConfig()
    cfg.inference.serving_context_tokens = 32768
    return cfg


def frame_with_chars(target, cfg):
    """Pages whose complete frame is about `target` characters."""
    base = [dict(path="wiki/big.md", body="", page_metadata={}, authority={})]
    probe = len(verification._SEMANTIC_SYSTEM) + len(
        json.dumps(dict(question="q", facets=["f"], applicability=[], pages=base))
    )
    base[0]["body"] = "x" * (target - probe)
    return base


def run(pages, cfg):
    client = FakeClient(cfg)
    out = asyncio.run(verification._semantic_evidence("q", ["f"], [], pages, model_client=client, budget=Budget()))
    return out, client


def test_measured_over_bound_frames_are_sent_with_default_context():
    cfg = _cfg()
    cfg.limits.prompt_char_budget = cfg.inference.prompt_char_budget  # the derived default
    for target in (34_689, 43_944, 54_432):
        pages = frame_with_chars(target, cfg)
        out, client = run(pages, cfg)
        assert out is not None and client.calls == 1, target


def test_frame_beyond_the_configured_context_is_still_refused_unsent():
    cfg = _cfg()
    pages = frame_with_chars(120_000, cfg)
    out, client = run(pages, cfg)
    assert out is None and client.calls == 0


def test_bound_follows_a_smaller_serving_context():
    cfg = _cfg()
    cfg.inference.serving_context_tokens = 16384
    cfg.limits.prompt_char_budget = cfg.inference.effective_prompt_target * 2
    out, client = run(frame_with_chars(40_000, cfg), cfg)
    assert out is None and client.calls == 0


def test_cap_leaves_room_for_output_and_a_margin():
    cfg = _cfg()
    cap = verification._frame_char_cap(cfg)
    tokens_at_worst_density = cap / 2.5
    room = cfg.inference.serving_context_tokens - cfg.inference.effective_output_target
    assert tokens_at_worst_density < room * 0.95


def test_explicit_char_budget_is_not_overridden():
    cfg = _cfg()
    cfg.limits.prompt_char_budget = 20
    assert verification._frame_char_cap(cfg) == 20


# -- an over-bound evidence set is judged in batches that each fit ------------------------- #


class BatchClient(FakeClient):
    """Answers a facet when its sentinel page is in the frame; records every frame size."""

    def __init__(self, cfg, facets):
        super().__init__(cfg)
        self.sizes = []
        self.facets = facets
        self.max_calls = 50

    async def chat_json(self, system, user, **kwargs):
        self.calls += 1
        self.sizes.append(len(system) + len(user))
        assert len(system) + len(user) <= kwargs["prompt_char_budget"]
        frame = json.loads(user)
        rows = []
        for facet in frame["facets"]:
            page = next((p for p in frame["pages"] if f"ANSWER-{facet}-" in p["body"]), None)
            rows.append(
                dict(
                    facet=facet,
                    answered=page is not None,
                    scope_preserved=True,
                    evidence=[dict(path=page["path"], quote=f"ANSWER-{facet}-is-here-today")] if page else [],
                )
            )
        return dict(answers=all(r["answered"] for r in rows), facets=rows, reason="batch")


def synthetic_pages(total=120_000, per_page=20_000, answers=((0, "a"), (3, "b"), (5, "c"))):
    pages = []
    for i in range(total // per_page):
        body = "filler " * (per_page // 7)
        for position, facet in answers:
            if i == position:
                body += f" ANSWER-{facet}-is-here-today "
        pages.append(dict(path=f"wiki/p{i}.md", body=body, page_metadata={}, authority={}))
    return pages


def run_batches(pages, facets):
    cfg = _cfg()
    client = BatchClient(cfg, facets)
    out = asyncio.run(verification._semantic_evidence("q", facets, [], pages, model_client=client, budget=Budget()))
    return out, client, verification._frame_char_cap(cfg)


def test_120k_evidence_is_judged_in_batches_each_under_the_bound():
    pages = synthetic_pages()
    assert sum(len(p["body"]) for p in pages) >= 120_000
    out, client, cap = run_batches(pages, ["a", "b", "c"])
    assert client.calls >= 2 and all(size <= cap for size in client.sizes)
    assert out is not None and out["answers"] is True
    assert [r["facet"] for r in out["facets"]] == ["a", "b", "c"]
    assert all(r["answered"] and r["evidence"] for r in out["facets"])


def test_one_unanswered_facet_keeps_the_gap_open_with_a_partial_aggregate():
    out, client, cap = run_batches(synthetic_pages(answers=((0, "a"), (3, "b"))), ["a", "b", "c"])
    assert out is not None and out["answers"] is False
    assert {r["facet"]: r["answered"] for r in out["facets"]} == {"a": True, "b": True, "c": False}


def test_a_page_that_cannot_fit_alone_is_still_refused_unsent():
    out, client, cap = run_batches(synthetic_pages(total=200_000, per_page=200_000), ["a"])
    assert out is None and client.calls == 0


def test_aggregate_verdict_survives_strict_revalidation_as_stored():
    pages = synthetic_pages()
    out, client, cap = run_batches(pages, ["a", "b", "c"])
    again = verification._validate_semantic(out, ["a", "b", "c"], [], pages, question="q")
    assert again == out
