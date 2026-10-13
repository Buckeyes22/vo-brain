"""Build hash-bound local text sources for gap records."""

import hashlib

#: An origin nothing dispatches: material carrying it stays out of curator payloads.
UNDISPATCHED_ORIGIN = "attached-local-text"


def local_source(label: str, text: str, origin: str = UNDISPATCHED_ORIGIN) -> dict:
    """A bounded, hash-bound text source; only the private writer's origin is dispatched."""
    return {
        "kind": "text",
        "label": label,
        "text": text,
        "authority": "local-observation",
        "origin": origin,
        "source_hash": hashlib.sha256(text.encode()).hexdigest(),
    }
