"""Financial engine adapter for canonical commercial DealInput records.

Phase 2 connects tools.commercial_data.get_deal_input() to the existing
deterministic financial model in tools.financial_model. This module is a thin
schema translation and orchestration layer; it does not create a second
financial model and it does not call an LLM.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

try:
    from tools.commercial_data import get_commercial_guardrails, get_deal_input
    from tools.financial_model import compute as compute_financial_model
except ModuleNotFoundError:
    from commercial_data import get_commercial_guardrails, get_deal_input
    from financial_model import compute as compute_financial_model


MINIMUM_MARGIN_PCT = None
MAXIMUM_STANDARD_DISCOUNT_PCT = 18.0
MAXIMUM_EXCEPTION_DISCOUNT_PCT = 25.0


class CommercialFinancialAnalysisError(ValueError):
    """Raised when a DealInput cannot be evaluated by the adapter."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _input(value: float, confidence: str = "stated", source: str = "deal_input", quote: str = "") -> dict:
    return {
        "value": value,
        "confidence_level": confidence,
        "source": source,
        "quote": quote or str(value),
    }


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _as_float(value: Any, field: str) -> float:
    if value is None:
        raise CommercialFinancialAnalysisError(f"MISSING_{field.upper()}", f"{field} is required.")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise CommercialFinancialAnalysisError(f"INVALID_{field.upper()}", f"{field} must be numeric.") from exc


def _validate_discount(discount_pct: float) -> None:
    if discount_pct < 0 or discount_pct > 100:
        raise CommercialFinancialAnalysisError("INVALID_DISCOUNT", "Discount must be between 0 and 100 percent.")


def _term_years(contract_term_months: int) -> float:
    if contract_term_months <= 0:
        raise CommercialFinancialAnalysisError("MISSING_CONTRACT_TERM", "Contract term must be positive.")
    return contract_term_months / 12.0


def _load_guardrail_thresholds() -> dict:
    thresholds = {
        "maximum_standard_discount_pct": MAXIMUM_STANDARD_DISCOUNT_PCT,
        "maximum_exception_discount_pct": MAXIMUM_EXCEPTION_DISCOUNT_PCT,
        "minimum_margin_pct": MINIMUM_MARGIN_PCT,
    }
    try:
        guardrails = get_commercial_guardrails().get("guardrails", [])
    except Exception:
        return thresholds
    for guardrail in guardrails:
        if guardrail.get("id") == "GR-DISCOUNT":
            text = f"{guardrail.get('negotiable_range', '')} {guardrail.get('escalation_condition', '')}"
            nums = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)%", text)]
            if nums:
                thresholds["maximum_standard_discount_pct"] = max(n for n in nums if n <= 25) if any(n <= 25 for n in nums) else thresholds["maximum_standard_discount_pct"]
                thresholds["maximum_exception_discount_pct"] = max(nums)
    return thresholds


def _scenario_from_deal_input(deal_input: dict, scenario_overrides: dict | None = None) -> dict:
    overrides = scenario_overrides or {}
    commercial = copy.deepcopy(deal_input.get("commercial") or {})
    commitment = copy.deepcopy(deal_input.get("consumption_commitment") or {})
    products = copy.deepcopy(deal_input.get("products") or [])

    if not products:
        raise CommercialFinancialAnalysisError("MISSING_PRODUCT_PRICING", "DealInput must include at least one product line.")

    list_value = _as_float(overrides.get("list_value", commercial.get("list_value")), "list_value")
    discount_pct = _as_float(
        overrides.get("discount_pct", overrides.get("proposed_discount_pct", commercial.get("proposed_discount_pct"))),
        "discount",
    )
    _validate_discount(discount_pct)

    term_months = int(overrides.get("contract_term_months", commercial.get("contract_term_months") or 0))
    years = _term_years(term_months)
    net_contract_value = list_value * (1 - discount_pct / 100.0)
    acv = net_contract_value / years
    arr = acv

    if "commitment_amount" in overrides:
        commitment["commitment_amount"] = overrides["commitment_amount"]
    if float(commitment.get("commitment_amount") or 0) < 0:
        raise CommercialFinancialAnalysisError("INVALID_COMMITMENT_AMOUNT", "Commitment amount cannot be negative.")

    for field in ["billing_frequency", "payment_terms"]:
        if field in overrides:
            commercial[field] = overrides[field]
    if "credits" in overrides:
        commercial["credits"] = overrides["credits"]

    return {
        "name": overrides.get("name", "baseline"),
        "list_value": list_value,
        "discount_pct": discount_pct,
        "net_contract_value": net_contract_value,
        "contract_term_months": term_months,
        "acv": acv,
        "arr": arr,
        "tcv": net_contract_value,
        "billing_frequency": commercial.get("billing_frequency"),
        "payment_terms": commercial.get("payment_terms"),
        "commitment": commitment,
        "products": products,
        "credits": float(overrides.get("credits", commercial.get("credits") or 0) or 0),
    }


def _to_existing_financial_model_input(deal_input: dict, scenario: dict) -> dict:
    account = deal_input["account"]
    opportunity = deal_input["opportunity"]
    product = (scenario["products"][0] or {}).get("product") or deal_input.get("financial_model_inputs", {}).get("product") or opportunity["name"]
    arr = float(scenario["arr"])
    growth = 0.12 if account.get("strategic_account") else 0.08
    headcount = max(5, min(500, int(arr / 50_000)))
    return {
        "deal_id": deal_input["deal_id"],
        "deal_name": opportunity["name"],
        "company": account["name"],
        "product": product,
        "assigned_rep": opportunity.get("assigned_rep", ""),
        "overall_score": opportunity.get("meddpicc_score", "NEEDS ATTENTION"),
        "deal_amount": arr,
        "confidence_level": "HIGH",
        "confidence_score": 85,
        "extraction_triggered": False,
        "inputs": {
            "pain_usd_annual": _input(arr * 2.5, "inferred", "deal_input_adapter", "benchmark: ARR x 2.5"),
            "hours_lost_per_week": _input(0, "assumed", "deal_input_adapter"),
            "headcount_affected": _input(headcount, "inferred", "deal_input_adapter"),
            "error_rate_pct": _input(0.02, "assumed", "deal_input_adapter"),
            "transactions_per_month": _input(max(10_000, arr / 25), "inferred", "deal_input_adapter"),
            "current_vendor_cost_usd": _input(arr * 0.25, "inferred", "deal_input_adapter"),
            "growth_rate_pct": _input(growth, "inferred", "deal_input_adapter"),
            "budget_usd": _input(arr, "stated", "deal_input.commercial.arr"),
        },
        "rates": {
            "loaded_hourly": 85.0,
            "sa_day_rate": 2_500.0,
            "proserv_day_rate": 3_000.0,
            "partner_day_rate": 1_800.0,
        },
        "resources": _resources_for_amount(arr),
        "metadata": {
            "source": "commercial_financial_analysis_adapter",
            "scenario_name": scenario["name"],
            "deal_input_id": deal_input["deal_id"],
        },
    }


def _resources_for_amount(amount: float) -> dict:
    if amount < 100_000:
        return {"sa_days": 5, "proserv_days": 10, "partner_days": 5, "timeline_weeks": 6}
    if amount <= 500_000:
        return {"sa_days": 15, "proserv_days": 30, "partner_days": 15, "timeline_weeks": 12}
    return {"sa_days": 30, "proserv_days": 60, "partner_days": 30, "timeline_weeks": 20}


def _normalized_commitment_targets(commitment: dict, amount: float) -> tuple[list[float], str]:
    stated_targets = [
        float(commitment.get("year_1_target") or 0),
        float(commitment.get("year_2_target") or 0),
        float(commitment.get("year_3_target") or 0),
    ]
    if not amount:
        return stated_targets, "stated"

    stated_total = sum(stated_targets)
    target_reconciles = abs(stated_total - amount) <= max(1.0, amount * 0.02)
    year_1_share = stated_targets[0] / amount if amount else 0.0
    later_years_present = stated_targets[1] > 0 and stated_targets[2] > 0

    if target_reconciles and later_years_present and year_1_share <= 0.60:
        return stated_targets, "stated"

    return [amount * 0.30, amount * 0.35, amount * 0.35], "normalized_30_35_35"


def _expected_consumption_curve(commitment: dict, model: dict) -> tuple[list[float], str]:
    current_expected = float(commitment.get("current_expected_consumption") or 0)
    growth_rate = float(model.get("y2_growth_rate") or 0)
    model_expected = [
        float(model.get("y1_consumption") or 0),
        float(model.get("y2_consumption") or 0),
        float(model.get("y3_consumption") or 0),
    ]
    if current_expected > 0:
        y1 = current_expected
        y2 = y1 * (1 + growth_rate)
        y3 = y2 * (1 + growth_rate)
        return [y1, y2, y3], "current_expected_consumption"
    return model_expected, "financial_model_arr"


def _commitment_economics(scenario: dict, model: dict) -> dict:
    commitment = scenario["commitment"]
    amount = float(commitment.get("commitment_amount") or 0)
    annual_targets, target_basis = _normalized_commitment_targets(commitment, amount)
    expected_annual, expected_basis = _expected_consumption_curve(commitment, model)
    expected = sum(expected_annual)
    coverage = expected / amount if amount else None
    return {
        "commitment_amount": amount,
        "expected_consumption": expected,
        "coverage_ratio": coverage,
        "expected_overage": max(0.0, expected - amount),
        "expected_shortfall": max(0.0, amount - expected),
        "annual_targets": annual_targets,
        "expected_annual_consumption": expected_annual,
        "attainment_pct": (expected / amount * 100) if amount else None,
        "target_schedule_basis": target_basis,
        "expected_consumption_basis": expected_basis,
        "schema_note": "Synthetic enterprise-commercial commitment normalization; not an internal MACC financial model.",
    }


def _flags(scenario: dict, model: dict, commitment: dict, thresholds: dict) -> list[str]:
    flags = []
    discount = float(scenario["discount_pct"])
    if discount > thresholds["maximum_standard_discount_pct"]:
        flags.append("DISCOUNT_ABOVE_STANDARD")
    if discount > thresholds["maximum_exception_discount_pct"]:
        flags.append("DISCOUNT_ABOVE_EXCEPTION_GUIDANCE")
    if model.get("npv_10pct", 0) < 0:
        flags.append("NEGATIVE_NPV")
    if commitment.get("coverage_ratio") is not None and commitment["coverage_ratio"] < 1:
        flags.append("COMMITMENT_UNDER_COVERED")
    if scenario.get("credits", 0) > scenario["net_contract_value"] * 0.10:
        flags.append("CREDITS_ABOVE_STANDARD")
    return flags


def _metric_trace(metric: str, value: Any, source: str, inputs: dict) -> dict:
    return {
        "metric": metric,
        "value": value,
        "calculation_source": source,
        "inputs": inputs,
    }


def evaluate_financial_scenario(deal_id: str, scenario_overrides: dict | None = None) -> dict:
    deal_input = get_deal_input(deal_id)
    if not deal_input:
        raise CommercialFinancialAnalysisError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")
    return evaluate_financial_scenario_for_deal_input(deal_input, scenario_overrides)


def evaluate_financial_scenario_for_deal_input(deal_input: dict, scenario_overrides: dict | None = None) -> dict:
    scenario = _scenario_from_deal_input(deal_input, scenario_overrides)
    financial_input = _to_existing_financial_model_input(deal_input, scenario)
    try:
        model = compute_financial_model(financial_input)
    except Exception as exc:
        raise CommercialFinancialAnalysisError("FINANCIAL_MODEL_EXECUTION_ERROR", str(exc)) from exc

    thresholds = _load_guardrail_thresholds()
    commitment = _commitment_economics(scenario, model)
    flags = _flags(scenario, model, commitment, thresholds)
    result = {
        "scenario": scenario["name"],
        "deal_id": deal_input["deal_id"],
        "currency": deal_input.get("commercial", {}).get("currency", "USD"),
        "baseline": {
            "list_value": round(scenario["list_value"]),
            "discount_pct": round(scenario["discount_pct"], 2),
            "net_contract_value": round(scenario["net_contract_value"]),
            "contract_term_months": scenario["contract_term_months"],
            "acv": round(scenario["acv"]),
            "arr": round(scenario["arr"]),
            "tcv": round(scenario["tcv"]),
            "gross_margin_pct": None,
            "gross_profit": None,
            "npv": round(model["npv_10pct"]),
            "roi_3yr_pct": round(model["roi_3yr_pct"], 2),
            "payback_months": round(model["payback_months"], 2),
        },
        "model_outputs": {
            "source": "tools.financial_model.compute",
            "net_3yr_usd": round(model["cumulative_3yr"]),
            "year1_net_usd": round(model["net_y1"]),
            "total_impl_cost": round(model["total_impl"]),
            "y1_consumption": round(model["y1_consumption"]),
            "y2_consumption": round(model["y2_consumption"]),
            "y3_consumption": round(model["y3_consumption"]),
            "edp_rate_pct": round(model["edp_rate"] * 100, 2),
            "net_annual_price_after_existing_tier": round(model["net_price"]),
            "three_year_commit_option_total": round(model["threeyear_3yr_total"]),
            "confidence_bands": model["bands"],
        },
        "commitment": commitment,
        "thresholds": thresholds,
        "flags": flags,
        "trace": {
            "adapter_input": {
                "scenario_overrides": scenario_overrides or {},
                "financial_model_deal_amount": financial_input["deal_amount"],
                "financial_model_confidence_level": financial_input["confidence_level"],
            },
            "metrics": [
                _metric_trace("net_contract_value", round(scenario["net_contract_value"]), "adapter_schema_translation", {"list_value": scenario["list_value"], "discount_pct": scenario["discount_pct"]}),
                _metric_trace("acv", round(scenario["acv"]), "adapter_schema_translation", {"net_contract_value": scenario["net_contract_value"], "contract_term_months": scenario["contract_term_months"]}),
                _metric_trace("npv", round(model["npv_10pct"]), "existing_financial_model", {"function": "tools.financial_model.compute", "cash_flow_model": "net_y1/net_y2/net_y3"}),
                _metric_trace("roi_3yr_pct", round(model["roi_3yr_pct"], 2), "existing_financial_model", {"function": "tools.financial_model.compute"}),
                _metric_trace("payback_months", round(model["payback_months"], 2), "existing_financial_model", {"function": "tools.financial_model.compute"}),
            ],
            "integration_gaps": [
                "Existing financial model does not calculate gross margin or gross profit; fields are returned as null.",
                "Discount scenario translation happens in adapter because existing model accepts net annual deal amount, not list price plus discount.",
            ],
        },
    }
    return result


def default_working_discount_pct(deal_input: dict) -> float:
    """Seller's working counter: midpoint of current offer and customer ask.

    Never exceeds the customer ask — a working scenario must not concede more
    discount than the customer requested.
    """
    commercial = deal_input.get("commercial", {})
    ask = float(commercial.get("requested_discount_pct") or 0)
    offer = float(commercial.get("proposed_discount_pct") or 0)
    if ask and offer:
        return round(min(ask, (ask + offer) / 2), 1)
    return round(ask or offer or 22.0, 1)


def commercial_floor_discount_pct(deal_input: dict) -> float:
    """Guardrail stress test: the worst discount plausibly on the table —
    the exception ceiling, or the customer ask when it exceeds the ceiling."""
    ask = float(deal_input.get("commercial", {}).get("requested_discount_pct") or 0)
    exception_ceiling = _load_guardrail_thresholds()["maximum_exception_discount_pct"]
    return round(max(ask, exception_ceiling), 1)


def _default_named_scenarios(deal_input: dict) -> list[dict]:
    commercial = deal_input["commercial"]
    ask = deal_input.get("negotiation_context", {}).get("customer_requested_commercial_terms", {})
    commitment = deal_input.get("consumption_commitment", {})
    term = commercial.get("contract_term_months") or 36
    return [
        {
            "name": "customer_ask",
            "discount_pct": commercial.get("requested_discount_pct"),
            "contract_term_months": ask.get("requested_agreement_term_months", commercial.get("contract_term_months")),
            "commitment_amount": ask.get("requested_total_commitment", commitment.get("commitment_amount")),
            "billing_frequency": commercial.get("billing_frequency"),
        },
        {
            "name": "recommended_test",
            "discount_pct": default_working_discount_pct(deal_input),
            "contract_term_months": term,
            "commitment_amount": commitment.get("commitment_amount"),
            "billing_frequency": commercial.get("billing_frequency"),
        },
        {
            "name": "commercial_floor_test",
            "discount_pct": commercial_floor_discount_pct(deal_input),
            "contract_term_months": term,
            "commitment_amount": commitment.get("commitment_amount"),
            "billing_frequency": commercial.get("billing_frequency"),
        },
    ]


def get_commercial_financial_analysis(deal_id: str, scenarios: list[dict] | None = None) -> dict:
    deal_input = get_deal_input(deal_id)
    if not deal_input:
        raise CommercialFinancialAnalysisError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")
    baseline = evaluate_financial_scenario_for_deal_input(deal_input, {"name": "baseline"})
    scenario_inputs = scenarios or _default_named_scenarios(deal_input)
    scenario_results = {
        s.get("name", f"scenario_{i + 1}"): evaluate_financial_scenario_for_deal_input(deal_input, s)
        for i, s in enumerate(scenario_inputs)
    }
    return {
        "deal_id": deal_id,
        "currency": baseline["currency"],
        "baseline": baseline["baseline"],
        "scenarios": scenario_results,
        "commitment": baseline["commitment"],
        "thresholds": baseline["thresholds"],
        "flags": sorted(set(baseline["flags"] + [f for r in scenario_results.values() for f in r["flags"]])),
        "trace": {
            "architecture": "get_deal_input -> tools.commercial_financial_analysis adapter -> tools.financial_model.compute -> CommercialFinancialAnalysis",
            "baseline_trace": baseline["trace"],
        },
    }


def compare_financial_scenarios(deal_id: str, scenarios: list[dict]) -> dict:
    results = [evaluate_financial_scenario(deal_id, s) for s in scenarios]
    return {
        "deal_id": deal_id,
        "scenario_comparison": [
            {
                "scenario": r["scenario"],
                "discount_pct": r["baseline"]["discount_pct"],
                "tcv": r["baseline"]["tcv"],
                "arr": r["baseline"]["arr"],
                "acv": r["baseline"]["acv"],
                "gross_margin_pct": r["baseline"]["gross_margin_pct"],
                "npv": r["baseline"]["npv"],
                "roi_3yr_pct": r["baseline"]["roi_3yr_pct"],
                "payback_months": r["baseline"]["payback_months"],
                "commitment_coverage_ratio": r["commitment"]["coverage_ratio"],
                "flags": r["flags"],
            }
            for r in results
        ],
    }


def get_financial_metric_trace(deal_id: str, scenario_name: str | None = None) -> dict:
    analysis = get_commercial_financial_analysis(deal_id)
    if scenario_name:
        scenario = analysis["scenarios"].get(scenario_name)
        if not scenario:
            raise CommercialFinancialAnalysisError("MISSING_SCENARIO", f"No scenario named {scenario_name}.")
        return scenario["trace"]
    return analysis["trace"]


def write_schema(path: Path) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "CommercialFinancialAnalysis",
        "type": "object",
        "required": ["deal_id", "currency", "baseline", "scenarios", "commitment", "thresholds", "flags"],
        "properties": {
            "deal_id": {"type": "string"},
            "currency": {"type": "string"},
            "baseline": {"type": "object"},
            "scenarios": {"type": "object"},
            "commitment": {"type": "object"},
            "thresholds": {"type": "object"},
            "flags": {"type": "array", "items": {"type": "string"}},
            "trace": {"type": "object"},
        },
    }
    path.write_text(json.dumps(schema, indent=2) + "\n")


