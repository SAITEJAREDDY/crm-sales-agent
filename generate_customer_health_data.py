#!/usr/bin/env python3
"""Generate native customer health records from Deal Desk data."""

from __future__ import annotations

import json
import re
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
PIPELINE_JSON = BASE_DIR / "knowledge_base" / "meddpicc_pipeline.json"
ENABLEMENT_JSON = BASE_DIR / "knowledge_base" / "enablement_data.json"
OUT_PATH = BASE_DIR / "knowledge_base" / "customer_health_data.json"


HEALTH_STAGES = [
    "Identify",
    "Qualify",
    "Business Case",
    "Solution Validation",
    "Implementation Readiness",
    "Enablement",
    "Renewal / Expansion",
]

FLAG_LABELS = {
    "missing_economic_buyer": "Missing economic buyer",
    "missing_champion": "No confirmed champion",
    "unquantified_pain": "Pain not quantified",
    "competitor_unknown": "Competitor unknown",
    "paper_process_unknown": "Procurement not discussed",
    "deal_overdue": "Close date passed",
    "stalled": "Deal stalled",
}

MONTH_LABELS = ["Mar 2026", "Apr 2026", "May 2026", "Jun 2026", "Jul 2026", "Aug 2026"]


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def score_status(status: str) -> int:
    return {"confirmed": 3, "partial": 2, "at_risk": 1, "unknown": 0}.get((status or "").lower(), 0)


def health_status(score: float) -> str:
    if score >= 60:
        return "green"
    if score >= 40:
        return "amber"
    return "red"


def risk_label(score: float) -> str:
    if score >= 70:
        return "critical"
    if score >= 50:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


def stage_from_deals(deals: list[dict[str, Any]], avg_enablement_coverage: float) -> tuple[int, str]:
    stages = {(d.get("stage") or "").upper() for d in deals}
    if "CUSTOMER" in stages and avg_enablement_coverage >= 50:
        idx = 7
    elif "CUSTOMER" in stages:
        idx = 6
    elif "PROPOSAL" in stages:
        idx = 5
    elif "MEETING" in stages:
        idx = 3
    else:
        idx = 2
    return idx, HEALTH_STAGES[idx - 1]


def readiness_artifacts(deals: list[dict[str, Any]], enablement_records: list[dict[str, Any]]) -> dict[str, Any]:
    elements = [d.get("elements") or {} for d in deals]

    def confirmed(element: str) -> bool:
        vals = [(e.get(element) or {}).get("status") for e in elements]
        return any(v == "confirmed" for v in vals)

    flags = [d.get("flags") or {} for d in deals]
    has_impl_plan = any((d.get("stage") or "").upper() in {"PROPOSAL", "CUSTOMER"} for d in deals)
    avg_coverage = mean([r.get("enablement_coverage_pct", 0) or 0 for r in enablement_records]) if enablement_records else 0
    min_renewal = min([r.get("days_to_renewal", 999) or 999 for r in enablement_records], default=999)

    return {
        "stakeholder_map": {
            "status": "current" if confirmed("champion") and confirmed("economic_buyer") else "gap",
            "source_basis": "inferred_from_meddpicc_champion_and_economic_buyer",
        },
        "business_case": {
            "status": "current" if confirmed("metrics") and confirmed("identify_pain") else "gap",
            "source_basis": "inferred_from_meddpicc_metrics_and_identified_pain",
        },
        "procurement_plan": {
            "status": "current" if not any(f.get("paper_process_unknown") for f in flags) else "gap",
            "source_basis": "inferred_from_meddpicc_paper_process_flags",
        },
        "implementation_plan": {
            "status": "current" if has_impl_plan else "early",
            "source_basis": "inferred_from_deal_stage",
        },
        "enablement_plan": {
            "status": "current" if avg_coverage >= 50 else "gap",
            "source_basis": "inferred_from_enablement_coverage",
        },
        "renewal_plan": {
            "status": "current" if min_renewal > 90 else "urgent",
            "source_basis": "inferred_from_enablement_days_to_renewal",
        },
    }


def build_flags(
    customer: str,
    deals: list[dict[str, Any]],
    enablement_records: list[dict[str, Any]],
    artifacts: dict[str, Any],
) -> list[dict[str, Any]]:
    out = []
    flag_counts = Counter()
    for d in deals:
        for key, val in (d.get("flags") or {}).items():
            if val:
                flag_counts[key] += 1

    for key, count in flag_counts.most_common():
        severity = "HIGH" if key in {"missing_economic_buyer", "missing_champion", "deal_overdue", "stalled"} else "MEDIUM"
        out.append({
            "code": key.upper(),
            "severity": severity,
            "reason": f"{FLAG_LABELS.get(key, key)} appears on {count} deal(s) for {customer}.",
            "owner": "Account Executive",
            "recommended_action": action_for_flag(key),
            "source_basis": "meddpicc_pipeline.flags",
        })

    if enablement_records:
        avg_coverage = mean([r.get("enablement_coverage_pct", 0) or 0 for r in enablement_records])
        avg_skill_risk = mean([r.get("skills_gap_risk_score", 0) or 0 for r in enablement_records])
        min_renewal = min([r.get("days_to_renewal", 999) or 999 for r in enablement_records])
        if avg_coverage < 35:
            out.append({
                "code": "LOW_ENABLEMENT_COVERAGE",
                "severity": "HIGH",
                "reason": f"Average trained-user coverage is {avg_coverage:.1f}%.",
                "owner": "CSM",
                "recommended_action": "Schedule a role-based skills assessment and attach a training plan to the account plan.",
                "source_basis": "enablement_data.enablement_coverage_pct",
            })
        if avg_skill_risk >= 65:
            out.append({
                "code": "HIGH_SKILLS_GAP_RISK",
                "severity": "HIGH",
                "reason": f"Average skills-gap risk is {avg_skill_risk:.0f}/100.",
                "owner": "Solutions Architect",
                "recommended_action": "Run an architecture and adoption readiness review before expanding scope.",
                "source_basis": "enablement_data.skills_gap_risk_score",
            })
        if min_renewal <= 60:
            out.append({
                "code": "RENEWAL_WINDOW_NEAR",
                "severity": "HIGH",
                "reason": f"Nearest renewal window is {min_renewal} days away.",
                "owner": "Account Executive",
                "recommended_action": "Create a renewal save plan with executive sponsor, adoption proof, and commercial next step.",
                "source_basis": "enablement_data.days_to_renewal",
            })

    for name, artifact in artifacts.items():
        if artifact["status"] in {"gap", "urgent"}:
            out.append({
                "code": f"{name.upper()}_{artifact['status'].upper()}",
                "severity": "MEDIUM" if artifact["status"] == "gap" else "HIGH",
                "reason": f"{name.replace('_', ' ').title()} is {artifact['status']}.",
                "owner": "Deal Desk",
                "recommended_action": f"Update the {name.replace('_', ' ')} before the next customer review.",
                "source_basis": artifact["source_basis"],
            })

    return out[:8]


def action_for_flag(key: str) -> str:
    return {
        "missing_economic_buyer": "Confirm the economic buyer and schedule value confirmation.",
        "missing_champion": "Identify a champion and validate their influence in the buying process.",
        "unquantified_pain": "Quantify pain in dollars, time, risk reduction, or renewal impact.",
        "competitor_unknown": "Document competitive alternatives and run a displacement plan.",
        "paper_process_unknown": "Confirm procurement steps, legal path, and signature owner.",
        "deal_overdue": "Reset close plan with dates, owner, and leadership escalation criteria.",
        "stalled": "Hold a mutual action plan review and remove the next blocker.",
    }.get(key, "Assign owner and resolve this account-health gap.")


def build_health_record(customer: str, deals: list[dict[str, Any]], enablement_by_deal: dict[str, dict[str, Any]]) -> dict[str, Any]:
    enablement_records = [enablement_by_deal[d.get("deal_id")] for d in deals if d.get("deal_id") in enablement_by_deal]
    total_value = sum(d.get("amount", 0) or 0 for d in deals)
    active_deals = [d for d in deals if (d.get("stage") or "").upper() in {"NEW", "MEETING", "PROPOSAL"}]
    won_deals = [d for d in deals if (d.get("stage") or "").upper() == "CUSTOMER"]
    at_risk_deals = [d for d in deals if "RISK" in (d.get("overall_score") or "").upper()]

    meddpicc_scores = []
    for d in deals:
        elems = d.get("elements") or {}
        if elems:
            meddpicc_scores.append(mean([score_status((elems.get(e) or {}).get("status")) for e in elems]) / 3 * 100)
    meddpicc_health = mean(meddpicc_scores) if meddpicc_scores else 50

    avg_enablement_coverage = mean([r.get("enablement_coverage_pct", 0) or 0 for r in enablement_records]) if enablement_records else 45
    avg_skill_gap = mean([r.get("skills_gap_risk_score", 50) or 50 for r in enablement_records]) if enablement_records else 50
    min_renewal = min([r.get("days_to_renewal", 180) or 180 for r in enablement_records], default=180)
    annual_consumption = sum(r.get("azure_annual_consumption", 0) or 0 for r in enablement_records)

    stage_num, stage_name = stage_from_deals(deals, avg_enablement_coverage)
    artifacts = readiness_artifacts(deals, enablement_records)
    flags = build_flags(customer, deals, enablement_records, artifacts)

    health_score = 100
    health_score -= len(at_risk_deals) * 9
    health_score -= sum(1 for d in deals if "NEEDS" in (d.get("overall_score") or "").upper()) * 4
    health_score -= max(0, 55 - avg_enablement_coverage) * 0.35
    health_score -= avg_skill_gap * 0.22
    health_score -= 12 if min_renewal <= 60 else 0
    health_score -= sum(5 for f in flags if f["severity"] == "HIGH")
    health_score = round(max(0, min(100, health_score)), 1)
    churn_score = round(max(0, min(100, 100 - health_score + avg_skill_gap * 0.15)), 1)

    rep_counts = Counter(d.get("assigned_rep", "Unknown") for d in deals)
    product_counts = Counter(extract_product(d.get("deal_name", "")) for d in deals)
    top_deal = max(deals, key=lambda d: d.get("amount", 0) or 0)

    record = {
        "customer_slug": slugify(customer),
        "account_info": {
            "account_name": customer,
            "segment": segment(total_value),
            "primary_rep": rep_counts.most_common(1)[0][0],
            "primary_product": product_counts.most_common(1)[0][0],
        },
        "health": {
            "score": health_score,
            "status": health_status(health_score),
            "churn_risk_score": churn_score,
            "churn_risk": risk_label(churn_score),
            "source_basis": "inferred_from_deal_meddpicc_enablement_and_stage_data",
        },
        "program": {
            "current_stage": stage_num,
            "stage_name": stage_name,
            "stage_owner": owner_for_stage(stage_num),
            "stage_history": stage_history(stage_num),
        },
        "portfolio": {
            "deal_count": len(deals),
            "active_deal_count": len(active_deals),
            "won_deal_count": len(won_deals),
            "at_risk_deal_count": len(at_risk_deals),
            "total_pipeline_value": total_value,
            "weighted_pipeline_value": round(sum((d.get("amount", 0) or 0) * (d.get("probability", 0) or 0) / 100 for d in deals)),
            "top_deal": {
                "deal_id": top_deal.get("deal_id"),
                "deal_name": top_deal.get("deal_name"),
                "amount": top_deal.get("amount", 0) or 0,
                "stage": top_deal.get("stage"),
                "overall_score": top_deal.get("overall_score"),
            },
        },
        "readiness": {
            "meddpicc_health_pct": round(meddpicc_health, 1),
            "artifacts": artifacts,
        },
        "enablement": {
            "coverage_pct": round(avg_enablement_coverage, 1),
            "skills_gap_risk_score": round(avg_skill_gap, 1),
            "annual_consumption": annual_consumption,
            "days_to_renewal": min_renewal,
            "source_records": len(enablement_records),
        },
        "revenue": {
            "estimated_arr": round(total_value * 0.82),
            "arr_at_risk": round(total_value * churn_score / 100 * 0.82),
            "expansion_signal_value": round(sum(d.get("amount", 0) or 0 for d in active_deals)),
            "renewal_probability": round(max(0.35, min(0.98, 1 - churn_score / 160)), 3),
            "source_basis": "inferred_from_pipeline_value_and_health_score",
        },
        "flags": flags,
        "recommended_actions": [f["recommended_action"] for f in flags[:5]],
        "deal_ids": [d.get("deal_id") for d in deals if d.get("deal_id")],
    }
    record["monthly_health_trend"] = monthly_health_trend(record)
    record["trend_summary"] = trend_summary(record["monthly_health_trend"])
    return record


def monthly_health_trend(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Create stable synthetic monthly health snapshots for account trend views."""
    rng = random.Random(record["customer_slug"])
    current_health = float(record["health"]["score"])
    current_churn = float(record["health"]["churn_risk_score"])
    current_coverage = float(record["enablement"]["coverage_pct"])
    current_skill_gap = float(record["enablement"]["skills_gap_risk_score"])
    current_arr_risk = float(record["revenue"]["arr_at_risk"])
    current_actions = len(record.get("flags", []))
    current_high_flags = sum(1 for f in record.get("flags", []) if f.get("severity") == "HIGH")

    if current_health < 35:
        start_delta = rng.uniform(8, 18)
        drift = -rng.uniform(1.4, 4.2)
    elif current_health < 60:
        start_delta = rng.uniform(-6, 8)
        drift = rng.uniform(-1.8, 2.0)
    else:
        start_delta = -rng.uniform(2, 10)
        drift = rng.uniform(0.4, 2.2)

    snapshots = []
    for idx, label in enumerate(MONTH_LABELS):
        progress = idx / (len(MONTH_LABELS) - 1)
        noise = rng.uniform(-2.2, 2.2)
        health = current_health + (1 - progress) * start_delta + (progress - 1) * drift + noise
        if idx == len(MONTH_LABELS) - 1:
            health = current_health
        health = round(max(0, min(100, health)), 1)

        churn = round(max(0, min(100, current_churn + (current_health - health) * 0.85 + rng.uniform(-2, 2))), 1)
        coverage = round(max(0, min(100, current_coverage - (1 - progress) * rng.uniform(2, 12) + progress * rng.uniform(0, 4))), 1)
        skill_gap = round(max(0, min(100, current_skill_gap + (current_coverage - coverage) * 0.8 + rng.uniform(-2, 2))), 1)
        arr_risk = round(max(0, current_arr_risk * (churn / max(current_churn, 1)) * rng.uniform(0.96, 1.04)))
        high_flags = max(0, round(current_high_flags + (churn - current_churn) / 18 + rng.choice([-1, 0, 0, 1])))
        open_actions = max(0, round(current_actions + (churn - current_churn) / 14 + rng.choice([-1, 0, 1])))
        adoption_impact = round(max(-10, min(45, (coverage - 25) * 0.42 + (100 - skill_gap) * 0.08)), 1)

        snapshots.append({
            "month_label": label,
            "month_index": idx + 1,
            "health_score": health,
            "health_status": health_status(health),
            "churn_risk_score": churn,
            "churn_risk": risk_label(churn),
            "enablement_coverage_pct": coverage,
            "skills_gap_risk_score": skill_gap,
            "arr_at_risk": arr_risk,
            "open_action_count": open_actions,
            "high_severity_flag_count": high_flags,
            "adoption_impact_pct": adoption_impact,
        })
    return snapshots


def trend_summary(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    if len(snapshots) < 2:
        return {"direction": "flat", "health_delta": 0, "arr_at_risk_delta": 0}
    first = snapshots[0]
    last = snapshots[-1]
    delta = round(last["health_score"] - first["health_score"], 1)
    arr_delta = round(last["arr_at_risk"] - first["arr_at_risk"])
    if delta >= 5:
        direction = "improving"
    elif delta <= -5:
        direction = "declining"
    else:
        direction = "flat"
    return {
        "direction": direction,
        "health_delta": delta,
        "arr_at_risk_delta": arr_delta,
        "coverage_delta": round(last["enablement_coverage_pct"] - first["enablement_coverage_pct"], 1),
        "latest_month": last["month_label"],
    }


def extract_product(deal_name: str) -> str:
    for sep in [" — ", " – ", " - "]:
        if sep in deal_name:
            return deal_name.split(sep)[0].strip()
    return (deal_name or "Unknown").split()[0]


def segment(total_value: float) -> str:
    if total_value > 1_000_000:
        return "Enterprise"
    if total_value > 300_000:
        return "Mid-Market"
    return "SMB"


def owner_for_stage(stage_num: int) -> str:
    return {
        1: "Account Executive",
        2: "Account Executive",
        3: "Deal Desk",
        4: "Solutions Architect",
        5: "Delivery Lead",
        6: "Customer Success",
        7: "Account Executive",
    }[stage_num]


def stage_history(current_stage: int) -> list[dict[str, Any]]:
    history = []
    for idx, name in enumerate(HEALTH_STAGES, start=1):
        if idx < current_stage:
            status = "Complete"
        elif idx == current_stage:
            status = "In Progress"
        else:
            status = "Pending"
        history.append({"stage": idx, "stage_name": name, "owner": owner_for_stage(idx), "status": status})
    return history


def main() -> None:
    deals = load_json(PIPELINE_JSON, [])
    enablement = load_json(ENABLEMENT_JSON, [])
    enablement_by_deal = {r.get("deal_id"): r for r in enablement if r.get("deal_id")}

    by_customer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for deal in deals:
        customer = deal.get("company") or deal.get("companyName") or "Unknown"
        by_customer[customer].append(deal)

    records = [build_health_record(customer, customer_deals, enablement_by_deal)
               for customer, customer_deals in sorted(by_customer.items())]

    status_counts = Counter(r["health"]["status"] for r in records)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_version": "customer_health_v1",
        "source_files": [
            str(PIPELINE_JSON.relative_to(BASE_DIR)),
            str(ENABLEMENT_JSON.relative_to(BASE_DIR)),
        ],
        "total_customers": len(records),
        "status_counts": dict(status_counts),
        "customers": records,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(records)} customer health records to {OUT_PATH}")


if __name__ == "__main__":
    main()
