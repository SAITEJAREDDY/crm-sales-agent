#!/usr/bin/env python3
"""Trigger rules for account enablement risk and opportunity detection."""

from __future__ import annotations

from typing import Any


TRIGGER_DEFINITIONS = {
    "LOW_TRAINED_COVERAGE": "trained_users / licensed_users < 0.25",
    "STALLED_ADOPTION": "attendance_rate_pct < 50.0 and days_inactive > 30",
    "RENEWAL_RISK": "days_to_renewal < 90 and enablement_coverage_pct < 40.0",
    "HIGH_CONS_LOW_TRAIN": "azure_annual_consumption > 50000 and skills_gap_risk_score > 70",
}


def evaluate_enablement_triggers(record: dict[str, Any]) -> list[dict[str, Any]]:
    licensed = max(int(record.get("licensed_users") or 0), 0)
    trained = max(int(record.get("trained_users") or 0), 0)
    coverage_ratio = trained / licensed if licensed else 0.0
    attendance = float(record.get("attendance_rate_pct") or 0.0)
    inactive = int(record.get("days_inactive") or 0)
    renewal = int(record.get("days_to_renewal") or 9999)
    coverage_pct = float(record.get("enablement_coverage_pct") or 0.0)
    consumption = float(record.get("azure_annual_consumption") or 0.0)
    risk = int(record.get("skills_gap_risk_score") or 0)

    triggers: list[dict[str, Any]] = []
    if coverage_ratio < 0.25:
        triggers.append({
            "code": "LOW_TRAINED_COVERAGE",
            "severity": "HIGH",
            "reason": f"Only {trained}/{licensed} licensed users are trained.",
        })
    if attendance < 50.0 and inactive > 30:
        triggers.append({
            "code": "STALLED_ADOPTION",
            "severity": "MEDIUM",
            "reason": f"Attendance is {attendance:.1f}% and account has been inactive {inactive} days.",
        })
    if renewal < 90 and coverage_pct < 40.0:
        triggers.append({
            "code": "RENEWAL_RISK",
            "severity": "CRITICAL",
            "reason": f"Renewal is in {renewal} days with {coverage_pct:.1f}% enablement coverage.",
        })
    if consumption > 50_000 and risk > 70:
        triggers.append({
            "code": "HIGH_CONS_LOW_TRAIN",
            "severity": "CRITICAL",
            "reason": f"${consumption:,.0f} annual consumption with skills risk score {risk}.",
        })
    return triggers


def highest_severity(triggers: list[dict[str, Any]]) -> str:
    order = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    if not triggers:
        return "LOW"
    return max((t.get("severity", "LOW") for t in triggers), key=lambda s: order.get(s, 0))
