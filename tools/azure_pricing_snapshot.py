"""Phase 8.4.5 Azure East US master pricing snapshot.

Downloads, normalizes, and persists a durable local copy of the Azure
Retail Prices API for East US PAYG Consumption pricing (plus an approved
set of global/non-regional meters), builds deterministic in-memory
indexes for meter resolution, and models West US pricing as a synthetic
0.95x multiplier over East US retail pricing.

This module owns the master snapshot file. It does NOT decide which
services belong in the curated architecture catalog (see
tools/azure_enterprise_catalog.py) and it does NOT replace the Phase 8.4
live Azure Retail Prices API client in tools/azure_retail_pricing.py --
it is consulted first, before that client is used.
"""
from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

try:
    from tools.azure_pricing_config import (
        EXCLUDED_SKU_TOKENS,
        PRIMARY_REGION,
        SECONDARY_REGION,
        SECONDARY_REGION_PRICE_MULTIPLIER,
        SECONDARY_REGION_PRICING_MODE,
        SNAPSHOT_PATH,
        SNAPSHOT_PRICE_TYPE,
        SNAPSHOT_STALE_AFTER_DAYS,
        SNAPSHOT_STATUS_COMPLETE,
        SNAPSHOT_STATUS_ERROR,
        SNAPSHOT_STATUS_PARTIAL,
        SNAPSHOT_STATUS_STALE,
        PRICING_REGION_SCOPE_EASTUS,
        PRICING_REGION_SCOPE_GLOBAL,
        model_westus_price,
    )
except ModuleNotFoundError:
    from azure_pricing_config import (
        EXCLUDED_SKU_TOKENS,
        PRIMARY_REGION,
        SECONDARY_REGION,
        SECONDARY_REGION_PRICE_MULTIPLIER,
        SECONDARY_REGION_PRICING_MODE,
        SNAPSHOT_PATH,
        SNAPSHOT_PRICE_TYPE,
        SNAPSHOT_STALE_AFTER_DAYS,
        SNAPSHOT_STATUS_COMPLETE,
        SNAPSHOT_STATUS_ERROR,
        SNAPSHOT_STATUS_PARTIAL,
        SNAPSHOT_STATUS_STALE,
        PRICING_REGION_SCOPE_EASTUS,
        PRICING_REGION_SCOPE_GLOBAL,
        model_westus_price,
    )

BASE_URL = "https://prices.azure.com/api/retail/prices"
SNAPSHOT_SCHEMA_VERSION = "1.0"
MAX_PAGES_PER_QUERY = 500  # safety ceiling, not a target -- real data is a few dozen pages


class SnapshotError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

def _paginate(filter_str: str, get_func, max_pages: int = MAX_PAGES_PER_QUERY) -> tuple[list[dict], int]:
    """Fetch every page for an OData filter. Returns (items, pages_fetched)."""
    get_func = get_func or requests.get
    url = f"{BASE_URL}?$filter={filter_str}"
    items: list[dict] = []
    pages = 0
    for _ in range(max_pages):
        response = get_func(url, timeout=30)
        response.raise_for_status()
        payload = response.json()
        page_items = payload.get("Items")
        if not isinstance(page_items, list):
            raise SnapshotError("MALFORMED_PRICING_RESPONSE", "Azure pricing response did not contain Items.")
        items.extend(page_items)
        pages += 1
        url = payload.get("NextPageLink")
        if not url:
            break
    return items, pages


def fetch_primary_region_records(get_func=None, region: str = PRIMARY_REGION) -> tuple[list[dict], int]:
    """All type=Consumption records scoped to the primary region (paginated in full)."""
    filter_str = f"armRegionName eq '{region}' and type eq '{SNAPSHOT_PRICE_TYPE}'"
    return _paginate(filter_str, get_func)


def fetch_global_records(get_func=None) -> tuple[list[dict], int]:
    """Approved global/non-regional type=Consumption records (armRegionName == '')."""
    filter_str = f"armRegionName eq '' and type eq '{SNAPSHOT_PRICE_TYPE}'"
    return _paginate(filter_str, get_func)


# --------------------------------------------------------------------------
# Exclusion / normalization (sections 6, 9, 10)
# --------------------------------------------------------------------------

def _sku_text(item: dict) -> str:
    return " ".join(str(item.get(k) or "") for k in ("skuName", "meterName", "productName", "armSkuName")).lower()


def exclusion_reason(item: dict) -> str | None:
    """Returns the reason a raw Azure record is excluded from the PAYG default catalog, or None."""
    if item.get("type") and item.get("type") != SNAPSHOT_PRICE_TYPE:
        return "non_consumption_type"
    if item.get("reservationTerm"):
        return "reservation_term"
    text = _sku_text(item)
    for token in EXCLUDED_SKU_TOKENS:
        if token in text:
            return "excluded_token:" + token
    price = item.get("retailPrice", item.get("unitPrice"))
    try:
        if price is not None and Decimal(str(price)) < 0:
            return "negative_price"
    except Exception:
        return "malformed_price"
    if not item.get("serviceName") or not item.get("meterId"):
        return "missing_required_fields"
    return None


def normalize_record(item: dict, region_scope: str) -> dict:
    """Normalize one raw Azure Retail Prices item into the Phase 8.4.5 snapshot schema."""
    retail_price = Decimal(str(item.get("retailPrice", item.get("unitPrice", 0)) or 0))
    westus_price = model_westus_price(retail_price)
    return {
        # normalized identity / classification
        "meter_id": item.get("meterId"),
        "service_family": item.get("serviceFamily"),
        "service_name": item.get("serviceName"),
        "product_name": item.get("productName"),
        "sku_name": item.get("skuName"),
        "arm_sku_name": item.get("armSkuName") or None,
        "meter_name": item.get("meterName"),
        "arm_region_name": item.get("armRegionName") or None,
        "location": item.get("location"),
        "unit_of_measure": item.get("unitOfMeasure"),
        "price_type": item.get("type"),
        # East US = actual Azure retail pricing
        "eastus_retail_price": str(retail_price),
        # West US = synthetic modeled pricing, never real Azure retail data
        "secondary_region": SECONDARY_REGION,
        "westus_modeled_price": str(westus_price),
        "secondary_region_multiplier": str(SECONDARY_REGION_PRICE_MULTIPLIER),
        "secondary_region_pricing_method": SECONDARY_REGION_PRICING_MODE,
        "currency": item.get("currencyCode") or "USD",
        "effective_start_date": item.get("effectiveStartDate"),
        "pricing_region_scope": region_scope,
        # preserved Azure source fields (section 10)
        "tier_minimum_units": item.get("tierMinimumUnits"),
        "product_id": item.get("productId"),
        "sku_id": item.get("skuId"),
        "service_id": item.get("serviceId"),
        "is_primary_meter_region": item.get("isPrimaryMeterRegion"),
    }


# --------------------------------------------------------------------------
# Build snapshot
# --------------------------------------------------------------------------

def build_snapshot(get_func=None) -> dict:
    """Download + normalize the full applicable East US snapshot. No file I/O."""
    eastus_raw, eastus_pages = fetch_primary_region_records(get_func)
    global_raw, global_pages = fetch_global_records(get_func)

    excluded_counts: dict[str, int] = {}
    seen_meter_ids: set[str] = set()
    records: list[dict] = []

    def process(raw_items: list[dict], scope: str):
        for item in raw_items:
            reason = exclusion_reason(item)
            if reason:
                excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
                continue
            meter_id = item.get("meterId")
            if meter_id in seen_meter_ids:
                excluded_counts["duplicate_meter_id"] = excluded_counts.get("duplicate_meter_id", 0) + 1
                continue
            seen_meter_ids.add(meter_id)
            records.append(normalize_record(item, scope))

    process(eastus_raw, PRICING_REGION_SCOPE_EASTUS)
    process(global_raw, PRICING_REGION_SCOPE_GLOBAL)

    metadata = {
        "source": "Azure Retail Prices API",
        "currency": "USD",
        "primary_region": PRIMARY_REGION,
        "secondary_region": SECONDARY_REGION,
        "secondary_region_pricing_method": SECONDARY_REGION_PRICING_MODE,
        "secondary_region_multiplier": str(SECONDARY_REGION_PRICE_MULTIPLIER),
        "snapshot_timestamp": _utc_now(),
        "record_count": len(records),
        "eastus_record_count": sum(1 for r in records if r["pricing_region_scope"] == PRICING_REGION_SCOPE_EASTUS),
        "global_record_count": sum(1 for r in records if r["pricing_region_scope"] == PRICING_REGION_SCOPE_GLOBAL),
        "api_pages": eastus_pages + global_pages,
        "api_pages_primary_region": eastus_pages,
        "api_pages_global": global_pages,
        "raw_record_count": len(eastus_raw) + len(global_raw),
        "raw_primary_region_count": len(eastus_raw),
        "raw_global_count": len(global_raw),
        "excluded_record_counts": excluded_counts,
        "excluded_record_total": sum(excluded_counts.values()),
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "status": SNAPSHOT_STATUS_COMPLETE if records else SNAPSHOT_STATUS_ERROR,
    }
    return {"metadata": metadata, "records": records}


def validate_snapshot(snapshot: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(snapshot, dict):
        return ["snapshot must be an object"]
    metadata = snapshot.get("metadata")
    records = snapshot.get("records")
    if not isinstance(metadata, dict):
        errors.append("metadata is required")
    if not isinstance(records, list):
        errors.append("records must be a list")
        return errors
    if not records:
        errors.append("records must not be empty")
    if metadata and metadata.get("record_count") != len(records):
        errors.append("metadata.record_count does not match records length")
    for i, record in enumerate(records[:5000]):  # spot-check bound to keep validation fast on huge snapshots
        if not record.get("meter_id"):
            errors.append(f"records[{i}] missing meter_id")
        if not record.get("service_name"):
            errors.append(f"records[{i}] missing service_name")
        try:
            Decimal(str(record.get("eastus_retail_price")))
            Decimal(str(record.get("westus_modeled_price")))
        except Exception:
            errors.append(f"records[{i}] has non-decimal-safe pricing")
        if record.get("pricing_region_scope") not in (PRICING_REGION_SCOPE_EASTUS, PRICING_REGION_SCOPE_GLOBAL):
            errors.append(f"records[{i}] has invalid pricing_region_scope")
    return errors


# --------------------------------------------------------------------------
# Atomic persistence (section 13)
# --------------------------------------------------------------------------

def atomic_write_snapshot(snapshot: dict, path: Path = SNAPSHOT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".azure_eastus_snapshot_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_name, path)  # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def refresh_snapshot(get_func=None, path: Path = SNAPSHOT_PATH) -> dict:
    """Download -> validate -> atomically replace. Retains the previous valid
    snapshot on any failure (section 13, 34, 35)."""
    try:
        snapshot = build_snapshot(get_func=get_func)
        errors = validate_snapshot(snapshot)
        if errors:
            snapshot["metadata"]["status"] = SNAPSHOT_STATUS_PARTIAL if snapshot["records"] else SNAPSHOT_STATUS_ERROR
            snapshot["metadata"]["validation_errors"] = errors
        atomic_write_snapshot(snapshot, path)
        _invalidate_cache()
        return {
            "refresh_status": "SUCCESS" if not errors else "PARTIAL",
            "snapshot": snapshot["metadata"],
            "validation_errors": errors,
        }
    except Exception as exc:
        previous = load_snapshot(path=path, allow_missing=True)
        return {
            "refresh_status": "FAILED",
            "error": str(exc),
            "previous_snapshot_available": bool(previous and previous.get("records")),
            "previous_snapshot_metadata": (previous or {}).get("metadata"),
        }


# --------------------------------------------------------------------------
# Load + status
# --------------------------------------------------------------------------

_CACHE: dict[str, Any] = {"path": None, "mtime": None, "snapshot": None, "indexes": None}


def _invalidate_cache() -> None:
    _CACHE.update({"path": None, "mtime": None, "snapshot": None, "indexes": None})


def load_snapshot(path: Path = SNAPSHOT_PATH, allow_missing: bool = True) -> dict | None:
    if not path.exists():
        if allow_missing:
            return None
        raise SnapshotError("SNAPSHOT_MISSING", f"No snapshot found at {path}")
    mtime = path.stat().st_mtime
    if _CACHE["path"] == str(path) and _CACHE["mtime"] == mtime and _CACHE["snapshot"] is not None:
        return _CACHE["snapshot"]
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    _CACHE.update({"path": str(path), "mtime": mtime, "snapshot": snapshot, "indexes": None})
    return snapshot


def snapshot_age_days(snapshot: dict) -> float | None:
    ts = (snapshot or {}).get("metadata", {}).get("snapshot_timestamp")
    if not ts:
        return None
    try:
        stamp = datetime.fromisoformat(ts)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds() / 86400.0
    except ValueError:
        return None


def snapshot_status(snapshot: dict | None, path: Path = SNAPSHOT_PATH) -> dict:
    """Recomputed status, distinguishing stored status from live staleness (section 14, 36)."""
    if not snapshot:
        return {
            "status": "MISSING",
            "record_count": 0,
            "primary_region": PRIMARY_REGION,
            "secondary_region": SECONDARY_REGION,
            "secondary_region_multiplier": str(SECONDARY_REGION_PRICE_MULTIPLIER),
            "snapshot_timestamp": None,
            "currency": "USD",
        }
    metadata = deepcopy(snapshot.get("metadata", {}))
    age_days = snapshot_age_days(snapshot)
    status = metadata.get("status", SNAPSHOT_STATUS_COMPLETE)
    if age_days is not None and age_days > SNAPSHOT_STALE_AFTER_DAYS and status == SNAPSHOT_STATUS_COMPLETE:
        status = SNAPSHOT_STATUS_STALE
    metadata["status"] = status
    metadata["snapshot_age_days"] = round(age_days, 2) if age_days is not None else None
    metadata["stale_after_days"] = SNAPSHOT_STALE_AFTER_DAYS
    return metadata


# --------------------------------------------------------------------------
# Indexes (section 11)
# --------------------------------------------------------------------------

def build_indexes(records: list[dict]) -> dict:
    by_meter_id: dict[str, dict] = {}
    by_service_name: dict[str, list[dict]] = {}
    by_service_sku: dict[tuple, list[dict]] = {}
    by_service_arm_sku: dict[tuple, list[dict]] = {}
    by_service_product_sku: dict[tuple, list[dict]] = {}
    by_service_family: dict[str, list[dict]] = {}
    by_region: dict[str, list[dict]] = {}
    by_unit: dict[str, list[dict]] = {}

    for r in records:
        if r.get("meter_id"):
            by_meter_id[r["meter_id"]] = r
        service_name = r.get("service_name")
        if service_name:
            by_service_name.setdefault(service_name, []).append(r)
            if r.get("sku_name"):
                by_service_sku.setdefault((service_name, r["sku_name"]), []).append(r)
            if r.get("arm_sku_name"):
                by_service_arm_sku.setdefault((service_name, r["arm_sku_name"]), []).append(r)
            if r.get("product_name") and r.get("sku_name"):
                by_service_product_sku.setdefault((service_name, r["product_name"], r["sku_name"]), []).append(r)
        if r.get("service_family"):
            by_service_family.setdefault(r["service_family"], []).append(r)
        region_key = r.get("arm_region_name") or r.get("pricing_region_scope")
        by_region.setdefault(region_key, []).append(r)
        if r.get("unit_of_measure"):
            by_unit.setdefault(r["unit_of_measure"], []).append(r)

    return {
        "by_meter_id": by_meter_id,
        "by_service_name": by_service_name,
        "by_service_sku": by_service_sku,
        "by_service_arm_sku": by_service_arm_sku,
        "by_service_product_sku": by_service_product_sku,
        "by_service_family": by_service_family,
        "by_region": by_region,
        "by_unit": by_unit,
    }


def get_indexes(path: Path = SNAPSHOT_PATH) -> dict | None:
    snapshot = load_snapshot(path=path)
    if not snapshot:
        return None
    if _CACHE.get("indexes") is not None and _CACHE.get("path") == str(path):
        return _CACHE["indexes"]
    indexes = build_indexes(snapshot.get("records", []))
    _CACHE["indexes"] = indexes
    return indexes


# --------------------------------------------------------------------------
# Lookup (used by tools/azure_retail_pricing.py for the local-first steps)
# --------------------------------------------------------------------------

def find_exact_candidates(service: dict, path: Path = SNAPSHOT_PATH) -> list[dict]:
    """Deterministic exact-key lookups only, in preference order. Region is
    ignored here on purpose: the snapshot only stores the primary (East US)
    and approved global scope -- regional modeling for West US is applied by
    the caller after a record is found."""
    indexes = get_indexes(path)
    if not indexes:
        return []
    azure_service = service.get("azure_service")
    sku = service.get("sku")
    if not azure_service:
        return []
    if sku and (azure_service, sku) in indexes["by_service_arm_sku"]:
        return indexes["by_service_arm_sku"][(azure_service, sku)]
    if sku and (azure_service, sku) in indexes["by_service_sku"]:
        return indexes["by_service_sku"][(azure_service, sku)]
    return []


def candidates_for_service(service: dict, path: Path = SNAPSHOT_PATH) -> list[dict]:
    """All snapshot records worth scoring for a service (by service_name)."""
    indexes = get_indexes(path)
    if not indexes:
        return []
    return list(indexes["by_service_name"].get(service.get("azure_service"), []))


def to_api_shape(record: dict) -> dict:
    """Project a normalized snapshot record back into the raw Azure Retail
    Prices API shape so the existing Phase 8.4 scoring/exclusion logic in
    tools/azure_retail_pricing.py can be reused unmodified (section 1)."""
    return {
        "currencyCode": record.get("currency"),
        "retailPrice": record.get("eastus_retail_price"),
        "unitPrice": record.get("eastus_retail_price"),
        "unitOfMeasure": record.get("unit_of_measure"),
        "tierMinimumUnits": record.get("tier_minimum_units"),
        "armRegionName": record.get("arm_region_name") or PRIMARY_REGION,
        "location": record.get("location"),
        "effectiveStartDate": record.get("effective_start_date"),
        "meterId": record.get("meter_id"),
        "meterName": record.get("meter_name"),
        "productId": record.get("product_id"),
        "skuId": record.get("sku_id"),
        "productName": record.get("product_name"),
        "skuName": record.get("sku_name"),
        "serviceName": record.get("service_name"),
        "serviceId": record.get("service_id"),
        "serviceFamily": record.get("service_family"),
        "unitOfMeasure": record.get("unit_of_measure"),
        "priceType": record.get("price_type"),
        "armSkuName": record.get("arm_sku_name"),
        "pricing_region_scope": record.get("pricing_region_scope"),
    }
