#!/usr/bin/env python3
"""Validate Factory v2 contract schemas, fixtures, and cross-contract invariants.

The validator intentionally uses only the Python standard library so the
contract gate can run in a minimal Controller or CI environment. It supports
the small JSON Schema 2020-12 subset used by these schemas and adds the
identity checks that JSON Schema cannot express (for example, candidate tuple
equality across independent verifier records).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


class SchemaValidator:
    """Small deterministic validator for the schema vocabulary used here."""

    def __init__(self, schema_path: Path) -> None:
        self.schema_path = schema_path.resolve()
        self.document_cache: dict[Path, Any] = {}

    def load_document(self, path: Path) -> Any:
        path = path.resolve()
        if path not in self.document_cache:
            with path.open(encoding="utf-8") as handle:
                self.document_cache[path] = json.load(handle)
        return self.document_cache[path]

    def resolve_ref(self, reference: str, current_file: Path) -> tuple[Any, Path]:
        document_name, _, fragment = reference.partition("#")
        document_path = current_file if not document_name else current_file.parent / document_name
        document = self.load_document(document_path)
        if not fragment:
            return document, document_path.resolve()
        target: Any = document
        for component in fragment.lstrip("/").split("/"):
            component = component.replace("~1", "/").replace("~0", "~")
            target = target[component]
        return target, document_path.resolve()

    @staticmethod
    def json_type(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, str):
            return "string"
        if isinstance(value, list):
            return "array"
        if isinstance(value, dict):
            return "object"
        return "unknown"

    def validate(
        self,
        value: Any,
        schema: dict[str, Any],
        path: str = "$",
        current_file: Path | None = None,
    ) -> list[str]:
        current_file = current_file or self.schema_path
        errors: list[str] = []

        if "$ref" in schema:
            try:
                target, target_file = self.resolve_ref(schema["$ref"], current_file)
            except (KeyError, FileNotFoundError, json.JSONDecodeError) as exc:
                return [f"{path}: cannot resolve {schema['$ref']!r}: {exc}"]
            return self.validate(value, target, path, target_file)

        for subschema in schema.get("allOf", []):
            errors.extend(self.validate(value, subschema, path, current_file))

        if "const" in schema and value != schema["const"]:
            errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")

        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: expected one of {schema['enum']!r}, got {value!r}")

        expected_types = schema.get("type")
        if expected_types is not None:
            if isinstance(expected_types, str):
                expected_types = [expected_types]
            actual_type = self.json_type(value)
            if actual_type not in expected_types and not (
                actual_type == "integer" and "number" in expected_types
            ):
                return errors + [
                    f"{path}: expected type {expected_types!r}, got {actual_type}"
                ]

        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                errors.append(f"{path}: string is shorter than minLength")
            if "pattern" in schema and re.search(schema["pattern"], value) is None:
                errors.append(f"{path}: value does not match {schema['pattern']!r}")
            if schema.get("format") == "date-time":
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        errors.append(f"{path}: date-time must include a timezone")
                except ValueError:
                    errors.append(f"{path}: invalid date-time")

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(f"{path}: number is below minimum")

        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                errors.append(f"{path}: array has fewer than minItems")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                errors.append(f"{path}: array has more than maxItems")
            if "items" in schema:
                for index, item in enumerate(value):
                    errors.extend(
                        self.validate(item, schema["items"], f"{path}[{index}]", current_file)
                    )

        if isinstance(value, dict):
            required = schema.get("required", [])
            for key in required:
                if key not in value:
                    errors.append(f"{path}: missing required property {key!r}")

            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                for key in value:
                    if key not in properties:
                        errors.append(f"{path}: unexpected property {key!r}")
            for key, subschema in properties.items():
                if key in value:
                    errors.extend(
                        self.validate(value[key], subschema, f"{path}.{key}", current_file)
                    )

        return errors


def candidate_tuple(candidate: Any) -> tuple[Any, ...] | None:
    if not isinstance(candidate, dict):
        return None
    keys = ("candidate_id", "source_revision", "artifact_hash", "artifact_uri")
    if any(key not in candidate for key in keys):
        return None
    return tuple(candidate[key] for key in keys)


def semantic_errors(contract_type: str, document: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    def equal(left: Any, right: Any, description: str) -> None:
        if left != right:
            errors.append(f"$: {description}")

    def require_antigravity(verifier: Any, path: str) -> None:
        if not isinstance(verifier, dict):
            return
        runtime = verifier.get("runtime", {})
        independence = verifier.get("independence", {})
        if runtime.get("harness") != "Antigravity":
            errors.append(f"{path}.runtime.harness: verifier must be Antigravity")
        if independence.get("independent_from_producer") is not True:
            errors.append(f"{path}.independence: verifier must be independent from producer")
        if independence.get("independent_from_candidate_author") is not True:
            errors.append(
                f"{path}.independence: verifier must be independent from candidate author"
            )

    if contract_type == "factory.v2.pcp_handoff":
        pcp = document.get("pcp", {})
        source = document.get("source", {})
        equal(
            pcp.get("immutable_revision"),
            source.get("revision"),
            "PCP immutable_revision must equal source revision",
        )
        approval = document.get("owner_approval", {})
        evidence = approval.get("evidence", {})
        if evidence and evidence.get("kind") != "owner-approval":
            errors.append("$.owner_approval.evidence.kind: expected owner-approval")

    elif contract_type == "factory.v2.verification":
        root_candidate = candidate_tuple(document.get("candidate"))
        code_review = document.get("code_review", {})
        qa_e2e = document.get("qa_e2e", {})
        equal(
            root_candidate,
            candidate_tuple(code_review.get("candidate")),
            "code review candidate tuple must equal root candidate tuple",
        )
        equal(
            root_candidate,
            candidate_tuple(qa_e2e.get("candidate")),
            "QA/E2E candidate tuple must equal root candidate tuple",
        )
        require_antigravity(code_review.get("verifier"), "$.code_review.verifier")
        require_antigravity(qa_e2e.get("verifier"), "$.qa_e2e.verifier")
        has_defects = bool(document.get("blocking_defects"))
        both_pass = code_review.get("verdict") == "PASS" and qa_e2e.get("verdict") == "PASS"
        if document.get("result") == "PASS" and (not both_pass or has_defects):
            errors.append("$: PASS requires both verifier passes and no blocking defects")
        if document.get("result") == "FAIL" and not has_defects:
            errors.append("$: FAIL requires at least one blocking defect")

    elif contract_type == "factory.v2.engineering_mission":
        attempts = document.get("grok_build", {}).get("attempts", [])
        completed = [attempt for attempt in attempts if attempt.get("status") == "COMPLETED"]
        if not completed:
            errors.append("$.grok_build.attempts: at least one completed Grok candidate is required")
        else:
            equal(
                candidate_tuple(document.get("current_candidate")),
                candidate_tuple(completed[-1].get("candidate")),
                "current_candidate must equal the latest completed Grok candidate",
            )

        defects = document.get("consolidated_defect_packet", {})
        status = defects.get("status")
        if status == "NONE" and defects.get("defects"):
            errors.append("$.consolidated_defect_packet: NONE cannot contain defects")
        if status == "OPEN" and not defects.get("defects"):
            errors.append("$.consolidated_defect_packet: OPEN requires defects")

        owner_history = document.get("owner_rc_verdict_history", [])
        rework_history = document.get("rework_history", [])
        if owner_history and owner_history[-1].get("decision") == "REJECT":
            if not rework_history:
                errors.append("$: an Owner rejection requires a rework history entry")
            else:
                latest_rework = rework_history[-1]
                if latest_rework.get("trigger") != "OWNER_REJECT":
                    errors.append("$: latest rework must record the Owner rejection trigger")
                if latest_rework.get("to_attempt") != document.get("attempt", {}).get("attempt_number"):
                    errors.append("$: rework to_attempt must equal current attempt number")

    elif contract_type == "factory.v2.verified_rc":
        root_candidate = candidate_tuple(document.get("candidate"))
        for path in ("code_review", "qa_e2e"):
            verification = document.get(path, {})
            equal(
                root_candidate,
                candidate_tuple(verification.get("candidate")),
                f"{path} candidate tuple must equal RC candidate tuple",
            )
            require_antigravity(verification.get("verifier"), f"$.{path}.verifier")
            if verification.get("verdict") != "PASS":
                errors.append(f"$.{path}.verdict: Verified RC requires PASS")
        rc = document.get("rc", {})
        candidate = document.get("candidate", {})
        equal(
            rc.get("immutable_revision"),
            candidate.get("source_revision"),
            "RC immutable_revision must equal candidate source_revision",
        )

    elif contract_type == "factory.v2.distribution_handoff":
        approved = document.get("approved_rc", {})
        artifact = document.get("deployment_artifact", {})
        for key in ("candidate_id", "source_revision", "artifact_hash"):
            equal(
                approved.get(key),
                artifact.get(key),
                f"deployment artifact {key} must equal approved RC {key}",
            )
        approval = document.get("owner_approval", {})
        equal(approved.get("rc_id"), approval.get("rc_id"), "Owner approval RC must equal approved RC")
        equal(
            approved.get("candidate_id"),
            approval.get("candidate_id"),
            "Owner approval candidate must equal approved RC",
        )
        recovery = document.get("rollback", {}).get("recovery_identity", {})
        equal(approved.get("rc_id"), recovery.get("rc_id"), "rollback RC must equal approved RC")
        equal(
            approved.get("artifact_hash"),
            recovery.get("artifact_hash"),
            "rollback artifact hash must equal approved RC",
        )
        evidence = approval.get("evidence", {})
        if evidence and evidence.get("kind") != "owner-approval":
            errors.append("$.owner_approval.evidence.kind: expected owner-approval")

    return errors


def parse_json(path: Path) -> tuple[Any | None, list[str]]:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle), []
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"{path}: {exc}"]


def run(root: Path) -> int:
    contracts_dir = root.resolve()
    manifest_path = contracts_dir / "fixtures" / "manifest.json"
    manifest, errors = parse_json(manifest_path)
    if errors:
        for error in errors:
            print(f"ERROR {error}")
        return 1

    schema_paths = sorted(contracts_dir.glob("*.schema.json"))
    for schema_path in schema_paths:
        _, schema_errors = parse_json(schema_path)
        for error in schema_errors:
            print(f"ERROR {error}")
            errors.extend(schema_errors)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("fixtures"), list):
        print("ERROR fixtures/manifest.json: fixtures must be an array")
        return 1

    checked = 0
    for entry in manifest["fixtures"]:
        checked += 1
        fixture_name = entry.get("fixture", "<missing fixture>")
        schema_name = entry.get("schema", "<missing schema>")
        expected = entry.get("expected")
        fixture_path = contracts_dir / "fixtures" / fixture_name
        schema_path = contracts_dir / schema_name
        document, fixture_errors = parse_json(fixture_path)
        if fixture_errors:
            actual_errors = fixture_errors
        elif not schema_path.exists():
            actual_errors = [f"schema does not exist: {schema_path}"]
        else:
            schema, schema_errors = parse_json(schema_path)
            if schema_errors:
                actual_errors = schema_errors
            else:
                validator = SchemaValidator(schema_path)
                actual_errors = validator.validate(document, schema)
                if not actual_errors and isinstance(document, dict):
                    actual_errors.extend(
                        semantic_errors(document.get("contract_type", ""), document)
                    )

        is_valid = not actual_errors
        expected_valid = expected == "valid"
        if is_valid == expected_valid:
            print(f"PASS {fixture_name} ({expected})")
        else:
            print(f"FAIL {fixture_name}: expected {expected}, got {'valid' if is_valid else 'invalid'}")
            for error in actual_errors[:12]:
                print(f"  - {error}")
            errors.append(f"fixture expectation failed: {fixture_name}")

    print(f"Checked {checked} fixtures against {len(schema_paths)} JSON schemas.")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contracts-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="directory containing the schemas and fixtures",
    )
    args = parser.parse_args()
    return run(args.contracts_dir)


if __name__ == "__main__":
    sys.exit(main())
