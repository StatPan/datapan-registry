#!/usr/bin/env python3
"""Validate a credential runtime collection session handoff artifact."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shlex
import sys
import tempfile
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before validating credential runtime sessions") from exc


DEFAULT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-collection-session.v1.schema.json")
DEFAULT_SESSION = pathlib.Path(".datapan/runtime-evidence/credential-runtime-collection-session.json")
SECRET_MARKER_RE = re.compile(
    r"(<secret>|credential_value|authorization:|bearer\s+|api[_-]?secret|service[_-]?key=)",
    re.IGNORECASE,
)


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def render_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def string_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def queue_sources(queue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw_source in as_list(queue.get("sources"), "queue.sources"):
        source = as_dict(raw_source, "queue.sources[]")
        source_id = string_value(source.get("source_id"), "queue.source_id")
        result[source_id] = source
    return result


def credential_env_names(source: dict[str, Any]) -> list[str]:
    command = string_value(source.get("operator_command"), "queue.source.operator_command")
    return [
        part.split("=", 1)[0]
        for part in shlex.split(command)
        if part.endswith("=<secret>")
    ]


def validate_schema(session: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(session), key=lambda error: list(error.path))
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("; ".join(rendered))


def validate_redaction(session: dict[str, Any], sources_by_id: dict[str, dict[str, Any]]) -> None:
    rendered = render_json(session)
    match = SECRET_MARKER_RE.search(rendered)
    if match:
        raise ValueError(f"session contains secret marker: {match.group(0)}")

    for source in sources_by_id.values():
        for env_name in credential_env_names(source):
            env_value = os.environ.get(env_name)
            if env_value and env_value in rendered:
                raise ValueError(f"session contains current credential env value for {env_name}")


def validate_summary(session: dict[str, Any]) -> None:
    summary = as_dict(session.get("summary"), "session.summary")
    results = [as_dict(result, "session.results[]") for result in as_list(session.get("results"), "session.results")]
    counts = {
        "sources": len(results),
        "succeeded": sum(1 for result in results if result.get("status") == "succeeded"),
        "skipped_not_ready": sum(1 for result in results if result.get("status") == "skipped_not_ready"),
        "failed": sum(1 for result in results if result.get("status") == "failed"),
    }
    for key, expected in counts.items():
        if summary.get(key) != expected:
            raise ValueError(f"summary.{key} expected {expected}, got {summary.get(key)}")
    if summary.get("checked_in_secrets_allowed") is not False:
        raise ValueError("summary.checked_in_secrets_allowed must be false")


def validate_results(
    session: dict[str, Any],
    queue_path: pathlib.Path,
    queue: dict[str, Any],
    *,
    require_complete_source_set: bool,
) -> None:
    if session.get("queue") != queue_path.as_posix():
        raise ValueError(f"session.queue expected {queue_path.as_posix()}, got {session.get('queue')}")

    sources_by_id = queue_sources(queue)
    results = [as_dict(result, "session.results[]") for result in as_list(session.get("results"), "session.results")]
    seen: set[str] = set()
    for result in results:
        source_id = string_value(result.get("source_id"), "session.result.source_id")
        if source_id in seen:
            raise ValueError(f"duplicate session result: {source_id}")
        seen.add(source_id)
        if source_id not in sources_by_id:
            raise ValueError(f"session result source is not in queue: {source_id}")
        source = sources_by_id[source_id]
        status = string_value(result.get("status"), f"{source_id}.status")
        if status == "succeeded":
            expected_staged = string_value(source.get("staged_receipt_path"), f"{source_id}.staged_receipt_path")
            if result.get("staged_receipt_path") != expected_staged:
                raise ValueError(f"{source_id}: staged_receipt_path does not match queue")
        elif status == "skipped_not_ready":
            reasons = [
                string_value(reason, f"{source_id}.reasons[]")
                for reason in as_list(result.get("reasons"), f"{source_id}.reasons")
            ]
            if result.get("candidate_batch") != source.get("candidate_batch"):
                raise ValueError(f"{source_id}: candidate_batch does not match queue")
            if result.get("reviewed_receipt_path") != source.get("reviewed_receipt_path"):
                raise ValueError(f"{source_id}: reviewed_receipt_path does not match queue")
            if result.get("candidate_batch_present") is False and "missing_candidate_batch" not in reasons:
                raise ValueError(f"{source_id}: missing candidate batch reason is absent")
            if result.get("reviewed_receipt_present") is True and "reviewed_receipt_present" not in reasons:
                raise ValueError(f"{source_id}: reviewed receipt reason is absent")
            if result.get("missing_credential_envs") and "missing_credential_env" not in reasons:
                raise ValueError(f"{source_id}: missing credential env reason is absent")
        elif status != "failed":
            raise ValueError(f"{source_id}: unsupported status {status}")

    if require_complete_source_set and seen != set(sources_by_id):
        missing = sorted(set(sources_by_id) - seen)
        extra = sorted(seen - set(sources_by_id))
        raise ValueError(f"session source set mismatch missing={missing} extra={extra}")


def validate_session(
    session: dict[str, Any],
    *,
    session_path: pathlib.Path,
    queue_path: pathlib.Path,
    schema_path: pathlib.Path,
    require_complete_source_set: bool,
) -> None:
    queue = load_json(queue_path)
    validate_schema(session, schema_path)
    validate_summary(session)
    validate_results(
        session,
        queue_path,
        queue,
        require_complete_source_set=require_complete_source_set,
    )
    validate_redaction(session, queue_sources(queue))


def self_test(schema_path: pathlib.Path) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = pathlib.Path(temp_dir)
        queue_path = root / "queue.json"
        session_path = root / "session.json"
        queue = {
            "sources": [
                {
                    "source_id": "alpha",
                    "operator_command": "datapan source runtime verify ALPHA_KEY=<secret>",
                    "candidate_batch": "reports/alpha/runtime-candidates.json",
                    "staged_receipt_path": ".datapan/runtime-evidence/alpha-credentialed-receipt.json",
                    "reviewed_receipt_path": "reports/credential-runtime-receipts/alpha-credentialed-receipt.json",
                }
            ]
        }
        session = {
            "schema_version": "datapan.credential-runtime-collection-session.v1",
            "queue": queue_path.as_posix(),
            "summary": {
                "sources": 1,
                "succeeded": 0,
                "skipped_not_ready": 1,
                "failed": 0,
                "checked_in_secrets_allowed": False,
                "next_action": "review_and_promote_staged_receipts",
            },
            "results": [
                {
                    "source_id": "alpha",
                    "status": "skipped_not_ready",
                    "reasons": ["missing_credential_env"],
                    "missing_credential_envs": ["ALPHA_KEY"],
                    "candidate_batch": "reports/alpha/runtime-candidates.json",
                    "candidate_batch_present": True,
                    "reviewed_receipt_path": "reports/credential-runtime-receipts/alpha-credentialed-receipt.json",
                    "reviewed_receipt_present": False,
                    "next_action": "resolve_readiness_then_rerun_batch",
                }
            ],
        }
        queue_path.write_text(render_json(queue), encoding="utf-8")
        session_path.write_text(render_json(session), encoding="utf-8")
        validate_session(
            load_json(session_path),
            session_path=session_path,
            queue_path=queue_path,
            schema_path=schema_path,
            require_complete_source_set=True,
        )
        session["results"][0]["candidate_batch"] = "authorization: bearer token"
        try:
            validate_session(
                session,
                session_path=session_path,
                queue_path=queue_path,
                schema_path=schema_path,
                require_complete_source_set=True,
            )
        except ValueError:
            return
        raise ValueError("self-test failed: secret marker was accepted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", nargs="?", default=DEFAULT_SESSION, type=pathlib.Path)
    parser.add_argument("--queue", default=DEFAULT_QUEUE, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--require-complete-source-set", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    try:
        if args.self_test:
            self_test(args.schema)
            print("ok credential runtime collection session validator self-test")
            return 0
        validate_session(
            load_json(args.session),
            session_path=args.session,
            queue_path=args.queue,
            schema_path=args.schema,
            require_complete_source_set=args.require_complete_source_set,
        )
    except Exception as exc:  # noqa: BLE001 - operators need the failed invariant
        print(f"FAIL credential runtime collection session: {exc}", file=sys.stderr)
        return 1

    print(f"ok {args.session} (credential runtime collection session)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
