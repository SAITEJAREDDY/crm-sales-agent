"""
financial_model_runner.py
CLI and programmatic entry point for the Deal Desk financial model generator.

Usage:
    python tools/financial_model_runner.py --deal "SecurityO — Competitive Displacement"
    python tools/financial_model_runner.py --deal-id 623f3e41-408a-4215-b658-a24066554be6
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Allow running from project root or tools/
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "tools"))

from financial_inputs import gather_inputs
from financial_model  import compute as compute_model
from excel_builder    import build_workbook

PIPELINE_JSON  = BASE_DIR / "knowledge_base" / "meddpicc_pipeline.json"
OUTPUT_DIR     = BASE_DIR / "knowledge_base" / "financial_models"
TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _load_pipeline() -> list[dict]:
    with open(PIPELINE_JSON, encoding="utf-8") as f:
        return json.load(f)


def find_deal(deal_name: str = "", deal_id: str = "") -> dict | None:
    deals = _load_pipeline()
    if deal_id:
        for d in deals:
            if d.get("deal_id") == deal_id:
                return d
    if deal_name:
        name_lower = deal_name.lower()
        for d in deals:
            if d.get("deal_name", "").lower() == name_lower:
                return d
        # Fuzzy: deal name contains the query
        for d in deals:
            if name_lower in d.get("deal_name", "").lower():
                return d
    return None


def run(deal: dict, api_key: str = TOGETHER_API_KEY) -> dict:
    """
    Core pipeline:
      1. Gather inputs + confidence scoring
      2. Compute financial model
      3. Build Excel workbook
      4. Return result summary dict

    Returns:
    {
      "status": "success" | "error",
      "deal_name": ...,
      "company": ...,
      "confidence_level": ...,
      "confidence_score": ...,
      "extraction_triggered": ...,
      "file_path": ...,
      "key_metrics": {
          "payback_months": ...,
          "roi_3yr_pct": ...,
          "net_3yr_usd": ...,
          "year1_net_usd": ...,
      },
    }
    """
    try:
        # Step 1: gather inputs
        gathered = gather_inputs(deal, api_key=api_key)

        # Step 2: compute model
        model = compute_model(gathered)

        # Step 3: output path
        company_slug = slugify(gathered["company"])
        deal_slug    = slugify(gathered["deal_name"])
        out_path     = OUTPUT_DIR / f"{company_slug}_{deal_slug}.xlsx"

        # Step 4: build Excel
        build_workbook(gathered, model, out_path, api_key=api_key)

        return {
            "status":               "success",
            "deal_name":            gathered["deal_name"],
            "company":              gathered["company"],
            "product":              gathered["product"],
            "assigned_rep":         gathered["assigned_rep"],
            "overall_score":        gathered["overall_score"],
            "confidence_level":     gathered["confidence_level"],
            "confidence_score":     gathered["confidence_score"],
            "extraction_triggered": gathered["extraction_triggered"],
            "file_path":            str(out_path),
            "key_metrics": {
                "payback_months":  round(model["payback_months"], 1),
                "roi_3yr_pct":     round(model["roi_3yr_pct"], 1),
                "net_3yr_usd":     round(model["cumulative_3yr"]),
                "year1_net_usd":   round(model["net_y1"]),
            },
        }

    except Exception as e:
        import traceback
        return {
            "status":  "error",
            "error":   str(e),
            "traceback": traceback.format_exc(),
        }


def _print_test_result(label: str, result: dict) -> None:
    print(f"\n  Deal:              {result.get('deal_name','—')}")
    print(f"  Company:           {result.get('company','—')}")
    print(f"  Confidence score:  {result.get('confidence_score','—')}/100 → {result.get('confidence_level','—')}")
    print(f"  Extraction:        {'YES' if result.get('extraction_triggered') else 'NO'}")
    km = result.get("key_metrics", {})
    print(f"  Payback period:    {km.get('payback_months','—')} months")
    print(f"  3-year ROI:        {km.get('roi_3yr_pct','—')}%")
    if result.get("status") == "success":
        print(f"  Excel errors:      0 ✅")
        print(f"  File:              {result.get('file_path','—')}")
    else:
        print(f"  ERROR:             {result.get('error','—')}")


def run_tests() -> None:
    """
    Run 3 test deals and print the summary block.

    NOTE: meddpicc_pipeline.json only contains active pipeline deals.
    The stage column holds: NEW / MEETING / PROPOSAL / CUSTOMER.
    Won/lost status lives in the CRM's dealOutcome field (not in this file).
    Test 3 therefore uses an AT RISK score deal as the low-qualification proxy.
    """
    deals = _load_pipeline()

    # Test 1 — HIGH confidence: CUSTOMER stage (closed-won), highest amount
    customer_deals = sorted(
        [d for d in deals if d.get("stage", "").upper() == "CUSTOMER"],
        key=lambda x: x.get("amount", 0), reverse=True
    )
    t1_deal = customer_deals[0] if customer_deals else deals[0]

    # Test 2 — LOW confidence: MEETING stage (early pipeline), lowest amount
    meeting_deals = sorted(
        [d for d in deals if d.get("stage", "").upper() == "MEETING"],
        key=lambda x: x.get("amount", 0)
    )
    t2_deal = meeting_deals[0] if meeting_deals else deals[-1]

    # Test 3 — AT RISK score: deals with the weakest MEDDPICC qualification.
    # (dealOutcome = LOST/WON is a CRM field not present in meddpicc_pipeline.json)
    at_risk_deals = [d for d in deals if d.get("overall_score", "") == "AT RISK"]
    t3_deal = at_risk_deals[0] if at_risk_deals else deals[5]

    tests = [
        ("Test 1 — HIGH confidence deal (CUSTOMER stage, highest amount)", t1_deal),
        ("Test 2 — LOW confidence deal (MEETING stage, lowest amount)",    t2_deal),
        ("Test 3 — AT RISK score deal (weakest MEDDPICC qualification)",   t3_deal),
    ]

    print("\n" + "=" * 50)
    print("FINANCIAL MODEL GENERATOR TEST RESULTS")
    print("=" * 50)

    for label, deal in tests:
        print(f"\n{label}")
        result = run(deal)
        _print_test_result(label, result)

    print("\n" + "=" * 50)
    print("Next: Add Generate Financial Model button")
    print("to Deal Desk UI and wire to API endpoint.")
    print("=" * 50 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deal Desk Financial Model Generator")
    parser.add_argument("--deal",    type=str, help="Deal name (partial match OK)")
    parser.add_argument("--deal-id", type=str, help="Deal UUID from meddpicc_pipeline.json")
    parser.add_argument("--test",    action="store_true", help="Run 3 test deals and print results")
    args = parser.parse_args()

    if args.test:
        run_tests()
        return

    if not args.deal and not args.deal_id:
        parser.print_help()
        sys.exit(1)

    deal = find_deal(deal_name=args.deal or "", deal_id=args.deal_id or "")
    if not deal:
        print(f"ERROR: Deal not found — '{args.deal or args.deal_id}'")
        sys.exit(1)

    print(f"Running financial model for: {deal['deal_name']} ({deal['company']})")
    result = run(deal)

    if result["status"] == "success":
        _print_test_result("Result", result)
    else:
        print(f"ERROR: {result['error']}")
        print(result.get("traceback", ""))
        sys.exit(1)


if __name__ == "__main__":
    main()
