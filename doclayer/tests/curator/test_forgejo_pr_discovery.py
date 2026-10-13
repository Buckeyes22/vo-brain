import httpx
import pytest

from doclayer.curator.forgejo import CuratorForgejo, ForgejoError


def client(handler):
    return CuratorForgejo(
        "owner",
        "brain",
        base_url="https://forgejo.test",
        token="fixture",
        transport=httpx.MockTransport(handler),
    )


def rows(start, stop, head="other"):
    return [{"number": i, "head": {"ref": head}} for i in range(start, stop)]


def test_recovers_pr_older_than_first_twenty():
    pages = []

    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(200, json=rows(1, 21) if page == 1 else rows(21, 22, "curator/job"))

    assert client(handler).find_prs_for_head("curator/job")[0]["number"] == 21
    assert pages == [1, 2]


def test_discovery_collects_matches_across_pages_and_deduplicates_overlap():
    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=rows(1, 21, "curator/job") if page == 1 else rows(20, 23, "curator/job"),
        )

    assert len(client(handler).find_prs_for_head("curator/job")) == 22


@pytest.mark.parametrize("failure", ["outage", "malformed", "repeated"])
def test_unseen_or_invalid_pages_cannot_mean_no_prior_pr(failure):
    def handler(request):
        if int(request.url.params["page"]) == 1:
            return httpx.Response(200, json=rows(1, 21))
        if failure == "outage":
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={} if failure == "malformed" else rows(1, 21))

    with pytest.raises(ForgejoError):
        client(handler).find_prs_for_head("curator/job")


@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx.Response(200, json={"commit": {"id": "a" * 40}}), "a" * 40),
        (httpx.Response(404, text="missing"), None),
    ],
)
def test_branch_head_uses_actual_commit_or_confirmed_missing(response, expected):
    assert client(lambda request: response).branch_head("curator/job") == expected


def test_branch_head_unreadable_or_outage_fail_closed():
    for response in [httpx.Response(200, json={}), httpx.Response(503, text="error")]:
        with pytest.raises(ForgejoError):
            client(lambda request: response).branch_head("curator/job")
