#!/usr/bin/env python3
"""Offline verification of the reviewed publication. Does not create records."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import sys


TOKEN = re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|(?:gsk_|lsv2_|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16})|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
FORBIDDEN_SUFFIXES = {".csv", ".xlsx", ".xls", ".db", ".sqlite", ".sqlite3", ".log", ".pdf", ".pptx", ".docx", ".bin", ".pyc"}
FORBIDDEN_JSON = {"canonical_deal_inputs.json", "historical_comparable_deals.json", "meddpicc_pipeline.json", "enablement_data.json", "customer_health_data.json", "gtm_feedback_history.json", "consumption_profiles.json", "consumption_architectures.json", "consumption_pricing.json", "consumption_forecasts.json", "consumption_business_cases.json", "active_commercial_negotiations.json", "azure_service_catalog.json", "azure_enterprise_service_catalog.json", "azure_eastus_retail_price_snapshot.json"}


def populated_record_literals(source):
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        try:
            value = ast.literal_eval(node)
        except (ValueError, TypeError):
            continue
        identifiers = ("deal_id", "account_id", "company_slug", "deal_slug", "customer_slug", "learner_id")
        if any(isinstance(value.get(key), str) and value[key] and not value[key].startswith("<") for key in identifiers):
            yield node.lineno


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="Also reject every unlisted local file, including ignored runtime files.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "PUBLICATION_MANIFEST.json").read_text())
    allowed = set(manifest["allowed_files"])
    entries = {entry["path"]: entry for entry in manifest["files"]}
    errors = []
    if allowed != set(entries) | {"PUBLICATION_MANIFEST.json"}:
        errors.append("Manifest file inventory is inconsistent.")
    schema_count = python_count = 0
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print("Install requirements-dev.txt to validate the publication schemas.", file=sys.stderr)
        return 2
    for name in sorted(allowed):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            errors.append(f"Missing or invalid manifest path: {name}")
            continue
        if path.suffix in FORBIDDEN_SUFFIXES or path.name in FORBIDDEN_JSON or path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            errors.append(f"Prohibited publication file: {name}")
        content = path.read_bytes()
        entry = entries.get(name)
        if entry and hashlib.sha256(content).hexdigest() != entry["sha256"]:
            errors.append(f"Changed reviewed file: {name}")
        source = content.decode("utf-8")
        if TOKEN.search(source):
            errors.append(f"Credential-shaped text found in: {name}")
        if re.search(r"/(?:Users|home)/[A-Za-z0-9_.-]+/", source):
            errors.append(f"Personal machine path found in: {name}")
        if path.suffix == ".py":
            try:
                compile(source, name, "exec")
                rows = list(populated_record_literals(source))
                if rows:
                    errors.append(f"Populated record literal in {name}, lines: {rows}")
                python_count += 1
            except SyntaxError:
                errors.append(f"Invalid Python syntax: {name}")
        if entry and entry["kind"] == "schema":
            schema = json.loads(source)
            Draft202012Validator.check_schema(schema)
            if not schema.get("description"):
                errors.append(f"Schema description missing: {name}")

            def check_fields(node):
                if isinstance(node, dict):
                    if "examples" in node or "example" in node:
                        errors.append(f"Schema contains example values: {name}")
                    for field_name, definition in node.get("properties", {}).items():
                        if not definition.get("description"):
                            errors.append(f"Field description missing: {name}:{field_name}")
                    required = node.get("required", [])
                    # A property named "required" is a field definition, not
                    # the JSON Schema required-keyword array.
                    missing = set(required) - set(node.get("properties", {})) if isinstance(required, list) else set()
                    if missing:
                        errors.append(f"Required fields lack definitions: {name}")
                    for value in node.values():
                        check_fields(value)
                elif isinstance(node, list):
                    for value in node:
                        check_fields(value)

            check_fields(schema)
            schema_count += 1
    for line in (root / ".env.example").read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if re.search(r"(?:API_KEY|TOKEN|PASSWORD|SECRET)", key) and value.strip():
            errors.append("The environment template contains a nonempty credential field.")
    rules = (root / ".gitignore").read_text().splitlines()
    if "*" not in rules or "!*/" not in rules or {line[2:] for line in rules if line.startswith("!/")} != allowed:
        errors.append("Git ignore rules do not match the explicit publication allowlist.")
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and ".git" not in path.relative_to(root).parts}
    extra = actual - allowed
    if args.strict and extra:
        errors.append(f"Unreviewed local files present: {len(extra)}")
    if errors:
        print("Publication verification failed:")
        for error in errors:
            print("- " + error)
        return 1
    print(f"Verified {len(allowed)} allowlisted files, {python_count} Python sources, and {schema_count} documented schemas.")
    print("No prohibited data files, populated record literals, or credential-shaped values in the publication inventory.")
    if extra:
        print(f"Ignored local files outside the publication inventory: {len(extra)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
