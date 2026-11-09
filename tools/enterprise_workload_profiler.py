"""Deterministic enterprise workload profiling for architecture planning."""
from __future__ import annotations

from copy import deepcopy


def classify_enterprise_scale(profile: dict) -> str:
    employees = float(profile.get("employee_count") or 0)
    users = float(profile.get("expected_users") or 0)
    data = float(profile.get("data_volume_tb") or 0)
    if employees >= 10000 or users >= 100000 or data >= 500:
        return "LARGE_ENTERPRISE"
    if employees >= 1000 or users >= 10000 or data >= 50:
        return "ENTERPRISE"
    if employees >= 100 or users >= 1000 or data >= 5:
        return "MID_MARKET"
    return "SMALL"


def profile_enterprise_workload(profile: dict, deal: dict | None = None) -> dict:
    """Return requirements with explicit provenance; this never asserts compliance."""
    p = deepcopy(profile or {})
    industry = str(p.get("industry") or (deal or {}).get("account", {}).get("industry") or "").lower()
    requirements = []

    def add(name, confidence="MEDIUM", reason=""):
        requirements.append({"requirement": name, "source": "INDUSTRY_INFERRED", "confidence": confidence, "reason": reason})

    if any(x in industry for x in ("financial", "bank", "insurance")):
        add("centralized_security_monitoring", "HIGH", "financial workloads benefit from centralized audit and security telemetry")
        add("private_networking", "HIGH", "sensitive enterprise data")
        add("multi_region_disaster_recovery", "MEDIUM", "business continuity")
    elif "retail" in industry:
        add("customer_facing_scalability", "HIGH", "variable customer demand")
        add("event_ingestion", "MEDIUM", "transaction and behavioral events")
        add("api_management", "MEDIUM", "customer and partner APIs")
    elif "health" in industry:
        add("strong_access_controls", "HIGH", "sensitive data access")
        add("encryption_and_auditability", "HIGH", "sensitive data")
        add("private_networking", "MEDIUM", "data isolation")
    elif "manufactur" in industry:
        add("integration", "HIGH", "plant and enterprise system integration")
        add("telemetry_event_ingestion", "MEDIUM", "operational telemetry")
        add("data_platform", "MEDIUM", "analytics and operational data")
    if p.get("dr_required"):
        add("multi_region_disaster_recovery", "HIGH", "explicit workload assumption")
    if p.get("centralized_logging_required") or p.get("siem_required"):
        add("centralized_security_monitoring", "HIGH", "explicit workload assumption")
    scale = classify_enterprise_scale(p)
    return {"industry": industry or "unknown", "enterprise_tier": scale.lower(), "enterprise_scale": scale,
            "requirements": requirements, "missing_information": [k for k in ("availability_target", "primary_region") if not p.get(k)]}


build_enterprise_profile = profile_enterprise_workload
