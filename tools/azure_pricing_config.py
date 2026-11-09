"""Phase 8.4.5 centralized regional and snapshot configuration.

Single source of truth for the East US primary / West US secondary regional
model used by Consumption Analysis. Nothing else in the codebase should
hardcode these values -- import them from here.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

# --- Regional defaults (Phase 8.4.5 section 2) ---------------------------
PRIMARY_REGION = "eastus"
PRIMARY_REGION_LABEL = "East US"
SECONDARY_REGION = "westus"
SECONDARY_REGION_LABEL = "West US"

# West US is never queried for real retail pricing in the default demo
# model. It is a deterministic synthetic modeling assumption -- see
# PRICING_METHOD_MODEL_MULTIPLIER below. This MUST NOT be represented as
# actual Microsoft/Azure West US retail pricing anywhere in the system.
SECONDARY_REGION_PRICING_MODE = "MODEL_MULTIPLIER"
SECONDARY_REGION_PRICE_MULTIPLIER = Decimal("0.95")

# --- Pricing method / source vocabulary (sections 3, 24, 29) -------------
PRICING_METHOD_ACTUAL_AZURE_RETAIL = "ACTUAL_AZURE_RETAIL"
PRICING_METHOD_MODEL_MULTIPLIER = "MODEL_MULTIPLIER"

PRICING_SOURCE_LOCAL_SNAPSHOT = "LOCAL_SNAPSHOT"
PRICING_SOURCE_LIVE_AZURE_API = "LIVE_AZURE_API"
PRICING_SOURCE_MANUAL_OVERRIDE = "MANUAL_OVERRIDE"

# --- Snapshot staleness (section 37) --------------------------------------
SNAPSHOT_STALE_AFTER_DAYS = 30

# --- Azure Retail Prices API filters used for the master snapshot --------
SNAPSHOT_PRICE_TYPE = "Consumption"
# Text tokens excluded from the default PAYG catalog (section 6). Matching
# is case-insensitive against skuName/meterName/productName.
EXCLUDED_SKU_TOKENS = (
    "spot",
    "low priority",
    "reservation",
    "reserved",
    "savings plan",
    "dev/test",
    "devtest",
    "free trial",
    "promo",
    "promotional",
)
PRICING_REGION_SCOPE_EASTUS = "EASTUS"
PRICING_REGION_SCOPE_GLOBAL = "GLOBAL"

# --- Friendly/LLM-style service names -> canonical Azure Retail Prices
# API serviceName. Single source of truth shared by live pricing resolution
# (tools/azure_retail_pricing.py) and architecture-generation catalog
# conformance checking (tools/consumption_architecture_planner.py) so the
# same alias always means the same real Azure service in both places.
SERVICE_NAME_MAP = {
    # Azure Retail Prices API's real serviceName for App Service meters is
    # "Azure App Service" (verified against the downloaded snapshot) -- not
    # the shorter "App Service" this previously mapped to, which silently
    # cost every App Service match its service-name score bonus.
    "Azure App Service": "Azure App Service",
    "Virtual Machines": "Virtual Machines",
    "Azure SQL Database": "SQL Database",
    "Blob Storage": "Storage",
    "Azure Blob Storage": "Storage",
    "Application Gateway": "Application Gateway",
    "Log Analytics": "Azure Monitor",
    "Azure Monitor": "Azure Monitor",
    "Azure Backup": "Backup",
    "Azure Key Vault": "Key Vault",
    "Azure Synapse Analytics": "Azure Synapse Analytics",
    "Azure Load Balancer": "Virtual Network",
    "Azure Virtual Network": "Virtual Network",
    "Virtual Network": "Virtual Network",
    "Azure Machine Learning": "Azure Machine Learning",
    "Azure Databricks": "Azure Databricks",
    # Microsoft renamed Azure Security Center to Microsoft Defender for Cloud;
    # the Azure Retail Prices API only carries pricing under the new name.
    "Azure Security Center": "Microsoft Defender for Cloud",
    "Security Center": "Microsoft Defender for Cloud",
    "Azure Kubernetes Service": "Azure Kubernetes Service",
    "Azure Cognitive Search": "Azure Cognitive Search",
    "Azure AI Search": "Azure Cognitive Search",
}

# --- Storage locations -----------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
COMMERCIAL_DIR = BASE_DIR / "knowledge_base" / "commercial_strategy"
SNAPSHOT_PATH = COMMERCIAL_DIR / "azure_eastus_retail_price_snapshot.json"
ARCHITECTURE_CATALOG_PATH = COMMERCIAL_DIR / "azure_enterprise_service_catalog.json"

SNAPSHOT_STATUS_COMPLETE = "COMPLETE"
SNAPSHOT_STATUS_PARTIAL = "PARTIAL"
SNAPSHOT_STATUS_STALE = "STALE"
SNAPSHOT_STATUS_ERROR = "ERROR"
SNAPSHOT_STATUSES = {
    SNAPSHOT_STATUS_COMPLETE,
    SNAPSHOT_STATUS_PARTIAL,
    SNAPSHOT_STATUS_STALE,
    SNAPSHOT_STATUS_ERROR,
}


def model_westus_price(eastus_retail_price: Decimal) -> Decimal:
    """West US modeled unit price = East US retail unit price x 0.95 (Decimal only)."""
    return Decimal(str(eastus_retail_price)) * SECONDARY_REGION_PRICE_MULTIPLIER
