"""Azure Retail Prices resolver and baseline monthly cost engine for Phase 8.4."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests

try:
    from tools.commercial_data import get_deal_input
    from tools.consumption_profiles import get_consumption_profile
    from tools.consumption_architecture_planner import get_architecture
    from tools.azure_pricing_config import (
        PRIMARY_REGION,
        SECONDARY_REGION,
        SECONDARY_REGION_PRICE_MULTIPLIER,
        PRICING_METHOD_ACTUAL_AZURE_RETAIL,
        PRICING_METHOD_MODEL_MULTIPLIER,
        PRICING_SOURCE_LOCAL_SNAPSHOT,
        PRICING_SOURCE_LIVE_AZURE_API,
        PRICING_SOURCE_MANUAL_OVERRIDE,
        SERVICE_NAME_MAP,
    )
    from tools.azure_pricing_snapshot import (
        load_snapshot,
        candidates_for_service,
        to_api_shape,
        snapshot_status,
    )
except ModuleNotFoundError:
    from commercial_data import get_deal_input
    from consumption_profiles import get_consumption_profile
    from consumption_architecture_planner import get_architecture
    from azure_pricing_config import (
        PRIMARY_REGION,
        SECONDARY_REGION,
        SECONDARY_REGION_PRICE_MULTIPLIER,
        PRICING_METHOD_ACTUAL_AZURE_RETAIL,
        PRICING_METHOD_MODEL_MULTIPLIER,
        PRICING_SOURCE_LOCAL_SNAPSHOT,
        PRICING_SOURCE_LIVE_AZURE_API,
        PRICING_SOURCE_MANUAL_OVERRIDE,
        SERVICE_NAME_MAP,
    )
    from azure_pricing_snapshot import (
        load_snapshot,
        candidates_for_service,
        to_api_shape,
        snapshot_status,
    )


BASE_URL = "https://prices.azure.com/api/retail/prices"
BASE_DIR = Path(__file__).resolve().parent.parent
COMMERCIAL_DIR = BASE_DIR / "knowledge_base" / "commercial_strategy"
PRICING_STORE_PATH = COMMERCIAL_DIR / "consumption_pricing.json"
PRICING_CACHE_PATH = COMMERCIAL_DIR / "azure_pricing_cache.json"
STATUSES = {"NOT_PRICED", "RESOLVED", "AMBIGUOUS", "NOT_FOUND", "API_ERROR", "MANUAL_OVERRIDE", "PRICING_UNAVAILABLE"}
# SERVICE_NAME_MAP now lives in tools/azure_pricing_config.py (single source
# of truth, also used by the architecture planner's catalog conformance check).


class AzurePricingError(ValueError):
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


def build_query(service: dict, currency: str = "USD", include_price_type: bool = True) -> str:
    service_name = SERVICE_NAME_MAP.get(service.get("azure_service"), service.get("azure_service"))
    parts = [f"serviceName eq '{service_name}'", f"armRegionName eq '{service.get('region')}'", f"currencyCode eq '{currency}'"]
    if include_price_type:
        parts.insert(2, "priceType eq 'Consumption'")
    sku = service.get("sku")
    if sku and str(sku).startswith("Standard_"):
        parts.insert(2, f"armSkuName eq '{sku}'")
    elif sku and service.get("azure_service") in {"Azure Synapse Analytics", "Azure Databricks"}:
        parts.insert(2, f"contains(skuName, '{sku}')")
    return " and ".join(parts)


def call_retail_prices_api(filter_str: str, get_func=None, max_pages: int = 5) -> list[dict]:
    get_func = get_func or requests.get
    url = f"{BASE_URL}?$filter={filter_str}"
    items: list[dict] = []
    for _ in range(max_pages):
        response = get_func(url, timeout=10)
        response.raise_for_status()
        payload = response.json()
        page_items = payload.get("Items")
        if not isinstance(page_items, list):
            raise AzurePricingError("MALFORMED_PRICING_RESPONSE", "Azure pricing response did not contain Items.")
        items.extend(page_items)
        url = payload.get("NextPageLink")
        if not url:
            break
    return items


def normalize_unit(unit: str | None) -> str:
    value = (unit or "").strip().lower().replace(" ", "")
    aliases = {
        "hour": "hour", "1hour": "hour", "1/hour": "hour", "hours": "hour",
        "gb": "gb", "1gb": "gb",
        "gb/month": "gb-month", "gb-month": "gb-month", "1gb/month": "gb-month", "gbmonth": "gb-month",
        "tb": "tb", "tb-month": "tb-month", "1tb/month": "tb-month",
        "operation": "operation", "operations": "operation", "1koperations": "1k-operations", "1moperations": "1m-operations",
        "request": "request", "requests": "request", "1mrequests": "1m-requests",
        "message": "message", "messages": "message",
        "vcore-hour": "hour", "gateway-hour": "hour", "dwu-hour": "hour",
    }
    return aliases.get(value, value)


_COUNT_SCALE_SUFFIX = {"": Decimal("1"), "k": Decimal("1000"), "m": Decimal("1000000")}
_COUNT_TIME_SUFFIX_RE = re.compile(r"/(month|hour|day|year|week)$")
_COUNT_NOUN_SUFFIX_RE = re.compile(r"-?(operations?|requests?|transactions?|tokens?|messages?|calls?|units?)$")
_COUNT_BATCH_RE = re.compile(r"^(\d+(?:\.\d+)?)([km]?)$")


def _parse_count_batch(unit: str | None) -> Decimal | None:
    """Parses the numeric batch size out of a bare Azure count-priced unit
    string (e.g. '10K', '1M', '100', '1/Month', '10K/Hour') or a lightly
    decorated BOM unit ('1k-operations', '10k-operations'). Returns None for
    anything that isn't a recognizable count batch (time/data units like
    '1 Hour' or '1 GB/Month' correctly fall through to None here)."""
    t = (unit or "").strip().lower().replace(" ", "")
    if not t:
        return None
    # Azure sometimes prices a count batch "per period" (e.g. "10K/Month");
    # the whole pricing engine already computes monthly totals from monthly
    # usage, so only the batch size itself matters here.
    t = _COUNT_TIME_SUFFIX_RE.sub("", t)
    if t.startswith("1/"):
        t = t[2:] or "1"
    t = _COUNT_NOUN_SUFFIX_RE.sub("", t) or "1"
    match = _COUNT_BATCH_RE.match(t)
    if not match:
        return None
    number, scale = match.groups()
    return Decimal(number) * _COUNT_SCALE_SUFFIX.get(scale, Decimal("1"))


def unit_multiplier(bom_unit: str, meter_unit: str) -> Decimal | None:
    bom = normalize_unit(bom_unit)
    meter = normalize_unit(meter_unit)
    if bom == meter:
        return Decimal("1")
    if bom == "tb" and meter in {"gb", "gb-month"}:
        return Decimal("1024")
    if bom == "tb-month" and meter == "gb-month":
        return Decimal("1024")
    if bom == "operation" and meter == "1k-operations":
        return Decimal("0.001")
    if bom == "operation" and meter == "1m-operations":
        return Decimal("0.000001")
    if bom == "request" and meter == "1m-requests":
        return Decimal("0.000001")
    # General count-batch fallback: Azure prices many operation/request/
    # message/token meters as a bare batch size ("10K", "1M", "100", ...)
    # with no indication in the unit string of what's being counted -- the
    # service/SKU match (already scored before this point) is what
    # establishes semantic correctness, not the unit string alone. This
    # covers meters the small hardcoded aliases above don't (Key Vault "10K"
    # operations, Cosmos DB "100 RU/s", Cognitive Search "1K" documents,
    # OpenAI "1K" tokens, etc.) without needing a new special case per meter.
    bom_batch = _parse_count_batch(bom_unit)
    meter_batch = _parse_count_batch(meter_unit)
    if bom_batch is not None and meter_batch is not None and meter_batch > 0:
        return bom_batch / meter_batch
    return None


def calculate_monthly_cost(service: dict, meter: dict) -> Decimal | None:
    multiplier = unit_multiplier(service.get("unit"), meter.get("unitOfMeasure"))
    if multiplier is None:
        return None
    quantity = Decimal(str(service.get("quantity", 0)))
    usage = Decimal(str(service.get("monthly_usage_per_unit", 0)))
    price = Decimal(str(meter.get("retailPrice", meter.get("unitPrice", 0))))
    return quantity * usage * multiplier * price


def _meter_summary(item: dict) -> dict:
    fields = ["currencyCode", "retailPrice", "unitPrice", "unitOfMeasure", "tierMinimumUnits", "armRegionName", "location", "effectiveStartDate", "meterId", "meterName", "productId", "skuId", "productName", "skuName", "serviceName", "serviceId", "serviceFamily", "priceType", "armSkuName", "reservationTerm"]
    return {field: item.get(field) for field in fields if item.get(field) is not None}


def _score_candidate(service: dict, item: dict, currency: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    sku_text = " ".join(str(item.get(k, "")) for k in ("skuName", "meterName", "productName", "armSkuName")).lower()
    if item.get("priceType") == "Consumption":
        score += 15; reasons.append("consumption price")
    if item.get("currencyCode") == currency:
        score += 10; reasons.append("currency")
    if item.get("armRegionName") == service.get("region"):
        score += 20; reasons.append("region")
    mapped = SERVICE_NAME_MAP.get(service.get("azure_service"), service.get("azure_service"))
    if item.get("serviceName") == mapped:
        score += 20; reasons.append("service")
    if service.get("sku") and item.get("armSkuName") == service.get("sku"):
        score += 30; reasons.append("arm sku")
    if service.get("sku") and str(service.get("sku")).lower() in sku_text:
        score += 15; reasons.append("sku text")
    if service.get("tier") and str(service.get("tier")).lower() in sku_text:
        score += 8; reasons.append("tier")
    if unit_multiplier(service.get("unit"), item.get("unitOfMeasure")) is not None:
        score += 25; reasons.append("unit")
    else:
        score -= 40; reasons.append("incompatible unit")
    if any(word in sku_text for word in ("spot", "low priority", "reservation", "savings plan", "dev/test")) or item.get("reservationTerm"):
        score -= 100; reasons.append("excluded baseline price")
    if item.get("retailPrice", 0) is None or Decimal(str(item.get("retailPrice", 0))) <= 0:
        score -= 20; reasons.append("non-positive price")
    score += _domain_meter_adjustment(service, item, sku_text, reasons)
    return score, reasons


def _domain_meter_adjustment(service: dict, item: dict, sku_text: str, reasons: list[str]) -> int:
    azure_service = service.get("azure_service")
    sku = str(service.get("sku") or "").lower().replace("_", " ")
    tier = str(service.get("tier") or "").lower()
    delta = 0
    if azure_service == "Virtual Machines":
        if any(word in sku_text for word in ("windows", "sql", "red hat", "rhel", "suse")):
            delta -= 35; reasons.append("non-linux/license add-on")
        if sku and sku in sku_text.replace("_", " "):
            delta += 20; reasons.append("vm size text")
    elif azure_service == "Azure Synapse Analytics":
        if service.get("sku") and str(service.get("sku")).lower() in sku_text:
            delta += 35; reasons.append("synapse dwu sku")
        if "data warehouse" in sku_text or "dwu" in sku_text:
            delta += 15; reasons.append("synapse dedicated pool meter")
        if any(word in sku_text for word in ("ahb", " v3", "pipeline activity", "hosted ir")):
            delta -= 40; reasons.append("non-dwu synapse meter")
    elif azure_service == "Azure Blob Storage":
        if "hot" in sku_text:
            delta += 25; reasons.append("hot storage")
        if "data stored" in sku_text:
            delta += 20; reasons.append("storage capacity meter")
        if "snapshots" in sku_text:
            delta -= 50; reasons.append("snapshot meter")
        if any(word in sku_text for word in ("grs", "zrs", "read access", "archive", "cool")):
            delta -= 15; reasons.append("non-lrs/hot storage")
    elif azure_service == "Azure Backup":
        if "data stored" in sku_text:
            delta += 25; reasons.append("backup stored data")
        if "standard" in sku_text:
            delta += 15; reasons.append("standard backup")
        if "protected instance" in sku_text:
            delta -= 30; reasons.append("protected instance meter")
    elif azure_service == "Azure Databricks":
        if "standard" in sku_text:
            delta += 25; reasons.append("standard databricks")
        if "all-purpose" in sku_text:
            delta += 12; reasons.append("all-purpose compute")
        if "photon" in sku_text:
            delta -= 10; reasons.append("photon meter")
        if any(word in sku_text for word in ("premium", "serverless", "free trial")):
            delta -= 30; reasons.append("non-standard databricks")
    elif azure_service == "Azure Machine Learning":
        if tier and tier in sku_text:
            delta += 15; reasons.append("ml tier")
        if "surcharge" in sku_text and Decimal(str(item.get("retailPrice", 0) or 0)) == 0:
            delta -= 30; reasons.append("zero surcharge")
        if "capacity unit" in sku_text:
            delta += 10; reasons.append("capacity unit")
    elif azure_service == "Azure Load Balancer":
        if "static ip" in sku_text and service.get("unit") == "hour":
            delta += 10; reasons.append("hourly network meter")
    elif azure_service in {"Azure Key Vault", "Key Vault"}:
        if "operations" in sku_text and "advanced" not in sku_text:
            delta += 25; reasons.append("key vault general operations meter")
        if any(word in sku_text for word in ("renewal", "rotation", "certificate", "hsm-protected", "advanced key")):
            delta -= 30; reasons.append("key vault specialized sub-meter")
    return delta


def filter_candidates(service: dict, items: list[dict], currency: str = "USD") -> list[dict]:
    candidates = []
    for item in items:
        if unit_multiplier(service.get("unit"), item.get("unitOfMeasure")) is None:
            continue
        score, reasons = _score_candidate(service, item, currency)
        if score > 0:
            summary = _meter_summary(item)
            summary["score"] = score
            summary["score_reasons"] = reasons
            candidates.append(summary)
    return sorted(candidates, key=lambda c: c["score"], reverse=True)


def _regional_lookup(service: dict) -> tuple[dict, bool]:
    """West US (or any configured SECONDARY_REGION) is never queried directly
    for pricing -- East US pricing is looked up instead and the synthetic
    0.95 multiplier is applied afterward (Phase 8.4.5 sections 3, 26-29)."""
    region = service.get("region")
    if region == SECONDARY_REGION:
        return dict(service, region=PRIMARY_REGION), True
    return service, False


def _apply_regional_modeling(monthly_cost: Decimal | None, retail_price: Any, is_secondary: bool):
    """Returns (monthly_cost, unit_price, pricing_method, base_unit_price, multiplier)."""
    base_price = Decimal(str(retail_price)) if retail_price not in (None, "") else None
    if not is_secondary:
        return monthly_cost, base_price, PRICING_METHOD_ACTUAL_AZURE_RETAIL, None, None
    multiplier = SECONDARY_REGION_PRICE_MULTIPLIER
    modeled_price = base_price * multiplier if base_price is not None else None
    modeled_cost = monthly_cost * multiplier if monthly_cost is not None else None
    return modeled_cost, modeled_price, PRICING_METHOD_MODEL_MULTIPLIER, base_price, multiplier


def _resolve_via_live_api(service: dict, get_func=None, currency: str = "USD", refresh: bool = False) -> dict:
    """Phase 8.4 live Azure Retail Prices API resolution -- unchanged scoring
    and exclusion behavior. Used directly when refresh=True (legacy/forced
    live path) and as step 3 of the Phase 8.4.5 snapshot-first order."""
    get_func = get_func or requests.get
    lookup_service, is_secondary = _regional_lookup(service)
    cache = _load_json(PRICING_CACHE_PATH, {})
    key = json.dumps({"service": lookup_service.get("azure_service"), "sku": lookup_service.get("sku"), "tier": lookup_service.get("tier"), "region": lookup_service.get("region"), "unit": lookup_service.get("unit"), "currency": currency}, sort_keys=True)
    if not refresh and key in cache:
        items = cache[key]["items"]
    else:
        try:
            items = call_retail_prices_api(build_query(lookup_service, currency), get_func=get_func)
            if not items:
                items = call_retail_prices_api(build_query(lookup_service, currency, include_price_type=False), get_func=get_func)
            cache[key] = {"cached_at": _utc_now(), "items": items}
            _write_json(PRICING_CACHE_PATH, cache)
        except Exception as exc:
            return _service_result(service, "API_ERROR", error=str(exc), currency=currency, pricing_source=PRICING_SOURCE_LIVE_AZURE_API, pricing_method=None)
    candidates = filter_candidates(lookup_service, items, currency)
    if not candidates:
        return _service_result(service, "NOT_FOUND", candidates=[], currency=currency, pricing_source=PRICING_SOURCE_LIVE_AZURE_API, pricing_method=None)
    top = candidates[0]
    if len(candidates) > 1 and candidates[1]["score"] >= top["score"] - 8 and Decimal(str(candidates[1].get("retailPrice", 0))) == Decimal(str(top.get("retailPrice", 0))):
        return _service_result(service, "AMBIGUOUS", candidates=candidates[:5], currency=currency, pricing_source=PRICING_SOURCE_LIVE_AZURE_API, pricing_method=None)
    cost = calculate_monthly_cost(lookup_service, top)
    if cost is None:
        return _service_result(service, "AMBIGUOUS", selected_meter=top, candidates=candidates[:5], error="Incompatible units", currency=currency, pricing_source=PRICING_SOURCE_LIVE_AZURE_API, pricing_method=None)
    modeled_cost, modeled_price, method, base_price, multiplier = _apply_regional_modeling(cost, top.get("retailPrice", top.get("unitPrice", 0)), is_secondary)
    return _service_result(
        service, "RESOLVED", selected_meter=top, monthly_cost=modeled_cost,
        confidence="HIGH" if top["score"] >= 100 else "MEDIUM", candidates=candidates[:3], currency=currency,
        pricing_source=PRICING_SOURCE_LIVE_AZURE_API, pricing_method=method, unit_price=modeled_price,
        base_region=PRIMARY_REGION if is_secondary else None, base_unit_price=base_price if is_secondary else None,
        secondary_multiplier=multiplier,
    )


def _resolve_via_snapshot(service: dict, currency: str = "USD") -> dict | None:
    """Local-first resolution (Phase 8.4.5 steps 1-2): deterministic exact
    match, else the deterministic best-scoring snapshot candidate. Returns
    None if no local snapshot is available. Reuses the exact same scoring
    and exclusion logic as the live API path via to_api_shape()."""
    snapshot = load_snapshot()
    if not snapshot or not snapshot.get("records"):
        return None
    lookup_service, is_secondary = _regional_lookup(service)
    raw_candidates = candidates_for_service(lookup_service)
    if not raw_candidates:
        return _service_result(service, "NOT_FOUND", candidates=[], currency=currency, pricing_source=PRICING_SOURCE_LOCAL_SNAPSHOT, pricing_method=None)
    items = [to_api_shape(r) for r in raw_candidates]
    candidates = filter_candidates(lookup_service, items, currency)
    if not candidates:
        return _service_result(service, "NOT_FOUND", candidates=[], currency=currency, pricing_source=PRICING_SOURCE_LOCAL_SNAPSHOT, pricing_method=None)
    top = candidates[0]
    is_ambiguous = len(candidates) > 1 and candidates[1]["score"] >= top["score"] - 8 and Decimal(str(candidates[1].get("retailPrice", 0))) == Decimal(str(top.get("retailPrice", 0)))
    snapshot_ts = snapshot.get("metadata", {}).get("snapshot_timestamp")
    exact_key_hit = bool(lookup_service.get("sku")) and top.get("armSkuName") == lookup_service.get("sku")
    if is_ambiguous:
        return _service_result(service, "AMBIGUOUS", candidates=candidates[:5], currency=currency, pricing_source=PRICING_SOURCE_LOCAL_SNAPSHOT, pricing_method=None, snapshot_timestamp=snapshot_ts)
    cost = calculate_monthly_cost(lookup_service, top)
    if cost is None:
        return _service_result(service, "AMBIGUOUS", selected_meter=top, candidates=candidates[:5], error="Incompatible units", currency=currency, pricing_source=PRICING_SOURCE_LOCAL_SNAPSHOT, pricing_method=None, snapshot_timestamp=snapshot_ts)
    modeled_cost, modeled_price, method, base_price, multiplier = _apply_regional_modeling(cost, top.get("retailPrice", top.get("unitPrice", 0)), is_secondary)
    confidence = "EXACT" if (exact_key_hit and top["score"] >= 100) else ("HIGH" if top["score"] >= 100 else "MEDIUM")
    return _service_result(
        service, "RESOLVED", selected_meter=top, monthly_cost=modeled_cost, confidence=confidence,
        candidates=candidates[:3], currency=currency, pricing_source=PRICING_SOURCE_LOCAL_SNAPSHOT, pricing_method=method,
        unit_price=modeled_price, base_region=PRIMARY_REGION if is_secondary else None,
        base_unit_price=base_price if is_secondary else None, secondary_multiplier=multiplier, snapshot_timestamp=snapshot_ts,
    )


def resolve_service_meter(service: dict, get_func=None, currency: str = "USD", refresh: bool = False) -> dict:
    """Phase 8.4.5 snapshot-first meter resolution.

    refresh=True forces the legacy Phase 8.4 live-API-only path (used by
    explicit "refresh pricing" actions and preserved for backward
    compatibility). refresh=False (the default) resolves in order:
      1. local snapshot exact match
      2. local snapshot deterministic best match (if confident: EXACT/HIGH)
      3. live Azure Retail Prices API
      4. AMBIGUOUS / NOT_FOUND

    If the live API errors but a local snapshot match exists (even at lower
    confidence), pricing continues from the snapshot rather than failing
    the whole operation (section 34).
    """
    if refresh:
        return _resolve_via_live_api(service, get_func=get_func, currency=currency, refresh=True)

    snapshot_result = _resolve_via_snapshot(service, currency=currency)
    if snapshot_result and snapshot_result["pricing_status"] in {"RESOLVED", "MANUAL_OVERRIDE"} and snapshot_result["resolution_confidence"] in {"EXACT", "HIGH"}:
        return snapshot_result

    try:
        live_result = _resolve_via_live_api(service, get_func=get_func, currency=currency, refresh=False)
    except Exception as exc:
        live_result = _service_result(service, "API_ERROR", error=str(exc), currency=currency, pricing_source=PRICING_SOURCE_LIVE_AZURE_API, pricing_method=None)

    if live_result["pricing_status"] in {"RESOLVED", "MANUAL_OVERRIDE"}:
        return live_result

    if live_result["pricing_status"] == "API_ERROR":
        if snapshot_result and snapshot_result["pricing_status"] in {"RESOLVED", "MANUAL_OVERRIDE"}:
            fallback = dict(snapshot_result)
            fallback["fallback_reason"] = "API_ERROR_LOCAL_SNAPSHOT_FALLBACK"
            return fallback
        if snapshot_result is None:
            return _service_result(service, "PRICING_UNAVAILABLE", error=live_result.get("error"), currency=currency, pricing_source=PRICING_SOURCE_LOCAL_SNAPSHOT, pricing_method=None)
        return live_result

    # Live API is reachable but produced no authoritative price: prefer
    # whichever result is more informative rather than silently trusting a
    # low-confidence local guess as authoritative.
    if snapshot_result and snapshot_result["pricing_status"] in {"RESOLVED", "MANUAL_OVERRIDE"}:
        return snapshot_result
    return live_result


def _money(value: Decimal | int | float | str) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _service_result(
    service: dict,
    status: str,
    selected_meter: dict | None = None,
    monthly_cost: Decimal | None = None,
    confidence: str = "LOW",
    candidates: list[dict] | None = None,
    error: str | None = None,
    currency: str = "USD",
    pricing_source: str = PRICING_SOURCE_LIVE_AZURE_API,
    pricing_method: str | None = None,
    unit_price: Decimal | str | None = None,
    base_region: str | None = None,
    base_unit_price: Decimal | str | None = None,
    secondary_multiplier: Decimal | str | None = None,
    snapshot_timestamp: str | None = None,
) -> dict:
    selected_meter = selected_meter or {}
    effective_unit_price = unit_price if unit_price is not None else selected_meter.get("retailPrice")
    if isinstance(effective_unit_price, Decimal):
        effective_unit_price = str(effective_unit_price)
    resolved = status in {"RESOLVED", "MANUAL_OVERRIDE"}
    result = {
        "service_id": service.get("service_id"),
        "azure_service": service.get("azure_service"),
        "pricing_status": status,
        # Phase 8.4.5: pricing_source distinguishes where the price came from
        # (LOCAL_SNAPSHOT / LIVE_AZURE_API / MANUAL_OVERRIDE); pricing_method
        # distinguishes actual Azure retail pricing from the synthetic West
        # US modeling assumption. West US is NEVER labeled as actual retail
        # pricing (section 3).
        "pricing_source": pricing_source,
        "pricing_method": pricing_method if pricing_method is not None else (PRICING_METHOD_ACTUAL_AZURE_RETAIL if resolved else None),
        "region": service.get("region"),
        "meter_id": selected_meter.get("meterId"),
        "meter_name": selected_meter.get("meterName"),
        "product_name": selected_meter.get("productName"),
        "sku_name": selected_meter.get("skuName"),
        "arm_sku_name": selected_meter.get("armSkuName"),
        "unit_of_measure": selected_meter.get("unitOfMeasure"),
        "retail_price": effective_unit_price,
        "unit_price": effective_unit_price,
        "currency": currency,
        "quantity": service.get("quantity"),
        "monthly_usage_per_unit": service.get("monthly_usage_per_unit"),
        "unit": service.get("unit"),
        "monthly_cost": _money(monthly_cost or 0),
        "resolution_confidence": confidence,
        "manual_override": status == "MANUAL_OVERRIDE",
    }
    if base_region is not None:
        result["base_region"] = base_region
    if base_unit_price is not None:
        result["base_unit_price"] = str(base_unit_price)
    if secondary_multiplier is not None:
        result["multiplier"] = str(secondary_multiplier)
    if snapshot_timestamp is not None:
        result["snapshot_timestamp"] = snapshot_timestamp
    if candidates is not None:
        result["candidates"] = candidates
    if error:
        result["error"] = error
    return result


def _architecture_signature(architecture: dict) -> str:
    rows = [
        {k: svc.get(k) for k in ("service_id", "azure_service", "sku", "tier", "region", "quantity", "monthly_usage_per_unit", "unit")}
        for svc in architecture.get("services", [])
    ]
    return json.dumps(rows, sort_keys=True)


def price_architecture(deal_id: str, get_func=None, currency: str = "USD", refresh: bool = False) -> dict:
    get_func = get_func or requests.get
    if not get_deal_input(deal_id):
        raise AzurePricingError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")
    if not get_consumption_profile(deal_id):
        raise AzurePricingError("CONSUMPTION_PROFILE_REQUIRED", "A workload profile is required before pricing.")
    record = get_architecture(deal_id)
    if not record or not record.get("architecture"):
        raise AzurePricingError("ARCHITECTURE_REQUIRED", "A validated architecture is required before pricing.")
    architecture = record["architecture"]
    services = [resolve_service_meter(svc, get_func=get_func, currency=currency, refresh=refresh) for svc in architecture.get("services", [])]
    resolved = [svc for svc in services if svc["pricing_status"] in {"RESOLVED", "MANUAL_OVERRIDE"}]
    total = sum(Decimal(str(svc["monthly_cost"])) for svc in resolved)
    pricing_sources_used = sorted({svc.get("pricing_source") for svc in services if svc.get("pricing_source")})
    snapshot_timestamps_used = sorted({svc.get("snapshot_timestamp") for svc in services if svc.get("snapshot_timestamp")})
    snapshot_meta = snapshot_status(load_snapshot())
    result = {
        "deal_id": deal_id,
        "architecture_version": architecture.get("architecture_version", "1.0"),
        "architecture_signature": _architecture_signature(architecture),
        "currency": currency,
        # Legacy label preserved for backward compatibility with any
        # existing consumer; pricing_sources_used is the authoritative
        # Phase 8.4.5 per-run breakdown (a run may mix LOCAL_SNAPSHOT,
        # LIVE_AZURE_API, and MANUAL_OVERRIDE across services).
        "pricing_source": "azure_retail_prices_api",
        "pricing_sources_used": pricing_sources_used,
        "primary_region": PRIMARY_REGION,
        "secondary_region": SECONDARY_REGION,
        "secondary_region_multiplier": str(SECONDARY_REGION_PRICE_MULTIPLIER),
        "snapshot_timestamp": snapshot_timestamps_used[-1] if snapshot_timestamps_used else None,
        "snapshot_status": snapshot_meta.get("status"),
        "priced_at": _utc_now(),
        "services": services,
        "baseline_monthly_cost": _money(total),
        "pricing_complete": len(resolved) == len(services),
        "resolved_services": len(resolved),
        "total_services": len(services),
        "pricing_status": "COMPLETE" if len(resolved) == len(services) else "INCOMPLETE",
        "stale": False,
    }
    save_pricing(result)
    print(f"[AzurePricing] deal_id={deal_id} status={result['pricing_status']} resolved={len(resolved)}/{len(services)}")
    return result


def load_pricing_store() -> dict:
    return _load_json(PRICING_STORE_PATH, {})


def save_pricing(result: dict) -> dict:
    store = load_pricing_store()
    store[result["deal_id"]] = result
    _write_json(PRICING_STORE_PATH, store)
    try:
        from tools.consumption_forecast import invalidate_forecast
    except ModuleNotFoundError:
        from consumption_forecast import invalidate_forecast
    invalidate_forecast(result["deal_id"])
    return result


def get_pricing(deal_id: str) -> dict | None:
    result = load_pricing_store().get(deal_id)
    arch = get_architecture(deal_id)
    if result and arch and result.get("architecture_signature") != _architecture_signature(arch.get("architecture", {})):
        result = {**result, "stale": True, "pricing_status": "STALE"}
    return result


def invalidate_pricing(deal_id: str) -> None:
    store = load_pricing_store()
    if deal_id in store:
        store[deal_id]["stale"] = True
        store[deal_id]["pricing_status"] = "STALE"
        _write_json(PRICING_STORE_PATH, store)
    try:
        from tools.consumption_forecast import invalidate_forecast
    except ModuleNotFoundError:
        from consumption_forecast import invalidate_forecast
    invalidate_forecast(deal_id)


def apply_manual_meter(deal_id: str, service_id: str, meter: dict) -> dict:
    pricing = get_pricing(deal_id)
    arch = get_architecture(deal_id)
    if not pricing or not arch:
        raise AzurePricingError("PRICING_REQUIRED", "Pricing and architecture are required before manual resolution.")
    architecture_service = next((svc for svc in arch["architecture"].get("services", []) if svc.get("service_id") == service_id), None)
    if not architecture_service:
        raise AzurePricingError("SERVICE_NOT_FOUND", f"No architecture service found for {service_id}.")
    cost = calculate_monthly_cost(architecture_service, meter)
    if cost is None:
        raise AzurePricingError("INCOMPATIBLE_UNITS", "Selected meter unit is not compatible with the BOM unit.")
    replacement = _service_result(architecture_service, "MANUAL_OVERRIDE", selected_meter=_meter_summary(meter), monthly_cost=cost, confidence="HIGH", currency=pricing.get("currency", "USD"), pricing_source=PRICING_SOURCE_MANUAL_OVERRIDE)
    services = [replacement if svc.get("service_id") == service_id else svc for svc in pricing["services"]]
    resolved = [svc for svc in services if svc["pricing_status"] in {"RESOLVED", "MANUAL_OVERRIDE"}]
    pricing.update({
        "services": services,
        "baseline_monthly_cost": _money(sum(Decimal(str(svc["monthly_cost"])) for svc in resolved)),
        "resolved_services": len(resolved),
        "pricing_complete": len(resolved) == len(services),
        "pricing_status": "COMPLETE" if len(resolved) == len(services) else "INCOMPLETE",
        "priced_at": _utc_now(),
        "stale": False,
    })
    return save_pricing(pricing)
