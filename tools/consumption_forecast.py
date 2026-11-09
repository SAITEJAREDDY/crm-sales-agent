"""36-month Azure consumption forecast engine for Phase 8.5."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

try:
    from tools.commercial_data import get_deal_input
    from tools.consumption_profiles import get_consumption_profile
    from tools.consumption_architecture_planner import get_architecture
    from tools.azure_retail_pricing import get_pricing, unit_multiplier
except ModuleNotFoundError:
    from commercial_data import get_deal_input
    from consumption_profiles import get_consumption_profile
    from consumption_architecture_planner import get_architecture
    from azure_retail_pricing import get_pricing, unit_multiplier


BASE_DIR = Path(__file__).resolve().parent.parent
COMMERCIAL_DIR = BASE_DIR / "knowledge_base" / "commercial_strategy"
FORECAST_STORE_PATH = COMMERCIAL_DIR / "consumption_forecasts.json"
FORECAST_VERSION = "1.0"
SCENARIOS = {
    "CONSERVATIVE": {"y2_factor": Decimal("0.40"), "y3_factor": Decimal("0.50")},
    "EXPECTED": {"y2_factor": Decimal("1.00"), "y3_factor": Decimal("1.00")},
    "HIGH_GROWTH": {"y2_factor": Decimal("2.00"), "y3_factor": Decimal("2.00")},
}
RESOLVED_STATUSES = {"RESOLVED", "MANUAL_OVERRIDE"}
DISCRETE_CATEGORIES = {"compute", "database", "networking", "container", "identity"}
DATA_CATEGORIES = {"storage", "backup"}
USAGE_GROWTH_CATEGORIES = {"monitoring", "analytics", "ai", "integration", "messaging"}
FIXED_CATEGORIES = {"security"}


class ConsumptionForecastError(ValueError):
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


def scenario_targets(profile: dict, scenario: str) -> tuple[Decimal, Decimal]:
    cfg = SCENARIOS[scenario]
    y2 = Decimal("1") + Decimal(str(profile.get("year_2_growth_rate", 0))) * cfg["y2_factor"]
    y3 = Decimal("1") + Decimal(str(profile.get("year_3_growth_rate", 0))) * cfg["y3_factor"]
    return y2, y3


def monthly_multiplier(month: int, profile: dict, scenario: str) -> Decimal:
    if not 1 <= month <= 36:
        raise ValueError("month must be between 1 and 36")
    y2, y3 = scenario_targets(profile, scenario)
    if month <= 12:
        return Decimal("1")
    if month <= 24:
        return Decimal("1") + (y2 - Decimal("1")) * Decimal(month - 12) / Decimal("12")
    return y2 + (y3 - y2) * Decimal(month - 24) / Decimal("12")


def data_multiplier(month: int, profile: dict) -> Decimal:
    base_tb = Decimal(str(profile.get("data_volume_tb") or 0))
    growth_tb = Decimal(str(profile.get("monthly_data_growth_tb") or 0))
    if base_tb <= 0 or growth_tb <= 0:
        return Decimal("1")
    return (base_tb + growth_tb * Decimal(month - 1)) / base_tb


def scaling_mode(category: str) -> str:
    if category in DISCRETE_CATEGORIES:
        return "discrete_quantity"
    if category in DATA_CATEGORIES:
        return "continuous_quantity"
    if category in FIXED_CATEGORIES:
        return "fixed"
    return "continuous_usage"


def forecast_service_month(service: dict, pricing: dict, profile: dict, scenario: str, month: int) -> dict:
    category = service.get("service_category")
    mode = scaling_mode(category)
    workload_mult = monthly_multiplier(month, profile, scenario)
    data_mult = data_multiplier(month, profile)
    growth = data_mult if category in DATA_CATEGORIES else (Decimal("1") if mode == "fixed" else workload_mult)
    base_qty = Decimal(str(service.get("quantity", 0)))
    base_usage = Decimal(str(service.get("monthly_usage_per_unit", 0)))
    forecast_qty = base_qty
    forecast_usage = base_usage
    if mode == "discrete_quantity":
        forecast_qty = Decimal(math.ceil(base_qty * growth))
    elif mode == "continuous_quantity":
        forecast_qty = base_qty * growth
    elif mode == "continuous_usage":
        forecast_usage = base_usage * growth
    retail_price = Decimal(str(pricing.get("retail_price", 0)))
    mult = unit_multiplier(service.get("unit"), pricing.get("unit_of_measure"))
    if mult is None:
        raise ConsumptionForecastError("INCOMPATIBLE_UNITS", f"Pricing meter unit is not compatible for {service.get('service_id')}.")
    monthly_cost = forecast_qty * forecast_usage * mult * retail_price
    return {
        "month": month,
        "scenario": scenario,
        "service_id": service.get("service_id"),
        "service_category": category,
        "azure_service": service.get("azure_service"),
        "sku": service.get("sku"),
        "baseline_quantity": _money(base_qty),
        "forecast_quantity": _money(forecast_qty),
        "baseline_monthly_usage_per_unit": _money(base_usage),
        "forecast_monthly_usage_per_unit": _money(forecast_usage),
        "growth_multiplier": _money(growth),
        "retail_unit_price": _money(retail_price),
        "unit_multiplier": _money(mult),
        "unit": service.get("unit"),
        "monthly_cost": _money(monthly_cost),
        "scaling_mode": mode,
    }


def _aggregate(records: list[dict], key: str = "monthly_cost") -> Decimal:
    return sum(Decimal(str(item[key])) for item in records)


def build_scenario(scenario: str, architecture: dict, pricing: dict, profile: dict) -> dict:
    pricing_by_id = {svc["service_id"]: svc for svc in pricing.get("services", [])}
    resolved_services = [svc for svc in architecture.get("services", []) if pricing_by_id.get(svc.get("service_id"), {}).get("pricing_status") in RESOLVED_STATUSES]
    monthly_records = [
        forecast_service_month(svc, pricing_by_id[svc["service_id"]], profile, scenario, month)
        for month in range(1, 37)
        for svc in resolved_services
    ]
    monthly_totals = [
        {"month": month, "monthly_cost": _money(_aggregate([r for r in monthly_records if r["month"] == month]))}
        for month in range(1, 37)
    ]
    y1 = _aggregate([m for m in monthly_totals if 1 <= m["month"] <= 12])
    y2 = _aggregate([m for m in monthly_totals if 13 <= m["month"] <= 24])
    y3 = _aggregate([m for m in monthly_totals if 25 <= m["month"] <= 36])
    by_category = {}
    for category in sorted({r["service_category"] for r in monthly_records}):
        rows = [r for r in monthly_records if r["service_category"] == category]
        cy1 = _aggregate([r for r in rows if 1 <= r["month"] <= 12])
        cy2 = _aggregate([r for r in rows if 13 <= r["month"] <= 24])
        cy3 = _aggregate([r for r in rows if 25 <= r["month"] <= 36])
        by_category[category] = {"year_1": _money(cy1), "year_2": _money(cy2), "year_3": _money(cy3), "total_36_month": _money(cy1 + cy2 + cy3)}
    return {
        "scenario": scenario,
        "year_1": _money(y1),
        "year_2": _money(y2),
        "year_3": _money(y3),
        "total_36_month": _money(y1 + y2 + y3),
        "monthly_totals": monthly_totals,
        "service_months": monthly_records,
        "by_category": by_category,
        "growth_assumptions": growth_assumptions(profile, scenario),
    }


def growth_assumptions(profile: dict, scenario: str) -> dict:
    y2, y3 = scenario_targets(profile, scenario)
    return {
        "scenario": scenario,
        "workload_y2_multiplier": _money(y2),
        "workload_y3_multiplier": _money(y3),
        "data_growth_precedence": "storage and backup use monthly_data_growth_tb when present; otherwise scenario workload growth",
        "price_assumption": "retail unit prices are fixed from Phase 8.4 pricing",
    }


def validate_reconciliation(forecast: dict) -> list[str]:
    errors: list[str] = []
    for name, scenario in forecast.get("scenarios", {}).items():
        for month in range(1, 37):
            service_sum = _aggregate([r for r in scenario["service_months"] if r["month"] == month])
            month_total = Decimal(str(next(m["monthly_cost"] for m in scenario["monthly_totals"] if m["month"] == month)))
            if service_sum != month_total:
                errors.append(f"{name} month {month} service totals do not reconcile")
        y1 = _aggregate([m for m in scenario["monthly_totals"] if 1 <= m["month"] <= 12])
        y2 = _aggregate([m for m in scenario["monthly_totals"] if 13 <= m["month"] <= 24])
        y3 = _aggregate([m for m in scenario["monthly_totals"] if 25 <= m["month"] <= 36])
        if y1 != Decimal(str(scenario["year_1"])) or y2 != Decimal(str(scenario["year_2"])) or y3 != Decimal(str(scenario["year_3"])):
            errors.append(f"{name} annual totals do not reconcile")
        if y1 + y2 + y3 != Decimal(str(scenario["total_36_month"])):
            errors.append(f"{name} 36-month total does not reconcile")
    return errors


def _dependency_signature(profile: dict, architecture: dict, pricing: dict) -> str:
    payload = {
        "profile_growth": {k: profile.get(k) for k in ("year_2_growth_rate", "year_3_growth_rate", "monthly_data_growth_tb", "data_volume_tb")},
        "architecture_signature": pricing.get("architecture_signature"),
        "pricing_timestamp": pricing.get("priced_at"),
        "pricing_status": pricing.get("pricing_status"),
    }
    return json.dumps(payload, sort_keys=True)


def generate_forecast(deal_id: str) -> dict:
    if not get_deal_input(deal_id):
        raise ConsumptionForecastError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")
    profile = get_consumption_profile(deal_id)
    if not profile:
        raise ConsumptionForecastError("CONSUMPTION_PROFILE_REQUIRED", "A workload profile is required before forecasting.")
    architecture_record = get_architecture(deal_id)
    if not architecture_record or not architecture_record.get("architecture"):
        raise ConsumptionForecastError("ARCHITECTURE_REQUIRED", "A validated architecture is required before forecasting.")
    pricing = get_pricing(deal_id)
    if not pricing:
        raise ConsumptionForecastError("PRICING_REQUIRED", "Azure pricing is required before forecasting.")
    if pricing.get("pricing_status") == "STALE" or pricing.get("stale"):
        raise ConsumptionForecastError("PRICING_STALE", "Refresh Azure pricing before generating a forecast.")
    resolved_count = len([s for s in pricing.get("services", []) if s.get("pricing_status") in RESOLVED_STATUSES])
    if resolved_count == 0:
        raise ConsumptionForecastError("NO_RESOLVED_SERVICES", "At least one resolved priced service is required before forecasting.")
    architecture = architecture_record["architecture"]
    forecast = {
        "deal_id": deal_id,
        "architecture_version": architecture.get("architecture_version", "1.0"),
        "pricing_timestamp": pricing.get("priced_at"),
        "forecast_version": FORECAST_VERSION,
        "forecasted_at": _utc_now(),
        "forecast_status": "COMPLETE" if resolved_count == len(architecture.get("services", [])) and pricing.get("pricing_complete") else "INCOMPLETE",
        "currency": pricing.get("currency", "USD"),
        "services_forecast": resolved_count,
        "total_services": len(architecture.get("services", [])),
        "unresolved_services": [s for s in pricing.get("services", []) if s.get("pricing_status") not in RESOLVED_STATUSES],
        "dependency_signature": _dependency_signature(profile, architecture, pricing),
        "scenarios": {name: build_scenario(name, architecture, pricing, profile) for name in SCENARIOS},
    }
    errors = validate_reconciliation(forecast)
    if errors:
        raise ConsumptionForecastError("FORECAST_RECONCILIATION_FAILED", "; ".join(errors))
    save_forecast(forecast)
    print(f"[ConsumptionForecast] deal_id={deal_id} status={forecast['forecast_status']} services={resolved_count}/{forecast['total_services']}")
    return forecast


def load_forecast_store() -> dict:
    return _load_json(FORECAST_STORE_PATH, {})


def save_forecast(forecast: dict) -> dict:
    store = load_forecast_store()
    store[forecast["deal_id"]] = forecast
    _write_json(FORECAST_STORE_PATH, store)
    try:
        from tools.consumption_commercial_intelligence import invalidate_intelligence
    except ModuleNotFoundError:
        from consumption_commercial_intelligence import invalidate_intelligence
    invalidate_intelligence(forecast["deal_id"])
    return forecast


def get_forecast(deal_id: str) -> dict | None:
    forecast = load_forecast_store().get(deal_id)
    if not forecast:
        return None
    profile = get_consumption_profile(deal_id)
    arch = get_architecture(deal_id)
    pricing = get_pricing(deal_id)
    if not profile or not arch or not pricing or pricing.get("pricing_status") == "STALE":
        return {**forecast, "forecast_status": "STALE", "stale": True}
    current = _dependency_signature(profile, arch.get("architecture", {}), pricing)
    if current != forecast.get("dependency_signature"):
        return {**forecast, "forecast_status": "STALE", "stale": True}
    return forecast


def invalidate_forecast(deal_id: str) -> None:
    store = load_forecast_store()
    if deal_id in store:
        store[deal_id]["forecast_status"] = "STALE"
        store[deal_id]["stale"] = True
        _write_json(FORECAST_STORE_PATH, store)
    try:
        from tools.consumption_commercial_intelligence import invalidate_intelligence
    except ModuleNotFoundError:
        from consumption_commercial_intelligence import invalidate_intelligence
    invalidate_intelligence(deal_id)
