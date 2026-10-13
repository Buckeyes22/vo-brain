"""Required CI gates exercised through the real Forgejo HTTP adapter."""

import json
from copy import deepcopy

import httpx
import pytest

from doclayer.curator.forgejo import CuratorForgejo, ForgejoError

SHA = "a" * 40


class Api:
    def __init__(self):
        self.pr = {"number": 1, "head": {"sha": SHA}, "base": {"ref": "main"}, "merged": False}
        self.policy = {"protected": True, "enable_status_check": True, "status_check_contexts": ["ci / test"]}
        self.pages = [[{"id": 1, "context": "ci / test", "status": "success"}]]
        self.posts = []
        self.status_requests = []
        self.pr_reads = 0
        self.policy_reads = 0
        self.change_head = False
        self.change_policy = False
        self.fail_status = False

    def handle(self, request):
        path = request.url.path
        if request.method == "POST":
            self.posts.append(json.loads(request.content))
            self.pr.update(merged=True, merge_commit_sha="b" * 40)
            return httpx.Response(200, json={})
        if path.endswith("/pulls/1"):
            self.pr_reads += 1
            if self.change_head and self.pr_reads == 2:
                self.pr["head"]["sha"] = "c" * 40
            return httpx.Response(200, json=deepcopy(self.pr))
        if path.endswith("/branches/main"):
            self.policy_reads += 1
            if self.change_policy and self.policy_reads == 2:
                self.policy["status_check_contexts"] = ["ci / additional"]
            return httpx.Response(200, json=self.policy)
        if path.endswith("/status"):
            self.status_requests.append(str(request.url))
            if self.fail_status:
                return httpx.Response(503)
            page = int(request.url.params["page"])
            statuses = self.pages[page - 1] if page <= len(self.pages) else []
            return httpx.Response(200, json={"sha": SHA, "statuses": statuses})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def client(self, force_merge=True):
        return CuratorForgejo(
            "Example-org",
            "brain",
            base_url="https://forgejo.test",
            token="fixture-key",
            force_merge=force_merge,
            transport=httpx.MockTransport(self.handle),
        )


@pytest.mark.parametrize("force_merge", [True, False])
def test_successful_required_checks_gate_head_pinned_merge(force_merge):
    api = Api()
    assert api.client(force_merge).merge_pr(1, expected_head=SHA)["merged"]
    assert len(api.posts) == 1
    assert api.posts[0]["head_commit_id"] == SHA
    assert api.posts[0].get("force_merge", False) is force_merge
    assert api.policy_reads == 2


@pytest.mark.parametrize("status", ["pending", "failure", "error", "warning", "skipped", None])
def test_unsuccessful_required_check_prevents_merge(status):
    api = Api()
    api.pages[0][0]["status"] = status
    with pytest.raises(ForgejoError, match="not successful"):
        api.client().merge_pr(1, expected_head=SHA)
    assert api.posts == []


def test_missing_check_prevents_merge():
    api = Api()
    api.pages = [[]]
    with pytest.raises(ForgejoError, match="missing"):
        api.client().merge_pr(1, expected_head=SHA)
    assert api.posts == []


def test_latest_failure_overrides_historical_success_across_pages():
    api = Api()
    api.pages += [[{"id": 2, "context": "ci / test", "status": "failure"}]]
    with pytest.raises(ForgejoError):
        api.client().merge_pr(1, expected_head=SHA)
    assert len(api.status_requests) == 3
    assert api.posts == []


def test_required_context_on_later_page_is_checked():
    api = Api()
    api.pages.insert(0, [{"id": 2, "context": "optional", "status": "failure"}])
    assert api.client().merge_pr(1, expected_head=SHA)["merged"]
    assert len(api.status_requests) == 3


@pytest.mark.parametrize("status", ["success", "failure"])
def test_context_patterns_require_all_matching_checks_success(status):
    api = Api()
    api.policy["status_check_contexts"] = ["ci / *"]
    api.pages[0].append({"id": 2, "context": "ci / lint", "status": status})
    if status == "failure":
        with pytest.raises(ForgejoError):
            api.client().merge_pr(1, expected_head=SHA)
        assert api.posts == []
    else:
        assert api.client().merge_pr(1, expected_head=SHA)["merged"]


@pytest.mark.parametrize("protected", [True, False])
def test_verified_no_required_ci_can_merge_without_status_requests(protected):
    api = Api()
    api.policy = {"protected": protected, "enable_status_check": False, "status_check_contexts": None}
    assert api.client().merge_pr(1, expected_head=SHA)["merged"]
    assert api.status_requests == []


@pytest.mark.parametrize("field", ["protected", "enable_status_check"])
def test_unreadable_policy_is_not_absence_of_required_checks(field):
    api = Api()
    del api.policy[field]
    with pytest.raises(ForgejoError):
        api.client().merge_pr(1, expected_head=SHA)
    assert api.posts == []


@pytest.mark.parametrize("case", ["head", "policy", "wrong-commit", "api-error", "empty-policy", "wrong-base"])
def test_changed_or_unavailable_evidence_prevents_merge(case):
    api = Api()
    expected = SHA
    if case == "head":
        api.change_head = True
    if case == "policy":
        api.change_policy = True
    if case == "wrong-commit":
        expected = "d" * 40
    if case == "api-error":
        api.fail_status = True
    if case == "empty-policy":
        api.policy["status_check_contexts"] = []
    if case == "wrong-base":
        api.pr["base"]["ref"] = "unprotected"
    with pytest.raises(ForgejoError):
        api.client().merge_pr(1, expected_head=expected)
    assert api.posts == []


def test_already_merged_does_not_repeat_post():
    api = Api()
    api.pr.update(merged=True, merge_commit_sha="b" * 40)
    assert api.client().merge_pr(1, expected_head=SHA)["merged"]
    assert api.posts == []


def test_malformed_status_data_prevents_merge():
    api = Api()
    api.pages[0] = [{"context": "ci / test", "status": "success"}]
    with pytest.raises(ForgejoError, match="identity"):
        api.client().merge_pr(1, expected_head=SHA)
    assert api.posts == []
