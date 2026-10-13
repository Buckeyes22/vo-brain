---
schema_version: 1
privacy: internal
source_origin: external
source_lineage:
- source:https://developers.openai.com/api/docs/guides/migrate-to-responses
- source:https://developers.openai.com/api/docs/api-reference/responses
- source:https://developers.openai.com/api/docs/guides/steering
- source:https://developers.openai.com/api/docs/guides/responses-multi-agent
- source:https://developers.openai.com/api/docs/guides/compaction
- source:https://developers.openai.com/api/docs/guides/deployment-checklist
- source:https://developers.openai.com/api/docs/libraries
- source:https://developers.openai.com/api/docs/guides/tools-shell
- source:https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra
- source:https://developers.openai.com/api/docs/guides/async-tool-calling
- source:https://developers.openai.com/api/docs/guides/tools-computer-use
- source:https://developers.openai.com/api/docs/guides/tools-computer-use-integration
- source:https://developers.openai.com/api/docs/guides/conversation-state
- source:https://developers.openai.com/api/docs/guides/text
- source:https://developers.openai.com/api/docs/guides/tools-code-interpreter
- source:https://developers.openai.com/api/reference/resources/responses/methods/create
tags:
- openai
- api
- responses-api
- gpt-6
- migration
updated: '2026-10-07'
title: OpenAI Responses API
concept_id: c-6ea20404
subject_type: product
---

# OpenAI Responses API

The Responses API is OpenAI's flagship API and the recommended starting point for accessing the newest model behavior, built-in tools, stateful workflows, and agent features [Source](<https://developers.openai.com/api/docs/guides/deployment-checklist>). It is recommended for all new projects, while the Chat Completions API remains supported [Source](<https://developers.openai.com/api/docs/guides/migrate-to-responses>).

## Prompting and Skills

For Codex and coding agents using GPT-6 Astra, overly specific guidance in skills can hinder results because the model has improved at understanding nuance and ambiguity, whereas such guidance previously helped [Source](<https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra>). Repository skills guide other contributors' agents which may use different models, so guidance that helps models like Sol or Luna may overconstrain GPT-6 Astra [Source](<https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra>). 

For Codex, skills are essentially prompts stored as Markdown files that can be packaged with resources and bundled scripts, generally useful for guidance around specific workflows or apps [Source](<https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra>). Skill names and descriptions are loaded into the model's context so it knows when to use them, but adding too many skills causes Codex to shorten descriptions, making it harder for the model to pick the correct skill [Source](<https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra>). Skill descriptions should be as short as possible while clearly indicating when the model should use them, to avoid pushing the model to use the skill for any related task rather than specific triggers [Source](<https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra>). A key marker of a useful skill is progressive disclosure; for skills with multiple workflows, the root document should be a minimal router pointing to supporting docs and scripts to avoid forcing the model to read irrelevant content [Source](<https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra>). 

AGENTS.md applies whenever the model works in the repository, so each instruction should be frequently revisited to determine if it is still needed [Source](<https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra>).

## Core Architecture

The Responses API uses **Items** as the basic unit of model context, which are distinct types (e.g., `message`, `function_call`), whereas Chat Completions uses **Messages** which glue multiple concerns into one object [Source](<https://developers.openai.com/api/docs/guides/migrate-to-responses>). The API omits the `n` parameter for multiple parallel generations, returning only one generation per request [Source](<https://developers.openai.com/api/docs/guides/migrate-to-responses>).

### Endpoints

- **Create**: `POST /responses` [Source](<https://developers.openai.com/api/docs/api-reference/responses>)
- **Retrieve**: `GET /responses/{response_id}` [Source](<https://developers.openai.com/api/docs/api-reference/responses>)
- **Delete**: `DELETE /responses/{response_id}` [Source](<https://developers.openai.com/api/docs/api-reference/responses>)
- **Cancel**: `POST /responses/{response_id}/cancel` [Source](<https://developers.openai.com/api/docs/api-reference/responses>)
- **Compact**: `POST /responses/compact` [Source](<https://developers.openai.com/api/docs/api-reference/responses>)

### State Management

Setting `store: true` maintains state from turn to turn, preserving reasoning and tool context [Source](<https://developers.openai.com/api/docs/guides/migrate-to-responses>). Responses are stored by default in the Responses API, while Chat Completions are stored by default only for new accounts; storage can be disabled in both by setting `store: false` [Source](<https://developers.openai.com/api/docs/guides/migrate-to-responses>).

### API Conversation vs Application Runtime

Continuing with `previous_response_id` preserves API conversation context but does not restore browser sessions, login state, or runtime variables. Browser and desktop execution integrations must persist their own session; Python variable persistence is part of the computer-use runtime contract, not a guarantee from a Responses ID [Source](<https://developers.openai.com/api/docs/guides/tools-computer-use>). Files and memory belong to their execution container, not to the conversation ID: for hosted Code Interpreter specifically, the container expires after 20 minutes of inactivity, discarding associated files and memory; application-owned runtimes have their own lifetimes [Source](<https://developers.openai.com/api/docs/guides/tools-code-interpreter>).

**What `previous_response_id` carries.** Passing the previous response ID chains responses across turns without a persistent conversation object [source: https://developers.openai.com/api/docs/guides/conversation-state @2026-10-07]. All previous input tokens for responses in the chain are billed as input tokens [source: https://developers.openai.com/api/docs/guides/conversation-state @2026-10-07]. For reasoning models, preserve every item in the response's `output` array: the Responses API returns encrypted reasoning items by default, and replaying the complete output keeps reasoning items and assistant `phase` values intact [source: https://developers.openai.com/api/docs/guides/conversation-state @2026-10-07]. In WebSocket mode, with `stream_id` each lane can retain its latest response while `previous_response_id` still controls lineage [source: https://developers.openai.com/api/docs/guides/conversation-state @2026-10-07].

**Prior instructions do not carry over.** The `instructions` parameter only applies to the current response generation request; when conversation state is managed with `previous_response_id`, the `instructions` used on previous turns will not be present in the context, so they must be re-specified when still needed [source: https://developers.openai.com/api/docs/guides/text @2026-10-07].

**API conversation history storage.** Response objects are saved for 30 days by default [source: https://developers.openai.com/api/docs/guides/conversation-state @2026-10-07]. The Conversations API works with the Responses API to persist conversation state as a long-running object with its own durable identifier that can be reused across sessions, devices, or jobs; conversations store items (messages, tool calls, tool outputs, and other data), and conversation objects and their items are not subject to the 30 day TTL [source: https://developers.openai.com/api/docs/guides/conversation-state @2026-10-07]. In a create request, the `conversation` parameter names the conversation the response belongs to, and items from that conversation are prepended to `input_items` for the request [source: https://developers.openai.com/api/reference/resources/responses/methods/create @2026-10-07]. Retention of these objects under ZDR is covered in [[openai-api-data-retention-controls]].

**Who owns runtime state.** For computer use, the `execute_in_sandbox` helper must preserve the browser or desktop session, enforce execution limits, and apply the application's permission rules; for production the guide advises setting step, time, or cost limits and supporting cancellation [source: https://developers.openai.com/api/docs/guides/tools-computer-use @2026-10-07]. The runtime contract for Python variable persistence noted above is the guide's `exec_py` tool definition [source: https://developers.openai.com/api/docs/guides/tools-computer-use @2026-10-07].

For hosted Code Interpreter, using an expired container in `v1/responses` will fail; a snapshot of the container's metadata remains visible at expiry but all associated data is discarded and not recoverable, and an expired container cannot be moved back to an active state [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter @2026-10-07]. Files that are needed should be downloaded from the container while it is active [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter @2026-10-07]. In `auto` mode the API creates a new container or reuses an active container used by a previous `code_interpreter_call` item; an explicit container is created through the containers endpoint and its ID assigned as the container value [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter @2026-10-07]. The selected `memory_limit` (`1g` default, `4g`, `16g`, or `64g`) applies for the entire life of the container, and higher tiers are billed at the built-in tools rates for Code Interpreter [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter @2026-10-07].

Unknown as of 2026-10-07: https://developers.openai.com/api/docs/guides/conversation-state does not state whether tool invocation or runtime environment state persists across turns of a `previous_response_id` chain.

Steering over the Responses API WebSocket is restricted to single-agent responses on models and execution modes that support steering; responses bound to a conversation or using automatic compaction do not support steering [Source](<https://developers.openai.com/api/reference/resources/responses/websocket-events>). The `response.steer` event accepts only `type`, `previous_response_id`, and `input`; do not send `stream_id`—the target response determines the WebSocket lane [Source](<https://developers.openai.com/api/docs/guides/steering>). A `response.steer.accepted` event acknowledges server ownership of the queued input, not that it has been applied; the successor's `response.created` event is the commit point. Input that cannot be committed is returned in `response.steer.failed` [Source](<https://developers.openai.com/api/reference/resources/responses/websocket-events>).

When the response stops for client-owned tool output or approval, accepted steering input remains queued and `response.steer.pending` is emitted after `response.completed`. Fill the `required_input` stubs with saved tool results or approval decisions and send one explicit `response.create` per parent with the same `previous_response_id` and WebSocket lane; do not rerun tools or resend accepted steering input [Source](<https://developers.openai.com/api/reference/resources/responses/websocket-events>).

`response.steer.failed` is emitted when steering input is rejected or cannot be committed to a successor response. It returns the original uncommitted input so the client can carry it into `response.create` when appropriate. Failures after acceptance include the same steering ID; failures before an ID is allocated omit `steer.id`. A lost connection or missing acknowledgement leaves the outcome unknown and is not proof that the input was rejected. Documented error codes include `successor_creation_failed` (the server could not start the next response; resend the steering input via `response.create`) and `response_not_found` (target response not available on this connection) [Source](<https://developers.openai.com/api/reference/resources/responses/websocket-events>).
## Built-in Tools

The Responses API includes built-in tools for web search, file search, computer use, code interpreter, and remote MCPs [Source](<https://developers.openai.com/api/docs/guides/migrate-to-responses>). The model can call multiple tools (web_search, image_generation, file_search, code_interpreter, remote MCPs, custom functions) within a single API request [Source](<https://developers.openai.com/api/docs/guides/migrate-to-responses>).

### Tool Choice

The `tool_choice` parameter supports specific types to force the model to use built-in tools, including `file_search`, `web_search_preview`, `computer`, `computer_use_preview`, `computer_use`, `code_interpreter`, and `image_generation` [Source](<https://developers.openai.com/api/docs/api-reference/responses>). It also supports `function` (requiring `name`), `mcp` (requiring `server_label` and optionally `name`), and `custom` (requiring `name`) types [Source](<https://developers.openai.com/api/docs/api-reference/responses>).

### Shell Tool

The shell tool is available through the Responses API but not the Chat Completions API [Source](<https://developers.openai.com/api/docs/guides/tools-shell>). It supports two execution modes: hosted shell containers managed by OpenAI and a local shell runtime hosted by the user [Source](<https://developers.openai.com/api/docs/guides/tools-shell>).

- **Hosted Runtime**: Based on Debian 12 with a default working directory of `/mnt/data` [Source](<https://developers.openai.com/api/docs/guides/tools-shell>). Preinstalled versions include Python 3.11, Node.js 22.16, Java 17.0, PHP 8.2, Ruby 3.1, and Go 1.23 [Source](<https://developers.openai.com/api/docs/guides/tools-shell>). Hosted environments do not support interactive TTY sessions and commands do not run with `sudo` [Source](<https://developers.openai.com/api/docs/guides/tools-shell>).
- **Network Access**: Hosted containers do not have outbound network access by default; enabling it requires an org-level allow list and an explicit `network_policy` in the request [Source](<https://developers.openai.com/api/docs/guides/tools-shell>).
- **Container Reuse**: Containers can be created via the `/v1/containers` endpoint and referenced in subsequent calls using `container_reference` and a `container_id` [Source](<https://developers.openai.com/api/docs/guides/tools-shell>).

## Advanced Features

### Server-side Compaction

Server-side compaction is enabled by setting `context_management` with `compact_threshold` in a Responses create request [Source](<https://developers.openai.com/api/docs/guides/compaction>). It triggers automatically when the rendered token count crosses the configured threshold [Source](<https://developers.openai.com/api/docs/guides/compaction>). The compaction item is opaque and carries forward key prior state and reasoning into the next run using fewer tokens [Source](<https://developers.openai.com/api/docs/guides/compaction>).

- **Stateless Chaining**: For stateless input-array chaining, output items (including compaction items) should be appended to the next input array; items before the most recent compaction item can be dropped to reduce request size [Source](<https://developers.openai.com/api/docs/guides/compaction>).
- **Stateful Chaining**: For `previous_response_id` chaining, only the new user message should be passed each turn, and manual pruning of context should not be performed [Source](<https://developers.openai.com/api/docs/guides/compaction>).
- **Standalone Endpoint**: The `POST /responses/compact` endpoint is fully stateless and ZDR-friendly, returning a new compacted context window for the next `/responses` call [Source](<https://developers.openai.com/api/docs/guides/compaction>). The output should not be pruned; it is the canonical next context window [Source](<https://developers.openai.com/api/docs/guides/compaction>).

### Multi-Agent (Beta)

Multi-agent is a beta feature available with GPT-6.1 Sol and all GPT-5.6 models [Source](<https://developers.openai.com/api/docs/guides/responses-multi-agent>). To enable it, set `multi_agent.enabled` to `true` and pass `responses_multi_agent=v1` in the `betas` argument (SDK) or `OpenAI-Beta` header (raw HTTP/WebSocket) [Source](<https://developers.openai.com/api/docs/guides/responses-multi-agent>).

- **Architecture**: The root agent is named `/root` and spawned subagents use hierarchical paths (e.g., `/root/researcher`) [Source](<https://developers.openai.com/api/docs/guides/responses-multi-agent>). There is no fixed limit on tree depth or the total number of subagents created [Source](<https://developers.openai.com/api/docs/guides/responses-multi-agent>).
- **Concurrency**: The `max_concurrent_subagents` parameter defaults to 3 and limits the number of active subagent turns across the entire tree, excluding the root agent [Source](<https://developers.openai.com/api/docs/guides/responses-multi-agent>).
- **Actions**: The API provides six hosted collaboration actions: `spawn_agent`, `send_message`, `followup_task`, `wait_agent`, `interrupt_agent`, and `list_agents` [Source](<https://developers.openai.com/api/docs/guides/responses-multi-agent>). Applications should not execute `multi_agent_call` items or submit outputs for them; the API executes these hosted actions [Source](<https://developers.openai.com/api/docs/guides/responses-multi-agent>).
- **WebSocket**: WebSocket is recommended over HTTP for tool-heavy or long-running Multi-agent workflows because it allows injecting function outputs as they become available, reducing latency [Source](<https://developers.openai.com/api/docs/guides/responses-multi-agent>). In WebSocket mode, function outputs are injected using a `response.inject` event [Source](<https://developers.openai.com/api/docs/guides/responses-multi-agent>).

### Mid-Turn Steering

Mid-turn steering is available with the GPT-6 model family over a WebSocket connection to the Responses API [Source](<https://developers.openai.com/api/docs/guides/steering>). GPT-5.6 and earlier models do not support mid-turn steering [Source](<https://developers.openai.com/api/docs/guides/steering>). Steering is supported only for single-agent responses on models and execution modes that support steering; responses bound to a conversation or using automatic compaction do not support steering [Source](<https://developers.openai.com/api/reference/resources/responses/websocket-events>).

- **Mechanism**: To send a steering message, use `response.steer` on the same connection after receiving `response.created`, using the response's ID as `previous_response_id` [Source](<https://developers.openai.com/api/docs/guides/steering>). The event accepts only `type`, `previous_response_id`, and `input` fields [Source](<https://developers.openai.com/api/docs/guides/steering>). Input accepts only `user`-role messages; each message may contain only `type`, `role`, and `content` fields, with `type` as `"message"` and content as a string or array of `input_text`, `input_image`, `input_file` parts. Other roles, tool outputs, and item types are not supported [Source](<https://developers.openai.com/api/reference/resources/responses/websocket-events>).
- **Behavior**: Steering does not rewrite output already sent, undo earlier actions, or cancel tools that have already started [Source](<https://developers.openai.com/api/docs/guides/steering>). Acceptance of a steering message means the input is queued, not that the model has acted on it [Source](<https://developers.openai.com/api/docs/guides/steering>). If steering interrupts the original response, it ends with `response.incomplete` and `incomplete_details.reason: "steered"`, followed automatically by a successor `response.created`; normal completion can also trigger an automatic successor that inherits the previous response's settings and continues with the queued input [Source](<https://developers.openai.com/api/reference/resources/responses/websocket-events>).
- **Pending Input**: If a response needs a client tool result or approval, the API keeps the steering queued and emits `response.steer.pending` after `response.completed`, with a `required_input` field [Source](<https://developers.openai.com/api/docs/guides/steering>). When `reason` is `waiting_for_required_input`, copy the stubs, fill result fields using ordinary `response.create` input schemas, and submit one continuation per parent with the same `previous_response_id` and WebSocket lane [Source](<https://developers.openai.com/api/reference/resources/responses/websocket-events>). An explicit `response.create` for tool results uses its own tools, instructions, and other settings (not inherited from the original); the server implicitly prepends the accepted steer input, so the client need not wait for `response.steer.pending` before returning results [Source](<https://developers.openai.com/api/docs/guides/steering>).

#### Failure Handling

`response.steer.failed` means the API did not apply the input through steering and will not apply it automatically later. The event returns the original `input` and `previous_response_id` under `steer` with an `error` object describing the failure. Track accepted submissions by `steer.id`; a later failure reuses the same ID [Source](<https://developers.openai.com/api/docs/guides/steering>).

Known error codes: `response_not_found` (target response not available on this connection), `invalid_input` (event or input failed validation), `steering_not_supported` (model or response execution mode does not support steering), `too_many_pending_steers` (too much steering input pending), `response_already_completed` (response completed, no longer accepting steering), `response_not_active` (response no longer accepting steering), `successor_creation_failed` (successor response could not be created). Clients should handle unknown values because additional codes may be introduced [Source](<https://developers.openai.com/api/reference/resources/responses/websocket-events>).
## Mid-turn steering, async results, Multi-agent and browser session state (GPT-6)

**1. Steering accepted vs. applied.** The client sends `response.steer` on the same connection after receiving the response's `response.created` event. Accepting a steering message means the input is queued; it does not mean the model has acted on it [source: https://developers.openai.com/api/docs/guides/steering fetched 2026-10-05]. Once accepted (`response.steer.accepted`), the API automatically creates a new response with the update, and the client keeps reading events rather than sending another `response.create` [source: https://developers.openai.com/api/docs/guides/steering fetched 2026-10-05]. If the response needs a tool result or approval from the application first, the API sends `response.steer.pending` with a `required_input` field; the client returns that input with `response.create` on the same connection, setting `previous_response_id` to the original response ID, and does not repeat the accepted steering, which the server prepends before processing the returned input [source: https://developers.openai.com/api/docs/guides/steering fetched 2026-10-05].

**2. Async results.** Async tool calling is supported by GPT-6 Astra and later models and applies only to function and custom tools that the application executes, not to hosted built-in tools [source: https://developers.openai.com/api/docs/guides/async-tool-calling fetched 2026-10-05]. When the job finishes, the application includes its output in a later Responses request with `previous_response_id`, matched by the original `call_id`, as a `function_call_output` item (function calls) or `custom_tool_call_output` item (custom tools) [source: https://developers.openai.com/api/docs/guides/async-tool-calling fetched 2026-10-05]. The application still runs the tool; OpenAI does not manage the background job. Async tools are not configured for programmatic tool calling and are not combined with parallel tool calls in multi-agent scenarios [source: https://developers.openai.com/api/docs/guides/async-tool-calling fetched 2026-10-05].

**3. Multi-agent (beta) supported models.** Multi-agent is a beta feature available with GPT-6.1 Sol and all GPT-5.6 models, enabled with `responses_multi_agent=v1` in `betas` (HTTP) or the `OpenAI-Beta: responses_multi_agent=v1` header (WebSocket) [source: https://developers.openai.com/api/docs/guides/responses-multi-agent fetched 2026-10-05].

**4. Compaction restrictions.** `/responses/compact` is not supported when Multi-agent is enabled; instead automatic server-side compaction is enabled implicitly and applied separately to each agent's context, and an explicit `context_management.compact_threshold` overrides it. `reasoning.summary` and `max_tool_calls` are also unsupported with Multi-agent [source: https://developers.openai.com/api/docs/guides/responses-multi-agent fetched 2026-10-05]. Outside Multi-agent, the window sent to `/responses/compact` must fit the model's context window, `/responses/compact` output must not be pruned, and with `previous_response_id` chaining items must not be manually pruned [source: https://developers.openai.com/api/docs/guides/compaction fetched 2026-10-05].

**5. Application browser session state.** Continuing a response does not restore a browser session, login state, or runtime variables; the API conversation and the execution environment have separate state, so the application keeps the browser or desktop session alive between calls [source: https://developers.openai.com/api/docs/guides/tools-computer-use fetched 2026-10-05]. The application's environment executes the requested actions and captures screenshots, and the same browser or desktop session is kept available throughout the task [source: https://developers.openai.com/api/docs/guides/tools-computer-use-integration fetched 2026-10-05]. Cookie persistence and login session lifetime: unknown as of 2026-10-05 (checked https://developers.openai.com/api/docs/guides/tools-computer-use-integration, fetched 2026-10-05).

## Migration from Chat Completions

### Key Differences

- **Performance**: Using reasoning models like GPT-5 with the Responses API results in a 3% improvement in SWE-bench compared to Chat Completions with the same prompt and setup [Source](<https://developers.openai.com/api/docs/guides/migrate-to-responses>).
- **Cost**: The Responses API achieves 40% to 80% lower costs compared to Chat Completions in internal tests due to improved cache utilization [Source](<https://developers.openai.com/api/docs/guides/migrate-to-responses>).
- **Tool Calling**: GPT-6 Astra and GPT-6.1 Sol require the Responses API for tool calling. GPT-6 Sol and GPT-6 Luna support function calling in Chat Completions only when `reasoning_effort` is "none" [Source](<https://developers.openai.com/api/docs/guides/deployment-checklist>). Starting with GPT-5.4, the Chat Completions API does not support tool calling with `reasoning_effort` values other than `none` [Source](<https://developers.openai.com/api/docs/guides/migrate-to-responses>).

### Reasoning Effort Configuration

- **Supported Levels**: GPT-6 Astra and GPT-6.1 Sol support `reasoning.effort` levels low, medium, high, xhigh, and max, but do not support "none". GPT-6 Sol and GPT-6 Luna also support "none" [Source](<https://developers.openai.com/api/docs/guides/deployment-checklist>).
- **Parameter Constraints**: When reasoning effort is not "none", parameters `temperature`, `top_p`, and `top_logprobs` must be removed from requests, as must `logprobs` from Chat Completions and `message.output_text.logprobs` from the Responses include array [Source](<https://developers.openai.com/api/docs/guides/deployment-checklist>).
- **Mid-Conversation Changes**: To change reasoning effort mid-conversation for GPT-6 models in standard single-agent mode, add a `configuration_update` input item before the next user message while leaving the request-level `reasoning.effort` unchanged to preserve prompt prefix caching eligibility [Source](<https://developers.openai.com/api/docs/guides/deployment-checklist>).
- **Pro Mode**: For the hardest quality-first workloads, `reasoning.mode: "pro"` can be compared with standard mode at the same effort; pro mode increases latency and token usage but may improve reliability [Source](<https://developers.openai.com/api/docs/guides/deployment-checklist>).

## SDKs and Access

The OpenAI API can be accessed via official SDKs, the OpenAI CLI, the Agents SDK, or custom HTTP clients [Source](<https://developers.openai.com/api/docs/libraries>).

- **Python**: Installed via pip [Source](<https://developers.openai.com/api/docs/libraries>).
- **JavaScript/TypeScript**: Supports server-side environments including Node.js, Deno, and Bun [Source](<https://developers.openai.com/api/docs/libraries>).
- **.NET**: Officially supported API client for C# in collaboration with Microsoft, installable via NuGet [Source](<https://developers.openai.com/api/docs/libraries>). Note: The .NET SDK does not provide a Responses WebSocket client [Source](<https://developers.openai.com/api/docs/guides/steering>).
- **Java**: Currently in beta, available as a Maven dependency [Source](<https://developers.openai.com/api/docs/libraries>).
- **Go**: Currently in beta, imported from `github.com/openai/openai-go/v3` [Source](<https://developers.openai.com/api/docs/libraries>).
- **CLI**: A generated command-line tool installable via Homebrew [Source](<https://developers.openai.com/api/docs/libraries>).
- **Agents SDK**: Intended for code-first orchestration of agents, tools, handoffs, guardrails, tracing, or sandbox execution [Source](<https://developers.openai.com/api/docs/libraries>).

Authentication is handled by configuring SDKs to automatically read the API key from the system environment variable `OPENAI_API_KEY` [Source](<https://developers.openai.com/api/docs/libraries>).

## Related Pages

- [[gpt-6-astra]]
- [[openai-api-tools]]