"""Local durable event outbox with one SSH-backed knowledge-gap owner.

Prompt hooks only enqueue; network delivery belongs to the flush timer or an
explicit tool call. A stable event ID survives lost replies and client restarts.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any

from doclayer.curator.jobstore import FileLock, atomic_write_json
from doclayer.gaps import GapLedger
from doclayer.settings import Settings, settings

OUTBOX_LIMIT = 8 * 1024 * 1024


@dataclass
class GapReceipt:
    gap_id: str
    status: str
    recurrence: int = 1
    need_class: str = "missing_coverage"


class GapClient:
    def __init__(self, config: Settings | None = None):
        self.config = config or settings
        self.outbox = self.config.gap_state_dir / "outbox"

    def _remote(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config.gap_owner_host:
            raise ValueError(
                "DOC_LAYER_GAP_OWNER_HOST is not set; the ssh gap owner needs a host "
                "(leave DOC_LAYER_GAP_OWNER unset to use this machine as the owner)"
            )
        wire = json.dumps({"operation": operation, "payload": payload})
        if len(wire) > 16_384:
            raise ValueError("gap request exceeds bounded protocol limit")
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                self.config.gap_owner_host,
                self.config.gap_owner_command,
            ],
            input=wire,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("knowledge-gap owner unavailable; event retained locally")
        reply = json.loads(result.stdout)
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            raise RuntimeError("knowledge-gap owner rejected request; event retained locally")
        return reply

    def operation(self, operation: str, payload: dict[str, Any] | None = None):
        if self.config.gap_owner == "local":
            from doclayer.gap_ops import handle

            return handle(operation, payload or {}, GapLedger(self.config.gap_state_dir))
        if self.config.gap_owner != "ssh":
            raise ValueError("DOC_LAYER_GAP_OWNER must be local or ssh")
        return self._remote(operation, payload or {})

    def submit_event(self, *, defer: bool = False, **event):
        if event.get("subject") and "catalog_subject" not in event:
            from doclayer.coverage import load_catalog
            from doclayer.routing import _match_subjects

            try:
                catalog = load_catalog(
                    self.config.brain_repo,
                    self.config.catalog_cache_dir,
                    self.config.routing_policy_path,
                )
                subjects, conflicts = _match_subjects(str(event["subject"]), catalog)
                if not conflicts and len(subjects) == 1:
                    event["subject"] = next(iter(subjects))
                    event["catalog_subject"] = True
            except (OSError, ValueError, RuntimeError):
                pass  # Unknown coverage is still a durable documentation need.
        if self.config.gap_owner == "local":
            return GapLedger(self.config.gap_state_dir).submit_event(**event)
        if not event.get("event_key"):
            event["event_key"] = "evt-" + uuid.uuid4().hex
        token = uuid.uuid5(uuid.NAMESPACE_URL, str(event["event_key"])).hex
        self.outbox.mkdir(parents=True, exist_ok=True)
        path = self.outbox / (token + ".json")
        encoded = (json.dumps(event, indent=2) + "\n").encode()
        wire = json.dumps({"operation": "submit_event", "payload": event})
        if len(wire) > 16_384:
            raise ValueError("gap event exceeds bounded technical-detail limit")
        with FileLock(self.outbox / "outbox.lock"):
            if path.exists():
                if json.loads(path.read_text()) != event:
                    raise ValueError("event identity already belongs to a different payload")
            else:
                size = sum(p.stat().st_size for p in self.outbox.glob("*.json"))
                if size + len(encoded) > OUTBOX_LIMIT:
                    atomic_write_json(self.outbox / "capture-degraded", {"reason": "outbox full"})
                    raise RuntimeError("capture degraded: local event outbox is full")
                atomic_write_json(path, event)
        if not defer:
            try:
                reply = self.operation("submit_event", event)
                with FileLock(self.outbox / "outbox.lock"):
                    path.unlink(missing_ok=True)
                record = reply["record"]
                return GapReceipt(
                    record["gap_id"],
                    record["status"],
                    record["recurrence"],
                    record["need_class"],
                ), reply["created"]
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                pass  # Durable local receipt; flush will reuse exactly this ID.
        return GapReceipt("event-" + token, "pending_delivery", need_class=event["need_class"]), True

    def flush(self, *, limit: int = 50) -> dict[str, Any]:
        if self.config.gap_owner == "local":
            return {"delivered": 0, "remaining": 0}
        self.outbox.mkdir(parents=True, exist_ok=True)
        delivered = 0
        with FileLock(self.outbox / "flush.lock"):
            for path in sorted(self.outbox.glob("*.json"))[:limit]:
                try:
                    event = json.loads(path.read_text())
                    self.operation("submit_event", event)
                    with FileLock(self.outbox / "outbox.lock"):
                        path.unlink(missing_ok=True)
                    delivered += 1
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    break
        remaining = len(list(self.outbox.glob("*.json")))
        if remaining == 0:
            (self.outbox / "capture-degraded").unlink(missing_ok=True)
        return {"delivered": delivered, "remaining": remaining}
