"""Curator configuration: paths, limits, inference, publication.

Loaded from a YAML file (default ``~/.config/brain-curator/config.yaml``,
overridable with ``BRAIN_CURATOR_CONFIG``) with environment-variable escapes
for the values that differ per host. Defaults suit a single small
deployment; docs/configuration.md lists every key and its default. Secrets
never live here: the inference API key is read from the credentials env file
(``credentials.env``, chmod 600).
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

MIB = 1024 * 1024
GIB = 1024 * MIB

#: Pipeline modes a job may run in.
MODES = ("single-pass", "multi-stage")

#: The model-calling stages that may each name their own inference profile.
STAGES: tuple[str, ...] = ("discovery", "planning", "curation", "verification", "extraction")

#: A stage with no overrides of its own takes this stage's overrides, and a
#: stage with overrides layers them on top of this stage's. Extraction is part
#: of curation unless it names its own endpoint.
STAGE_FALLBACK: dict[str, str] = {"extraction": "curation"}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class InferenceConfig:
    """The one OpenAI-compatible model endpoint the curator uses. The default is
    a local OpenAI-compatible server; the deployed config may point it at a
    hosted model. There is no fallback: if the endpoint fails, the job
    preserves progress and reports it."""

    url: str = "http://localhost:8000/v1"
    model: str = "Qwen/Qwen3-32B"
    api_key_env: str = "CURATOR_INFERENCE_API_KEY"
    #: Serving limits; requests are capped to them, never the server
    #: reconfigured to match the client. Set these to what the serving
    #: endpoint actually reports.
    serving_context_tokens: int = 262144
    serving_max_output_tokens: int = 8192
    #: Per-request targets (prompt includes instructions + context + evidence).
    prompt_token_target: int = 16384
    output_token_target: int = 4096
    timeout_s: float = 300.0
    retries: int = 2
    #: Parallel inference requests within one job (a per-job semaphore). The
    #: worker-wide ceiling is limits.parallel_jobs x concurrency.
    concurrency: int = 1
    enable_thinking: bool = False  # structured editorial tasks need the final JSON budget
    #: Request key that bounds thinking tokens: vLLM reads `thinking_token_budget`;
    #: Alibaba's compatible-mode API reads `thinking_budget` and ignores the vLLM key.
    thinking_budget_field: str = "thinking_token_budget"
    #: Thinking tokens allowed on reasoning-enabled tasks, further capped at half
    #: the output target. 2048 suits an 8k output; raise it for hosted models.
    thinking_token_budget: int = 2048
    #: The one token->character conversion for every budget derived from a
    #: token target. Measured on qwen3.8-flash (2026-10-10): 3.9-4.1 chars per
    #: token on documentation copies, 2.64 on dense wiki tables; 2.5 stays
    #: below both so a derived character budget never overruns its tokens.
    chars_per_token: float = 2.5
    #: vLLM priority-scheduling value sent as the top-level request key
    #: ``priority`` (lower runs first; the server default is 0). None omits it.
    request_priority: int | None = None
    #: A Prometheus metrics URL of the serving endpoint. When set, each request
    #: waits until no other user is running or queued there. None disables the gate.
    idle_metrics_url: str | None = None
    #: Seconds between idle-gate checks while the endpoint is busy.
    idle_poll_s: float = 15.0

    @property
    def effective_prompt_target(self) -> int:
        """Prompt target capped to the serving context, keeping a margin for
        accounting differences between client and server."""
        cap = max(1024, int(self.serving_context_tokens * 0.8))
        return max(1024, min(self.prompt_token_target, cap))

    @property
    def effective_output_target(self) -> int:
        return max(256, min(self.output_token_target, self.serving_max_output_tokens))

    @property
    def prompt_char_budget(self) -> int:
        """Characters one prompt may hold: the effective prompt target converted
        with ``chars_per_token``."""
        return int(self.effective_prompt_target * self.chars_per_token)

    @property
    def output_char_budget(self) -> int:
        """Characters one reply may hold: the effective output target converted
        with ``chars_per_token``."""
        return int(self.effective_output_target * self.chars_per_token)


def output_char_budget(inference: Any = None) -> int:
    """The reply character budget of ``inference`` (any object exposing
    ``effective_output_target``). Missing fields fall back to the configured
    defaults, never to a separate literal."""
    defaults = InferenceConfig()
    target = getattr(inference, "effective_output_target", defaults.effective_output_target)
    ratio = getattr(inference, "chars_per_token", defaults.chars_per_token)
    return int(target * ratio)


@dataclass
class LimitsConfig:
    """Per-job workload limits (design: "Processing limits" table)."""

    max_sources: int = 50
    max_source_bytes: int = 64 * MIB
    max_job_bytes: int = 512 * MIB
    max_active_seconds: float = 2 * 3600.0
    max_model_calls: int = 200
    fetch_concurrency: int = 4
    connect_timeout_s: float = 10.0
    fetch_timeout_s: float = 120.0
    transient_retries: int = 2
    max_repair_passes: int = 2
    #: Conservative char budget per model prompt. Text is chunked to this many
    #: characters. 0 means derive it from the inference prompt target with
    #: ``inference.chars_per_token`` (CuratorConfig.__post_init__, load_config).
    #: It is an estimate, never reported as an exact token count.
    prompt_char_budget: int = 0
    #: Jobs the worker runs at once (one thread each). Publication is still
    #: one at a time (the store's publish lock). 1 is strictly sequential.
    parallel_jobs: int = 1


@dataclass
class PathsConfig:
    """Deployment paths on the worker host."""

    brain_checkout: str = "~/.local/share/brain-curator/checkout"
    jobs: str = "~/.local/share/brain-curator/jobs"
    worktrees: str = "~/.local/share/brain-curator/worktrees"
    cache: str = "~/.cache/brain-curator"

    def expanded(self) -> PathsConfig:
        return PathsConfig(
            brain_checkout=str(Path(self.brain_checkout).expanduser()),
            jobs=str(Path(self.jobs).expanduser()),
            worktrees=str(Path(self.worktrees).expanduser()),
            cache=str(Path(self.cache).expanduser()),
        )


@dataclass
class PublishConfig:
    """Where curated changes land and by which route."""

    remote: str = "origin"
    base_branch: str = "main"
    branch_prefix: str = "curator/"
    #: Canonical destinations a curator job may write. Anything else is out of
    #: scope; librarian-owned files are denied by name wherever they appear.
    allowed_prefixes: list[str] = field(default_factory=lambda: ["wiki/", "infra/"])
    denied_names: list[str] = field(default_factory=lambda: ["index.md", "log.md"])
    #: Empty until configured; publishing requires it.
    forgejo_url: str = ""
    forgejo_token_file: str = "~/.config/doclayer/forgejo-token"
    #: Opt-in: lets a single-maintainer repo land curator PRs with the admin
    #: merge override, since Forgejo forbids self-approval.
    force_merge: bool = False
    author_name: str = "brain-curator"
    author_email: str = "brain-curator@localhost.invalid"


@dataclass
class SshConfig:
    """How the workstation CLI/MCP reaches the worker's ops entry point."""

    host: str = ""
    #: Fixed remote entry point; JSON goes in on stdin, JSON comes back on
    #: stdout. Never interpolate submitted text into this string.
    remote_command: str = "brain-curator ops"
    staging_subdir: str = "staging"
    connect_timeout_s: float = 15.0
    command_timeout_s: float = 120.0


@dataclass
class CacheConfig:
    max_bytes: int = 2 * GIB
    retention_days: int = 7  # measured from the job's latest processing activity


@dataclass
class RetentionConfig:
    """Job artifact retention; corrections always outlive job cleanup."""

    finished_job_days: int = 90


@dataclass
class PipelineConfig:
    """Which pipeline new submissions run in (corrections and legacy jobs stay
    multi-stage)."""

    default_mode: str = "single-pass"


@dataclass
class SinglePassConfig:
    """Runaway guards for single-pass jobs (draft, one validation, publish).
    None of them limits how many supported claims a page receives."""

    max_model_calls: int = 200
    max_active_seconds: float = 4 * 3600.0
    max_sources: int = 50  # submitted sources only
    max_extract_calls: int = 60  # per job: extraction chunks
    max_pages: int = 20
    max_calls_per_page: int = 10  # draft + JSON re-ask + grounding chunks
    max_grounding_chunks: int = 10


@dataclass
class PrivateWriterConfig:
    """Automatic private pages: allowlisted read-only evidence."""

    hosts: list[str] = field(default_factory=list)
    #: Role notes per host alias, shown to the model so questions can name roles.
    host_notes: dict[str, str] = field(default_factory=dict)
    max_commands: int = 30
    command_timeout_s: int = 60
    output_cap_bytes: int = 131072
    max_job_calls: int = 200


@dataclass
class GapWorkerConfig:
    """Gap worker cycle pacing: no per-cycle need count, and by default no time
    budget either; a cycle is bounded only if cycle_budget_s is configured."""

    #: None (default) = no soft budget (run until no eligible need remains); set
    #: seconds to bound a cycle. The unit's TimeoutStartSec=8h only kills a hung run.
    cycle_budget_s: float | None = None
    #: How many gaps the dispatcher keeps submitted to the curator at once.
    max_active_jobs: int = 1


@dataclass
class CuratorConfig:
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    single_pass: SinglePassConfig = field(default_factory=SinglePassConfig)
    private_writer: PrivateWriterConfig = field(default_factory=PrivateWriterConfig)
    gap_worker: GapWorkerConfig = field(default_factory=GapWorkerConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    ssh: SshConfig = field(default_factory=SshConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    #: Per-stage InferenceConfig overrides, e.g. {"verification": {"model": "deepseek-v4.1-flash"}}.
    #: An absent stage uses `inference` unchanged.
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    credentials_file: str = "~/.config/brain-curator/credentials.env"
    #: Optional accelerator; its outage never blocks curation.
    vo_rag_url: str | None = None
    user_agent: str = "brain-curator/0.1 (brain wiki curator; single-user)"

    def __post_init__(self) -> None:
        if not self.limits.prompt_char_budget:
            self.limits.prompt_char_budget = self.inference.prompt_char_budget

    def credentials(self) -> dict[str, str]:
        """KEY=VALUE pairs from the credentials env file. Never logged."""
        path = Path(self.credentials_file).expanduser()
        out: dict[str, str] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip().strip("'\"")
        except OSError:
            return out
        return out

    def inference_api_key(self) -> str | None:
        value = os.environ.get(self.inference.api_key_env, "")
        if not value:
            value = self.credentials().get(self.inference.api_key_env, "")
        return value or None

    def cache_dir(self) -> Path:
        return Path(self.paths.cache).expanduser()

    def jobs_dir(self) -> Path:
        return Path(self.paths.jobs).expanduser()

    def worktrees_dir(self) -> Path:
        return Path(self.paths.worktrees).expanduser()


def _apply(obj: Any, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if not hasattr(obj, key):
            continue
        current = getattr(obj, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _apply(current, value)
        else:
            setattr(obj, key, value)


def _validate_stages(stages: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(stages, dict):
        raise ValueError("stages must be a mapping of stage name to inference overrides")
    known = {f.name for f in dataclasses.fields(InferenceConfig)}
    out: dict[str, dict[str, Any]] = {}
    for stage, overrides in stages.items():
        if stage not in STAGES:
            raise ValueError(f"unknown model stage {stage!r}; expected one of {STAGES}")
        if not isinstance(overrides, dict):
            raise ValueError(f"stages.{stage} must be a mapping of inference keys")
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise ValueError(f"stages.{stage}: unknown inference keys {unknown}")
        _validate_gate_keys(f"stages.{stage}", overrides)
        out[stage] = dict(overrides)
    return out


def _validate_gate_keys(where: str, values: dict[str, Any]) -> None:
    """request_priority is a whole number, idle_metrics_url an http(s) URL and
    idle_poll_s a positive number; a bad value fails at load, never at call time."""
    if values.get("request_priority") is not None and type(values["request_priority"]) is not int:
        raise ValueError(f"{where}.request_priority must be an integer, got {values['request_priority']!r}")
    url = values.get("idle_metrics_url")
    if url is not None and not (
        isinstance(url, str) and url.startswith(("http://", "https://")) and len(url.split("://", 1)[1]) > 0
    ):
        raise ValueError(f"{where}.idle_metrics_url must be an http(s) URL, got {url!r}")
    poll = values.get("idle_poll_s")
    if poll is not None and (type(poll) not in (int, float) or poll <= 0):
        raise ValueError(f"{where}.idle_poll_s must be a positive number, got {poll!r}")


def require_count(value: Any, key: str) -> int:
    """A whole number of at least 1 (not a bool, float or string), else ValueError."""
    if type(value) is not int or value < 1:
        raise ValueError(f"{key} must be a whole number of at least 1, got {value!r}")
    return value


def _validate_parallelism(cfg: CuratorConfig) -> None:
    """Parallelism settings fail at load, like stages; never a silent fallback."""
    require_count(cfg.limits.parallel_jobs, "limits.parallel_jobs")
    require_count(cfg.gap_worker.max_active_jobs, "gap_worker.max_active_jobs")


def llama_swap_target(metrics_url: str) -> tuple[str, str] | None:
    """(``/running`` URL, model) for a llama-swap metrics URL of the form
    ``<base>/upstream/<model>/metrics``; None for any other shape."""
    parts = urlsplit(metrics_url)
    segments = parts.path.split("/")
    if "upstream" not in segments:
        return None
    index = segments.index("upstream")
    if index + 1 >= len(segments) or not segments[index + 1]:
        return None
    base = f"{parts.scheme}://{parts.netloc}{'/'.join(segments[:index])}"
    return f"{base}/running", unquote(segments[index + 1])


def _validate_gate_models(cfg: CuratorConfig) -> None:
    """The model id in ``idle_metrics_url``'s /upstream/<model>/ segment must be
    the model that block or stage actually requests, else the gate would wait
    for a model that never shows ready. Stages are checked after layering."""
    blocks = [("inference", cfg.inference)]
    blocks += [(f"stages.{stage}", stage_config(cfg, stage).inference) for stage in STAGES]
    for where, inference in blocks:
        target = llama_swap_target(inference.idle_metrics_url) if inference.idle_metrics_url else None
        if target is not None and target[1] != inference.model:
            raise ValueError(
                f"{where}.idle_metrics_url names model {target[1]!r} but {where} uses model "
                f"{inference.model!r}; the /upstream/<model>/ segment must match"
            )


def stage_config(cfg: CuratorConfig, stage: str) -> CuratorConfig:
    """`cfg` with `stage`'s inference overrides applied; the global is never mutated."""
    if stage not in STAGES:
        raise ValueError(f"unknown model stage {stage!r}; expected one of {STAGES}")
    overrides = cfg.stages.get(stage) or {}
    fallback = STAGE_FALLBACK.get(stage)
    if fallback:
        # extraction states only what differs from curation
        overrides = {**(cfg.stages.get(fallback) or {}), **overrides}
    if not overrides:
        return cfg
    inference = dataclasses.replace(cfg.inference, **overrides)
    staged = dataclasses.replace(cfg, inference=inference)
    if cfg.limits.prompt_char_budget == cfg.inference.prompt_char_budget:
        staged.limits = dataclasses.replace(cfg.limits, prompt_char_budget=inference.prompt_char_budget)
    return staged


def load_config(path: str | Path | None = None) -> CuratorConfig:
    """Load the config file, then apply env escapes. Missing file -> defaults."""
    cfg = CuratorConfig()
    env_path = _env("BRAIN_CURATOR_CONFIG", "")
    config_path = Path(path or env_path or "~/.config/brain-curator/config.yaml")
    config_path = config_path.expanduser()
    if config_path.is_file():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            explicit_char_budget = isinstance(data.get("limits"), dict) and "prompt_char_budget" in data["limits"]
            _apply(cfg, data)
            # the default derives from the (possibly overridden) inference
            # targets; an explicitly configured budget is never clobbered
            if not explicit_char_budget:
                cfg.limits.prompt_char_budget = cfg.inference.prompt_char_budget
            cfg.stages = _validate_stages(cfg.stages)
            _validate_gate_keys("inference", dataclasses.asdict(cfg.inference))
    # Env escapes for the values most likely to differ per host.
    cfg.inference.url = _env("CURATOR_INFERENCE_URL", cfg.inference.url)
    cfg.inference.model = _env("CURATOR_INFERENCE_MODEL", cfg.inference.model)
    cfg.inference.api_key_env = _env("CURATOR_INFERENCE_KEY_ENV", cfg.inference.api_key_env)
    cfg.credentials_file = _env("BRAIN_CURATOR_CREDENTIALS_FILE", cfg.credentials_file)
    cfg.pipeline.default_mode = _env("BRAIN_CURATOR_MODE", cfg.pipeline.default_mode)
    if cfg.pipeline.default_mode not in MODES:
        raise ValueError(f"unknown pipeline mode {cfg.pipeline.default_mode!r}; expected one of {MODES}")
    _validate_parallelism(cfg)
    _validate_gate_models(cfg)
    cfg.paths = cfg.paths.expanded()
    return cfg
