"""A repeat discovery remains attributed to the actual source-finding route."""

import httpx

from doclayer import discovery
from doclayer.coverage import Catalog


def test_stored_search_candidate_revalidates_without_becoming_attachment(monkeypatch):
    judged = []
    url = "https://raw.githubusercontent.com/vendor/widget/main/guide.md"
    monkeypatch.setattr(discovery, "_qwen_available", lambda: True)
    monkeypatch.setattr(
        discovery,
        "_qwen_authoritative",
        lambda *args, **kwargs: judged.append(args) or True,
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="Widget complete public guide"))
    ) as client:
        result = discovery.discover(
            {
                "gap_id": "gap-provenance",
                "identity": "unknown:widget",
                "representative_question": "How does Widget configure retries?",
                "source_candidates": [{"url": url, "authority": "official", "origin": "search-candidate"}],
            },
            Catalog(status="ok", revision="a" * 40, policy_version=1),
            client=client,
        )
    assert len(judged) == result.model_calls == 1
    assert result.seeds[0].origin == "search-candidate"
    assert result.seeds[0].relevance == "semantic-authority"
    assert "attachment" not in result.seeds[0].to_dict()
