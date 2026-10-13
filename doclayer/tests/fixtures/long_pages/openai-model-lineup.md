---
schema_version: 1
privacy: internal
source_origin: external
source_lineage:
- source:https://developers.openai.com/api/docs/guides/model-selection
- source:https://developers.openai.com/api/docs/guides/deployment-checklist
- source:https://developers.openai.com/api/docs/models/gpt-6-luna
- source:https://developers.openai.com/api/docs/models/gpt-6.1-sol
- source:https://developers.openai.com/api/docs/models
- source:https://developers.openai.com/api/docs/models/gpt-6-sol
- source:https://developers.openai.com/api/docs/models/gpt-live-1
- source:https://developers.openai.com/api/docs/api-reference/batch
- source:https://developers.openai.com/api/docs/guides/latest-model
- source:https://developers.openai.com/api/docs/guides/fast-mode
- source:https://developers.openai.com/api/docs/models/gpt-6-astra
- source:https://developers.openai.com/api/docs/pricing
tags:
- openai
- gpt-6
- model-selection
- api
updated: '2026-10-07'
title: OpenAI Model Lineup
concept_id: c-8386e86b
subject_type: model-family
---

# OpenAI Model Lineup

This page provides a comparative overview of the GPT-6 family and related OpenAI models for selection purposes. All latest OpenAI models support text and image input, text output, multilingual capabilities, and vision, though specific models like GPT-Live 1 have different modality support [source: https://developers.openai.com/api/docs/models]. Latest OpenAI models are available via the Responses API and Client SDKs [source: https://developers.openai.com/api/docs/models]. Model availability, tools, reasoning settings, and usage limits vary by product and model version [source: https://developers.openai.com/api/docs/guides/model-selection].

Regarding image capabilities: all latest models accept image input for analysis and produce text-only native output. Image generation or editing is not a native output modality of these models [source: https://developers.openai.com/api/docs/models].

## Fast Mode

Fast mode (renamed from Priority processing on July 30, 2026) delivers up to 2.5× faster speeds with more consistent latency at pay-as-you-go pricing. For `gpt-5.6-sol`, Fast mode speed was increased to up to 2.5× faster than Standard processing [source: https://developers.openai.com/api/docs/guides/fast-mode].

**Rate limits:** Fast mode consumption counts toward rate limits the same way as Standard processing; Standard and Fast share the same rate limit per model [source: https://developers.openai.com/api/docs/guides/fast-mode].

**SLA:** For GPT-5.6 and earlier models, Fast mode and Scale Tier receive the same service-level agreement treatment, and eligible Enterprise agreements may provide service credits when latency targets aren't met [source: https://developers.openai.com/api/docs/guides/fast-mode].

**Model support:** Fast mode supports multimodal capabilities available with Standard processing, including image inputs. It does not support fine-tuned models or embeddings. Future GPT models may support Fast mode, but support isn't guaranteed for every model [source: https://developers.openai.com/api/docs/guides/fast-mode].
## Model Overview

### GPT-6 Astra
GPT-6 Astra is the highest-intelligence model in the GPT-6 family, intended for the most demanding reasoning, coding, and professional work [source: https://developers.openai.com/api/docs/guides/latest-model]. It is described as the most capable model for the most demanding work [source: https://developers.openai.com/api/docs/models] and as the state-of-the-art, most powerful model, intended for ambiguous problems, deep analysis, and ambitious deliverables [source: https://developers.openai.com/api/docs/guides/model-selection]. It delivers state-of-the-art results in computer use, browsing, software engineering, science, and professional work, and executes multi-step workflows spanning code, browsers, and professional software [source: https://developers.openai.com/api/docs/guides/latest-model]. In several evaluations, Astra achieved stronger results using substantially fewer output tokens, and its estimated API cost per task was lower than earlier models despite higher per-token pricing [source: https://developers.openai.com/api/docs/guides/latest-model]. GPT-6 Astra is also OpenAI's most aligned model yet, excelling at exercising care, respecting task boundaries, and communicating transparently [source: https://developers.openai.com/api/docs/guides/latest-model]. As part of strengthened safeguards, OpenAI's systems asynchronously monitor for misalignment and trigger alerts when necessary [source: https://developers.openai.com/api/docs/guides/latest-model]. If cost and latency are not concerns, Astra is the default recommendation [source: https://developers.openai.com/api/docs/guides/model-selection].

*   **Model ID:** `gpt-6-astra` [source: https://developers.openai.com/api/docs/models]
*   **Context Window:** 1.05M tokens [source: https://developers.openai.com/api/docs/models]
*   **Max Output:** 128K tokens [source: https://developers.openai.com/api/docs/models]
*   **Knowledge Cutoff:** April 30, 2026 [source: https://developers.openai.com/api/docs/models]
*   **Reasoning Effort:** Supports `low`, `medium`, `high`, `xhigh`, `max`. Does not support `none` [source: https://developers.openai.com/api/docs/models].
*   **Tools:** Functions, Web search, File search, Computer use [source: https://developers.openai.com/api/docs/models].
*   **Pricing:** $10.00 per 1M input tokens, $50.00 per 1M output tokens [source: https://developers.openai.com/api/docs/models].
*   **Vision & Image:** Supports text and image input with text-only native output [source: https://developers.openai.com/api/docs/models].
*   **Performance Modes:** All GPT-6 Astra users have access to Fast mode and the new Ultrafast mode for the fastest API speeds [source: https://developers.openai.com/api/docs/guides/latest-model]. Fast mode for GPT-6 Astra does not include a latency SLA [source: https://developers.openai.com/api/docs/guides/fast-mode]. For faster speeds beyond Fast mode, Ultrafast mode is the relevant option [source: https://developers.openai.com/api/docs/guides/fast-mode].

### GPT-6.1 Sol
GPT-6.1 Sol is positioned as a model delivering near-Astra performance for complex coding, computer use, and professional work at a lower cost [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol]. It is optimized for complex tasks where time and cost management are required [source: https://developers.openai.com/api/docs/guides/model-selection]. It is the newer model compared to GPT-6 Sol [source: https://developers.openai.com/api/docs/models/gpt-6-sol].

*   **Model ID:** `gpt-6.1-sol` [source: https://developers.openai.com/api/docs/models]
*   **Context Window:** 1,050,000 tokens [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol]
*   **Max Output:** 128,000 tokens [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol]
*   **Knowledge Cutoff:** April 30, 2026 [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol]
*   **Reasoning Effort:** Supports `low`, `medium` (default), `high`, `xhigh`, `max`. Does not support `none` or `minimal` [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol].
*   **Endpoints:** Supports Chat Completions, Responses, and Batch. Does not support Live, Realtime, Assistants, Fine-tuning, or Embeddings [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol]. Tool calling requires the Responses API; Chat Completions supports requests without tools [source: https://developers.openai.com/api/docs/guides/latest-model].
*   **Tools (Responses API):** Web search, file search, image generation, code interpreter, hosted shell, apply patch, skills, computer use, MCP, and tool search [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol].
*   **Pricing:** $2.00 per 1M input tokens, $0.10 per 1M cached input tokens, $2.50 per 1M cache write tokens, $10.00 per 1M output tokens [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol].

### GPT-6 Sol
GPT-6 Sol is built for complex coding and agentic workflows [source: https://developers.openai.com/api/docs/models/gpt-6-sol]. It is an older variant compared to GPT-6.1 Sol [source: https://developers.openai.com/api/docs/models/gpt-6-sol].

*   **Context Window:** 1,050,000 tokens [source: https://developers.openai.com/api/docs/models/gpt-6-sol]
*   **Max Output:** 128,000 tokens [source: https://developers.openai.com/api/docs/models/gpt-6-sol]
*   **Knowledge Cutoff:** April 20, 2026 [source: https://developers.openai.com/api/docs/models/gpt-6-sol]
*   **Reasoning Effort:** Supports `none`, `low`, `medium` (default), `high`, `xhigh`, `max` [source: https://developers.openai.com/api/docs/models/gpt-6-sol].
*   **Pricing:** $2.00 per 1M input tokens, $0.20 per 1M cached input tokens, $2.50 per 1M cache writes, $10.00 per 1M output tokens [source: https://developers.openai.com/api/docs/models/gpt-6-sol].

### GPT-6 Luna
GPT-6 Luna is the fastest and most cost-effective GPT-6 model, suited to focused, high-volume tasks [source: https://developers.openai.com/api/docs/guides/latest-model]. It is positioned as a smart and efficient model for scoped tasks, triage, and frequent automations [source: https://developers.openai.com/api/docs/guides/model-selection].

*   **Context Window:** 1,050,000 tokens [source: https://developers.openai.com/api/docs/models/gpt-6-luna]
*   **Max Output:** 128,000 tokens [source: https://developers.openai.com/api/docs/models/gpt-6-luna]
*   **Knowledge Cutoff:** May 18, 2026 [source: https://developers.openai.com/api/docs/models/gpt-6-luna]
*   **Reasoning Effort:** Supports `none`, `low`, `medium` (default), `high`, `xhigh`, `max` [source: https://developers.openai.com/api/docs/models/gpt-6-luna].
*   **Endpoints:** Supports Chat Completions, Responses, and Batch. Does not support Live, Realtime, or Fine-tuning [source: https://developers.openai.com/api/docs/models/gpt-6-luna].
*   **Pricing:** $0.10 per 1M input tokens, $0.50 per 1M output tokens [source: https://developers.openai.com/api/docs/models/gpt-6-luna].

### GPT-Live 1
GPT-Live 1 is a full-duplex voice model for real-time conversations that can listen and speak simultaneously and delegate reasoning and tool use to a backend agent [source: https://developers.openai.com/api/docs/models/gpt-live-1].

*   **Model ID:** `gpt-live-1` [source: https://developers.openai.com/api/docs/models/gpt-live-1]
*   **Knowledge Cutoff:** July 31, 2025 [source: https://developers.openai.com/api/docs/models/gpt-live-1]
*   **Modalities:** Supports Audio and Text for input and output. Does not support Image or Video [source: https://developers.openai.com/api/docs/models/gpt-live-1].
*   **Endpoints:** Only supported by the Live endpoint (`v1/live/sessions`). Not supported by Chat Completions, Responses, Realtime, or other standard endpoints [source: https://developers.openai.com/api/docs/models/gpt-live-1].
*   **Pricing:** $0.05 per minute for voice sessions, billed per second [source: https://developers.openai.com/api/docs/models/gpt-live-1]. Backend model and tool usage is billed separately using normal pricing [source: https://developers.openai.com/api/docs/models/gpt-live-1].

## Limits, Snapshots and Endpoints by Model

The base sections above give each model's context window, max output, cutoff and reasoning-effort set. Additional per-model limits:

| Property | GPT-6 Astra | GPT-6.1 Sol | GPT-6 Sol | GPT-6 Luna |
| :--- | :--- | :--- | :--- | :--- |
| Max input tokens | 922,000 | 922,000 | 922,000 | 922,000 |
| Standard rate limits (RPM / TPM) Build | 5,000 / 1,000,000 | 5,000 / 1,000,000 | 5,000 / 1,000,000 | 5,000 / 2,000,000 |
| Launch | 10,000 / 4,000,000 | 10,000 / 4,000,000 | 10,000 / 4,000,000 | 10,000 / 10,000,000 |
| Grow | 15,000 / 40,000,000 | 15,000 / 40,000,000 | 15,000 / 40,000,000 | 30,000 / 180,000,000 |
| Model ID / snapshot | `gpt-6-astra` (only snapshot) | `gpt-6.1-sol` ("Use `gpt-6.1-sol` to select this model") | `gpt-6-sol` ("Use `gpt-6-sol` in your API requests") | `gpt-6-luna` |

[source: https://developers.openai.com/api/docs/models/gpt-6-astra @2026-10-07] [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol @2026-10-07] [source: https://developers.openai.com/api/docs/models/gpt-6-sol @2026-10-07] [source: https://developers.openai.com/api/docs/models/gpt-6-luna @2026-10-07]

Chat Completions, Responses, and Batch are supported on all four model pages [source: https://developers.openai.com/api/docs/models/gpt-6-astra @2026-10-07] [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol @2026-10-07] [source: https://developers.openai.com/api/docs/models/gpt-6-sol @2026-10-07] [source: https://developers.openai.com/api/docs/models/gpt-6-luna @2026-10-07]. The GPT-6 Astra page lists as not supported: Live (`v1/live/sessions`), Realtime, Realtime translation, Realtime transcription, Assistants, Fine-tuning, Embeddings, Image generation, Videos, Image edit, Speech generation, Transcription, Translation, Moderation, and legacy Completions [source: https://developers.openai.com/api/docs/models/gpt-6-astra @2026-10-07]. The GPT-6.1 Sol and GPT-6 Luna pages list the same set [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol @2026-10-07] [source: https://developers.openai.com/api/docs/models/gpt-6-luna @2026-10-07]. Like the other three pages, the GPT-6 Sol page lists Live (`v1/live/sessions`) as not supported [source: https://developers.openai.com/api/docs/models/gpt-6-sol @2026-10-07]. All four list streaming, structured outputs, function calling, file search, image input, web search, and prompt caching, and the Responses API tools web search, file search, image generation, code interpreter, hosted shell, apply patch, skills, computer use, MCP, and tool search; the GPT-6.1 Sol page also lists fine-tuning and predicted outputs as not supported [source: https://developers.openai.com/api/docs/models/gpt-6-astra @2026-10-07] [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol @2026-10-07] [source: https://developers.openai.com/api/docs/models/gpt-6-sol @2026-10-07] [source: https://developers.openai.com/api/docs/models/gpt-6-luna @2026-10-07].

Chat Completions tool calling across sources: the latest-model guide says GPT-6 Astra and GPT-6.1 Sol support Chat Completions but tool calling requires Responses, and that GPT-6 Sol and GPT-6 Luna support function calling in Chat Completions only with `reasoning_effort: "none"`; the GPT-6 Sol model page says Chat Completions supports function calling only with `reasoning_effort` set to `none`. The two sources agree for GPT-6 Sol on a re-read dated 2026-10-07 [source: https://developers.openai.com/api/docs/guides/latest-model @2026-10-07] [source: https://developers.openai.com/api/docs/models/gpt-6-sol @2026-10-07].

## Current Pricing by Tier (USD per 1M tokens)

Columns are Input / Cached input / Cache writes / Output [source: https://developers.openai.com/api/docs/pricing @2026-10-07].

| Model | Standard short | Standard long | Batch and Flex short | Batch and Flex long | Fast short | Fast long | Ultrafast short | Ultrafast long |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| GPT-6 Astra | 10.00 / 1.00 / 12.50 / 50.00 | 20.00 / 2.00 / 25.00 / 75.00 | 5.00 / 0.50 / 6.25 / 25.00 | 10.00 / 1.00 / 12.50 / 37.50 | 20.00 / 2.00 / 25.00 / 100.00 | 40.00 / 4.00 / 50.00 / 150.00 | 60.00 / 6.00 / 75.00 / 300.00 | 120.00 / 12.00 / 150.00 / 450.00 |
| GPT-6.1 Sol | 2.00 / 0.10 / 2.50 / 10.00 | 4.00 / 0.20 / 5.00 / 15.00 | 1.00 / 0.05 / 1.25 / 5.00 | 2.00 / 0.10 / 2.50 / 7.50 | 4.00 / 0.20 / 5.00 / 20.00 | 8.00 / 0.40 / 10.00 / 30.00 | not listed | not listed |
| GPT-6 Sol | 2.00 / 0.20 / 2.50 / 10.00 | 4.00 / 0.40 / 5.00 / 15.00 | 1.00 / 0.10 / 1.25 / 5.00 | 2.00 / 0.20 / 2.50 / 7.50 | 4.00 / 0.40 / 5.00 / 20.00 | 8.00 / 0.80 / 10.00 / 30.00 | not listed | not listed |
| GPT-6 Luna | 0.10 / 0.01 / 0.125 / 0.50 | 0.20 / 0.02 / 0.25 / 0.75 | 0.05 / 0.005 / 0.0625 / 0.25 | 0.10 / 0.01 / 0.125 / 0.375 | 0.20 / 0.02 / 0.25 / 1.00 | 0.40 / 0.04 / 0.50 / 1.50 | not listed | not listed |

[source: https://developers.openai.com/api/docs/pricing @2026-10-07]

The pricing page lists Ultrafast only for gpt-6-astra, and the Astra Ultrafast rates are 3x the Astra Fast rates in both short and long context (input $60.00 vs $20.00 and $120.00 vs $40.00; output $300.00 vs $100.00 and $450.00 vs $150.00) [source: https://developers.openai.com/api/docs/pricing @2026-10-07]. GPT-6.1 Sol and GPT-6 Sol differ on price only in cached input: $0.10 versus $0.20 per 1M tokens, with identical $2.00 input, $2.50 cache writes, and $10.00 output [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol @2026-10-07] [source: https://developers.openai.com/api/docs/models/gpt-6-sol @2026-10-07]. The GPT-6 Luna page states a 10% premium for regional processing [source: https://developers.openai.com/api/docs/models/gpt-6-luna @2026-10-07]. Mode behavior, availability, and constraints for Fast and Ultrafast are on [[openai-api-performance-modes]].

## Selection Guidance

### By Workload
*   **Complex Reasoning & Coding:** GPT-6 Astra is the highest capability model in the GPT-6 family [source: https://developers.openai.com/api/docs/guides/deployment-checklist].
*   **Complex Coding & Professional Work:** GPT-6.1 Sol is recommended to balance intelligence and cost [source: https://developers.openai.com/api/docs/models]. It is suitable for complex projects where cost is a factor, such as creating board presentations or building websites from briefs [source: https://developers.openai.com/api/docs/guides/model-selection].
*   **High-Volume & Cost-Sensitive:** GPT-6 Luna is recommended for cost-sensitive, high-volume workloads [source: https://developers.openai.com/api/docs/models].
*   **Small Edits & Low Latency:** Luna with Medium reasoning effort is recommended for small edits to existing files with a priority on lower latency and costs [source: https://developers.openai.com/api/docs/guides/model-selection].

### By Reasoning Effort
*   **Astra:**
    *   *Extra High:* Suitable for demanding analysis and complex deliverables with exacting requirements [source: https://developers.openai.com/api/docs/guides/model-selection].
*   **GPT-6.1 Sol:**
    *   *Medium:* Suitable for complex technical work and coordinated deliverables expected to be revised [source: https://developers.openai.com/api/docs/guides/model-selection].
    *   *Extra High:* Suitable for polished deliverables, connected visual systems, and decisions built from conflicting evidence [source: https://developers.openai.com/api/docs/guides/model-selection].
*   **Luna:**
    *   *Low:* Suitable for fine-grained edits, well-scoped problem-solving, and simple data extraction [source: https://developers.openai.com/api/docs/guides/model-selection].
    *   *Extra High:* Suitable for finding current context across multiple apps, prioritizing work, and solving problems with clear constraints [source: https://developers.openai.com/api/docs/guides/model-selection].

## API & Configuration Notes

### API Endpoints
*   The Responses API is OpenAI's flagship API and the recommended starting point for accessing the newest model behavior, built-in tools, stateful workflows, and agent features [source: https://developers.openai.com/api/docs/guides/deployment-checklist].
*   GPT-6 Astra and GPT-6.1 Sol support the Chat Completions API, but tool calling requires the Responses API [source: https://developers.openai.com/api/docs/guides/latest-model].
*   GPT-6 Sol and GPT-6 Luna support function calling in Chat Completions only when `reasoning_effort` is "none" [source: https://developers.openai.com/api/docs/guides/deployment-checklist].

*   The Batch API supports the model ID `gpt-6-astra` [source: https://developers.openai.com/api/docs/api-reference/batch]. The Batch object includes a `model` field which is an optional string representing the Model ID used to process the batch [source: https://developers.openai.com/api/docs/api-reference/batch].
### Reasoning Effort Configuration
*   GPT-6 Astra and GPT-6.1 Sol do not support the `none` reasoning effort; GPT-6 Sol and GPT-6 Luna do [source: https://developers.openai.com/api/docs/guides/latest-model].
*   When reasoning effort is not "none", parameters `temperature`, `top_p`, and `top_logprobs` must be removed from requests, as must `logprobs` from Chat Completions and `message.output_text.logprobs` from the Responses include array [source: https://developers.openai.com/api/docs/guides/deployment-checklist].
*   To change reasoning effort mid-conversation for GPT-6 models in standard single-agent mode, add a `configuration_update` input item before the next user message while leaving the request-level `reasoning.effort` unchanged to preserve prompt prefix caching eligibility [source: https://developers.openai.com/api/docs/guides/deployment-checklist].
*   For the hardest quality-first workloads, `reasoning.mode: "pro"` can be compared with standard mode at the same effort; pro mode increases latency and token usage but may improve reliability [source: https://developers.openai.com/api/docs/guides/deployment-checklist].

### Pricing Multipliers
*   **Long Context:** Prompts exceeding 272K input tokens incur a 2x multiplier on input and cache rates and a 1.5x multiplier on output rates for the entire request for GPT-6.1 Sol, GPT-6 Sol, and GPT-6 Luna [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol].
*   **Processing Modes:** Batch and Flex processing are priced at 50% of Standard rates, while Fast mode is priced at 2x the applicable rates for GPT-6.1 Sol, GPT-6 Sol, and GPT-6 Luna [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol].
*   **GPT-5.6 Sol Fast Mode Pricing:** Fast mode costs 2x the Standard rate: short-context $8/1M input and $40/1M output tokens; long-context $16/1M input and $60/1M output tokens. Promotional pricing available at least through November 21, 2026 [source: https://developers.openai.com/api/docs/guides/fast-mode].
*   **Regional Processing:** Regional processing adds a 10% premium to pricing where available for GPT-6.1 Sol [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol].

### Data Residency
*   **GPT-6.1 Sol:** Supports US and EU data residency, but Fast mode is unavailable when using EU data residency [source: https://developers.openai.com/api/docs/models/gpt-6.1-sol].
*   **GPT-6 Sol & GPT-6 Luna:** EU data residency is available with Standard, Flex, and Batch processing [source: https://developers.openai.com/api/docs/models/gpt-6-sol].
*   **GPT-6 Astra:** Fast mode is not available with EU data residency [source: https://developers.openai.com/api/docs/guides/latest-model].
*   **Fast mode (GPT-6 family):** Not available with EU data residency for GPT-6 Astra, GPT-6.1 Sol, GPT-6 Sol, or GPT-6 Luna. Compatible with data residency, Zero Data Retention, and a BAA, subject to model-specific availability; existing endpoint, tool, eligibility, and contractual requirements still apply [source: https://developers.openai.com/api/docs/guides/fast-mode].
*   **Ultrafast mode:** Supports US data residency and global processing only; does not support EU or other non-US regional processing endpoints [source: https://developers.openai.com/api/docs/guides/latest-model].

## Related Pages
*   [[gpt-6-astra]]