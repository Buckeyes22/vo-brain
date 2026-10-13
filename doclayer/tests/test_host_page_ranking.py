"""A question that asks only about a known host also considers that host's pages
(its own and those of the services recorded on it), on a bounded ranking credit."""

from test_resolver_selection import page

from doclayer import applicability, resolver


def rank(pages, question, cap=8):
    answer = resolver.Answer(question, None)
    for p in pages:
        resolver._append_page_facts(answer, p, question)
    return resolver._rank_facts(answer.git_facts, question, cap)


def sources(facts):
    return {f["source"].split("@", 1)[0] for f in facts}


FORGEJO = page(
    "infra/forgejo.md",
    "# Forgejo (git host)\n\nForgejo is the git remote for the repositories.\n\nIt listens on port 3000.\n",
    "forgejo",
)
OWN = page(
    "infra/svc1-services.md", "# SVC1 services\n\nThe services box runs the monitoring exporters.\n", "svc1-services"
)
FLEET = page(
    "infra/fleet-hosts.md",
    "# Fleet\n\n- **SVC1** is the services host.\n- **App1** runs the inference stack.\n",
    "fleet-hosts",
)
UBUNTU = page(
    "wiki/ubuntu-inference.md",
    "# Inference\n\nApp1 serves the local model endpoint on port 8080.\n",
    "ubuntu-inference",
)
LITELLM = page(
    "infra/litellm-hub.md", "# LiteLLM hub\n\nThe hub routes model traffic and keeps usage logs.\n", "litellm-hub"
)
BACKUP = page(
    "wiki/vaultwarden-backup.md",
    "# Vaultwarden backup\n\nRestore the Vaultwarden backup on App1 with the restore script.\n",
    "vaultwarden-backup",
)


def profiles():
    return (
        applicability.HostProfile(
            frozenset({"svc1"}),
            frozenset({"infra/svc1-services"}),
            frozenset({"forgejo"}),
            frozenset({"infra/forgejo"}),
        ),
        applicability.HostProfile(
            frozenset({"app1"}), frozenset(), frozenset({"litellmhub"}), frozenset({"infra/litellm-hub"})
        ),
    )


def test_svc1_question_delivers_the_page_of_the_service_recorded_on_it(identities):
    identities(hosts=["SVC1", "App1"], profiles=profiles())
    got = sources(rank([FORGEJO, OWN, FLEET, UBUNTU, LITELLM], "What does SVC1 serve?"))
    assert {"infra/forgejo.md", "infra/svc1-services.md", "infra/fleet-hosts.md"} <= got
    assert "infra/litellm-hub.md" not in got and "wiki/ubuntu-inference.md" not in got


def test_without_the_profile_the_forgejo_page_is_not_delivered(identities):
    identities(hosts=["SVC1", "App1"])
    assert "infra/forgejo.md" not in sources(rank([FORGEJO, OWN, FLEET], "What does SVC1 serve?"))


def test_app1_question_still_delivers_its_own_pages(identities):
    identities(hosts=["SVC1", "App1"], profiles=profiles())
    got = sources(rank([FORGEJO, FLEET, UBUNTU, LITELLM], "What does App1 serve?"))
    assert {"wiki/ubuntu-inference.md", "infra/fleet-hosts.md"} <= got
    assert "infra/forgejo.md" not in got


def test_question_with_its_own_subject_gets_no_host_page_additions(identities):
    identities(hosts=["SVC1", "App1"], profiles=profiles())
    got = sources(rank([BACKUP, LITELLM, FLEET], "How do I restore the Vaultwarden backup on App1?"))
    assert "infra/litellm-hub.md" not in got and "wiki/vaultwarden-backup.md" in got


def test_credit_is_bounded_and_never_displaces_ordinary_leaders(identities):
    identities(hosts=["SVC1"], profiles=profiles())
    ordinary = [
        page(f"wiki/svc1-note-{i}.md", f"# SVC1 note {i}\n\nSVC1 handles job {i} for the services.\n", f"svc1-note-{i}")
        for i in range(8)
    ]
    facts = rank([FORGEJO, *ordinary], "What does SVC1 serve?")
    # The eight ordinary leaders keep the cap selection; the host's service page is
    # added after them, never in place of one.
    assert sources(facts[:8]) == {f"wiki/svc1-note-{i}.md" for i in range(8)}
    assert sources(facts[8:]) == {"infra/forgejo.md"}


def test_host_only_question_detection(identities):
    identities(hosts=["SVC1", "App1"])
    for q in ("What does SVC1 serve?", "Which services run on SVC1?"):
        assert resolver._asks_only_about_host(q)
    for q in ("Which Docker containers run on App1?", "How do I restore the Vaultwarden backup on App1?"):
        assert not resolver._asks_only_about_host(q)


# An "... on <host>" phrasing captures the host as a platform constraint. A statement
# on a page in that host's profile (its own pages, its services' pages) satisfies it; a page
# about another host is still filtered by the constraint.
import pytest  # noqa: E402
from test_resolver_selection import Repo  # noqa: E402


@pytest.fixture
def offline(monkeypatch):
    async def query(*args, **kwargs):
        return [], "offline"

    async def docs(*args, **kwargs):
        return {"error": "offline"}

    monkeypatch.setattr(resolver, "query_vo_rag", query)
    monkeypatch.setattr(resolver, "query_context7", docs)
    monkeypatch.setattr(resolver, "_safe_index_lag", lambda repo: [])
    monkeypatch.setattr(resolver, "stale_observation_flags", lambda repo: [])


ON_HOST_PHRASINGS = ("What runs on SVC1?", "Which services run on SVC1?", "What is hosted on SVC1?")


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ON_HOST_PHRASINGS)
async def test_on_host_phrasing_delivers_the_hosts_service_page(tmp_path, identities, offline, question):
    identities(hosts=["SVC1", "App1"], profiles=profiles())
    answer = await resolver.resolve(question, repo=Repo(tmp_path, [FORGEJO, FLEET, UBUNTU, LITELLM]), live=False)
    assert {"dimension": "platform", "value": "SVC1"} in answer.scope_constraints
    assert "infra/forgejo.md" in sources(answer.git_facts)


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ON_HOST_PHRASINGS)
async def test_on_host_phrasing_still_filters_another_hosts_pages(tmp_path, identities, offline, question):
    identities(hosts=["SVC1", "App1"], profiles=profiles())
    other = page(
        "wiki/ubuntu-services.md",
        "# App1 services\n\nApp1 runs the services that are run and hosted for the inference stack.\n",
        "ubuntu-services",
    )
    answer = await resolver.resolve(question, repo=Repo(tmp_path, [FORGEJO, FLEET, UBUNTU, LITELLM, other]), live=False)
    got = sources(answer.git_facts)
    assert "infra/forgejo.md" in got
    assert not got & {"wiki/ubuntu-services.md", "wiki/ubuntu-inference.md", "infra/litellm-hub.md"}
    # The platform constraint itself (not only later filters) rejects the other host's page.
    scoped = resolver.Answer(question, None)
    resolver._append_page_facts(scoped, other, question)
    assert scoped.git_facts == []


def test_host_profile_page_with_an_explicit_other_platform_scope_stays_filtered(identities):
    identities(hosts=["SVC1", "App1"], profiles=profiles())
    moved = page(
        "infra/forgejo.md",
        "# Forgejo (git host)\n\n## On App1\n\nForgejo runs the git remote for the repositories.\n",
        "forgejo",
    )
    answer = resolver.Answer("What runs on SVC1?", None)
    resolver._append_page_facts(answer, moved, "What runs on SVC1?")
    assert answer.git_facts == []


# The relaxation is for statements about the asked host. A statement on a profile
# page that names another known host (or its alias, address or page) keeps the constraint.
MULTI_HOST_FORGEJO = page(
    "infra/forgejo.md",
    "# Forgejo\n\nThe Forgejo Actions runner is installed on App1 and runs the CI jobs.\n\n"
    "Forgejo is not installed on ws1.\n\nA second Forgejo instance runs on prod for the mirror.\n",
    "forgejo",
)
SVC1_FLEET = page(
    "infra/fleet-hosts.md", "# Fleet\n\n- **SVC1** is the services host.\n- **ws1** is the edge host.\n", "fleet-hosts"
)


@pytest.mark.asyncio
async def test_profile_page_statements_naming_another_host_are_not_delivered(tmp_path, identities, offline):
    identities(hosts=["SVC1", "App1", "ws1", "prod"], profiles=profiles())
    answer = await resolver.resolve(
        "Is Forgejo installed on SVC1?", repo=Repo(tmp_path, [MULTI_HOST_FORGEJO, OWN, SVC1_FLEET]), live=False
    )
    assert "infra/forgejo.md" not in sources(answer.git_facts)
    assert any("automatic coverage unresolved" in unknown for unknown in answer.unknowns)


def test_profile_page_statement_naming_another_hosts_alias_address_or_page_keeps_the_constraint(identities):
    identities(
        hosts=["SVC1", "App1"],
        groups=[{"App1", "jammy", "10.0.0.7"}, {"SVC1", "fw"}],
        profiles=(
            *profiles(),
            applicability.HostProfile(frozenset({"app1", "jammy", "10007"}), frozenset({"infra/build-box"})),
        ),
    )
    texts = (
        "Forgejo runs its runner on jammy.",
        "Forgejo runs a runner at 10.0.0.7.",
        "Forgejo runs a runner, see [[infra/build-box]].",
    )
    for text in texts:
        answer = resolver.Answer("What runs on SVC1?", None)
        resolver._append_page_facts(
            answer, page("infra/forgejo.md", f"# Forgejo\n\n{text}\n", "forgejo"), "What runs on SVC1?"
        )
        assert answer.git_facts == [], text
    # The asked host's own aliases do not count as another host.
    answer = resolver.Answer("What runs on SVC1?", None)
    resolver._append_page_facts(
        answer,
        page("infra/forgejo.md", "# Forgejo\n\nForgejo runs the git remote on fw.\n", "forgejo"),
        "What runs on SVC1?",
    )
    assert answer.git_facts


# (1) Another host named by a DNS-qualified name is that host; (2) a question that
# names a host only by its address captures that host, as its canonical name does.
from doclayer import coverage  # noqa: E402

FLEET_WITH_ADDRESSES = (
    "# Fleet\n\n- **SVC1** — `192.0.2.40` services host.\n- **App1** — `198.51.100.7` inference host.\n"
)


def address_groups():
    """Equivalence groups the way identities build them from the fleet page's address lines."""
    return [{name, ip} for name, ip in coverage.fleet_host_addresses(FLEET_WITH_ADDRESSES)]


def address_of(host):
    return dict(coverage.fleet_host_addresses(FLEET_WITH_ADDRESSES))[host]


@pytest.mark.parametrize(
    "text",
    [
        "Forgejo runs its runner on jammy.lan.",
        "Forgejo runs a runner on app1.lan.",
        "Forgejo runs a runner at user@ci1.home.arpa.",
    ],
)
def test_profile_page_statement_naming_another_host_by_qualified_name_keeps_the_constraint(identities, text):
    identities(
        hosts=["SVC1", "App1", "jammy", "ci1", "fw"],
        groups=[{"App1", "jammy"}, {"SVC1", "fw"}, {"ci1"}],
        profiles=profiles(),
    )
    answer = resolver.Answer("What runs on SVC1?", None)
    resolver._append_page_facts(
        answer, page("infra/forgejo.md", f"# Forgejo\n\n{text}\n", "forgejo"), "What runs on SVC1?"
    )
    assert answer.git_facts == []


def test_profile_page_statement_naming_the_asked_host_by_qualified_name_keeps_the_relaxation(identities):
    identities(hosts=["SVC1", "App1", "fw"], groups=[{"SVC1", "fw"}], profiles=profiles())
    answer = resolver.Answer("What runs on SVC1?", None)
    resolver._append_page_facts(
        answer,
        page("infra/forgejo.md", "# Forgejo\n\nForgejo runs the git remote on fw.lan.\n", "forgejo"),
        "What runs on SVC1?",
    )
    assert answer.git_facts


def test_question_naming_a_host_only_by_address_captures_that_host(identities):
    identities(hosts=["SVC1", "App1"], groups=address_groups(), profiles=profiles())
    ip = address_of("SVC1")
    assert applicability.question_hosts(f"Is Forgejo installed on {ip}?") >= {"svc1"}
    assert applicability.question_host_pages(f"What runs on {ip}?") == applicability.question_host_pages(
        "What runs on SVC1?"
    )
    assert resolver._asks_only_about_host(f"What runs on {ip}?")


@pytest.mark.asyncio
async def test_address_only_question_delivers_the_hosts_service_page_and_filters_other_hosts(
    tmp_path, identities, offline
):
    identities(hosts=["SVC1", "App1"], groups=address_groups(), profiles=profiles())
    ip = address_of("SVC1")
    forgejo = page(
        "infra/forgejo.md",
        "# Forgejo (git host)\n\nForgejo is installed as the git remote for the repositories.\n\n"
        "The Forgejo Actions runner is installed on App1.\n",
        "forgejo",
    )
    answer = await resolver.resolve(
        f"Is Forgejo installed on {ip}?", repo=Repo(tmp_path, [forgejo, OWN, FLEET]), live=False
    )
    values = [f["value"] for f in answer.git_facts if f["source"].startswith("infra/forgejo.md")]
    assert any("git remote" in value for value in values)
    assert not any("App1" in value for value in values)


# The name, alias and address forms of a host question are one question, and a
# host-only "runs on" question delivers the host's service page like "serve" does, even when
# ordinary pages that mention the host fill the cap selection.
UBUNTU_FLEET = "# Fleet\n\n- **App1** — `198.51.100.7` inference host.\n- **SVC1** — `192.0.2.40` services host.\n"
UBUNTU_NOTES = [
    page(f"wiki/app1-note-{i}.md", f"# App1 note {i}\n\nApp1 runs job {i} for the services.\n", f"app1-note-{i}")
    for i in range(10)
]
LITELLM_MULTI = page(
    "infra/litellm-hub.md",
    "# LiteLLM hub\n\nThe hub routes model traffic and keeps usage logs.\n\nA standby copy of the hub runs on SVC1.\n",
    "litellm-hub",
)


def ubuntu_identities(identities):
    groups = [{name, ip} for name, ip in coverage.fleet_host_addresses(UBUNTU_FLEET)]
    groups[0] |= {"jammy"}
    identities(hosts=["SVC1", "App1", "jammy"], groups=groups, profiles=profiles())
    return dict(coverage.fleet_host_addresses(UBUNTU_FLEET))["App1"]


@pytest.mark.asyncio
async def test_name_alias_and_address_forms_deliver_the_same_pages(tmp_path, identities, offline):
    ip = ubuntu_identities(identities)
    repo = Repo(tmp_path, [LITELLM_MULTI, UBUNTU, FORGEJO, *UBUNTU_NOTES])
    delivered = {}
    for spelling in ("App1", "jammy", ip):
        question = f"What runs on {spelling}?"
        answer = await resolver.resolve(question, repo=repo, live=False)
        assert answer.question == question  # the answer keeps the asked wording
        delivered[spelling] = [(f["source"], f["value"]) for f in answer.git_facts]
    assert delivered["App1"] == delivered["jammy"] == delivered[ip]
    got = sources(answer.git_facts)
    assert "infra/litellm-hub.md" in got and "infra/forgejo.md" not in got
    values = [value for source, value in delivered[ip] if source.startswith("infra/litellm-hub.md")]
    assert values and not any("SVC1" in value for value in values)  # names another host: still excluded


@pytest.mark.asyncio
async def test_runs_on_agrees_with_serve_for_the_hosts_service_page(tmp_path, identities, offline):
    ubuntu_identities(identities)
    repo = Repo(tmp_path, [LITELLM, UBUNTU, *UBUNTU_NOTES])
    for question in ("What runs on App1?", "What does App1 serve?"):
        answer = await resolver.resolve(question, repo=repo, live=False)
        assert "infra/litellm-hub.md" in sources(answer.git_facts), question


# A host question is never rewritten. The asked token resolves to its host's
# equivalence group, and constraint matching, ranking and service-page selection use the group.
SVC1_ADDRESS = "192.0.2.40"
GPU2_ADDRESS = "198.51.100.9"
FORGEJO_SERVICE = page(
    "infra/forgejo.md",
    "# Forgejo (git host)\n\n**Host:** git.example.test, co-located on the services host.\n\n"
    "**Auth:** git credential store on each host.\n",
    "forgejo",
)
ACLI = page(
    "wiki/atlassian-cli-svc1.md",
    "# Atlassian CLI on SVC1\n\nSVC1 has the Atlassian CLI installed and authenticated.\n\n"
    "The CLI is installed from the Debian package on SVC1.\n",
    "atlassian-cli-svc1",
)
SVC1_FLEET = page(
    "infra/fleet-hosts.md", "# Fleet\n\n- **SVC1** is the services host; co-located with Forgejo.\n", "fleet-hosts"
)


def svc1_identities(identities):
    profile = applicability.HostProfile(
        frozenset({"svc1", "fw", SVC1_ADDRESS, "1920240"}),
        frozenset(),
        frozenset({"forgejo"}),
        frozenset({"infra/forgejo"}),
        frozenset({("forgejo", "infra/forgejo")}),
    )
    return identities(
        hosts=["SVC1", "fw", "App1"],
        groups=[{"SVC1", "fw", SVC1_ADDRESS}],
        profiles=(profile,),
        aliases=["fw"],
        declared=[["SVC1"], ["App1"]],
    )


def delivered(answer):
    return [(f["source"], f["value"]) for f in answer.git_facts]


@pytest.mark.asyncio
async def test_name_alias_address_and_qualified_forms_deliver_identical_pages(tmp_path, identities, offline):
    svc1_identities(identities)
    repo = Repo(tmp_path, [FORGEJO_SERVICE, ACLI, SVC1_FLEET, UBUNTU])
    for template in ("Is Forgejo installed on {}?", "What runs on {}?"):
        results = {}
        for spelling in ("SVC1", "fw", SVC1_ADDRESS, "fw.lan"):
            question = template.format(spelling)
            answer = await resolver.resolve(question, repo=repo, live=False)
            # Never rewritten: the question and its scope keep the asked spelling.
            assert answer.question == question
            assert {"dimension": "platform", "value": spelling} in answer.scope_constraints
            results[spelling] = delivered(answer)
        assert results["SVC1"] == results["fw"] == results[SVC1_ADDRESS] == results["fw.lan"], template
        assert "infra/forgejo.md" in sources(answer.git_facts), template


def test_an_address_shared_by_two_declared_hosts_is_ambiguous(tmp_path, identities, monkeypatch):
    from doclayer import coverage

    monkeypatch.setattr(
        coverage,
        "ssh_host_groups",
        lambda path: [
            frozenset({"WS3", "ws", GPU2_ADDRESS}),
            frozenset({"gpu2", GPU2_ADDRESS}),
            frozenset({"SVC1", SVC1_ADDRESS}),
            frozenset({"fw", SVC1_ADDRESS}),
        ],
    )
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra/fleet-hosts.md").write_text(
        "# Fleet\n\n- **SVC1** services host.\n- **WS3** workstation.\n", encoding="utf-8"
    )
    keys = {"ws3", "ws", "gpu2", "svc1", "fw"}
    declared = applicability._declared_hosts(tmp_path, [{"gpu2"}, {"svc1"}], keys)  # noqa: SLF001
    groups = applicability._host_groups(tmp_path, [{"gpu2"}, {"svc1"}], declared)  # noqa: SLF001
    # fw is an ssh-only name for SVC1's address: one machine. WS3 (fleet page) and gpu2
    # (catalog) are distinct declared hosts: their shared address names neither, while
    # WS3's own other name stays its alias.
    assert groups == {frozenset({"svc1", "fw", "1920240", SVC1_ADDRESS}), frozenset({"ws3", "ws"})}
    identities(hosts=["WS3", "gpu2", "SVC1", "fw"], groups=[set(g) for g in groups], aliases=["fw", "ws"])
    assert applicability.question_hosts("Does WS3 have root disk pressure?") == {"ws3", "ws"}
    assert applicability.question_hosts(f"What runs on {GPU2_ADDRESS}?") == set()


@pytest.mark.asyncio
async def test_stale_shared_address_keeps_the_ws3_answer(tmp_path, identities, offline):
    identities(
        hosts=["WS3", "gpu2"], groups=[{"WS3", GPU2_ADDRESS}, {"gpu2", GPU2_ADDRESS}], declared=[["WS3"], ["gpu2"]]
    )
    fleet = page(
        "infra/fleet-hosts.md",
        "# Fleet\n\n- **WS3** workstation-class host; flagged previously for root disk pressure.\n",
        "fleet-hosts",
    )
    curator = page(
        "wiki/curator-config.md",
        "# Curator config\n\nThe curator model runs on gpu2 with a root disk of 2 TB.\n",
        "curator-config",
    )
    question = "Does WS3 have root disk pressure?"
    assert applicability.question_hosts(question) == {"ws3"}  # never gpu2's group
    answer = await resolver.resolve(question, repo=Repo(tmp_path, [fleet, curator]), live=False)
    assert answer.question == question
    assert "infra/fleet-hosts.md" in sources(answer.git_facts)


@pytest.mark.parametrize(
    "question",
    [
        "Which hosts are on 192.0.2.40/24?",
        "Is 1.192.0.2.40 reachable?",
        "Is 92.0.2.40 reachable?",
        "Upgrade notes for 192.0.24.0",
        "Upgrade notes for 1920.2.40",
        "Release 19.20.2.40 notes",
        "Release v192.0.2.40-beta notes",
    ],
)
def test_only_the_exact_address_token_names_the_host(identities, question):
    svc1_identities(identities)
    assert applicability.question_hosts(question) == set()


@pytest.mark.parametrize(
    "question",
    [f"What runs on {SVC1_ADDRESS}?", f"Is ssh open on {SVC1_ADDRESS}:22?", f"Reach user@{SVC1_ADDRESS} now"],
)
def test_the_exact_address_names_its_host(identities, question):
    svc1_identities(identities)
    assert "svc1" in applicability.question_hosts(question)


# A short alias (fw) is a host token anywhere; an ordinary-word alias still needs
# host position.
@pytest.mark.parametrize(
    "question,named",
    [
        ("Is the edge cache enabled in Cloudflare?", False),
        ("Is jammy the Ubuntu 22.04 codename?", False),
        ("What is the MacBook Pro used for?", False),
        ("Is Forgejo installed on fw?", True),
        ("What does fw serve?", True),
        ("fw runs Forgejo, right?", True),
        ("What is fw?", True),
        ("Copy the file to edge", True),
        ("Does the MacBook Pro have Docker?", True),
    ],
)
def test_an_alias_names_its_host_only_in_host_position(identities, question, named):
    identities(
        hosts=["SVC1", "fw", "ws1", "edge", "App1", "jammy", "macbook", "macbook pro"],
        groups=[{"SVC1", "fw"}, {"ws1", "edge"}, {"App1", "jammy"}, {"macbook", "macbook pro", "203.0.113.5"}],
        aliases=["fw", "edge", "jammy", "macbook", "macbook pro"],
        declared=[["SVC1"], ["ws1"], ["App1"]],
    )
    assert bool(applicability.question_hosts(question)) is named


@pytest.mark.asyncio
async def test_forgejo_on_svc1_delivers_the_forgejo_page_in_every_form(tmp_path, identities, offline):
    svc1_identities(identities)
    repo = Repo(tmp_path, [FORGEJO_SERVICE, ACLI, SVC1_FLEET])
    for question in (
        "Is Forgejo installed on SVC1?",
        "Is Forgejo installed on fw?",
        "What runs on SVC1?",
        "What does SVC1 serve?",
    ):
        answer = await resolver.resolve(question, repo=repo, live=False)
        assert "infra/forgejo.md" in sources(answer.git_facts), question


@pytest.mark.asyncio
async def test_service_page_addition_stays_on_host_only_questions_in_every_form(tmp_path, identities, offline):
    ip = ubuntu_identities(identities)
    repo = Repo(tmp_path, [LITELLM, UBUNTU, BACKUP, *UBUNTU_NOTES])
    for spelling in ("App1", ip):
        assert "infra/litellm-hub.md" in sources(
            (await resolver.resolve(f"What runs on {spelling}?", repo=repo, live=False)).git_facts
        )
        subject = await resolver.resolve(
            f"How do I restore the Vaultwarden backup on {spelling}?", repo=repo, live=False
        )
        assert "infra/litellm-hub.md" not in sources(subject.git_facts)


# Alias-only hosts and two-word host names.
DELL_ADDRESS = "198.51.100.76"
DELL_CAPTURE = page(
    "wiki/laptop-1-android-capture-access.md",
    "# Android capture\n\n`adb` runs on laptop-1 for the phone capture.\n",
    "Android capture",
)
DELL_PARITY = page(
    "wiki/laptop-1-parity-rectification.md",
    "# Parity\n\nThe parity check runs between the Laptop 1 and the GPU box.\n",
    "Parity",
)
DELL_BY_ADDRESS = page(
    "wiki/capture-notes.md", f"# Capture notes\n\nThe capture service runs on {DELL_ADDRESS}.\n", "Capture notes"
)
DELL_BY_LINK = page(
    "wiki/phone-notes.md", "# Phone notes\n\nThe phone sync runs on [[infra/workstation-laptop]].\n", "Phone notes"
)
RUN_NOTES = [
    page(f"wiki/runs-{i}.md", f"# Runs {i}\n\nJob {i} runs on the scheduler and runs nightly.\n", f"runs-{i}")
    for i in range(6)
]
GPU_FLEET = page(
    "infra/fleet-hosts.md",
    "# Fleet\n\n- **App1** has the RTX 3090 GPU.\n- **Gpu1** has the RTX 3070 GPU.\n",
    "fleet-hosts",
)
DELL_GPU = page("wiki/laptop-1-gpu.md", "# Dell GPU\n\nThe laptop-1 GPU is an RTX A1000.\n", "Dell GPU")


def dell_identities(identities):
    """A host whose every name is an alias (no catalog entry or fleet bullet)."""
    profile = applicability.HostProfile(frozenset({"laptop1", DELL_ADDRESS}), frozenset({"infra/workstation-laptop"}))
    return identities(
        hosts=["laptop 1", "App1", "Gpu1"],
        groups=[{"laptop-1", "laptop 1", DELL_ADDRESS}],
        profiles=(profile,),
        aliases=["laptop 1"],
        declared=[["App1"], ["Gpu1"]],
    )


@pytest.mark.parametrize(
    "text",
    [
        "`adb` runs on laptop-1.",
        "The parity check ran on the Laptop 1.",
        f"The capture service runs on {DELL_ADDRESS}.",
        "The phone sync runs on [[infra/workstation-laptop]].",
    ],
)
def test_any_name_in_the_group_earns_the_hosts_ranking_term(identities, text):
    dell_identities(identities)
    question = "What runs on the Laptop 1?"
    assert resolver._host_terms(text, question) == set(resolver._host_markers(question)[0])


@pytest.mark.asyncio
async def test_alias_only_host_delivers_its_pages_and_no_other_hosts_gpu(tmp_path, identities, offline):
    dell_identities(identities)
    repo = Repo(tmp_path, [DELL_CAPTURE, DELL_PARITY, DELL_BY_ADDRESS, DELL_BY_LINK, *RUN_NOTES, GPU_FLEET, DELL_GPU])
    runs = sources((await resolver.resolve("What runs on the Laptop 1?", repo=repo, live=False)).git_facts)
    assert {
        "wiki/laptop-1-android-capture-access.md",
        "wiki/laptop-1-parity-rectification.md",
        "wiki/capture-notes.md",
        "wiki/phone-notes.md",
    } <= runs
    gpu = (await resolver.resolve("What GPU does the Laptop 1 have?", repo=repo, live=False)).git_facts
    assert "wiki/laptop-1-gpu.md" in sources(gpu)
    assert not any("3090" in f["value"] or "3070" in f["value"] for f in gpu)


def test_a_two_word_host_name_keeps_the_weight_of_its_two_words(identities):
    dell_identities(identities)
    assert resolver._host_term_words(applicability.host_group("Laptop 1")) == 2
    pages = [
        page("wiki/laptop-1-runtime.md", "# Runtime\n\nThe verify script runs on laptop-1.\n", "Runtime"),
        page("wiki/laptop-1-capture.md", "# Capture\n\n`adb` is not on the PATH on laptop-1.\n", "Capture"),
        *[
            page(f"wiki/dell-note-{i}.md", f"# Note {i}\n\nThe laptop-1 keeps log {i}.\n", f"dell-note-{i}")
            for i in range(2)
        ],
    ]
    # As "dell" and "7680" did as two terms, the host's one term lets a statement that
    # names the host and where it is clear the cut beside the best "runs on" statement.
    assert "wiki/laptop-1-capture.md" in sources(rank(pages, "What runs on the Laptop 1?"))


FW_PHRASINGS = [
    "Where does {} store backups?",
    "{} disk usage",
    "Is {} reachable?",
    "Is {} up?",
    "How full is {}?",
    "Did {} reboot last night?",
    "Where are the {} tokens stored?",
]


@pytest.mark.parametrize("template", FW_PHRASINGS)
def test_a_short_or_digit_alias_is_the_host_anywhere(identities, template):
    identities(
        hosts=["SVC1", "fw", "ws2", "cvps3", "mini g2"],
        groups=[{"SVC1", "fw"}, {"ws2", "192.0.2.12"}, {"cvps3", "192.0.2.13"}, {"mini-g2", "192.0.2.14"}],
        aliases=["fw", "ws2", "cvps3", "mini g2"],
        declared=[["SVC1"]],
    )
    assert (
        applicability.question_hosts(template.format("fw"))
        == applicability.question_hosts(template.format("SVC1"))
        >= {"svc1", "fw"}
    )
    for alias, key in (("ws2", "ws2"), ("cvps3", "cvps3"), ("mini-g2", "minig2")):
        assert key in applicability.question_hosts(template.format(alias))
    assert applicability.question_hosts(template.format("jhx")) == set()  # a whole token only


@pytest.mark.asyncio
async def test_fw_questions_give_the_svc1_pages(tmp_path, identities, offline):
    svc1_identities(identities)
    backup = page(
        "wiki/svc1-backups.md",
        "# SVC1 backups\n\nSVC1 stores backups under /srv/backup and is reachable over ssh.\n",
        "svc1-backups",
    )
    repo = Repo(tmp_path, [backup, ACLI, SVC1_FLEET, FORGEJO_SERVICE])
    for template in FW_PHRASINGS:
        by_alias = await resolver.resolve(template.format("fw"), repo=repo, live=False)
        by_name = await resolver.resolve(template.format("SVC1"), repo=repo, live=False)
        assert delivered(by_alias) == delivered(by_name), template


@pytest.mark.parametrize(
    "question,named",
    [
        ("Is jammy the Ubuntu 22.04 codename?", False),
        ("Is the edge cache enabled?", False),
        ("What is the MacBook Pro used for?", False),
        ("Which proxmox release is current?", False),
        ("Is Forgejo installed on jammy?", True),
        ("What runs on proxmox?", True),
        ("edge runs Caddy", True),
    ],
)
def test_an_ordinary_word_alias_still_needs_host_position(identities, question, named):
    identities(
        hosts=["App1", "jammy", "ws1", "edge", "macbook", "macbook pro", "proxmox"],
        groups=[
            {"App1", "jammy"},
            {"ws1", "edge"},
            {"macbook", "macbook pro", "203.0.113.5"},
            {"proxmox", "203.0.113.6"},
        ],
        aliases=["jammy", "edge", "macbook", "macbook pro", "proxmox"],
        declared=[["App1"], ["ws1"]],
    )
    assert bool(applicability.question_hosts(question)) is named


@pytest.mark.parametrize(
    "question,named",
    [
        ("Where are the tokens in the edge vault?", False),
        ("What is configured on the edge proxy?", False),
        ("What is installed on the edge?", True),
        ("Is Caddy running on the edge for TLS?", True),
        ("Which files are in the edge, and why?", True),
        ("What runs on the edge now", False),
        ("What is on edge", True),
    ],
)
def test_on_the_alias_counts_only_when_it_ends_the_noun_phrase(identities, question, named):
    identities(hosts=["ws1", "edge"], groups=[{"ws1", "edge"}], aliases=["edge"], declared=[["ws1"]])
    assert bool(applicability.question_hosts(question)) is named


@pytest.mark.asyncio
async def test_every_spelling_of_a_host_question_gives_identical_answers(tmp_path, identities, offline):
    svc1_identities(identities)
    repo = Repo(tmp_path, [FORGEJO_SERVICE, ACLI, SVC1_FLEET, UBUNTU, *RUN_NOTES])
    for template in (
        "Is Forgejo installed on {}?",
        "What runs on {}?",
        "What does {} serve?",
        "What's on {}?",
        "Is Docker running on {}",
    ):
        answers = [
            delivered(await resolver.resolve(template.format(spelling), repo=repo, live=False))
            for spelling in ("SVC1", "fw", SVC1_ADDRESS, "fw.lan")
        ]
        assert all(answer == answers[0] for answer in answers), template


def test_a_wikilink_earns_a_host_only_when_one_host_owns_the_page(identities, monkeypatch, tmp_path):
    # Profiles come from _host_profiles on a vault where SVC1 has its own
    # infra page, so one host yields both its single-key profile and its group profile.
    from types import SimpleNamespace

    from doclayer import coverage

    def entry(slug, aliases=()):
        return coverage.SubjectEntry(slug=slug, kind="host", title=slug, aliases=list(aliases), pages=[])

    (tmp_path / "infra").mkdir()
    (tmp_path / "infra/services-box.md").write_text(
        "---\ntitle: Services box\ntags:\n  - svc1\n---\n# Services box\n", encoding="utf-8"
    )
    (tmp_path / "infra/rack-a.md").write_text(
        "---\ntitle: Rack A\ntags:\n  - svc1\n  - app1\n---\n# Rack A\n", encoding="utf-8"
    )
    catalog = SimpleNamespace(subjects={e.slug: e for e in [entry("SVC1", ["fw"]), entry("App1")]})
    groups = applicability._merge_groups([applicability._group_keys({"SVC1", "fw", SVC1_ADDRESS})])  # noqa: SLF001
    profiles = applicability._host_profiles(tmp_path, catalog, groups)  # noqa: SLF001
    owners = [sorted(p.keys) for p in profiles if "infra/services-box" in p.pages]
    assert len(owners) == 2  # one host, two profiles: the case 3c miscounted
    identities(
        hosts=["SVC1", "fw", "App1"],
        groups=[{"SVC1", "fw", SVC1_ADDRESS}],
        profiles=profiles,
        declared=[["SVC1"], ["App1"]],
    )
    question = "What runs on SVC1?"
    # One host owns services-box: the link earns SVC1's term.
    assert resolver._host_terms("Plex runs on the media box; see [[infra/services-box]].", question) == set(
        resolver._host_markers(question)[0]
    )
    # Two distinct hosts own rack-a: the link earns nothing.
    assert resolver._host_terms("Plex runs on the media box; see [[infra/rack-a]].", question) == set()


@pytest.mark.parametrize(
    "question",
    [
        "| runs in the edge |",
        "Is it on the edge - the old box?",
        "Is it on the edge (ws1)?",
        "[runs in the edge]",
        "Is it on the edge\u2014not the origin?",
        'He said "on the edge" twice',
        "It's 'on the edge' now",
        "It is \u201con the edge\u201d now",
        "It is \u2018on the edge\u2019 now",
    ],
)
def test_table_bracket_dash_quote_and_paren_end_the_noun_phrase(identities, question):
    identities(hosts=["ws1", "edge"], groups=[{"ws1", "edge"}], aliases=["edge"], declared=[["ws1"]])
    assert applicability.question_hosts(question) >= {"ws1", "edge"}


@pytest.mark.parametrize(
    "text,earns",
    [
        ("for H in App1 SVC1; do ssh $H uptime; done", {"app1", "svc1"}),
        ("Hosts: worker-1 ws2 App1 all run the agent.", {"worker1", "ws2", "app1"}),
        ("The HP WS2 Worker 1 runs the build agent.", set()),
        ("The Dell PowerEdge SVC1 runs Forgejo.", set()),
        ("The Lenovo M920q App1 box runs Plex.", set()),
    ],
)
def test_a_host_list_is_not_a_model_phrase(identities, text, earns):
    identities(
        hosts=["worker-1", "ws2", "SVC1", "App1"],
        groups=[
            {"worker-1", "worker 1", "192.0.2.9"},
            {"ws2", "192.0.2.12"},
            {"SVC1", "192.0.2.40"},
            {"App1", "192.0.2.28"},
        ],
        aliases=["ws2"],
        declared=[["worker-1"], ["SVC1"], ["App1"]],
    )
    earned = {
        key
        for host, key in (("worker-1", "worker1"), ("ws2", "ws2"), ("SVC1", "svc1"), ("App1", "app1"))
        if resolver._host_terms(text, f"What runs on {host}?")
    }
    assert earned == earns


@pytest.mark.parametrize(
    "question,named",
    [
        ("What is configured on the edge/proxy pair?", False),
        ("Which files sit in the edge/cache folder?", False),
        ("What is configured on the edge?", True),
        ("What is configured on the edge.", True),
        ("Is it on the edge, really?", True),
        ("Is it on the edge; and why?", True),
        ("Is it on the edge: yes?", True),
        ("Is it on the edge!", True),
        ("(is it on the edge)", True),
    ],
)
def test_a_slash_compound_does_not_end_the_noun_phrase(identities, question, named):
    identities(hosts=["ws1", "edge"], groups=[{"ws1", "edge"}], aliases=["edge"], declared=[["ws1"]])
    assert bool(applicability.question_hosts(question)) is named


def models_identities(identities):
    return identities(
        hosts=["worker-1", "ws2", "laptop 1"],
        groups=[{"worker-1", "worker 1", "192.0.2.9"}, {"ws2", "192.0.2.12"}, {"laptop-1", "laptop 1", "192.0.2.76"}],
        aliases=["ws2", "laptop 1"],
        declared=[["worker-1"]],
    )


@pytest.mark.parametrize(
    "text,earns",
    [
        ("The HP WS2 Worker 1 runs the build agent.", set()),
        ("worker-1 runs the Agent-runtime runtime.", {"worker1"}),
        ("Agent-runtime is deployed on worker-1.", {"worker1"}),
        ("ws2 runs the backups.", {"ws2"}),
        ("The Laptop 1 runs the capture.", {"laptop1"}),
        ("The HP Elite Mini 800 G9 Desktop PC is the worker-1 box.", {"worker1"}),
        ("The HP WS2 SFF G5 Workstation runs Plex.", set()),
    ],
)
def test_a_host_name_inside_a_hardware_model_phrase_earns_nothing(identities, text, earns):
    models_identities(identities)
    earned = set()
    for host, key in (("worker-1", "worker1"), ("ws2", "ws2"), ("the Laptop 1", "laptop1")):
        if resolver._host_terms(text, f"What runs on {host}?"):
            earned.add(key)
    assert earned == earns


def test_neighbouring_tags_are_not_a_model_phrase(identities):
    models_identities(identities)
    question = "What runs on the Laptop 1?"
    tagged = page(
        "wiki/example-app-hosts.md",
        "# Example-app hosts\n\nThe repository runs the dataset checks.\n",
        "Example-app hosts",
        tags=["example-app", "ws2", "laptop-1"],
    )
    answer = resolver.Answer(question, None)
    resolver._append_page_facts(answer, tagged, question)
    assert answer.git_facts and all("host:" in fact["_context"] for fact in answer.git_facts)
