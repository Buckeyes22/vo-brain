"""Durable attempt grants and usage. Charge attempts before I/O, never after success.

Immutable attempt grants and charged requests are retained for audit and recovery.
These planned ceilings do not withhold daily capacity. Local inference has no
daily quota; native per-job caps remain unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from doclayer.curator.jobstore import FileLock, atomic_write_json


class BudgetUnavailable(ValueError):
    pass


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class ResourceBudget:
    def __init__(
        self,
        state_dir: Path | None = None,
        *,
        max_jobs: int | None = None,
        max_calls_day: int | None = None,
        max_calls_job: int | None = None,
    ) -> None:
        if state_dir is None:
            from doclayer.settings import settings

            state_dir = settings.gap_state_dir
        from doclayer.settings import settings

        max_jobs = settings.gap_jobs_per_day if max_jobs is None else max_jobs
        max_calls_day = settings.gap_model_calls_day if max_calls_day is None else max_calls_day
        max_calls_job = settings.gap_model_calls_job if max_calls_job is None else max_calls_job
        self.root = Path(state_dir)
        self.dir = self.root / "budgets"
        moved = self.root / "owner-migrated.json"
        if not (moved.exists() or moved.is_symlink()):
            self.dir.mkdir(parents=True, exist_ok=True)
        self.max_jobs, self.max_calls_day, self.max_calls_job = (
            max_jobs,
            max_calls_day,
            max_calls_job,
        )
        if any(type(n) is not int or n <= 0 for n in (max_jobs, max_calls_day, max_calls_job) if n is not None):
            raise ValueError("budget limits must be positive integers")

    def lock(self):
        from doclayer.gaps import GapLedger

        GapLedger(self.root).assert_owner_writable()
        self.dir.mkdir(parents=True, exist_ok=True)
        return FileLock(self.dir / "budgets.lock")

    def load(self, day: str) -> dict[str, Any]:
        path = self.dir / f"{day}.json"
        if not path.exists():
            return {
                "day": day,
                "jobs": 0,
                "reserved_calls": 0,
                "model_calls": 0,
                "entries": [],
                "charges": {},
            }
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or data.get("day") != day:
                raise ValueError("invalid day")
            for name in ("jobs", "reserved_calls"):
                if type(data.get(name)) is not int or data[name] < 0:
                    raise ValueError("invalid counter")
            if not isinstance(data.get("entries"), list):
                raise TypeError("invalid reservations")
            for row in data["entries"]:
                if (
                    not isinstance(row, dict)
                    or type(row.get("reserved_calls")) is not int
                    or row["reserved_calls"] < 0
                    or type(row.get("released")) is not bool
                    or type(row.get("carryover", False)) is not bool
                ):
                    raise ValueError("invalid reservation")
            if sum(r["reserved_calls"] for r in data["entries"] if not r.get("released")) != data["reserved_calls"]:
                raise ValueError("reservation counters disagree")
            if data["jobs"] != sum(1 for r in data["entries"] if not r.get("released") and not r.get("carryover")):
                raise ValueError("job counters disagree")
            data.setdefault("model_calls", 0)
            data.setdefault("charges", {})
            if (
                type(data["model_calls"]) is not int
                or data["model_calls"] < 0
                or not isinstance(data["charges"], dict)
                or any(type(n) is not int or n < 0 for n in data["charges"].values())
            ):
                raise ValueError("invalid model charges")
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise BudgetUnavailable(f"daily budget unreadable ({day}); no allowance granted") from exc

    def save(self, day: str, data: dict[str, Any]) -> None:
        from doclayer.gaps import GapLedger

        GapLedger(self.root).assert_owner_writable()
        atomic_write_json(self.dir / f"{day}.json", data)

    def _active_hold(self, data: dict[str, Any]) -> None:
        from doclayer.gaps import active_slot_paths

        for path in active_slot_paths(self.root):
            self._hold_one(path, data)

    @staticmethod
    def _hold_one(path: Path, data: dict[str, Any]) -> None:
        try:
            active = json.loads(path.read_text())
            attempt = active["attempt_id"]
            calls = active["calls"]
            gap_id = active["gap_id"]
            if not isinstance(attempt, str) or not isinstance(gap_id, str) or type(calls) is not int or calls <= 0:
                raise ValueError("invalid active hold")
        except FileNotFoundError:
            return  # released between listing and reading
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise BudgetUnavailable("active submission unreadable; no allowance granted") from exc
        if not any(r.get("attempt_id") == attempt and not r.get("released") for r in data["entries"]):
            data["reserved_calls"] += calls
            data["entries"].append(
                {
                    "attempt_id": attempt,
                    "gap_id": gap_id,
                    "reserved_calls": calls,
                    "released": False,
                    "carryover": True,
                }
            )

    def account_active(self) -> None:
        """Record potential current-day native use BEFORE terminal slot release."""
        day = today()
        with self.lock():
            data = self.load(day)
            self._active_hold(data)
            self.save(day, data)

    def reserve_attempt(self, attempt_id: str, gap_id: str, calls: int, *, headroom: int = 0) -> dict[str, Any] | None:
        if type(headroom) is not int or headroom < 0:
            raise ValueError("headroom must be a nonnegative integer")
        day = today()
        with self.lock():
            data = self.load(day)
            self._active_hold(data)
            existing = next(
                (r for r in data["entries"] if r.get("attempt_id") == attempt_id and not r.get("released")),
                None,
            )
            if existing:
                self.save(day, data)
                return {
                    "id": attempt_id,
                    "day": day,
                    "calls": existing["reserved_calls"],
                }
            grant = min(calls, self.max_calls_job)
            if self.max_calls_day is not None:
                grant = min(
                    grant,
                    self.max_calls_day - data["reserved_calls"] - data["model_calls"] - headroom,
                )
            if (self.max_jobs is not None and data["jobs"] >= self.max_jobs) or grant <= 0:
                self.save(day, data)
                return None
            data["jobs"] += 1
            data["reserved_calls"] += grant
            data["entries"].append(
                {
                    "attempt_id": attempt_id,
                    "gap_id": gap_id,
                    "reserved_calls": grant,
                    "released": False,
                }
            )
            self.save(day, data)
            return {"id": attempt_id, "day": day, "calls": grant}

    def consume_model(self, *, kind: str, attempt_id: str | None = None) -> None:
        """Synchronous and async callers call this immediately before each HTTP attempt."""
        day = today()
        with self.lock():
            data = self.load(day)
            self._active_hold(data)
            if self.max_calls_day is not None and data["model_calls"] + data["reserved_calls"] >= self.max_calls_day:
                self.save(day, data)
                raise BudgetUnavailable("daily inference allowance exhausted")
            if attempt_id:
                key = f"{kind}:{attempt_id}"
                if data["charges"].get(key, 0) >= self.max_calls_job:
                    raise BudgetUnavailable("attempt inference allowance exhausted")
                data["charges"][key] = data["charges"].get(key, 0) + 1
            data["model_calls"] += 1
            self.save(day, data)

    def release(self, reservation: dict[str, Any], *, proven_unused: bool = False) -> int:
        """Only a proven never-submitted reservation may be refunded. Duplicate != unused."""
        if not proven_unused:
            return 0
        with self.lock():
            day = reservation["day"]
            data = self.load(day)
            for row in data["entries"]:
                if row.get("attempt_id") == reservation["id"] and not row.get("released"):
                    row["released"] = True
                    data["jobs"] = max(0, data["jobs"] - 1)
                    data["reserved_calls"] -= row["reserved_calls"]
                    self.save(day, data)
                    return row["reserved_calls"]
        return 0
