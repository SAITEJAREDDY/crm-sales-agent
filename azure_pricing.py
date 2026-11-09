"""
azure_pricing.py
Fetches Azure Retail Prices for services found in SOW Section 5.
Always uses eastus region.

CRITICAL: $filter must be passed directly in the URL string.
Never use requests params dict — it encodes $ as %24 and breaks the API.

  CORRECT:  requests.get(f"{BASE_URL}?$filter={filter_str}", timeout=5)
  BROKEN:   requests.get(BASE_URL, params={"$filter": filter_str})
"""

from __future__ import annotations

import sys
from typing import Optional

import requests

BASE_URL = "https://prices.azure.com/api/retail/prices"

# ── SOW display name → Azure Retail Prices API serviceName ───────────────────
_SERVICE_MAP: dict[str, str] = {
    "Azure Functions":            "Functions",
    "Azure Virtual Machines":     "Virtual Machines",
    "Azure Blob Storage":         "Storage",
    "Azure API Management":       "API Management",
    "Azure Key Vault":            "Key Vault",
    "Azure Monitor":              "Azure Monitor",
    "Azure CosmosDB":             "Azure Cosmos DB",
    "Azure Kubernetes Service":   "Azure Kubernetes Service",
    "Azure Container Apps":       "Azure Container Apps",
    "Azure AI Search":            "Azure Cognitive Search",
    "Azure Data Factory":         "Azure Data Factory",
    "Azure Service Bus":          "Service Bus",
    "Azure Synapse Analytics":    "Azure Synapse Analytics",
    "Azure Data Transfer":        "Bandwidth",
}

# ── Services with no useful API results — use standard estimates directly ─────
# Never attempt an API call for these.
_HARDCODED: dict[str, dict] = {
    "Azure OpenAI Service": {
        "sku": "GPT-4o Standard Input",
        "unit": "1K tokens",
        "unit_rate": 0.005,
        "monthly_estimate": 500.00,
        "annual_estimate": 6000.00,
        "source": "Standard estimate — Azure pricing page",
    },
    "Azure AI Foundry": {
        "sku": "GPT-4o Standard Input",
        "unit": "1K tokens",
        "unit_rate": 0.005,
        "monthly_estimate": 500.00,
        "annual_estimate": 6000.00,
        "source": "Standard estimate — Azure pricing page",
    },
    "Azure SQL Database": {
        "sku": "General Purpose 4 vCores",
        "unit": "1 Hour",
        "unit_rate": 0.724,
        "monthly_estimate": 528.52,
        "annual_estimate": 6342.24,
        "source": "Standard estimate — Azure pricing page",
    },
    "Microsoft Entra ID": {
        "sku": "P1 per user (50 users)",
        "unit": "1 User/Month",
        "unit_rate": 6.00,
        "monthly_estimate": 300.00,
        "annual_estimate": 3600.00,
        "source": "Standard estimate — Azure pricing page",
    },
    "Azure DevOps": {
        "sku": "Basic Plan per user (30 users)",
        "unit": "1 User/Month",
        "unit_rate": 6.00,
        "monthly_estimate": 180.00,
        "annual_estimate": 2160.00,
        "source": "Standard estimate — Azure pricing page",
    },
    "PostgreSQL": {
        "sku": "Azure Database for PostgreSQL Flexible — 4 vCores",
        "unit": "1 Hour",
        "unit_rate": 0.362,
        "monthly_estimate": 264.26,
        "annual_estimate": 3171.12,
        "source": "Standard estimate — Azure pricing page",
    },
    "MongoDB": {
        "sku": "Azure Cosmos DB for MongoDB — Standard",
        "unit": "1 Hour",
        "unit_rate": 0.008,
        "monthly_estimate": 200.00,
        "annual_estimate": 2400.00,
        "source": "Standard estimate — Azure pricing page",
    },
    "Redis": {
        "sku": "Azure Cache for Redis — C1 Standard",
        "unit": "1 Hour",
        "unit_rate": 0.068,
        "monthly_estimate": 49.64,
        "annual_estimate": 595.68,
        "source": "Standard estimate — Azure pricing page",
    },
    "Docker": {
        "sku": "Open source — no Azure licensing cost",
        "unit": "—",
        "unit_rate": 0.00,
        "monthly_estimate": 0.00,
        "annual_estimate": 0.00,
        "source": "Open source — no cost",
    },
    "Terraform": {
        "sku": "Open source — no Azure licensing cost",
        "unit": "—",
        "unit_rate": 0.00,
        "monthly_estimate": 0.00,
        "annual_estimate": 0.00,
        "source": "Open source — no cost",
    },
    "LangChain": {
        "sku": "Open source — no Azure licensing cost",
        "unit": "—",
        "unit_rate": 0.00,
        "monthly_estimate": 0.00,
        "annual_estimate": 0.00,
        "source": "Open source — no cost",
    },
    "LlamaIndex": {
        "sku": "Open source — no Azure licensing cost",
        "unit": "—",
        "unit_rate": 0.00,
        "monthly_estimate": 0.00,
        "annual_estimate": 0.00,
        "source": "Open source — no cost",
    },
}

_DEFAULT_FALLBACK: dict = {
    "sku": "Standard tier — verify on Azure pricing page",
    "unit": "1 Month",
    "unit_rate": 200.00,
    "monthly_estimate": 200.00,
    "annual_estimate": 2400.00,
    "source": "Default estimate — not found in API",
}

# Module-level cache to avoid duplicate API calls within a single request
_PRICE_CACHE: dict[str, dict] = {}


def _pick_best_sku(items: list) -> Optional[dict]:
    if not items:
        return None
    filtered = [
        i for i in items
        if not any(x in i.get("skuName", "") for x in [
            "Spot", "Low Priority", "Dev/Test", "Windows", "Free Trial", "Basic",
        ])
        and i.get("retailPrice", 0) > 0
        and i.get("isPrimaryMeterRegion", False)
    ]
    if not filtered:
        filtered = [i for i in items if i.get("retailPrice", 0) > 0]
    if not filtered:
        return items[0]
    standard = [i for i in filtered if "Standard" in i.get("skuName", "")]
    return standard[0] if standard else filtered[0]


def _monthly_from_rate(unit_rate: float, uom: str) -> float:
    u = (uom or "").strip()
    if u == "1 Hour":              return unit_rate * 730
    if u == "1 GB":                return unit_rate * 100
    if u == "1 GB Second":         return unit_rate * 100 * 730
    if u == "1 GB/Month":          return unit_rate * 100
    if u in ("1/Month", "1 Unit"): return unit_rate
    if u == "1K Operations":       return unit_rate * 1000
    if u == "1M Operations":       return unit_rate
    if u == "10K Transactions":    return unit_rate * 100
    if u == "1 User/Month":        return unit_rate * 50
    return unit_rate * 730  # default: assume hourly


def get_azure_service_price(service_name: str) -> dict:
    """
    Returns:
    {
      "service": str,
      "sku": str,
      "unit": str,
      "unit_rate": float,
      "monthly_estimate": float,
      "annual_estimate": float,
      "source": str
    }
    Never raises. Always returns a valid dict.
    """
    if service_name in _PRICE_CACHE:
        return _PRICE_CACHE[service_name]

    # Hardcoded services — skip API entirely
    if service_name in _HARDCODED:
        result = {"service": service_name, **_HARDCODED[service_name]}
        _PRICE_CACHE[service_name] = result
        return result

    # Map to API service name (fall back to the service name itself for unknowns)
    api_name = _SERVICE_MAP.get(service_name, service_name)

    try:
        filter_str = f"serviceName eq '{api_name}' and armRegionName eq 'eastus'"
        resp = requests.get(f"{BASE_URL}?$filter={filter_str}", timeout=5)
        resp.raise_for_status()
        items = resp.json().get("Items", [])
        best = _pick_best_sku(items)
        if best:
            unit_rate = float(best.get("retailPrice", 0))
            uom = best.get("unitOfMeasure", "1 Hour")
            monthly = _monthly_from_rate(unit_rate, uom)
            result = {
                "service":          service_name,
                "sku":              best.get("skuName", "Standard"),
                "unit":             uom,
                "unit_rate":        round(unit_rate, 4),
                "monthly_estimate": round(monthly, 2),
                "annual_estimate":  round(monthly * 12, 2),
                "source":           "Azure Retail Prices API — eastus",
            }
            _PRICE_CACHE[service_name] = result
            return result
    except Exception as e:
        print(f"[azure_pricing] API call failed for '{service_name}': {e}", file=sys.stderr)

    result = {"service": service_name, **_DEFAULT_FALLBACK}
    _PRICE_CACHE[service_name] = result
    return result


def get_prices_for_sow(services: list) -> list:
    """
    Calls get_azure_service_price for each service.
    Never raises — catches all exceptions per service.
    Caches results to avoid duplicate API calls.
    Prints summary table to stdout.
    Returns list of dicts in same order as input.
    """
    results = []
    print(
        f"\n{'Service':<35} {'SKU':<42} {'Monthly':>10} {'Annual':>12}  Source"
    )
    print("-" * 112)
    for svc in services:
        try:
            p = get_azure_service_price(svc)
        except Exception as e:
            print(f"[azure_pricing] Unexpected error for '{svc}': {e}", file=sys.stderr)
            p = {"service": svc, **_DEFAULT_FALLBACK}
        results.append(p)
        print(
            f"{p['service']:<35} {p['sku']:<42} "
            f"${p['monthly_estimate']:>9,.2f} ${p['annual_estimate']:>11,.2f}  {p['source']}"
        )
    print("-" * 112)
    total_m = sum(p.get("monthly_estimate", 0) for p in results)
    total_a = sum(p.get("annual_estimate",  0) for p in results)
    print(f"{'TOTAL':<35} {'':<42} ${total_m:>9,.2f} ${total_a:>11,.2f}")
    print()
    return results


# ── Standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_services = [
        "Azure API Management",
        "Azure Key Vault",
        "Azure OpenAI Service",
        "PostgreSQL",
        "Azure Monitor",
        "Docker",
    ]
    print("Testing azure_pricing.py against Azure Retail Prices API...\n")
    prices = get_prices_for_sow(test_services)
    print("\nDetailed results:")
    for p in prices:
        print(
            f"  {p['service']}: ${p['monthly_estimate']:,.2f}/mo  "
            f"({p['source']})"
        )
