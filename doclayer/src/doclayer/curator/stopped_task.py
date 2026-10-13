"""Typed prepublication task stops; unavailable evidence is never a negative fact."""

from __future__ import annotations

PREPUBLICATION = frozenset({"intake", "discover", "extract", "place", "draft", "validate"})
TASK_OPERATIONS = frozenset(
    {
        "json-parse",
        "article-review",
        "draft-scope-witness",
        "draft-enumeration-fidelity",
        "draft-claim-check",
        "source-support",
        "source-claim-verification",
        "placement-json",
        "bounded-edit",
        "bounded-section-routing",
        "source-claim-extraction",
        "source-quotation-repair",
        "source-semantic-recheck",
        "source-ancestor-constraints",
    }
)
RESPONSE_STOP = "model_response_unresolved"


def record_task_stop(state, *, kind: str, operation: str | None = None, subject: str | None = None) -> None:
    """Record only an owning, actually caught stop, bound to its error/stage."""
    if state.stage not in PREPUBLICATION:
        return
    if kind != "output_truncated" and operation not in TASK_OPERATIONS:
        return
    marker = {
        "operation": "automatic-task-stop",
        "category": RESPONSE_STOP,
        "kind": kind,
        "task_operation": operation,
        "stage": state.stage,
        "error": state.error,
        **({"subject": subject} if subject else {}),
    }
    state.inference_diagnostics = (state.inference_diagnostics + [marker])[-3:]


def task_stop(state) -> dict | None:
    """Do not infer a stop category from legacy/free-text errors or stale markers."""
    if state.status != "retryable" or state.stage not in PREPUBLICATION:
        return None
    for marker in reversed(state.inference_diagnostics):
        if (
            isinstance(marker, dict)
            and marker.get("operation") == "automatic-task-stop"
            and marker.get("category") == RESPONSE_STOP
            and marker.get("stage") == state.stage
            and marker.get("error") == state.error
            and (
                (
                    marker.get("kind") == "output_truncated"
                    and state.error
                    == "model output truncated repeatedly; split the batch or raise the serving output limit"
                )
                or (
                    marker.get("kind") == "model_task"
                    and marker.get("task_operation") in TASK_OPERATIONS
                    and isinstance(state.error, str)
                    and state.error.startswith("inference: ")
                )
            )
        ):
            return dict(marker)
    return None


#: A gap job that stopped and was not (or could not be) recovered by bounded
#: conditional resumes: its allowance is retired so its dispatcher slot is
#: released, while the job keeps its work for operator readmission.
JOB_STOP = "stopped_unresolved"
