"""Commercial commitment and customer ROI intelligence for consumption analysis."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

try:
    from tools.commercial_data import get_deal_input
    from tools.consumption_forecast import get_forecast
except ModuleNotFoundError:
    from commercial_data import get_deal_input
    from consumption_forecast import get_forecast


BASE_DIR = Path(__file__).resolve().parent.parent
COMMERCIAL_DIR = BASE_DIR / "knowledge_base" / "commercial_strategy"
BUSINESS_CASE_ASSUMPTIONS_PATH = COMMERCIAL_DIR / "business_case_assumptions.json"
COMMERCIAL_INTELLIGENCE_PATH = COMMERCIAL_DIR / "consumption_commercial_intelligence.json"
BUSINESS_CASES_PATH = COMMERCIAL_DIR / "consumption_business_cases.json"
INTELLIGENCE_VERSION = "1.0"
SCENARIOS = ("CONSERVATIVE", "EXPECTED", "HIGH_GROWTH")
MAX_DISCOUNT_RATE = Decimal("0.50")
COMMITMENT_THRESHOLDS = [
    (Decimal("0.75"), "HIGH UNDER-CONSUMPTION RISK"),
    (Decimal("0.90"), "MODERATE UNDER-CONSUMPTION RISK"),
    (Decimal("1.05"), "WELL ALIGNED"),
    (Decimal("1.20"), "EXPANSION LIKELY"),
    (None, "HIGH EXPANSION / CAPACITY REVIEW"),
]
DEFAULT_BUSINESS_CASE_ASSUMPTIONS = {}  # Supply operator-owned inputs at runtime.


class ConsumptionCommercialIntelligenceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return deepcopy(fallback)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return deepcopy(fallback)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _money(value: Decimal | int | float | str) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _ratio(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _as_decimal(value: Any, field: str, allow_zero: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ConsumptionCommercialIntelligenceError("INVALID_INPUT", f"{field} must be numeric.") from exc
    if result < 0 or (result == 0 and not allow_zero):
        raise ConsumptionCommercialIntelligenceError("INVALID_INPUT", f"{field} must be greater than zero." if not allow_zero else f"{field} cannot be negative.")
    return result


def load_business_case_assumptions_store() -> dict:
    store = _load_json(BUSINESS_CASE_ASSUMPTIONS_PATH, {})
    merged = deepcopy(DEFAULT_BUSINESS_CASE_ASSUMPTIONS)
    merged.update(store)
    return merged


def save_business_case_assumptions(assumptions: dict) -> dict:
    validated = validate_business_case_assumptions(assumptions)
    store = _load_json(BUSINESS_CASE_ASSUMPTIONS_PATH, {})
    existing = store.get(validated["deal_id"], {})
    validated["assumption_source"] = "USER_INPUT"
    validated["provenance"] = "USER_INPUT"
    validated["updated_at"] = _utc_now()
    validated["created_at"] = existing.get("created_at", validated["updated_at"])
    store[validated["deal_id"]] = validated
    _write_json(BUSINESS_CASE_ASSUMPTIONS_PATH, store)
    invalidate_business_case(validated["deal_id"])
    return validated


def get_business_case_assumptions(deal_id: str) -> dict | None:
    assumptions = load_business_case_assumptions_store().get(deal_id)
    return deepcopy(assumptions) if assumptions else None


def validate_business_case_assumptions(assumptions: dict) -> dict:
    required = [
        "deal_id", "current_state_annual_infrastructure_cost", "current_state_annual_operations_cost",
        "current_state_annual_software_cost", "migration_cost", "implementation_cost",
        "training_change_management_cost", "annual_operations_savings", "annual_productivity_benefit",
        "discount_rate",
    ]
    for field in required:
        if field not in assumptions:
            raise ConsumptionCommercialIntelligenceError("MISSING_BUSINESS_CASE_ASSUMPTION", f"{field} is required.")
    validated = deepcopy(assumptions)
    money_fields = [f for f in required if f != "deal_id" and f != "discount_rate"]
    money_fields += ["other_transition_cost", "other_annual_benefit"]
    for field in money_fields:
        validated[field] = _money(_as_decimal(validated.get(field, 0), field))
    rate = _as_decimal(validated.get("discount_rate"), "discount_rate")
    if rate > MAX_DISCOUNT_RATE:
        raise ConsumptionCommercialIntelligenceError("INVALID_DISCOUNT_RATE", "discount_rate is above the configured maximum.")
    validated["discount_rate"] = _ratio(rate)
    validated.setdefault("currency", "USD")
    validated.setdefault("assumption_source", "synthetic_demo_data_phase8_6")
    validated.setdefault("provenance", "SYNTHETIC_DEMO")
    validated.setdefault("confidence", confidence_from_provenance(validated.get("provenance")))
    return validated


def confidence_from_provenance(provenance: str | None) -> str:
    if provenance == "USER_INPUT":
        return "HIGH"
    if provenance == "EXISTING_DATA":
        return "HIGH"
    if provenance == "MIXED":
        return "MEDIUM"
    return "LOW / DEMO"


def _require_current_forecast(deal_id: str) -> dict:
    forecast = get_forecast(deal_id)
    if not forecast:
        raise ConsumptionCommercialIntelligenceError("FORECAST_REQUIRED", "A current 36-month consumption forecast is required.")
    if forecast.get("forecast_status") == "STALE" or forecast.get("stale"):
        raise ConsumptionCommercialIntelligenceError("FORECAST_STALE", "Refresh the consumption forecast before calculating commercial intelligence.")
    return forecast


def _commitment_value(deal_input: dict) -> tuple[Decimal, str, str | None]:
    commitment = deal_input.get("consumption_commitment") or {}
    for field in ("azure_commitment_value", "cloud_consumption_commitment", "commitment_amount"):
        if commitment.get(field) is not None:
            return _as_decimal(commitment.get(field), field, allow_zero=False), field.upper(), None
    commercial = deal_input.get("commercial") or {}
    tcv = commercial.get("tcv", commercial.get("proposed_net_value"))
    if tcv is not None:
        return _as_decimal(tcv, "commercial.tcv", allow_zero=False), "TCV_FALLBACK", "Azure-specific commitment was unavailable; Deal TCV is being used as the demo commitment baseline."
    raise ConsumptionCommercialIntelligenceError("COMMITMENT_REQUIRED", "Azure commitment value is required for coverage analysis.")


def classify_commitment_coverage(coverage: Decimal) -> str:
    for upper, label in COMMITMENT_THRESHOLDS:
        if upper is None or coverage < upper:
            return label
    return COMMITMENT_THRESHOLDS[-1][1]


def calculate_commercial_intelligence(deal_id: str) -> dict:
    deal_input = get_deal_input(deal_id)
    if not deal_input:
        raise ConsumptionCommercialIntelligenceError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")
    forecast = _require_current_forecast(deal_id)
    currency = deal_input.get("commercial", {}).get("currency", "USD")
    if forecast.get("currency", "USD") != currency:
        raise ConsumptionCommercialIntelligenceError("CURRENCY_MISMATCH", "Forecast currency does not match the commercial deal currency.")
    commitment, source, warning = _commitment_value(deal_input)
    scenarios = {}
    for name in SCENARIOS:
        projected = _as_decimal(forecast["scenarios"][name]["total_36_month"], f"{name}.total_36_month")
        coverage = projected / commitment
        headroom = commitment - projected
        scenarios[name] = {
            "projected_consumption": _money(projected),
            "commitment_coverage": _ratio(coverage),
            "commitment_coverage_pct": _money(coverage * Decimal("100")),
            "headroom": _money(headroom),
            "expansion_potential": _money(max(projected - commitment, Decimal("0"))),
            "assessment": classify_commitment_coverage(coverage),
            "recommendation": commitment_recommendation(coverage),
        }
    result = {
        "deal_id": deal_id,
        "analysis_version": INTELLIGENCE_VERSION,
        "calculated_at": _utc_now(),
        "status": "CURRENT",
        "commitment_value": _money(commitment),
        "commitment_source": source,
        "commitment_warning": warning,
        "currency": currency,
        "default_scenario": "EXPECTED",
        "scenarios": scenarios,
        "dependency_signature": _dependency_signature(deal_input, forecast, None),
    }
    save_commercial_intelligence(result)
    return result


def commitment_recommendation(coverage: Decimal) -> str:
    label = classify_commitment_coverage(coverage)
    return {
        "HIGH UNDER-CONSUMPTION RISK": "Projected consumption is materially below commitment. Review commitment sizing.",
        "MODERATE UNDER-CONSUMPTION RISK": "Projected consumption is below commitment. Validate adoption and ramp assumptions.",
        "WELL ALIGNED": "Commitment appears well aligned with projected consumption.",
        "EXPANSION LIKELY": "Projected consumption exceeds commitment, indicating potential expansion capacity.",
        "HIGH EXPANSION / CAPACITY REVIEW": "Projected consumption materially exceeds commitment. Review capacity and expansion motion.",
    }[label]


def calculate_business_case(deal_id: str) -> dict:
    deal_input = get_deal_input(deal_id)
    if not deal_input:
        raise ConsumptionCommercialIntelligenceError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")
    forecast = _require_current_forecast(deal_id)
    assumptions = get_business_case_assumptions(deal_id)
    if not assumptions:
        raise ConsumptionCommercialIntelligenceError("BUSINESS_CASE_ASSUMPTIONS_REQUIRED", "Business-case assumptions are required.")
    assumptions = validate_business_case_assumptions(assumptions)
    currency = deal_input.get("commercial", {}).get("currency", "USD")
    if assumptions.get("currency", "USD") != currency or forecast.get("currency", "USD") != currency:
        raise ConsumptionCommercialIntelligenceError("CURRENCY_MISMATCH", "Business-case, forecast, and deal currencies must match.")
    scenarios = {name: calculate_business_case_scenario(forecast["scenarios"][name], assumptions, currency) for name in SCENARIOS}
    result = {
        "deal_id": deal_id,
        "analysis_version": INTELLIGENCE_VERSION,
        "calculated_at": _utc_now(),
        "status": "CURRENT",
        "currency": currency,
        "default_scenario": "EXPECTED",
        "confidence": confidence_from_provenance(assumptions.get("provenance")),
        "assumption_source": assumptions.get("assumption_source"),
        "assumptions": assumptions,
        "methodology": {
            "current_state_tco": "Annual current-state infrastructure, operations, and software costs are held flat for 3 years unless explicit assumptions are edited.",
            "proposed_azure_tco": "36-month Azure consumption plus explicit migration, implementation, training/change, and other transition costs.",
            "npv": "Year 0 transition investment, then annual avoided current-state cost plus explicit benefits minus Azure consumption, discounted by discount_rate.",
            "payback": "Monthly cumulative cash flow using flat monthly current-state costs and benefits minus forecast monthly Azure consumption.",
        },
        "scenarios": scenarios,
        "dependency_signature": _dependency_signature(deal_input, forecast, assumptions),
    }
    save_business_case(result)
    return result


def calculate_business_case_scenario(scenario: dict, assumptions: dict, currency: str = "USD") -> dict:
    annual_current = sum(_as_decimal(assumptions.get(f, 0), f) for f in (
        "current_state_annual_infrastructure_cost",
        "current_state_annual_operations_cost",
        "current_state_annual_software_cost",
    ))
    transition = sum(_as_decimal(assumptions.get(f, 0), f) for f in (
        "migration_cost",
        "implementation_cost",
        "training_change_management_cost",
        "other_transition_cost",
    ))
    annual_benefits = sum(_as_decimal(assumptions.get(f, 0), f) for f in (
        "annual_operations_savings",
        "annual_productivity_benefit",
        "other_annual_benefit",
    ))
    azure_y1 = _as_decimal(scenario.get("year_1"), "year_1")
    azure_y2 = _as_decimal(scenario.get("year_2"), "year_2")
    azure_y3 = _as_decimal(scenario.get("year_3"), "year_3")
    azure_consumption = azure_y1 + azure_y2 + azure_y3
    current_tco = annual_current * Decimal("3")
    additional_benefits = annual_benefits * Decimal("3")
    proposed_tco = azure_consumption + transition
    savings = current_tco - proposed_tco
    total_benefits = current_tco + additional_benefits
    net_benefit = total_benefits - proposed_tco
    if proposed_tco == 0:
        raise ConsumptionCommercialIntelligenceError("ZERO_PROPOSED_COST", "Proposed Azure TCO must be greater than zero for ROI.")
    roi = net_benefit / proposed_tco * Decimal("100")
    annual_cash_flows = [
        -transition,
        annual_current + annual_benefits - azure_y1,
        annual_current + annual_benefits - azure_y2,
        annual_current + annual_benefits - azure_y3,
    ]
    discount_rate = _as_decimal(assumptions.get("discount_rate"), "discount_rate")
    npv = sum(cf / ((Decimal("1") + discount_rate) ** i) for i, cf in enumerate(annual_cash_flows))
    payback_month = monthly_payback_month(scenario.get("monthly_totals", []), annual_current, annual_benefits, transition)
    return {
        "scenario": scenario.get("scenario"),
        "currency": currency,
        "current_state_annual_cost": _money(annual_current),
        "current_state_3yr_tco": _money(current_tco),
        "azure_consumption_3yr": _money(azure_consumption),
        "migration_cost": _money(assumptions.get("migration_cost", 0)),
        "implementation_cost": _money(assumptions.get("implementation_cost", 0)),
        "training_change_management_cost": _money(assumptions.get("training_change_management_cost", 0)),
        "other_transition_cost": _money(assumptions.get("other_transition_cost", 0)),
        "transition_costs": _money(transition),
        "proposed_azure_3yr_tco": _money(proposed_tco),
        "tco_savings": _money(savings),
        "additional_benefits": _money(additional_benefits),
        "total_benefits": _money(total_benefits),
        "net_benefit": _money(net_benefit),
        "roi_pct": _money(roi),
        "npv": _money(npv),
        "payback_month": payback_month,
        "payback_label": f"{payback_month} months" if payback_month else "Not achieved within forecast period",
        "annual_cash_flows": [_money(cf) for cf in annual_cash_flows],
    }


def monthly_payback_month(monthly_totals: list[dict], annual_current: Decimal, annual_benefits: Decimal, transition: Decimal) -> int | None:
    cumulative = -transition
    monthly_benefit = (annual_current + annual_benefits) / Decimal("12")
    for row in monthly_totals[:36]:
        cumulative += monthly_benefit - _as_decimal(row.get("monthly_cost", 0), "monthly_cost")
        if cumulative >= 0:
            return int(row.get("month"))
    return None


def _dependency_signature(deal_input: dict, forecast: dict, assumptions: dict | None) -> str:
    commitment = deal_input.get("consumption_commitment") or {}
    payload = {
        "commitment": {k: commitment.get(k) for k in ("azure_commitment_value", "cloud_consumption_commitment", "commitment_amount")},
        "commercial_tcv": deal_input.get("commercial", {}).get("tcv"),
        "forecast_signature": forecast.get("dependency_signature"),
        "forecasted_at": forecast.get("forecasted_at"),
        "assumptions": assumptions,
    }
    return json.dumps(payload, sort_keys=True)


def load_commercial_intelligence_store() -> dict:
    return _load_json(COMMERCIAL_INTELLIGENCE_PATH, {})


def save_commercial_intelligence(result: dict) -> dict:
    store = load_commercial_intelligence_store()
    store[result["deal_id"]] = result
    _write_json(COMMERCIAL_INTELLIGENCE_PATH, store)
    return result


def get_commercial_intelligence(deal_id: str) -> dict | None:
    result = load_commercial_intelligence_store().get(deal_id)
    if not result:
        return None
    try:
        deal = get_deal_input(deal_id)
        forecast = _require_current_forecast(deal_id)
        if _dependency_signature(deal, forecast, None) != result.get("dependency_signature"):
            return {**result, "status": "STALE", "stale": True}
    except ConsumptionCommercialIntelligenceError:
        return {**result, "status": "STALE", "stale": True}
    return result


def load_business_case_store() -> dict:
    return _load_json(BUSINESS_CASES_PATH, {})


def save_business_case(result: dict) -> dict:
    store = load_business_case_store()
    store[result["deal_id"]] = result
    _write_json(BUSINESS_CASES_PATH, store)
    return result


def get_business_case(deal_id: str) -> dict | None:
    result = load_business_case_store().get(deal_id)
    if not result:
        return None
    try:
        deal = get_deal_input(deal_id)
        forecast = _require_current_forecast(deal_id)
        assumptions = get_business_case_assumptions(deal_id)
        if _dependency_signature(deal, forecast, validate_business_case_assumptions(assumptions)) != result.get("dependency_signature"):
            return {**result, "status": "STALE", "stale": True}
    except ConsumptionCommercialIntelligenceError:
        return {**result, "status": "STALE", "stale": True}
    return result


def invalidate_commercial_intelligence(deal_id: str) -> None:
    store = load_commercial_intelligence_store()
    if deal_id in store:
        store[deal_id]["status"] = "STALE"
        store[deal_id]["stale"] = True
        _write_json(COMMERCIAL_INTELLIGENCE_PATH, store)


def invalidate_business_case(deal_id: str) -> None:
    store = load_business_case_store()
    if deal_id in store:
        store[deal_id]["status"] = "STALE"
        store[deal_id]["stale"] = True
        _write_json(BUSINESS_CASES_PATH, store)


def invalidate_intelligence(deal_id: str) -> None:
    invalidate_commercial_intelligence(deal_id)
    invalidate_business_case(deal_id)
