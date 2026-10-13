"""Questions whose single words ("state", "job", "agent", "call", "host", "server")
also appear in headings of landed pages must not pull in those sections.

Fixtures are shaped on real vault pages.
"""

from __future__ import annotations

from test_resolver_answer_sections import BITWARDEN, _rank
from test_resolver_selection import page


def sources(facts):
    return {f["source"].split("@", 1)[0] for f in facts}


def text(facts):
    return " ".join(f["value"] for f in facts)


RESPONSES = page(
    "wiki/openai-responses-api.md",
    """# OpenAI Responses API

## Migrating from Chat Completions

The Responses API replaces Chat Completions; migration moves messages to input items and tools to the Responses tool format.

Chat Completions function-calling schemas migrate to Responses function tools with the same JSON schema.

## State Management

Set `store: true` to keep response objects so later turns can reference previous_response_id.

Stored responses are retained for thirty days.

## Mid-turn steering, async results, Multi-agent and browser session state

Steering messages are accepted while a turn runs but applied at the next tool boundary.

Async tool results arrive as separate items after the turn started.

Multi-agent beta models share one browser session state across subagents.

## API Conversation vs Application Runtime

The API conversation and the application runtime keep separate state; the API never restores browser sessions.

## Compaction

Compaction restrictions apply to long running responses.
""",
    "OpenAI Responses API",
)
LINEUP = page(
    "wiki/openai-model-lineup.md",
    """# OpenAI model lineup

## API Endpoints

The API endpoints for each model are listed per family.

## Models

GPT-6 Astra is the flagship model.
""",
    "OpenAI model lineup",
)


def test_responses_migration_question_excludes_state_and_steering_sections():
    facts = _rank([RESPONSES, LINEUP], "current state of the Responses API migration from Chat Completions")
    body = text(facts)
    assert "replaces Chat Completions" in body
    for irrelevant in (
        "store: true",
        "Steering messages",
        "Async tool results",
        "Multi-agent beta models",
        "API conversation and the application runtime",
        "API endpoints for each model",
    ):
        assert irrelevant not in body


CURATOR = page(
    "wiki/brain-curator-import-curation-publication.md",
    """# Brain curator import curation publication

## How imports become published knowledge

After a job validates, the curator publishes the curated page to the brain through the Forgejo admin-bypass route.

Verify a publication by checking the merged commit on main and the job's publication evidence.

## Installed repository and job/state storage paths

| Item | Path |
|---|---|
| Credential env file | `/home/user/.config/brain-curator/credentials.env` |
| Forgejo token file | `/home/user/.config/brain-curator/forgejo-token` |
| Job store | `/home/user/.local/state/brain-curator/jobs` |

## Job Pipeline and States

A job moves through intake, curation, validation and publication; its state is recorded in the job store.

## Job Submission and Idempotency

Submitting the same job twice returns the existing job id.

## Budgets

The curator limits model calls per import.

## Recovery

The curator resumes an interrupted import from its last stage.

## Review

The operator may review curator digests after publication.

## Owner

The curator worker runs on worker1 as a systemd user service.

## Inference

The curator uses the local Qwen service for curation.

## Validation

The curator validates each import before publication.
""",
    "Brain curator import curation publication",
)


def test_curator_publication_question_excludes_storage_paths_and_job_plumbing():
    facts = _rank([CURATOR], "verify the brain curator publication after a job")
    body = text(facts)
    assert "publishes the curated page" in body or "Verify a publication" in body
    for irrelevant in ("Credential env file", "Forgejo token file", "Job store", "returns the existing job id"):
        assert irrelevant not in body


TOOLS = page(
    "wiki/openai-api-tools.md",
    """# OpenAI API tools

## Built-in tools

The OpenAI API provides built-in tools: web search, file search, code interpreter and computer use.

Tools are enabled per request in the Responses API tools array.

## Multi-Agent (Beta)

Multi-agent beta lets one model delegate tasks to sub-models inside a single response.

Delegated workers inherit the tools array of the parent.

## Waiting between tool calls

To add a wait between OpenAI API tool calls, sleep in your own loop before sending the next tool call.

A wait between tool calls does not extend the OpenAI API request timeout.

## Programmatic Tool Calling

Programmatic tool calling lets code loop over many tools.

## Function Calling

Function calling lets the model call functions; each tool call carries JSON arguments.

## API Conversation vs Application Runtime

The API conversation and the application runtime keep separate state.
""",
    "OpenAI API tools",
)


def test_agents_sdk_question_excludes_multi_agent_beta_sections(identities):
    identities(subjects=["OpenAI API tools", "OpenAI Responses API", "OpenAI model lineup"])
    facts = _rank([TOOLS, RESPONSES], "OpenAI API tools available to the Agents SDK")
    body = text(facts)
    assert "built-in tools" in body
    for irrelevant in (
        "Delegated workers inherit",
        "Steering messages",
        "API conversation and the application runtime",
    ):
        assert irrelevant not in body


def test_wait_between_tool_calls_question_excludes_unrelated_call_sections(identities):
    identities(subjects=["OpenAI API tools", "OpenAI Responses API", "OpenAI model lineup"])
    facts = _rank([TOOLS], "add a wait between OpenAI API tool calls")
    body = text(facts)
    for irrelevant in (
        "Function calling lets the model",
        "Programmatic tool calling lets",
        "API conversation and the application runtime",
    ):
        assert irrelevant not in body


FLEET = page(
    "infra/fleet-hosts.md",
    """# Fleet hosts

## Forgejo

Forgejo listens on port 3000 on the SVC1 and is hosted there behind the reverse proxy.

The Forgejo listen port 3000 is published on the SVC1 only.

## Hosts and roles

| Host | Address | Role |
|---|---|---|
| App1 | 192.0.2.228:22 | app host |
| worker1 | 192.0.2.160:22 | Agent runtime host |
""",
    "Fleet hosts",
)


def test_forgejo_question_excludes_other_hosts_inventory_rows():
    facts = _rank([FLEET], "What port does Forgejo listen on and where is it hosted?")
    body = text(facts)
    assert "port 3000" in body
    assert "192.0.2.228" not in body and "192.0.2.160" not in body


WS1 = page(
    "wiki/ws1-desktop-runtime-and-crash-observations.md",
    """# ws1 desktop runtime and crash observations

## Desktop session

ws1 runs the GNOME desktop under Wayland.

## Crash observations

The compositor crashed once on 2026-10-04.
""",
    "ws1 desktop runtime and crash observations",
)


def test_ws1_display_server_question_excludes_bitwarden_configured_server():
    bitwarden = page(
        BITWARDEN.path,
        BITWARDEN.body.replace(
            "Sam's workstation ws1 has three Bitwarden clients installed (CLI, desktop app, browser extension).",
            "ws1 has Bitwarden clients for its desktop environment installed (CLI, desktop app, browser extension).",
        ).replace(
            "The bw CLI is configured for `https://vault.example.org`, the Vaultwarden server instance.",
            "The bw CLI on ws1 is configured for `https://vault.example.org`, the Vaultwarden instance.",
        ),
        BITWARDEN.title,
    )
    facts = _rank([WS1, bitwarden], "What desktop environment and display server does ws1 run?")
    body = text(facts)
    assert "GNOME" in body
    assert "vault.example.org" not in body


def test_filler_stripped_api_capture_is_not_a_hard_platform_filter(identities):
    identities(subjects=["OpenAI API tools", "OpenAI Responses API", "OpenAI model lineup"])
    # "Status of the Responses API ..." used to yield platform "Status of Responses",
    # which the page never states, and hid the page holding the answer.
    facts = _rank([RESPONSES, LINEUP], "Status of the Responses API steering events")
    assert "wiki/openai-responses-api.md" in sources(facts)
    assert "Steering messages" in text(facts)


def test_deploy_question_with_host_returns_facts_not_a_fragment_filter(identities):
    identities(hosts=["App1"])
    vo_rag = page(
        "wiki/vo-rag-stack.md",
        """# vo-rag stack

## Runtime layout

The vo-rag production deployment runs on App1 as a docker compose stack.
""",
        "vo-rag stack",
        tags=["app-1"],
    )
    facts = _rank([vo_rag], "How do I deploy the vo-rag production deployment on App1?")
    assert "docker compose stack" in text(facts)
