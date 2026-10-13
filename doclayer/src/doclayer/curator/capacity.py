"""Page scope and editorial-review capacity checks.

Everything here is a pure computation over its arguments: nothing mutates a
job, a change or an evidence record. The pipeline calls these functions and
persists results only at its own checkpoint sites.

Two concerns live here because both decide, before expensive model work,
whether a draft can be accepted:

- scope: does a draft newly cite a source the import policy rejects?
- capacity: can the editorial grounding review hold a page's evidence?
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from doclayer.curator import curate, editorial, policy
from doclayer.curator.editorial import EditorialContextOverflow
from doclayer.curator.state import Evidence, PageChange
from doclayer.curator.validate import Failure

# Drafts made after evidence was bounded to the review budget carry this stamp
# in state.evidence_selection; older drafts do not and must be redrafted when
# their full record set cannot be reviewed.
CAPACITY_GENERATION = 1


# --------------------------------------------------------------------------- #
# scope
# --------------------------------------------------------------------------- #


def url_cited(text: str, url: str) -> bool:
    """Whole-URL occurrence of a known source URL in text.

    The URL must not continue into a longer URL (path, query or word
    characters), so a rejected prefix of an allowed URL never matches, while
    parentheses, quotes and brackets inside the URL itself are matched exactly.
    """
    if url not in text:
        return False
    pattern = r"(?<![\w/%~.:=&?+-])" + re.escape(url) + r"(?![\w/%~=&+-]|[?.,;:!*]\w)"
    return re.search(pattern, text) is not None


def raw_sentences(text: str) -> list[str]:
    """Sentences and lines exactly as written (internal whitespace kept)."""
    return [piece.strip() for piece in re.split(r"(?<=[.!?])[ \t]+|\n", text) if piece.strip()]


def sentences(text: str) -> list[str]:
    """Sentences and lines of text, whitespace-normalized, for verbatim comparison."""
    normalized = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return [piece.strip() for piece in re.split(r"(?<=[.!?])\s+|\n", normalized) if piece.strip()]


_BULLET = re.compile(r"^\s*(?:[*+-]|\d+[.)])(?:\s+|$)")
_CODE = re.compile(r"`([^`\n]+)`")
# Paired emphasis only: the opener follows a non-word character and precedes a
# non-space, the closer follows a non-space and precedes a non-word character,
# and the pair stays on one line. Markers inside identifiers or arithmetic
# (max_tokens, 2*3) are therefore content, not formatting. Underscore emphasis
# is a single marker on each side, so dunder names (__init__) stay content.
_STAR = re.compile(r"(?<![\w*])(\*{1,3})(?=\S)([^\n]+?)(?<=\S)\1(?![\w*])")
_UNDERSCORE = re.compile(r"(?<![\w_])(_)(?=[^\s_])([^\n]+?)(?<=[^\s_])\1(?![\w_])")


def verbatim_form(sentence: str) -> str:
    """A sentence reduced to its wording, so formatting differences are not edits.

    Citations are canonicalized (``[source: U]`` and a markdown link to U are the
    same citation); a list bullet at the start, paired emphasis and paired code
    markers, and whitespace are dropped. Words, numbers, identifiers, link
    labels and URLs are untouched."""
    text = re.sub(r"\[source:\s*(https?://[^\]\s]+)\s*\]", r"<\1>", sentence)
    text = re.sub(
        r"\[([^\]]*)\]\((https?://[^)\s]+(?:\([^)\s]*\)[^)\s]*)*)\)",
        lambda m: (
            (
                ""
                if m.group(1).strip().lower().startswith("source") or m.group(1).strip() == m.group(2)
                else m.group(1) + " "
            )
            + f"<{m.group(2)}>"
        ),
        text,
    )
    text = _BULLET.sub("", text)
    text = _CODE.sub(r"\1", text)
    for pattern in (_STAR, _UNDERSCORE):
        previous = None
        while previous != text:  # nested pairs such as **_x_**
            previous, text = text, pattern.sub(r"\2", text)
    return re.sub(r"\s+", " ", text).strip()


SENTENCE_MARKER = "sentence: "
# Failure.code of a draft citing a source the import policy rejects.
SCOPE_CITATION = "scope-citation"


def _heading_above(paragraph: str, sentence: str, before: str) -> str:
    """The innermost heading above `sentence`: the nearest heading line that starts
    before the sentence's own start, else the one in force when the paragraph began.

    The sentence is located whitespace-insensitively (it may wrap across lines or
    carry doubled spaces), the way repair locates it in the draft unit. A sentence
    that is not in the paragraph has no heading above it: the label is empty.
    """
    words = sentence.split()
    found = re.search(r"\s+".join(re.escape(word) for word in words), paragraph) if words else None
    if not found:
        return ""
    heading = before
    for line in re.finditer(r"^#{1,6}\s.*$", paragraph, re.M):
        if line.start() <= found.start():
            heading = line.group(0).lstrip("#").strip()
    return heading


def scope_failures(
    source_policy: dict[str, Any], source_urls: list[str | None], body: str, original_body: str | None = None
) -> list[Failure]:
    """Flag newly introduced citations of sources the policy rejects.

    Source URLs are matched as whole URLs, never as substrings, and only in
    paragraphs that are new or changed relative to the page's original body.
    Unchanged paragraphs are never checked. In a changed paragraph a rejected
    URL is exempt only when every sentence or line that cites it appears
    verbatim in the original page body, wherever the drafter moved or merged
    it (whitespace, markdown emphasis and code markers, and citation
    formatting are normalized away). A reworded sentence is a new claim and
    cannot carry a rejected citation; neither can a new sentence, wherever it
    is placed and whatever else the page cites.
    """
    failures = []
    if not policy.claim_decision(None, body, {}, source_policy)[0]:
        failures.append(Failure("scope", "draft violates global semantic source policy"))
    urls = list(dict.fromkeys(url for url in source_urls if url))
    paragraphs = re.split(r"\n\s*\n", body)
    original_set = {p.strip() for p in re.split(r"\n\s*\n", original_body or "")}
    verbatim = {verbatim_form(x) for x in sentences(original_body or "")}
    heading = ""
    for paragraph in paragraphs:
        heading_before = heading
        for line in paragraph.splitlines():
            if re.match(r"#{1,6}\s", line):
                heading = line.lstrip("#").strip()
        if paragraph.strip() in original_set:
            continue
        for url in urls:
            if not url_cited(paragraph, url):
                continue
            citing = [x for x in raw_sentences(paragraph) if url_cited(x, url)] or [paragraph.strip()]
            # Quoted as written: repair targeting finds the sentence in the draft unit.
            new_citing = [x for x in citing if verbatim_form(x) not in verbatim]
            if not new_citing:
                continue
            allowed, reason = policy.claim_decision(url, paragraph, {}, source_policy)
            if not allowed:
                # Name the sentence and its unit: repair edits the unit holding a new
                # citing sentence, never every unit that cites the same URL.
                label = _heading_above(paragraph, new_citing[0], heading_before)
                failures.append(
                    Failure(
                        "scope",
                        f"draft cites excluded or semantically restricted source {url} ({reason}); "
                        f"unit: {label or '(untitled)'}; {SENTENCE_MARKER}{json.dumps(new_citing[0][:200], ensure_ascii=False)}",
                        code=SCOPE_CITATION,
                    )
                )
    return failures


# --------------------------------------------------------------------------- #
# editorial review size probe
# --------------------------------------------------------------------------- #


class _ReviewReached(Exception):
    """The editorial review accepted its input and is about to call a model."""


class _ReviewTripwire:
    """A client that proves the size check passed instead of making a call."""

    async def _reached(self, *args, **kwargs):
        raise _ReviewReached()

    chat = chat_json = chat_verdicts = chat_bounded_json = _reached


def _drive_without_suspension(coroutine) -> None:
    """Run a coroutine that must finish (or raise) without awaiting real I/O."""
    try:
        coroutine.send(None)
    except StopIteration:
        return
    coroutine.close()
    raise RuntimeError("editorial review suspended during a size probe; the probe cannot vouch for its input")


OriginalPage = tuple[dict[str, Any], str]  # (frontmatter, body) of the page at the job's base commit


class ReviewCapacity:
    """Size checks that run the editorial review itself up to its first model call.

    One instance fixes the budget, intent and source policy, so the fail-fast
    before drafting and the drafting-evidence bound cannot disagree. The
    optional ``cache`` is a plain memo dict owned by the caller.
    """

    def __init__(
        self, budget: int, intent: str, source_policy: dict[str, Any] | None, cache: dict | None = None
    ) -> None:
        self.budget = budget
        self.intent = intent
        self.source_policy = source_policy or {}
        self.cache = cache if cache is not None else {}

    def fits(
        self,
        change: PageChange,
        original: OriginalPage,
        records: list[Evidence],
        *,
        grow: bool = False,
        actual: bool = False,
    ) -> bool:
        """Would the editorial grounding review accept these records? (Deliberately conservative.)

        The planner keeps the pre-split evidence rule (``capacity_probe``):
        uncited text must fit beside every record relevant to it, although the
        real review no longer grounds uncited sentences in split mode. It may
        therefore refuse records the review would accept, never the reverse.

        Every record is made relevant to one page section of realistic size
        (with grow, the text of every record may land in that section as well;
        with actual, the change's real draft is reviewed instead of that
        stand-in): the review must hold the complete relevant record set per
        section, and shared vocabulary makes nearly every record relevant to
        every section.
        """
        if not records:
            return True
        original_meta, original_body = original
        key = (
            change.path,
            change.title,
            self.budget,
            grow,
            actual and hashlib.sha256(change.draft_body.encode()).hexdigest(),
            hashlib.sha256(original_body.encode()).hexdigest(),
            hashlib.sha256(json.dumps([self.intent, self.source_policy], sort_keys=True).encode()).hexdigest(),
            tuple((curate.evidence_id(r), len(r.text), len(r.quote or "")) for r in records),
        )
        if key in self.cache:
            return self.cache[key]
        growth = min(self.budget // 2, sum(len(r.text) for r in records)) if grow else 0
        documents = [curate._terms(r.text + " " + r.locator + " " + str(r.applicability)) for r in records]
        counts: dict[str, int] = {}
        for terms in documents:
            for term in terms:
                counts[term] = counts.get(term, 0) + 1
        chosen: list[str] = []
        for terms in documents:
            if terms and not terms.intersection(chosen):
                chosen.append(max(sorted(terms), key=lambda term: counts[term]))
        units = [u for u in re.split(r"(?m)^(?=#{1,6} )", original_body) if u.strip()]
        largest = max(units, key=len)[: max(1, self.budget // 4)] if units else ""
        probe = copy.copy(change)
        probe.check_progress = {}
        if not actual:
            probe.draft_meta = dict(original_meta)
            probe.draft_body = (
                f"# {change.title or Path(change.path).stem}\n\n{largest}\n\n{' '.join(chosen)}\n{'x' * growth}\n"
            )
            probe.claim_ids = []
        try:
            _drive_without_suspension(
                editorial.review(
                    _ReviewTripwire(),
                    self.intent + f"\nBinding source/applicability policy: {self.source_policy}",
                    probe,
                    records,
                    [],
                    char_budget=self.budget,
                    original_body=original_body,
                    original_meta=original_meta,
                    context={"source_policy": self.source_policy, "source_binding": None},
                    capacity_probe=True,
                )
            )
            fits = True
        except _ReviewReached:
            fits = True
        except EditorialContextOverflow:
            fits = False
        self.cache[key] = fits
        return fits

    def assigned_fit(self, change: PageChange, original: OriginalPage, assigned: list[Evidence]) -> bool:
        """The one predicate for "this page's assigned claims can be reviewed".

        Both the fail-fast before drafting and the drafting-evidence bound use
        it, so a page cannot pass one and fail the other."""
        return self.fits(change, original, assigned, grow=True)

    def bounded(
        self, change: PageChange, original: OriginalPage, verified: list[Evidence], selected: list[Evidence]
    ) -> list[Evidence]:
        """Assigned claims always stay; optional ones only while the review can hold them."""
        required = set(change.claim_ids)
        if all(curate.evidence_id(e) in required for e in selected):
            return selected
        if self.fits(change, original, selected, grow=True):
            return selected
        # Truncating: rank without the draft body so drafting, validation and
        # reconciliation all keep the same optional records.
        stable = copy.copy(change)
        stable.draft_body = ""
        ranked = curate.select_page_evidence(verified, stable, limit=max(80, len(required)))
        mandatory = [e for e in ranked if curate.evidence_id(e) in required]
        optional = [e for e in ranked if curate.evidence_id(e) not in required]
        if not self.assigned_fit(change, original, mandatory):
            # More assigned claims than one review holds together: the editor
            # batches them and the reviews split (per section and sentence; per
            # cited source, then in-order chunks of one source's records; batched
            # required claims; chunked corrections). Never drafts less than
            # assigned, and adds no optional records.
            return mandatory
        low, high = 0, len(optional)  # low fits, high does not (the full set was refused)
        while high - low > 1:
            middle = (low + high) // 2
            if self.fits(change, original, mandatory + optional[:middle], grow=True):
                low = middle
            else:
                high = middle
        return mandatory + optional[:low]

    def claim_fits(self, change: PageChange, original: OriginalPage, record: Evidence) -> bool:
        """Can ONE claim be drafted on this page and reviewed?

        Drafting: the bounded editor must route and edit the record alone on
        THIS page, sized exactly as it will be: the routing frame of the
        page's own sections at the minimal preview, and the edit task's widest
        single-record bound for the page's own outline
        (editing.single_record_draft_fits). Otherwise the claim would pass here
        and fail during drafting, after model calls.

        Review: the smallest real review frame, which is the review system
        prompt, the claim's record (its text once and its source excerpt) and
        the minimal section context, a one-section page with one short
        sentence citing the claim's source; the draft may state the claim far
        more briefly than its evidence. Both the grounding and the
        required-claim review must accept it, measured by running the
        editorial review itself up to its first model call."""
        from doclayer import frontmatter
        from doclayer.curator.editing import editor_page, page_units, single_record_draft_fits

        original_meta, original_body = original
        title = change.title or Path(change.path).stem
        key = (
            "claim",
            self.budget,
            title,
            change.path,
            curate.evidence_id(record),
            len(record.text),
            len(record.quote or ""),
            hashlib.sha256(original_body.encode()).hexdigest(),
            hashlib.sha256(
                json.dumps(
                    [self.intent, self.source_policy, original_meta, change.draft_meta], sort_keys=True, default=str
                ).encode()
            ).hexdigest(),
        )
        if key in self.cache:
            return self.cache[key]
        # The page exactly as edit_page builds it from the drafting input (the
        # canonical page, or none for a new page); dated like any drafting day.
        existing = frontmatter.build(original_meta, original_body) if original_body else None
        meta, body = editor_page(change, existing, "0000-00-00")
        fits = single_record_draft_fits(self.intent, change, page_units(body), meta, record, self.budget)
        citation = record.url or record.label or record.source_id
        probe = copy.copy(change)
        probe.check_progress, probe.claim_ids = {}, [curate.evidence_id(record)]
        probe.draft_meta = {"title": title}
        probe.draft_body = f"# {title}\n\nThe claim [source: {citation}].\n"
        for mode in ("grounding", "required") if fits else ():
            try:
                _drive_without_suspension(
                    editorial.review(
                        _ReviewTripwire(),
                        self.intent + f"\nBinding source/applicability policy: {self.source_policy}",
                        probe,
                        [record],
                        [],
                        char_budget=self.budget,
                        original_body=f"# {title}\n",
                        original_meta={"title": title},
                        mode=mode,
                        context={"source_policy": self.source_policy, "source_binding": None},
                    )
                )
            except _ReviewReached:
                pass
            except EditorialContextOverflow:
                fits = False
                break
        self.cache[key] = fits
        return fits

    def require_assigned(self, change: PageChange, original: OriginalPage, records: list[Evidence]) -> None:
        """Fail before drafting only when a SINGLE assigned claim cannot be reviewed.

        Each assigned record not already rejected (whatever its verification
        state) must, on its own, be draftable on this page and fit the smallest
        real review frame (``claim_fits``). Assigned claims that only exceed one review together
        are not a failure: the editor drafts them in batches and the reviews
        split them (see ``bounded``). The error names the claim and whether its
        claim text or its source excerpt is what overflows."""
        assigned = set(change.claim_ids)
        mandatory = [r for r in records if r.support_status != "unsupported" and curate.evidence_id(r) in assigned]
        if self.assigned_fit(change, original, mandatory):
            return  # fits together, so every claim fits alone
        for record in mandatory:
            if self.claim_fits(change, original, record):
                continue
            identity = curate.evidence_id(record)
            text, excerpt = len(record.text), len(record.quote or "")
            text_only, excerpt_only = copy.copy(record), copy.copy(record)
            text_only.quote = ""
            excerpt_only.text = record.text[:80]
            if not self.claim_fits(change, original, text_only):
                part = f"its claim text ({text} chars) alone"
                remedy = "split that claim into smaller claims"
            elif excerpt and not self.claim_fits(change, original, excerpt_only):
                part = f"its source excerpt ({excerpt} chars) alone"
                remedy = "shorten that claim's source excerpt"
            else:
                part = f"its claim text ({text} chars) and source excerpt ({excerpt} chars) together"
                remedy = "split that claim or shorten its source excerpt"
            raise EditorialContextOverflow(
                f"{change.path}: assigned claim {identity} cannot fit the {self.budget}-char context budget on "
                f"its own: {part} overflows this page's drafting frame or the smallest review frame; {remedy} and "
                "resubmit",
                transient=False,
                diagnostic={
                    "operation": "page-capacity",
                    "page": change.path,
                    "claim_id": identity,
                    "claim_text_chars": text,
                    "source_excerpt_chars": excerpt,
                    "context_budget": self.budget,
                },
            )

    def review_records(
        self,
        change: PageChange,
        original: OriginalPage,
        everything: list[Evidence],
        bounded: list[Evidence],
        stamp: Any,
    ) -> list[Evidence]:
        """Records the grounding review of an existing draft sees.

        Every supported record of the page when the actual draft can hold them
        all; otherwise the bounded set the editor drafted from. A draft that
        predates bounded evidence (no current stamp) cannot be reviewed against
        a smaller set without condemning facts it legitimately drafted: it must
        be redrafted."""
        if len(bounded) >= len(everything) or self.fits(change, original, everything, actual=True):
            return everything
        if not isinstance(stamp, dict) or stamp.get("capacity_generation") != CAPACITY_GENERATION:
            raise EditorialContextOverflow(
                f"redraft required: {change.path} was drafted before page evidence was bounded to the review budget "
                f"and its {len(everything)} source records cannot be reviewed together (continue the import so the "
                "page is drafted again, or split it per page)",
                transient=False,
                diagnostic={
                    "operation": "redraft-required",
                    "page": change.path,
                    "records": len(everything),
                    "context_budget": self.budget,
                },
            )
        return bounded
