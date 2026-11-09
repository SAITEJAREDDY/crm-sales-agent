#!/usr/bin/env python3
"""Generate enablement_data.json from pipeline, profiles, and resource estimates."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from knowledge_base.schemas.enablement_schema import normalize_and_validate
from tools.enablement_rules import evaluate_enablement_triggers


BASE_DIR = Path(__file__).resolve().parent
PIPELINE_JSON = BASE_DIR / "knowledge_base" / "meddpicc_pipeline.json"
RESOURCE_DIR = BASE_DIR / "knowledge_base" / "resource_estimates"
OUT_PATH = BASE_DIR / "knowledge_base" / "enablement_data.json"
WARN_PATH = BASE_DIR / "knowledge_base" / "enablement_schema_warnings.json"

ROLE_PERSONAS = ["Lead Cloud Architect", "Data Engineer", "DevSecOps Lead", "Platform Engineer", "FinOps Lead"]
COURSES = [
    "Azure Data Platform Fundamentals",
    "Azure Cost Optimization",
    "Azure Security & Governance",
    "Azure AI and ML Operations",
    "Azure Integration Architecture",
    "Azure Analytics Engineering",
    "Azure Reliability and Observability",
]
CERTS = ["AZ-900", "DP-300", "AZ-305", "AZ-104", "DP-203"]


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def resource_for(company_slug: str, deal_slug: str) -> dict:
    matches = sorted(RESOURCE_DIR.glob(f"{company_slug}_{deal_slug}.json"))
    return load_json(matches[0], {}) if matches else {}


def learner_roster(seed: int, licensed: int, trained: int) -> list[dict]:
    rng = random.Random(seed)
    count = min(max(trained, 3), 12)
    roster = []
    for i in range(count):
        completed = rng.sample(COURSES, k=rng.randint(1, min(3, len(COURSES))))
        roster.append({
            "learner_id": f"L-{seed}-{i+1:03d}",
            "role_persona": rng.choice(ROLE_PERSONAS),
            "courses_completed": completed,
            "certifications": rng.sample(CERTS, k=rng.randint(0, 2)),
        })
    return roster


def build_record(deal: dict, idx: int) -> dict:
    company_slug = slugify(deal.get("company", ""))
    deal_slug = slugify(deal.get("deal_name", ""))
    amount = float(deal.get("amount") or 0)
    resource = resource_for(company_slug, deal_slug)
    annual_consumption = (
        resource.get("monthly_consumption", {}).get("total_annual_usd")
        or max(amount * 0.60, 0)
    )
    tier = "Enterprise" if amount > 500_000 else "Mid-Market" if amount > 100_000 else "SMB"
    licensed = 450 if tier == "Enterprise" else 140 if tier == "Mid-Market" else 45

    flags = deal.get("flags") or {}
    risk_base = 35
    risk_base += 18 if flags.get("stalled") else 0
    risk_base += 12 if flags.get("paper_process_unknown") else 0
    risk_base += 15 if "RISK" in str(deal.get("overall_score", "")).upper() else 0
    risk_base += 10 if annual_consumption > 50_000 else 0
    skills_gap = min(100, risk_base + (idx % 17))
    coverage_target = max(8, 62 - skills_gap // 2)
    trained = min(licensed, max(0, round(licensed * coverage_target / 100)))

    return {
        "company_slug": company_slug,
        "deal_slug": deal_slug,
        "company": deal.get("company", ""),
        "deal_name": deal.get("deal_name", ""),
        "deal_id": deal.get("deal_id", ""),
        "deal_amount": amount,
        "company_tier": tier,
        "licensed_users": licensed,
        "trained_users": trained,
        "enablement_coverage_pct": round(trained * 100 / licensed, 2) if licensed else 0,
        "skills_gap_risk_score": skills_gap,
        "azure_annual_consumption": float(annual_consumption or 0),
        "certifications_held": sorted({c for l in learner_roster(idx + 1, licensed, trained) for c in l["certifications"]}),
        "attendance_rate_pct": float(max(20, min(95, 88 - skills_gap + (idx % 23)))),
        "days_inactive": int(12 + skills_gap // 2 + (idx % 19)),
        "days_to_renewal": int(30 + (idx * 11) % 240),
        "partner_managed_status": bool(idx % 5 == 0),
        "learner_roster": learner_roster(idx + 1, licensed, trained),
    }


def main() -> None:
    deals = load_json(PIPELINE_JSON, [])
    output = []
    warning_log = []
    for idx, deal in enumerate(deals):
        normalized, warnings, errors = normalize_and_validate(build_record(deal, idx))
        normalized["trigger_flags"] = evaluate_enablement_triggers(normalized)
        if warnings or errors:
            warning_log.append({
                "company_slug": normalized.get("company_slug"),
                "deal_slug": normalized.get("deal_slug"),
                "warnings": warnings,
                "errors": errors,
            })
        if not errors:
            output.append(normalized)

    OUT_PATH.write_text(json.dumps(output, indent=2))
    WARN_PATH.write_text(json.dumps(warning_log, indent=2))
    print(f"Wrote {len(output)} enablement records to {OUT_PATH}")
    print(f"Wrote {len(warning_log)} schema warnings to {WARN_PATH}")


if __name__ == "__main__":
    main()
