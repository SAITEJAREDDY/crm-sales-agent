"""Phase 8.4.5 curated Enterprise Architecture Service Catalog.

Distinct from the meter-level master pricing snapshot
(tools/azure_pricing_snapshot.py): this is a curated, architecture-friendly
catalog of ~80-120 important enterprise Azure services/service patterns
that the architecture planner selects from by catalog_id. Every supported
entry is validated against the master snapshot at build time so the
architecture agent only ever sees services/SKUs that can actually be
priced later.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from tools.azure_pricing_config import (
        ARCHITECTURE_CATALOG_PATH,
        PRIMARY_REGION,
        PRICING_REGION_SCOPE_EASTUS,
        PRICING_REGION_SCOPE_GLOBAL,
    )
    from tools.azure_pricing_snapshot import load_snapshot, build_indexes, SNAPSHOT_PATH
except ModuleNotFoundError:
    from azure_pricing_config import (
        ARCHITECTURE_CATALOG_PATH,
        PRIMARY_REGION,
        PRICING_REGION_SCOPE_EASTUS,
        PRICING_REGION_SCOPE_GLOBAL,
    )
    from azure_pricing_snapshot import load_snapshot, build_indexes, SNAPSHOT_PATH


CATALOG_SCHEMA_VERSION = "1.0"
SCALE_TIERS = ("SMALL", "MID_MARKET", "ENTERPRISE", "LARGE_ENTERPRISE")

# Maps each architecture_domain to the Phase 8.3.5B orchestration stage that
# should retrieve it (section 33). Never send the full snapshot to the LLM --
# only the stage-scoped, price-free catalog slice.
STAGE_DOMAINS = {
    "platform_security": {"networking", "security", "monitoring", "backup", "dr", "hybrid"},
    "application_integration": {"compute", "containers", "web", "integration", "messaging", "cache", "vdi"},
    "data_analytics_ai": {"database", "storage", "data_analytics", "streaming", "ai_ml", "iot", "hpc"},
}


class CatalogError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Curated definitions. Each maps to real Azure Retail Prices API service /
# product / SKU fields observed in the master snapshot. `match` describes how
# to resolve one or more snapshot records deterministically; no price is
# stored here -- only enough identity to look pricing up later.
# ---------------------------------------------------------------------------

def _m(service_name: str, **kwargs) -> dict:
    return {"service_name": service_name, **kwargs}


CATALOG_DEFINITIONS: list[dict] = [
    # ---------------- Compute -------------------------------------------
    dict(catalog_id="compute-vm-general-d8sv5", architecture_domain="compute", architecture_label="General Purpose Enterprise VM",
         match=_m("Virtual Machines", arm_sku_name="Standard_D8s_v5"), preferred_sku="D8s v5", supported_usage_unit="hour",
         enterprise_use_cases=["application_compute", "api_compute", "general_enterprise_workload"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="compute-vm-general-d4sv5", architecture_domain="compute", architecture_label="General Purpose Standard VM",
         match=_m("Virtual Machines", arm_sku_name="Standard_D4s_v5"), preferred_sku="D4s v5", supported_usage_unit="hour",
         enterprise_use_cases=["application_compute", "non_production_workload"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="compute-vm-general-d2sv5", architecture_domain="compute", architecture_label="Small General Purpose VM",
         match=_m("Virtual Machines", arm_sku_name="Standard_D2s_v5"), preferred_sku="D2s v5", supported_usage_unit="hour",
         enterprise_use_cases=["utility_compute", "management_compute"], scale_tiers=["SMALL", "MID_MARKET"]),
    dict(catalog_id="compute-vm-memory-e16sv5", architecture_domain="compute", architecture_label="Memory Optimized Enterprise VM",
         match=_m("Virtual Machines", arm_sku_name="Standard_E16s_v5"), preferred_sku="E16s v5", supported_usage_unit="hour",
         enterprise_use_cases=["in_memory_workload", "database_compute"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="compute-vm-compute-f16sv2", architecture_domain="compute", architecture_label="Compute Optimized VM",
         match=_m("Virtual Machines", arm_sku_name="Standard_F16s_v2"), preferred_sku="F16s v2", supported_usage_unit="hour",
         enterprise_use_cases=["cpu_intensive_workload", "batch_processing"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="compute-vmss-general-d4sv5", architecture_domain="compute", architecture_label="VM Scale Set (General Purpose)",
         match=_m("Virtual Machines", arm_sku_name="Standard_D4s_v5"), preferred_sku="D4s v5 (scale set instance)", supported_usage_unit="hour",
         enterprise_use_cases=["autoscale_compute", "customer_facing_scalability"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"],
         pricing_note="VM Scale Sets are priced per instance through the underlying VM meter; no separate scale-set meter exists."),
    dict(catalog_id="compute-appservice-p1v3", architecture_domain="compute", architecture_label="App Service Premium v3 (Standard)",
         match=_m("Azure App Service", product_name="Azure App Service Premium v3 Plan", sku_name="P1 v3"), preferred_sku="P1v3", supported_usage_unit="hour",
         enterprise_use_cases=["application_compute", "web_hosting"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="compute-appservice-p2v3", architecture_domain="compute", architecture_label="App Service Premium v3 (Large)",
         match=_m("Azure App Service", product_name="Azure App Service Premium v3 Plan", sku_name="P2 v3"), preferred_sku="P2v3", supported_usage_unit="hour",
         enterprise_use_cases=["application_compute", "customer_facing_scalability"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="compute-appservice-isolated-i4mv4", architecture_domain="compute", architecture_label="App Service Isolated (Network Isolated)",
         match=_m("Azure App Service", arm_sku_name="Azure_App_Service_Isolated_v4_Plan_I4mv4"), preferred_sku="I4mv4", supported_usage_unit="hour",
         enterprise_use_cases=["private_networking", "regulated_workload"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="compute-functions-premium", architecture_domain="compute", architecture_label="Functions Premium Plan",
         match=_m("Functions", product_name="Premium Functions", sku_name="Premium", unit_of_measure="1 Hour"), preferred_sku="Premium (EP1-class)", supported_usage_unit="hour",
         enterprise_use_cases=["event_driven_compute", "api_compute"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="compute-functions-flex-consumption", architecture_domain="compute", architecture_label="Functions Flex Consumption",
         match=_m("Functions", product_name="Flex Consumption", sku_name="Always Ready", unit_of_measure="1 GB Second"), preferred_sku="Flex Consumption", supported_usage_unit="gb-second",
         enterprise_use_cases=["event_driven_compute", "variable_demand"], scale_tiers=["SMALL", "MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="compute-batch-pool-d4sv5", architecture_domain="compute", architecture_label="Azure Batch Compute Pool",
         match=_m("Virtual Machines", arm_sku_name="Standard_D4s_v5"), preferred_sku="D4s v5 (Batch pool node)", supported_usage_unit="hour",
         enterprise_use_cases=["batch_processing", "hpc_workload"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"],
         pricing_note="Azure Batch pool nodes are billed as the underlying VM meter; there is no separate Batch service meter."),

    # ---------------- Containers -----------------------------------------
    dict(catalog_id="containers-aks-nodepool-d4sv5", architecture_domain="containers", architecture_label="AKS Node Pool (General Purpose)",
         match=_m("Virtual Machines", arm_sku_name="Standard_D4s_v5"), preferred_sku="D4s v5 (AKS node)", supported_usage_unit="hour",
         enterprise_use_cases=["container_orchestration", "microservices"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"],
         pricing_note="AKS worker nodes bill as standard VM meters; the AKS control plane (Free/Standard tier) is a separate low-cost management fee."),
    dict(catalog_id="containers-aks-uptime-sla", architecture_domain="containers", architecture_label="AKS Standard Tier Control Plane",
         match=_m("Azure Kubernetes Service", sku_name="Standard"), preferred_sku="Standard", supported_usage_unit="hour",
         enterprise_use_cases=["container_orchestration", "high_availability"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="containers-app-standard", architecture_domain="containers", architecture_label="Container Apps (Standard Consumption)",
         match=_m("Azure Container Apps", sku_name="Standard", unit_of_measure="1 GiB Second"), preferred_sku="Standard", supported_usage_unit="gib-second",
         enterprise_use_cases=["microservices", "event_driven_compute"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="containers-app-dedicated", architecture_domain="containers", architecture_label="Container Apps (Dedicated Plan)",
         match=_m("Azure Container Apps", sku_name="Dedicated"), preferred_sku="Dedicated", supported_usage_unit="hour",
         enterprise_use_cases=["microservices", "predictable_capacity"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="containers-instances-standard", architecture_domain="containers", architecture_label="Container Instances (Standard)",
         match=_m("Container Instances", sku_name="Standard", unit_of_measure="1 GB Hour"), preferred_sku="Standard", supported_usage_unit="gb-hour",
         enterprise_use_cases=["burst_workload", "batch_processing"], scale_tiers=["SMALL", "MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="containers-registry-premium", architecture_domain="containers", architecture_label="Container Registry (Premium)",
         match=_m("Container Registry", sku_name="Premium", unit_of_measure="1 GB/Month"), preferred_sku="Premium", supported_usage_unit="gb-month",
         enterprise_use_cases=["container_image_storage", "geo_replication"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="containers-registry-standard", architecture_domain="containers", architecture_label="Container Registry (Standard)",
         match=_m("Container Registry", sku_name="Standard", unit_of_measure="1/Day"), preferred_sku="Standard", supported_usage_unit="day",
         enterprise_use_cases=["container_image_storage"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),

    # ---------------- Databases -------------------------------------------
    dict(catalog_id="database-sql-bc-gen5", architecture_domain="database", architecture_label="SQL Database Business Critical",
         match=_m("SQL Database", product_name="SQL Database Single/Elastic Pool Business Critical - Compute Gen5", sku_name_contains="vCore"), preferred_sku="Business Critical Gen5", supported_usage_unit="vcore-hour",
         enterprise_use_cases=["transactional_database", "high_availability"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="database-sql-gp-gen5", architecture_domain="database", architecture_label="SQL Database General Purpose",
         match=_m("SQL Database", product_name="SQL Database Single/Elastic Pool General Purpose - Compute Gen5", sku_name_contains="vCore"), preferred_sku="General Purpose Gen5", supported_usage_unit="vcore-hour",
         enterprise_use_cases=["transactional_database"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="database-sql-hyperscale-gen5", architecture_domain="database", architecture_label="SQL Database Hyperscale",
         match=_m("SQL Database", product_name="SQL Database SingleDB/Elastic Pool Hyperscale - Compute Gen5", sku_name_contains="vCore"), preferred_sku="Hyperscale Gen5", supported_usage_unit="vcore-hour",
         enterprise_use_cases=["transactional_database", "rapid_scale"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="database-sqlmi-gp-gen5", architecture_domain="database", architecture_label="SQL Managed Instance General Purpose",
         match=_m("SQL Managed Instance", arm_sku_name="SQLMI_GP_Compute_Gen5"), preferred_sku="General Purpose Gen5 vCore", supported_usage_unit="vcore-hour",
         enterprise_use_cases=["lift_and_shift_database", "transactional_database"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="database-sqlmi-bc-gen5", architecture_domain="database", architecture_label="SQL Managed Instance Business Critical",
         match=_m("SQL Managed Instance", product_name="SQL Managed Instance Business Critical - Compute Gen5", sku_name_contains="16 vCore"), preferred_sku="Business Critical Gen5 16 vCore", supported_usage_unit="vcore-hour",
         enterprise_use_cases=["lift_and_shift_database", "high_availability"], scale_tiers=["LARGE_ENTERPRISE"]),
    dict(catalog_id="database-postgresql-flexible-memory", architecture_domain="database", architecture_label="PostgreSQL Flexible Server (Memory Optimized)",
         match=_m("Azure Database for PostgreSQL", product_name_contains="Flexible Server Memory Optimized Eadsv5", sku_name="32 vCore"), preferred_sku="32 vCore Memory Optimized", supported_usage_unit="hour",
         enterprise_use_cases=["managed_open_source_database"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="database-postgresql-flexible-burstable", architecture_domain="database", architecture_label="PostgreSQL Flexible Server (Burstable)",
         match=_m("Azure Database for PostgreSQL", product_name_contains="Flexible Server Burstable", sku_name="Basic"), preferred_sku="Burstable Basic", supported_usage_unit="hour",
         enterprise_use_cases=["managed_open_source_database", "non_production_workload"], scale_tiers=["SMALL", "MID_MARKET"]),
    dict(catalog_id="database-mysql-flexible-memory", architecture_domain="database", architecture_label="MySQL Flexible Server (Memory Optimized)",
         match=_m("Azure Database for MySQL", product_name_contains="Flexible Server Memory Optimized Edsv5", sku_name_contains="Standard_E2d_v5"), preferred_sku="E2ds v5", supported_usage_unit="hour",
         enterprise_use_cases=["managed_open_source_database"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="database-cosmosdb-ru-provisioned", architecture_domain="database", architecture_label="Cosmos DB Provisioned Throughput",
         match=_m("Azure Cosmos DB", product_name="Azure Cosmos DB", sku_name="RUs"), preferred_sku="Provisioned RU/s", supported_usage_unit="ru-100-hour",
         enterprise_use_cases=["globally_distributed_database", "high_throughput"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="database-cosmosdb-autoscale", architecture_domain="database", architecture_label="Cosmos DB Autoscale Throughput",
         match=_m("Azure Cosmos DB", sku_name_contains="autoscale"), preferred_sku="Autoscale RU/s", supported_usage_unit="ru-100-hour",
         enterprise_use_cases=["globally_distributed_database", "variable_demand"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="cache-redis-standard", architecture_domain="cache", architecture_label="Azure Cache for Redis (Standard)",
         match=_m("Redis Cache", product_name="Azure Redis Cache Standard", sku_name="C1"), preferred_sku="C1 Standard", supported_usage_unit="hour",
         enterprise_use_cases=["application_cache", "session_state"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="cache-redis-enterprise-flash", architecture_domain="cache", architecture_label="Azure Cache for Redis Enterprise (Flash)",
         match=_m("Redis Cache", product_name="Azure Redis Cache Enterprise Flash", sku_name="F300"), preferred_sku="F300 Enterprise Flash", supported_usage_unit="hour",
         enterprise_use_cases=["application_cache", "high_throughput"], scale_tiers=["LARGE_ENTERPRISE"]),

    # ---------------- Storage ---------------------------------------------
    dict(catalog_id="storage-blob-hot-lrs", architecture_domain="storage", architecture_label="Blob Storage (Hot LRS)",
         match=_m("Storage", product_name="Blob Storage", sku_name="Hot LRS"), preferred_sku="Hot LRS", supported_usage_unit="gb-month",
         enterprise_use_cases=["application_data", "analytics_data"], scale_tiers=["SMALL", "MID_MARKET", "ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="storage-blob-cool-lrs", architecture_domain="storage", architecture_label="Blob Storage (Cool LRS)",
         match=_m("Storage", product_name="Blob Storage", sku_name="Cool LRS"), preferred_sku="Cool LRS", supported_usage_unit="gb-month",
         enterprise_use_cases=["archival_data", "backup_data"], scale_tiers=["MID_MARKET", "ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="storage-datalake-gen2-cool", architecture_domain="storage", architecture_label="Data Lake Storage Gen2 (Cool)",
         match=_m("Storage", product_name="Azure Data Lake Storage Gen2 Flat Namespace", sku_name="Cool LRS"), preferred_sku="Cool LRS", supported_usage_unit="gb-month",
         enterprise_use_cases=["analytics_data", "data_platform"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="storage-files-provisioned-v2", architecture_domain="storage", architecture_label="Azure Files (Provisioned v2)",
         match=_m("Storage", product_name="Azure Files Provisioned v2", sku_name="SSD LRS"), preferred_sku="SSD LRS", supported_usage_unit="hour",
         enterprise_use_cases=["shared_file_storage", "lift_and_shift"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="storage-managed-disk-premium-ssd", architecture_domain="storage", architecture_label="Managed Disk (Premium SSD)",
         match=_m("Storage", product_name="Premium SSD Managed Disks", sku_name="P40 LRS"), preferred_sku="P40 LRS", supported_usage_unit="month",
         enterprise_use_cases=["vm_disk", "database_storage"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="storage-managed-disk-standard-ssd", architecture_domain="storage", architecture_label="Managed Disk (Standard SSD)",
         match=_m("Storage", product_name="Standard SSD Managed Disks", sku_name="E30 LRS"), preferred_sku="E30 LRS", supported_usage_unit="month",
         enterprise_use_cases=["vm_disk", "non_production_workload"], scale_tiers=["SMALL", "MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="storage-queue-standard", architecture_domain="storage", architecture_label="Queue Storage (Standard RA-GRS)",
         match=_m("Storage", product_name="Queues v2", sku_name="Standard RA-GRS"), preferred_sku="Standard RA-GRS", supported_usage_unit="10k-operations",
         enterprise_use_cases=["messaging", "decoupled_processing"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="storage-table-standard", architecture_domain="storage", architecture_label="Table Storage (Standard GZRS)",
         match=_m("Storage", product_name="Tables", sku_name="Standard GZRS"), preferred_sku="Standard GZRS", supported_usage_unit="10k-operations",
         enterprise_use_cases=["nosql_key_value_store"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),

    # ---------------- Networking --------------------------------------------
    dict(catalog_id="network-appgw-waf-v2", architecture_domain="networking", architecture_label="Application Gateway (WAF v2)",
         match=_m("Application Gateway", product_name="Application Gateway Standard v2", sku_name="Standard"), preferred_sku="WAF_v2", supported_usage_unit="hour",
         enterprise_use_cases=["web_application_firewall", "customer_facing_scalability"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="network-publicip-standard", architecture_domain="networking", architecture_label="Load Balanced Public IP (Standard)",
         match=_m("Virtual Network", product_name="IP Addresses", sku_name="Standard"), preferred_sku="Standard Public IP", supported_usage_unit="hour",
         enterprise_use_cases=["load_balancing", "public_ingress"], scale_tiers=["MID_MARKET", "ENTERPRISE", "LARGE_ENTERPRISE"],
         pricing_note="Standard Load Balancer rule/data processing is not separately metered in the current Azure Retail Prices catalog; represented via the associated Standard Public IP meter."),
    dict(catalog_id="network-firewall-standard", architecture_domain="networking", architecture_label="Azure Firewall (Standard)",
         match=_m("Azure Firewall", sku_name="Standard Secure Virtual Hub", unit_of_measure="1 Hour"), preferred_sku="Standard Secure Virtual Hub", supported_usage_unit="hour",
         enterprise_use_cases=["network_security", "centralized_security_monitoring"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="network-natgateway", architecture_domain="networking", architecture_label="NAT Gateway",
         match=_m("Virtual Network", product_name_contains="NAT Gateway"), preferred_sku="Standard", supported_usage_unit="hour",
         enterprise_use_cases=["outbound_connectivity"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"],
         pricing_note="No NAT Gateway meter found in the current snapshot; documented as unsupported pending a future refresh."),
    dict(catalog_id="network-vpngw-vpngw1az", architecture_domain="networking", architecture_label="VPN Gateway (Zone Redundant)",
         match=_m("VPN Gateway", sku_name="VpnGw1AZ"), preferred_sku="VpnGw1AZ", supported_usage_unit="hour",
         enterprise_use_cases=["hybrid_connectivity", "site_to_site_vpn"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="network-expressroute-standard", architecture_domain="networking", architecture_label="ExpressRoute Gateway (Standard)",
         match=_m("ExpressRoute", product_name="ExpressRoute Standard Gateway", sku_name="Standard"), preferred_sku="Standard", supported_usage_unit="hour",
         enterprise_use_cases=["hybrid_connectivity", "private_networking"], scale_tiers=["LARGE_ENTERPRISE"]),
    dict(catalog_id="network-privatelink-endpoint", architecture_domain="networking", architecture_label="Private Link / Private Endpoint",
         match=_m("Virtual Network", product_name_contains="Private Link"), preferred_sku="Private Endpoint", supported_usage_unit="hour",
         enterprise_use_cases=["private_networking", "data_isolation"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"],
         pricing_note="No standalone Private Link meter found in the current snapshot; documented as unsupported pending a future refresh."),
    dict(catalog_id="network-frontdoor-standard", architecture_domain="networking", architecture_label="Front Door (Standard)",
         match=_m("Azure Front Door Service", sku_name="Standard", unit_of_measure="1M/Month"), preferred_sku="Standard", supported_usage_unit="million-requests-month",
         enterprise_use_cases=["global_load_balancing", "customer_facing_scalability"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="network-dns-public", architecture_domain="networking", architecture_label="Azure DNS (Public Zone)",
         match=_m("Azure DNS", sku_name="Public", unit_of_measure="1"), preferred_sku="Public", supported_usage_unit="hosted-zone-month",
         enterprise_use_cases=["dns_management"], scale_tiers=["MID_MARKET", "ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="network-ddos-protection", architecture_domain="networking", architecture_label="Azure DDoS Network Protection",
         match=_m("Azure DDOS Protection", sku_name="Network Protection", unit_of_measure="1 Hour"), preferred_sku="Network Protection", supported_usage_unit="hour",
         enterprise_use_cases=["network_security", "regulated_workload"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),

    # ---------------- Security ----------------------------------------------
    dict(catalog_id="security-sentinel-payg", architecture_domain="security", architecture_label="Microsoft Sentinel (Pay-as-you-go)",
         match=_m("Sentinel", sku_name="Pay-as-you-go"), preferred_sku="Pay-as-you-go", supported_usage_unit="gb",
         enterprise_use_cases=["centralized_security_monitoring", "siem"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="security-defender-sql", architecture_domain="security", architecture_label="Microsoft Defender for SQL",
         match=_m("Microsoft Defender for Cloud", product_name="Microsoft Defender for SQL", sku_name="Standard"), preferred_sku="Standard", supported_usage_unit="hour",
         enterprise_use_cases=["cloud_security_posture", "regulated_workload"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="security-defender-containers", architecture_domain="security", architecture_label="Microsoft Defender for Containers",
         match=_m("Microsoft Defender for Cloud", product_name="Microsoft Defender for Containers", sku_name="Standard Images"), preferred_sku="Standard Images", supported_usage_unit="image",
         enterprise_use_cases=["cloud_security_posture", "container_security"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="security-keyvault-standard", architecture_domain="security", architecture_label="Key Vault (Standard)",
         match=_m("Key Vault", sku_name="Standard", unit_of_measure="10K"), preferred_sku="Standard", supported_usage_unit="10k-operations",
         enterprise_use_cases=["secrets_management", "encryption_key_management"], scale_tiers=["SMALL", "MID_MARKET", "ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="security-keyvault-premium-hsm", architecture_domain="security", architecture_label="Key Vault Managed HSM",
         match=_m("Key Vault", product_name="Key Vault HSM Pool", sku_name="Standard B1"), preferred_sku="Standard B1 HSM Pool", supported_usage_unit="hour",
         enterprise_use_cases=["encryption_key_management", "regulated_workload"], scale_tiers=["LARGE_ENTERPRISE"]),

    # ---------------- Monitoring / Operations --------------------------------
    dict(catalog_id="monitoring-log-analytics-payg", architecture_domain="monitoring", architecture_label="Log Analytics (Pay-as-you-go)",
         match=_m("Log Analytics", sku_name="Analytics Logs", unit_of_measure="1 GB/Month"), preferred_sku="Pay-as-you-go", supported_usage_unit="gb",
         enterprise_use_cases=["centralized_security_monitoring", "operational_telemetry"], scale_tiers=["SMALL", "MID_MARKET", "ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="monitoring-appinsights-enterprise", architecture_domain="monitoring", architecture_label="Application Insights (Enterprise)",
         match=_m("Application Insights", sku_name="Enterprise", unit_of_measure="1 GB"), preferred_sku="Enterprise", supported_usage_unit="gb",
         enterprise_use_cases=["application_performance_monitoring"], scale_tiers=["MID_MARKET", "ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="monitoring-azuremonitor-alerts", architecture_domain="monitoring", architecture_label="Azure Monitor Alerts",
         match=_m("Azure Monitor", sku_name="Alerts"), preferred_sku="Alerts", supported_usage_unit="month",
         enterprise_use_cases=["operational_telemetry", "incident_response"], scale_tiers=["MID_MARKET", "ENTERPRISE", "LARGE_ENTERPRISE"]),

    # ---------------- Backup / DR --------------------------------------------
    dict(catalog_id="backup-standard-gb", architecture_domain="backup", architecture_label="Azure Backup (Standard)",
         match=_m("Backup", sku_name="Standard", unit_of_measure="1 GB/Month"), preferred_sku="Standard", supported_usage_unit="gb-month",
         enterprise_use_cases=["data_protection", "business_continuity"], scale_tiers=["SMALL", "MID_MARKET", "ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="backup-archive-gb", architecture_domain="backup", architecture_label="Azure Backup (Archive Tier)",
         match=_m("Backup", sku_name="Archive"), preferred_sku="Archive", supported_usage_unit="gb-month",
         enterprise_use_cases=["long_term_retention", "regulated_workload"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="dr-site-recovery-azure", architecture_domain="dr", architecture_label="Azure Site Recovery",
         match=_m("Azure Site Recovery", sku_name="Azure"), preferred_sku="Azure-to-Azure", supported_usage_unit="protected-instance-month",
         enterprise_use_cases=["multi_region_disaster_recovery", "business_continuity"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),

    # ---------------- Integration ---------------------------------------------
    dict(catalog_id="integration-apim-standardv2", architecture_domain="integration", architecture_label="API Management (Standard v2)",
         match=_m("API Management", sku_name="Standard v2"), preferred_sku="Standard v2", supported_usage_unit="hour",
         enterprise_use_cases=["api_management", "partner_integration"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="integration-apim-premium-isolated", architecture_domain="integration", architecture_label="API Management (Isolated)",
         match=_m("API Management", sku_name="Isolated"), preferred_sku="Isolated", supported_usage_unit="hour",
         enterprise_use_cases=["api_management", "private_networking"], scale_tiers=["LARGE_ENTERPRISE"]),
    dict(catalog_id="messaging-servicebus-standard", architecture_domain="messaging", architecture_label="Service Bus (Standard)",
         match=_m("Service Bus", sku_name="Standard", unit_of_measure="1/Hour"), preferred_sku="Standard", supported_usage_unit="hour",
         enterprise_use_cases=["messaging", "decoupled_processing"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="messaging-servicebus-premium", architecture_domain="messaging", architecture_label="Service Bus (Premium)",
         match=_m("Service Bus", sku_name="Premium", unit_of_measure="1/Hour"), preferred_sku="Premium", supported_usage_unit="hour",
         enterprise_use_cases=["messaging", "high_throughput"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="integration-eventgrid-standard", architecture_domain="messaging", architecture_label="Event Grid (Standard)",
         match=_m("Event Grid", sku_name="Standard", unit_of_measure="1M"), preferred_sku="Standard", supported_usage_unit="million-operations",
         enterprise_use_cases=["event_driven_architecture", "decoupled_processing"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="integration-logicapps-standard", architecture_domain="integration", architecture_label="Logic Apps (Standard)",
         match=_m("Logic Apps", product_name="Logic Apps", sku_name="Standard", unit_of_measure="1 Hour"), preferred_sku="Standard", supported_usage_unit="hour",
         enterprise_use_cases=["workflow_automation", "partner_integration"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="integration-logicapps-consumption", architecture_domain="integration", architecture_label="Logic Apps (Consumption)",
         match=_m("Logic Apps", sku_name="Consumption", unit_of_measure="1 GB/Month"), preferred_sku="Consumption", supported_usage_unit="gb-month",
         enterprise_use_cases=["workflow_automation"], scale_tiers=["SMALL", "MID_MARKET"]),

    # ---------------- Streaming -------------------------------------------
    dict(catalog_id="streaming-eventhubs-standard", architecture_domain="streaming", architecture_label="Event Hubs (Standard)",
         match=_m("Event Hubs", sku_name="Standard", unit_of_measure="1 Hour"), preferred_sku="Standard", supported_usage_unit="hour",
         enterprise_use_cases=["event_ingestion", "telemetry_event_ingestion"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="streaming-eventhubs-dedicated", architecture_domain="streaming", architecture_label="Event Hubs (Dedicated)",
         match=_m("Event Hubs", sku_name="Dedicated", unit_of_measure="1 Hour"), preferred_sku="Dedicated", supported_usage_unit="hour",
         enterprise_use_cases=["event_ingestion", "high_throughput"], scale_tiers=["LARGE_ENTERPRISE"]),
    dict(catalog_id="streaming-stream-analytics-standard", architecture_domain="streaming", architecture_label="Stream Analytics (Standard)",
         match=_m("Stream Analytics", sku_name="Standard", unit_of_measure="1 Hour"), preferred_sku="Standard Streaming Unit", supported_usage_unit="hour",
         enterprise_use_cases=["real_time_analytics", "telemetry_event_ingestion"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),

    # ---------------- Data / Analytics -------------------------------------
    dict(catalog_id="data-factory-v2-cloud", architecture_domain="data_analytics", architecture_label="Data Factory v2 (Cloud Pipeline)",
         match=_m("Azure Data Factory v2", product_name="Azure Data Factory v2", sku_name="Cloud"), preferred_sku="Cloud", supported_usage_unit="hour",
         enterprise_use_cases=["data_pipeline", "data_platform"], scale_tiers=["MID_MARKET", "ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="data-synapse-dedicated-dw500c", architecture_domain="data_analytics", architecture_label="Synapse Dedicated SQL Pool",
         match=_m("Azure Synapse Analytics", product_name="Azure Synapse Analytics Dedicated SQL Pool", sku_name="DW500c"), preferred_sku="DW500c", supported_usage_unit="dwu-hour",
         enterprise_use_cases=["data_warehouse", "enterprise_analytics"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="data-explorer-standard", architecture_domain="data_analytics", architecture_label="Azure Data Explorer (Standard)",
         match=_m("Azure Data Explorer", sku_name="Standard"), preferred_sku="Standard", supported_usage_unit="hour",
         enterprise_use_cases=["log_and_telemetry_analytics"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="data-hdinsight-dseries", architecture_domain="data_analytics", architecture_label="HDInsight (D-series cluster node)",
         match=_m("HDInsight", product_name_contains="HDInsight D Series", sku_name_contains="D16a"), preferred_sku="D16a v4", supported_usage_unit="hour",
         enterprise_use_cases=["big_data_processing"], scale_tiers=["LARGE_ENTERPRISE"]),
    dict(catalog_id="data-databricks-standard-allpurpose", architecture_domain="data_analytics", architecture_label="Azure Databricks (Standard All-purpose Compute)",
         match=_m("Azure Databricks", product_name="Azure Databricks", sku_name="Standard All-purpose Compute"), preferred_sku="Standard All-purpose Compute", supported_usage_unit="hour",
         enterprise_use_cases=["data_platform", "advanced_analytics"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="data-fabric-capacity", architecture_domain="data_analytics", architecture_label="Microsoft Fabric Capacity",
         match=_m("Microsoft Fabric", product_name="Fabric Capacity", sku_name_contains="Eventhouse"), preferred_sku="Eventhouse Capacity", supported_usage_unit="hour",
         enterprise_use_cases=["unified_analytics"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),

    # ---------------- AI / ML -------------------------------------------------
    dict(catalog_id="ai-openai-gpt4-input", architecture_domain="ai_ml", architecture_label="Azure OpenAI (GPT-4 class, input tokens)",
         match=_m("Foundry Models", product_name_contains="Azure OpenAI", sku_name_contains="gpt 4.1 Inp"), preferred_sku="GPT-4.1 input tokens", supported_usage_unit="1k-tokens",
         enterprise_use_cases=["generative_ai", "copilot_integration"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="ai-search-standard-s2", architecture_domain="ai_ml", architecture_label="Azure AI Search (Standard S2)",
         match=_m("Azure Cognitive Search", product_name="Azure AI Search", sku_name="Standard S2"), preferred_sku="Standard S2", supported_usage_unit="hour",
         enterprise_use_cases=["retrieval_augmented_generation", "enterprise_search"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="ai-ml-managed-hosting-a100", architecture_domain="ai_ml", architecture_label="Azure ML Managed Online Endpoint (GPU A100 class)",
         match=_m("Azure Machine Learning", product_name="Managed Model Hosting Service", sku_name="NCadsA100v4"), preferred_sku="NCadsA100v4", supported_usage_unit="hour",
         enterprise_use_cases=["model_inferencing", "generative_ai"], scale_tiers=["LARGE_ENTERPRISE"]),

    # ---------------- IoT -------------------------------------------------
    dict(catalog_id="iot-hub-s1", architecture_domain="iot", architecture_label="IoT Hub (Standard S1)",
         match=_m("IoT Hub", product_name="IoT Hub", sku_name="S1"), preferred_sku="S1", supported_usage_unit="month",
         enterprise_use_cases=["device_telemetry_ingestion", "iot_platform"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),
    dict(catalog_id="iot-hub-s2", architecture_domain="iot", architecture_label="IoT Hub (Standard S2)",
         match=_m("IoT Hub", product_name="IoT Hub", sku_name="S2"), preferred_sku="S2", supported_usage_unit="month",
         enterprise_use_cases=["device_telemetry_ingestion", "iot_platform"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),
    dict(catalog_id="iot-digital-twins-standard", architecture_domain="iot", architecture_label="Azure Digital Twins (Standard)",
         match=_m("Digital Twins", sku_name="Standard"), preferred_sku="Standard", supported_usage_unit="1k-operations",
         enterprise_use_cases=["digital_twin_modeling", "iot_platform"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"]),

    # ---------------- Web ---------------------------------------------------
    dict(catalog_id="web-appservice-linux-p1v3", architecture_domain="web", architecture_label="App Service Premium v3 (Linux)",
         match=_m("Azure App Service", product_name="Azure App Service Premium v3 Plan - Linux", sku_name="P1 v3"), preferred_sku="P1v3 Linux", supported_usage_unit="hour",
         enterprise_use_cases=["web_hosting", "application_compute"], scale_tiers=["MID_MARKET", "ENTERPRISE"]),

    # ---------------- VDI ----------------------------------------------------
    dict(catalog_id="vdi-avd-desktop-hosting", architecture_domain="vdi", architecture_label="Azure Virtual Desktop (Desktop & App Hosting)",
         match=_m("Windows Virtual Desktop", product_name="Azure Virtual Desktop", sku_name="Desktop & App Hosting"), preferred_sku="Desktop & App Hosting", supported_usage_unit="month",
         enterprise_use_cases=["virtual_desktop_infrastructure", "remote_workforce"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"],
         pricing_note="AVD host pool compute bills separately as the underlying VM meter; this entry covers the AVD service fee."),
    dict(catalog_id="vdi-avd-host-vm-d4sv5", architecture_domain="vdi", architecture_label="AVD Session Host VM",
         match=_m("Virtual Machines", arm_sku_name="Standard_D4s_v5"), preferred_sku="D4s v5 (AVD session host)", supported_usage_unit="hour",
         enterprise_use_cases=["virtual_desktop_infrastructure"], scale_tiers=["ENTERPRISE", "LARGE_ENTERPRISE"],
         pricing_note="AVD session hosts are billed as standard VM compute; there is no separate AVD compute meter."),

    # ---------------- HPC ------------------------------------------------
    dict(catalog_id="hpc-vm-hb176rsv4", architecture_domain="hpc", architecture_label="HPC VM (HBv4-class, memory bandwidth optimized)",
         match=_m("Virtual Machines", arm_sku_name="Standard_HB176rs_v4"), preferred_sku="HB176rs v4", supported_usage_unit="hour",
         enterprise_use_cases=["hpc_workload", "scientific_computing"], scale_tiers=["LARGE_ENTERPRISE"]),
    dict(catalog_id="hpc-vm-hc44rs", architecture_domain="hpc", architecture_label="HPC VM (HC-class, compute optimized)",
         match=_m("Virtual Machines", arm_sku_name="Standard_HC44rs"), preferred_sku="HC44rs", supported_usage_unit="hour",
         enterprise_use_cases=["hpc_workload", "scientific_computing"], scale_tiers=["LARGE_ENTERPRISE"]),
    dict(catalog_id="hpc-vm-gpu-nc24-a100", architecture_domain="hpc", architecture_label="GPU VM (NC A100-class)",
         match=_m("Virtual Machines", arm_sku_name="Standard_NC24ads_A100_v4"), preferred_sku="NC24ads A100 v4", supported_usage_unit="hour",
         enterprise_use_cases=["hpc_workload", "model_training"], scale_tiers=["LARGE_ENTERPRISE"]),

    # ---------------- Hybrid ---------------------------------------------
    dict(catalog_id="hybrid-arc-enabled-database", architecture_domain="hybrid", architecture_label="Azure Arc-enabled Database Services",
         match=_m("Azure Arc Enabled Databases", product_name_contains="Arc enabled"), preferred_sku="Arc-enabled SQL Managed Instance", supported_usage_unit="core-month",
         enterprise_use_cases=["hybrid_data_platform"], scale_tiers=["LARGE_ENTERPRISE"]),
]


# ---------------------------------------------------------------------------
# Resolution against the master snapshot
# ---------------------------------------------------------------------------

def _match_records(match: dict, records_for_service: list[dict]) -> list[dict]:
    out = []
    for r in records_for_service:
        if match.get("arm_sku_name") is not None and r.get("arm_sku_name") != match["arm_sku_name"]:
            continue
        if match.get("product_name") is not None and r.get("product_name") != match["product_name"]:
            continue
        if match.get("product_name_contains") is not None and match["product_name_contains"].lower() not in (r.get("product_name") or "").lower():
            continue
        if match.get("sku_name") is not None and r.get("sku_name") != match["sku_name"]:
            continue
        if match.get("sku_name_contains") is not None and match["sku_name_contains"].lower() not in (r.get("sku_name") or "").lower():
            continue
        if match.get("meter_name") is not None and r.get("meter_name") != match["meter_name"]:
            continue
        if match.get("unit_of_measure") is not None and r.get("unit_of_measure") != match["unit_of_measure"]:
            continue
        out.append(r)
    return out


def _resolve_entry(definition: dict, indexes: dict) -> dict:
    entry = deepcopy(definition)
    match = entry.pop("match")
    service_name = match["service_name"]
    candidates = indexes["by_service_name"].get(service_name, [])
    matched = _match_records(match, candidates)
    entry["service_name"] = service_name
    entry["pricing_lookup_key"] = {"service_name": service_name, **{k: v for k, v in match.items() if k != "service_name"}}
    if not matched:
        entry["resolution_status"] = "UNSUPPORTED"
        entry["service_family"] = None
        entry["product_name"] = None
        entry["arm_sku_name"] = None
        entry["default_region"] = PRIMARY_REGION
        entry["pricing_region_scope"] = None
        entry["multiple_meter_candidates"] = False
        entry["matched_meter_ids"] = []
        return entry
    # Deterministic pick: prefer an EASTUS-scoped record, then lowest meter_id for stability.
    matched_sorted = sorted(matched, key=lambda r: (r.get("pricing_region_scope") != PRICING_REGION_SCOPE_EASTUS, r.get("meter_id") or ""))
    top = matched_sorted[0]
    entry["resolution_status"] = "RESOLVED"
    entry["service_family"] = top.get("service_family")
    entry["product_name"] = top.get("product_name")
    entry["arm_sku_name"] = top.get("arm_sku_name")
    entry["default_region"] = PRIMARY_REGION
    entry["pricing_region_scope"] = top.get("pricing_region_scope")
    entry["multiple_meter_candidates"] = len({r.get("meter_id") for r in matched}) > 1
    entry["matched_meter_ids"] = sorted({r.get("meter_id") for r in matched})[:10]
    return entry


def build_catalog(snapshot: dict | None = None) -> dict:
    snapshot = snapshot if snapshot is not None else load_snapshot()
    if not snapshot or not snapshot.get("records"):
        raise CatalogError("SNAPSHOT_REQUIRED", "A master pricing snapshot is required before building the architecture catalog.")
    indexes = build_indexes(snapshot["records"])
    entries = [_resolve_entry(d, indexes) for d in CATALOG_DEFINITIONS]

    resolved = [e for e in entries if e["resolution_status"] == "RESOLVED"]
    unsupported = [e for e in entries if e["resolution_status"] == "UNSUPPORTED"]
    eastus_priced = [e for e in resolved if e["pricing_region_scope"] == PRICING_REGION_SCOPE_EASTUS]
    global_priced = [e for e in resolved if e["pricing_region_scope"] == PRICING_REGION_SCOPE_GLOBAL]
    multi_candidate = [e for e in resolved if e["multiple_meter_candidates"]]

    domains = sorted({e["architecture_domain"] for e in entries})
    by_domain = {d: sum(1 for e in entries if e["architecture_domain"] == d) for d in domains}

    metadata = {
        "generated_at": _utc_now(),
        "schema_version": CATALOG_SCHEMA_VERSION,
        "source_snapshot_timestamp": snapshot.get("metadata", {}).get("snapshot_timestamp"),
        "primary_region": PRIMARY_REGION,
        "architecture_entries": len(entries),
        "entries_with_valid_eastus_pricing": len(eastus_priced),
        "entries_using_global_pricing": len(global_priced),
        "entries_with_multiple_meter_candidates": len(multi_candidate),
        "unsupported_entries": len(unsupported),
        "unsupported_catalog_ids": sorted(e["catalog_id"] for e in unsupported),
        "domains": domains,
        "entries_by_domain": by_domain,
    }
    return {"metadata": metadata, "entries": entries}


def save_catalog(catalog: dict, path: Path = ARCHITECTURE_CATALOG_PATH) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return catalog


def refresh_catalog(snapshot: dict | None = None, path: Path = ARCHITECTURE_CATALOG_PATH) -> dict:
    catalog = build_catalog(snapshot)
    return save_catalog(catalog, path)


_CATALOG_CACHE: dict[str, Any] = {"path": None, "mtime": None, "catalog": None}


def load_catalog(path: Path = ARCHITECTURE_CATALOG_PATH) -> dict | None:
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    if _CATALOG_CACHE["path"] == str(path) and _CATALOG_CACHE["mtime"] == mtime:
        return _CATALOG_CACHE["catalog"]
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    _CATALOG_CACHE.update({"path": str(path), "mtime": mtime, "catalog": catalog})
    return catalog


# ---------------------------------------------------------------------------
# Retrieval for architecture generation (section 33)
# ---------------------------------------------------------------------------

LLM_VIEW_FIELDS = ("catalog_id", "architecture_domain", "service_name", "architecture_label", "preferred_sku", "supported_usage_unit", "enterprise_use_cases", "scale_tiers")


def to_llm_view(entry: dict) -> dict:
    """Strip an entry down to what the architecture LLM is allowed to see --
    service/SKU/capability/usage-unit/use-case/scale-tier. No pricing (section 32)."""
    return {k: entry.get(k) for k in LLM_VIEW_FIELDS}


def get_entries_for_domains(domains: set[str], enterprise_scale: str | None = None, exclude_unsupported: bool = True, catalog: dict | None = None) -> list[dict]:
    catalog = catalog if catalog is not None else load_catalog()
    if not catalog:
        return []
    entries = catalog.get("entries", [])
    out = []
    for e in entries:
        if e["architecture_domain"] not in domains:
            continue
        if exclude_unsupported and e["resolution_status"] != "RESOLVED":
            continue
        if enterprise_scale and e.get("scale_tiers") and enterprise_scale not in e["scale_tiers"]:
            continue
        out.append(e)
    return out


def get_catalog_for_stage(stage: str, enterprise_scale: str | None = None, exclude_unsupported: bool = True, catalog: dict | None = None, as_llm_view: bool = True) -> list[dict]:
    domains = STAGE_DOMAINS.get(stage)
    if not domains:
        raise CatalogError("UNKNOWN_STAGE", f"Unknown architecture planning stage: {stage}")
    entries = get_entries_for_domains(domains, enterprise_scale=enterprise_scale, exclude_unsupported=exclude_unsupported, catalog=catalog)
    return [to_llm_view(e) for e in entries] if as_llm_view else entries


def get_catalog_entry(catalog_id: str, catalog: dict | None = None) -> dict | None:
    catalog = catalog if catalog is not None else load_catalog()
    if not catalog:
        return None
    for e in catalog.get("entries", []):
        if e["catalog_id"] == catalog_id:
            return e
    return None


def validate_catalog_ids_unique(catalog: dict) -> list[str]:
    ids = [e["catalog_id"] for e in catalog.get("entries", [])]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    return [f"duplicate catalog_id: {d}" for d in dupes]
