"""Consumption profile access and validation for Phase 8.2.

Profiles are workload assumptions linked to canonical commercial deals by
deal_id. They intentionally exclude commercial economics, pricing, ROI, and
forecast outputs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    from commercial_data import DEAL_INPUTS_PATH, get_deal_input
except ModuleNotFoundError:
    from tools.commercial_data import DEAL_INPUTS_PATH, get_deal_input


BASE_DIR = Path(__file__).resolve().parent.parent
COMMERCIAL_DIR = BASE_DIR / "knowledge_base" / "commercial_strategy"
CONSUMPTION_PROFILES_PATH = COMMERCIAL_DIR / "consumption_profiles.json"
PROFILE_OVERRIDES_PATH = COMMERCIAL_DIR / "consumption_profile_overrides.json"
ALLOWED_CONFIDENCE = {"LOW", "MEDIUM", "HIGH"}
AVAILABILITY_RE = re.compile(r"^99(?:\.\d{1,3})?%$")


class ConsumptionProfileValidationError(ValueError):
    """Raised when a consumption profile fixture is invalid."""


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _profile_overrides() -> dict:
    if not PROFILE_OVERRIDES_PATH.exists():
        return {}
    try:
        return _load_json(PROFILE_OVERRIDES_PATH)
    except (OSError, json.JSONDecodeError):
        return {}


def load_consumption_profiles() -> list[dict]:
    return _load_json(CONSUMPTION_PROFILES_PATH)


def _valid_deal_ids() -> set[str]:
    return {item.get("deal_id") for item in _load_json(DEAL_INPUTS_PATH)}


def _require_non_negative_number(profile: dict, field: str, errors: list[str]) -> None:
    value = profile.get(field)
    if not isinstance(value, (int, float)) or value < 0:
        errors.append(f"{field} must be a non-negative number")


def validate_consumption_profile(profile: dict, valid_deal_ids: set[str] | None = None) -> list[str]:
    errors: list[str] = []
    valid_deal_ids = valid_deal_ids if valid_deal_ids is not None else _valid_deal_ids()

    deal_id = profile.get("deal_id")
    if not deal_id:
        errors.append("deal_id is required")
    elif deal_id not in valid_deal_ids:
        errors.append(f"deal_id {deal_id} does not reference an existing commercial deal")

    if not profile.get("workload_type"):
        errors.append("workload_type is required")

    for field in (
        "expected_users",
        "monthly_transactions",
        "data_volume_tb",
        "year_2_growth_rate",
        "year_3_growth_rate",
    ):
        _require_non_negative_number(profile, field, errors)

    environment_count = profile.get("environment_count")
    if not isinstance(environment_count, int) or environment_count < 1:
        errors.append("environment_count must be an integer >= 1")

    confidence = profile.get("confidence")
    if confidence not in ALLOWED_CONFIDENCE:
        errors.append(f"confidence must be one of {sorted(ALLOWED_CONFIDENCE)}")

    availability = profile.get("availability_target")
    if not isinstance(availability, str) or not AVAILABILITY_RE.match(availability):
        errors.append("availability_target must use a percentage format like 99.9% or 99.99%")

    if not isinstance(profile.get("dr_required"), bool):
        errors.append("dr_required must be boolean")

    if not profile.get("primary_region"):
        errors.append("primary_region is required")

    return errors


def validate_consumption_profiles(profiles: list[dict] | None = None) -> dict:
    profiles = profiles if profiles is not None else load_consumption_profiles()
    valid_deal_ids = _valid_deal_ids()
    errors: list[str] = []
    seen: set[str] = set()

    for index, profile in enumerate(profiles):
        deal_id = profile.get("deal_id")
        if deal_id in seen:
            errors.append(f"duplicate consumption profile for deal_id {deal_id}")
        if deal_id:
            seen.add(deal_id)
        for error in validate_consumption_profile(profile, valid_deal_ids):
            errors.append(f"profile[{index}] {error}")


    return {"valid": not errors, "errors": errors, "count": len(profiles)}


def get_consumption_profile(deal_id: str) -> dict | None:
    override = _profile_overrides().get(deal_id)
    if override:
        return override
    for profile in load_consumption_profiles():
        if profile.get("deal_id") == deal_id:
            return profile
    return None


def save_consumption_profile(deal_id: str, updates: dict) -> dict:
    """Persist user-edited assumptions without mutating the canonical fixtures."""
    deal = get_deal_input(deal_id)
    if not deal:
        raise ConsumptionProfileValidationError(f"No DealInput found for {deal_id}")
    current = get_consumption_profile(deal_id)
    if not current:
        raise ConsumptionProfileValidationError(f"No consumption profile found for {deal_id}")
    profile = {**current, **updates, "deal_id": deal_id}
    provenance = dict(current.get("provenance") or {})
    for field in updates:
        if field != "provenance":
            provenance[field] = {"field": field, "value": profile[field], "source": "USER_PROVIDED", "confidence": "HIGH"}
    profile["provenance"] = provenance
    errors = validate_consumption_profile(profile)
    if errors:
        raise ConsumptionProfileValidationError("; ".join(errors))
    overrides = _profile_overrides()
    overrides[deal_id] = profile
    _write_json(PROFILE_OVERRIDES_PATH, overrides)
    # The dependency chain is deliberately explicit and best-effort: profile editing
    # remains usable even when an optional downstream store is absent.
    try:
        from tools.consumption_architecture_planner import invalidate_architecture
        invalidate_architecture(deal_id)
    except (ImportError, AttributeError):
        pass
    return profile


def get_consumption_analysis_record(deal_id: str) -> dict | None:
    deal = get_deal_input(deal_id)
    if not deal:
        return None
    profile = get_consumption_profile(deal_id)
    try:
        from tools.enterprise_workload_profiler import profile_enterprise_workload
        enterprise_profile = profile_enterprise_workload(profile or {}, deal)
    except ImportError:
        enterprise_profile = None
    return {
        "deal": deal,
        "deal_input": deal,
        "consumption_profile": profile,
        "enterprise_profile": enterprise_profile,
    }
