"""
eval_dataset.py
Creates or updates the LangSmith dataset 'deal-desk-golden-v1' from live Twenty CRM deals.
Selects 2 deals per stage (most recently created), uploads as examples with stage rubrics.

Run once to seed:
    python tools/eval_dataset.py

Re-running is safe — it does NOT deduplicate; run against an empty dataset or
pass --reset to delete and recreate it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import requests
from langsmith import Client

# ── CRM connection (same token as server.py) ─────────────────────────────────
TWENTY_URL   = "http://localhost:3000/graphql"
TWENTY_TOKEN = os.environ.get("TWENTY_API_TOKEN", "")
TWENTY_HEADERS = {
    "Authorization": f"Bearer {TWENTY_TOKEN}",
    "Content-Type": "application/json",
}

DATASET_NAME    = "deal-desk-golden-v1"
DEALS_PER_STAGE = 2
MEDDPICC_JSON   = BASE_DIR / "knowledge_base" / "meddpicc_pipeline.json"

STAGE_RUBRICS: dict[str, str] = {
    "NEW": (
        "Did the risk agent flag missing information given the early stage? "
        "Did the arbitrator avoid overconfident recommendations without sufficient qualification data?"
    ),
    "MEETING": (
        "Did agents identify early qualification signals from the discovery stage? "
        "Did devil's advocate question unverified assumptions before a proposal exists?"
    ),
    "PROPOSAL": (
        "Did agents reference specific proposal details (amount, structure, close date)? "
        "Is the arbitrator recommendation concrete and actionable for closing?"
    ),
    "CUSTOMER": (
        "Did agents correctly identify winning signals in an active customer relationship? "
        "Does the memo reflect positive trajectory and upsell/expansion opportunities?"
    ),
    "WON": (
        "Did the pipeline recognize closed-won signals and avoid treating the deal as still open? "
        "Did the arbitrator correctly recommend no further pursuit action?"
    ),
    "LOST": (
        "Did the risk agent identify specific factors that contributed to the loss? "
        "Did devil's advocate flag warning signs that were present in the deal data?"
    ),
}


def _gql(query: str) -> dict:
    resp = requests.post(
        TWENTY_URL, headers=TWENTY_HEADERS,
        json={"query": query}, timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise ValueError(f"GraphQL error: {data['errors']}")
    return data["data"]


def _load_meddpicc() -> dict[str, dict]:
    if not MEDDPICC_JSON.exists():
        return {}
    with open(MEDDPICC_JSON, encoding="utf-8") as f:
        records = json.load(f)
    return {r["deal_id"]: r for r in records if "deal_id" in r}


def fetch_deals_by_stage() -> dict[str, list[dict]]:
    query = """
    query {
      opportunities(first: 500) {
        edges {
          node {
            id name stage closeDate probability createdAt
            companyName assignedRep
            amount { amountMicros currencyCode }
          }
        }
      }
    }
    """
    data = _gql(query)
    by_stage: dict[str, list[dict]] = {}
    for e in data["opportunities"]["edges"]:
        n = e["node"]
        n["amount"] = ((n.get("amount") or {}).get("amountMicros") or 0) / 1_000_000
        stage = n.get("stage") or "UNKNOWN"
        by_stage.setdefault(stage, []).append(n)
    for stage in by_stage:
        by_stage[stage].sort(key=lambda d: d.get("createdAt") or "", reverse=True)
    return by_stage


def select_deals(by_stage: dict[str, list[dict]], meddpicc: dict) -> list[dict]:
    selected = []
    for stage in ["NEW", "MEETING", "PROPOSAL", "CUSTOMER", "WON", "LOST"]:
        for deal in by_stage.get(stage, [])[:DEALS_PER_STAGE]:
            deal_id = deal.get("id", "")
            if deal_id in meddpicc:
                m = meddpicc[deal_id]
                deal["overall_score"]     = m.get("overall_score", "")
                deal["overall_reason"]    = m.get("overall_reason", "")
                deal["meddpicc_elements"] = m.get("elements", {})
                deal["meddpicc_flags"]    = m.get("flags", {})
            deal["_stage_rubric"] = STAGE_RUBRICS.get(stage, "")
            selected.append(deal)
    return selected


def push_to_langsmith(deals: list[dict], reset: bool = False) -> None:
    client = Client()

    if reset:
        try:
            existing = client.read_dataset(dataset_name=DATASET_NAME)
            client.delete_dataset(dataset_id=existing.id)
            print(f"[dataset] Deleted existing '{DATASET_NAME}'")
        except Exception:
            pass

    try:
        dataset = client.read_dataset(dataset_name=DATASET_NAME)
        print(f"[dataset] Reusing '{DATASET_NAME}' (id={dataset.id})")
    except Exception:
        dataset = client.create_dataset(
            dataset_name=DATASET_NAME,
            description=(
                "Deal Desk Agent golden eval set. "
                "2 deals per CRM stage: NEW, MEETING, PROPOSAL, CUSTOMER, WON, LOST. "
                "Each example includes full deal object + MEDDPICC data where available."
            ),
        )
        print(f"[dataset] Created '{DATASET_NAME}' (id={dataset.id})")

    ok = fail = 0
    for deal in deals:
        rubric = deal.pop("_stage_rubric", "")
        try:
            client.create_example(
                inputs={"deal": deal},
                dataset_name=DATASET_NAME,
                metadata={
                    "stage":     deal.get("stage", ""),
                    "deal_name": deal.get("name", ""),
                    "deal_id":   deal.get("id", ""),
                    "amount":    deal.get("amount", 0),
                    "rubric":    rubric,
                },
            )
            print(f"  ✓ [{deal.get('stage','?'):8s}] {deal.get('name','?')}")
            ok += 1
        except Exception as exc:
            print(f"  ✗ [{deal.get('stage','?'):8s}] {deal.get('name','?')} — {exc}")
            fail += 1

    print(f"\n[dataset] {ok} uploaded, {fail} failed → dataset '{DATASET_NAME}'")


def build_and_push(reset: bool = False) -> None:
    print("Fetching deals from Twenty CRM…")
    by_stage = fetch_deals_by_stage()
    total = sum(len(v) for v in by_stage.values())
    print(f"  {total} deals across {sorted(by_stage)} stages")

    meddpicc = _load_meddpicc()
    print(f"  {len(meddpicc)} MEDDPICC records loaded")

    deals = select_deals(by_stage, meddpicc)
    print(f"  {len(deals)} deals selected\n")

    push_to_langsmith(deals, reset=reset)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build deal-desk-golden-v1 LangSmith dataset")
    parser.add_argument("--reset", action="store_true",
                        help="Delete and recreate the dataset before uploading")
    args = parser.parse_args()
    build_and_push(reset=args.reset)
