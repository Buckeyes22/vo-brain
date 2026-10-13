"""Reserve ordinary publication work while advancing bounded source review.

This is a soft yield inside source admission, never a second model route or a
budget reset. The underlying client continues to charge every HTTP attempt and
enforce its original hard limits. Unknown source decisions remain unresolved.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager

from doclayer.curator import curate
from doclayer.curator.state import BudgetExceeded


class SourceAdmissionDeferred(Exception):
    """Source review yielded before spending the publication reserve."""


def publication_reserve(max_calls):
    """Keep the hard cap while reserving drafting, editorial review and repairs."""
    return min(max_calls, 80, max_calls // 2 + 2)


class _ReservedClient:
    def __init__(self, client, *, reserve=None):
        self.client = client
        self.usage = client.usage
        self.max_calls = client.max_calls
        self.reserve = publication_reserve(self.max_calls) if reserve is None else max(0, min(self.max_calls, reserve))
        self.threshold = self.max_calls - self.reserve

    def _before(self):
        if self.usage.model_calls >= self.max_calls:
            raise BudgetExceeded("model_calls")
        if self.usage.model_calls >= self.threshold:
            raise SourceAdmissionDeferred("source review yielded to preserve publication budget")

    async def chat_json(self, system, user, **kwargs):
        if not hasattr(self.client, "_before_call"):
            self._before()
        return await self.client.chat_json(system, user, **kwargs)

    async def chat_verdicts(self, system, user, **kwargs):
        if not hasattr(self.client, "_before_call"):
            self._before()
        return await self.client.chat_verdicts(system, user, **kwargs)

    async def chat_bounded_json(self, system, user, **kwargs):
        if not hasattr(self.client, "_before_call"):
            self._before()
        return await self.client.chat_bounded_json(system, user, **kwargs)


@contextmanager
def source_budget(client, *, reserve=None):
    """Yield the source-only client with a per-attempt publication reserve.

    Use ``with source_budget(client) as admitted`` around extraction or source
    verification. Native ModelClient retry/repair admission is guarded too.
    The original cancellation/time callback and hard accounting remain intact;
    the callback is restored on success, soft defer, outage, or cancellation.
    The worker uses this stage client sequentially.
    """
    reserved = _ReservedClient(client, reserve=reserve)
    original_before = getattr(client, "_before_call", None)
    has_attempt_hook = hasattr(client, "_before_call")

    def admit_attempt():
        if original_before is not None:
            original_before()
        reserved._before()

    if has_attempt_hook:
        client._before_call = admit_attempt
    try:
        yield reserved
    finally:
        if has_attempt_hook:
            client._before_call = original_before


async def review_sources(
    client,
    topic,
    records,
    source_texts,
    *,
    char_budget,
    known=None,
    progress=None,
    max_records=12,
    settled_ids=None,
):
    """Admit at most twelve complete records, retaining all deferred identities.

    Records are the complete currently policy-eligible pool with authoritative
    scope already attached. Only exact current-generation source proofs count
    towards the known coherent work. A complete unknown is not retried against
    identical material every continuation; changed source/claim/version bindings
    invalidate it. Pending per-region source proofs remain resumable.

    progress receives the same proof-map deltas as verify_source_claims, so the
    caller retains its existing durable persistence/cancellation/time behavior.
    The result contains proof deltas, selected IDs and every still-unresolved ID;
    the caller rebuilds its full policy/source frontier through its own adapter.
    """
    records = list({curate.evidence_id(record): record for record in records}.values())
    hashes = curate.source_support_hashes(source_texts)
    valid = {}
    for record in records:
        identity = curate.evidence_id(record)
        proof = (known or {}).get(identity)
        if (
            proof
            and proof.get("support_version") == curate.SOURCE_SUPPORT_VERSION
            and proof.get("source_binding") == curate.source_support_binding(record, source_texts, source_hashes=hashes)
        ):
            valid[identity] = copy.deepcopy(proof)
    supported = {
        identity
        for identity, proof in valid.items()
        if not proof.get("verification_pending")
        and proof.get("status") == "supported"
        and proof.get("supported") is True
    }
    complete = {identity for identity, proof in valid.items() if not proof.get("verification_pending")}
    cached_unknown = [identity for identity in complete if valid[identity].get("status") == "unknown"]
    waiting = [record for record in records if curate.evidence_id(record) not in complete]
    ranked = curate.relevant_evidence(waiting, topic, limit=len(waiting)) if waiting else []
    seen = {curate.evidence_id(record) for record in ranked}
    ranked += sorted(
        (record for record in waiting if curate.evidence_id(record) not in seen),
        key=curate.evidence_id,
    )
    # These are budget scheduling hints ONLY. Actual placement still receives
    # every supported fact and independently rechecks current canonical bytes.
    coherent_pending = supported - set(settled_ids or [])
    selected = [] if len(coherent_pending) >= 12 else ranked[: max(0, min(12, max_records))]
    deltas = {}

    def retain(proofs):
        deltas.update(copy.deepcopy(proofs))
        if progress is not None:
            progress(proofs)

    yielded = False
    if selected:
        try:
            with source_budget(client) as admitted:
                result = await curate.verify_source_claims(
                    admitted,
                    selected,
                    source_texts,
                    char_budget=char_budget,
                    known=valid,
                    progress=retain,
                )
                deltas.update(copy.deepcopy(result))
        except SourceAdmissionDeferred:
            yielded = True
    latest = {**valid, **deltas}
    unresolved = [
        curate.evidence_id(record)
        for record in records
        if (
            latest.get(curate.evidence_id(record), {}).get("status") not in ("supported", "unsupported")
            or latest.get(curate.evidence_id(record), {}).get("verification_pending")
        )
    ]
    return {
        "proofs": deltas,
        "selected": [curate.evidence_id(record) for record in selected],
        "deferred": unresolved,
        "cached_unknown": sorted(cached_unknown),
        "yielded": yielded,
        "reserve": publication_reserve(client.max_calls),
        "known_supported": sorted(supported),
        "coherent_pending": sorted(coherent_pending),
    }
