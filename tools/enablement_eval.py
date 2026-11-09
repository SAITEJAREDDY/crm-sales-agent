#!/usr/bin/env python3
"""Evaluation and guardrails for enablement recommendations."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Any


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "metrics.db")
LANGSMITH_PROJECT = "deal-desk-agent"

TRACK_TO_SERVICES = {
    "Azure Data Platform Fundamentals": {"Azure Data Factory", "Azure SQL Database", "Azure Synapse", "Amazon S3", "AWS Glue", "PostgreSQL"},
    "Azure Cost Optimization": {"Azure Virtual Machines", "Azure SQL Database", "Azure Blob Storage", "Azure Functions", "AWS Glue", "Amazon S3", "Cloud Infrastructure Management"},
    "Azure Security & Governance": {"Azure Key Vault", "Azure Policy", "Azure Monitor", "data_security_encryption", "Data Security and Encryption", "Regulatory Workflow Automation", "AI Powered Compliance Automation", "Immutable Record Keeping", "ComplianceAI", "RegTrack", "SecureVault", "SecurityO"},
    "Azure AI and ML Operations": {"Azure Machine Learning", "ml_risk_scoring_platform", "ML Risk Scoring Platform", "Real Time Fraud Detection", "RiskEngine", "RiskA", "FraudGuard"},
    "Azure Integration Architecture": {"Azure API Management", "Azure Functions", "data_pipeline_integration", "AWS Glue", "API Integration Platform", "Data Pipeline and Integration", "Enterprise Integration Platform", "CapitalConnect", "DataBridge"},
    "Azure Analytics Engineering": {"Azure Synapse", "Azure Databricks", "Azure Data Factory", "business_intelligence_analytics", "Business Intelligence and Analytics", "Unified Analytics Platform", "Cloud Native Reporting", "Data Mesh and Fabric", "Data Warehousing and Storage", "InsightIQ", "AnalyticsHub", "ReportingCloud", "DataFabric", "DataVault"},
    "Azure Reliability and Observability": {"Azure Monitor", "observability_audit_logging", "ALB", "Observability and Audit Logging", "AuditTrail"},
}


def ensure_eval_table() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS enablement_eval_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_slug TEXT,
            deal_slug TEXT,
            groundedness_score REAL,
            specificity_score REAL,
            actionability_score REAL,
            passed BOOLEAN,
            failure_reasons TEXT,
            evaluated_at TIMESTAMP,
            langsmith_run_id TEXT,
            full_json TEXT
        )
        """)
        cols = [r[1] for r in con.execute("PRAGMA table_info(enablement_eval_results)").fetchall()]
        if "langsmith_run_id" not in cols:
            con.execute("ALTER TABLE enablement_eval_results ADD COLUMN langsmith_run_id TEXT")


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9\-]{2,}", text.lower())}


def _resource_services(resource_estimate: dict[str, Any]) -> set[str]:
    services = set()
    for item in resource_estimate.get("additional_services") or []:
        if item.get("service"):
            services.add(str(item["service"]))
    for section in ("compute", "storage", "database", "network"):
        value = resource_estimate.get(section)
        if isinstance(value, dict):
            for v in value.values():
                if isinstance(v, str):
                    services.add(v)
    for field in ("product", "base_category", "implementation_pattern"):
        if resource_estimate.get(field):
            services.add(str(resource_estimate[field]))
    return services


def evaluate_recommendation(
    recommendation: dict[str, Any],
    source_context: dict[str, Any],
    resource_estimate: dict[str, Any],
    log: bool = True,
) -> dict[str, Any]:
    services = _resource_services(resource_estimate)
    course = recommendation.get("recommended_course_path", "")
    allowed_tracks = [track for track, svc in TRACK_TO_SERVICES.items() if svc & services]
    sku_match = course in allowed_tracks if allowed_tracks else True
    specificity = 1.0 if sku_match and course else 0.0

    source_text = json.dumps(source_context, sort_keys=True) + " " + json.dumps(resource_estimate, sort_keys=True)
    trigger_codes = {t.get("code") for t in source_context.get("trigger_flags", [])}
    rec_trigger_codes = {t.get("code") for t in recommendation.get("trigger_flags", [])}
    grounded_checks = [
        bool(source_context.get("company_slug") and source_context["company_slug"] in source_text),
        bool(source_context.get("deal_slug") and source_context["deal_slug"] in source_text),
        bool(str(resource_estimate.get("product", "")) and str(resource_estimate.get("product", "")) in source_text),
        bool(course and (sku_match or course in source_text)),
        bool(recommendation.get("target_personas")),
        bool(rec_trigger_codes <= trigger_codes),
        str(int(float(source_context.get("azure_annual_consumption") or 0))) in re.sub(r"[,.]", "", source_text),
        str(round(float(source_context.get("enablement_coverage_pct") or 0), 1)).split(".")[0] in source_text,
    ]
    grounded = sum(1 for x in grounded_checks if x) / len(grounded_checks)

    free_paid = recommendation.get("free_to_paid_opportunity") or {}
    cta = recommendation.get("cta_for_account_team", "")
    actionability_checks = [
        bool(recommendation.get("target_personas")),
        bool(re.search(r"\b(today|this week|before|within|q[1-4]|[0-9]{1,2}\s*(?:day|week|month))", cta, re.I)),
        bool(cta),
        bool(free_paid.get("pitch_narrative")),
    ]
    actionability = sum(1 for x in actionability_checks if x) / len(actionability_checks)

    failure_reasons = []
    if not sku_match:
        failure_reasons.append("SKU_MATCH_GATE_FAILED")
    if grounded < 0.85:
        failure_reasons.append("GROUNDING_GATE_FAILED")
    passed = not failure_reasons

    result = {
        "groundedness_score": round(grounded, 3),
        "specificity_score": round(specificity, 3),
        "actionability_score": round(actionability, 3),
        "sku_match_gate": sku_match,
        "grounding_gate": grounded >= 0.85,
        "passed": passed,
        "failure_reasons": failure_reasons,
        "allowed_course_paths": allowed_tracks,
    }

    langsmith_run_id = _push_enablement_eval_to_langsmith(
        recommendation=recommendation,
        source_context=source_context,
        resource_estimate=resource_estimate,
        evaluation=result,
    )
    if langsmith_run_id:
        result["langsmith_run_id"] = langsmith_run_id

    if log:
        ensure_eval_table()
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                """INSERT INTO enablement_eval_results
                   (company_slug, deal_slug, groundedness_score, specificity_score,
                    actionability_score, passed, failure_reasons, evaluated_at,
                    langsmith_run_id, full_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_context.get("company_slug"),
                    source_context.get("deal_slug"),
                    result["groundedness_score"],
                    result["specificity_score"],
                    result["actionability_score"],
                    passed,
                    ",".join(failure_reasons),
                    datetime.now().isoformat(),
                    langsmith_run_id,
                    json.dumps({"recommendation": recommendation, "eval": result}),
                ),
            )
    return result


def _push_enablement_eval_to_langsmith(
    recommendation: dict[str, Any],
    source_context: dict[str, Any],
    resource_estimate: dict[str, Any],
    evaluation: dict[str, Any],
) -> str:
    """Push one enablement eval run to LangSmith when configured.

    This is intentionally non-fatal: local evals and SQLite logging should still
    work without LangSmith credentials or network access.
    """
    api_key = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")
    if not api_key:
        return ""
    try:
        from langsmith import Client as LSClient

        os.environ.setdefault("LANGSMITH_API_KEY", api_key)
        client = LSClient()
        run_id = str(uuid.uuid4())
        now = datetime.now()
        company = source_context.get("company") or source_context.get("company_slug") or "unknown"
        deal = source_context.get("deal_name") or source_context.get("deal_slug") or "unknown"
        client.create_run(
            name=f"enablement eval — {company} / {deal}",
            run_type="chain",
            inputs={
                "enablement_record": source_context,
                "resource_estimate": resource_estimate,
            },
            outputs={
                "recommendation": recommendation,
                "evaluation": evaluation,
            },
            project_name=os.environ.get("LANGSMITH_ENABLEMENT_PROJECT", LANGSMITH_PROJECT),
            run_id=run_id,
            start_time=now,
            end_time=now,
        )
        feedback = {
            "groundedness_score": evaluation["groundedness_score"],
            "specificity_score": evaluation["specificity_score"],
            "actionability_score": evaluation["actionability_score"],
            "sku_match_gate": 1 if evaluation["sku_match_gate"] else 0,
            "grounding_gate": 1 if evaluation["grounding_gate"] else 0,
            "passed": 1 if evaluation["passed"] else 0,
        }
        for key, score in feedback.items():
            client.create_feedback(run_id=run_id, key=key, score=score)
        return run_id
    except Exception as exc:
        print(f"[enablement_eval] LangSmith push failed: {exc}")
        return ""
