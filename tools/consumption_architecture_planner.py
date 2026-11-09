"""Phase 8.3 Azure architecture planner for Consumption Analysis."""

from __future__ import annotations

try:
    from tools.llm_client import TOGETHER_CHAT_URL, TOGETHER_MODEL, request_completion
except ModuleNotFoundError:
    from llm_client import TOGETHER_CHAT_URL, TOGETHER_MODEL, request_completion

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from tools.commercial_data import get_deal_input
    from tools.consumption_profiles import get_consumption_profile
    from tools.azure_pricing_config import PRIMARY_REGION, SECONDARY_REGION, SERVICE_NAME_MAP
    from tools.azure_enterprise_catalog import get_catalog_for_stage, get_catalog_entry, load_catalog, STAGE_DOMAINS
except ModuleNotFoundError:
    from commercial_data import get_deal_input
    from consumption_profiles import get_consumption_profile
    from azure_pricing_config import PRIMARY_REGION, SECONDARY_REGION, SERVICE_NAME_MAP
    from azure_enterprise_catalog import get_catalog_for_stage, get_catalog_entry, load_catalog, STAGE_DOMAINS


BASE_DIR = Path(__file__).resolve().parent.parent
COMMERCIAL_DIR = BASE_DIR / "knowledge_base" / "commercial_strategy"
ARCHITECTURE_STORE_PATH = COMMERCIAL_DIR / "consumption_architectures.json"
GOLDEN_ARCHITECTURE_PATH = COMMERCIAL_DIR / "golden_consumption_architecture.json"
ALLOWED_CATEGORIES = {
    "compute", "database", "storage", "networking", "monitoring", "backup", "security", "analytics",
    "integration", "ai", "container", "messaging", "identity",
    # Added so the Phase 8.4.5 architecture catalog's richer domain vocabulary
    # (tools/azure_enterprise_catalog.py STAGE_DOMAINS) can be used directly
    # as service_category without every catalog-conformant service failing
    # structural validation.
    "cache", "dr", "hpc", "hybrid", "iot", "streaming", "vdi", "web",
}
ALLOWED_CONFIDENCE = {"LOW", "MEDIUM", "HIGH"}
PRICING_FIELDS = {"price", "unit_price", "retail_price", "monthly_cost", "annual_cost", "total_cost", "tco", "roi", "discount", "margin", "npv", "payback"}
REGION_ALIASES = {"East US": "eastus", "East US 2": "eastus2", "West US 2": "westus2", "Central US": "centralus", "West Europe": "westeurope", "North Europe": "northeurope", "Southeast Asia": "southeastasia"}
CATEGORY_ALIASES = {
    "data": "database", "devops": "monitoring", "ai/ml": "ai", "security": "security",
    # Catalog architecture_domain values that map onto an existing legacy
    # service_category rather than needing a new one.
    "ai_ml": "ai", "containers": "container", "data_analytics": "analytics",
}
ENV_ALIASES = {"prod": "production", "nonprod": "non-production", "non-prod": "non-production", "stage": "staging", "test": "test", "dev": "development"}
ARCHITECTURE_MODES = {"EXPECTED", "COMMITMENT_ALIGNED"}
# Catalog-constrained generation (section 31/47): a free-form service is
# only acceptable when nothing in the curated catalog covers it. When a
# generation attempt uses a free-form azure_service that the catalog *does*
# cover, generation is rejected and retried with corrective feedback rather
# than silently accepted, up to this many total attempts.
MAX_GENERATION_ATTEMPTS = 3


class ArchitecturePlannerError(ValueError):
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


def _contains_pricing_field(obj: Any, path: str = "") -> list[str]:
    errors: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            if key_l in PRICING_FIELDS or any(token in key_l for token in ("price", "cost", "roi", "margin", "discount", "npv", "payback")):
                errors.append(f"pricing field is not allowed: {path + key}")
            errors.extend(_contains_pricing_field(value, f"{path}{key}."))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            errors.extend(_contains_pricing_field(item, f"{path}[{i}]."))
    return errors


def build_architecture_context(deal_id: str) -> dict:
    deal = get_deal_input(deal_id)
    if not deal:
        raise ArchitecturePlannerError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")
    profile = get_consumption_profile(deal_id)
    if not profile:
        raise ArchitecturePlannerError("CONSUMPTION_PROFILE_REQUIRED", "A workload profile is required before architecture generation.")
    account = deal.get("account", {})
    commercial = deal.get("commercial", {})
    try:
        from tools.enterprise_workload_profiler import profile_enterprise_workload
        enterprise_profile = profile_enterprise_workload(profile, deal)
    except ImportError:
        enterprise_profile = None
    return {
        "deal": {
            "deal_id": deal.get("deal_id"),
            "industry": account.get("industry"),
            "customer_segment": account.get("segment"),
            "customer_region": account.get("region"),
            "deal_term_months": commercial.get("contract_term_months"),
            "tcv": commercial.get("tcv"),
            "products": [p.get("product") for p in deal.get("products", []) if p.get("product")],
        },
        "consumption_profile": profile,
        "enterprise_profile": enterprise_profile,
        "regional_defaults": {"primary_region": PRIMARY_REGION, "secondary_region": SECONDARY_REGION},
    }


def _sizing_guidance(context: dict) -> str:
    profile = context.get("consumption_profile") or {}
    scale = (context.get("enterprise_profile") or {}).get("enterprise_scale") or "unknown"
    users = profile.get("expected_users")
    concurrent = profile.get("concurrent_users")
    transactions = profile.get("monthly_transactions")
    rps = profile.get("requests_per_second")
    data_tb = profile.get("data_volume_tb")
    data_growth = profile.get("monthly_data_growth_tb")
    env_count = profile.get("environment_count")
    return f"""
QUANTITY AND SCALE SIZING (do not default every service's quantity to 1):
This workload profile is enterprise_scale={scale}, expected_users={users}, concurrent_users={concurrent},
monthly_transactions={transactions}, requests_per_second={rps}, data_volume_tb={data_tb},
monthly_data_growth_tb={data_growth}, environment_count={env_count}.
Use these numbers to size each service instead of a placeholder quantity:
- Compute (VMs, App Service instances, container/AKS node counts): quantity should reflect how many
  instances are needed to serve concurrent_users/requests_per_second at this enterprise_scale with
  redundancy -- for ENTERPRISE/LARGE_ENTERPRISE scale this is normally several instances per production
  environment, not a single instance, unless the service is genuinely a scale-up singleton (e.g. one
  primary database, one Key Vault, one API Management instance).
- Usage/throughput-based services (messaging, event ingestion, monitoring/log ingestion, API calls,
  operations, AI/token consumption): monthly_usage_per_unit should reflect the real monthly volume
  implied by monthly_transactions/requests_per_second/expected_users, not a small placeholder like 1 or 10.
- Storage/backup: size quantity (capacity in GB or the relevant unit) from data_volume_tb and
  monthly_data_growth_tb, not a token amount.
- Non-production environments (staging/development/test) should generally use smaller quantities than
  production, and DR/secondary-region services may use different quantities than primary.
Every quantity and monthly_usage_per_unit must still be justified by the rationale field -- do not invent
numbers unrelated to the profile above, and do not inflate scale beyond what the profile supports.
"""


def build_prompt(context: dict) -> str:
    return f"""You are an Azure solution planning assistant.

Use only the supplied commercial deal context and workload profile.
Generate a practical Azure bill of materials for the workload. Avoid overengineering.
Consider availability, disaster recovery, regions, and production versus non-production environments.
Default production services to region "{PRIMARY_REGION}" and disaster-recovery/secondary services to
region "{SECONDARY_REGION}" unless the workload profile explicitly names a different region.
Return only valid JSON. Do not return markdown.
Do not calculate or include pricing, unit prices, monthly cost, annual cost, TCO, ROI, discounts, margins, NPV, or payback.
Do not invent customer-specific facts that are not present in the context.
Include rationale and confidence for each service.
When a catalog of supported services is provided for a planning stage, prefer selecting a "catalog_id"
from that list so the service can later be priced deterministically; only invent a free-form
azure_service/sku when no supplied catalog entry fits the requirement.

Required JSON shape:
{{
  "architecture_version": "1.0",
  "deal_id": "{context['deal']['deal_id']}",
  "summary": "string",
  "assumptions": ["string"],
  "services": [
    {{
      "service_id": "svc-001",
      "service_category": "compute|database|storage|networking|monitoring|backup|security|analytics|integration|ai|container|messaging|identity",
      "catalog_id": "string or null (preferred when a supplied catalog entry fits)",
      "azure_service": "string",
      "sku": "string or null",
      "tier": "string or null",
      "region": "azure region id such as eastus",
      "environment": "production|staging|development|test|non-production",
      "quantity": "integer, sized realistically against the workload profile -- not always 1",
      "usage_type": "hours|capacity|ingestion|operations|requests|messages",
      "monthly_usage_per_unit": "number, sized realistically against the workload profile",
      "unit": "hour|GB-month|GB|operation|request|message|vCore-hour|gateway-hour|DWU-hour",
      "dr_replication": false,
      "rationale": "string -- must reference the workload numbers that justify quantity/usage",
      "confidence": "LOW|MEDIUM|HIGH"
    }}
  ]
}}

CONTEXT:
{json.dumps(context, indent=2, sort_keys=True)}
"""


def call_llm(prompt: str) -> str:
    api_key = os.environ.get("TOGETHER_API_KEY", "")
    if not api_key:
        raise ArchitecturePlannerError("LLM_UNAVAILABLE", "TOGETHER_API_KEY is not configured.")
    try:
        response = request_completion(
            TOGETHER_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": TOGETHER_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 2500},
            timeout=75,
        )
    except requests.exceptions.Timeout as exc:
        raise ArchitecturePlannerError("LLM_TIMEOUT", "The AI planning service took too long to respond. This is usually transient -- please try again.") from exc
    except requests.exceptions.RequestException as exc:
        raise ArchitecturePlannerError("LLM_CONNECTION_ERROR", f"Could not reach the AI planning service: {exc}") from exc
    if response.status_code >= 400:
        raise ArchitecturePlannerError("LLM_REQUEST_FAILED", response.text[:500])
    return response.json()["choices"][0]["message"]["content"]


def parse_architecture_response(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArchitecturePlannerError("MALFORMED_ARCHITECTURE_JSON", str(exc)) from exc
    if not isinstance(payload, dict):
        raise ArchitecturePlannerError("INVALID_ARCHITECTURE_JSON", "Architecture response must be a JSON object.")
    return payload


def normalize_architecture(architecture: dict, enrich_from_catalog: bool = True) -> dict:
    """enrich_from_catalog=True (fresh LLM generation): a selected catalog_id
    is authoritative, so azure_service/sku/arm_sku_name/product_name are
    OVERWRITTEN from the catalog entry -- a slightly-off free-form label the
    LLM also wrote (e.g. "Microsoft Sentinel" vs. the catalog's real
    "Sentinel") must not win, or pricing can never resolve it even though
    catalog_id is set. enrich_from_catalog=False (manual BOM edits via
    update_architecture): the user's hand-typed azure_service/sku always
    wins; catalog fields are only used to fill in gaps, since a stale
    catalog_id can linger on a row the user has since edited by hand."""
    normalized = deepcopy(architecture)
    normalized["architecture_version"] = str(normalized.get("architecture_version") or "1.0")
    for service in normalized.get("services", []) or []:
        cat = str(service.get("service_category") or "").strip().lower()
        service["service_category"] = CATEGORY_ALIASES.get(cat, cat)
        catalog_entry = get_catalog_entry(service["catalog_id"]) if service.get("catalog_id") else None
        if catalog_entry and catalog_entry.get("resolution_status") == "RESOLVED":
            # Backend enrichment from the selected catalog entry (section 31):
            # the LLM only ever names a catalog_id -- service/product/SKU
            # identity is filled in deterministically here, never invented.
            if enrich_from_catalog:
                service["azure_service"] = catalog_entry.get("service_name")
                service["sku"] = catalog_entry.get("arm_sku_name") or catalog_entry.get("preferred_sku")
            else:
                service.setdefault("azure_service", catalog_entry.get("service_name"))
                service.setdefault("sku", catalog_entry.get("arm_sku_name") or catalog_entry.get("preferred_sku"))
            service["arm_sku_name"] = catalog_entry.get("arm_sku_name")
            service["product_name"] = catalog_entry.get("product_name")
            service["pricing_lookup_key"] = catalog_entry.get("pricing_lookup_key")
        region = service.get("region")
        service["region"] = REGION_ALIASES.get(region, str(region or "").replace(" ", "").lower()) or PRIMARY_REGION
        env = str(service.get("environment") or "").strip().lower()
        service["environment"] = ENV_ALIASES.get(env, env)
        if service.get("sku") is not None:
            service["sku"] = str(service.get("sku")).strip()
        if service.get("tier") is not None:
            service["tier"] = str(service.get("tier")).strip()
        service["usage_type"] = str(service.get("usage_type") or "").strip().lower()
        service["unit"] = str(service.get("unit") or "").strip()
        service["confidence"] = str(service.get("confidence") or "").strip().upper()
    return normalized


def validate_architecture(architecture: dict, expected_deal_id: str | None = None) -> list[str]:
    errors = _contains_pricing_field(architecture)
    if not isinstance(architecture, dict):
        return ["architecture must be an object"]
    if expected_deal_id and architecture.get("deal_id") != expected_deal_id:
        errors.append("deal_id must match requested deal")
    if not architecture.get("summary"):
        errors.append("summary is required")
    services = architecture.get("services")
    if not isinstance(services, list) or not services:
        errors.append("services must be a non-empty list")
        return errors
    seen_ids: set[str] = set()
    seen_rows: set[tuple] = set()
    for i, service in enumerate(services):
        sid = service.get("service_id")
        if not sid:
            errors.append(f"services[{i}].service_id is required")
        elif sid in seen_ids:
            errors.append(f"duplicate service_id {sid}")
        seen_ids.add(sid)
        category = service.get("service_category")
        if category not in ALLOWED_CATEGORIES:
            errors.append(f"services[{i}].service_category is invalid")
        if service.get("catalog_id"):
            catalog_entry = get_catalog_entry(service["catalog_id"])
            if not catalog_entry or catalog_entry.get("resolution_status") != "RESOLVED":
                errors.append(f"services[{i}].catalog_id does not reference a supported catalog entry")
        if not service.get("azure_service"):
            errors.append(f"services[{i}].azure_service is required")
        if not service.get("region"):
            errors.append(f"services[{i}].region is required")
        if service.get("confidence") not in ALLOWED_CONFIDENCE:
            errors.append(f"services[{i}].confidence is invalid")
        quantity = service.get("quantity")
        if not isinstance(quantity, (int, float)) or quantity <= 0:
            errors.append(f"services[{i}].quantity must be > 0")
        usage = service.get("monthly_usage_per_unit")
        if not isinstance(usage, (int, float)) or usage < 0:
            errors.append(f"services[{i}].monthly_usage_per_unit must be >= 0")
        if not service.get("usage_type") or not service.get("unit"):
            errors.append(f"services[{i}].usage_type and unit are required")
        row = (category, service.get("azure_service"), service.get("sku"), service.get("tier"), service.get("region"), service.get("environment"), quantity, usage, service.get("unit"))
        if row in seen_rows:
            errors.append(f"duplicate exact service row at services[{i}]")
        seen_rows.add(row)
    return errors


def _canonical_service_name(azure_service: str | None) -> str | None:
    if not azure_service:
        return None
    return SERVICE_NAME_MAP.get(azure_service, azure_service)


def _catalog_covered_service_names(catalog: dict) -> set[str]:
    return {
        e.get("service_name")
        for e in catalog.get("entries", [])
        if e.get("resolution_status") == "RESOLVED" and e.get("service_name")
    }


def find_catalog_conformance_violations(architecture: dict, catalog: dict | None = None) -> list[dict]:
    """Reject free-form services when the curated catalog already covers
    them (section 31, 47). A service is only allowed to skip catalog_id when
    no RESOLVED catalog entry exists for its (canonicalized) azure_service --
    e.g. a genuinely niche service outside the curated ~90 entries. Manual
    user edits (update_architecture) are intentionally NOT run through this
    check; it only gates automated LLM generation."""
    catalog = catalog if catalog is not None else load_catalog()
    if not catalog or not catalog.get("entries"):
        return []
    covered = _catalog_covered_service_names(catalog)
    violations: list[dict] = []
    for i, service in enumerate(architecture.get("services", []) or []):
        if service.get("catalog_id"):
            continue
        canonical = _canonical_service_name(service.get("azure_service"))
        if canonical and canonical in covered:
            matching_ids = sorted({
                e["catalog_id"] for e in catalog["entries"]
                if e.get("resolution_status") == "RESOLVED" and e.get("service_name") == canonical
            })
            violations.append({
                "index": i,
                "service_id": service.get("service_id"),
                "azure_service": service.get("azure_service"),
                "canonical_service_name": canonical,
                "candidate_catalog_ids": matching_ids[:8],
                "message": (
                    f"services[{i}] ({service.get('azure_service')!r}) is free-form but the catalog covers "
                    f"'{canonical}' -- must set catalog_id to one of {matching_ids[:8]}."
                ),
            })
    return violations


def _build_correction_note(structural_errors: list[str] | None, conformance_violations: list[dict] | None) -> str:
    lines = ["\nCORRECTIONS REQUIRED before this is acceptable -- fix these specific problems and return the full corrected JSON BOM:"]
    for err in structural_errors or []:
        lines.append(f"- {err}")
    for v in conformance_violations or []:
        lines.append(f"- {v['message']}")
    return "\n".join(lines)


def _wrap_architecture(architecture: dict, source: str, model: str, user_modified: bool = False, status: str = "generated") -> dict:
    return {
        "deal_id": architecture.get("deal_id"),
        "architecture": architecture,
        "architecture_version": architecture.get("architecture_version", "1.0"),
        "generated_at": _utc_now(),
        "source": source,
        "model": model,
        "validation_status": status,
        "user_modified": user_modified,
        "architecture_mode": architecture.get("architecture_mode", "EXPECTED"),
    }


def architecture_completeness(architecture: dict, profile: dict | None = None) -> dict:
    services = architecture.get("services", [])
    domains = {str(s.get("service_domain") or s.get("service_category") or "").lower() for s in services}
    required = {"platform", "application", "data", "security", "operations", "integration"}
    p = profile or {}
    if p.get("ai_workloads_required"): required.add("ai")
    if p.get("dr_required"): required.add("dr")
    aliases = {"networking": "platform", "monitoring": "operations", "backup": "operations", "container": "application", "messaging": "integration", "analytics": "data", "database": "data", "storage": "data"}
    covered = {aliases.get(d, d) for d in domains}
    missing = sorted(required - covered)
    return {"required_domains": len(required), "covered_domains": len(required - set(missing)), "coverage_percent": round((len(required)-len(missing))*100/len(required)) if required else 100, "missing_domains": missing}


def load_architecture_store() -> dict:
    return _load_json(ARCHITECTURE_STORE_PATH, {})


def get_architecture(deal_id: str) -> dict | None:
    return load_architecture_store().get(deal_id) or load_architecture_store().get(f"{deal_id}:EXPECTED")


def get_architecture_for_mode(deal_id: str, mode: str = "EXPECTED") -> dict | None:
    store = load_architecture_store()
    return store.get(f"{deal_id}:{str(mode).upper()}") or (store.get(deal_id) if str(mode).upper() == "EXPECTED" else None)


def invalidate_architecture(deal_id: str) -> None:
    store = load_architecture_store()
    if deal_id in store:
        store[deal_id]["stale"] = True
        store[deal_id]["validation_status"] = "STALE"
        _write_json(ARCHITECTURE_STORE_PATH, store)
    try:
        from tools.azure_retail_pricing import invalidate_pricing
        invalidate_pricing(deal_id)
    except (ImportError, AttributeError):
        pass


def save_architecture(record: dict) -> dict:
    store = load_architecture_store()
    mode = str(record.get("architecture_mode") or record.get("architecture", {}).get("architecture_mode") or "EXPECTED").upper()
    record["architecture_mode"] = mode
    store[f"{record['deal_id']}:{mode}"] = record
    if mode == "EXPECTED":
        store[record["deal_id"]] = record
    _write_json(ARCHITECTURE_STORE_PATH, store)
    return record


def load_golden_architecture() -> dict:
    return _load_json(GOLDEN_ARCHITECTURE_PATH, {})


_STAGE_LABELS = ("enterprise requirements", "platform and security", "application and integration", "data analytics and AI", "reviewer and synthesizer")
_STAGE_KEYS = ("enterprise", "platform", "application", "data", "reviewer")
_STAGE_KEY_TO_CATALOG_STAGE = {"platform": "platform_security", "application": "application_integration", "data": "data_analytics_ai"}


def _run_generation_stages(llm_func, prompt: str, enterprise_scale: str | None, context: dict, correction_note: str = "") -> tuple[str, list[str], dict]:
    """Runs the multi-stage planning pipeline once and returns the final raw
    response, the stages completed, and how many catalog options each stage
    saw. Each application/platform/data stage receives only its own
    price-free, catalog-constrained slice of supported services (section 33)
    -- the full master pricing snapshot is never sent to the LLM.

    Only the "reviewer and synthesizer" stage's response is actually parsed
    as the final BOM (the earlier stages are narrower planning passes whose
    raw text is not reused), so that stage is the one that must receive the
    FULL combined catalog, the quantity/scale sizing guidance, and any
    corrective feedback -- not just a slice, and not diluted (with the
    latency/timeout risk that comes with it) across every earlier stage
    whose output is discarded anyway."""
    catalog_options_by_stage: dict[str, int] = {}
    combined_catalog_options: list[dict] = []
    seen_catalog_ids: set[str] = set()
    raw = None
    stages_completed: list[str] = []
    for label, key in zip(_STAGE_LABELS, _STAGE_KEYS):
        stage_prompt = prompt + f"\nPLANNING STAGE: {label}. Return JSON planning notes or the final BOM."
        catalog_stage = _STAGE_KEY_TO_CATALOG_STAGE.get(key)
        if catalog_stage:
            try:
                options = get_catalog_for_stage(catalog_stage, enterprise_scale=enterprise_scale)
            except Exception:
                options = []
            catalog_options_by_stage[key] = len(options)
            for opt in options:
                if opt.get("catalog_id") not in seen_catalog_ids:
                    seen_catalog_ids.add(opt.get("catalog_id"))
                    combined_catalog_options.append(opt)
            if options:
                stage_prompt += f"\nSUPPORTED CATALOG OPTIONS FOR THIS STAGE (choose catalog_id from this list when possible; no pricing is included):\n{json.dumps(options, indent=2)}"
        if key == "reviewer":
            stage_prompt += _sizing_guidance(context)
            if combined_catalog_options:
                stage_prompt += (
                    "\nFULL SUPPORTED CATALOG (every catalog_id you may use across all domains -- "
                    "set catalog_id on every service that has a matching entry here; only invent a "
                    f"free-form azure_service/sku when nothing here fits):\n{json.dumps(combined_catalog_options, indent=2)}"
                )
            if correction_note:
                stage_prompt += correction_note
        raw = llm_func(stage_prompt)
        stages_completed.append(key)
    return raw, stages_completed, catalog_options_by_stage


def generate_architecture(deal_id: str, llm_func=None, architecture_mode: str = "EXPECTED") -> dict:
    architecture_mode = str(architecture_mode or "EXPECTED").upper()
    if architecture_mode not in ARCHITECTURE_MODES:
        raise ArchitecturePlannerError("INVALID_ARCHITECTURE_MODE", "architecture_mode must be EXPECTED or COMMITMENT_ALIGNED.")
    llm_func = llm_func or call_llm
    context = build_architecture_context(deal_id)
    prompt = build_prompt(context)
    enterprise_scale = (context.get("enterprise_profile") or {}).get("enterprise_scale")
    catalog = load_catalog()

    correction_note = ""
    normalized = None
    stages_completed: list[str] = []
    catalog_options_by_stage: dict[str, int] = {}
    structural_errors: list[str] = []
    conformance_violations: list[dict] = []

    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        raw, stages_completed, catalog_options_by_stage = _run_generation_stages(llm_func, prompt, enterprise_scale, context, correction_note)
        parsed = parse_architecture_response(raw)
        parsed["architecture_mode"] = architecture_mode
        parsed["completeness"] = architecture_completeness(parsed, context.get("consumption_profile"))
        normalized = normalize_architecture(parsed)
        structural_errors = validate_architecture(normalized, deal_id)
        if structural_errors:
            if attempt == MAX_GENERATION_ATTEMPTS:
                failed = _wrap_architecture(normalized, "llm", TOGETHER_MODEL, False, "validation_failed")
                failed["validation_errors"] = structural_errors
                failed["planning_trace"] = {"stages_completed": stages_completed, "generation_attempts": attempt}
                save_architecture(failed)
                raise ArchitecturePlannerError("ARCHITECTURE_VALIDATION_FAILED", "; ".join(structural_errors))
            print(f"[ConsumptionArchitecture] deal_id={deal_id} attempt={attempt} structural_errors={len(structural_errors)} retrying")
            correction_note = _build_correction_note(structural_errors, None)
            continue

        # Catalog-constrained generation (section 31, 47): a free-form
        # azure_service that the curated catalog already covers is rejected
        # and the generation is retried with the exact catalog_id(s) it must
        # use, rather than being silently accepted as an unpriceable guess.
        conformance_violations = find_catalog_conformance_violations(normalized, catalog)
        if conformance_violations:
            if attempt == MAX_GENERATION_ATTEMPTS:
                failed = _wrap_architecture(normalized, "llm", TOGETHER_MODEL, False, "catalog_conformance_failed")
                failed["catalog_conformance_violations"] = conformance_violations
                failed["planning_trace"] = {"stages_completed": stages_completed, "generation_attempts": attempt, "catalog_conformance": "FAILED"}
                save_architecture(failed)
                raise ArchitecturePlannerError(
                    "CATALOG_CONFORMANCE_FAILED",
                    "; ".join(v["message"] for v in conformance_violations),
                )
            print(f"[ConsumptionArchitecture] deal_id={deal_id} attempt={attempt} catalog_violations={len(conformance_violations)} retrying")
            correction_note = _build_correction_note(None, conformance_violations)
            continue

        record = _wrap_architecture(normalized, "llm", TOGETHER_MODEL, False, "generated")
        record["planning_trace"] = {
            "stages_completed": stages_completed,
            "review_status": "completed",
            "catalog_options_by_stage": catalog_options_by_stage,
            "catalog_conformance": "PASSED" if catalog and catalog.get("entries") else "SKIPPED_NO_CATALOG",
            "generation_attempts": attempt,
        }
        print(f"[ConsumptionArchitecture] deal_id={deal_id} success=true attempt={attempt} services={len(normalized.get('services', []))} model={TOGETHER_MODEL}")
        saved = save_architecture(record)
        try:
            from tools.azure_retail_pricing import invalidate_pricing
        except ModuleNotFoundError:
            from azure_retail_pricing import invalidate_pricing
        invalidate_pricing(deal_id)
        return saved

    # Unreachable: the loop always returns or raises on its final attempt.
    raise ArchitecturePlannerError("ARCHITECTURE_GENERATION_FAILED", "Exhausted generation attempts without a valid result.")


def update_architecture(deal_id: str, architecture: dict) -> dict:
    if not get_deal_input(deal_id):
        raise ArchitecturePlannerError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")
    normalized = normalize_architecture({**architecture, "deal_id": deal_id}, enrich_from_catalog=False)
    errors = validate_architecture(normalized, deal_id)
    if errors:
        raise ArchitecturePlannerError("ARCHITECTURE_VALIDATION_FAILED", "; ".join(errors))
    record = _wrap_architecture(normalized, "manual_override", "user", True, "user_modified")
    print(f"[ConsumptionArchitecture] deal_id={deal_id} user_modified=true services={len(normalized.get('services', []))}")
    saved = save_architecture(record)
    try:
        from tools.azure_retail_pricing import invalidate_pricing
    except ModuleNotFoundError:
        from azure_retail_pricing import invalidate_pricing
    invalidate_pricing(deal_id)
    return saved
