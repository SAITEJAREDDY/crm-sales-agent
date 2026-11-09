"""
Deal Desk — Create assignedRep field & bulk-update all 230 opportunities.

Endpoints:
  Metadata API  →  POST http://localhost:3000/metadata  (field creation, schema queries)
  Data API      →  POST http://localhost:3000/graphql   (CRUD on records)
"""

import json
import os
import sys
import time
from collections import defaultdict

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWENTY_TOKEN = os.environ.get("TWENTY_API_TOKEN", "")

METADATA_URL = "http://localhost:3000/metadata"
DATA_URL     = "http://localhost:3000/graphql"

HEADERS = {
    "Authorization": f"Bearer {TWENTY_TOKEN}",
    "Content-Type": "application/json",
}

BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
ASSIGNMENTS_FILE = os.path.join(BASE_DIR, "knowledge_base", "personas", "deal_assignments.json")
RESULTS_FILE     = os.path.join(BASE_DIR, "knowledge_base", "personas", "field_update_results.json")

OPPORTUNITY_OBJECT_ID = "509ce935-c81e-43db-be0e-6589eee80e4e"


# ── HELPERS ───────────────────────────────────────────────────────────────────
def gql(url, query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    try:
        resp = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        print("\n❌ Could not connect to Twenty CRM at http://localhost:3000")
        print("   Make sure Twenty is running: docker compose up -d")
        print("   And your TWENTY_API_TOKEN is correct in .env")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            print("\n❌ 401 Unauthorized — check your TWENTY_API_TOKEN in .env")
            sys.exit(1)
        raise


# ── STEP 1: LOAD ASSIGNMENTS ──────────────────────────────────────────────────
def load_assignments():
    if not os.path.exists(ASSIGNMENTS_FILE):
        print("❌ deal_assignments.json not found.")
        print("   Run generate_personas.py first.")
        sys.exit(1)
    with open(ASSIGNMENTS_FILE) as f:
        data = json.load(f)
    print(f"  Loaded {len(data)} deal assignments from deal_assignments.json")
    return data


# ── STEP 2: GET OBJECT METADATA & EXISTING FIELDS ────────────────────────────
FIELDS_QUERY = """
query {
  fields {
    edges {
      node {
        id
        name
        label
        type
        isCustom
        object {
          id
          nameSingular
        }
      }
    }
  }
}
"""


def get_existing_fields():
    data = gql(METADATA_URL, FIELDS_QUERY)
    existing = []
    for edge in data.get("data", {}).get("fields", {}).get("edges", []):
        node = edge["node"]
        obj  = node.get("object") or {}
        if obj.get("nameSingular") == "opportunity":
            existing.append(node["name"])
    return existing


# ── STEP 3: CREATE assignedRep FIELD ─────────────────────────────────────────
CREATE_FIELD_MUTATION = """
mutation CreateCustomField($input: CreateOneFieldMetadataInput!) {
  createOneField(input: $input) {
    id
    name
    label
    type
    isCustom
  }
}
"""


def create_assigned_rep_field(existing_fields):
    if "assignedRep" in existing_fields:
        print("  ✅ assignedRep field already exists — skipping creation")
        return

    variables = {
        "input": {
            "field": {
                "objectMetadataId": OPPORTUNITY_OBJECT_ID,
                "type": "TEXT",
                "name": "assignedRep",
                "label": "Assigned Rep",
                "description": "Sales persona assigned to this deal by Deal Desk Agent",
                "isNullable": True,
            }
        }
    }

    result = gql(METADATA_URL, CREATE_FIELD_MUTATION, variables)

    if "errors" in result:
        err_msg = result["errors"][0]["message"]
        # Extension errors may also carry "already used" / "already exists"
        ext_errors = result["errors"][0].get("extensions", {}).get("errors", {})
        field_errors = ext_errors.get("fieldMetadata", [])
        already_exists = (
            "already exists" in err_msg.lower()
            or "duplicate" in err_msg.lower()
            or "not available" in err_msg.lower()
            or any(
                e2.get("code") in ("NOT_AVAILABLE", "DUPLICATE", "ALREADY_EXISTS")
                for fe in field_errors
                for e2 in fe.get("errors", [])
            )
        )
        if already_exists:
            print("  ✅ assignedRep field already exists — skipping creation")
            time.sleep(2)  # still wait for cache
            return
        print(f"\n❌ Failed to create assignedRep field: {err_msg}")
        sys.exit(1)

    print("  ✅ assignedRep field created — waiting for metadata cache to propagate...")
    time.sleep(3)


# ── STEP 4: TEST SINGLE UPDATE ────────────────────────────────────────────────
UPDATE_MUTATION = """
mutation UpdateOpportunity($id: UUID!, $assignedRep: String!) {
  updateOpportunity(
    id: $id
    data: { assignedRep: $assignedRep }
  ) {
    id
    name
    assignedRep
  }
}
"""


def test_single_update(assignment):
    result = gql(DATA_URL, UPDATE_MUTATION, {
        "id":          assignment["deal_id"],
        "assignedRep": assignment["assigned_persona_name"],
    })

    if "errors" in result:
        err = result["errors"][0]["message"]
        # Retry once after short wait (cache propagation)
        if "field" in err.lower() or "assignedRep" in err:
            print("  ⏳ Cache may still be propagating — retrying in 3s...")
            time.sleep(3)
            result = gql(DATA_URL, UPDATE_MUTATION, {
                "id":          assignment["deal_id"],
                "assignedRep": assignment["assigned_persona_name"],
            })

        if "errors" in result:
            print(f"\n❌ Test update failed. Do not proceed with bulk update.")
            print(f"   Error: {result['errors'][0]['message']}")
            sys.exit(1)

    opp = result.get("data", {}).get("updateOpportunity", {})
    if opp.get("assignedRep") == assignment["assigned_persona_name"]:
        print(f"  ✅ Test update succeeded → {opp['name']} = \"{opp['assignedRep']}\"")
    else:
        print(f"  ⚠️  Test returned unexpected value: {opp}")


# ── STEP 5: BULK UPDATE ───────────────────────────────────────────────────────
def bulk_update(assignments):
    results = {"success": [], "failed": []}
    total   = len(assignments)

    for i, assignment in enumerate(assignments, 1):
        try:
            result = gql(DATA_URL, UPDATE_MUTATION, {
                "id":          assignment["deal_id"],
                "assignedRep": assignment["assigned_persona_name"],
            })

            if "errors" in result:
                err = result["errors"][0]["message"]
                if "rate limit" in err.lower():
                    time.sleep(1)
                    result = gql(DATA_URL, UPDATE_MUTATION, {
                        "id":          assignment["deal_id"],
                        "assignedRep": assignment["assigned_persona_name"],
                    })

                if "errors" in result:
                    results["failed"].append({
                        "deal_id":   assignment["deal_id"],
                        "deal_name": assignment["deal_name"],
                        "error":     result["errors"][0]["message"],
                    })
                    print(f"  ❌ [{i}/{total}] {assignment['deal_name']} — FAILED: {result['errors'][0]['message']}")
                    continue

            results["success"].append(assignment["deal_id"])
            print(f"  ✅ [{i}/{total}] {assignment['deal_name']:<40} → {assignment['assigned_persona_name']}")

        except Exception as e:
            results["failed"].append({
                "deal_id":   assignment["deal_id"],
                "deal_name": assignment["deal_name"],
                "error":     str(e),
            })
            print(f"  ❌ [{i}/{total}] {assignment['deal_name']} — EXCEPTION: {e}")

        time.sleep(0.15)

    return results


# ── STEP 6: VERIFY ────────────────────────────────────────────────────────────
VERIFY_QUERY = """
query VerifyAssignedReps {
  opportunities(first: 10, orderBy: { createdAt: DescNullsLast }) {
    edges {
      node {
        id
        name
        assignedRep
      }
    }
  }
}
"""


def verify_updates():
    result = gql(DATA_URL, VERIFY_QUERY)
    edges  = result.get("data", {}).get("opportunities", {}).get("edges", [])
    populated = 0
    print("\n  Spot-check (10 most recent deals):")
    for edge in edges:
        node = edge["node"]
        rep  = node.get("assignedRep") or "(empty)"
        if node.get("assignedRep"):
            populated += 1
        print(f"    {node['name']:<40} → {rep}")
    print(f"\n  {populated}/{len(edges)} spot-checked deals have assignedRep populated.")
    return populated


# ── STEP 7: SAVE RESULTS & SUMMARY ───────────────────────────────────────────
def save_and_print_summary(assignments, results):
    os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
    output = {
        "total_deals":         len(assignments),
        "successful_updates":  len(results["success"]),
        "failed_updates":      len(results["failed"]),
        "success_ids":         results["success"],
        "failures":            results["failed"],
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(output, f, indent=2)

    per_persona = defaultdict(int)
    for a in assignments:
        if a["deal_id"] in results["success"]:
            per_persona[a["assigned_persona_name"]] += 1

    print("\n==========================================")
    print("TWENTY CRM FIELD UPDATE COMPLETE")
    print("==========================================\n")
    print("Custom field:   assignedRep (TEXT) on Opportunities")
    print("Status:         Created & populated\n")
    print("Results:")
    print(f"  ✅ Successful updates:  {len(results['success'])} / {len(assignments)}")
    print(f"  ❌ Failed updates:      {len(results['failed'])} / {len(assignments)}\n")
    print("Assignments by persona:")
    for name, count in sorted(per_persona.items(), key=lambda x: -x[1]):
        print(f"  {name:<28} →  {count:>3} deals updated")
    print("\nTo view in Twenty CRM:")
    print("  1. Open http://localhost:3000")
    print("  2. Go to Opportunities")
    print("  3. Click the + button on table header")
    print("  4. Enable \"Assigned Rep\" column")
    print("\n==========================================")
    print("Next step: Run note_generator.py to create")
    print("deal history notes for all assigned deals.")
    print("==========================================\n")

    if results["failed"]:
        print(f"⚠️  {len(results['failed'])} failures saved to: knowledge_base/personas/field_update_results.json")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("\n=== Deal Desk — CRM Field Updater ===\n")

    print("[1/7] Loading deal assignments...")
    assignments = load_assignments()

    print("[2/7] Checking opportunity metadata...")
    existing_fields = get_existing_fields()
    print(f"  Found {len(existing_fields)} existing custom field(s): {existing_fields or 'none'}")

    print("[3/7] Creating assignedRep field...")
    create_assigned_rep_field(existing_fields)

    print("[4/7] Testing on first deal...")
    test_single_update(assignments[0])

    print(f"[5/7] Bulk updating all {len(assignments)} deals...")
    results = bulk_update(assignments)

    print("[6/7] Verifying updates in Twenty CRM...")
    verify_updates()

    print("[7/7] Saving results & printing summary...")
    save_and_print_summary(assignments, results)


if __name__ == "__main__":
    main()
