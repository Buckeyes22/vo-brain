"""Existing-mode candidates are canonical wiki/infra pages only."""

import hashlib
import json

import httpx
from test_verification_contract import git, positive, published  # noqa: F401

from doclayer import resolver, verification
from doclayer.brainrepo import BrainRepo
from doclayer.curator.config import CuratorConfig
from doclayer.curator.model import ModelClient
from doclayer.curator.state import Usage
from doclayer.gap_budget import ResourceBudget

NOISE = "# Nimbus request rate limit\n\n" + "".join(
    f"Nimbus request rate limit {n} is {n} RPM. What is the Nimbus rate limit? "
    "Nimbus request rate limit rate limit.\n\n"
    for n in range(1, 25)
)


def recording_client(tmp_path, reply):
    cfg = CuratorConfig()
    cfg.credentials_file = str(tmp_path / "no-credentials")
    cfg.inference.retries = 0
    frames = []

    def handler(request):
        frames.append(request.content.decode())
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]},
        )

    return ModelClient(cfg, Usage(), transport=httpx.MockTransport(handler)), frames


def commit_files(root, files, index):
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(root, "add", rel)
    git(root, "commit", "-m", "add files")
    index["last_commit"] = git(root, "rev-parse", "HEAD")


def test_canonical_view_lists_only_wiki_and_infra(published):
    root, path, commit, ledger, record, reply, index = published
    commit_files(
        root,
        {
            "docs/plan.md": "# Plan\n",
            "doclayer/STATUS.md": "# Status\n",
            "inbox/proposal.md": "# Proposal\n",
            "infra/host.md": "---\ntitle: Host\n---\n# Host\n",
            "index.md": "# Index\n",
        },
        index,
    )
    repo = BrainRepo(root, snapshot=True)
    view = verification._CanonicalBrainRepo(repo)
    assert view.markdown_paths() == ["infra/host.md", "wiki/nimbus.md"]
    assert view.read_page("wiki/nimbus.md").body == repo.read_page("wiki/nimbus.md").body
    assert view.head_commit() == repo.head_commit() and view.root == repo.root
    assert view.find_page("STATUS") is None


def test_doclayer_docs_and_inbox_pages_do_not_compete_for_candidates(published, tmp_path, monkeypatch):
    root, path, commit, ledger, record, reply, index = published
    quiet = "Nimbus rate limit is 40 RPM.\n"
    commit_files(root, {path: "---\ntitle: Nimbus\nproduct: nimbus\n---\n# Nimbus\n## Rate\n" + quiet}, index)
    commit_files(
        root,
        {
            "doclayer/STATUS.md": NOISE,
            "docs/planning/plans/nimbus.md": NOISE,
            "inbox/nimbus-proposal.md": NOISE,
        },
        index,
    )
    index["files"][path] = hashlib.sha256((root / path).read_bytes()).hexdigest()
    record.job_links = []
    ledger.save(record)
    reply = positive(path)
    reply["facets"][0]["evidence"][0]["quote"] = "Nimbus rate limit is 40 RPM."
    answers = []
    real_resolve = resolver.resolve

    async def spy(question, **kwargs):
        answer = await real_resolve(question, **kwargs)
        answers.append(answer)
        return answer

    monkeypatch.setattr(verification.resolver_module, "resolve", spy)
    model, frames = recording_client(tmp_path, reply)
    result = verification.verify_existing_gap(
        record.gap_id, ledger, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "resolved", result.notes
    assert answers, "candidates were discovered through the resolver"
    for answer in answers:
        sources = {f["source"].split("@", 1)[0] for f in answer.git_facts + answer.doc_facts}
        assert sources and all(src.startswith(("wiki/", "infra/")) for src in sources), sources
    assert frames, "the canonical frame must be sent"
    for frame in frames:
        assert "doclayer/STATUS.md" not in frame
        assert "docs/planning" not in frame
        assert "inbox/nimbus-proposal.md" not in frame
    saved = ledger.load(record.gap_id)
    assert saved.resolution_verification["verified_paths"] == [path]


def test_catalog_subject_pages_are_candidates_even_when_the_resolver_nominates_none(published, tmp_path, monkeypatch):
    root, path, commit, ledger, record, reply, index = published
    commit_files(root, {"doclayer/routing-policy.yaml": "version: 1\n"}, index)
    record.job_links = []
    ledger.save(record)

    async def nothing(question, **kwargs):
        return resolver.Answer(question, "nimbus", git_facts=[])

    monkeypatch.setattr(verification.resolver_module, "resolve", nothing)
    model, frames = recording_client(tmp_path, positive(path))
    result = verification.verify_existing_gap(
        record.gap_id, ledger, model_client=model, budget=ResourceBudget(ledger.root)
    )
    saved = ledger.load(record.gap_id)
    assert saved.resolution_verification["verified_paths"] == [path], result.notes
    assert frames and path in frames[0]
