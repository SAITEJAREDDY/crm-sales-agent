"""
Deal Desk — Push Notes to Twenty CRM
Reads generated markdown notes from knowledge_base/notes/ and creates
Note records in Twenty CRM, linked to their deals via NoteTargets.
Resume-safe: skips notes already pushed.
"""

import json
import os
from dotenv import load_dotenv
import re
import time

import requests

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWENTY_TOKEN = os.environ.get("TWENTY_API_TOKEN", "")

DATA_URL = "http://localhost:3000/graphql"
HEADERS  = {"Authorization": f"Bearer {TWENTY_TOKEN}", "Content-Type": "application/json"}

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
NOTES_DIR    = os.path.join(BASE_DIR, "knowledge_base", "notes")
PUSH_LOG     = os.path.join(NOTES_DIR, "_crm_push_log.json")

NOTE_TYPE_LABELS = {
    "01_discovery":           "Discovery Call",
    "02_technical_review":    "Technical Review",
    "03_stakeholder_meeting": "Stakeholder Meeting",
    "04_risk_review":         "Risk Review",
    "05_close_plan":          "Close Plan",
    "_summary":               "Executive Summary",
    "06_win_analysis":        "Win Analysis",
    "06_loss_analysis":       "Loss Analysis",
}

# ── HELPERS ───────────────────────────────────────────────────────────────────
def slugify(text):
    text = text.lower()
    text = re.sub(r"[—–\-]+", "_", text)
    text = re.sub(r"[^a-z0-9_]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def gql(query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(DATA_URL, headers=HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise ValueError(f"GraphQL error: {data['errors']}")
    return data["data"]


# ── FETCH ALL DEALS ───────────────────────────────────────────────────────────
DEAL_QUERY = """
query FetchDeals($after: String) {
  opportunities(first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        companyName
      }
    }
  }
}
"""


def fetch_all_deals():
    deals = []
    cursor = None
    while True:
        data = gql(DEAL_QUERY, {"after": cursor} if cursor else {})
        edges = data["opportunities"]["edges"]
        for e in edges:
            n = e["node"]
            deals.append({
                "id":           n["id"],
                "name":         n["name"],
                "company_name": n.get("companyName") or "Unknown Company",
            })
        page = data["opportunities"]["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]
    return deals


def build_slug_index(deals):
    """Map (company_slug, deal_slug) → deal_id."""
    index = {}
    for d in deals:
        cs = slugify(d["company_name"])
        ds = slugify(d["name"])
        key = (cs, ds)
        if key in index:
            # Duplicate slug — keep both under a list so we can warn
            existing = index[key]
            if isinstance(existing, list):
                existing.append(d["id"])
            else:
                index[key] = [existing, d["id"]]
        else:
            index[key] = d["id"]
    return index


# ── PUSH LOG ──────────────────────────────────────────────────────────────────
def load_push_log():
    if os.path.exists(PUSH_LOG):
        with open(PUSH_LOG) as f:
            return set(json.load(f))
    return set()


def save_push_log(pushed):
    with open(PUSH_LOG, "w") as f:
        json.dump(sorted(pushed), f, indent=2)


# ── CREATE NOTE + TARGET ──────────────────────────────────────────────────────
CREATE_NOTE = """
mutation CreateNote($title: String!, $markdown: String!) {
  createNote(data: {
    title: $title
    bodyV2: { markdown: $markdown, blocknote: "[]" }
  }) {
    id
  }
}
"""

CREATE_TARGET = """
mutation CreateNoteTarget($noteId: ID!, $oppId: ID!) {
  createNoteTarget(data: { noteId: $noteId, targetOpportunityId: $oppId }) {
    id
  }
}
"""


def push_note(note_path, deal_id, note_type, deal_name):
    with open(note_path) as f:
        markdown = f.read()

    title = f"{NOTE_TYPE_LABELS.get(note_type, note_type)} — {deal_name}"

    note_data = gql(CREATE_NOTE, {"title": title, "markdown": markdown})
    note_id = note_data["createNote"]["id"]

    gql(CREATE_TARGET, {"noteId": note_id, "oppId": deal_id})
    return note_id


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("\n=== Deal Desk — Push Notes to CRM ===\n")

    print("[1/4] Fetching all deals from Twenty CRM...")
    deals = fetch_all_deals()
    print(f"  Found {len(deals)} deals.")

    print("[2/4] Building slug index...")
    slug_index = build_slug_index(deals)
    deal_by_id = {d["id"]: d for d in deals}

    print("[3/4] Loading push log (resume-safe)...")
    pushed = load_push_log()
    print(f"  Already pushed: {len(pushed)} notes.")

    print("[4/4] Pushing notes...\n")

    stats = {"pushed": 0, "skipped": 0, "no_match": 0, "errors": 0}
    no_match_dirs = set()

    for company_dir in sorted(os.listdir(NOTES_DIR)):
        company_path = os.path.join(NOTES_DIR, company_dir)
        if not os.path.isdir(company_path) or company_dir.startswith("_"):
            continue

        for deal_dir in sorted(os.listdir(company_path)):
            deal_path = os.path.join(company_path, deal_dir)
            if not os.path.isdir(deal_path):
                continue

            key = (company_dir, deal_dir)
            deal_id_entry = slug_index.get(key)

            if deal_id_entry is None:
                no_match_dirs.add(f"{company_dir}/{deal_dir}")
                stats["no_match"] += 1
                continue

            # If multiple deals map to same slug, take the first
            deal_id = deal_id_entry if isinstance(deal_id_entry, str) else deal_id_entry[0]
            deal_name = deal_by_id.get(deal_id, {}).get("name", deal_dir)

            for fname in sorted(os.listdir(deal_path)):
                if not fname.endswith(".md"):
                    continue
                note_type = fname[:-3]
                note_path = os.path.join(deal_path, fname)
                log_key   = f"{company_dir}/{deal_dir}/{fname}"

                if log_key in pushed:
                    stats["skipped"] += 1
                    continue

                try:
                    note_id = push_note(note_path, deal_id, note_type, deal_name)
                    pushed.add(log_key)
                    stats["pushed"] += 1
                    print(f"  ✅ {log_key}")

                    # Save progress after every note
                    if stats["pushed"] % 10 == 0:
                        save_push_log(pushed)

                except Exception as e:
                    stats["errors"] += 1
                    print(f"  ❌ {log_key}: {e}")

                time.sleep(0.05)  # gentle rate limiting for local CRM

    save_push_log(pushed)

    print(f"\n{'─'*50}")
    print(f"  ✅ Pushed : {stats['pushed']}")
    print(f"  ⏭  Skipped: {stats['skipped']} (already in CRM)")
    print(f"  ❓ No match: {stats['no_match']} deal folders (slug not found)")
    print(f"  ❌ Errors : {stats['errors']}")

    if no_match_dirs:
        print(f"\n  Unmatched deal folders (first 10):")
        for d in sorted(no_match_dirs)[:10]:
            print(f"    {d}")

    print(f"\n  Done. {stats['pushed'] + stats['skipped']} notes now in Twenty CRM.\n")


if __name__ == "__main__":
    main()
