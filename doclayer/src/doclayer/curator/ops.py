"""The curator's operations dispatcher: JSON in on stdin, JSON out on stdout.

This is the fixed remote entry point SSH clients call
(``brain-curator ops <operation>``). Nothing submitted is ever interpolated
into shell commands; everything arrives as one JSON document. Malformed
input is rejected with ``{"ok": false, ...}``, never a traceback.

Operations: submit, status, result, resume, cancel, correct, list, inspect.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from doclayer.curator import policy
from doclayer.curator.config import MODES, CuratorConfig, load_config, stage_config
from doclayer.curator.intake import (
    IntakeError,
    build_sources_from_manifest,
    extract_links,
    make_file_source,
    make_text_source,
    make_url_source,
    matches_exclusions,
)
from doclayer.curator.intake import (
    content_hash as intake_content_hash,
)
from doclayer.curator.jobstore import JobStore, atomic_write_json, now_iso
from doclayer.curator.state import STAGES, JobState, PageChange

_OK = 0
_ERROR = 3
PRIVATE_SUBMITTER = "gap-worker-private"
_PRIVATE_FORBIDDEN = ("urls", "files", "manifest", "manifest_file", "directory")


class OpsError(ValueError):
    """A client error: malformed or unactionable request."""


# --------------------------------------------------------------------------- #
# request normalization
# --------------------------------------------------------------------------- #


def derive_idempotency_key(payload: dict[str, Any], default_mode: str | None = None) -> str:
    """Content-derived key so retrying an uncertain submission returns the
    same job instead of enqueueing a duplicate.

    An explicit ``mode`` equal to ``default_mode`` (the configured default)
    hashes like an omitted mode, and duplicate ``targets`` collapse in order,
    so payloads without mode or targets keep their historical keys."""
    parts = [
        str(payload.get("intent", "")),
        json.dumps(sorted(str(e) for e in payload.get("exclusions", []))),
        json.dumps([str(u) for u in payload.get("urls", [])]),
        json.dumps(sorted(intake_content_hash(str(t.get("text", ""))) for t in payload.get("texts", []))),
        json.dumps(sorted(f"{f.get('name')}:{f.get('size', 0)}" for f in payload.get("files", []))),
        str(payload.get("manifest_digest", "")),
        str(payload.get("correction_of", "")),
        json.dumps(payload.get("source_policy", {}), sort_keys=True),
    ]
    mode = payload.get("mode")
    if mode and str(mode) != default_mode:
        parts.append("mode:" + str(mode))
    if payload.get("targets"):
        parts.append("targets:" + json.dumps(list(dict.fromkeys(str(t) for t in payload["targets"]))))
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:24]


def build_sources(payload: dict[str, Any], staging: Path) -> list:
    """SourceRecords from a submit payload. Files must already sit in the
    staging directory (transferred by the CLI over SSH); pasted texts are
    persisted there as staging files by the caller."""
    from doclayer.curator.state import SourceRecord

    sources: list[SourceRecord] = []
    for url in payload.get("urls", []):
        sources.append(make_url_source(str(url)))
    for item in payload.get("texts", []):
        source = make_text_source(str(item.get("label", "pasted-text")), str(item.get("text", "")))
        staged_name = item.get("staged_name")
        if staged_name:
            source.local_path = str(staging / str(staged_name))
        sources.append(source)
    staged = staging
    for item in payload.get("files", []):
        name = Path(str(item.get("name", ""))).name
        if name == payload.get("manifest_file"):
            continue  # the manifest describes inputs; it is not itself evidence
        if not name:
            raise OpsError("file entry without a name")
        path = staged / name
        if not path.is_file():
            raise OpsError(f"staged file missing: {name}")
        source = make_file_source(path)
        source.local_path = str(path)
        sources.append(source)
    manifest_name = payload.get("manifest_file")
    if manifest_name:
        manifest_path = staged / Path(str(manifest_name)).name
        if not manifest_path.is_file():
            raise OpsError(f"staged manifest missing: {manifest_name}")
        try:
            manifest = payload.get("manifest") or _load_yaml(manifest_path)
            _topic, exclusions, manifest_sources = build_sources_from_manifest(manifest, base_dir=staged)
            # Inline manifest inputs need the same durable bytes as payload
            # texts. Generated IDs, never submitted labels, name staged files.
            manifest_texts = {
                make_text_source(str(entry.get("label", "pasted-text")), str(entry["text"])).id: str(entry["text"])
                for entry in manifest.get("sources") or manifest.get("inputs") or []
                if isinstance(entry, dict) and "text" in entry and "url" not in entry and "file" not in entry
            }
            for source in manifest_sources:
                if source.kind == "text" and source.id in manifest_texts:
                    text_path = staged / f"manifest-text-{source.id}.md"
                    text_path.write_text(manifest_texts[source.id], encoding="utf-8")
                    source.local_path = str(text_path)
            if not payload.get("source_policy"):
                payload["source_policy"] = policy.validate_policy(manifest.get("source_policy", {}))
        except IntakeError as exc:
            raise OpsError(f"manifest invalid: {exc}") from exc
        if not payload.get("exclusions"):
            payload["exclusions"] = exclusions
        sources.extend(manifest_sources)
    if payload.get("directory"):
        from doclayer.curator.intake import enumerate_directory

        dir_name = Path(str(payload["directory"])).name
        directory = staged / dir_name
        if not directory.is_dir():
            raise OpsError(f"staged directory missing: {dir_name}")
        for file_path in enumerate_directory(directory):
            source = make_file_source(file_path)
            source.local_path = str(file_path)
            sources.append(source)
    if not sources:
        raise OpsError("submit carries no inputs (urls/texts/files/manifest/directory)")
    return sources


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise OpsError(f"manifest {path.name} is not a mapping")
    return data


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #


def _status_view(state: JobState, store: JobStore) -> dict[str, Any]:
    from doclayer.curator.coverage import assess_coverage
    from doclayer.curator.stopped_task import task_stop

    assessment = assess_coverage(state)
    pub = state.publication
    return {
        "job_id": state.job_id,
        "status": state.status,
        "stage": state.stage,
        "created_at": state.created_at,
        "mode": state.mode,
        "private": state.private,
        "single_pass_calls": {
            path: row.get("calls", 0)
            for path, row in state.evidence_selection.get("single_pass", {}).get("pages", {}).items()
        },
        "intent": state.intent[:200],
        "coverage": state.effective_coverage,
        "coverage_gaps": list(assessment.current_gaps),
        "historical_coverage_gaps": list(assessment.historical_gaps),
        "continuation_of": state.continuation_of,
        "inherited_sources": len(state.inherited_source_ids),
        "sources": {
            "total": len(state.sources),
            "fetched": sum(1 for s in state.sources if s.status == "fetched"),
            "failed": sum(1 for s in state.sources if s.status == "failed"),
            "skipped": sum(1 for s in state.sources if s.status == "skipped"),
            "duplicate": sum(1 for s in state.sources if s.status == "duplicate"),
            "pending": sum(1 for s in state.sources if s.status in ("pending", "scored")),
            "rejected": sum(1 for s in state.sources if s.status == "rejected"),
            "sections_mapped": sum(s.section_count for s in state.sources),
            "review_units_mapped": sum(s.review_unit_count for s in state.sources),
            "unknown_reviews": sum(len(s.unknown_reviewed_sections) for s in state.sources),
            "obsolete_parent_reviews": sum(len(s.obsolete_reviewed_sections) for s in state.sources),
            "sections_reviewed": sum(len(s.reviewed_sections) for s in state.sources),
            "sections_omitted": sum(len(s.omitted_sections) for s in state.sources),
        },
        "changes": [{"path": c.path, "action": c.action, "status": c.status} for c in state.changes],
        "usage": state.usage.to_dict(),
        "limits": dict(state.limits),
        "resume_checkpoint": resume_checkpoint(state),
        "automatic_stop": task_stop(state),
        "publication": {
            "branch": pub.branch,
            "pr_number": pub.pr_number,
            "merged": pub.merged,
            "merge_commit": pub.merge_commit,
            "commit_sha": pub.commit_sha,
            "pushed": pub.pushed,
            "committed_paths": len(pub.committed_paths),
        },
        "error": state.error,
        "cancel_requested": state.cancel_requested,
        "updated_at": state.updated_at,
    }


def handle(
    op: str,
    payload: dict[str, Any],
    cfg: CuratorConfig | None = None,
    store: JobStore | None = None,
) -> dict[str, Any]:
    """One operation against the durable store. Pure function of (op,
    payload, store): the CLI/MCP layers only carry bytes."""
    cfg = cfg or load_config()
    store = store or JobStore(cfg.jobs_dir())

    if op == "submit":
        return _op_submit(payload, cfg, store)
    if op == "status":
        return _op_status(payload, store)
    if op == "result":
        return _op_result(payload, store)
    if op == "continue":
        return _op_continue(payload, cfg, store)
    if op == "resume":
        return _op_resume(payload, store)
    if op == "retire_allowance":
        return _op_retire_allowance(payload, store)
    if op == "cancel":
        return _op_cancel(payload, store)
    if op == "correct":
        return _op_correct(payload, cfg, store)
    if op == "list":
        return _op_list(payload, store)
    if op == "inspect":
        return _op_inspect(payload, cfg, store)
    raise OpsError(f"unknown operation: {op!r}")


def _validated_targets(raw: Any, cfg: CuratorConfig, mode: str) -> list[str]:
    """Explicit single-pass target pages: allowed .md paths, deduplicated in
    order, at most max_pages. Multi-stage jobs would ignore targets, so they
    are rejected there instead of silently dropped."""
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list) or any(not isinstance(path, str) for path in raw):
        raise OpsError("targets must be a list of page paths")
    if mode != "single-pass":
        raise OpsError("targets apply only to single-pass jobs")
    from doclayer.curator.validate import check_path_allowed

    unique = list(dict.fromkeys(raw))
    for path in unique:
        failure = check_path_allowed(path, cfg)
        if failure is not None:
            raise OpsError(f"invalid target {path!r}: {failure.detail}")
    if len(unique) > cfg.single_pass.max_pages:
        raise OpsError(f"at most {cfg.single_pass.max_pages} targets per single-pass job")
    return unique


def _op_submit(payload: dict[str, Any], cfg: CuratorConfig, store: JobStore) -> dict[str, Any]:
    intent = str(payload.get("intent", "")).strip()
    if not intent:
        raise OpsError("submit needs an intent (topic / task instruction)")
    mode = str(payload.get("mode") or cfg.pipeline.default_mode)
    if mode not in MODES:
        raise OpsError(f"unknown mode {mode!r}; expected one of {MODES}")
    private = payload.get("private") is True
    if private:
        if str(payload.get("submitted_by", "")) != PRIVATE_SUBMITTER:
            raise OpsError("private jobs are accepted only from the gap worker")
        if mode != "single-pass":
            raise OpsError("private jobs run single-pass")
        if any(payload.get(key) for key in _PRIVATE_FORBIDDEN):
            raise OpsError("private jobs carry text evidence only")
        if not payload.get("texts"):
            raise OpsError("private jobs need text evidence")
    targets = _validated_targets(payload.get("targets"), cfg, mode)
    key = str(payload.get("idempotency_key") or derive_idempotency_key(payload, cfg.pipeline.default_mode))
    existing = None
    key_path = store.index_dir / f"idem-{key}.json"
    if key_path.is_file():
        try:
            existing = store.load(json.loads(key_path.read_text())["job_id"])
        except (OSError, ValueError, KeyError):
            existing = None
    if existing is not None and payload.get("mode") and existing.mode != mode and not payload.get("idempotency_key"):
        # An explicit mode the replayed job does not have (say, a job submitted
        # before a default-mode change) is a different request: give it its own
        # key. Ordinary replays keep their key.
        key = hashlib.sha256(f"{key}\x00mode:{mode}".encode()).hexdigest()[:24]
        key_path = store.index_dir / f"idem-{key}.json"
        existing = None
        if key_path.is_file():
            try:
                existing = store.load(json.loads(key_path.read_text())["job_id"])
            except (OSError, ValueError, KeyError):
                existing = None
    if existing is not None:
        return {
            "ok": True,
            "job_id": existing.job_id,
            "created": False,
            "status": existing.status,
            "note": "idempotent: an existing job carries this submission key",
        }

    # The transport stages inputs under jobs/incoming/<its own key> and names
    # that directory in the payload; only directories under jobs/incoming are
    # honored (submitted paths are data, never trusted).
    staging = store.jobs_dir / "incoming" / key
    claimed_dir = payload.get("staging_dir")
    if claimed_dir:
        candidate = Path(str(claimed_dir)).resolve()
        incoming_root = (store.jobs_dir / "incoming").resolve()
        if candidate.is_dir() and candidate.parent == incoming_root:
            staging = candidate
    staging.mkdir(parents=True, exist_ok=True)
    # Persist pasted texts as staging files so intake reads every source the
    # same durable way (a text source never cites a cache path).
    import re as _re

    for index, item in enumerate(payload.get("texts", [])):
        label = _re.sub(r"[^a-zA-Z0-9._-]+", "-", str(item.get("label", "pasted")))[:60]
        staged_name = f"text-{index}-{label or 'pasted'}.md"
        (staging / staged_name).write_text(str(item.get("text", "")), encoding="utf-8")
        item["staged_name"] = staged_name
    sources = build_sources(payload, staging)
    # Move staged inputs into a safe place keyed by the job (the job dir is
    # created by store.create below; keep them under incoming/<key> and point
    # sources at their final staged path).
    state = JobState(
        job_id="",
        idempotency_key=key,
        intent=intent,
        exclusions=[str(e) for e in payload.get("exclusions", [])],
        submitted_by=str(payload.get("submitted_by", "agent")),
        correction_of=payload.get("correction_of"),
        source_policy=policy.validate_policy(payload.get("source_policy", {})),
        mode=mode,
    )
    state.private = private
    if targets:
        state.evidence_selection["single_pass"] = {"version": 1, "targets": targets, "pages": {}}
    state.sources = list(sources)
    limits = payload.get("limits")
    if isinstance(limits, dict) and limits:
        state.limits = {
            k: v
            for k, v in limits.items()
            if k
            in (
                "max_sources",
                "max_source_bytes",
                "max_job_bytes",
                "max_model_calls",
                "max_active_seconds",
                "max_repair_passes",
            )
        }
    for source in state.sources:
        if source.local_path:
            # files live under incoming/<key>/ until intake moves nothing:
            # keep the path stable (job staging is incoming/<key> itself)
            source.local_path = str(source.local_path)
    created_state, created = store.create(state)
    if created:
        _move_staging(store, key, created_state.job_id, staging)
    return {
        "ok": True,
        "job_id": created_state.job_id,
        "created": created,
        "status": created_state.status,
    }


def _move_staging(store: JobStore, key: str, job_id: str, staging: Path) -> None:
    target = store.staging_dir(job_id)
    if staging.is_dir() and staging != target:
        target.mkdir(parents=True, exist_ok=True)
        moved: dict[str, str] = {}
        for item in list(staging.iterdir()):
            shutil.move(str(item), str(target / item.name))
            moved[str(staging / item.name)] = str(target / item.name)
        try:
            staging.rmdir()
        except OSError:
            pass
        if moved:
            with store.control_lock(job_id):
                state = store.load(job_id)
                for source in state.sources:
                    if source.local_path and source.local_path in moved:
                        source.local_path = moved[source.local_path]
                store.save(state)


def _op_status(payload: dict[str, Any], store: JobStore) -> dict[str, Any]:
    job_id = str(payload.get("job_id", ""))
    if not store.exists(job_id):
        raise OpsError(f"unknown job: {job_id}")
    return {"ok": True, "job": _status_view(store.load(job_id), store)}


def _op_result(payload: dict[str, Any], store: JobStore) -> dict[str, Any]:
    job_id = str(payload.get("job_id", ""))
    if not store.exists(job_id):
        raise OpsError(f"unknown job: {job_id}")
    state = store.load(job_id)
    result: dict[str, Any] = {
        "ok": True,
        "job": _status_view(state, store),
    }
    if payload.get("include_digest", True):
        result["digest"] = store.read_summary(job_id) or state.summary
    return result


def _op_continue(payload: dict[str, Any], cfg: CuratorConfig, store: JobStore) -> dict[str, Any]:
    """A new bounded import linked to a finished import, never a PR replay.

    Cache references/evidence are reused without copying source binaries. The
    original immutable publication remains the source of historical truth.
    Targets optionally limit review to specific sources and omitted sections.
    Historical jobs lacking a frontier rebuild it explicitly from cached pages.
    Discovery renews deferred candidates; repeated ``urls`` add primary seeds
    before discovery. URL seeds require discovery, and explicit exclusion or
    unexplained rejection history is never overridden. A relevance-only rejection
    can be reconsidered by an explicit seed under the child's current policy.
    """
    job_id = str(payload.get("job_id", ""))
    if not store.exists(job_id):
        raise OpsError(f"unknown job: {job_id}")
    previous = store.load(job_id)
    if previous.private and any(payload.get(key) for key in (*_PRIVATE_FORBIDDEN, "source_policy")):
        raise OpsError("a private job continues from its own text evidence only")
    if previous.in_flight or previous.status in ("retryable", "cancelled"):
        raise OpsError("continue requires a finished import; resume stopped work first")
    if previous.correction_of:
        raise OpsError("correction jobs cannot be continued as imports")
    if payload.get("limits") or payload.get("extend"):
        raise OpsError("continuations use fresh configured budgets; limit overrides are not supported")
    source_policy = policy.validate_policy(payload.get("source_policy", previous.source_policy))
    targets = set(str(value) for value in payload.get("source_ids", []))
    if targets and not targets <= {s.id for s in previous.sources}:
        raise OpsError("unknown continuation source target")
    review_omitted = bool(payload.get("review_omitted", True))
    discover = bool(payload.get("discover", True))
    raw_refresh = payload.get("refresh_source_ids", [])
    if not isinstance(raw_refresh, list) or any(not isinstance(identity, str) for identity in raw_refresh):
        raise OpsError("refresh_source_ids must be a list of source IDs")
    refresh_ids = set(raw_refresh)
    if refresh_ids and not discover:
        raise OpsError("source refresh requires discovery")
    originals = {source.id: source for source in previous.sources}
    for identity in refresh_ids:
        source = originals.get(identity)
        if source is None or source.kind != "url" or source.status != "fetched" or not source.url:
            raise OpsError("refresh targets must be fetched URL sources")
        for url in [source.url, *source.redirect_chain]:
            allowed, reason, _ = policy.url_decision(url, source_policy)
            if (
                not allowed
                or source.exclusions_hit
                or matches_exclusions(url + " " + str(source.relevance.get("anchor", "")), previous.exclusions)
            ):
                raise OpsError("refresh target is excluded by current policy: " + identity)
    raw_urls = payload.get("urls", [])
    if not isinstance(raw_urls, list) or any(not isinstance(url, str) for url in raw_urls):
        raise OpsError("continuation urls must be a list of URL strings")
    if raw_urls and not discover:
        raise OpsError("additional continuation URLs require discovery; remove --no-discover")
    try:
        added_sources = {source.url: source for source in (make_url_source(url) for url in raw_urls)}
    except (IntakeError, ValueError) as exc:
        raise OpsError(f"invalid continuation URL: {exc}") from exc
    request = {
        "parent": job_id,
        "targets": sorted(targets),
        "review_omitted": review_omitted,
        "discover": discover,
        "policy": source_policy,
        "intent": payload.get("intent", previous.intent),
    }
    if refresh_ids:
        request["refresh_source_ids"] = sorted(refresh_ids)
    if added_sources:
        request["urls"] = sorted(added_sources)
    mode = str(payload.get("mode") or previous.mode)
    if mode not in MODES:
        raise OpsError(f"unknown mode {mode!r}; expected one of {MODES}")
    if previous.private and mode != "single-pass":
        raise OpsError("private jobs run single-pass")
    if mode != previous.mode:
        # Only a mode that differs from the parent's joins the key, so an
        # explicit mode equal to the inherited one hashes like an omitted mode
        # and existing continuation keys stay stable.
        request["mode"] = mode
    page_targets = _validated_targets(payload.get("targets"), cfg, mode)
    if page_targets:
        request["page_targets"] = page_targets  # joins the key only when given
    key = str(
        payload.get("idempotency_key") or hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:24]
    )
    state = JobState(
        job_id="",
        idempotency_key=key,
        intent=str(request["intent"]),
        status="preparing",
        stage="discover" if discover else "extract",
        continuation_of=job_id,
        source_policy=source_policy,
        exclusions=list(previous.exclusions),
        sources=copy.deepcopy(previous.sources),
        inherited_source_ids=[s.id for s in previous.sources if s.status == "fetched"],
        private=previous.private,
        **({"submitted_by": previous.submitted_by} if previous.private else {}),
    )
    state.mode = mode
    previous_targets = previous.evidence_selection.get("single_pass", {}).get("targets", [])
    if mode == "single-pass":
        # Single-pass stage setup: its own page progress, never the
        # multi-stage placement ledger (single-pass places without it). Claims
        # the parent published or proved covered are not placed again, as
        # multi-stage placement.continuation marks them.
        state.evidence_selection["single_pass"] = {
            "version": 1,
            "targets": list(page_targets or previous_targets),
            "pages": {},
        }
        settled = dict(previous.evidence_selection.get("single_pass", {}).get("published_claims") or {})
        settled.update(
            {
                identity: change.path
                for change in previous.changes
                if change.status == "published"
                for identity in change.claim_ids
            }
        )
        ledger_claims = previous.evidence_selection.get("placement", {}).get("ledger", {}).get("claims", {})
        settled.update(
            {
                identity: (row.get("coverage") or {}).get("path") or row.get("target") or ""
                for identity, row in ledger_claims.items()
                if row.get("status") == "covered"
            }
        )
        if settled:
            state.evidence_selection["single_pass"]["published_claims"] = settled
    state.evidence_selection["continuation_review"] = {
        "review_omitted": review_omitted,
        "source_ids": sorted(targets),
        "refresh_source_ids": sorted(refresh_ids),
    }
    from doclayer.curator import placement

    published_claims = {
        identity: change.path
        for change in previous.changes
        if change.status == "published"
        for identity in change.claim_ids
    }
    previous_placement = previous.evidence_selection.get("placement", {})
    if previous_placement.get("ledger") and mode != "single-pass":
        state.evidence_selection["placement"] = {
            "ledger": placement.continuation(previous_placement["ledger"], published=published_claims)
        }
    # Matching source proofs may be reused; current source/version/policy
    # bindings independently invalidate them before admission. No page edit or
    # checker proof is copied into a new publication.
    state.evidence_verification = copy.deepcopy(previous.evidence_verification)
    for source in state.sources:
        allowed, reason, rule = policy.url_decision(source.url, source_policy)
        if not allowed or matches_exclusions(
            (source.url or "") + " " + str(source.relevance.get("anchor", "")), state.exclusions
        ):
            source.status, source.error, source.exclusions_hit = (
                "rejected",
                reason if not allowed else "matched batch exclusion",
                True,
            )
            continue
        source.scope.update(rule.get("applicability", {}))
        if not discover and source.status in ("pending", "scored"):
            source.status, source.error = "deferred", "discovery deferred for targeted continuation"
        # Deferred frontier is work withheld by a previous no-crawl pass,
        # not a relevance rejection. Only explicit discovery reactivates it.
        if discover and source.status == "deferred" and not source.exclusions_hit:
            source.relevance.setdefault("continuation_history", []).append(
                {
                    "parent": job_id,
                    "status": source.status,
                    "error": source.error,
                    "submitted": source.submitted,
                    "event": "discovery reactivated",
                }
            )
            source.status = (
                "scored" if "score" in source.relevance or source.relevance.get("relevant") is True else "pending"
            )
            source.error = None
            source.submitted = False  # normal discovery owns reactivated inputs
        if targets and source.id not in targets:
            continue
        if review_omitted and (
            source.omitted_sections or (targets and source.id in targets and not source.reviewed_sections)
        ):
            source.extraction_complete = False
            source.relevance["review_candidates"] = list(source.omitted_sections)
        if source.status == "failed" and discover:
            source.status, source.error = "pending", None
            source.submitted = False
    # A fresh URL is a primary submitted seed. Explicit re-submission may
    # supersede relevance scheduling; excluded identities remain protected.
    known_urls = {source.url for source in state.sources if source.url}
    existing_urls = {source.url: source for source in state.sources if source.url}
    intake_ids: set[str] = set()
    fresh_pending = False
    for source in state.sources:
        if source.id not in refresh_ids:
            continue
        # Preserve the old snapshot as history, never reuse its completion or
        # cache as the newly requested source generation. Raw claim identities
        # and old verdicts remain; their actual-source bindings re-admit them.
        history = source.relevance.setdefault("refresh_history", [])
        history.append({"parent": job_id, "source": copy.deepcopy(originals[source.id].to_dict())})
        for field in ("review_candidates", "historical_reviewed_sections", "retained_extract_hash", "links_expanded"):
            source.relevance.pop(field, None)
        source.status, source.error, source.submitted = "pending", None, True
        source.content_hash, source.extract_path, source.inspected_at = None, None, None
        source.title = source.publisher = source.published_info = None
        source.doc_kind = "other"
        source.scope = dict(policy.url_decision(source.url, source_policy)[2].get("applicability", {}))
        source.bytes = source.section_count = source.review_unit_count = 0
        source.extraction_complete = False
        source.reviewed_sections = []
        source.omitted_sections = []
        source.unknown_reviewed_sections = []
        source.obsolete_reviewed_sections = []
        source.omitted_categories = {}
        source.redirect_chain = []
        intake_ids.add(source.id)
        fresh_pending = True
    for url, source in sorted(added_sources.items()):
        existing = existing_urls.get(url)
        if existing is not None:
            # Explicit seeds can supersede a relevance-only scheduling verdict,
            # never a policy/exclusion rejection or an unexplained rejection.
            allowed, reason, rule = policy.url_decision(url, source_policy)
            eligible = existing.status in ("pending", "scored", "deferred", "failed") or (
                existing.status == "rejected" and existing.relevance.get("relevant") is False
            )
            if (
                not allowed
                or existing.exclusions_hit
                or matches_exclusions(url or "", state.exclusions)
                or not eligible
            ):
                continue
            existing.relevance.setdefault("continuation_history", []).append(
                {
                    "parent": job_id,
                    "event": "explicit primary seed",
                    "status": existing.status,
                    "error": existing.error,
                    "role": existing.role,
                    "submitted": existing.submitted,
                }
            )
            existing.status, existing.error, existing.role, existing.submitted = "pending", None, "primary", True
            existing.scope.update(rule.get("applicability", {}))
            intake_ids.add(existing.id)
            fresh_pending = True
            continue
        allowed, reason, rule = policy.url_decision(url, source_policy)
        source.scope = dict(rule.get("applicability", {}))
        if not allowed or matches_exclusions(url or "", state.exclusions):
            source.status, source.error, source.exclusions_hit = (
                "rejected",
                reason if not allowed else "matched batch exclusion",
                True,
            )
            source.submitted = False
        else:
            fresh_pending = True
            intake_ids.add(source.id)
        state.sources.append(source)
    if fresh_pending:
        state.stage = "intake"
        # Intake owns only new seeds. Historical submitted inputs (including
        # failed/excluded inputs) belong to discovery's disposition machinery.
        for source in state.sources:
            if source.id not in intake_ids:
                source.relevance.setdefault("continuation_history", []).append(
                    {
                        "parent": job_id,
                        "event": "retained input",
                        "submitted": source.submitted,
                    }
                )
                source.submitted = False
    # Legacy maps/frontiers are absent, not magically complete. Rebuild them
    # through the same discovery/extraction mechanisms and report that fact.
    if not any("links_expanded" in s.relevance for s in previous.sources):
        state.consolidation_notes.append(
            "Legacy discovery frontier reconstructed from retained extracts; exact historical assessment ordering unavailable"
        )
    created_state, created = store.create(state)
    if not created and created_state.status != "preparing":
        return {"ok": True, "job_id": created_state.job_id, "created": False, "continuation_of": job_id}
    retained_records = store.read_evidence(job_id)
    records = (
        retained_records
        if refresh_ids
        else [
            e
            for e in retained_records
            if policy.claim_decision(e.get("url"), e.get("text", ""), e.get("applicability", {}), source_policy)[0]
        ]
    )
    if not store.read_evidence(created_state.job_id):
        store.append_evidence(created_state.job_id, records)
    # Own the extracted text before queueing. Parent cache cleanup cannot
    # invalidate a queued/active child; never copy the original PDF bytes.
    child_cache = cfg.cache_dir() / created_state.job_id
    child_cache.mkdir(parents=True, exist_ok=True)
    for source in created_state.sources:
        if source.status != "fetched" or not source.extract_path:
            continue
        extract = Path(source.extract_path).resolve()
        target = child_cache / f"{source.id}.md"
        if not target.exists() and extract.is_relative_to(cfg.cache_dir().resolve()) and extract.is_file():
            temporary = target.with_suffix(".tmp")
            shutil.copyfile(extract, temporary)
            temporary.replace(target)
        if target.is_file():
            source.omitted_categories.pop("cache", None)
            source.extract_path = str(target)
            from doclayer.curator.curate import map_source_structure

            map_source_structure(
                source,
                target.read_text(encoding="utf-8"),
                char_budget=stage_config(cfg, "curation").limits.prompt_char_budget,
            )
        else:
            source.omitted_categories["cache"] = "retained extract unavailable; selective continuation cannot review it"
    # Recover the legacy frontier even for a targeted no-crawl continuation:
    # deferred work remains durable and its omission cannot disappear merely
    # because editorial placement later decides no page needs changing.
    known_urls = {source.url for source in created_state.sources if source.url}
    for parent in list(created_state.sources):
        if parent.status != "fetched" or not parent.url or parent.relevance.get("links_expanded"):
            continue
        cached = child_cache / f"{parent.id}.md"
        if not cached.is_file():
            continue
        for url, anchor in extract_links(cached.read_text(encoding="utf-8"), parent.url):
            if url in known_urls:
                continue
            known_urls.add(url)
            source = make_url_source(
                url, role="supplementary" if parent.role == "primary" else parent.role, parent=parent
            )
            source.relevance = {"anchor": anchor, "why": "legacy frontier reconstructed", "priority": "normal"}
            allowed, reason, rule = policy.url_decision(url, source_policy)
            source.scope = dict(rule.get("applicability", {}))
            if not allowed or matches_exclusions(url + " " + anchor, created_state.exclusions):
                source.status, source.error, source.exclusions_hit = (
                    "rejected",
                    reason if not allowed else "matched batch exclusion",
                    True,
                )
            elif not discover:
                source.status, source.error = "deferred", "discovery deferred for targeted continuation"
            created_state.sources.append(source)
        parent.relevance["links_expanded"] = True
    with store.control_lock(created_state.job_id):
        current = store.load(created_state.job_id)
        created_state.cancel_requested |= current.cancel_requested
        created_state.status = (
            "cancelled" if created_state.cancel_requested or current.status == "cancelled" else "queued"
        )
        store.save(created_state)
    return {
        "ok": True,
        "job_id": created_state.job_id,
        "created": created,
        "continuation_of": job_id,
        "reused_evidence": len(records),
        "inherited_sources": len(created_state.inherited_source_ids),
    }


def resume_checkpoint(state: JobState) -> str:
    """Exact observed stop generation; resume never substitutes new work."""
    return hashlib.sha256(json.dumps(state.to_dict(), sort_keys=True).encode()).hexdigest()


def _op_retire_allowance(payload: dict[str, Any], store: JobStore) -> dict[str, Any]:
    """Detach a proven stopped automatic allowance, never its unresolved work."""
    from doclayer.curator.stopped_task import JOB_STOP, RESPONSE_STOP, task_stop

    job_id = str(payload.get("job_id", ""))
    if not store.exists(job_id):
        raise OpsError("unknown retirement job")
    grant = payload.get("original_grant")
    attempt = payload.get("attempt_id")
    expected = payload.get("expected_checkpoint")
    category = payload.get("stop_category", "resource_exhausted")
    if (
        type(grant) is not int
        or not 0 < grant <= 50
        or not isinstance(attempt, str)
        or not attempt
        or not isinstance(expected, str)
        or len(expected) != 64
        or category not in ("resource_exhausted", RESPONSE_STOP, JOB_STOP)
    ):
        raise OpsError("invalid automatic allowance retirement identity")
    claim = store.job_claim(job_id)
    if not claim.acquire(blocking=False):
        raise OpsError("job is currently claimed; retirement withheld")
    try:
        with store.control_lock(job_id):
            state = store.load(job_id)
            receipt_path = store.job_dir(job_id) / "allowance-retirement.json"
            readmission = store.job_dir(job_id) / "allowance-readmission.json"
            # A job that has started publication (commit, push, PR or merge) is
            # never retired: the gap worker keeps resuming that same job, so its
            # branch/PR is never orphaned and its material never republished.
            # A stopped job (JOB_STOP) may be retired at the publish stage only
            # before anything was committed.
            publication_started = publication_has_started(state)
            if (
                readmission.exists()
                or state.idempotency_key != attempt
                or state.submitted_by not in ("doclayer-gap-dispatcher", PRIVATE_SUBMITTER)
                or state.status != "retryable"
                or state.cancel_requested
                or state.in_flight
                or resume_checkpoint(state) != expected
                or type(state.limits.get("max_model_calls")) is not int
                or state.limits["max_model_calls"] != grant
                or type(state.usage.model_calls) is not int
                or state.usage.model_calls < 0
                or (
                    category == "resource_exhausted"
                    and (state.error != "limit reached: model_calls" or state.usage.model_calls < grant)
                )
                or (category == RESPONSE_STOP and (task_stop(state) is None or state.usage.model_calls > grant))
                or publication_started
                or (category != JOB_STOP and state.stage == "publish")
            ):
                raise OpsError("automatic allowance stop changed or publication uncertain; retirement withheld")
            identity = {
                "job_id": job_id,
                "attempt_id": attempt,
                "checkpoint": expected,
                "original_grant": grant,
                "disposition": category,
            }
            if receipt_path.exists():
                receipt = json.loads(receipt_path.read_text())
                if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in identity.items()):
                    raise OpsError("foreign retirement receipt")
            else:
                receipt = {**identity, "model_calls": state.usage.model_calls, "retired_at": now_iso()}
                if category == JOB_STOP:
                    receipt.update(stage=state.stage, error=state.error, publication_started=False)
                atomic_write_json(receipt_path, receipt)
            return {"ok": True, "receipt": receipt}
    finally:
        claim.release()


def _op_resume(payload: dict[str, Any], store: JobStore) -> dict[str, Any]:
    job_id = str(payload.get("job_id", ""))
    if not store.exists(job_id):
        raise OpsError(f"unknown job: {job_id}")
    claim = store.job_claim(job_id)
    if not claim.acquire(blocking=False):
        raise OpsError("job is currently claimed; resume withheld")
    try:
        with store.control_lock(job_id):
            state = store.load(job_id)
            if state.in_flight or state.status == "preparing":
                raise OpsError("job is already active; resume withheld")
            expected = payload.get("expected_checkpoint")
            retirement = store.job_dir(job_id) / "allowance-retirement.json"
            if retirement.exists():
                if state.cancel_requested or state.status == "cancelled":
                    raise OpsError("cancelled retired work cannot be readmitted")
                if state.finished:
                    return {
                        "ok": True,
                        "job_id": job_id,
                        "status": state.status,
                        "note": "job already finished; no readmission or extension applied",
                        "can_continue": state.effective_coverage == "incomplete",
                    }
                if state.status != "retryable":
                    raise OpsError("retired work is not at a retryable stop; readmission withheld")
                extension = payload.get("extend")
                added = extension.get("max_model_calls") if isinstance(extension, dict) else None
                if (
                    expected is not None
                    or payload.get("operator_readmission") is not True
                    or type(added) is not int
                    or added <= 0
                ):
                    raise OpsError(
                        "retired automatic allowance requires explicit operator readmission and model-call extension"
                    )
                acknowledgement = store.job_dir(job_id) / "allowance-readmission.json"
                if not acknowledgement.exists():
                    atomic_write_json(
                        acknowledgement,
                        {
                            "retirement_sha256": hashlib.sha256(retirement.read_bytes()).hexdigest(),
                            "checkpoint": resume_checkpoint(state),
                            "at": now_iso(),
                            "added_model_calls": added,
                        },
                    )
            if expected is not None:
                if (
                    not isinstance(expected, str)
                    or state.status != "retryable"
                    or expected != resume_checkpoint(state)
                    or state.cancel_requested
                    or not _publication_extension_ok(payload.get("extend"), state)
                ):
                    raise OpsError("retryable checkpoint changed; conditional resume withheld")
            return _resume_stopped(payload, store, state)
    finally:
        claim.release()


def publication_has_started(state: JobState) -> bool:
    """Something of this job's publication exists: a commit, push, PR or merge."""
    pub = state.publication
    return bool(pub.commit_sha or pub.pushed or pub.pr_number or pub.merged or pub.merge_commit or pub.committed_paths)


#: Active time one conditional publication resume may add (no model calls).
PUBLICATION_RESUME_ACTIVE_S = 1800


def _publication_extension_ok(extend: Any, state: JobState) -> bool:
    """A conditional resume carries no extension, except bounded active time
    for a job whose publication has started, so that publication can finish."""
    if extend is None:
        return True
    return (
        publication_has_started(state)
        and isinstance(extend, dict)
        and set(extend) == {"max_active_seconds"}
        and type(extend["max_active_seconds"]) in (int, float)
        and 0 < extend["max_active_seconds"] <= PUBLICATION_RESUME_ACTIVE_S
    )


def _resume_stopped(payload: dict[str, Any], store: JobStore, state: JobState) -> dict[str, Any]:
    job_id = state.job_id
    if state.status == "cancelled":
        raise OpsError("a cancelled job stays cancelled; submit the work again")
    unfinished_failure = state.status == "failed" and state.stage in STAGES[:-1] and "report" not in state.stages_done
    if state.finished and state.status != "retryable" and not unfinished_failure:
        return {
            "ok": True,
            "job_id": job_id,
            "status": state.status,
            "note": "job already finished; use continue for remaining coverage (new linked publication)",
            "can_continue": state.effective_coverage == "incomplete",
        }
    # Explicit limit extension (never silently reset a hard cap).
    extend = payload.get("extend")
    if isinstance(extend, dict):
        allowed = ("max_sources", "max_source_bytes", "max_job_bytes", "max_model_calls", "max_active_seconds")
        for key, value in extend.items():
            if key in allowed and isinstance(value, (int, float)) and value > 0:
                current = float(state.limits.get(key, 0))
                state.limits[key] = current + value
    state.status = "queued"
    state.cancel_requested = False
    state.error = None
    store.save(state)
    return {"ok": True, "job_id": job_id, "status": "queued", "stage": state.stage}


def _op_cancel(payload: dict[str, Any], store: JobStore) -> dict[str, Any]:
    job_id = str(payload.get("job_id", ""))
    if not store.exists(job_id):
        raise OpsError(f"unknown job: {job_id}")
    with store.control_lock(job_id):
        return _cancel_controlled(job_id, store)


def _cancel_controlled(job_id: str, store: JobStore) -> dict[str, Any]:
    state = store.load(job_id)
    if state.finished:
        return {"ok": True, "job_id": job_id, "status": state.status, "note": "already finished"}
    state.cancel_requested = True
    store.save(state)
    claimed = None
    # A queued or stopped (retryable) job is not running: cancel it now.
    if state.status in ("queued", "retryable"):
        claim = store.job_claim(job_id)
        if claim.acquire(blocking=False):
            claimed = claim
    if claimed is not None:
        # Not running anywhere: cancel immediately.
        state.status = "cancelled"
        store.save(state)
        claimed.release()
        return {"ok": True, "job_id": job_id, "status": "cancelled"}
    return {
        "ok": True,
        "job_id": job_id,
        "status": state.status,
        "note": "cancel requested; the worker honors it at the next stage boundary",
    }


def _op_correct(payload: dict[str, Any], cfg: CuratorConfig, store: JobStore) -> dict[str, Any]:
    page = str(payload.get("page", "")).strip()
    claim = str(payload.get("claim", "")).strip()
    instruction = str(payload.get("instruction", "")).strip()
    if not page or not instruction:
        raise OpsError("correct needs at least a page and an instruction")
    record = store.add_correction(
        {
            "page": page,
            "claim": claim,
            "instruction": instruction,
            "source_evidence_hash": payload.get("source_evidence_hash", ""),
        }
    )
    checkout = Path(cfg.paths.brain_checkout).expanduser()
    existing_raw = None
    base = ""
    try:
        from doclayer.curator import gittools

        gittools.fetch(checkout, cfg.publish.remote)
        base = gittools.rev_parse(checkout, f"{cfg.publish.remote}/{cfg.publish.base_branch}")
        existing_raw = gittools.read_blob(checkout, base, page)
    except Exception:  # noqa: BLE001 - corrections must be durable even without git
        pass
    state = JobState(
        job_id="",
        idempotency_key=f"corr-{record['correction_id']}",
        intent=f"Apply operator correction on {page}: {instruction}",
        correction_of="direct",
        base_commit=base,
    )
    action = "update" if existing_raw is not None else "create"
    change = PageChange(
        group_id="g1",
        path=page,
        action=action,
        title=f"Correction: {instruction[:80]}",
        rationale=f"Operator correction: {instruction}",
        base_commit=base,
    )
    state.changes = [change]
    state.stage = "draft"
    created_state, created = store.create(state)
    return {
        "ok": True,
        "correction": record,
        "job_id": created_state.job_id,
        "created": created,
    }


def _op_list(payload: dict[str, Any], store: JobStore) -> dict[str, Any]:
    states = store.load_all()
    states.sort(key=lambda s: s.created_at, reverse=True)
    limit = int(payload.get("limit", 20))
    return {
        "ok": True,
        "jobs": [
            {
                "job_id": s.job_id,
                "status": s.status,
                "stage": s.stage,
                "intent": s.intent[:100],
                "updated_at": s.updated_at,
            }
            for s in states[:limit]
        ],
    }


def _op_inspect(payload: dict[str, Any], cfg: CuratorConfig, store: JobStore) -> dict[str, Any]:
    """Bounded inspection: a job's draft/evidence records, or the exact diff
    one change would make against its base revision."""
    job_id = str(payload.get("job_id", ""))
    if not store.exists(job_id):
        raise OpsError(f"unknown job: {job_id}")
    state = store.load(job_id)
    what = str(payload.get("what", "draft"))
    if what == "draft":
        path = str(payload.get("path", ""))
        draft = store.read_draft(job_id, path) if path else None
        if draft is None and path:
            change = next((c for c in state.changes if c.path == path), None)
            if change is not None and change.draft_body:
                from doclayer import frontmatter as fm

                draft = fm.build(change.draft_meta, change.draft_body)
        if draft is None:
            raise OpsError(f"no draft for {path!r}")
        return {"ok": True, "what": "draft", "path": path, "content": draft[:40000]}
    if what == "diff":
        path = str(payload.get("path", ""))
        change = next((c for c in state.changes if c.path == path), None)
        if change is None:
            raise OpsError(f"job has no change for {path!r}")
        from doclayer import frontmatter as fm
        from doclayer.curator import gittools

        base_text = (
            gittools.read_blob(Path(cfg.paths.brain_checkout).expanduser(), state.base_commit, path)
            if state.base_commit
            else None
        )
        new_text = fm.build(change.draft_meta, change.draft_body)
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                (base_text or "").splitlines(),
                new_text.splitlines(),
                fromfile=f"{path}@{state.base_commit[:8] or 'new'}",
                tofile=f"{path}@draft",
                lineterm="",
            )
        )
        return {"ok": True, "what": "diff", "path": path, "diff": diff[:40000]}
    if what == "evidence":
        records = store.read_evidence(job_id)
        return {"ok": True, "what": "evidence", "records": records[:100], "total": len(records)}
    raise OpsError(f"unknown inspect target: {what!r}")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain-curator-ops")
    parser.add_argument("operation")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise OpsError("request must be a JSON object")
        result = handle(args.operation, payload)
    except OpsError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return _ERROR
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"malformed JSON: {exc}"}))
        return _ERROR
    print(json.dumps(result, ensure_ascii=False))
    return _OK


if __name__ == "__main__":
    sys.exit(main())
