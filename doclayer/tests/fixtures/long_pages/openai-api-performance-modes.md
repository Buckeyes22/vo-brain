---
schema_version: 1
privacy: internal
source_origin: external
source_lineage:
- https://developers.openai.com/api/docs/guides/ultrafast-mode
- https://developers.openai.com/api/docs/guides/fast-mode
- https://developers.openai.com/api/docs/guides/prompt-caching
- https://developers.openai.com/api/docs/guides/streaming-responses
- source:https://developers.openai.com/api/docs/guides/ultrafast-mode
- source:https://developers.openai.com/api/docs/guides/fast-mode
- source:https://developers.openai.com/api/docs/guides/prompt-caching
- source:https://developers.openai.com/api/docs/guides/streaming-responses
- source:https://developers.openai.com/api/docs/guides/reasoning
- source:https://developers.openai.com/api/docs/guides/compaction
- source:https://developers.openai.com/api/docs/guides/prompt-caching/diagnostics
tags:
- openai
- api
- performance
- latency
- cost
- streaming
- prompt-caching
- gpt-6-astra
updated: '2026-10-07'
title: OpenAI API Performance Modes
concept_id: c-87a3a1a1
subject_type: product
---

# OpenAI API Performance Modes

This page covers the performance and cost levers in the OpenAI API: Ultrafast mode, Fast mode, streaming, prompt caching, and compaction. These features directly affect latency, throughput, and per-token cost. Fast mode delivers lower and more predictable latency than Standard at a premium per-token price, fitting interactive chat and coding workloads ([Fast Mode](#fast-mode)). Ultrafast accelerates token generation regardless of reasoning effort; it is available for GPT-6 Astra and works in both Codex and the API for quick coding loops ([Ultrafast Mode](#ultrafast-mode)). [https://openai.com/index/practical-guide-building-gpt-6/](https://openai.com/index/practical-guide-building-gpt-6/) @ 2026-10-04

## Asynchronous Tool Calling

In the GPT-6 family API, asynchronous tool calling lets the model continue independent work while the application runs a slower task (e.g., tests). The app returns the result when ready; dependent work should wait for that result before starting. This keeps long-running workflows from stalling on a single slow step. ([Practical Guide to Building GPT-6](https://openai.com/index/practical-guide-building-gpt-6/) @ 2026-10-04)

## Codex Clarification and Steering

In Codex with GPT-6 Astra, the model can ask for clarification while it works on long-running tasks. The developer should resolve questions that affect the next step and specify which independent work can continue while deciding. Steering the active task with new information—explaining what should change and what should stay the same—helps avoid spending time on an approach that no longer meets needs. ([Practical Guide to Building GPT-6](https://openai.com/index/practical-guide-building-gpt-6/) @ 2026-10-04)

## Computer Use

Computer use lets GPT-6 Astra, GPT-6.1 Sol, and GPT-6 Luna interact directly with websites and desktop apps, including applications without an API. The guide recommends using an API or connected tool when it can do the job directly, and reserving computer use for reading screens, clicking buttons, or filling forms. Playwright works with browsers; PyAutoGUI works with desktop apps. ([Practical Guide to Building GPT-6](https://openai.com/index/practical-guide-building-gpt-6/) @ 2026-10-04)

## GPT-6 Model Lineup and Pricing

The GPT-6 family comprises three models priced per 1M tokens (input/output/cached-input): GPT-6 Astra ($10.00/$50.00/$1.00), GPT-6.1 Sol ($2.00/$10.00/$0.10), and GPT-6 Luna ($0.10/$0.50/$0.01). Selection is an intelligence/price tradeoff: Astra for the hardest reasoning requiring maximum intelligence; Sol for complex coding, research, [computer use](#computer-use), and [multi-agent workflows](#multi-agent-workflows-beta); Luna for focused tasks at scale and everyday repeated work with a clear goal (field extraction, request classification, structured summaries). A reasoning-level parameter (Low through Max) further tunes per-task effort. ([Practical Guide to Building GPT-6](https://openai.com/index/practical-guide-building-gpt-6/) @ 2026-10-04)

## Mid-Turn Steering

In the GPT-6 family API, mid-turn steering lets a developer send a correction through the Responses WebSocket API while the model is working. Updates are queued; they do not cancel running tools or undo completed actions. ([Practical Guide to Building GPT-6](https://openai.com/index/practical-guide-building-gpt-6/) @ 2026-10-04)

## Multi-Agent Workflows (Beta)

GPT-6.1 Sol supports multi-agent workflows in the Responses API. It can assign independent subtasks to subagents—for example, investigating different parts of a codebase—and combine their findings into a single final response. This capability is currently in beta. ([Practical Guide to Building GPT-6](https://openai.com/index/practical-guide-building-gpt-6/) @ 2026-10-04)

## Production Context Efficiency

For longer conversations, [compaction](#compaction) reduces context size while preserving the state needed to continue. The guide recommends cutting context the task doesn't need while keeping the evidence it does, and running independent tasks together so one slow step doesn't hold up unrelated work. ([Practical Guide to Building GPT-6](https://openai.com/index/practical-guide-building-gpt-6/) @ 2026-10-04)
## Compaction

Compaction reduces context size while preserving state needed for subsequent turns, balancing quality, cost, and latency as conversations grow. To cut long-tail latency, drop items preceding the most recent compaction item; it carries the context required to continue the conversation. [^compaction-src]

[^compaction-src]: https://developers.openai.com/api/docs/guides/compaction

## Prompt Cache Diagnostics

Prompt cache diagnostics are available in the Responses API for GPT-5.6 and later supported models. They explain why a request reused fewer tokens than expected by comparing it with an earlier response to identify changes to the model, tools, settings, or input that prevented reuse. Diagnostics incur no additional cost and do not count separately toward rate limits; any extra baseline or retry requests are billed normally and count toward rate limits. [^diagnostics-src]

To confirm a caching improvement after a change: send another representative request, compare it with the intended baseline, check the diagnostic result for any remaining difference, and compare `cached_tokens`, `cache_write_tokens`, and total cost across several requests. [^diagnostics-src]

On supported models, a configuration update can change `reasoning.effort` while preserving the earlier prefix, avoiding a cache miss caused solely by the effort change. [^caching-src]

[^diagnostics-src]: https://developers.openai.com/api/docs/guides/prompt-caching/diagnostics
[^caching-src]: https://developers.openai.com/api/docs/guides/prompt-caching

## Reasoning Mode

GPT-5.6 and GPT-6 models support `standard` and `pro` reasoning modes in the Responses API; `standard` is the default. Pro mode aggregates the model work performed to produce the final answer and bills those tokens at the selected model's standard token rates; it performs more model work than standard mode, increasing token usage and cost. Existing Pro model IDs keep their current behavior and pricing. [^reasoning-src]

Reasoning mode and reasoning effort are independent settings. If `reasoning.effort` is omitted, GPT-6.1 Sol, GPT-6 Sol, and GPT-6 Luna default to `medium` effort in both modes. [^reasoning-src]

The GPT-6 family API exposes four reasoning effort levels: **Low** (routine tasks such as fact extraction or small edits), **Medium** (work requiring judgment such as planning or comparing options), **High** (difficult debugging, deeper analysis, or careful review), and **Extra high / Max** (test where supported when High falls short; keep only if the improvement justifies added time and cost). Reasoning effort can be changed mid-conversation without breaking the prompt cache. [^gpt6-guide]

Set `reasoning.context` to control which available reasoning items the model can use. `all_turns` renders available, compatible reasoning items from earlier turns into the next sample; supported by GPT-5.6 models and GPT-6.1 Sol. [^reasoning-src]

> GPT-6.1 Sol also supports the all_turns value for reasoning.context. [^reasoning-src]

`all_turns` has an effect only when the request has access to earlier response items via `previous_response_id`, conversation attachment, or manual replay; on the first request both modes behave identically. [^reasoning-src]

When switching model families, the API omits incompatible reasoning from the model's context even when `reasoning.context` is `all_turns`. [^reasoning-src]

Persisted reasoning is reusable only within the same model family (e.g., `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`); it does not carry between the GPT-5.6 and GPT-5.5 families. [^reasoning-src]

[^reasoning-src]: https://developers.openai.com/api/docs/guides/reasoning

[^gpt6-guide]: https://openai.com/index/practical-guide-building-gpt-6/@2026-10-04
## Ultrafast Mode

Ultrafast mode is the fastest service tier in the OpenAI API [source: https://developers.openai.com/api/docs/guides/ultrafast-mode]. It is broadly available for GPT-6 Astra and in preview for GPT-5.6 Sol [source: https://developers.openai.com/api/docs/guides/ultrafast-mode].

### Configuration

To use Ultrafast mode, set the model to `gpt-6-astra` and `service_tier` to `ultrafast` in the request [source: https://developers.openai.com/api/docs/guides/ultrafast-mode].

### Transport

WebSockets are strongly recommended for Ultrafast mode, especially for agentic applications with many tool calls, to reduce network overhead [source: https://developers.openai.com/api/docs/guides/ultrafast-mode]. Ultrafast mode also supports HTTP requests through the SDK, though WebSocket is preferred for agentic applications [source: https://developers.openai.com/api/docs/guides/ultrafast-mode].

### Availability and Rate Limits

Ultrafast mode supports US data residency and global processing only; it does not support EU or other non-US regional processing endpoints [source: https://developers.openai.com/api/docs/guides/ultrafast-mode].

Default token rate limits for GPT-6 Astra Ultrafast mode [source: https://developers.openai.com/api/docs/guides/ultrafast-mode]:
- API Usage Tiers 1–3: 500,000 TPM
- API Usage Tier 4: 1,000,000 TPM
- API Usage Tier 5: 5,000,000 TPM

### Constraints (GPT-6 Astra)

Ultrafast is currently available to all API users at low rate limits; organizations with OpenAI account teams can request higher rate limits [source: https://developers.openai.com/api/docs/guides/ultrafast-mode @2026-10-07]. The guide says to use it when speed justifies the higher cost [source: https://developers.openai.com/api/docs/guides/ultrafast-mode @2026-10-07]. Its transport text reads: "We strongly recommend WebSockets, especially for agentic applications that make many tool calls in quick succession," "Ultrafast also supports HTTP requests through the SDK," and "To display output as it arrives, enable streaming." [source: https://developers.openai.com/api/docs/guides/ultrafast-mode @2026-10-07]. As of 2026-10-07 the guide's rate limit table labels these tiers Build (500,000 TPM), Launch (1,000,000 TPM), and Grow (5,000,000 TPM) rather than by usage tier number; the numbered list above is from an earlier fetch [source: https://developers.openai.com/api/docs/guides/ultrafast-mode @2026-10-07]. Ultrafast pricing, including the long-context row, is in [[openai-model-lineup]].

Unknown as of 2026-10-07: https://developers.openai.com/api/docs/guides/ultrafast-mode does not state a latency SLA, batch processing support, reasoning-effort constraints, background-mode support, or ZDR support.

## Fast Mode

Fast mode delivers up to 2.5× faster speeds and more consistent latency with pay-as-you-go pricing, intended for user-facing applications with regular traffic where latency matters [source: https://developers.openai.com/api/docs/guides/fast-mode]. Priority processing was renamed Fast mode on July 30, 2026, and the speed for `gpt-5.6-sol` was increased to be up to 2.5× faster than Standard processing [source: https://developers.openai.com/api/docs/guides/fast-mode].

### Configuration

Fast mode can be configured per-request by setting the `service_tier` parameter to `fast` or `priority` in the Responses or Chat Completions API [source: https://developers.openai.com/api/docs/guides/fast-mode]. Both `service_tier: "priority"` and `service_tier: "fast"` can be used in API requests to access Fast mode functionality [source: https://developers.openai.com/api/docs/guides/fast-mode].

Fast mode can also be configured at the project level by changing the Project Service Tier to Fast in Settings, causing requests without a specified `service_tier` to default to Fast mode [source: https://developers.openai.com/api/docs/guides/fast-mode].

For GPT-5.6 and earlier models, the response object returns `service_tier: "priority"` regardless of whether the request specified `priority` or `fast` [source: https://developers.openai.com/api/docs/guides/fast-mode].

### Rate Limits and Ramp Rate

Fast mode consumption counts toward rate limits the same way as Standard processing, and Standard processing and Fast mode share the same rate limit for a given model [source: https://developers.openai.com/api/docs/guides/fast-mode]. If traffic ramps too fast, the system may downgrade Fast mode requests to standard speeds and charge standard rates, indicated by `service_tier: "default"` in the response [source: https://developers.openai.com/api/docs/guides/fast-mode]. As a rule of thumb, once traffic reaches 1 million input tokens per minute (TPM), it should be increased by no more than 50% every 15 minutes to avoid triggering the ramp-rate limit [source: https://developers.openai.com/api/docs/guides/fast-mode].

### Billing

Fast mode charges a per-token premium over Standard processing, and cached input discounts still apply to Fast mode requests [source: https://developers.openai.com/api/docs/guides/fast-mode]. For GPT-5.6 Sol, Fast mode costs twice the corresponding Standard rate: $8 per 1M input tokens and $40 per 1M output tokens for short-context, and $16 per 1M input tokens and $60 per 1M output tokens for long-context, with promotional pricing available at least through November 21, 2026 [source: https://developers.openai.com/api/docs/guides/fast-mode].

### Capabilities

Fast mode supports multimodal requests including image inputs, but does not support fine-tuned models or embeddings [source: https://developers.openai.com/api/docs/guides/fast-mode].

### GPT-6 Family Notes

The Fast mode guide details Fast pricing only for GPT-5.6 Sol; GPT-6 Fast prices come from the model and pricing pages and are in [[openai-model-lineup]] [source: https://developers.openai.com/api/docs/guides/fast-mode @2026-10-07]. The Fast constraints for GPT-6 models (EU data residency, Astra latency SLA, ZDR and BAA compatibility) are stated in [[openai-model-lineup]] under its Fast Mode and Data Residency headings.

## Streaming

By default, the OpenAI API generates the entire model output before sending it in a single HTTP response [source: https://developers.openai.com/api/docs/guides/streaming-responses]. Streaming responses allows processing the beginning of the model's output while it continues generating the full response [source: https://developers.openai.com/api/docs/guides/streaming-responses].

### Enabling Streaming

OpenAI recommends using the Responses API for streaming over Chat Completions because it was designed with streaming in mind and is type-safe [source: https://developers.openai.com/api/docs/guides/streaming-responses]. The guide focuses on HTTP streaming using `stream=true` over server-sent events (SSE) [source: https://developers.openai.com/api/docs/guides/streaming-responses].

To enable streaming in the Responses API, set `stream=True` in the request [source: https://developers.openai.com/api/docs/guides/streaming-responses]. The Responses API uses semantic events for streaming, where each event is typed with a predefined schema [source: https://developers.openai.com/api/docs/guides/streaming-responses]. Common lifecycle events to listen for when streaming text in the Responses API include `response.created`, `response.output_text.delta`, `response.completed`, and `error` [source: https://developers.openai.com/api/docs/guides/streaming-responses].

To stream Chat Completions, set `stream=True` when calling the endpoint, which returns data-only server-sent events [source: https://developers.openai.com/api/docs/guides/streaming-responses]. In streamed Chat Completions, the response contains a `delta` field rather than a `message` field [source: https://developers.openai.com/api/docs/guides/streaming-responses]. The `delta` field can hold a role token, a content token, or nothing [source: https://developers.openai.com/api/docs/guides/streaming-responses].

### Moderation Risk

Streaming model output in production makes content moderation more difficult because partial completions are harder to evaluate [source: https://developers.openai.com/api/docs/guides/streaming-responses]. Moderation scores requested with a generation request arrive after the full output is available and are not included with partial output deltas [source: https://developers.openai.com/api/docs/guides/streaming-responses].

## Prompt Caching

Prompt caching is enabled by default for supported OpenAI models [source: https://developers.openai.com/api/docs/guides/prompt-caching]. Cached input tokens cost up to 95% less than uncached input tokens, depending on the model [source: https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04]. To maximize cache reuse, place stable instructions and reference material before changing task details, and keep tool definitions consistent [source: https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04].

### How Caching Works

The prompt cache stores key-value (KV) tensors, not the tokens themselves; OpenAI caches the full rendered context including OpenAI-provided instructions, developer messages, tool definitions, and conversation history [source: https://developers.openai.com/api/docs/guides/prompt-caching]. Cache reuse requires the entire rendered prefix to match; if content or a relevant setting changes before a breakpoint, the prefix after that change cannot match the existing cache entry [source: https://developers.openai.com/api/docs/guides/prompt-caching].

For GPT-5.6 and later, the minimum cacheable prompt length is 1,024 tokens, excluding tokens in OpenAI-provided hidden system content [source: https://developers.openai.com/api/docs/guides/prompt-caching]. For GPT-5.6 and later, cache writes cost 1.25× the standard uncached input-token rate, and subsequent reads cost 0.1× that rate (0.05× for GPT-6.1 Sol) [source: https://developers.openai.com/api/docs/guides/prompt-caching]. At the 0.1× read rate, writing a prefix once and fully reusing it once costs 1.35× its ordinary input cost versus 2× without caching; across ten requests, one write and nine full reads cost 2.15× versus 10× without caching [source: https://developers.openai.com/api/docs/guides/prompt-caching]. GPT-5.6 and later support both implicit and explicit caching. In explicit mode, developers choose breakpoint locations; each request can create up to four cache writes, and content after the last selected breakpoint is processed at the uncached input-token rate without a cache-write charge. In implicit mode, OpenAI places a breakpoint at the end of the latest eligible message—user messages, the last tool response in a consecutive group of tool responses, or the last developer message in the initial consecutive group of developer messages [source: https://developers.openai.com/api/docs/guides/prompt-caching].

For models before GPT-5.6, reported `cached_tokens` is calculated by subtracting hidden system tokens from the last matched breakpoint and rounding down to the nearest multiple of 128 [source: https://developers.openai.com/api/docs/guides/prompt-caching].

### Cache Lifetime and Location

For GPT-5.6 and later, the only supported value for `prompt_cache_options.ttl` is `30m`, which is also the default; a cached prefix remains eligible for reuse for 30 minutes after its most recent write or reuse [source: https://developers.openai.com/api/docs/guides/prompt-caching]. Cached states live on individual machines, and traffic above 15 requests per minute can lead to overflow routing, which may prevent cache reuse if the request does not reach a machine holding a matching entry [source: https://developers.openai.com/api/docs/guides/prompt-caching].

### Optimization Strategies

On GPT-5.6 and later models, use `prompt_cache_options.mode` and `prompt_cache_breakpoint` to control cache breakpoints [source: https://developers.openai.com/api/docs/guides/prompt-caching]. `prompt_cache_options.mode` selects implicit or explicit-only caching, and `prompt_cache_breakpoint` marks a boundary you choose [source: https://developers.openai.com/api/docs/guides/prompt-caching]. In explicit caching mode, each request can create up to four cache writes, and content after the last selected breakpoint is processed at the uncached input-token rate without a cache-write charge [source: https://developers.openai.com/api/docs/guides/prompt-caching]. Set `prompt_cache_options.prewarm` to `true` in a Responses API request to prepare the prompt cache without generating output; tokens written during prewarm are billed at the standard cache-write rate [source: https://developers.openai.com/api/docs/guides/prompt-caching].

On GPT-5.6 and later, OpenAI handles cache routing automatically, so `prompt_cache_key` is not needed to optimize caching but can be used for separate cache accounting [source: https://developers.openai.com/api/docs/guides/prompt-caching]. On models before GPT-5.6, use a stable `prompt_cache_key` to optimize cache routing for requests that share a reusable prefix, aiming for about 15 requests per minute in total across all prefixes using each key [source: https://developers.openai.com/api/docs/guides/prompt-caching].

In multi-turn applications, preserve earlier messages and tool results so later turns can reuse the full shared prefix, as reusing growing conversation history can save more input tokens than caching only initial instructions [source: https://developers.openai.com/api/docs/guides/prompt-caching]. To disable tool use for a request without breaking cache prefixes, set `tool_choice` to "none" instead of removing the tool definitions [source: https://developers.openai.com/api/docs/guides/prompt-caching]. Use `allowed_tools` to restrict which tools are callable while keeping the supplied `tools` list stable to preserve reusable prefixes [source: https://developers.openai.com/api/docs/guides/prompt-caching].

On GPT-6 models, append a `configuration_update` input item to change reasoning effort between responses while keeping request-level `reasoning.effort` unchanged to preserve the original prefix for cache reuse [source: https://developers.openai.com/api/docs/guides/prompt-caching].

When migrating from earlier models to GPT-5.6 and later, replace `prompt_cache_retention` with `prompt_cache_options.ttl` [source: https://developers.openai.com/api/docs/guides/prompt-caching].

### Gotchas

In implicit mode for GPT-5.6 and later, if requests share a long prefix but have different suffixes, caching the first complete request does not make the shorter shared prefix reusable unless an explicit breakpoint is placed after the static content [source: https://developers.openai.com/api/docs/guides/prompt-caching]. Switching from implicit to explicit-only mode in a follow-up request will not reuse a prefix cached by a previous implicit request unless an explicit breakpoint in the new request matches the cached endpoint from the previous request [source: https://developers.openai.com/api/docs/guides/prompt-caching].

## Related Pages

- [[openai-responses-api]]