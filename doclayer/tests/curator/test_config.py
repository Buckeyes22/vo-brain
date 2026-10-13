"""Config loading: file overrides, derived budgets, env escapes."""

from __future__ import annotations

from pathlib import Path

import yaml

from doclayer.curator.config import load_config


def test_explicit_prompt_char_budget_is_not_clobbered(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "limits": {"prompt_char_budget": 9999, "max_sources": 7},
                "inference": {"prompt_token_target": 8192},
            }
        )
    )
    cfg = load_config(path)
    assert cfg.limits.prompt_char_budget == 9999
    assert cfg.limits.max_sources == 7
    assert cfg.inference.prompt_token_target == 8192


def test_derived_budget_follows_inference_target(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"inference": {"prompt_token_target": 4096}}))
    cfg = load_config(path)
    assert cfg.limits.prompt_char_budget == int(4096 * cfg.inference.chars_per_token) == 10240


def test_chars_per_token_is_the_one_conversion(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {"inference": {"prompt_token_target": 4096, "output_token_target": 2000, "chars_per_token": 4.0}}
        )
    )
    cfg = load_config(path)
    assert cfg.limits.prompt_char_budget == 4096 * 4
    assert cfg.inference.output_char_budget == 2000 * 4


def test_default_budgets_derive_from_serving_limits() -> None:
    from doclayer.curator.config import CuratorConfig, output_char_budget

    cfg = CuratorConfig()
    assert cfg.limits.prompt_char_budget == int(cfg.inference.effective_prompt_target * 2.5)
    assert output_char_budget() == cfg.inference.output_char_budget == int(cfg.inference.effective_output_target * 2.5)
    cfg.inference.output_token_target = 10**6  # never past what the server serves
    assert cfg.inference.output_char_budget == int(cfg.inference.serving_max_output_tokens * 2.5)


def test_missing_config_file_yields_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg.limits.max_sources == 50
    assert cfg.inference.model
