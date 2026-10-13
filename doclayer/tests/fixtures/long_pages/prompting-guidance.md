---
schema_version: 1
privacy: internal
source_origin: external
source_lineage:
- source:https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra
- source:https://agents.md/
- source:https://github.com/openai/codex/tree/main/codex-rs/skills/src/assets/samples/openai-docs
- source:https://github.com/openai/skills/tree/main/skills/.curated/openai-docs
- source:https://developers.openai.com/api/docs/guides/reasoning-best-practices
tags:
- gpt-6-astra
- prompting
- skills
- agents-md
- openai
- reasoning-models
- o-series
updated: '2026-10-06'
title: GPT-6 Astra Prompting Guidance
concept_id: c-21d0ba3a
subject_type: procedure
---

# GPT-6 Astra Prompting Guidance

GPT-6 Astra requires less handholding and scaffolding than previous models, making it important to revisit accumulated instructions such as skills and `AGENTS.md` files [source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra]. This page outlines strategic shifts in prompting for GPT-6 Astra, focusing on reduced constraints and optimized context management. The GPT-6 family comprises GPT-6 Astra (most intelligent; $10/$50/$1 per 1M input/output/cached tokens), GPT-6.1 Sol (near-Astra at a fifth the price; $2/$10/$0.10), and GPT-6 Luna (fast everyday work; $0.10/$0.50/$0.01) [source: https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04].

Speed modes shape prompting and workflow design: API Fast mode trades higher per-token cost for faster, more consistent response times (chat apps, coding tools); Ultrafast accelerates token generation independently of reasoning effort and is available for GPT-6 Astra in both Codex and the API [source: https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04].

## Codex: Clarification and Steering

Long-running Codex tasks can surface decisions not anticipated in the initial prompt. With GPT-6 Astra, Codex can ask for clarification while it works; resolve questions that affect the next step and specify which independent work can continue while you decide. If you will be away, tell Codex which tasks can proceed and when to pause for your answer. Steering the active task with new information—explaining what should change and what should stay the same—helps avoid spending time on an approach that no longer meets your needs [source: https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04].

## GPT-6 Model Selection and Workload Matching

Model choice and reasoning level form an intelligence/price tradeoff. GPT-6 Astra is recommended for the hardest reasoning work where maximum intelligence is needed; GPT-6.1 Sol for complex coding, research, and computer use; GPT-6 Luna for focused tasks at scale and everyday repeated work with a clear goal, such as extracting invoice fields, classifying requests, or producing structured summaries [source: https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04].

## Production Workflow and Context Management

Production best practices from the guide [source: https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04]:

- **Prompt caching** reuses shared context; cached input tokens cost up to 95% less than uncached, depending on model. Place stable instructions before changing task details; keep tool definitions consistent.
- **Compaction** reduces context size in long conversations while preserving continuation state. Cut unneeded context, keep needed evidence, and run independent tasks in parallel.
- **Mid-turn steering** (API): send corrections via the Responses WebSocket API during a run; updates queue without cancelling running tools or undoing completed actions.
- **Async tool calling** (API): the model continues independent work while the app runs a slower task; return results when ready and gate dependent work on them.
- **Multi-agent** (Responses API, beta): GPT-6.1 Sol delegates independent subtasks to subagents and combines findings.
- **Computer use**: GPT-6 Astra, GPT-6.1 Sol, and GPT-6 Luna can interact with websites and desktop apps (including those without an API). Prefer an API or connected tool when available; use computer use for screen reading, clicking, or form-filling. Playwright for browsers; PyAutoGUI for desktop.

## Prompting Principles for GPT-6

The guide recommends four prompting adjustments for the GPT-6 family [source: https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04]:

- **Clear assignment**: State the desired result, target audience, relevant context and constraints, and what counts as done.
- **Decision boundaries**: Specify which actions the model can take independently and which require your approval, replacing blanket "always ask" rules with explicit boundaries.
- **Prescriptive persistence**: Define "done" as implementing the change, running it, inspecting the result, and fixing failures; identify decisions that need your review.
- **Useful response**: Specify what a useful response looks like and which decisions the model can make on its own.

Because GPT-6 models handle nuance and ambiguity better than earlier generations, overly specific guidance that once helped can now hinder results (Eric Provencher, Developer Experience, OpenAI) [source: https://openai.com/index/practical-guide-building-gpt-6/ @2026-10-04].
## Skills Optimization

Skills are defined as prompts stored as Markdown files that can be packaged with resources and bundled scripts, primarily useful for specific workflows or app usage [source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra]. For GPT-6 Astra, the following principles apply:

*   **Progressive Disclosure:** Useful skills should use progressive disclosure, where the root document acts as a minimal router pointing to supporting docs and scripts to save context [source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra].
*   **Concise Descriptions:** Skill descriptions should be as short as possible while clearly defining when the model should use them to avoid over-emphasis or contradiction [source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra].
*   **Avoid Over-Specificity:** Overly specific guidance in skills can hinder GPT-6 Astra's results because the model has improved at understanding nuance and ambiguity [source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra].
*   **Context Management:** Loading too many skills causes Codex to shorten their descriptions to fit context, reducing the model's ability to distinguish which skill to use [source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra].
*   **Model-Specific Review:** Repository skills should be reviewed for which models will use them, as guidance helpful for models like Sol or Luna may overconstrain GPT-6 Astra [source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra].

## AGENTS.md Best Practices

`AGENTS.md` is an open format for guiding coding agents, used by over 60,000 open-source projects and stewarded by the Agentic AI Foundation under the Linux Foundation [source: https://agents.md/]. It serves as a dedicated, predictable place to provide context and instructions for AI coding agents, complementing `README.md` files which are for humans [source: https://agents.md/].

### Structural Guidelines

*   **Placement:** Create the file at the root of the repository; most coding agents can scaffold one if requested [source: https://agents.md/].
*   **Nested Files:** For large monorepos, nested `AGENTS.md` files can be placed in subprojects; agents read the nearest file in the directory tree, so the closest one takes precedence [source: https://agents.md/].
*   **Conflict Resolution:** If instructions in `AGENTS.md` files conflict, the closest `AGENTS.md` to the edited file wins, and explicit user chat prompts override everything [source: https://agents.md/].
*   **Format:** `AGENTS.md` has no required fields; it is standard Markdown and the agent parses the provided text [source: https://agents.md/].

### Content Strategy for GPT-6 Astra

*   **Contextual Instructions:** `AGENTS.md` instructions should be contextual rather than requiring a full repo map or stack of docs before every edit, as GPT-6 Astra can determine what it needs to read [source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra].
*   **Avoid Redundant Testing Instructions:** GPT-6 Astra runs tests and checks its work on its own, so explicit instructions to do so in `AGENTS.md` can lead to unnecessary testing [source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra].
*   **Permission Grants:** `AGENTS.md` can be used to give GPT-6 Astra permission for specific safe workflows, such as running local tests without asking for approval at each step [source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra].
*   **Recommended Sections:** Recommended sections include project overview, build and test commands, code style guidelines, testing instructions, and security considerations [source: https://agents.md/].

## Behavioral Adjustments

*   **Decision Boundaries:** Strong boundary language added for previous models may cause GPT-6 Astra to stop work prematurely because it has better judgment and alignment [source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra].
*   **Persistence and Completion:** GPT-6 Astra is more tentative about stopping than GPT-5.6 Sol, often returning for review after a first implementation; defining completion criteria in the request helps it continue until fully done [source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra].

## Reasoning Model Prompting (o-series)

OpenAI offers two model families that behave differently: reasoning models (e.g., o3, o4-mini) and GPT models (e.g., GPT-4.1); one family is not better than the other—they are simply different [source: https://developers.openai.com/api/docs/guides/reasoning-best-practices]. While GPT-6 Astra guidance focuses on reduced scaffolding, o-series models have specific prompting requirements due to their internal reasoning processes.

### Prompting Techniques

*   **Avoid Explicit Reasoning Prompts:** Since reasoning models perform reasoning internally, prompting them to "think step by step" or "explain your reasoning" is unnecessary and may sometimes hinder performance [source: https://developers.openai.com/api/docs/guides/reasoning-best-practices].
*   **Markdown Formatting Control:** Starting with `o1-2024-12-17`, reasoning models in the API avoid generating responses with markdown formatting by default. To signal that markdown is desired, include the string `Formatting re-enabled` on the first line of the developer message [source: https://developers.openai.com/api/docs/guides/reasoning-best-practices].

### Model Selection Guidance

*   **GPT Models:** Best for speed, cost efficiency, and straightforward, well-defined tasks [source: https://developers.openai.com/api/docs/guides/reasoning-best-practices].
*   **o-series Models:** Best for accuracy, reliability, and complex, multistep problem-solving where the model must work through ambiguity [source: https://developers.openai.com/api/docs/guides/reasoning-best-practices].
*   **Combined Workflow Pattern:** Most AI workflows use both families together—o-series for agentic planning and decision-making, GPT series for task execution [source: https://developers.openai.com/api/docs/guides/reasoning-best-practices].

### API Behavior: Persisted Reasoning Items

The handling of persisted reasoning items in the Responses API varies by model generation:

*   **o1, o3-mini, o1-mini, o1-preview:** Reasoning items were always ignored in follow-up API requests, even if included in input items [source: https://developers.openai.com/api/docs/guides/reasoning-best-practices].
*   **o3 and o4-mini:** Some reasoning items adjacent to function calls are included in the model’s context to improve performance while using the least amount of reasoning tokens [source: https://developers.openai.com/api/docs/guides/reasoning-best-practices].

### Capabilities and Use Cases

*   **Strategizing and Expert Domains:** O-series reasoning models are trained to think longer and harder about complex tasks, making them effective at strategizing, planning solutions to complex problems, and making decisions based on large volumes of ambiguous information. They execute tasks with high accuracy and precision in domains requiring human expertise—math, science, engineering, financial services, and legal services. They are also effective at understanding user intent from limited or disparate information and often ask clarifying questions before making guesses [source: https://developers.openai.com/api/docs/guides/reasoning-best-practices].
*   **Visual Reasoning:** As of the documentation's publication, o1 is the only reasoning model supporting vision capabilities, distinguishing it from GPT-4o by handling challenging visuals like ambiguous charts or poor-quality photos. In internal testing, o1 identified fixtures and materials from detailed architectural drawings to generate a bill of materials, and applied a legend from one page to another without explicit instructions (e.g., recognizing "PT" as pressure treated) [source: https://developers.openai.com/api/docs/guides/reasoning-best-practices].
*   **Evaluation:** Reasoning models perform well as LLM-as-a-judge; one Braintrust customer observed an F1 score improvement from 0.12 (GPT-4o) to 0.74 (o1) when assessing summary quality in a healthcare eval pipeline [source: https://developers.openai.com/api/docs/guides/reasoning-best-practices].

*   **Information Retrieval:** When passing large amounts of unstructured information, reasoning models are effective at understanding and pulling out only the most relevant information to answer a question [source: https://developers.openai.com/api/docs/guides/reasoning-best-practices].
## Ecosystem Compatibility

`AGENTS.md` is compatible with a wide ecosystem of AI coding agents and tools, including OpenAI Codex, Google Jules, Cursor, GitHub Copilot, and VS Code [source: https://agents.md/]. Specific configurations include:

*   **Aider:** Add `read: AGENTS.md` to `.aider.conf.yml` [source: https://agents.md/].
*   **Gemini CLI:** Set `{ "context": { "fileName": "AGENTS.md" } }` in `.gemini/settings.json` [source: https://agents.md/].

## Related Pages

*   [[gpt-6-astra]]
*   [[openai-responses-api]]