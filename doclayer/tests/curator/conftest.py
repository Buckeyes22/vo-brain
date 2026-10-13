"""Shared fixtures for curator tests: isolated git repos, scripted Qwen
responses over a real HTTP-protocol mock, deterministic HTTP fixtures, a
hand-built text PDF, and a fake Forgejo."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from doclayer.curator.config import CuratorConfig
from doclayer.curator.forgejo import CuratorForgejo
from doclayer.curator.jobstore import JobStore


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture()
def origin(tmp_path: Path) -> Path:
    """A canonical brain repo as a BARE remote (like Forgejo), seeded from a
    working repo: two wiki pages, own git identity."""
    seed = tmp_path / "seed-brain"
    (seed / "wiki").mkdir(parents=True)
    (seed / "doclayer" / "recipes").mkdir(parents=True)
    (seed / "templates").mkdir(parents=True)
    (seed / "log.md").write_text("# log\n")
    (seed / "index.md").write_text("# index\n")
    (seed / "wiki" / "existing-model.md").write_text(
        "---\nschema_version: 1\nprivacy: internal\nsource_origin: operator\n"
        "source_lineage:\n  - seed:test\ntags:\n  - model\nupdated: 2026-09-01\n"
        "concept_id: c-1111aaaa\ntitle: Existing model\n---\n\n"
        "# Existing model\n\n## Capabilities\n\nKnown facts.\n\n## Safety\n\nKnown safety notes.\n"
    )
    (seed / "wiki" / "other-page.md").write_text(
        "---\nschema_version: 1\nprivacy: internal\nsource_origin: operator\n"
        "source_lineage:\n  - seed:test\ntags:\n  - infra\nupdated: 2026-09-01\n"
        "concept_id: c-2222bbbb\ntitle: Other page\n---\n\n# Other page\n\nContent.\n"
    )
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "test"],
        ["add", "-A"],
        ["commit", "-qm", "init brain"],
    ):
        run_git(seed, *cmd)
    bare = tmp_path / "origin-brain.git"
    run_git(tmp_path, "init", "-q", "--bare", str(bare))
    run_git(seed, "push", "-q", str(bare), "main")
    run_git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return bare


@pytest.fixture()
def checkout(tmp_path: Path, origin: Path) -> Path:
    """The curator's dedicated brain checkout (a clone of the bare origin)."""
    clone = tmp_path / "brain-curator"
    run_git(tmp_path, "clone", "-q", str(origin), str(clone))
    run_git(clone, "config", "user.email", "curator@test.invalid")
    run_git(clone, "config", "user.name", "brain-curator")
    return clone


@pytest.fixture()
def cfg(tmp_path: Path, checkout: Path) -> CuratorConfig:
    config = CuratorConfig()
    config.paths.brain_checkout = str(checkout)
    config.paths.jobs = str(tmp_path / "jobs")
    config.paths.worktrees = str(tmp_path / "worktrees")
    config.paths.cache = str(tmp_path / "cache")
    config.publish.remote = "origin"
    config.publish.base_branch = "main"
    config.publish.forgejo_url = "https://forgejo.test"
    config.inference.url = "http://inference.test/v1"
    config.inference.model = "qwen-test"
    config.credentials_file = str(tmp_path / "credentials.env")
    config.publish.forgejo_token_file = str(tmp_path / "forgejo-token")
    return config


@pytest.fixture()
def store(cfg: CuratorConfig) -> JobStore:
    return JobStore(cfg.jobs_dir())


# --------------------------------------------------------------------------- #
# deterministic HTTP fixture
# --------------------------------------------------------------------------- #


class HttpFixture:
    """Scripted HTTP: {url: (status, html) | bytes | FetchFail}."""

    def __init__(self, pages: dict[str, Any]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            self.requested.append(url)
            entry = self.pages.get(url)
            if entry is None:
                return httpx.Response(404, text="not found")
            if isinstance(entry, Exception):
                raise entry
            if isinstance(entry, tuple):
                status, body = entry
                return httpx.Response(
                    status,
                    text=body,
                    headers={"content-type": "text/html"},
                )
            if isinstance(entry, int):  # redirect
                return httpx.Response(
                    entry,
                    headers={"location": self.redirects[url]},
                )
            return httpx.Response(200, content=entry)

        return httpx.MockTransport(handler)

    redirects: dict[str, str] = {}


@pytest.fixture()
def http_fixture() -> HttpFixture:
    return HttpFixture({})


# --------------------------------------------------------------------------- #
# scripted Qwen (real OpenAI-style HTTP contract over MockTransport)
# --------------------------------------------------------------------------- #


class ScriptedQwen:
    """Returns scripted JSON per task type (keyed on the system prompt), with
    per-test overrides and failure injection."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.overrides: dict[str, list[Any]] = {}  # task -> JSON or request-bound callable replies
        self.failures: list[Exception | str] = []  # consumed per call, any task

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":  # the client's reachability probe: the server answers
                return httpx.Response(404)
            body = json.loads(request.content)
            system = body["messages"][0]["content"]
            user = body["messages"][1]["content"]
            task = self._task_for(system)
            self.calls.append({"system": system[:200], "user": user[:2000], "task": task})
            if self.failures:
                failure = self.failures.pop(0)
                if isinstance(failure, Exception):
                    raise failure
                return httpx.Response(int(failure), text="server error")
            script = self.overrides.get(task)
            if script:
                content = script.pop(0)
                if callable(content):
                    content = content(user)
                if isinstance(content, dict):
                    content = json.dumps(content)
            else:
                content = self.default_reply(task, user)
            if task == "check" and user.startswith("ARTICLE EDITORIAL REVIEW\n"):
                # Preserve existing scripted check outcomes while adapting ONLY
                # their old response schema to exact article locations.
                frame = json.loads(user.split("\n", 1)[1])
                decoded = json.loads(content)
                if isinstance(decoded, dict) and "failures" in decoded:
                    decoded.setdefault("complete", True)
                    for failure in decoded["failures"]:
                        failure.setdefault("section_id", frame["sections"][0]["id"])
                        failure.setdefault("anchor", None)
                    content = json.dumps(decoded)
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "model": "qwen-test",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                },
            )

        return httpx.MockTransport(handler)

    @staticmethod
    def _task_for(system: str) -> str:
        markers = {
            "sp_draft": "single-pass page editor",
            "sp_ground": "single-pass grounding reviewer",
            "plan_pages": "placement editor of a personal knowledge wiki",
            "support": "Verify selected claims against original source excerpts",
            "source_constraints": "Read the CURRENT original source region carefully",
            "relevance": "assess whether a hyperlink",
            "extract": "extract durable, checkable claims",
            "plan": "Place every listed complete source-backed claim",
            "canonical_coverage": "Independently review this CURRENT complete canonical region",
            "draft": "You write one wiki page",
            "check": "fact editor",
            "repair": "repair a wiki page draft",
            "stale": "changed after a curation draft",
            "collision": "Two wiki subjects",
        }
        for task, marker in markers.items():
            if marker in system:
                return task
        return "unknown"

    def default_reply(self, task: str, user: str) -> str:
        if task == "sp_draft":
            # One cited sentence per claim, appended to the section it was
            # routed to, each proven by an integrated anchor. (A JSON re-ask
            # appends its repair note after the frame.)
            frame = json.JSONDecoder().raw_decode(user.split("\n", 1)[1])[0]
            urls = {row["id"]: row["url"] for row in frame["evidence"]}
            claims = {row["id"]: row["claim"] for row in frame["evidence"]}
            sections = []
            for section in frame["sections"]:
                sentences = {
                    identity: f"{claims[identity]} [source: {urls[identity]}]" for identity in section["claim_ids"]
                }
                sections.append(
                    {
                        "section_id": section["section_id"],
                        "edits": [],
                        "append": "\n\n".join(sentences.values()),
                        "handled_claim_ids": list(sentences),
                        "claim_dispositions": [
                            {"id": identity, "status": "integrated", "anchor": sentence}
                            for identity, sentence in sentences.items()
                        ],
                    }
                )
            return json.dumps({"sections": sections, "notes": []})
        if task == "sp_ground":
            frame = json.JSONDecoder().raw_decode(user.split("\n", 1)[1])[0]
            return json.dumps(
                {
                    "verdicts": [
                        {"id": row["id"], "supported": True, "reason": "stated by the evidence"}
                        for row in frame["contributions"]
                    ]
                }
            )
        if task == "plan_pages":
            return json.dumps(
                {
                    "already_covered": False,
                    "explanation": "one page for the model",
                    "pages": [
                        {
                            "action": "create",
                            "path": "wiki/test-model.md",
                            "title": "Test model",
                            "subject_type": "model",
                            "rationale": "model overview",
                            "related": [],
                            "source_ids": [],
                            "claim_ids": [],
                        }
                    ],
                    "consolidation_opportunities": [],
                }
            )
        if task == "source_constraints":
            item = json.loads(user.split("\n", 1)[1])
            assertion = "The model supports a 128,000-token maximum output."
            present = assertion in item["ancestor_context"]
            return json.dumps(
                {
                    "verdicts": [
                        {
                            "id": item["id"],
                            "supported": present,
                            "consistent": True,
                            "reason": "Actual complete fixture source assertion present, otherwise sparse context pending.",
                            "qualifiers": [],
                            "support_quotes": [assertion] if present else [],
                        }
                    ]
                }
            )
        if task == "canonical_coverage":
            item = json.loads(user.split("Claim JSON:\n", 1)[1].split("\nPage metadata:", 1)[0])
            passage = user.split("Canonical passage:\n", 1)[1]
            represented = item["claim"] in passage
            return json.dumps(
                {
                    "covered": represented,
                    "consistent": True,
                    "anchor": item["claim"] if represented else "",
                    "qualifiers": [],
                    "reason": "Exact complete fixture claim occurs in actual canonical passage.",
                }
            )
        if task == "support":
            items = json.loads(user.split("\n", 1)[1])
            return json.dumps(
                {
                    "verdicts": [
                        {
                            "id": item["id"],
                            "supported": True,
                            "reason": "scripted supported source fixture",
                        }
                        for item in items
                    ]
                }
            )
        if task == "relevance":
            return json.dumps({"assessments": []})
        if task == "extract":
            return json.dumps(
                {
                    "doc_kind": "model-card",
                    "claims": [
                        {
                            "claim": "The model supports a 128,000-token maximum output.",
                            "locator": "(introduction)",
                            "modality": "quote",
                            "quote": "128,000-token maximum output",
                            "applicability": {
                                "model": "test-model",
                                "provider": "OpenAI",
                            },
                            "important": True,
                            "excluded_topic": False,
                        }
                    ],
                }
            )
        if task == "plan":
            selected = json.loads(user.split("Claims JSON:\n", 1)[1])
            return json.dumps(
                {
                    "pages": [
                        {
                            "action": "create",
                            "path": "wiki/test-model.md",
                            "title": "Test model",
                            "subject_type": "model",
                            "rationale": "model overview from submitted whole claims",
                            "related": [],
                            "source_ids": list(dict.fromkeys(item["source_id"] for item in selected)),
                            "claim_ids": [item["id"] for item in selected],
                        }
                    ],
                    "covered": [],
                }
            )
        if task in ("draft", "repair", "stale"):
            return json.dumps(
                {
                    "frontmatter": {
                        "schema_version": 1,
                        "privacy": "internal",
                        "source_origin": "external",
                        "source_lineage": ["curator:test"],
                        "tags": ["model"],
                        "updated": "2026-10-01",
                        "title": "Test model",
                        "concept_id": "c-3333cccc",
                        "subject_type": "model",
                    },
                    "body": (
                        "# Test model\n\n## Capabilities\n\nThe model supports a "
                        "128,000-token maximum output. [source: https://docs.test/model]\n\n"
                        "## Related\n\n[[test-model-prompting]]\n"
                    ),
                    "notes": [],
                }
            )
        if task == "collision":
            return json.dumps(
                {
                    "resolution": "same-subject",
                    "explanation": "same model, merge",
                    "frontmatter": {},
                    "body": "# merged\n",
                }
            )
        if task == "check":
            return json.dumps({"failures": [], "conflicts": []})
        return json.dumps({})


@pytest.fixture()
def qwen() -> ScriptedQwen:
    return ScriptedQwen()


# --------------------------------------------------------------------------- #
# fake Forgejo (same interface as CuratorForgejo)
# --------------------------------------------------------------------------- #


class FakeForgejo:
    """In-memory Forgejo backed by the bare origin: a merge really advances
    `main` server-side (fast-forward), the way Forgejo would. Injectable
    failures at each step."""

    def __init__(self, force_merge: bool = True) -> None:
        self.force_merge = force_merge
        self.branches: set[str] = set()
        self.prs: dict[int, dict[str, Any]] = {}
        self._next = 1
        self.origin_repo: Path | None = None
        self.base_branch: str = "main"
        self.fail_create = 0  # raise on the next N create_pr calls
        self.fail_merge = 0  # raise before the next N merges
        self.fail_after_merge = 0  # merge, then lose the response (raise)
        self.merge_calls: list[int] = []

    # -- interface used by Publisher -------------------------------------- #

    def branch_exists(self, branch: str) -> bool:
        return branch in self.branches

    def branch_head(self, branch: str) -> str | None:
        return self._branch_sha(branch) if branch in self.branches else None

    def create_pr(self, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        if self.fail_create > 0:
            self.fail_create -= 1
            from doclayer.curator.forgejo import ForgejoError

            raise ForgejoError("forgejo POST pulls: HTTP 502 (flaky)")
        number = self._next
        self._next += 1
        pr = {
            "number": number,
            "title": title,
            "head": {"ref": head, "sha": self._branch_sha(head)},
            "base": {"ref": base},
            "state": "open",
            "merged": False,
            "merge_commit_sha": None,
            "html_url": f"https://forgejo.test/Example-org/brain/pulls/{number}",
        }
        self.prs[number] = pr
        return pr

    def get_pr(self, number: int) -> dict[str, Any]:
        return self.prs[number]

    def find_prs_for_head(self, head: str) -> list[dict[str, Any]]:
        return [pr for pr in self.prs.values() if pr["head"]["ref"] == head]

    def _branch_sha(self, branch: str) -> str:
        assert self.origin_repo is not None, "tests must set origin_repo"
        return run_git(self.origin_repo, "rev-parse", f"refs/heads/{branch}").strip()

    def merge_pr(self, number: int, *, expected_head: str | None = None) -> dict[str, Any]:
        if self.fail_merge > 0:
            self.fail_merge -= 1
            from doclayer.curator.forgejo import ForgejoError

            raise ForgejoError("forgejo POST merge: HTTP 502 (flaky)")
        self.merge_calls.append(number)
        pr = self.prs[number]
        head_sha = self._branch_sha(pr["head"]["ref"])
        if expected_head and head_sha != expected_head:
            from doclayer.curator.forgejo import ForgejoError

            raise ForgejoError("PR head changed since validation")
        if self.origin_repo is not None:
            # simulate the server-side (fast-forward) merge landing on main
            run_git(
                self.origin_repo,
                "update-ref",
                f"refs/heads/{self.base_branch}",
                head_sha,
            )
        pr["state"] = "closed"
        pr["merged"] = True
        pr["merge_commit_sha"] = head_sha
        if self.fail_after_merge > 0:
            # the merge landed but the HTTP response was lost
            self.fail_after_merge -= 1
            from doclayer.curator.forgejo import ForgejoError

            raise ForgejoError("forgejo POST merge: connection reset after merge")
        return pr

    # -- statics (CuratorForgejo static methods) --------------------------- #

    pr_merged = staticmethod(CuratorForgejo.pr_merged)
    merge_commit = staticmethod(CuratorForgejo.merge_commit)


@pytest.fixture()
def forgejo(origin: Path) -> FakeForgejo:
    fake = FakeForgejo()
    fake.origin_repo = origin
    return fake


# --------------------------------------------------------------------------- #
# hand-built text PDF (real PDF syntax; pypdf parses it for real)
# --------------------------------------------------------------------------- #


def make_pdf(path: Path, pages: list[str]) -> Path:
    objs: list[str] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append("<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>")
    font_id = 3 + 2 * n
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text.replace('(', '[').replace(')', ']')}) Tj ET"
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {4 + 2 * i} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        )
        objs.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = "%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out.encode()))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref_pos = len(out.encode())
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n"
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    path.write_bytes(out.encode())
    return path


def valid_meta(cid: str = "c-3333cccc", **overrides: Any) -> dict[str, Any]:
    meta = {
        "schema_version": 1,
        "privacy": "internal",
        "source_origin": "external",
        "source_lineage": ["curator:test"],
        "tags": ["model"],
        "updated": "2026-10-01",
        "title": "Test model",
        "concept_id": cid,
    }
    meta.update(overrides)
    return meta
