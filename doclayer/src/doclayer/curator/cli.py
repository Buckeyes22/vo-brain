"""The brain-curator CLI: the same operations the MCP tools expose.

Subcommands: submit, status, result, resume, cancel, correct, list, inspect,
and `ops` (a stdin/stdout JSON passthrough used by the SSH transport itself).

Default transport is local on the worker host itself and SSH elsewhere; `--local`
(or BRAIN_CURATOR_LOCAL=1) forces local and BRAIN_CURATOR_LOCAL=0 forces SSH.
Local runs against the local job store, which is how the worker host itself and
the test suite drive it. Output is one JSON document.

Usage examples:
  brain-curator submit --intent "Model Studio quotas" --url https://... --file notes.md
  brain-curator status --job job-abc123
  brain-curator result --job job-abc123
  brain-curator cancel  --job job-abc123
  brain-curator resume  --job job-abc123 --extend max_model_calls=50
  brain-curator correct --page wiki/x.md --claim "..." --instruction "..."
  brain-curator inspect --job job-abc --what diff --path wiki/x.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from doclayer.curator.config import MODES, load_config
from doclayer.curator.transport import LocalTransport, TransportError, transport_for


def _add_submit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--intent", required=True, help="topic / task instruction")
    parser.add_argument("--url", action="append", default=[], help="seed URL (repeatable)")
    parser.add_argument(
        "--text-file",
        action="append",
        default=[],
        help="read this local file and submit it as pasted text (repeatable)",
    )
    parser.add_argument("--file", action="append", default=[], help="transfer this md/txt/pdf file (repeatable)")
    parser.add_argument("--directory", default=None, help="import supported files from a directory")
    parser.add_argument("--manifest", default=None, help="YAML batch manifest file (transferred)")
    parser.add_argument("--exclude", action="append", default=[], help="material to leave out (repeatable)")
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=None,
        help="pipeline mode (default: the configured pipeline.default_mode, single-pass)",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="existing/new wiki page path the import should update (repeatable)",
    )
    parser.add_argument("--idempotency-key", default=None, help="submission key (derived from content by default)")
    parser.add_argument(
        "--limit",
        action="append",
        default=[],
        help="override one workload limit, e.g. max_sources=80 (repeatable)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain-curator")
    parser.add_argument("--local", action="store_true", help="use the local job store, not SSH")
    parser.add_argument("--config", default=None, help="config file path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="submit material for curation")
    _add_submit_args(p_submit)

    for name, help_text in (
        ("status", "job status"),
        ("cancel", "cancel a job"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--job", required=True)
    p_result = sub.add_parser("result", help="completion digest + outcome")
    p_result.add_argument("--job", required=True)
    p_result.add_argument("--no-digest", action="store_true")

    p_resume = sub.add_parser("resume", help="requeue a stopped/retryable job")
    p_resume.add_argument("--job", required=True)
    p_resume.add_argument(
        "--operator-readmission",
        action="store_true",
        help="explicitly re-admit a retired automatic allowance; requires positive model-call extension",
    )
    p_resume.add_argument(
        "--extend",
        action="append",
        default=[],
        help="explicitly extend a limit, e.g. max_model_calls=50 (repeatable)",
    )

    p_continue = sub.add_parser("continue", help="new linked import for remaining coverage")
    p_continue.add_argument("--job", required=True)
    p_continue.add_argument("--source", action="append", default=[])
    p_continue.add_argument(
        "--refresh-source", action="append", default=[], help="refetch a retained fetched URL source in the new import"
    )
    p_continue.add_argument(
        "--url", action="append", default=[], help="additional primary URL seed (requires discovery)"
    )
    p_continue.add_argument("--no-discover", action="store_true")
    p_continue.add_argument("--no-review-omitted", action="store_true")
    p_continue.add_argument("--intent", default=None)
    p_continue.add_argument(
        "--mode",
        choices=MODES,
        default=None,
        help="pipeline mode for the new import (default: inherit the parent's)",
    )
    p_continue.add_argument(
        "--target",
        action="append",
        default=[],
        help="single-pass target page for the new import (repeatable; replaces the parent's targets)",
    )
    p_continue.add_argument("--policy-manifest", default=None)

    p_correct = sub.add_parser("correct", help="record an operator correction and fix the page")
    p_correct.add_argument("--page", required=True)
    p_correct.add_argument("--claim", default="", help="the corrected claim (replay protection)")
    p_correct.add_argument("--instruction", required=True)

    sub.add_parser("list", help="recent jobs")

    p_inspect = sub.add_parser("inspect", help="inspect a job's draft/diff/evidence")
    p_inspect.add_argument("--job", required=True)
    p_inspect.add_argument("--what", default="diff", choices=["diff", "draft", "evidence"])
    p_inspect.add_argument("--path", default=None)

    p_ops = sub.add_parser("ops", help="raw JSON operation on stdin (transport entry point)")
    p_ops.add_argument("operation")
    return parser


def _submit_payload(args: argparse.Namespace) -> tuple[dict[str, Any], list[Path], Path | None]:
    payload: dict[str, Any] = {"intent": args.intent}
    payload["urls"] = list(args.url)
    texts = []
    for text_file in args.text_file:
        path = Path(text_file).expanduser()
        texts.append({"label": path.name, "text": path.read_text(encoding="utf-8")})
    payload["texts"] = texts
    payload["exclusions"] = list(args.exclude)
    if args.idempotency_key:
        payload["idempotency_key"] = args.idempotency_key
    if args.mode:
        payload["mode"] = args.mode
    if args.target:
        payload["targets"] = list(args.target)
    limits: dict[str, float] = {}
    for spec in args.limit:
        key, _, value = spec.partition("=")
        try:
            limits[key.strip()] = float(value)
        except ValueError:
            raise SystemExit(f"bad --limit {spec!r}; expected key=value")
    if limits:
        payload["limits"] = limits
    files = [Path(f).expanduser() for f in args.file]
    manifest_path = Path(args.manifest).expanduser() if args.manifest else None
    if manifest_path is not None:
        files.append(manifest_path)
        import hashlib

        payload["manifest_file"] = manifest_path.name
        payload["manifest_digest"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()[:16]
    directory = Path(args.directory).expanduser() if args.directory else None
    return payload, files, directory


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    transport = LocalTransport(cfg) if args.local else transport_for(cfg)

    try:
        if args.command == "submit":
            payload, files, directory = _submit_payload(args)
            reply = transport.call("submit", payload, files=files, directory=directory)
        elif args.command in ("status", "result", "cancel"):
            payload: dict[str, Any] = {"job_id": args.job}
            if args.command == "result" and args.no_digest:
                payload["include_digest"] = False
            reply = transport.call(args.command, payload)
        elif args.command == "resume":
            extend = {}
            for spec in args.extend:
                key, _, value = spec.partition("=")
                try:
                    parsed = float(value)
                    extend[key.strip()] = (
                        int(parsed)
                        if args.operator_readmission and key.strip() == "max_model_calls" and parsed.is_integer()
                        else parsed
                    )
                except ValueError:
                    raise SystemExit(f"bad --extend {spec!r}; expected key=value")
            payload = {"job_id": args.job, "extend": extend or None}
            if args.operator_readmission:
                payload["operator_readmission"] = True
            reply = transport.call("resume", payload)
        elif args.command == "continue":
            payload = {
                "job_id": args.job,
                "source_ids": args.source,
                "urls": args.url,
                "discover": not args.no_discover,
                "review_omitted": not args.no_review_omitted,
            }
            if args.refresh_source:
                payload["refresh_source_ids"] = args.refresh_source
            if args.intent:
                payload["intent"] = args.intent
            if args.mode:
                payload["mode"] = args.mode
            if args.target:
                payload["targets"] = list(args.target)
            if args.policy_manifest:
                from doclayer.curator.intake import load_manifest

                payload["source_policy"] = load_manifest(Path(args.policy_manifest).expanduser()).get(
                    "source_policy", {}
                )
            reply = transport.call("continue", payload)
        elif args.command == "correct":
            reply = transport.call(
                "correct",
                {
                    "page": args.page,
                    "claim": args.claim,
                    "instruction": args.instruction,
                },
            )
        elif args.command == "list":
            reply = transport.call("list", {})
        elif args.command == "inspect":
            payload = {"job_id": args.job, "what": args.what}
            if args.path:
                payload["path"] = args.path
            reply = transport.call("inspect", payload)
        elif args.command == "ops":
            # Passthrough: the SSH transport's own remote entry point.
            from doclayer.curator import ops

            raw = sys.stdin.read()
            try:
                payload = json.loads(raw) if raw.strip() else {}
                if not isinstance(payload, dict):
                    raise ValueError("request must be a JSON object")
                print(json.dumps(ops.handle(args.operation, payload, cfg=cfg), ensure_ascii=False))
                return 0
            except (ValueError, json.JSONDecodeError) as exc:
                print(json.dumps({"ok": False, "error": str(exc)}))
                return 3
            except Exception as exc:  # noqa: BLE001
                print(json.dumps({"ok": False, "error": str(exc)[:300]}))
                return 3
        else:  # pragma: no cover
            parser.error(f"unknown command {args.command!r}")
            return 2
    except TransportError as exc:
        print(json.dumps({"ok": False, "error": f"transport: {exc}"}))
        return 4

    print(json.dumps(reply, ensure_ascii=False, indent=2))
    return 0 if reply.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
