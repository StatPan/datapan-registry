#!/usr/bin/env python3
"""Validate datapan doctor JSON for installed registry release smoke tests."""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def validate_doctor(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("ok") is not True:
        raise ValueError("doctor ok must be true")

    registry = as_dict(payload.get("registry"), "registry")
    if registry.get("installed") is not True:
        raise ValueError("doctor registry.installed must be true")
    path = registry.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("doctor registry.path is required")
    specs = positive_int(registry.get("specs"), "doctor registry.specs")
    operations = positive_int(registry.get("operations"), "doctor registry.operations")

    return {
        "specs": specs,
        "operations": operations,
        "path": path,
        "source": registry.get("source", "<unknown>"),
    }


def validate_against_install(summary: dict[str, Any], install_payload: dict[str, Any]) -> dict[str, Any]:
    install_path = install_payload.get("registry")
    if not isinstance(install_path, str) or not install_path:
        raise ValueError("install registry path is required for doctor cross-check")
    install_specs = positive_int(install_payload.get("specs"), "install specs")

    if summary["path"] != install_path:
        raise ValueError(
            "doctor registry.path does not match install registry: "
            f"{summary['path']} != {install_path}"
        )
    if summary["specs"] != install_specs:
        raise ValueError(
            "doctor registry.specs does not match install specs: "
            f"{summary['specs']} != {install_specs}"
        )
    return {"registry": install_path, "specs": install_specs}


def build_summary_json(
    summary: dict[str, Any],
    doctor_json: pathlib.Path,
    install_json: pathlib.Path | None,
    install_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    cross_check_performed = install_summary is not None
    payload: dict[str, Any] = {
        "schema_version": "datapan.doctor-smoke-summary.v1",
        "doctor_json": doctor_json.as_posix(),
        "install_json": install_json.as_posix() if install_json is not None else None,
        "provider": "datapan-registry",
        "registry": {
            "path": summary["path"],
            "specs": summary["specs"],
            "operations": summary["operations"],
            "source": summary["source"],
        },
        "install_cross_check": {
            "performed": cross_check_performed,
        },
    }
    if install_summary is not None:
        payload["install_cross_check"].update(
            {
                "registry": install_summary["registry"],
                "specs": install_summary["specs"],
            }
        )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install-json",
        type=pathlib.Path,
        help="optional install smoke JSON to cross-check registry path and specs",
    )
    parser.add_argument(
        "--summary-json",
        type=pathlib.Path,
        help="optional path for a structured doctor smoke summary JSON artifact",
    )
    parser.add_argument("doctor_json", type=pathlib.Path)
    args = parser.parse_args()

    try:
        summary = validate_doctor(as_dict(load_json(args.doctor_json), args.doctor_json.as_posix()))
        install_summary = None
        if args.install_json is not None:
            install_summary = validate_against_install(
                summary,
                as_dict(load_json(args.install_json), args.install_json.as_posix()),
            )
        if args.summary_json is not None:
            write_json(
                args.summary_json,
                build_summary_json(summary, args.doctor_json, args.install_json, install_summary),
            )
    except Exception as exc:  # noqa: BLE001 - CI should show the failed invariant
        print(f"FAIL {args.doctor_json}: {exc}")
        return 1

    print(
        f"ok {args.doctor_json} "
        f"(specs={summary['specs']}, operations={summary['operations']}, source={summary['source']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
