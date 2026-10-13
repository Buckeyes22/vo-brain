"""A page's repo path stem is scope context in the resolver."""

from __future__ import annotations

import json

from test_resolver_selection import page

from doclayer import resolver

QUESTION = "Where does app-db keep its data directory on App1?"
PAGE = page(
    "wiki/app-db-app-1-access.md",
    "# app-db access\n\nThe app-db data directory is `/var/lib/postgresql/data` inside the container.\n",
    "app-db access",
)


def test_resolver_passes_the_page_path_to_source_scope(monkeypatch):
    seen = []
    real = resolver.source_scope

    def spy(meta, **kwargs):
        seen.append(kwargs.get("path"))
        return real(meta, **kwargs)

    monkeypatch.setattr(resolver, "source_scope", spy)
    unscoped = "Where does app-db keep its data directory?"
    answer = resolver.Answer(unscoped, None)
    resolver._append_page_facts(answer, PAGE, unscoped)
    assert answer.git_facts
    assert seen and set(seen) == {"wiki/app-db-app-1-access.md"}


def test_path_stem_naming_the_scope_keeps_an_untagged_page_in_scope():
    answer = resolver.Answer(QUESTION, None)
    assert any(c["dimension"] == "platform" for c in answer.scope_constraints)
    resolver._append_page_facts(answer, PAGE, QUESTION)
    assert any("/var/lib/postgresql/data" in f["value"] for f in answer.git_facts)
    scope = json.loads(answer.git_facts[0]["_scope"])
    assert "app db app 1 access" in scope["context"]
