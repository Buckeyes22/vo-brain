"""Strict adapters for the owning curator ops wire, shared by worker/verifier."""

from __future__ import annotations

import re
from typing import Any


class CuratorReplyError(ValueError):
    pass


def parse_submit_reply(reply: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise CuratorReplyError("curator submission rejected or unavailable")
    if (
        not isinstance(reply.get("job_id"), str)
        or not re.fullmatch(r"job-[a-z0-9]{8,24}", reply["job_id"])
        or type(reply.get("created")) is not bool
    ):
        raise CuratorReplyError("curator submission lacks a valid job receipt")
    return reply


def parse_job_reply(reply: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise CuratorReplyError("curator status/result rejected or unavailable")
    job = reply.get("job")
    if (
        not isinstance(job, dict)
        or not isinstance(job.get("job_id"), str)
        or not re.fullmatch(r"job-[a-z0-9]{8,24}", job["job_id"])
        or not isinstance(job.get("status"), str)
        or not job["status"]
    ):
        raise CuratorReplyError("curator status/result lacks a valid job view")
    return job
