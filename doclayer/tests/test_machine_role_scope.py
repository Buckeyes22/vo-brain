"""A machine-role phrase ("the Linux workstation") describes the asker's machine, not a
platform the answer must declare. It must not filter out the named host's own bullet."""

import pytest
from test_resolver_selection import page

from doclayer import applicability, resolver

QUESTION = "How can the operator mount the Mac mini in Files on the Linux workstation?"
MAC_ADDRESS = "192.0.2.16"

FLEET = page(
    "infra/fleet-hosts.md",
    (
        "# Fleet hosts\n\n## Hosts and roles\n\n"
        "- **Mac mini** — hostname `Example-Mini` (macOS 26.3, arm64), LAN IP `192.0.2.16`. SSH alias `mac1`.\n"
        "  - **File sharing that already exists:** macOS Sharing lists one SMB share point; port 445 is open.\n"
        '  - **Mount in Files on ws1 (GNOME Files, Other Locations, "Connect to Server"):** enter `smb://192.0.2.16/` for the SMB share.\n'
        "- **ws1** — the local workstation.\n"
    ),
    "fleet-hosts",
)
UNRELATED = page(
    "wiki/agents-guidance.md",
    (
        "# Agent guidance\n\nAGENTS.md is an open format used by open-source projects "
        "and stewarded by the Linux Foundation; keep Files small.\n"
    ),
    "agents-guidance",
)


def select(pages, question):
    answer = resolver.Answer(question, None)
    for p in pages:
        resolver._append_page_facts(answer, p, question)
    return resolver._rank_facts(answer.git_facts, question, 8)


@pytest.fixture
def mac_hosts(identities):
    identities(
        hosts=["Mac mini", "mac1", "ws1"],
        groups=[{"Mac mini", "mac1", MAC_ADDRESS}],
        declared=[["Mac mini", "mac1"], ["ws1"]],
    )


def test_linux_workstation_is_not_a_platform_filter_when_a_host_is_named(mac_hosts):
    assert applicability.requested_scopes(QUESTION) == [{"dimension": "platform", "value": "Files"}]


def test_mac_mini_mount_sentence_beats_the_unrelated_page(mac_hosts):
    facts = select([UNRELATED, FLEET], QUESTION)
    assert {f["source"].split("@", 1)[0] for f in facts} == {"infra/fleet-hosts.md"}
    assert any("smb://192.0.2.16/" in f["value"] for f in facts)


def test_machine_phrase_still_scopes_a_question_that_names_no_host(mac_hosts):
    scopes = applicability.requested_scopes("Is the agent installer supported on the Linux workstation?")
    assert {"dimension": "platform", "value": "Linux"} in scopes
