#!/usr/bin/env python3
"""List or validate operator-supplied inputs without generating records."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


INPUTS = [
    ("knowledge_base/meddpicc_pipeline.json", "schemas/meddpicc_record.schema.json", True, "Pipeline and Deal Desk"),
    ("knowledge_base/commercial_strategy/canonical_deal_inputs.json", "knowledge_base/commercial_strategy/deal_input_schema.json", True, "Commercial and Consumption"),
    ("knowledge_base/enablement_data.json", "schemas/enablement.schema.json", False, "Enablement"),
    ("knowledge_base/customer_health_data.json", "schemas/customer_health.schema.json", False, "Account health"),
    ("knowledge_base/gtm_feedback_history.json", "schemas/gtm_feedback_history.schema.json", False, "GTM Intelligence"),
    ("knowledge_base/commercial_strategy/consumption_profiles.json", "schemas/consumption_profile.schema.json", True, "Consumption architecture"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Print required paths and contracts only.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if args.list:
        for name, schema, collection, workflow in INPUTS:
            print(f"{workflow}: {name} -> {'collection of ' if collection else ''}{schema}")
        print("Additional catalog, notes, and historical-input requirements are documented in docs/RUNTIME_INPUTS.md.")
        return 0
    from jsonschema import Draft202012Validator

    missing = invalid = 0
    for name, contract, collection, workflow in INPUTS:
        path = root / name
        if not path.is_file():
            print(f"MISSING {name} ({workflow})")
            missing += 1
            continue
        try:
            data = json.loads(path.read_text())
            schema = json.loads((root / contract).read_text())
            validator = Draft202012Validator(schema)
            if collection:
                if not isinstance(data, list):
                    raise ValueError("Expected a collection")
                error_count = sum(1 for record in data for _ in validator.iter_errors(record))
            else:
                error_count = sum(1 for _ in validator.iter_errors(data))
            if error_count:
                print(f"INVALID {name}: {error_count} schema issue(s); record values are not printed.")
                invalid += 1
            else:
                print(f"VALID {name}")
        except (ValueError, OSError):
            print(f"INVALID {name}: cannot read the expected JSON shape.")
            invalid += 1
    print(f"Missing inputs: {missing}; invalid inputs: {invalid}. No records were generated or sent to an external service.")
    return 1 if missing or invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
