"""Generate synthetic Jan-Jul GTM enablement feedback-loop history.

The output extends existing CRM AI / Deal Desk entities instead of inventing a
parallel demo model. It uses current customer-health and enablement records as
July anchors, then backcasts coherent monthly account behavior and dated AI
recommendation events from January through July.
"""

from __future__ import annotations

import json
import random
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
KB_DIR = BASE_DIR / "knowledge_base"
CUSTOMER_HEALTH_PATH = KB_DIR / "customer_health_data.json"
ENABLEMENT_PATH = KB_DIR / "enablement_data.json"
OUTPUT_PATH = KB_DIR / "gtm_feedback_history.json"

MONTHS = [
    ("2026-01", "January"),
    ("2026-02", "February"),
    ("2026-03", "March"),
    ("2026-04", "April"),
    ("2026-05", "May"),
    ("2026-06", "June"),
    ("2026-07", "July"),
]

JOURNEYS = [
    "high_consumption_high_training",
    "high_consumption_low_training_positive",
    "high_consumption_low_training_no_response",
    "low_consumption_high_training_blocked",
    "low_consumption_low_training_recovery",
    "low_consumption_low_training_continued_risk",
    "expansion_ready",
    "renewal_risk_stabilized",
    "renewal_risk_deteriorated",
    "mixed_poorly_targeted_recommendation",
    "organic_improvement_no_ai",
]

STAGE_SEQUENCE = ["NEW", "MEETING", "PROPOSAL", "NEGOTIATION", "CUSTOMER"]


def _load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    with path.open() as f:
        return json.load(f)


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _status(score: float) -> str:
    if score >= 72:
        return "green"
    if score >= 50:
        return "amber"
    return "red"


def _churn(score: float) -> str:
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def _stage_at(current_stage: str, month_idx: int, journey: str) -> str:
    current = (current_stage or "NEW").upper()
    if current not in STAGE_SEQUENCE:
        current = "CUSTOMER" if current in {"WON", "CLOSED_WON"} else "PROPOSAL"
    cur_i = STAGE_SEQUENCE.index(current)
    if "deteriorated" in journey and month_idx >= 5:
        return "LOST" if month_idx == 6 else STAGE_SEQUENCE[max(0, cur_i - 1)]
    if "stalled" in journey or "no_response" in journey:
        return STAGE_SEQUENCE[max(0, min(cur_i, 2))]
    back = max(0, cur_i - (6 - month_idx) // 2)
    return STAGE_SEQUENCE[min(cur_i, back)]


def _segment(consumption: float, coverage: float) -> str:
    consumption_band = "High Consumption" if consumption >= 400_000 else "Low Consumption"
    training_band = "High Training" if coverage >= 55 else "Low Training"
    return f"{consumption_band} / {training_band}"


def _target_july_segment(idx: int) -> str:
    """Skew the demo portfolio toward the two most actionable GTM segments."""
    slot = idx % 10
    if slot == 0:
        return "High Consumption / High Training"
    if slot == 1:
        return "Low Consumption / Low Training"
    if slot in {2, 3, 4, 5}:
        return "Low Consumption / High Training"
    return "High Consumption / Low Training"


def _journey_for(idx: int, target_segment: str) -> str:
    if target_segment == "High Consumption / High Training":
        preferred = ["high_consumption_high_training", "expansion_ready", "organic_improvement_no_ai"]
    elif target_segment == "High Consumption / Low Training":
        preferred = ["high_consumption_low_training_positive", "high_consumption_low_training_no_response", "renewal_risk_stabilized"]
    elif target_segment == "Low Consumption / High Training":
        preferred = ["low_consumption_high_training_blocked", "mixed_poorly_targeted_recommendation"]
    else:
        preferred = ["low_consumption_low_training_recovery", "low_consumption_low_training_continued_risk", "renewal_risk_deteriorated"]
    return preferred[idx % len(preferred)]


def _profile(journey: str) -> dict:
    profiles = {
        "high_consumption_high_training": {
            "trained": [0.84, 0.86, 0.88, 0.9, 0.92, 0.95, 1.0],
            "active": [0.82, 0.84, 0.86, 0.87, 0.9, 0.93, 1.0],
            "consumption": [0.88, 0.91, 0.93, 0.94, 0.98, 1.0, 1.03],
            "health": [-3, -2, -1, 0, 1, 2, 0],
        },
        "high_consumption_low_training_positive": {
            "trained": [0.32, 0.34, 0.42, 0.58, 0.72, 0.86, 1.0],
            "active": [0.48, 0.46, 0.5, 0.56, 0.68, 0.82, 1.0],
            "consumption": [0.72, 0.76, 0.79, 0.82, 0.9, 0.97, 1.0],
            "health": [-18, -16, -12, -8, -3, 2, 0],
        },
        "high_consumption_low_training_no_response": {
            "trained": [0.78, 0.76, 0.78, 0.82, 0.86, 0.91, 1.0],
            "active": [1.08, 1.04, 1.0, 0.96, 0.94, 0.98, 1.0],
            "consumption": [1.0, 1.04, 0.98, 1.02, 0.96, 1.03, 1.0],
            "health": [5, 3, 1, -2, -5, -3, 0],
        },
        "low_consumption_high_training_blocked": {
            "trained": [0.78, 0.82, 0.88, 0.94, 1.0, 1.02, 1.0],
            "active": [0.72, 0.75, 0.73, 0.76, 0.79, 0.88, 1.0],
            "consumption": [0.9, 0.88, 0.85, 0.86, 0.9, 0.95, 1.0],
            "health": [2, 1, -4, -6, -5, -2, 0],
        },
        "low_consumption_low_training_recovery": {
            "trained": [0.25, 0.27, 0.38, 0.55, 0.72, 0.88, 1.0],
            "active": [0.4, 0.38, 0.42, 0.5, 0.62, 0.78, 1.0],
            "consumption": [0.66, 0.64, 0.67, 0.72, 0.8, 0.9, 1.0],
            "health": [-22, -24, -19, -14, -8, -3, 0],
        },
        "low_consumption_low_training_continued_risk": {
            "trained": [0.9, 0.88, 0.87, 0.9, 0.92, 0.95, 1.0],
            "active": [1.18, 1.08, 1.02, 0.9, 0.82, 0.86, 1.0],
            "consumption": [1.18, 1.12, 1.05, 0.96, 0.88, 0.9, 1.0],
            "health": [12, 8, 4, -2, -8, -5, 0],
        },
        "expansion_ready": {
            "trained": [0.62, 0.64, 0.7, 0.78, 0.86, 0.94, 1.0],
            "active": [0.58, 0.62, 0.68, 0.76, 0.86, 0.94, 1.0],
            "consumption": [0.7, 0.76, 0.84, 0.92, 1.02, 1.11, 1.18],
            "health": [-7, -4, -2, 1, 4, 7, 0],
        },
        "renewal_risk_stabilized": {
            "trained": [1.05, 0.98, 0.9, 0.86, 0.88, 0.96, 1.0],
            "active": [1.2, 1.1, 0.96, 0.86, 0.78, 0.88, 1.0],
            "consumption": [1.14, 1.08, 0.98, 0.9, 0.86, 0.92, 1.0],
            "health": [14, 8, 1, -7, -10, -4, 0],
        },
        "renewal_risk_deteriorated": {
            "trained": [1.25, 1.12, 1.0, 0.9, 0.82, 0.78, 1.0],
            "active": [1.45, 1.28, 1.08, 0.9, 0.76, 0.66, 1.0],
            "consumption": [1.42, 1.25, 1.08, 0.92, 0.78, 0.68, 1.0],
            "health": [24, 18, 11, 4, -5, -12, 0],
        },
        "mixed_poorly_targeted_recommendation": {
            "trained": [0.55, 0.6, 0.7, 0.84, 0.96, 1.05, 1.0],
            "active": [0.82, 0.78, 0.75, 0.74, 0.78, 0.88, 1.0],
            "consumption": [0.95, 0.9, 0.86, 0.84, 0.88, 0.96, 1.0],
            "health": [4, 0, -5, -7, -5, -1, 0],
        },
        "organic_improvement_no_ai": {
            "trained": [0.6, 0.62, 0.65, 0.7, 0.78, 0.9, 1.0],
            "active": [0.62, 0.66, 0.72, 0.8, 0.88, 0.95, 1.0],
            "consumption": [0.74, 0.79, 0.86, 0.91, 0.96, 1.0, 1.05],
            "health": [-10, -8, -5, -3, 0, 2, 0],
        },
    }
    return profiles[journey]


def _recommendations(account: dict, journey: str, rows: list[dict]) -> list[dict]:
    name = account["account_name"]
    slug = account["account_slug"]
    rep = account["primary_rep"]
    deal = account["deal_name"]
    target = "Data Engineer" if "Data" in deal else "Platform Owner"
    base = {
        "account_slug": slug,
        "account": name,
        "deal_id": account["deal_id"],
        "deal_name": deal,
        "generated_by": "Enablement Impact Agent",
        "owner": rep,
    }
    events = []

    def add(month: int, trigger: str, action: str, response: str, status: str, outcome: str, confidence: float, reason: str, target_persona: str = target):
        events.append({
            **base,
            "recommendation_id": f"REC-{slug[:18]}-{month:02d}",
            "date": f"2026-{month:02d}-15",
            "trigger": trigger,
            "recommended_action": action,
            "target_persona": target_persona,
            "reason": reason,
            "confidence": round(confidence, 2),
            "seller_response": response,
            "seller_response_reason": _response_reason(response, journey),
            "execution_status": status,
            "outcome": outcome,
            "subsequent_observation_month": "July",
        })

    if journey == "organic_improvement_no_ai":
        return events
    if journey in {"high_consumption_low_training_positive", "low_consumption_low_training_recovery"}:
        add(2, "High consumption with low trained-user coverage", f"Run role-based {target} workshop and identify two customer champions", "Accepted", "Completed", "Training coverage and active use improved over the following two months; consumption lift lagged until June.", 0.82, "January coverage was materially below consumption intensity.")
        if rows[-1]["segment"] == "High Consumption / High Training":
            add(6, "Sustained adoption and consumption improvement", "Open an expansion discovery conversation tied to the newly active workloads", "Modified", "In Progress", "Expansion opportunity progressed in July, but scope was narrowed to the highest-usage team.", 0.74, "June signals showed broader usage and improving account health.", "Economic Buyer")
    elif journey == "high_consumption_low_training_no_response":
        add(2, "Adoption concentration risk", f"Launch certification path for {target}s", "Rejected", "Not Started", "Account improved modestly from consumption already in flight, but trained-user concentration remains a July risk.", 0.68, "Few trained users supported a high-consumption account.")
    elif journey == "low_consumption_high_training_blocked":
        add(3, "Training not converting to platform usage", "Run technical blocker review before adding more learning content", "Accepted", "Completed", "Technical remediation helped active users recover in June, but consumption remained below expectation.", 0.79, "Training completion was healthy while usage and consumption lagged.", "Solutions Architect")
    elif journey == "mixed_poorly_targeted_recommendation":
        add(2, "Low adoption after training", f"Assign additional {target} training", "Modified", "Completed", "Training rose, but later account-team notes showed the primary blocker was implementation quality rather than skills.", 0.57, "Recommendation detected an enablement gap but targeted the wrong persona.")
    elif journey in {"renewal_risk_stabilized", "renewal_risk_deteriorated"}:
        add(5, "Renewal risk from declining active users and support escalations", "Start renewal-risk intervention with executive sponsor, technical remediation, and enablement refresh", "Accepted" if "stabilized" in journey else "Modified", "Completed" if "stabilized" in journey else "Partially Completed", "Account stabilized by July." if "stabilized" in journey else "Account continued deteriorating and opportunity regressed.", 0.84, "Multiple months showed declining adoption before renewal.")
    elif journey == "expansion_ready":
        add(6, "Expansion signal from rising workloads, active users, and stakeholder engagement", "Create expansion business case and run advanced capability workshop", "Accepted", "In Progress", "Expansion opportunity progressed in July after adoption gains.", 0.77, "Consumption increased before the recommendation and training reinforced the trend.", "Economic Buyer")
    else:
        add(3, "Low consumption and low training", "Schedule foundational enablement and account-team investigation", "Modified", "Completed", "Mixed outcome; training improved faster than consumption.", 0.71, "Account needed diagnosis before prescribing more content.", "CSM")
    return events


def _response_reason(response: str, journey: str) -> str:
    if response == "Accepted":
        return "Account team agreed the signal matched customer behavior and attached it to the account plan."
    if response == "Rejected":
        return "Seller believed customer-led activity already addressed the issue and timing was wrong."
    if "technical" in journey or "poorly" in journey:
        return "Seller changed the motion because the blocker was technical or persona targeting was off."
    return "Seller accepted the direction but narrowed scope to match customer priorities and available budget."


def generate() -> dict:
    rng = random.Random(42)
    customer_data = _load_json(CUSTOMER_HEALTH_PATH, {})
    enablement_rows = _load_json(ENABLEMENT_PATH, [])
    enablement_by_company = {}
    for row in enablement_rows:
        enablement_by_company.setdefault(row.get("company_slug"), []).append(row)

    account_histories = []
    recommendation_events = []
    customers = customer_data.get("customers", [])

    for idx, rec in enumerate(customers):
        info = rec.get("account_info", {})
        health = rec.get("health", {})
        enablement = rec.get("enablement", {})
        portfolio = rec.get("portfolio", {})
        slug = rec.get("customer_slug") or _slugify(info.get("account_name", f"account_{idx}"))
        erows = enablement_by_company.get(slug) or []
        erow = erows[0] if erows else {}
        top_deal = portfolio.get("top_deal") or {}
        target_segment = _target_july_segment(idx)
        journey = _journey_for(idx, target_segment)
        profile = _profile(journey)

        licensed = max(8, int(round(sum(float(r.get("licensed_users") or 0) for r in erows) or (portfolio.get("deal_count", 1) * 40))))
        if "High Training" in target_segment:
            target_coverage = rng.uniform(62, 82)
        else:
            target_coverage = rng.uniform(16, 36)
        july_trained = max(1, int(round(licensed * target_coverage / 100)))
        july_active = max(1, int(round(july_trained * (0.72 + (100 - float(enablement.get("skills_gap_risk_score") or 55)) / 250))))
        source_consumption = float(enablement.get("annual_consumption") or erow.get("azure_annual_consumption") or portfolio.get("total_pipeline_value") or 100_000)
        if "High Consumption" in target_segment:
            july_consumption = max(450_000, source_consumption * rng.uniform(0.9, 1.25), rng.uniform(500_000, 1_400_000))
        else:
            july_consumption = min(320_000, max(45_000, source_consumption * rng.uniform(0.18, 0.48), rng.uniform(70_000, 260_000)))
        july_health = float(health.get("score") or 55)
        july_pipeline = float(portfolio.get("total_pipeline_value") or top_deal.get("amount") or 0)
        days_to_renewal = int(enablement.get("days_to_renewal") or erow.get("days_to_renewal") or (60 + idx % 240))

        monthly = []
        previous_trained = 0
        for m_idx, (month_key, month_name) in enumerate(MONTHS):
            noise = rng.uniform(-0.035, 0.035)
            trained = int(_clamp(round(july_trained * profile["trained"][m_idx] + rng.randint(-1, 1)), 0, licensed))
            active = int(_clamp(round(july_active * profile["active"][m_idx] + rng.randint(-2, 2)), 0, licensed))
            consumption = round(max(0, july_consumption * (profile["consumption"][m_idx] + noise)), 2)
            health_score = round(_clamp(july_health + profile["health"][m_idx] + rng.uniform(-2.5, 2.5), 5, 95), 1)
            churn_score = round(_clamp(100 - health_score + max(0, 45 - trained * 100 / licensed) * 0.35 + rng.uniform(-3, 3), 5, 95), 1)
            training_completions = max(0, trained - previous_trained + rng.randint(0, 2))
            previous_trained = trained
            coverage = round(trained * 100 / licensed, 1)
            support = int(_clamp(round((100 - health_score) / 18 + (1 if "risk" in journey else 0) + rng.choice([0, 0, 1])), 0, 10))
            stage = _stage_at(top_deal.get("stage") or erow.get("stage") or "PROPOSAL", m_idx, journey)
            pipeline_value = round(july_pipeline * (0.86 + m_idx * 0.025 + rng.uniform(-0.04, 0.04))) if july_pipeline else 0
            if journey == "expansion_ready" and m_idx >= 5:
                pipeline_value = round(max(pipeline_value, july_pipeline * (1.05 + 0.08 * (m_idx - 5))))
            if "deteriorated" in journey and m_idx >= 5:
                pipeline_value = round(pipeline_value * 0.65)

            monthly.append({
                "month": month_key,
                "month_label": month_name,
                "licensed_users": licensed,
                "trained_users": trained,
                "active_users": active,
                "training_completions": training_completions,
                "workshop_attendance": int(_clamp(round(training_completions * rng.uniform(0.8, 1.4) + active * 0.08), 0, licensed)),
                "enablement_coverage_pct": coverage,
                "active_user_rate_pct": round(active * 100 / licensed, 1),
                "cloud_platform_consumption": consumption,
                "account_health_score": health_score,
                "account_health_status": _status(health_score),
                "churn_risk_score": churn_score,
                "churn_risk": _churn(churn_score),
                "support_escalation_count": support,
                "days_to_renewal": max(0, days_to_renewal - (6 - m_idx) * 30),
                "opportunity_stage": stage,
                "pipeline_value": pipeline_value,
                "arr_or_commercial_value": round(max(july_pipeline, july_consumption * 0.22)),
                "segment": _segment(consumption, coverage),
                "observation": _observation(journey, m_idx),
            })

        account = {
            "account_slug": slug,
            "account_name": info.get("account_name"),
            "primary_rep": info.get("primary_rep"),
            "segment": info.get("segment"),
            "primary_product": info.get("primary_product"),
            "journey_type": journey,
            "deal_id": top_deal.get("deal_id") or erow.get("deal_id"),
            "deal_name": top_deal.get("deal_name") or erow.get("deal_name"),
            "monthly_observations": monthly,
            "journey_summary": {
                "january_segment": monthly[0]["segment"],
                "july_segment": monthly[-1]["segment"],
                "health_delta": round(monthly[-1]["account_health_score"] - monthly[0]["account_health_score"], 1),
                "coverage_delta": round(monthly[-1]["enablement_coverage_pct"] - monthly[0]["enablement_coverage_pct"], 1),
                "active_user_delta": monthly[-1]["active_users"] - monthly[0]["active_users"],
                "consumption_delta": round(monthly[-1]["cloud_platform_consumption"] - monthly[0]["cloud_platform_consumption"], 2),
                "outcome": _outcome(journey),
            },
        }
        recs = _recommendations(account, journey, monthly)
        account["recommendation_ids"] = [r["recommendation_id"] for r in recs]
        account_histories.append(account)
        recommendation_events.extend(recs)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_version": "gtm_feedback_history_v1",
        "historical_period": {"start": "2026-01-01", "end": "2026-07-31", "latest_month": "July 2026"},
        "source_files": [
            "knowledge_base/customer_health_data.json",
            "knowledge_base/enablement_data.json",
            "knowledge_base/meddpicc_pipeline.json",
        ],
        "design_note": "Synthetic account behavior is generated from existing CRM entities and July account-health / enablement anchors. It intentionally includes successful, unsuccessful, stagnant, mixed, rejected, and no-AI-intervention journeys and should not be interpreted as causal proof.",
        "accounts": account_histories,
        "recommendations": recommendation_events,
    }


def _observation(journey: str, month_idx: int) -> str:
    notes = {
        0: "Baseline month; account behavior reflects inherited adoption and renewal posture.",
        1: "AI/account team signal review month for accounts with visible gaps.",
        2: "Seller response and intervention planning begin where teams choose to act.",
        3: "Execution month; training or remediation activity may rise before business metrics move.",
        4: "Lagging adoption indicators begin to show whether the motion is converting.",
        5: "AI can detect expansion or renewal-risk patterns from accumulated signals.",
        6: "Latest account state used by the July portfolio view.",
    }
    if journey == "organic_improvement_no_ai":
        return "Customer-led operating rhythm improves without an AI recommendation in this period."
    if "poorly" in journey and month_idx in {3, 4}:
        return "Training increased, but usage did not move proportionally because the recommendation was poorly targeted."
    if "deteriorated" in journey and month_idx >= 4:
        return "Renewal risk escalates as active usage and consumption soften despite intervention."
    return notes[month_idx]


def _outcome(journey: str) -> str:
    if "positive" in journey or "recovery" in journey:
        return "Improved after enablement intervention with lagged adoption and consumption gains."
    if "no_response" in journey:
        return "Recommendation rejected; account remains commercially valuable but concentrated."
    if "blocked" in journey:
        return "Training was present, but technical blockers limited consumption conversion."
    if "deteriorated" in journey:
        return "Renewal risk continued to deteriorate despite partial action."
    if "stabilized" in journey:
        return "Renewal-risk intervention stabilized the account by July."
    if "expansion" in journey:
        return "Expansion readiness emerged after sustained adoption and workload growth."
    if "organic" in journey:
        return "Improved without AI intervention, useful as a non-treatment comparison."
    return "Mixed result; useful for evaluating recommendation quality without assuming causation."


def main() -> None:
    payload = generate()
    with OUTPUT_PATH.open("w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    print(f"Wrote {OUTPUT_PATH}")
    print(f"Accounts: {len(payload['accounts'])}")
    print(f"Recommendations: {len(payload['recommendations'])}")


if __name__ == "__main__":
    main()
