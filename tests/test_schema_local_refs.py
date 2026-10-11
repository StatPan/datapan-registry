from __future__ import annotations

import json
from pathlib import Path
import unittest

import jsonschema


ROOT = Path(__file__).resolve().parents[1]


def resolve_pointer(document: object, reference: str) -> object:
    if not reference.startswith("#/"):
        raise AssertionError(f"not a local JSON Pointer reference: {reference}")
    value = document
    for raw in reference[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if token not in value:
                raise AssertionError(f"unresolved local schema reference: {reference}")
            value = value[token]
        elif isinstance(value, list):
            if not token.isdigit() or int(token) >= len(value):
                raise AssertionError(f"unresolved local schema reference: {reference}")
            value = value[int(token)]
        else:
            raise AssertionError(f"unresolved local schema reference: {reference}")
    return value


class SchemaLocalReferenceTests(unittest.TestCase):
    def test_owned_schema_local_references_resolve(self) -> None:
        paths = sorted((ROOT / "schemas").glob("datapan.*.schema.json"))
        self.assertTrue(paths)
        for path in paths:
            schema = json.loads(path.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator.check_schema(schema)

            def visit(value: object) -> None:
                if isinstance(value, dict):
                    reference = value.get("$ref")
                    if isinstance(reference, str) and reference.startswith("#/"):
                        with self.subTest(schema=path.name, ref=reference):
                            resolve_pointer(schema, reference)
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)

            visit(schema)

    def test_policy_qname_accepts_documented_empty_namespace(self) -> None:
        schema_path = ROOT / "schemas/datapan.operation-observation-policy.v1.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema["$defs"]["qname"], format_checker=jsonschema.FormatChecker())
        validator.validate({"namespace": "", "local_name": "Response"})
