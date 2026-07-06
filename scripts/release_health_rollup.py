#!/usr/bin/env python3
"""Build and validate datapan-registry release-health rollup payloads."""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from typing import Any


INSTALL_SCHEMA_VERSION = "datapan.install-smoke-summary.v1"
DOCTOR_SCHEMA_VERSION = "datapan.doctor-smoke-summary.v1"
ROLLUP_SCHEMA_VERSION = "datapan.release-health-rollup.v1"
PROVIDER = "datapan-registry"
EXPECTED_SUMMARY_INPUTS = {
    "current": {
        "install": ".datapan/ci/current-release-install-smoke.json",
        "doctor": ".datapan/ci/current-release-doctor-smoke.json",
    },
    "latest": {
        "install": ".datapan/ci/latest-release-install-smoke.json",
        "doctor": ".datapan/ci/latest-release-doctor-smoke.json",
    },
}


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: pathlib.Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def require_positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def require_count(value: object, label: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def require_schema(summary: dict[str, Any], expected: str, label: str) -> None:
    actual = summary.get("schema_version")
    if actual != expected:
        raise ValueError(f"{label}.schema_version must be {expected}, got {actual!r}")
    provider = summary.get("provider")
    if provider != PROVIDER:
        raise ValueError(f"{label}.provider must be {PROVIDER}, got {provider!r}")


def path_has_expected_suffix(actual: object, expected_suffix: str, label: str) -> str:
    actual_path = require_string(actual, label)
    normalized_actual = actual_path.replace("\\", "/")
    normalized_expected = expected_suffix.replace("\\", "/")
    if normalized_actual != normalized_expected and not normalized_actual.endswith(f"/{normalized_expected}"):
        raise ValueError(f"{label} must reference {expected_suffix}, got {actual_path}")
    return actual_path


def build_check(scope: str, install_path: pathlib.Path, doctor_path: pathlib.Path) -> dict[str, Any]:
    if scope not in {"current", "latest"}:
        raise ValueError(f"unsupported release-health scope: {scope}")

    install = as_dict(load_json(install_path), install_path.as_posix())
    doctor = as_dict(load_json(doctor_path), doctor_path.as_posix())
    require_schema(install, INSTALL_SCHEMA_VERSION, install_path.as_posix())
    require_schema(doctor, DOCTOR_SCHEMA_VERSION, doctor_path.as_posix())
    expected_inputs = EXPECTED_SUMMARY_INPUTS[scope]
    path_has_expected_suffix(install_path.as_posix(), expected_inputs["install"], f"{scope}.install_summary")
    path_has_expected_suffix(doctor_path.as_posix(), expected_inputs["doctor"], f"{scope}.doctor_summary")

    install_registry = as_dict(install.get("registry"), f"{install_path}.registry")
    install_release = as_dict(install.get("release"), f"{install_path}.release")
    doctor_registry = as_dict(doctor.get("registry"), f"{doctor_path}.registry")
    doctor_cross_check = as_dict(doctor.get("install_cross_check"), f"{doctor_path}.install_cross_check")

    install_specs = require_positive_int(install.get("specs"), f"{install_path}.specs")
    doctor_specs = require_positive_int(doctor_registry.get("specs"), f"{doctor_path}.registry.specs")
    if install_specs != doctor_specs:
        raise ValueError(f"{scope} install specs do not match doctor specs: {install_specs} != {doctor_specs}")

    install_registry_path = require_string(install_registry.get("path"), f"{install_path}.registry.path")
    doctor_registry_path = require_string(doctor_registry.get("path"), f"{doctor_path}.registry.path")
    if install_registry_path != doctor_registry_path:
        raise ValueError(
            f"{scope} install registry path does not match doctor registry path: "
            f"{install_registry_path} != {doctor_registry_path}"
        )

    cross_check_performed = require_bool(
        doctor_cross_check.get("performed"), f"{doctor_path}.install_cross_check.performed"
    )
    if not cross_check_performed:
        raise ValueError(f"{scope} doctor summary must include install cross-check evidence")
    if doctor_cross_check.get("registry") != doctor_registry_path:
        raise ValueError(f"{scope} doctor install cross-check registry does not match doctor registry path")
    if doctor_cross_check.get("specs") != doctor_specs:
        raise ValueError(f"{scope} doctor install cross-check specs do not match doctor registry specs")

    release_zip_checked = require_bool(
        install_release.get("release_zip_checked"), f"{install_path}.release.release_zip_checked"
    )
    registry_checksum_checked = require_bool(
        install_registry.get("checksum_checked"), f"{install_path}.registry.checksum_checked"
    )
    if release_zip_checked != registry_checksum_checked:
        raise ValueError(f"{scope} release_zip_checked must match registry checksum_checked")
    if scope == "current" and not release_zip_checked:
        raise ValueError("current release-health rollup requires release zip checksum evidence")
    if scope == "latest" and release_zip_checked:
        raise ValueError("latest release-health rollup must not report local release zip checksum evidence")

    install_payload: dict[str, Any] = {
        "summary_json": install_path.as_posix(),
        "mode": require_string(install.get("mode"), f"{install_path}.mode"),
        "specs": install_specs,
        "registry_path": install_registry_path,
        "registry_install_bytes": require_positive_int(
            install_registry.get("install_bytes"), f"{install_path}.registry.install_bytes"
        ),
        "registry_checksum_checked": registry_checksum_checked,
        "release_zip_checked": release_zip_checked,
        "shards_asset_present": require_bool(
            install_release.get("shards_asset_present"), f"{install_path}.release.shards_asset_present"
        ),
        "shards_validated": require_bool(
            install_release.get("shards_validated"), f"{install_path}.release.shards_validated"
        ),
        "shards_inventory_present": require_bool(
            install_release.get("shards_inventory_present"), f"{install_path}.release.shards_inventory_present"
        ),
        "shards_count": require_count(install_release.get("shards_count"), f"{install_path}.release.shards_count"),
        "shards_records": require_count(
            install_release.get("shards_records"), f"{install_path}.release.shards_records"
        ),
    }

    if release_zip_checked:
        install_payload["release_zip"] = require_string(
            install_release.get("release_zip"), f"{install_path}.release.release_zip"
        )
        install_payload["registry_bytes"] = require_positive_int(
            install_registry.get("bytes"), f"{install_path}.registry.bytes"
        )
        install_payload["registry_sha256"] = require_string(
            install_registry.get("sha256"), f"{install_path}.registry.sha256"
        )
    else:
        if install_release.get("release_zip") is not None:
            raise ValueError(f"{scope} unchecked release summary must not include release_zip")
        for field in ("bytes", "sha256", "canonical_archive_path"):
            if field in install_registry:
                raise ValueError(f"{scope} unchecked release summary must not include registry.{field}")

    doctor_payload = {
        "summary_json": doctor_path.as_posix(),
        "install_json": require_string(doctor.get("install_json"), f"{doctor_path}.install_json"),
        "specs": doctor_specs,
        "operations": require_positive_int(doctor_registry.get("operations"), f"{doctor_path}.registry.operations"),
        "registry_path": doctor_registry_path,
        "source": require_string(doctor_registry.get("source"), f"{doctor_path}.registry.source"),
        "install_cross_check_performed": cross_check_performed,
    }

    return {
        "scope": scope,
        "ok": True,
        "install": install_payload,
        "doctor": doctor_payload,
    }


def build_rollup(
    current_install: pathlib.Path,
    current_doctor: pathlib.Path,
    latest_install: pathlib.Path,
    latest_doctor: pathlib.Path,
) -> dict[str, Any]:
    checks = [
        build_check("current", current_install, current_doctor),
        build_check("latest", latest_install, latest_doctor),
    ]
    checks_passed = sum(1 for check in checks if check.get("ok") is True)
    return {
        "schema_version": ROLLUP_SCHEMA_VERSION,
        "provider": PROVIDER,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "ok": checks_passed == len(checks),
        "summary": {
            "checks_total": len(checks),
            "checks_passed": checks_passed,
            "scopes": [check["scope"] for check in checks],
            "current_release_zip_checked": checks[0]["install"]["release_zip_checked"],
            "latest_release_zip_checked": checks[1]["install"]["release_zip_checked"],
        },
        "checks": checks,
    }


def validate_rollup_consistency(rollup: dict[str, Any]) -> None:
    require_schema(rollup, ROLLUP_SCHEMA_VERSION, "rollup")
    if rollup.get("ok") is not True:
        raise ValueError("rollup.ok must be true")

    summary = as_dict(rollup.get("summary"), "rollup.summary")
    checks_value = rollup.get("checks")
    if not isinstance(checks_value, list):
        raise ValueError("rollup.checks must be an array")
    checks = [as_dict(check, f"rollup.checks[{index}]") for index, check in enumerate(checks_value)]
    scopes = [check.get("scope") for check in checks]
    if scopes != ["current", "latest"]:
        raise ValueError(f"rollup.checks must contain current then latest scopes, got {scopes!r}")
    if summary.get("checks_total") != len(checks):
        raise ValueError("rollup.summary.checks_total must match checks length")
    checks_passed = sum(1 for check in checks if check.get("ok") is True)
    if summary.get("checks_passed") != checks_passed:
        raise ValueError("rollup.summary.checks_passed must match passed checks")
    if summary.get("scopes") != scopes:
        raise ValueError("rollup.summary.scopes must match check scopes")

    for check in checks:
        scope = require_string(check.get("scope"), "rollup.check.scope")
        if check.get("ok") is not True:
            raise ValueError(f"{scope} rollup check must be ok")
        install = as_dict(check.get("install"), f"{scope}.install")
        doctor = as_dict(check.get("doctor"), f"{scope}.doctor")
        expected_inputs = EXPECTED_SUMMARY_INPUTS[scope]
        path_has_expected_suffix(
            install.get("summary_json"),
            expected_inputs["install"],
            f"{scope}.install.summary_json",
        )
        path_has_expected_suffix(
            doctor.get("summary_json"),
            expected_inputs["doctor"],
            f"{scope}.doctor.summary_json",
        )
        install_specs = require_positive_int(install.get("specs"), f"{scope}.install.specs")
        doctor_specs = require_positive_int(doctor.get("specs"), f"{scope}.doctor.specs")
        if install_specs != doctor_specs:
            raise ValueError(f"{scope} install specs do not match doctor specs")
        if install.get("registry_path") != doctor.get("registry_path"):
            raise ValueError(f"{scope} install registry path does not match doctor registry path")
        if doctor.get("install_cross_check_performed") is not True:
            raise ValueError(f"{scope} doctor install cross-check must be performed")

        release_zip_checked = require_bool(install.get("release_zip_checked"), f"{scope}.install.release_zip_checked")
        checksum_checked = require_bool(
            install.get("registry_checksum_checked"), f"{scope}.install.registry_checksum_checked"
        )
        if release_zip_checked != checksum_checked:
            raise ValueError(f"{scope} release zip and registry checksum flags must match")
        if scope == "current":
            if not release_zip_checked:
                raise ValueError("current release zip checksum evidence is required")
            require_string(install.get("release_zip"), "current.install.release_zip")
            require_positive_int(install.get("registry_bytes"), "current.install.registry_bytes")
            require_string(install.get("registry_sha256"), "current.install.registry_sha256")
        elif scope == "latest":
            if release_zip_checked:
                raise ValueError("latest release zip checksum evidence must be false")
            for field in ("release_zip", "registry_bytes", "registry_sha256"):
                if field in install:
                    raise ValueError(f"latest unchecked release summary must not include install.{field}")
        else:
            raise ValueError(f"unsupported rollup scope: {scope}")

    if summary.get("current_release_zip_checked") is not True:
        raise ValueError("rollup.summary.current_release_zip_checked must be true")
    if summary.get("latest_release_zip_checked") is not False:
        raise ValueError("rollup.summary.latest_release_zip_checked must be false")
