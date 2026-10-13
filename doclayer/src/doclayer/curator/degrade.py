"""One policy for a unit that a per-unit task cannot complete.

A per-unit task (a section edit, a claim placement or routing, a structural
move, a coverage batch, a review chunk, an extraction chunk, a source-support
batch, a reconciliation view) whose reply is still rejected after the task's
own bounded retries, or whose complete input does not fit the bounded
context, drops just that unit: the unit keeps its original content, a CURRENT
coverage gap names the unit and the cause (with sizes for an overflow), and
the page and the job continue.

A job still stops on job-level limits (model calls, active time, bytes), on
transport or outage errors, and on page-level invariant violations. Safety validators (scope,
grounding, preservation, protected material) are never degraded here; they
still block publication.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator

from doclayer.curator.model import OutputTruncated, ReplyRejected, UnitTooLarge

GAP_MARK = "unit dropped"
MAX_REASON_CHARS = 300  # the rejection text kept in a gap note

_record: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("curator_degrade_record", default=None)


def droppable(exc: BaseException) -> bool:
    """A rejected reply, a reply truncated even for this unit, or a unit too
    large for the bounded input. Nothing else (transport, budget, bindings)."""
    return isinstance(exc, (ReplyRejected, OutputTruncated, UnitTooLarge))


def cause(exc: BaseException) -> str:
    """Why a droppable unit was dropped, in a few words."""
    return (
        "input exceeds bounded context"
        if isinstance(exc, UnitTooLarge)
        else "model output truncated"
        if isinstance(exc, OutputTruncated)
        else "model reply rejected"
    )


def gap_note(unit: str, exc: BaseException) -> str:
    reason = " ".join(str(exc).split())[:MAX_REASON_CHARS]
    return f"{unit}: {GAP_MARK} ({cause(exc)}: {reason})"


@contextlib.contextmanager
def recording(gaps: list[str] | None) -> Iterator[None]:
    """Bind the job's coverage-gap list that dropped units record on; None
    binds nowhere, so a droppable unit re-raises."""
    token = _record.set(gaps)
    try:
        yield
    finally:
        _record.reset(token)


def can_record() -> bool:
    """Whether a dropped unit has a job to record its gap on (else it re-raises)."""
    return _record.get() is not None


def degrade_unit(unit: str, exc: BaseException) -> str:
    """Drop one unit whose reply was rejected after its bounded retries, or
    whose complete input does not fit the bounded context.

    Records the current gap and returns its note; the caller leaves the unit's
    content unchanged and continues. Any other error, or a call with nowhere to
    record the gap, re-raises ``exc`` unchanged: nothing is dropped silently.
    """
    gaps = _record.get()
    if gaps is None or not droppable(exc):
        raise exc
    note = gap_note(unit, exc)
    if note not in gaps:
        gaps.append(note)
    return note


def unit_succeeded(unit: str) -> None:
    """A unit that was dropped earlier and has now completed is no longer a gap."""
    gaps = _record.get()
    if gaps is None:
        return
    prefix = f"{unit}: {GAP_MARK} ("
    gaps[:] = [note for note in gaps if not str(note).startswith(prefix)]
