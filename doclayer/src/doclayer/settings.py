"""Doc layer settings: endpoints and knobs, all overridable by env vars."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def is_host(name: str) -> bool:
    """True when this machine is `name`: the first DNS label matches
    case-insensitively (short name vs FQDN, either way) or the FQDNs are equal."""
    wanted = (name or "").strip().lower()
    current = socket.gethostname().strip().lower()
    return bool(wanted and current) and (wanted == current or wanted.split(".", 1)[0] == current.split(".", 1)[0])


class BrainRepoConfigurationError(ValueError):
    """An installed canonical reference is unavailable; never fall back silently."""


class BrainCorpusConfigurationError(ValueError):
    """The configured search target does not match the owning brain writer."""


BRAIN_COLLECTION = "vo_rag_brain_v1"


class Settings:
    """Endpoint settings; the canonical brain pointer is read on each access.
    No secrets in here by default —
    the vo-rag API key is loaded lazily from a file."""

    def __init__(self) -> None:
        self._brain_repo_override: Path | None = None
        self.vo_rag_url = _env("DOC_LAYER_VO_RAG_URL", "http://localhost:8001")
        self.vo_rag_key_file = Path(_env("DOC_LAYER_VO_RAG_KEY_FILE", "~/.config/doclayer/vo-rag-api-key")).expanduser()
        self.qdrant_url = _env("DOC_LAYER_QDRANT_URL", "http://localhost:6333")
        self.brain_collection = _env("DOC_LAYER_BRAIN_COLLECTION", BRAIN_COLLECTION)
        self.context7_url = _env("DOC_LAYER_CONTEXT7_URL", "https://mcp.context7.com/mcp")
        #: Forgejo web/API base ("" = publishing disabled until configured)
        #: and where the API token is kept (chmod 600; never commit or print it).
        self.forgejo_url = _env("DOC_LAYER_FORGEJO_URL", "")
        self.forgejo_token_file = Path(
            _env("DOC_LAYER_FORGEJO_TOKEN_FILE", "~/.config/doclayer/forgejo-token")
        ).expanduser()
        #: Optional routing-config JSON the doc layer may read (route fact
        #: only). None = no route facts.
        _route = _env("DOC_LAYER_ROUTE_CONFIG", "")
        self.route_config: Path | None = Path(_route).expanduser() if _route else None
        self.brain_sync_state = _env("DOC_LAYER_BRAIN_SYNC_STATE", "~/.local/state/vo-rag/brain_sync.json")
        self.ssh_host = _env("DOC_LAYER_SSH_HOST", "")
        #: Address of the search host, which serves the vo-rag search API ("" = unset).
        self.search_host_ip = _env("DOC_LAYER_SEARCH_HOST_IP", "")
        self.timeout_vo_rag_s = float(_env("DOC_LAYER_TIMEOUT_VO_RAG_S", "25"))
        self.timeout_context7_s = float(_env("DOC_LAYER_TIMEOUT_CONTEXT7_S", "40"))
        self.timeout_live_s = float(_env("DOC_LAYER_TIMEOUT_LIVE_S", "15"))
        #: A doc copy older than this (days) is flagged stale.
        self.stale_copy_days = int(_env("DOC_LAYER_STALE_COPY_DAYS", "60"))
        #: Selective routing: the committed policy travels with the brain repo.
        self.routing_policy_path = (
            Path(_env("DOC_LAYER_ROUTING_POLICY", "")).expanduser() if _env("DOC_LAYER_ROUTING_POLICY", "") else None
        )
        self.catalog_cache_dir = Path(_env("DOC_LAYER_CATALOG_CACHE_DIR", "~/.cache/doclayer")).expanduser()
        #: Knowledge-gap ledger state root.
        self.gap_state_dir = Path(
            _env(
                "DOC_LAYER_GAPS_DIR",
                "~/.local/state/doclayer/knowledge-gaps",
            )
        ).expanduser()
        # Client capture spools locally; one owner host holds the ledger. An
        # empty owner host means this machine owns it. On the named owner host
        # itself the ledger is local; elsewhere it is reached over ssh. An
        # explicit DOC_LAYER_GAP_OWNER always wins.
        self.gap_owner_host = _env("DOC_LAYER_GAP_OWNER_HOST", "")
        self.gap_owner = _env(
            "DOC_LAYER_GAP_OWNER",
            "local" if not self.gap_owner_host or is_host(self.gap_owner_host) else "ssh",
        )
        self.gap_owner_command = _env("DOC_LAYER_GAP_OWNER_COMMAND", "doclayer-gap-ops")
        # Local inference has no daily quota. Retired daily-limit environment
        # variables cannot silently re-enable it after an upgrade.
        self.gap_jobs_per_day = None
        self.gap_model_calls_day = None
        self.gap_model_calls_job = int(_env("DOC_LAYER_GAP_MODEL_CALLS_JOB", "50"))
        #: Capture can be paused without deleting durable records.
        self.gap_capture_enabled = _env("DOC_LAYER_GAP_CAPTURE", "1") == "1"
        #: Negative-result cache for observed unsuccessful lookups.
        self.negative_cache_path = Path(
            _env(
                "DOC_LAYER_NEGATIVE_CACHE",
                "~/.cache/doclayer/negative-cache.json",
            )
        ).expanduser()
        #: Operator-declared local evidence profiles (which kinds of question
        #: need local evidence). Absent = dormant.
        self.local_profiles_path = Path(
            _env("DOC_LAYER_LOCAL_PROFILES", "~/.config/doclayer/local-profiles.yaml")
        ).expanduser()
        #: SearXNG search endpoint for public-source discovery ("" = SearXNG
        #: discovery off; other discovery sources still run) and the directory
        #: holding its mutual-TLS files (ca.crt, client.crt, client.key).
        self.searxng_url = _env("DOC_LAYER_SEARXNG_URL", "")
        self.searxng_cert_dir = Path(_env("DOC_LAYER_SEARXNG_CERT_DIR", "~/.config/doclayer/searxng")).expanduser()
        #: Release symlink of the deployed search service on the search host
        #: ("" = the release live check reports "not configured").
        self.search_release_link = _env("DOC_LAYER_SEARCH_RELEASE_LINK", "")
        #: Gateway container names the gateway live check lists (regex alternation).
        self.gateway_containers = _env("DOC_LAYER_GATEWAY_CONTAINERS", "litellm|litellm-db")
        #: Roots under which private-page `head` reads are allowed (`:`-separated).
        self.private_read_roots = tuple(
            str(Path(root).expanduser()).rstrip("/") + "/"
            for root in _env("DOC_LAYER_PRIVATE_READ_ROOTS", "~/git/:~/src/").split(":")
            if root.strip()
        )

    def validated_brain_collection(self) -> str:
        """Pin brain searches to the supported writer, including injected config.

        A relocation needs a coordinated writer migration. Never interpret an
        empty, wildcard, library or combined target as a brain search.
        """
        if self.brain_collection != BRAIN_COLLECTION:
            raise BrainCorpusConfigurationError(
                "DOC_LAYER_BRAIN_COLLECTION must be vo_rag_brain_v1; "
                "brain search withheld because the configured target does not "
                "match the brain writer (collection relocation requires a migration)"
            )
        return BRAIN_COLLECTION

    def brain_repo_selection(self) -> tuple[Path, bool]:
        """Return the repository and committed-read policy from one observation."""
        # Resolve on every use, including long-lived MCP processes.
        if self._brain_repo_override is not None:
            return self._brain_repo_override, False
        explicit = os.environ.get("DOC_LAYER_BRAIN_REPO")
        if explicit is not None:
            return Path(explicit).expanduser(), False
        pointer = Path.home() / ".config/doclayer/brain-repo"
        try:
            if not pointer.exists() and not pointer.is_symlink():
                return Path("~/brain").expanduser(), False
            value = pointer.read_text(encoding="utf-8").strip()
            root = Path(value)
            if not value or "\n" in value or not root.is_absolute():
                raise ValueError("expected one absolute repository path")
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode or Path(result.stdout.strip()).resolve() != root.resolve():
                raise ValueError(f"reference is missing or is not a Git repository: {root}")
            return root, True
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise BrainRepoConfigurationError(
                f"canonical brain reference unavailable ({pointer}): {exc}; repair the pointer or run install-workstation.py"
            ) from exc

    @property
    def brain_repo(self) -> Path:
        return self.brain_repo_selection()[0]

    @brain_repo.setter
    def brain_repo(self, value: Path | str) -> None:
        # Retains dependency injection for callers and tests.
        self._brain_repo_override = Path(value)

    @property
    def brain_reference_enabled(self) -> bool:
        return self.brain_repo_selection()[1]

    def vo_rag_api_key(self) -> str | None:
        """The key from the key file, or None (the source degrades gracefully)."""
        try:
            key = self.vo_rag_key_file.read_text(encoding="utf-8").strip()
            return key or None
        except OSError:
            return None


settings = Settings()
