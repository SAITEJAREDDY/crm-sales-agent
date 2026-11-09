"""Cached, normalized Azure Retail Prices catalog used to constrain architecture BOMs."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from tools.azure_retail_pricing import call_retail_prices_api

BASE_DIR = Path(__file__).resolve().parent.parent
CATALOG_PATH = BASE_DIR / "knowledge_base" / "commercial_strategy" / "azure_service_catalog.json"

def normalize_catalog_items(items: list[dict]) -> dict:
    records, seen = [], set()
    for item in items:
        row = {"service_family": item.get("serviceFamily"), "service_name": item.get("serviceName"),
               "product_name": item.get("productName"), "sku_name": item.get("skuName"),
               "arm_sku_name": item.get("armSkuName"), "meter_name": item.get("meterName"),
               "region": item.get("armRegionName"), "unit": item.get("unitOfMeasure"),
               "price_type": item.get("priceType"), "meter_id": item.get("meterId")}
        key = tuple(row.get(k) for k in ("service_name", "arm_sku_name", "meter_name", "region", "unit", "price_type"))
        if key not in seen and row["service_name"]:
            seen.add(key); records.append(row)
    return {"version": "1.0", "records": records, "count": len(records)}

def save_catalog(catalog: dict) -> dict:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True); CATALOG_PATH.write_text(json.dumps(catalog, indent=2) + "\n")
    return catalog

def load_catalog() -> dict:
    if not CATALOG_PATH.exists(): return {"version": "1.0", "records": [], "count": 0}
    return json.loads(CATALOG_PATH.read_text())

def refresh_catalog(filter_str: str, get_func=None) -> dict:
    return save_catalog(normalize_catalog_items(call_retail_prices_api(filter_str, get_func=get_func)))

def find_catalog_options(requirement: str, region: str | None = None, limit: int = 25) -> list[dict]:
    term = requirement.lower(); rows = load_catalog().get("records", [])
    rows = [r for r in rows if (not region or r.get("region") == region) and term in " ".join(str(v or "") for v in r.values()).lower()]
    return rows[:limit]

def validate_catalog_service(service: dict, catalog: dict | None = None) -> list[str]:
    rows = (catalog or load_catalog()).get("records", [])
    if not rows: return []
    matches = [r for r in rows if r.get("service_name") == service.get("azure_service") and (not service.get("sku") or r.get("arm_sku_name") == service.get("sku") or r.get("sku_name") == service.get("sku")) and (not service.get("region") or r.get("region") == service.get("region"))]
    return [] if matches else [f"unsupported catalog service/SKU: {service.get('azure_service')} / {service.get('sku')} / {service.get('region')}" ]
