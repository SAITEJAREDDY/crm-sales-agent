"""Deterministic commercial policy and approval engine.

Phase 4 evaluates canonical DealInput + CommercialFinancialAnalysis against
synthetic configurable policy rules. It does not call an LLM, send approvals,
or implement negotiation strategy.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from tools.commercial_data import get_approval_matrix, get_deal_input
    from tools.commercial_financial_analysis import evaluate_financial_scenario
except ModuleNotFoundError:
    from commercial_data import get_approval_matrix, get_deal_input
    from commercial_financial_analysis import evaluate_financial_scenario


BASE_DIR = Path(__file__).resolve().parent.parent
COMMERCIAL_DIR = BASE_DIR / "knowledge_base" / "commercial_strategy"
POLICY_RULES_PATH = COMMERCIAL_DIR / "commercial_policy_rules.json"
APPROVAL_HIERARCHY_PATH = COMMERCIAL_DIR / "commercial_approval_hierarchy.json"
POLICY_SCHEMA_PATH = COMMERCIAL_DIR / "policy_evaluation_schema.json"

STATUS_ORDER = {"PASS": 0, "NOT_APPLICABLE": 0, "NOT_MODELED": 0, "REVIEW": 1, "EXCEPTION": 2, "BLOCK": 3}
SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


class CommercialPolicyError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _payment_days(payment_terms: str | None) -> int:
    if not payment_terms:
        return 30
    matches = [int(m) for m in re.findall(r"Net\s*(\d+)", payment_terms, flags=re.I)]
    return max(matches) if matches else 30


def _price_protection_months(deal_input: dict, scenario_overrides: dict | None) -> int:
    text = ""
    if scenario_overrides and scenario_overrides.get("price_protection"):
        text = str(scenario_overrides["price_protection"])
    else:
        ask = deal_input.get("negotiation_context", {}).get("customer_requested_commercial_terms", {})
        text = str(ask.get("requested_price_protection") or "")
    if re.search(r"36|three", text, flags=re.I):
        return 36
    if re.search(r"24|two", text, flags=re.I):
        return 24
    if re.search(r"12|one", text, flags=re.I):
        return 12
    return 0


def _scenario_name(scenario_overrides: dict | None) -> str:
    return (scenario_overrides or {}).get("name", "baseline")


def _actuals(deal_input: dict, financial: dict, scenario_overrides: dict | None) -> dict:
    baseline = financial["baseline"]
    commitment = financial["commitment"]
    payment_terms = (scenario_overrides or {}).get("payment_terms") or deal_input.get("commercial", {}).get("payment_terms")
    return {
        "discount_pct": baseline.get("discount_pct"),
        "tcv": baseline.get("tcv"),
        "acv": baseline.get("acv"),
        "contract_term_months": baseline.get("contract_term_months"),
        "payment_terms_days": _payment_days(payment_terms),
        "coverage_ratio": commitment.get("coverage_ratio"),
        "npv": baseline.get("npv"),
        "gross_margin_pct": baseline.get("gross_margin_pct"),
        "price_protection_months": _price_protection_months(deal_input, scenario_overrides),
    }


def _compare(value: Any, rule: dict) -> bool:
    op = rule["operator"]
    threshold = rule.get("threshold")
    upper = rule.get("upper_bound")
    if op == "is_null":
        return value is None
    if value is None:
        return False
    value = float(value)
    if op == "<=":
        return value <= float(threshold)
    if op == ">=":
        return value >= float(threshold)
    if op == ">":
        if not value > float(threshold):
            return False
        return upper is None or value <= float(upper)
    if op == "<":
        return value < float(threshold)
    if op == "between":
        return float(threshold) <= value <= float(upper)
    raise CommercialPolicyError("UNKNOWN_POLICY_OPERATOR", f"Unsupported operator {op}")


def _fmt(value: Any, unit: str | None) -> str:
    if value is None:
        return "Not modeled"
    if unit == "usd":
        return f"{round(float(value)):,}"
    if unit == "ratio":
        return f"{float(value):.2f}"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _message(rule: dict, actual_value: Any) -> str:
    return rule.get("message_template", "{policy_id}: {actual_value}").format(
        actual_value=_fmt(actual_value, rule.get("unit")),
        threshold=_fmt(rule.get("threshold"), rule.get("unit")),
    )


def _policy_check(rule: dict, actual_value: Any) -> dict:
    return {
        "rule_id": rule["rule_id"],
        "policy_id": rule["policy_id"],
        "category": rule["category"],
        "status": rule["status"],
        "severity": rule["severity"],
        "actual_value": actual_value,
        "threshold": rule.get("threshold"),
        "upper_bound": rule.get("upper_bound"),
        "unit": rule.get("unit"),
        "message": _message(rule, actual_value),
        "remediation": rule.get("remediation"),
        "source_configuration": str(POLICY_RULES_PATH),
    }


def _pass_checks_for_missing_categories(matched: list[dict], actuals: dict) -> list[dict]:
    categories = {c["category"] for c in matched}
    checks = []
    if "Payment Terms" not in categories:
        checks.append({
            "rule_id": "PAYMENT_STANDARD",
            "policy_id": "PAYMENT_TERMS",
            "category": "Payment Terms",
            "status": "PASS",
            "severity": "LOW",
            "actual_value": actuals["payment_terms_days"],
            "threshold": 45,
            "unit": "days",
            "message": f"Payment terms are Net {actuals['payment_terms_days']}, within standard Net 45.",
            "remediation": None,
            "source_configuration": str(POLICY_RULES_PATH),
        })
    if "Commitment" not in categories and actuals.get("coverage_ratio") is not None:
        checks.append({
            "rule_id": "COMMITMENT_COVERAGE_PASS",
            "policy_id": "CONSUMPTION_COMMITMENT",
            "category": "Commitment",
            "status": "PASS",
            "severity": "LOW",
            "actual_value": actuals["coverage_ratio"],
            "threshold": 1,
            "unit": "ratio",
            "message": f"Expected consumption covers {actuals['coverage_ratio']:.2f}x of commitment.",
            "remediation": None,
            "source_configuration": str(POLICY_RULES_PATH),
        })
    if "Legal Terms" not in categories:
        checks.append({
            "rule_id": "LEGAL_STANDARD",
            "policy_id": "LEGAL_TERMS",
            "category": "Legal Terms",
            "status": "PASS",
            "severity": "LOW",
            "actual_value": actuals["price_protection_months"],
            "threshold": 36,
            "unit": "months",
            "message": "No non-standard legal term detected by the deterministic policy engine.",
            "remediation": None,
            "source_configuration": str(POLICY_RULES_PATH),
        })
    return checks


def _exceptions(checks: list[dict], rules_by_id: dict) -> list[dict]:
    out = []
    for check in checks:
        if check["status"] not in {"REVIEW", "EXCEPTION", "BLOCK"}:
            continue
        rule = rules_by_id.get(check["rule_id"], {})
        out.append({
            "exception_type": rule.get("exception_type", f"{check['category'].upper().replace(' ', '_')}_EXCEPTION"),
            "triggering_policy": check["policy_id"],
            "rule_id": check["rule_id"],
            "reason": check["message"],
            "actual_value": check["actual_value"],
            "threshold": check["threshold"],
            "required_approver": _approver_from_rule(rule),
            "documentation_required": bool(rule.get("documentation_required", False)),
            "resolution_status": "open",
            "remediation": check.get("remediation"),
        })
    return out


def _approver_from_rule(rule: dict) -> dict | None:
    if not rule.get("approver_function"):
        return None
    return {
        "function": rule["approver_function"],
        "level": rule.get("approver_level", ""),
        "priority": rule.get("approval_priority", 99),
    }


def _required_approvals(checks: list[dict], rules_by_id: dict) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for check in checks:
        if check["status"] not in {"REVIEW", "EXCEPTION", "BLOCK"}:
            continue
        rule = rules_by_id.get(check["rule_id"], {})
        approver = _approver_from_rule(rule)
        if not approver:
            continue
        key = (approver["function"], approver["level"])
        row = grouped.setdefault(key, {
            "function": approver["function"],
            "level": approver["level"],
            "priority": approver["priority"],
            "required": True,
            "reasons": [],
            "source_rules": [],
        })
        row["reasons"].append(check["message"])
        row["source_rules"].append(check["rule_id"])
        row["priority"] = min(row["priority"], approver["priority"])
    return sorted(grouped.values(), key=lambda r: r["priority"])


def _overall_status(checks: list[dict]) -> str:
    statuses = {c["status"] for c in checks}
    if "BLOCK" in statuses:
        return "BLOCKED"
    if "EXCEPTION" in statuses:
        return "REQUIRES_APPROVAL"
    if "REVIEW" in statuses:
        return "REQUIRES_REVIEW"
    return "WITHIN_POLICY"


def _summary(checks: list[dict]) -> dict:
    counts = Counter(c["status"] for c in checks)
    return {
        "passed": counts.get("PASS", 0),
        "reviews": counts.get("REVIEW", 0),
        "exceptions": counts.get("EXCEPTION", 0),
        "blocks": counts.get("BLOCK", 0),
        "not_modeled": counts.get("NOT_MODELED", 0),
    }


def evaluate_commercial_policy(deal_id: str, scenario_overrides: dict | None = None) -> dict:
    deal_input = get_deal_input(deal_id)
    if not deal_input:
        raise CommercialPolicyError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")
    financial = evaluate_financial_scenario(deal_id, scenario_overrides or {"name": "baseline"})
    config = _load_json(POLICY_RULES_PATH)
    rules = config["rules"]
    rules_by_id = {r["rule_id"]: r for r in rules}
    actuals = _actuals(deal_input, financial, scenario_overrides)
    matched = []
    trace = []
    for rule in rules:
        value = actuals.get(rule["input_field"])
        result = _compare(value, rule)
        trace.append({
            "rule_id": rule["rule_id"],
            "policy_id": rule["policy_id"],
            "input_field": rule["input_field"],
            "actual_value": value,
            "condition": _condition_text(rule),
            "matched": result,
            "configured_status": rule["status"],
            "approver_mapping": _approver_from_rule(rule),
            "source_configuration": str(POLICY_RULES_PATH),
        })
        if result:
            # For tiered duplicate pricing/deal-size rules, keep all triggered approvals
            # except PASS rules when a higher-severity same-policy rule matched.
            matched.append(_policy_check(rule, value))
    matched = _dedupe_passes(matched)
    checks = sorted(matched + _pass_checks_for_missing_categories(matched, actuals), key=lambda c: (c["category"], -STATUS_ORDER[c["status"]], c["rule_id"]))
    approvals = _required_approvals(checks, rules_by_id)
    return {
        "deal_id": deal_id,
        "scenario": financial["scenario"],
        "overall_status": _overall_status(checks),
        "summary": _summary(checks),
        "policy_checks": checks,
        "required_approvals": approvals,
        "approval_sequence": [{"step": i + 1, **a} for i, a in enumerate(approvals)],
        "exceptions": _exceptions(checks, rules_by_id),
        "financial_flags": financial.get("flags", []),
        "synthetic_policy_disclaimer": config["metadata"]["disclaimer"],
        "trace": {
            "inputs": actuals,
            "rules_evaluated": trace,
            "approval_matrix_source": str(APPROVAL_HIERARCHY_PATH),
            "legacy_approval_matrix_available": bool(get_approval_matrix()),
        },
    }


def _dedupe_passes(checks: list[dict]) -> list[dict]:
    by_policy = {}
    for check in checks:
        cur = by_policy.get(check["policy_id"])
        if not cur or STATUS_ORDER[check["status"]] >= STATUS_ORDER[cur["status"]]:
            by_policy[check["policy_id"]] = check
    return list(by_policy.values())


def _condition_text(rule: dict) -> str:
    op = rule["operator"]
    field = rule["input_field"]
    if op == "between":
        return f"{field} between {rule.get('threshold')} and {rule.get('upper_bound')}"
    if op == "is_null":
        return f"{field} is null"
    upper = f" and <= {rule['upper_bound']}" if rule.get("upper_bound") is not None and op == ">" else ""
    return f"{field} {op} {rule.get('threshold')}{upper}"


def compare_scenario_policy(deal_id: str, scenarios: list[dict]) -> dict:
    evaluations = [evaluate_commercial_policy(deal_id, s) for s in scenarios]
    return {
        "deal_id": deal_id,
        "scenario_policy_comparison": [
            {
                "scenario": e["scenario"],
                "overall_status": e["overall_status"],
                "required_approval_count": len(e["required_approvals"]),
                "exception_count": len(e["exceptions"]),
                "summary": e["summary"],
                "approval_sequence": e["approval_sequence"],
            }
            for e in evaluations
        ],
    }


def get_policy_trace(deal_id: str, scenario_overrides: dict | None = None) -> dict:
    return evaluate_commercial_policy(deal_id, scenario_overrides).get("trace", {})


def write_policy_schema(path: Path = POLICY_SCHEMA_PATH) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "PolicyEvaluation",
        "type": "object",
        "required": ["deal_id", "scenario", "overall_status", "policy_checks", "required_approvals", "exceptions"],
        "properties": {
            "deal_id": {"type": "string"},
            "scenario": {"type": "string"},
            "overall_status": {"enum": ["WITHIN_POLICY", "REQUIRES_REVIEW", "REQUIRES_APPROVAL", "BLOCKED"]},
            "summary": {"type": "object"},
            "policy_checks": {"type": "array"},
            "required_approvals": {"type": "array"},
            "approval_sequence": {"type": "array"},
            "exceptions": {"type": "array"},
            "financial_flags": {"type": "array"},
            "trace": {"type": "object"}
        }
    }
    path.write_text(json.dumps(schema, indent=2) + "\n")


