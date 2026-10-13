"""A terminal API page is exhaustion; required head-bound checks still gate merge."""

import httpx
import pytest
from test_forgejo_checks import SHA, Api

from doclayer.curator.forgejo import CuratorForgejo, ForgejoError


def client_with_terminal(api, terminal):
    def handler(request):
        if request.url.path.endswith("/status") and request.url.params.get("page") == "2":
            api.status_requests.append(str(request.url))
            return httpx.Response(200, json=terminal)
        return api.handle(request)

    return CuratorForgejo(
        "Example-org", "brain", base_url="https://forgejo.test", token="fixture", transport=httpx.MockTransport(handler)
    )


def test_verified_head_checks_allow_real_empty_sha_terminal_page():
    api = Api()
    result = client_with_terminal(api, {"sha": "", "statuses": [], "total_count": 0}).merge_pr(1, expected_head=SHA)
    assert result["merged"] and len(api.posts) == 1 and len(api.status_requests) == 2


@pytest.mark.parametrize(
    "terminal",
    [
        {"sha": "", "statuses": [{"id": 2, "context": "ci / test", "status": "success"}]},
        {"sha": "b" * 40, "statuses": []},
        {"sha": "", "statuses": [], "total_count": 2},
        {"sha": "", "statuses": [], "total_count": True},
    ],
)
def test_empty_page_exception_never_admits_foreign_rows_or_incomplete_pagination(terminal):
    api = Api()
    with pytest.raises(ForgejoError):
        client_with_terminal(api, terminal).merge_pr(1, expected_head=SHA)
    assert api.posts == []


def test_empty_terminal_cannot_supply_missing_required_check():
    api = Api()
    api.pages = [[{"id": 1, "context": "optional", "status": "success"}]]
    with pytest.raises(ForgejoError, match="missing"):
        client_with_terminal(api, {"sha": "", "statuses": [], "total_count": 0}).merge_pr(1, expected_head=SHA)
    assert api.posts == []


def test_first_empty_sha_page_cannot_attest_validated_head():
    api = Api()

    def handler(request):
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"sha": "", "statuses": [], "total_count": 0})
        return api.handle(request)

    client = CuratorForgejo(
        "Example-org", "brain", base_url="https://forgejo.test", token="fixture", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ForgejoError, match="validated head"):
        client.merge_pr(1, expected_head=SHA)
    assert api.posts == []
