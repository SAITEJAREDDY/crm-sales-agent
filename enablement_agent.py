#!/usr/bin/env python3
"""Customer Enablement & Skills Intelligence recommendation agent."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from tools.enablement_eval import TRACK_TO_SERVICES, evaluate_recommendation
from tools.enablement_rules import evaluate_enablement_triggers


BASE_DIR = Path(__file__).resolve().parent
ENABLEMENT_DATA = BASE_DIR / "knowledge_base" / "enablement_data.json"
PIPELINE_JSON = BASE_DIR / "knowledge_base" / "meddpicc_pipeline.json"
RESOURCE_DIR = BASE_DIR / "knowledge_base" / "resource_estimates"


CATEGORY_COURSE_RULES = [
    (
        ("observability", "logging", "monitoring", "audit logging"),
        "Azure Reliability and Observability",
    ),
    (
        ("security", "encryption", "governance", "compliance", "regulatory", "immutable", "audit"),
        "Azure Security & Governance",
    ),
    (
        ("ml risk", "machine learning", "ai ", "ai/", "ai powered", "risk scoring", "fraud"),
        "Azure AI and ML Operations",
    ),
    (
        ("api", "integration", "pipeline"),
        "Azure Integration Architecture",
    ),
    (
        ("analytics", "business intelligence", "reporting", "data mesh", "data fabric", "data warehousing"),
        "Azure Analytics Engineering",
    ),
    (
        ("cloud infrastructure", "cost", "optimization"),
        "Azure Cost Optimization",
    ),
]


CATEGORY_PERSONA_RULES = [
    (
        ("observability", "logging", "monitoring", "audit logging"),
        ["Reliability Engineering Lead", "Cloud Operations Lead", "Platform Engineering Lead"],
    ),
    (
        ("security", "encryption", "governance", "compliance", "regulatory", "immutable", "audit"),
        ["Cloud Security Architect", "DevSecOps Lead", "Compliance Operations Lead"],
    ),
    (
        ("ml risk", "machine learning", "ai ", "ai/", "ai powered", "risk scoring"),
        ["ML Platform Lead", "Risk Analytics Lead", "Model Operations Lead"],
    ),
    (
        ("fraud",),
        ["Fraud Operations Lead", "Risk Analytics Lead", "ML Platform Lead"],
    ),
    (
        ("api", "integration", "pipeline"),
        ["Integration Architect", "Platform Engineering Lead", "Data Engineering Lead"],
    ),
    (
        ("analytics", "business intelligence", "reporting"),
        ["Analytics Engineering Lead", "BI Platform Owner", "Data Analyst Lead"],
    ),
    (
        ("data mesh", "data fabric", "data warehousing"),
        ["Data Platform Owner", "Data Engineering Lead", "Analytics Engineering Lead"],
    ),
    (
        ("cloud infrastructure", "cost", "optimization"),
        ["Cloud Platform Owner", "FinOps Lead", "Infrastructure Engineering Lead"],
    ),
    (
        ("client portal", "digital experience"),
        ["Digital Product Owner", "Frontend Platform Lead", "Customer Experience Lead"],
    ),
]


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def _resource_file_for(company_slug: str, deal_slug: str) -> Path | None:
    candidates = sorted(RESOURCE_DIR.glob(f"{company_slug}_{deal_slug}.json"))
    return candidates[0] if candidates else None


def _deal_context(company_slug: str, deal_slug: str) -> dict[str, Any]:
    deals = _load_json(PIPELINE_JSON, [])
    for deal in deals:
        if slugify(deal.get("company", "")) == company_slug and slugify(deal.get("deal_name", "")) == deal_slug:
            return deal
    return {}


def _resource_descriptor(resource_estimate: dict[str, Any]) -> str:
    parts = [
        resource_estimate.get("product", ""),
        resource_estimate.get("base_category", ""),
        resource_estimate.get("implementation_pattern", ""),
    ]
    return " ".join(str(p).lower() for p in parts if p)


def _first_category_match(descriptor: str, rules: list[tuple[tuple[str, ...], Any]]) -> Any | None:
    for keywords, value in rules:
        if any(keyword in descriptor for keyword in keywords):
            return value
    return None


def _pick_course(resource_estimate: dict[str, Any], triggers: list[dict[str, Any]]) -> str:
    trigger_codes = [t["code"] for t in triggers]
    if "HIGH_CONS_LOW_TRAIN" in trigger_codes:
        descriptor = _resource_descriptor(resource_estimate)
        if "cloud infrastructure" in descriptor or "cost" in descriptor:
            return "Azure Cost Optimization"

    category_course = _first_category_match(_resource_descriptor(resource_estimate), CATEGORY_COURSE_RULES)
    if category_course:
        return category_course

    services_text = json.dumps(resource_estimate).lower()
    track_order = [
        "Azure Security & Governance",
        "Azure AI and ML Operations",
        "Azure Integration Architecture",
        "Azure Analytics Engineering",
        "Azure Reliability and Observability",
        "Azure Cost Optimization",
        "Azure Data Platform Fundamentals",
    ]
    for track in track_order:
        services = TRACK_TO_SERVICES.get(track, set())
        if any(service in services_text for service in services):
            return track
    return "Azure Cost Optimization"


def _target_personas(resource_estimate: dict[str, Any], enablement: dict[str, Any]) -> list[str]:
    category_personas = _first_category_match(_resource_descriptor(resource_estimate), CATEGORY_PERSONA_RULES)
    roster_roles = [l.get("role_persona") for l in enablement.get("learner_roster", []) if l.get("role_persona")]
    if category_personas:
        generic_roles = {"Data Engineer", "DevSecOps Lead", "FinOps Lead"}
        specific_roster = [r for r in sorted(set(roster_roles)) if r not in generic_roles]
        merged = list(category_personas)
        for role in specific_roster:
            if role not in merged:
                merged.append(role)
        return merged[:3]
    if roster_roles:
        return sorted(set(roster_roles))[:3]
    return ["Lead Cloud Architect", "Platform Engineering Lead"]


def _money(value: float) -> str:
    return f"${value:,.0f}"


def _pct(value: float) -> str:
    return f"{value:.1f}%"


def _coverage_gap(coverage_pct: float, target_pct: float = 70.0) -> float:
    return round(max(0.0, target_pct - coverage_pct), 1)


def _skills_risk_band(risk_score: int) -> str:
    if risk_score >= 75:
        return "High"
    if risk_score >= 50:
        return "Medium"
    return "Low"


def _consumption_at_risk(consumption: float, risk_score: int, coverage_pct: float) -> float:
    risk_weight = min(max(risk_score, 0), 100) / 100
    coverage_weight = min(max(_coverage_gap(coverage_pct) / 70, 0), 1)
    return round(consumption * max(risk_weight, coverage_weight) * 0.35)


def _expansion_potential(consumption: float) -> float:
    return round(consumption * 0.20)


def _renewal_urgency(days_to_renewal: int) -> str:
    if days_to_renewal <= 45:
        return "Immediate"
    if days_to_renewal <= 90:
        return "Near-term"
    if days_to_renewal <= 180:
        return "Planned"
    return "Watch"


def _adoption_productivity_risk(attendance_pct: float, days_inactive: int) -> str:
    if attendance_pct < 40 or days_inactive > 45:
        return "High"
    if attendance_pct < 60 or days_inactive > 30:
        return "Medium"
    return "Low"


def _trigger_reason(triggers: list[dict[str, Any]], code: str, fallback: str) -> str:
    match = next((t for t in triggers if t.get("code") == code), None)
    return match.get("reason") if match else fallback


def _clean_agent_fragment(text: Any) -> str:
    value = str(text or "")
    value = re.sub(r"\*\*", "", value)
    value = re.sub(r"\(Source:[^)]+\)", "", value, flags=re.I)
    value = re.sub(r"^\s*[-*:.\d\s]+", "", value).strip()
    label_match = re.match(r"^([A-Za-z][A-Za-z\s/-]{2,60}):", value)
    label = label_match.group(1).strip() if label_match else ""
    value = re.sub(r"^[A-Za-z][A-Za-z\s/-]{2,60}:\s*", "", value).strip()
    value = re.sub(r"\s+", " ", value).strip(" ;")
    lower = f"{label} {value}".lower()
    if "procurement" in lower:
        return "Procurement process not confirmed"
    if "metric" in lower or "quantifiable" in lower or "measured" in lower:
        return "Success metrics need clarification"
    if "stalled" in lower or "blocker" in lower:
        return "Deal blocker needs owner and next step"
    if "economic buyer" in lower:
        return "Economic buyer alignment needs confirmation"
    if label:
        return label
    if len(value) > 120:
        value = value[:117].rsplit(" ", 1)[0] + "..."
    return value


def _clean_agent_fragments(items: list[Any], limit: int = 2) -> list[str]:
    cleaned = []
    for item in items or []:
        value = _clean_agent_fragment(item)
        if value:
            cleaned.append(value)
        if len(cleaned) >= limit:
            break
    return cleaned


def _build_business_metrics(
    enablement_record: dict[str, Any],
    triggers: list[dict[str, Any]],
) -> list[dict[str, str]]:
    licensed = int(enablement_record.get("licensed_users") or 0)
    trained = int(enablement_record.get("trained_users") or 0)
    coverage = float(enablement_record.get("enablement_coverage_pct") or 0)
    risk = int(enablement_record.get("skills_gap_risk_score") or 0)
    consumption = float(enablement_record.get("azure_annual_consumption") or 0)
    renewal = int(enablement_record.get("days_to_renewal") or 999)
    at_risk = _consumption_at_risk(consumption, risk, coverage)

    return [
        {"metric": "Azure Annual Consumption", "value": _money(consumption), "notes": "Source-backed annual consumption tied to this account/deal"},
        {"metric": "Estimated Consumption at Risk", "value": _money(at_risk), "notes": "Proxy based on skills risk and enablement coverage gap"},
        {"metric": "Estimated Expansion Potential", "value": _money(_expansion_potential(consumption)), "notes": "Proxy using 20% of annual consumption"},
        {"metric": "Enablement Coverage", "value": _pct(coverage), "notes": f"{trained}/{licensed} licensed users trained"},
        {"metric": "Coverage Gap to Target", "value": _pct(_coverage_gap(coverage)), "notes": "Proxy gap against 70% target coverage"},
        {"metric": "Skills-Gap Risk", "value": f"{risk}/100", "notes": f"{_skills_risk_band(risk)} risk band"},
        {"metric": "Days to Renewal", "value": str(renewal), "notes": f"{_renewal_urgency(renewal)} renewal urgency"},
        {"metric": "Active Trigger Count", "value": str(len(triggers)), "notes": "Source-backed enablement trigger flags"},
    ]


def _build_predictive_signals(
    enablement_record: dict[str, Any],
    triggers: list[dict[str, Any]],
    pipeline_packet: dict[str, Any],
) -> list[dict[str, str]]:
    coverage = float(enablement_record.get("enablement_coverage_pct") or 0)
    attendance = float(enablement_record.get("attendance_rate_pct") or 0)
    inactive = int(enablement_record.get("days_inactive") or 0)
    risk = int(enablement_record.get("skills_gap_risk_score") or 0)
    renewal = int(enablement_record.get("days_to_renewal") or 999)
    consumption = float(enablement_record.get("azure_annual_consumption") or 0)
    signals = [
        {
            "signal": "Renewal Risk",
            "status": _renewal_urgency(renewal),
            "evidence": _trigger_reason(triggers, "RENEWAL_RISK", f"{renewal} days to renewal with {_pct(coverage)} coverage."),
            "recommended_action": "Attach enablement milestone review to the renewal action plan.",
        },
        {
            "signal": "Training Coverage Gap",
            "status": _skills_risk_band(risk),
            "evidence": _trigger_reason(triggers, "LOW_TRAINED_COVERAGE", f"Coverage gap is {_pct(_coverage_gap(coverage))}."),
            "recommended_action": "Prioritize role-based training for the personas in the recommended path.",
        },
        {
            "signal": "Adoption Productivity Risk",
            "status": _adoption_productivity_risk(attendance, inactive),
            "evidence": f"Attendance is {_pct(attendance)} and days inactive is {inactive}.",
            "recommended_action": "Use manager coaching and AE follow-up to restart learning momentum.",
        },
        {
            "signal": "High Consumption / Low Training",
            "status": "High" if consumption > 50_000 and risk > 70 else "Watch",
            "evidence": _trigger_reason(triggers, "HIGH_CONS_LOW_TRAIN", f"{_money(consumption)} consumption with {risk}/100 skills risk."),
            "recommended_action": "Position paid enablement when consumption protection or expansion is material.",
        },
    ]
    for gap in (pipeline_packet.get("stakeholder_gaps") or [])[:2]:
        signals.append({
            "signal": "Stakeholder Gap",
            "status": "Deal Committee",
            "evidence": str(gap),
            "recommended_action": "Resolve stakeholder risk through targeted champion and manager coaching.",
        })
    return signals


def _build_impact_pillars(
    enablement_record: dict[str, Any],
    triggers: list[dict[str, Any]],
    pipeline_packet: dict[str, Any],
) -> list[dict[str, str]]:
    coverage = float(enablement_record.get("enablement_coverage_pct") or 0)
    risk = int(enablement_record.get("skills_gap_risk_score") or 0)
    consumption = float(enablement_record.get("azure_annual_consumption") or 0)
    attendance = float(enablement_record.get("attendance_rate_pct") or 0)
    inactive = int(enablement_record.get("days_inactive") or 0)
    at_risk = _consumption_at_risk(consumption, risk, coverage)
    committee_risks = pipeline_packet.get("top_risks") or []
    return [
        {
            "pillar": "Performance",
            "risk_level": "High" if at_risk > 25_000 or "RENEWAL_RISK" in [t.get("code") for t in triggers] else "Medium",
            "metric_impacted": "Renewal confidence, consumption protection, expansion readiness",
            "evidence": f"{_money(at_risk)} estimated consumption at risk; {len(committee_risks)} deal committee risk signals.",
            "enablement_response": "Tie learning milestones to the account action plan and renewal/consumption review.",
        },
        {
            "pillar": "Proficiency",
            "risk_level": _skills_risk_band(risk),
            "metric_impacted": "Role readiness, certification progress, manager-observed competency",
            "evidence": f"{_pct(coverage)} coverage and {risk}/100 skills-gap risk.",
            "enablement_response": "Run role-based path completion and manager coaching against the target personas.",
        },
        {
            "pillar": "Productivity",
            "risk_level": _adoption_productivity_risk(attendance, inactive),
            "metric_impacted": "Time to useful content, meeting readiness, adoption follow-through",
            "evidence": f"{_pct(attendance)} attendance and {inactive} inactive days.",
            "enablement_response": "Give the field team one sales play, one content pack, and one CTA to reduce prep friction.",
        },
    ]


def _build_manager_coaching_toolkit(
    personas: list[str],
    course: str,
    enablement_record: dict[str, Any],
    pipeline_packet: dict[str, Any],
) -> dict[str, Any]:
    primary = personas[0] if personas else "account technical owner"
    actions = pipeline_packet.get("next_actions") or []
    return {
        "coaching_focus": f"Help the field manager inspect whether {primary} can apply {course} to the active deal risk.",
        "behaviors_to_inspect": [
            "Can the champion explain the business outcome and adoption milestone in their own words?",
            "Can the technical persona identify the next implementation blocker and owner?",
            "Has the AE/CSM tied training progress to the renewal or consumption review?",
        ],
        "manager_questions": [
            f"What evidence shows {primary} is ready to execute the next technical milestone?",
            "Which enablement gap could slow consumption, renewal confidence, or expansion?",
            "What customer-facing proof point should be reviewed in the next account meeting?",
        ],
        "evidence_to_review": [
            f"Coverage: {_pct(float(enablement_record.get('enablement_coverage_pct') or 0))}",
            f"Skills risk: {int(enablement_record.get('skills_gap_risk_score') or 0)}/100",
            actions[0] if actions else "Next account action from AE/CSM plan",
        ],
    }


def _build_success_measures(enablement_record: dict[str, Any]) -> list[dict[str, str]]:
    coverage = float(enablement_record.get("enablement_coverage_pct") or 0)
    attendance = float(enablement_record.get("attendance_rate_pct") or 0)
    risk = int(enablement_record.get("skills_gap_risk_score") or 0)
    inactive = int(enablement_record.get("days_inactive") or 0)
    return [
        {"metric": "Enablement Coverage", "current": _pct(coverage), "target": "70.0%+", "owner": "CSM / Enablement", "time_horizon": "30-45 days"},
        {"metric": "Attendance Rate", "current": _pct(attendance), "target": "65.0%+", "owner": "CSM / Manager", "time_horizon": "Next cohort"},
        {"metric": "Skills-Gap Risk", "current": f"{risk}/100", "target": "<50/100", "owner": "SA / Manager", "time_horizon": "Before renewal review"},
        {"metric": "Inactive Days", "current": str(inactive), "target": "<14", "owner": "AE / CSM", "time_horizon": "This month"},
    ]


def build_enablement_recommendation(
    enablement_record: dict[str, Any],
    deal_context: dict[str, Any] | None = None,
    resource_estimate: dict[str, Any] | None = None,
    pipeline_packet: dict[str, Any] | None = None,
    run_eval: bool = True,
) -> dict[str, Any]:
    deal_context = deal_context or _deal_context(enablement_record["company_slug"], enablement_record["deal_slug"])
    if resource_estimate is None:
        rpath = _resource_file_for(enablement_record["company_slug"], enablement_record["deal_slug"])
        resource_estimate = _load_json(rpath, {}) if rpath else {}

    triggers = evaluate_enablement_triggers(enablement_record)
    pipeline_packet = pipeline_packet or {}
    trigger_codes = [t["code"] for t in triggers]
    course = _pick_course(resource_estimate, triggers)
    personas = _target_personas(resource_estimate, enablement_record)
    product = resource_estimate.get("product") or deal_context.get("deal_name", "Azure solution")
    consumption = float(enablement_record.get("azure_annual_consumption") or 0)
    coverage = float(enablement_record.get("enablement_coverage_pct") or 0)
    renewal = int(enablement_record.get("days_to_renewal") or 120)
    business_metrics = _build_business_metrics(enablement_record, triggers)
    predictive_signals = _build_predictive_signals(enablement_record, triggers, pipeline_packet)
    impact_pillars = _build_impact_pillars(enablement_record, triggers, pipeline_packet)

    paid_motion = "HIGH_CONS_LOW_TRAIN" in trigger_codes or consumption > 100_000
    motion_type = "PAID_EXPANSION" if paid_motion else "FREE_CONCESSION"
    package = "$25k Custom Enablement Package" if paid_motion else "Azure ECIF or ProServ credits"
    partner_note = ""
    if enablement_record.get("partner_managed_status"):
        partner_note = (
            "Partner-managed account: route the motion through architecture governance, "
            "partner handoff, and deployment sign-off rather than basic end-user training."
        )
    else:
        partner_note = "Direct account: prioritize hands-on role-based training and AE/CSM follow-through."

    recommendation = {
        "recommended_course_path": course,
        "target_personas": personas,
        "account_health_rationale": (
            f"{enablement_record.get('company')} has {coverage:.1f}% trained-user coverage against "
            f"${consumption:,.0f} annual Azure consumption for {product}. "
            f"The active enablement triggers ({', '.join(trigger_codes) or 'none'}) indicate skill debt that can raise support risk, slow adoption, and weaken renewal confidence."
        ),
        "deal_committee_context": pipeline_packet,
        "business_metrics": business_metrics,
        "impact_pillars": impact_pillars,
        "predictive_signals": predictive_signals,
        "manager_coaching_toolkit": _build_manager_coaching_toolkit(
            personas,
            course,
            enablement_record,
            pipeline_packet,
        ),
        "success_measures": _build_success_measures(enablement_record),
        "recommended_content": {
            "course_path": course,
            "target_personas": personas,
            "sales_play": (
                f"Use a {course} enablement sprint to protect adoption of {product}, "
                "then convert proof of progress into the account team CTA."
            ),
            "resource_product": resource_estimate.get("product") or product,
            "resource_category": resource_estimate.get("base_category") or "",
            "implementation_pattern": resource_estimate.get("implementation_pattern") or "",
            "call_to_action": (
                f"Schedule a 30-min Skills Assessment with {personas[0]} before the renewal window "
                f"in {renewal} days."
            ),
        },
        "free_to_paid_opportunity": {
            "motion_type": motion_type,
            "pitch_narrative": (
                f"Position {package} as the fastest way to protect the {product} investment before renewal. "
                f"The AE can frame this as a targeted skills plan for {', '.join(personas)} tied to consumption stability, adoption, and measurable enablement coverage."
            ),
        },
        "cta_for_account_team": (
            f"Schedule a 30-min Skills Assessment with {personas[0]} before the renewal window "
            f"in {renewal} days and attach the {course} plan to the account action plan."
        ),
        "partner_adaptation_note": partner_note,
        "trigger_flags": triggers,
        "source_refs": {
            "company_slug": enablement_record["company_slug"],
            "deal_slug": enablement_record["deal_slug"],
            "resource_product": resource_estimate.get("product"),
            "meddpicc_score": deal_context.get("overall_score"),
            "pipeline_packet_used": bool(pipeline_packet),
        },
    }

    if pipeline_packet:
        gaps = _clean_agent_fragments(pipeline_packet.get("stakeholder_gaps") or [], limit=2)
        actions = _clean_agent_fragments(pipeline_packet.get("next_actions") or [], limit=1)
        if gaps:
            recommendation["account_health_rationale"] += " Committee context shows unresolved deal risk that enablement should help de-risk."
            existing = recommendation.get("predictive_signals") or []
            for gap in gaps:
                existing.append({
                    "signal": "Deal Committee Risk",
                    "status": "Needs Follow-Up",
                    "evidence": gap,
                    "recommended_action": "Assign an owner and connect the enablement plan to the next account-team action.",
                })
            recommendation["predictive_signals"] = existing
        if actions:
            recommendation["cta_for_account_team"] += (
                " Align enablement follow-up to committee next action: " + actions[0]
            )

    if run_eval:
        recommendation["evaluation"] = evaluate_recommendation(
            recommendation,
            {**enablement_record, "deal_context": deal_context, "pipeline_packet": pipeline_packet},
            resource_estimate,
            log=True,
        )
    return recommendation


def recommend_for_deal(
    company: str = "",
    deal: str = "",
    company_slug: str = "",
    deal_slug: str = "",
    pipeline_packet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = _load_json(ENABLEMENT_DATA, [])
    company_slug = company_slug or slugify(company)
    deal_slug = deal_slug or slugify(deal)
    match = next(
        (
            r for r in data
            if (not company_slug or r.get("company_slug") == company_slug)
            and (not deal_slug or r.get("deal_slug") == deal_slug)
        ),
        None,
    )
    if not match:
        raise ValueError("No enablement record found for requested company/deal")
    return build_enablement_recommendation(match, pipeline_packet=pipeline_packet)
