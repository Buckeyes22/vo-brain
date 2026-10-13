"""The draft scope check matches whole URLs in new or changed paragraphs."""

import pytest

from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import JobState, SourceRecord

BASE = "https://developers.openai.com/api/docs"
POLICY = {"include": [{"url": f"{BASE}/models/gpt-6-astra"}, {"url": f"{BASE}/guides/tools-computer-use"}]}


def make_state():
    return JobState(
        "",
        "",
        "openai",
        source_policy=POLICY,
        sources=[
            SourceRecord("allowed-1", "url", "astra", url=f"{BASE}/models/gpt-6-astra"),
            SourceRecord("allowed-2", "url", "cu", url=f"{BASE}/guides/tools-computer-use"),
            # Discovered but rejected by the include-only policy; each is a string
            # prefix of an allowed URL above.
            SourceRecord("rejected-1", "url", "models", url=f"{BASE}/models", status="skipped"),
            SourceRecord("rejected-2", "url", "tools", url=f"{BASE}/guides/tools", status="skipped"),
        ],
    )


def scope(body, original=None):
    return Pipeline._scope_failures(object(), make_state(), body, original)


def test_citing_an_allowed_url_that_extends_a_rejected_url_does_not_block():
    body = f"# Astra\n\nSee [model]({BASE}/models/gpt-6-astra) and {BASE}/guides/tools-computer-use.\n"
    assert scope(body) == []
    assert scope(body, "# Astra\n\nOld.\n") == []


def test_rejected_url_in_unchanged_canonical_paragraph_never_blocks():
    legacy = f"Legacy fact, see {BASE}/models for the catalogue."
    original = f"# Page\n\n{legacy}\n\nOther.\n"
    body = f"# Page\n\n{legacy}\n\nOther.\n\nA new unrelated paragraph.\n"
    assert scope(body, original) == []


def test_rejected_url_already_cited_on_canonical_page_survives_a_changed_paragraph():
    original = f"# Page\n\nLegacy fact, see {BASE}/models for the catalogue.\n"
    body = f"# Page\n\nLegacy fact, see {BASE}/models for the catalogue. Plus one more sentence.\n"
    assert scope(body, original) == []


def test_new_citation_of_rejected_url_blocks_and_names_the_url():
    original = "# Page\n\nOld.\n"
    body = f"# Page\n\nOld.\n\nNew fact from {BASE}/guides/tools today.\n"
    failures = scope(body, original)
    assert [f.check for f in failures] == ["scope"]
    assert f"{BASE}/guides/tools" in failures[0].detail
    assert f"{BASE}/guides/tools-computer-use" not in failures[0].detail


def test_url_inside_approved_claim_text_that_policy_rejects_blocks_with_the_url():
    # An admitted claim's own text names a discovered, rejected URL.
    claim = f"The safeguards are described at {BASE}/models."
    failures = scope(f"# GPT\n\n{claim}\n", "")
    assert len(failures) == 1 and failures[0].check == "scope"
    assert f"{BASE}/models" in failures[0].detail


def test_markdown_link_and_trailing_punctuation_delimit_the_url():
    body = f"# P\n\nSee [guide]({BASE}/guides/tools-computer-use), or ({BASE}/models/gpt-6-astra).\n"
    assert scope(body) == []
    bad = f"# P\n\nSee [guide]({BASE}/models).\n"
    assert f"{BASE}/models" in scope(bad)[0].detail


def test_without_original_body_every_paragraph_counts_as_new():
    assert len(scope(f"# P\n\nSee {BASE}/models.\n")) == 1


def test_new_paragraph_citing_a_rejected_url_blocks_even_when_the_page_cites_it_elsewhere():
    original = f"# Page\n\nLegacy fact, see {BASE}/models for the catalogue.\n"
    body = f"{original}\nA new admitted claim says the list lives at {BASE}/models.\n"
    failures = scope(body, original)
    assert len(failures) == 1 and f"{BASE}/models" in failures[0].detail


def test_replaced_unrelated_paragraph_has_no_original_to_inherit_a_rejected_url_from():
    original = f"# Page\n\nLegacy fact, see {BASE}/models for the catalogue.\n\nKeep.\n"
    body = f"# Page\n\nEntirely different statement about billing tiers at {BASE}/models.\n\nKeep.\n"
    assert len(scope(body, original)) == 1


def test_changed_paragraph_inherits_only_from_its_own_original():
    legacy = f"Legacy fact, see {BASE}/models for the catalogue."
    other = f"Second fact, see {BASE}/guides/tools for tooling."
    original = f"# Page\n\n{legacy}\n\n{other}\n"
    # Edit the first paragraph and add a new sentence citing the second paragraph's URL:
    # the new sentence is not legacy text for that URL, so it blocks.
    body = (
        f"# Page\n\nLegacy fact, see {BASE}/models for the catalogue. "
        f"Separately, pricing for hosted tool calls is published at {BASE}/guides/tools today.\n\n{other}\n"
    )
    failures = scope(body, original)
    assert len(failures) == 1 and f"{BASE}/guides/tools" in failures[0].detail


def test_moved_unchanged_paragraph_is_not_checked():
    legacy = f"Legacy fact, see {BASE}/models for the catalogue."
    assert scope(f"# Page\n\nFirst.\n\n{legacy}\n", f"# Page\n\n{legacy}\n\nFirst.\n") == []


WIKI = "https://en.wikipedia.org/wiki"


def wiki_scope(body, original=None):
    state = JobState(
        "",
        "",
        "wiki",
        source_policy={"include": [{"url": f"{WIKI}/Allowed_(thing)"}]},
        sources=[
            SourceRecord("ok", "url", "ok", url=f"{WIKI}/Allowed_(thing)"),
            SourceRecord("bad", "url", "bad", url=f"{WIKI}/GPT_(model)", status="skipped"),
            SourceRecord("bad-prefix", "url", "bad", url=f"{WIKI}/GPT", status="skipped"),
        ],
    )
    return Pipeline._scope_failures(object(), state, body, original)


def test_rejected_url_containing_balanced_parentheses_blocks_in_every_surrounding():
    for text in (
        f"New claim from {WIKI}/GPT_(model) today.",
        f"New claim ({WIKI}/GPT_(model)).",
        f"New claim [GPT]({WIKI}/GPT_(model)).",
        f"New claim <{WIKI}/GPT_(model)>, really.",
    ):
        failures = wiki_scope(f"# P\n\n{text}\n", "")
        assert len(failures) == 1, text
        assert f"{WIKI}/GPT_(model)" in failures[0].detail, text


def test_parenthesised_url_is_neither_truncated_nor_confused_with_its_prefix():
    # The allowed URL and a rejected shorter URL share a prefix with parenthesised neighbours.
    assert wiki_scope(f"# P\n\nSee [x]({WIKI}/Allowed_(thing)) and ({WIKI}/Allowed_(thing)).\n", "") == []
    failures = wiki_scope(f"# P\n\nSee {WIKI}/GPT_(model) only.\n", "")
    assert [f.detail.count(f"{WIKI}/GPT ") for f in failures] == [0]


# Shapes taken from real saved drafts.
SOL = f"{BASE}/models/gpt-6-sol"
SELECT = f"{BASE}/guides/model-selection"
PRICING = "https://aws.amazon.com/bedrock/pricing/"
AZURE = "https://azure.microsoft.com/en-us/pricing/details/azure-openai/"


def lineup_state(extra_policy_urls=()):
    return JobState(
        "",
        "",
        "openai",
        source_policy={"include": [{"url": f"{BASE}/models/gpt-6-astra"}, *({"url": u} for u in extra_policy_urls)]},
        sources=[
            SourceRecord("astra", "url", "astra", url=f"{BASE}/models/gpt-6-astra"),
            SourceRecord("sol", "url", "sol", url=SOL, status="skipped"),
            SourceRecord("select", "url", "select", url=SELECT, status="skipped"),
            SourceRecord("aws", "url", "aws", url=PRICING, status="skipped"),
            SourceRecord("azure", "url", "azure", url=AZURE, status="skipped"),
        ],
    )


LEGACY_SOL = (
    "### GPT-6.1 Sol\n\n"
    f"It is the newer model compared to GPT-6 Sol [source: {SOL}]. "
    f"It is optimized for complex tasks where time and cost management are required [source: {SELECT}].\n"
)
MERGED = (
    "### Model comparison\n\n"
    "In Codex, Astra can ask for clarification before it starts a long task and then continues on its own. "
    "Teams usually pair it with a reviewer model for sensitive changes, and the default effort is medium. "
    "Larger context windows change how much of a repository it can hold at once. "
    f"It is the newer model compared to GPT-6 Sol [source: {SOL}]. "
    f"It is optimized for complex tasks where time and cost management are required [source: {SELECT}]. "
    "Astra, by contrast, targets long-running agentic work with many tool calls and parallel steps. "
    "Its pricing is tiered by effort level, and batch requests are discounted when latency is not a concern. "
    "Both models share the same tool schema and the same structured output guarantees across the API surface. "
    "Migration notes recommend replaying a representative sample of production traffic before switching defaults.\n"
)


def lineup(body, original):
    return Pipeline._scope_failures(object(), lineup_state(), body, original)


def test_legacy_sentences_merged_into_a_larger_new_paragraph_do_not_block():
    # Whole-paragraph similarity of the merged block to the legacy paragraph is far below one half.
    import difflib

    legacy_paragraph = LEGACY_SOL.split("\n\n", 1)[1].strip()
    merged_paragraph = MERGED.split("\n\n", 1)[1].strip()
    assert difflib.SequenceMatcher(None, legacy_paragraph, merged_paragraph, autojunk=False).ratio() < 0.5
    assert lineup(MERGED, LEGACY_SOL) == []


def test_a_new_sentence_citing_a_rejected_url_still_blocks_inside_the_merged_block():
    body = MERGED.replace(
        "Astra, by contrast", f"A brand new claim says Sol pricing is listed at {SOL}. Astra, by contrast"
    )
    failures = lineup(body, LEGACY_SOL)
    assert [f.check for f in failures] == ["scope"] and SOL in failures[0].detail
    assert SELECT not in failures[0].detail


def test_pricing_citations_on_a_performance_modes_page_still_block():
    original = "# Performance modes\n\nPriority processing costs more.\n"
    body = (
        original + "\nOpenAI models on Amazon Bedrock and Microsoft Azure are billed through those cloud services. "
        f"See [Amazon Bedrock pricing]({PRICING}) and [Microsoft Azure pricing]({AZURE}).\n"
    )
    details = " ".join(f.detail for f in lineup(body, original))
    assert PRICING in details and AZURE in details


def test_deploymentsafety_citation_on_both_94e3_page_shapes_still_blocks():
    safeguards = "https://deploymentsafety.openai.com/gpt-6-astra/safeguards"
    state = JobState(
        "",
        "",
        "gpt-6-1-sol",
        source_policy={"include": [{"url": "https://deploymentsafety.openai.com/gpt-6-1-sol"}]},
        sources=[
            SourceRecord("sol", "url", "sol", url="https://deploymentsafety.openai.com/gpt-6-1-sol"),
            SourceRecord("safe", "url", "safe", url=safeguards, status="skipped"),
        ],
    )
    claim = f"GPT-6.1 Sol safeguards build on the Astra safeguards described at {safeguards}."
    created = Pipeline._scope_failures(object(), state, f"# GPT-6.1 Sol\n\n{claim}\n", "")
    updated = Pipeline._scope_failures(
        object(), state, f"# Model lineup\n\nOld.\n\n{claim}\n", "# Model lineup\n\nOld.\n"
    )
    for failures in (created, updated):
        assert len(failures) == 1 and safeguards in failures[0].detail


def test_reworded_legacy_sentence_is_a_new_claim_and_cannot_carry_the_rejected_url():
    original = f"# P\n\nLegacy fact, see {BASE}/models for the complete catalogue of models.\n"
    body = f"# P\n\nLegacy fact, see {BASE}/models for the complete catalogue of every model.\n"
    failures = scope(body, original)
    assert len(failures) == 1 and f"{BASE}/models" in failures[0].detail


def test_sentence_citing_a_rejected_url_only_legacy_elsewhere_blocks_even_when_prose_is_unrelated():
    original = f"# P\n\nLegacy fact, see {BASE}/models for the catalogue.\n"
    body = f"{original}\nBilling tiers are summarised on a separate pricing screen, see {BASE}/models.\n"
    assert len(scope(body, original)) == 1


LUNA = f"* **GPT-6 Luna:** Smart and efficient model for scoped tasks [source: {SELECT}].\n"
LUNA_PAGE = f"# Models\n\n{LUNA}* **GPT-6 Astra:** Highest capability [source: {SELECT}].\n"
CONTEXT = (
    f"# P\n\nThe context window of GPT-6 Astra is 400,000 tokens according to the model page [source: {SELECT}].\n"
)


def test_rewritten_list_item_blocks_and_a_new_item_blocks():
    reworded = LUNA_PAGE.replace(
        "Smart and efficient model for scoped tasks", "Fastest and most cost-effective model for focused work"
    )
    failures = lineup(reworded, LUNA_PAGE)
    assert len(failures) == 1 and SELECT in failures[0].detail
    added = LUNA_PAGE + f"* **GPT-6 Mini:** A brand new smaller model [source: {SELECT}].\n"
    failures = lineup(added, LUNA_PAGE)
    assert len(failures) == 1 and SELECT in failures[0].detail


@pytest.mark.parametrize(
    "name,body,original",
    [  # noqa: E501
        (
            "same label, new fact",
            LUNA_PAGE.replace(
                "Smart and efficient model for scoped tasks",
                "Costs $0.40 per million input tokens and was retired in March 2026",
            ),
            LUNA_PAGE,
        ),
        (
            "same colon label, new fact",
            f"# P\n\n- Pricing: Batch is 90% cheaper and has a 1-hour SLA [source: {SELECT}].\n",
            f"# P\n\n- Pricing: Standard tier is the default [source: {SELECT}].\n",
        ),
        (
            "extra sentence in the same item",
            LUNA_PAGE.replace("scoped tasks [source", "scoped tasks. It supports 2M context [source"),
            LUNA_PAGE,
        ),
        ("changed number", CONTEXT.replace("400,000", "2,000,000"), CONTEXT),
        ("negation", CONTEXT.replace("is 400,000", "is not 400,000"), CONTEXT),
        ("different model name", CONTEXT.replace("GPT-6 Astra", "GPT-6 Luna"), CONTEXT),
        (
            "half the wording plus a new claim",
            f"# P\n\nThe context window of GPT-6 Astra is 2,000,000 tokens, and Astra is deprecated for all customers in the EU [source: {SELECT}].\n",
            CONTEXT,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and len(value) < 40 else None,
)
def test_a_reworded_or_extended_sentence_cannot_carry_a_rejected_citation(name, body, original):
    failures = lineup(body, original)
    assert len(failures) == 1 and SELECT in failures[0].detail, name


def test_verbatim_legacy_sentence_stays_exempt_whatever_the_formatting():
    original = LUNA_PAGE
    # whitespace, emphasis and code markers, and citation formatting do not make a sentence new
    reformatted = (
        "# Models\n\n"
        f"*   GPT-6 Luna:   Smart and efficient model for scoped tasks [source]({SELECT}).\n"
        f"* **GPT-6 Astra:** Highest   capability [source: {SELECT}].\n"
    )
    assert lineup(reformatted, original) == []
    legacy = f"# P\n\nUse `reasoning_effort` for _hard_ tasks [source: {SELECT}].\n"
    again = f"# P\n\nUse reasoning_effort for hard tasks   [source: {SELECT}].\n\nA new unrelated paragraph.\n"
    assert lineup(again, legacy) == []


def one(sentence_before, sentence_after):
    original = f"# P\n\n{sentence_before} [source: {SELECT}].\n"
    return lineup(f"# P\n\n{sentence_after} [source: {SELECT}].\n", original)


@pytest.mark.parametrize(
    "before,after",
    [
        ("Set max_tokens to 5", "Set maxtokens to 5"),
        ("Multiply 2*3 gives 6", "Multiply 23 gives 6"),
        ("Use snake_case_names here", "Use snakecasenames here"),
        ("Call get_user and set_user", "Call getuser and setuser"),
    ],
)
def test_markers_inside_identifiers_and_arithmetic_are_content_not_formatting(before, after):
    failures = one(before, after)
    assert len(failures) == 1 and SELECT in failures[0].detail
    assert one(before, before) == []


@pytest.mark.parametrize(
    "before,after",
    [
        ("This is **bold** and _it_ here", "This is bold and it here"),
        ("Use `max_tokens` for ***both*** limits", "Use max_tokens for both limits"),
        ("This is **_nested_** text", "This is nested text"),
        ("Multiply 2*3 and 4*5 here", "Multiply 2*3 and 4*5 here"),
    ],
)
def test_paired_emphasis_and_code_markers_at_word_boundaries_are_formatting(before, after):
    assert one(before, after) == []


def test_unpaired_or_spanning_markers_are_not_stripped():
    # An opener with no closer on the line is content.
    assert len(one("Note the *star here", "Note the star here")) == 1
    assert len(one("Pay 5 * 3 now", "Pay 5 3 now")) == 1


@pytest.mark.parametrize(
    "before,after",
    [
        ("Edit __init__.py first", "Edit init.py first"),
        ("Run the __main__ module", "Run the main module"),
    ],
)
def test_dunder_names_are_content_not_underscore_emphasis(before, after):
    failures = one(before, after)
    assert len(failures) == 1 and SELECT in failures[0].detail
    assert one(before, before) == []


@pytest.mark.parametrize(
    "before,after",
    [
        ("This is _it_ here", "This is it here"),
        ("This is **bold** here", "This is bold here"),
    ],
)
def test_single_underscore_and_double_star_emphasis_still_normalize(before, after):
    assert one(before, after) == []


@pytest.mark.parametrize(
    "before,after",
    [
        ("* Smart model for scoped tasks", "- Smart model for scoped tasks"),
        ("- Smart model for scoped tasks", "+ Smart model for scoped tasks"),
        ("* Smart model for scoped tasks", "1. Smart model for scoped tasks"),
        ("2) Smart model for scoped tasks", "* Smart model for scoped tasks"),
        ("* **Luna:** Smart model", "- Luna: Smart model"),
        ("Smart model for scoped tasks", "- Smart model for scoped tasks"),
    ],
)
def test_a_bullet_style_change_alone_stays_exempt(before, after):
    original = f"# P\n\n{before} [source: {SELECT}].\n"
    assert lineup(f"# P\n\n{after} [source: {SELECT}].\n", original) == []


def test_bullet_marker_normalization_does_not_hide_a_changed_link_label_or_wording():
    original = f"# P\n\n* Smart model for scoped tasks [source: {SELECT}].\n"
    assert len(lineup(f"# P\n\n- Smart model for scoped tasks [GPT-6 selection]({SELECT}).\n", original)) == 1
    assert len(lineup(f"# P\n\n- Smart model for scoped chores [source: {SELECT}].\n", original)) == 1
