---
schema_version: 1
privacy: internal
source_origin: external
source_lineage:
- source:https://developers.openai.com/api/docs/guides/async-tool-calling
- source:https://developers.openai.com/api/docs/guides/tools-computer-use
- source:https://developers.openai.com/api/docs/guides/structured-outputs
- source:https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling
- source:https://developers.openai.com/api/docs/guides/tools
- source:https://developers.openai.com/api/docs/guides/tools-shell
- source:https://developers.openai.com/api/docs/guides/tools-code-interpreter
tags:
- openai
- api
- tools
- gpt-6-astra
- responses-api
- code-interpreter
- programmatic-tool-calling
- async-tool-calling
updated: '2026-10-06'
title: OpenAI API Tools
concept_id: c-19add7a0
subject_type: product
---

# OpenAI API Tools

The OpenAI platform supports built-in tools, function calling, Programmatic Tool Calling, tool search, and remote MCP servers to extend model capabilities [source: https://developers.openai.com/api/docs/guides/tools]. Available tools include Function calling, Web search, Remote MCP servers, Skills, Shell, Computer use, Image generation, File search, Tool search, and Programmatic Tool Calling [source: https://developers.openai.com/api/docs/guides/tools]. Tools can be configured on Responses API requests, Agents API agents, or in Agents SDK definitions [source: https://developers.openai.com/api/docs/guides/tools]. For broader API context, see [[openai-responses-api]].

## Context Management

For the GPT-6 family, the practical guide recommends cutting context the task doesn't need while keeping the evidence it does, and, where your application supports it, running independent tasks together so one slow step doesn't hold up unrelated work. For longer conversations, compaction reduces context size while preserving the state needed to continue [source: https://openai.com/index/practical-guide-building-gpt-6/ @ 2026-10-04]. See [[openai-api-tools#Async Tool Calling]] for patterns supporting parallel independent work.

## Mid-turn Steering

Mid-turn steering in the API lets a developer send a correction through the Responses WebSocket API while the model is working; updates are queued and do not cancel running tools or undo completed actions [source: https://openai.com/index/practical-guide-building-gpt-6/ @ 2026-10-04]. In Codex with GPT-6 Astra, the model can ask for clarification while it works; resolve questions affecting the next step and specify which independent work can continue while deciding. Steering the active task with new information—explaining what should change and what should stay the same—avoids spending time on an approach that no longer meets needs [source: https://openai.com/index/practical-guide-building-gpt-6/ @ 2026-10-04]. See [[openai-api-tools#Async Tool Calling]] for keeping independent work moving alongside slower tasks.

## Model Selection

The GPT-6 family comprises three models with distinct pricing tiers (per 1M tokens, input/output/cached-input): GPT-6 Astra ($10.00/$50.00/$1.00), GPT-6.1 Sol ($2.00/$10.00/$0.10), and GPT-6 Luna ($0.10/$0.50/$0.01) [source: https://openai.com/index/practical-guide-building-gpt-6/ @ 2026-10-04].

Model-to-workload matching: GPT-6 Astra targets the hardest reasoning work requiring maximum intelligence; GPT-6.1 Sol suits complex coding, research, and computer use; GPT-6 Luna handles focused tasks at scale and everyday repeated work with a clear goal (e.g., extracting invoice fields, classifying requests, structured summaries) [source: https://openai.com/index/practical-guide-building-gpt-6/ @ 2026-10-04].

Reasoning effort in the API is set per request: Low (routine tasks, fact extraction, small edits), Medium (judgment work like planning or comparing options), High (difficult debugging, deeper analysis, careful review), or Extra high/Max (test where supported when High falls short; keep only if improvement justifies added time and cost). Reasoning effort can be changed mid-conversation without breaking cache [source: https://openai.com/index/practical-guide-building-gpt-6/ @ 2026-10-04].

GPT-6.1 Sol supports multi-agent workflows in the Responses API (beta), where it can assign independent work to subagents and combine their findings into a final response [source: https://openai.com/index/practical-guide-building-gpt-6/ @ 2026-10-04]. See [[openai-responses-api]] for API details.

## Prompt Caching

Prompt caching reduces cached input token cost by up to 95% compared to uncached input tokens, depending on the model. To maximize cache reuse, place stable instructions and reference material before changing task details, and keep tool definitions consistent across requests [source: https://openai.com/index/practical-guide-building-gpt-6/ @ 2026-10-04]. The caching dashboard and diagnostics guide help identify where cache reuse breaks down; include cache writes and long-context rates when estimating workflow cost [source: https://openai.com/index/practical-guide-building-gpt-6/ @ 2026-10-04].

## Speed Modes

Fast mode in the API provides faster, more consistent response times at a higher per-token cost than Standard processing, suitable for chat apps or coding tools. Ultrafast speeds up token generation independently of reasoning effort and is available for GPT-6 Astra; it is usable in both Codex and the API for rapid coding iterations [source: https://openai.com/index/practical-guide-building-gpt-6/ @ 2026-10-04].
## Tool Configuration

### Function Calling
Function calling tools require a `name`, `description`, `parameters` (JSON schema), and `strict` flag [source: https://developers.openai.com/api/docs/guides/tools]. The `tool_choice` parameter can be set in the API request to explicitly control or guide tool usage behavior [source: https://developers.openai.com/api/docs/guides/tools].

### Built-in Tools
- **Web Search**: Configured by adding `{ type: "web_search" }` to the `tools` array in a Responses API request [source: https://developers.openai.com/api/docs/guides/tools].
- **File Search**: Requires a `vector_store_ids` parameter to specify which vector stores to search [source: https://developers.openai.com/api/docs/guides/tools].
- **Remote MCP Servers**: Configured with `type: "mcp"`, `server_label`, `server_description`, `server_url`, and `require_approval` [source: https://developers.openai.com/api/docs/guides/tools].
- **Tool Search**: Allows loading deferred tools at runtime using namespaces and the `tool_search` tool type. Only `gpt-5.4` and later models support the `tool_search` feature [source: https://developers.openai.com/api/docs/guides/tools].

## Code Interpreter

The Code Interpreter tool allows models to write and run Python code in a sandboxed environment to solve complex problems in domains like data analysis, coding, and math [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter]. It is used for processing files with diverse data and formatting, generating files with data and images of graphs, and writing and running code iteratively to solve problems [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter]. The tool boosts visual intelligence in reasoning models like o3 and o4-mini by allowing the model to crop, zoom, rotate, and otherwise process and transform images [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter].

### Prompting Guidance
While the tool is called Code Interpreter, the model knows it as the 'python tool', and the most explicit way to invoke it in prompts is to ask for 'the python tool' [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter].

### Containers
The Code Interpreter tool requires a container object, which is a fully sandboxed virtual machine that the model can run Python code in [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter].

- **Auto Mode**: Enabled by passing `"container": { "type": "auto", "memory_limit": "4g" }` in the tool configuration, which automatically creates a new container or reuses an active one from the model's context [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter].
- **Explicit Mode**: Involves creating a container via the `v1/containers` endpoint and assigning its `id` as the `container` value in the tool configuration [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter].
- **Memory Limits**: Can be set to `1g` (default), `4g`, `16g`, or `64g`, with higher tiers offering more RAM and billed at built-in tools rates [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter].
- **Expiration**: Containers expire if not used for 20 minutes, at which point using the container in `v1/responses` will fail and all associated data is discarded and not recoverable. A container cannot be moved from an expired state to an active one; a new container must be created and files re-uploaded, with any state in the old container's memory lost [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter].

### Usage Notes
The Code Interpreter tool is available via the Responses, Chat Completions, and Assistants APIs, with a rate limit of 100 RPM per org [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter]. Files and images generated by the model are returned as `container_file_citation` annotations on the assistant’s message, including `container_id`, `file_id`, and `filename` [source: https://developers.openai.com/api/docs/guides/tools-code-interpreter].

## Programmatic Tool Calling

Programmatic Tool Calling allows a model to write and run JavaScript that coordinates tools, supporting parallel calls, loops, and conditions within a hosted runtime [source: https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling]. In the Responses API, the application decides which tools are eligible for direct, programmatic, or both calling modes, while the Agents API enables Programmatic Tool Calling by default [source: https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling].

## Async Tool Calling

Async tool calling allows the model to continue working after issuing a tool call without waiting for the result, enabling early start of slow lookups and answering independent parts of a request [source: https://developers.openai.com/api/docs/guides/async-tool-calling]. It is supported by GPT-6 Astra and later models, applies to function and custom tools run by the application (not hosted built-in tools), and should not be combined with parallel tool calls in Multi-agent mode [source: https://developers.openai.com/api/docs/guides/async-tool-calling].

### How Async Tools Work
To enable async tool calling, set `async: true` on a function or custom tool definition; the application still executes the tool, and async tools do not move execution to OpenAI or manage background jobs [source: https://developers.openai.com/api/docs/guides/async-tool-calling]. Async tool calling differs from Background mode: Background mode runs response generation asynchronously, while async tool calling lets the model continue independent work while the application runs a slower task (e.g., tests); the app returns the result when ready, and dependent work should wait for that result before starting [source: https://developers.openai.com/api/docs/guides/async-tool-calling; https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04]. Tool results are matched to their calls using the original API `call_id`; function tools use `function_call`/`function_call_output` items, and custom tools use `custom_tool_call`/`custom_tool_call_output` items [source: https://developers.openai.com/api/docs/guides/async-tool-calling].

### Calling an Async Tool
The response to an async tool call can contain both the async call and an answer to independent parts of the request; if other conversation turns occur before the job finishes, update `latest_response_id` to continue from the latest response while keeping the original tool `call_id` [source: https://developers.openai.com/api/docs/guides/async-tool-calling]. For streaming responses, to achieve earlier dispatch, start the application's job when the complete call item arrives while continuing to consume the response [source: https://developers.openai.com/api/docs/guides/async-tool-calling].

### Adding a Wait Tool
A wait tool (e.g., `wait_for_tasks`) can be added as an ordinary synchronous function to let the model choose when it needs pending results; it is not a built-in Responses tool, and its schema and behavior belong to the application [source: https://developers.openai.com/api/docs/guides/async-tool-calling]. When using a wait tool, add a `task_handle` argument to each async tool; the model assigns a handle to each call, and the application binds it to the original API `call_id` and the running job. Handles must be unique throughout the conversation, including completed tasks [source: https://developers.openai.com/api/docs/guides/async-tool-calling]. When delivering results for a wait tool, resolve the requested handles in the registry, await only those jobs, return each newly completed result on its original `call_id`, and then return status on the wait call's own `call_id` [source: https://developers.openai.com/api/docs/guides/async-tool-calling].

### Asking the User for Input
An async tool can be used to ask the user for input (e.g., `request_user_input_async`); the application displays the question, collects the reply, and returns it as the tool result. This is not a built-in Responses tool [source: https://developers.openai.com/api/docs/guides/async-tool-calling]. When displaying a question via an async tool, do not complete the tool call with an acknowledgment that the question was displayed; the result of the tool call is the user's answer [source: https://developers.openai.com/api/docs/guides/async-tool-calling].

## Computer Use

Computer use lets GPT-6 Astra, GPT-6.1 Sol, and GPT-6 Luna interact directly with websites and desktop apps, including applications without an API [source: https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04]. For GPT-6 Astra, OpenAI recommends using code execution for computer use, while the `computer` tool remains supported as an alternative [source: https://developers.openai.com/api/docs/guides/tools-computer-use].

Prefer an API or connected tool when it can do the job directly; reserve computer use for reading screens, clicking buttons, or filling forms [source: https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04]. Playwright handles browser automation; PyAutoGUI handles desktop apps [source: https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04]. See [Code Interpreter](#code-interpreter) for execution details and [Tool Configuration](#tool-configuration) for the `computer` tool schema.

### Code Execution Integration
In a code-execution integration, the model uses a function tool to send scripts to an isolated browser or desktop environment, which must remain available between calls to preserve state [source: https://developers.openai.com/api/docs/guides/tools-computer-use]. The `execute_in_sandbox` or `executeInSandbox` helper in code-execution integrations must preserve the browser or desktop session, enforce execution limits, and apply permission rules [source: https://developers.openai.com/api/docs/guides/tools-computer-use].

- **Python/PyAutoGUI**: The tool definition should specify that variables persist across calls, operations are synchronous, and PyAutoGUI's fail-safe should be enabled [source: https://developers.openai.com/api/docs/guides/tools-computer-use].
- **JavaScript/Playwright**: The context viewport is 1440x900, and the model should use top-level await for async operations [source: https://developers.openai.com/api/docs/guides/tools-computer-use].

### State Management
To preserve state in computer use, the API conversation and the execution environment have separate state; continuing a response via `previous_response_id` does not restore browser sessions, login state, or runtime variables [source: https://developers.openai.com/api/docs/guides/tools-computer-use]. When returning screenshots to the model, use `detail: "original"` to preserve resolution; if downscaled, coordinates must be mapped back to the environment's coordinate space [source: https://developers.openai.com/api/docs/guides/tools-computer-use].

### The `computer` Tool
The `computer` tool is an alternative to code execution for integrations expecting structured actions; it requires a model that supports the computer tool [source: https://developers.openai.com/api/docs/guides/tools-computer-use]. A `computer_call` output contains an ordered `actions` array; the model can request `click`, `double_click`, `drag`, `move`, `scroll`, `keypress`, `type`, `wait`, or `screenshot` [source: https://developers.openai.com/api/docs/guides/tools-computer-use]. A `computer_call` with `status: "completed"` indicates the model has finished generating the call, but the application is still responsible for executing the actions [source: https://developers.openai.com/api/docs/guides/tools-computer-use]. When returning a screenshot for the `computer` tool, the input must be a `computer_call_output` with a matching `call_id` and an output object of type `computer_screenshot` containing the base64 image URL [source: https://developers.openai.com/api/docs/guides/tools-computer-use].

### Safety
For safe computer use, screen content should be treated as untrusted, and users should confirm consequential actions such as purchases, data transmission, or destructive changes [source: https://developers.openai.com/api/docs/guides/tools-computer-use].

## Agents SDK Integration

In the OpenAI Agents SDK, tool semantics remain the same as the Responses API, but the wiring moves into the agent definition and workflow design rather than a single request [source: https://developers.openai.com/api/docs/guides/tools]. Hosted tools, function tools, or hosted MCP tools should be attached directly on the agent when a single specialist should call them itself [source: https://developers.openai.com/api/docs/guides/tools]. A specialist agent should be exposed as a tool when a manager agent needs to stay in control of the user-facing reply [source: https://developers.openai.com/api/docs/guides/tools]. Shell, apply patch, and computer-use harnesses should be kept in the runtime even when the SDK models the tool decision [source: https://developers.openai.com/api/docs/guides/tools].

- **JavaScript**: Local logic can be wrapped as a function tool using the `tool` function from `@openai/agents` and Zod for parameter validation [source: https://developers.openai.com/api/docs/guides/tools]. An agent can be exposed as a tool to another agent using the `.asTool()` method with `toolName` and `toolDescription` parameters [source: https://developers.openai.com/api/docs/guides/tools].
- **Python**: Local logic can be wrapped as a function tool using the `@function_tool` decorator from the `agents` package [source: https://developers.openai.com/api/docs/guides/tools]. An agent can be exposed as a tool to another agent using the `.as_tool()` method with `tool_name` and `tool_description` parameters [source: https://developers.openai.com/api/docs/guides/tools].