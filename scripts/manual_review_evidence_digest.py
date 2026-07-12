"""Stable digests for manual-review evidence bindings."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


def compatibility_binding_sha256(record: dict[str, Any]) -> str:
    """Hash compatibility semantics without manifest-owned artifact digests."""
    normalized = copy.deepcopy(record)
    contracts = normalized.get("manifest_evidence_contracts", [])
    if not isinstance(contracts, list):
        raise ValueError("compatibility manifest_evidence_contracts must be an array")
    for contract in contracts:
        if not isinstance(contract, dict):
            raise ValueError("compatibility manifest evidence contract must be an object")
        contract.pop("bytes", None)
        contract.pop("sha256", None)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
