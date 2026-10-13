"""An error message's object ("No space left on device") is not a hosting platform."""

import pytest

from doclayer.applicability import effective_question_scopes, requested_scopes

Q2869 = (
    "Where is App1 app-db PostgreSQL data mounted, and are there documented storage limits or recovery "
    "procedures for No space left on device and recovery mode?"
)


def test_the_gap_2869_question_has_no_device_platform_scope():
    assert requested_scopes(Q2869) == []
    # the artifact already stored on the gap record is dropped when scopes are recomputed
    assert effective_question_scopes(Q2869, [{"dimension": "platform", "value": "device"}]) == []


@pytest.mark.parametrize(
    "question",
    [
        "What does No space left on device mean for docker?",
        "How is the no space left on device error handled on the app-db volume?",
    ],
)
def test_error_message_objects_are_not_platforms(question):
    assert all(item["value"].lower() != "device" for item in requested_scopes(question))


def test_a_real_host_named_beside_the_error_message_stays_a_scope():
    scopes = requested_scopes("Why does No space left on device happen on App1?")
    assert [s["value"] for s in scopes] == ["App1"]


def test_a_device_named_outside_the_error_phrase_is_still_a_platform():
    values = [s["value"] for s in requested_scopes("How do I free space on device sda1? No space left on device")]
    assert values == ["device sda1"]


@pytest.mark.parametrize(
    "question, expected",
    [
        ("How much space left on OneCloud for Nimbus jobs?", ["OneCloud"]),
        ("Is there storage space left on OneCloud?", ["OneCloud"]),
        ("Does Nimbus run on OneCloud?", ["OneCloud"]),
        ("Why does postgres say Connection refused on port 5432?", []),
        ("How do I fix 'address already in use on port 8080' with Forgejo?", []),
        ("Why do I get 'Read-only file system' on device?", []),
        ("Why do I get Permission denied on /data?", []),
        ("Why do writes fail with disk quota exceeded on /mnt/fast?", []),
        ("What does 'too many open files on worker1' mean?", ["worker1"]),
        ("Why does No space left on device happen on App1?", ["App1"]),
        ("Is there space left on SVC1?", ["SVC1"]),
        ("Does grafana run on worker1?", ["worker1"]),
        ("Is ollama hosted on Gpu1?", ["Gpu1"]),
    ],
)
def test_error_messages_are_not_platforms_but_hosts_and_named_platforms_stay(question, expected, identities):
    identities(hosts=["SVC1", "worker1", "App1", "Gpu1"])
    assert [s["value"] for s in requested_scopes(question)] == expected


def test_port_numbers_are_never_hosting_platforms():
    for q in ("Why is port 5432 refused?", "Connection refused on port 5432", "What listens on port 8080?"):
        assert all(not s["value"].lower().startswith("port ") for s in requested_scopes(q)), q


def test_a_bare_backticked_or_quoted_name_stays_a_scope_end_to_end(identities):
    identities(hosts=["SVC1"])
    stored = [{"dimension": "platform", "value": "OneCloud"}]
    for q in (
        "How do I deploy Nimbus on `OneCloud`?",
        'Does Nimbus run on "OneCloud"?',
        "Does Nimbus run on \u2018OneCloud\u2019?",
        "Does Nimbus run on \u201cOneCloud\u201d?",
    ):
        assert [s["value"] for s in requested_scopes(q)] == ["OneCloud"], q
        assert effective_question_scopes(q, stored) == stored, q
    assert [s["value"] for s in requested_scopes("Does Nimbus run on `SVC1` today?")] == ["SVC1"]
    assert [s["value"] for s in requested_scopes("Does Nimbus run on `Hetzner Cloud`?")] == ["Hetzner Cloud"]


def test_a_quoted_message_object_is_still_dropped():
    stored = [{"dimension": "platform", "value": "OneCloud"}]
    for q in (
        "What does 'deploy failed on OneCloud' mean?",
        'What does "deploy failed on OneCloud" mean?',
        "What does \u201cdeploy failed on OneCloud\u201d mean?",
        "What does \u2018deploy failed on OneCloud\u2019 mean?",
    ):
        assert [s["value"] for s in requested_scopes(q)] == [], q
        assert effective_question_scopes(q, stored) == [], q
    # a platform named outside the message still counts
    both = "What does 'deploy failed on OneCloud' mean for Nimbus on OneCloud?"
    assert [s["value"] for s in requested_scopes(both)] == ["OneCloud"]


@pytest.mark.parametrize(
    "question",
    [
        "Why do I get No space left on the device?",
        "Why do I get No space left on `device`?",
        "Why do I get \u201cNo space left on device\u201d?",
    ],
)
def test_an_article_or_quote_around_the_error_object_still_drops_it(question):
    assert requested_scopes(question) == []


def test_error_object_helper_cases():
    from doclayer.applicability import _error_message_object

    assert _error_message_object("How do I deploy Nimbus on `OneCloud`?", "OneCloud") is False
    assert _error_message_object("How much space left on OneCloud?", "OneCloud") is False
    assert _error_message_object("Is there no space left on OneCloud for jobs?", "OneCloud") is True
    assert _error_message_object("No space left on the device", "device") is True
    assert _error_message_object("Connection refused on port 5432", "port 5432") is True
    stored = [{"dimension": "platform", "value": "OneCloud"}]
    assert effective_question_scopes("How much space left on OneCloud?", stored) == stored


def test_the_article_allowance_covers_only_generic_storage_objects():
    from doclayer.applicability import _error_message_object

    for obj in ("device", "disk", "drive", "volume", "filesystem", "file system", "partition", "mount"):
        assert _error_message_object(f"Why do I get No space left on the {obj}?", obj) is True, obj
        assert _error_message_object(f"Why do I get No space left on {obj}?", obj) is True, obj
    # a named platform after an article is a platform, not an error object
    assert _error_message_object("Is there no space left on the OneCloud instance?", "OneCloud") is False
    assert _error_message_object("Is there no space left on the OneCloud?", "OneCloud") is False
    assert [s["value"] for s in requested_scopes("Is there no space left on the OneCloud instance?")] == ["OneCloud"]
    assert requested_scopes("Why do I get No space left on the device?") == []
