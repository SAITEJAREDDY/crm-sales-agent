"""
generate_resource_estimates.py
Generates infrastructure resource estimates for all deals in the pipeline
by calling Together AI Llama 3.3 70B once per deal.

Usage:
    python generate_resource_estimates.py           # full run (skips existing)
    python generate_resource_estimates.py --test    # 3-deal validation only
    python generate_resource_estimates.py --reset   # clear progress and re-run
"""

from __future__ import annotations

from tools.llm_client import TOGETHER_CHAT_URL, TOGETHER_MODEL, request_completion

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).resolve().parent
KB_DIR         = BASE_DIR / "knowledge_base"
PIPELINE_JSON  = KB_DIR / "meddpicc_pipeline.json"
OPP_JSON       = KB_DIR / "raw" / "opportunities_clean.json"
PRODUCT_MAP    = KB_DIR / "kb" / "product_mapping.json"
PROFILES_DIR   = KB_DIR / "profiles"
CAPS_DIR       = KB_DIR / "kb" / "capabilities"
OUTPUT_DIR     = KB_DIR / "resource_estimates"
PROGRESS_FILE  = OUTPUT_DIR / "_progress.json"
FAILED_FILE    = OUTPUT_DIR / "_failed_estimates.json"
ROLLUP_FILE    = OUTPUT_DIR / "resource_pipeline.json"
SUMMARY_FILE   = OUTPUT_DIR / "_summary.json"
CSV_FILE       = OUTPUT_DIR / "resource_pipeline.csv"

TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")
MODEL = TOGETHER_MODEL

# ── Product prefix → CRM product name (for product_mapping.json lookup) ───────
PRODUCT_PREFIX_MAP: dict[str, str] = {
    # Exact matches (most common)
    "CloudCore":       "CloudCore",
    "RiskEngine":      "RiskEngine",
    "SecureVault":     "SecureVault",
    "InsightIQ":       "InsightIQ",
    "AuditTrail":      "AuditTrail",
    "RegTrack":        "RegTrack",
    "StreamlineAML":   "StreamlineAML",
    "CapitalConnect":  "CapitalConnect",
    "LendingOS":       "LendingOS",
    "Nexus Platform":  "Nexus Platform",
    "ReportingCloud":  "ReportingCloud",
    "DataFabric":      "DataFabric",
    "DataVault":       "DataVault",
    "SmartLedger":     "SmartLedger",
    "TreasuryLink":    "TreasuryLink",
    "FraudGuard":      "FraudGuard",
    "ComplianceAI":    "ComplianceAI",
    "AnalyticsHub":    "AnalyticsHub",
    "DataBridge":      "DataBridge",
    "WealthPortal":    "WealthPortal",
    # Abbreviated / variant prefixes
    "WealthM":         "WealthPortal",
    "FraudD":          "FraudGuard",
    "TreasuryM":       "TreasuryLink",
    "SecurityO":       "SecureVault",
    "CloudM":          "CloudCore",
    "RegTechP":        "RegTrack",
    "DataF":           "DataFabric",
    "PaymentsI":       "CapitalConnect",
    "AI/MLI":          "RiskEngine",
    "ComplianceS":     "ComplianceAI",
    "RiskA":           "RiskEngine",
    "CoreP":           "Nexus Platform",
    "CoreB":           "Nexus Platform",
}


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def extract_product_prefix(deal_name: str) -> str:
    """Return the product prefix from a deal name like 'DataFabric — Proof of Concept'."""
    parts = re.split(r"\s*[—–-]\s*", deal_name, maxsplit=1)
    return parts[0].strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Data loading ──────────────────────────────────────────────────────────────

def load_pipeline() -> list[dict]:
    with open(PIPELINE_JSON, encoding="utf-8") as f:
        return json.load(f)


def load_opportunities() -> dict[str, dict]:
    """Return dict keyed by deal name (lowercased) for O(1) lookup."""
    with open(OPP_JSON, encoding="utf-8") as f:
        raw = json.load(f)
    return {rec["name"].lower(): rec for rec in raw if rec.get("name")}


def load_product_mapping() -> dict[str, dict]:
    """Return dict keyed by crm_product name."""
    with open(PRODUCT_MAP, encoding="utf-8") as f:
        data = json.load(f)
    mappings = data.get("mappings", data) if isinstance(data, dict) else data
    if isinstance(mappings, list):
        return {m["crm_product"]: m for m in mappings}
    return mappings


def list_capability_files() -> list[str]:
    return [p.stem for p in CAPS_DIR.glob("*.md") if p.stem != "README"]


def _profile_slug(company: str) -> str:
    """Profiles are stored as lowercase alphanumeric only, no separators."""
    return re.sub(r"[^a-z0-9]", "", company.lower())


def load_company_profile(company: str) -> dict[str, str]:
    """Return sections: size_structure, tech_profile, challenges."""
    slug = _profile_slug(company)
    path = PROFILES_DIR / f"{slug}.md"
    if not path.exists():
        return {"size_structure": "", "tech_profile": "", "challenges": ""}

    text = path.read_text(encoding="utf-8")

    def _section(header: str) -> str:
        # Profiles use **Header** bold style, not ## markdown headers
        pattern = rf"\*\*{re.escape(header)}\*\*\s*\n(.*?)(?=\n\*\*|\Z)"
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
        # Fallback: ## markdown headers
        pattern2 = rf"##\s*{re.escape(header)}\s*\n(.*?)(?=\n##|\Z)"
        m2 = re.search(pattern2, text, re.DOTALL | re.IGNORECASE)
        return m2.group(1).strip() if m2 else ""

    return {
        "size_structure": _section("Size & Structure") or _section("Size and Structure"),
        "tech_profile":   _section("Technology Profile"),
        "challenges":     _section("Business Challenges & Pain Points") or _section("Business Challenges"),
    }


def load_kb_capability(kb_slug: str) -> dict[str, str]:
    """Return reference_architectures and resource_requirements sections."""
    path = CAPS_DIR / f"{kb_slug}.md"
    if not path.exists():
        return {"reference_architectures": "", "resource_requirements": ""}

    text = path.read_text(encoding="utf-8")

    def _section(header: str) -> str:
        pattern = rf"###\s*{re.escape(header)}\s*\n(.*?)(?=\n###|\n##|\Z)"
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    return {
        "reference_architectures": _section("Reference Architectures"),
        "resource_requirements":   _section("Implementation Resource Requirements"),
    }


# ── Implementation pattern assignment ─────────────────────────────────────────

def assign_implementation_pattern(amount: float, evidence_text: str) -> str:
    if amount < 100_000:
        tier = 0  # Quick Start
    elif amount <= 500_000:
        tier = 1  # Production Grade
    else:
        tier = 2  # Enterprise Scale

    text_lower = evidence_text.lower()

    up_signals = [
        ("legacy system", "migration"), ("legacy", "migration"),
        ("complex integration", None), ("multiple systems", None),
        ("regulated", "compliance"), ("regulated", "audit"),
        ("high availability", None), ("disaster recovery", None),
    ]
    down_signals = [
        ("simple", None), ("straightforward", None), ("quick start", None),
        ("pilot", None), ("proof of concept", None), ("poc", None),
        ("small team", None), ("limited scope", None),
    ]

    def signal_present(pair: tuple) -> bool:
        a, b = pair
        if a in text_lower:
            return b is None or b in text_lower
        return False

    if any(signal_present(s) for s in up_signals):
        tier = min(tier + 1, 2)
    elif any(signal_present(s) for s in down_signals):
        tier = max(tier - 1, 0)

    return ["Quick Start", "Production Grade", "Enterprise Scale"][tier]


# ── Together AI call ──────────────────────────────────────────────────────────

def _call_together(prompt: str, max_tokens: int = 1200) -> str | None:
    import requests

    try:
        resp = request_completion(
            TOGETHER_CHAT_URL,
            headers={
                "Authorization": f"Bearer {TOGETHER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"    [Together AI error] {e}")
        return None


def _extract_json(text: str) -> dict | None:
    """Extract JSON from a response string."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return None


# ── Estimation prompt ─────────────────────────────────────────────────────────

def build_prompt(ctx: dict) -> str:
    return f"""You are a principal solutions architect at a cloud technology company with deep expertise in {ctx['base_category']} implementations.

Your job is to estimate the cloud infrastructure resources required for this specific customer engagement. Base your estimate on the signals provided. Be specific and grounded — not generic.

DEAL CONTEXT:
Company: {ctx['company']}
Employees: {ctx['employees']}
Location: {ctx['city']}, {ctx['country']}
Product: {ctx['product']} ({ctx['base_category']})
Deal Amount: ${ctx['amount']:,.0f}
Stage: {ctx['stage']}
Implementation Pattern: {ctx['implementation_pattern']}

BUSINESS SIGNALS FROM SALES NOTES:
Pain identified: {ctx['pain_evidence']}
Metrics mentioned: {ctx['metrics_evidence']}
Decision criteria: {ctx['decision_criteria_evidence']}

COMPANY TECHNOLOGY PROFILE:
{ctx['tech_profile']}

COMPANY SIZE AND STRUCTURE:
{ctx['size_structure']}

BUSINESS CHALLENGES:
{ctx['challenges']}

KB REFERENCE ARCHITECTURE:
{ctx['reference_architectures'][:1500]}

Based on all signals above, estimate the cloud infrastructure required for this implementation.

Think through:
1. What workload type does this company run based on their industry, size, and pain signals?
2. What scale does their operation suggest — transactions per second, data volume, user count?
3. What availability and compliance requirements does their industry typically mandate?
4. What does a {ctx['implementation_pattern']} implementation of {ctx['base_category']} look like architecturally?

Return ONLY valid JSON matching this exact schema. No preamble. No explanation. Raw JSON only.

{{
  "deal_id": "",
  "deal_name": "",
  "company": "",
  "product": "",
  "base_category": "",
  "implementation_pattern": "",
  "estimated_at": "",
  "confidence": "HIGH or MEDIUM or LOW",
  "confidence_reasoning": "one sentence explaining confidence level",
  "workload_profile": {{
    "workload_type": "OLTP or Analytics or Streaming or Mixed or API or Batch",
    "estimated_daily_transactions": 0,
    "estimated_peak_rps": 0,
    "estimated_data_volume_tb": 0,
    "estimated_active_users": 0,
    "availability_requirement": "99.9% or 99.95% or 99.99%",
    "compliance_flags": ["list any that apply: SOC2, PCI-DSS, HIPAA, FedRAMP, GDPR"]
  }},
  "compute": {{
    "primary_instances": {{
      "type": "e.g. m5.xlarge or equivalent",
      "count": 0,
      "purpose": "what these instances do",
      "monthly_cost_usd": 0
    }},
    "secondary_instances": {{
      "type": "",
      "count": 0,
      "purpose": "",
      "monthly_cost_usd": 0
    }},
    "auto_scaling": true,
    "multi_az": true,
    "total_compute_monthly_usd": 0
  }},
  "storage": {{
    "primary_storage_tb": 0,
    "primary_storage_type": "SSD or HDD or Object",
    "backup_storage_tb": 0,
    "archive_storage_tb": 0,
    "storage_growth_rate_pct": 0,
    "total_storage_monthly_usd": 0
  }},
  "database": {{
    "engine": "e.g. PostgreSQL or MySQL or NoSQL",
    "tier": "small or medium or large or xlarge",
    "multi_az": true,
    "read_replicas": 0,
    "storage_gb": 0,
    "monthly_cost_usd": 0
  }},
  "network": {{
    "bandwidth_gbps": 0,
    "cdn_required": true,
    "vpn_or_direct_connect": true,
    "load_balancer_type": "ALB or NLB or Global",
    "monthly_cost_usd": 0
  }},
  "additional_services": [
    {{
      "service": "service name e.g. Message Queue",
      "purpose": "what it does in this architecture",
      "monthly_cost_usd": 0
    }}
  ],
  "monthly_consumption": {{
    "compute_usd": 0,
    "storage_usd": 0,
    "database_usd": 0,
    "network_usd": 0,
    "additional_services_usd": 0,
    "total_monthly_usd": 0,
    "total_annual_usd": 0
  }},
  "implementation_resources": {{
    "sa_days_required": 0,
    "proserv_days_required": 0,
    "partner_days_required": 0,
    "estimated_timeline_weeks": 0,
    "one_time_implementation_cost_usd": 0
  }},
  "architect_narrative": "2-3 sentences explaining the architecture rationale specific to this company. Reference their size, industry, and key signals from the notes."
}}"""


# ── Validation ────────────────────────────────────────────────────────────────

def validate_and_fix(estimate: dict, ctx: dict) -> tuple[bool, list[str], dict]:
    errors: list[str] = []
    mc = estimate.get("monthly_consumption", {})

    # Check required top-level fields
    required_top = ["deal_id", "deal_name", "company", "product", "base_category",
                    "implementation_pattern", "confidence", "workload_profile",
                    "compute", "storage", "database", "network", "monthly_consumption",
                    "implementation_resources", "architect_narrative"]
    for field in required_top:
        if field not in estimate:
            errors.append(f"Missing field: {field}")

    if errors:
        return False, errors, estimate

    # Fix and validate monthly totals
    computed_total = (
        mc.get("compute_usd", 0) +
        mc.get("storage_usd", 0) +
        mc.get("database_usd", 0) +
        mc.get("network_usd", 0) +
        mc.get("additional_services_usd", 0)
    )

    if computed_total > 0:
        mc["total_monthly_usd"] = computed_total
        mc["total_annual_usd"] = computed_total * 12
        estimate["monthly_consumption"] = mc

    if mc.get("total_monthly_usd", 0) <= 0:
        errors.append("total_monthly_usd is zero or missing — estimate is unusable")
        return False, errors, estimate

    # Validate confidence value
    if estimate.get("confidence") not in ("HIGH", "MEDIUM", "LOW"):
        estimate["confidence"] = "MEDIUM"

    # Enforce implementation_pattern matches assignment
    estimate["implementation_pattern"] = ctx["implementation_pattern"]

    # Stamp metadata
    estimate["deal_id"]   = ctx["deal_id"]
    estimate["deal_name"] = ctx["deal_name"]
    estimate["company"]   = ctx["company"]
    estimate["product"]   = ctx["product"]
    estimate["base_category"] = ctx["base_category"]
    estimate["estimated_at"]  = now_iso()

    return True, [], estimate


# ── Progress tracking ─────────────────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "total_deals": 0, "estimated": 0, "skipped_existing": 0, "failed": 0,
        "high_confidence": 0, "medium_confidence": 0, "low_confidence": 0,
        "total_monthly_consumption_usd": 0.0, "total_annual_consumption_usd": 0.0,
        "last_deal_processed": "", "started_at": now_iso(), "updated_at": now_iso(),
    }


def save_progress(progress: dict) -> None:
    progress["updated_at"] = now_iso()
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)


def append_to_rollup(estimate: dict) -> None:
    if ROLLUP_FILE.exists():
        with open(ROLLUP_FILE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = []
    data.append(estimate)
    with open(ROLLUP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def log_failure(deal_name: str, reason: str) -> None:
    if FAILED_FILE.exists():
        with open(FAILED_FILE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = []
    data.append({"deal_name": deal_name, "reason": reason, "at": now_iso()})
    with open(FAILED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ── Per-deal context builder ──────────────────────────────────────────────────

def build_context(deal: dict, opp_lookup: dict, prod_map: dict) -> dict:
    deal_name = deal["deal_name"]
    company   = deal["company"]
    stage     = deal["stage"]
    amount    = float(deal.get("amount") or 0)
    deal_id   = deal["deal_id"]

    # Extract product
    prefix  = extract_product_prefix(deal_name)
    crm_prod = PRODUCT_PREFIX_MAP.get(prefix, prefix)
    prod_entry = prod_map.get(crm_prod, {})

    # Fallback fuzzy match if exact not found
    if not prod_entry:
        for pname, pdata in prod_map.items():
            if prefix.lower() in pname.lower() or pname.lower() in prefix.lower():
                prod_entry = pdata
                crm_prod = pname
                break

    base_category = prod_entry.get("base_category", crm_prod or prefix)
    kb_slug       = prod_entry.get("kb_slug", slugify(base_category))

    # CRM opportunity data
    opp = opp_lookup.get(deal_name.lower(), {})
    employees = opp.get("company_employees") or 0
    city      = opp.get("company_city") or ""
    country   = opp.get("company_country") or ""

    # MEDDPICC elements
    elements = deal.get("elements", {})
    pain_evidence = elements.get("identify_pain", {}).get("evidence", "")
    metrics_evidence = elements.get("metrics", {}).get("evidence", "")
    decision_criteria_evidence = elements.get("decision_criteria", {}).get("evidence", "")
    flags = deal.get("flags", {})

    # Company profile
    profile = load_company_profile(company)

    # KB capability
    kb = load_kb_capability(kb_slug)

    # Evidence text for pattern assignment
    all_evidence = " ".join([
        pain_evidence, metrics_evidence, decision_criteria_evidence,
        profile["tech_profile"], profile["challenges"],
    ])

    impl_pattern = assign_implementation_pattern(amount, all_evidence)

    return {
        "deal_id":             deal_id,
        "deal_name":           deal_name,
        "company":             company,
        "stage":               stage,
        "amount":              amount,
        "product":             crm_prod,
        "base_category":       base_category,
        "kb_slug":             kb_slug,
        "implementation_pattern": impl_pattern,
        "employees":           employees,
        "city":                city,
        "country":             country,
        "pain_evidence":       pain_evidence,
        "metrics_evidence":    metrics_evidence,
        "decision_criteria_evidence": decision_criteria_evidence,
        "flags":               flags,
        "size_structure":      profile["size_structure"],
        "tech_profile":        profile["tech_profile"],
        "challenges":          profile["challenges"],
        "reference_architectures": kb["reference_architectures"],
        "resource_requirements":   kb["resource_requirements"],
        "overall_score":       deal.get("overall_score", ""),
        "assigned_rep":        deal.get("assigned_rep", ""),
        "probability":         deal.get("probability", 0),
    }


# ── Single deal estimator ─────────────────────────────────────────────────────

def estimate_deal(ctx: dict, retry: bool = True) -> dict | None:
    prompt = build_prompt(ctx)
    raw = _call_together(prompt)

    if raw is None:
        if retry:
            print("    Retrying in 5s...")
            time.sleep(5)
            raw = _call_together(prompt)

    if raw is None:
        return None

    estimate = _extract_json(raw)
    if estimate is None:
        if retry:
            # Retry with explicit JSON instruction
            prompt2 = prompt + "\n\nIMPORTANT: Your previous response was not valid JSON. Return ONLY the JSON object, nothing else."
            time.sleep(5)
            raw2 = _call_together(prompt2)
            if raw2:
                estimate = _extract_json(raw2)

    return estimate


def output_path_for(ctx: dict) -> Path:
    company_slug = slugify(ctx["company"])
    deal_slug    = slugify(ctx["deal_name"])
    return OUTPUT_DIR / f"{company_slug}_{deal_slug}.json"


# ── Test runner ───────────────────────────────────────────────────────────────

def run_tests(pipeline: list[dict], opp_lookup: dict, prod_map: dict) -> bool:
    print("\n" + "=" * 50)
    print("TEST RESULTS — 3 DEALS")
    print("=" * 50)

    # Test 1 — largest CUSTOMER stage deal
    customer_deals = sorted(
        [d for d in pipeline if d.get("stage", "").upper() == "CUSTOMER"],
        key=lambda x: x.get("amount", 0), reverse=True
    )
    t1 = customer_deals[0] if customer_deals else sorted(pipeline, key=lambda x: x.get("amount", 0), reverse=True)[0]

    # Test 2 — smallest MEETING stage deal
    meeting_deals = sorted(
        [d for d in pipeline if d.get("stage", "").upper() == "MEETING"],
        key=lambda x: x.get("amount", 0)
    )
    t2 = meeting_deals[0] if meeting_deals else pipeline[-1]

    # Test 3 — AT RISK deal (weakest qualification, proxy for low-confidence)
    at_risk = [d for d in pipeline if d.get("overall_score", "") == "AT RISK"]
    # prefer ones with financial services signals in company name
    fs_at_risk = [d for d in at_risk if any(w in d["company"].lower() for w in
                  ["financial", "bank", "capital", "credit", "invest", "wealth", "fund"])]
    t3 = fs_at_risk[0] if fs_at_risk else (at_risk[0] if at_risk else pipeline[5])

    tests = [
        ("Test 1 — Largest CUSTOMER stage deal", t1, "Enterprise Scale", "HIGH"),
        ("Test 2 — Smallest MEETING stage deal",  t2, "Quick Start",      "MEDIUM"),
        ("Test 3 — AT RISK / financial services", t3, None,               None),
    ]

    all_passed = True
    for label, deal, expected_pattern, expected_conf in tests:
        print(f"\n{label}")
        ctx = build_context(deal, opp_lookup, prod_map)
        print(f"  Deal:        {ctx['deal_name']}")
        print(f"  Company:     {ctx['company']}")
        print(f"  Amount:      ${ctx['amount']:,.0f}")
        print(f"  Pattern:     {ctx['implementation_pattern']}")
        print(f"  KB slug:     {ctx['kb_slug']}")

        estimate = estimate_deal(ctx, retry=True)
        if estimate is None:
            print("  RESULT:      FAILED — no response from Together AI")
            all_passed = False
            continue

        ok, errs, estimate = validate_and_fix(estimate, ctx)
        if not ok:
            print(f"  RESULT:      FAILED — {'; '.join(errs)}")
            all_passed = False
            continue

        mc = estimate.get("monthly_consumption", {})
        wp = estimate.get("workload_profile", {})
        print(f"  Confidence:  {estimate.get('confidence')}")
        print(f"  Monthly est: ${mc.get('total_monthly_usd', 0):,.0f}")
        print(f"  Annual est:  ${mc.get('total_annual_usd', 0):,.0f}")
        print(f"  Workload:    {wp.get('workload_type')}")
        print(f"  Compliance:  {', '.join(wp.get('compliance_flags', [])) or 'none'}")
        print(f"  Narrative:   {estimate.get('architect_narrative', '')[:120]}...")
        print(f"  RESULT:      PASSED ✅")

        time.sleep(1)

    print(f"\nAll tests passed — proceeding to full run? {'YES' if all_passed else 'NO — fix failures first'}")
    print("=" * 50)
    return all_passed


# ── Summary generation ────────────────────────────────────────────────────────

def generate_summary() -> None:
    if not ROLLUP_FILE.exists():
        return

    with open(ROLLUP_FILE, encoding="utf-8") as f:
        all_estimates = json.load(f)

    if not all_estimates:
        return

    from collections import defaultdict, Counter

    conf_dist = Counter(e.get("confidence", "MEDIUM") for e in all_estimates)
    total_monthly = sum(e.get("monthly_consumption", {}).get("total_monthly_usd", 0) for e in all_estimates)
    total_annual  = sum(e.get("monthly_consumption", {}).get("total_annual_usd", 0)  for e in all_estimates)

    by_pattern: dict[str, list] = defaultdict(list)
    for e in all_estimates:
        by_pattern[e.get("implementation_pattern", "Unknown")].append(
            e.get("monthly_consumption", {}).get("total_monthly_usd", 0)
        )

    by_product: dict[str, list] = defaultdict(list)
    for e in all_estimates:
        by_product[e.get("product", "Unknown")].append(
            e.get("monthly_consumption", {}).get("total_annual_usd", 0)
        )

    by_workload: dict[str, list] = defaultdict(list)
    for e in all_estimates:
        wt = e.get("workload_profile", {}).get("workload_type", "Mixed")
        by_workload[wt].append(e.get("monthly_consumption", {}).get("total_monthly_usd", 0))

    compliance_flags = Counter()
    for e in all_estimates:
        for flag in e.get("workload_profile", {}).get("compliance_flags", []):
            compliance_flags[flag] += 1

    top10 = sorted(
        all_estimates,
        key=lambda x: x.get("monthly_consumption", {}).get("total_annual_usd", 0),
        reverse=True
    )[:10]

    summary = {
        "generated_at": now_iso(),
        "total_deals_estimated": len(all_estimates),
        "confidence_distribution": {
            "high":   conf_dist.get("HIGH", 0),
            "medium": conf_dist.get("MEDIUM", 0),
            "low":    conf_dist.get("LOW", 0),
        },
        "total_pipeline_consumption": {
            "monthly_usd": round(total_monthly),
            "annual_usd":  round(total_annual),
        },
        "by_implementation_pattern": {
            k: {
                "count": len(v),
                "avg_monthly_usd": round(sum(v) / len(v)) if v else 0,
                "total_annual_usd": round(sum(v) * 12),
            }
            for k, v in by_pattern.items()
        },
        "by_product": [
            {
                "product": prod,
                "deal_count": len(amounts),
                "avg_monthly_consumption_usd": round(sum(amounts) / 12 / len(amounts)) if amounts else 0,
                "total_annual_consumption_usd": round(sum(amounts)),
            }
            for prod, amounts in sorted(by_product.items(), key=lambda x: sum(x[1]), reverse=True)
        ],
        "by_workload_type": {
            wt: {
                "count": len(vals),
                "avg_monthly_usd": round(sum(vals) / len(vals)) if vals else 0,
            }
            for wt, vals in sorted(by_workload.items())
        },
        "top_10_by_annual_consumption": [
            {
                "deal_name": e.get("deal_name"),
                "company":   e.get("company"),
                "product":   e.get("product"),
                "annual_consumption_usd": round(e.get("monthly_consumption", {}).get("total_annual_usd", 0)),
            }
            for e in top10
        ],
        "compliance_flag_frequency": dict(compliance_flags),
    }

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    # QuickSight CSV
    import csv
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "deal_id", "deal_name", "company", "product", "base_category",
            "stage", "amount", "assigned_rep", "overall_score",
            "implementation_pattern", "confidence",
            "workload_type", "availability_requirement",
            "compute_instances_count", "compute_monthly_usd",
            "storage_tb", "storage_monthly_usd",
            "database_tier", "database_monthly_usd",
            "network_monthly_usd", "additional_services_monthly_usd",
            "total_monthly_usd", "total_annual_usd",
            "sa_days", "proserv_days", "implementation_cost_usd",
            "compliance_soc2", "compliance_pci", "compliance_hipaa",
        ])
        for e in all_estimates:
            wp = e.get("workload_profile", {})
            comp = e.get("compute", {})
            stor = e.get("storage", {})
            db   = e.get("database", {})
            mc   = e.get("monthly_consumption", {})
            ir   = e.get("implementation_resources", {})
            flags = wp.get("compliance_flags", [])
            primary = comp.get("primary_instances", {})
            secondary = comp.get("secondary_instances", {})
            instance_count = primary.get("count", 0) + secondary.get("count", 0)

            writer.writerow([
                e.get("deal_id", ""), e.get("deal_name", ""), e.get("company", ""),
                e.get("product", ""), e.get("base_category", ""),
                "", e.get("amount", 0), e.get("assigned_rep", ""), e.get("overall_score", ""),
                e.get("implementation_pattern", ""), e.get("confidence", ""),
                wp.get("workload_type", ""), wp.get("availability_requirement", ""),
                instance_count, mc.get("compute_usd", 0),
                stor.get("primary_storage_tb", 0), mc.get("storage_usd", 0),
                db.get("tier", ""), mc.get("database_usd", 0),
                mc.get("network_usd", 0), mc.get("additional_services_usd", 0),
                mc.get("total_monthly_usd", 0), mc.get("total_annual_usd", 0),
                ir.get("sa_days_required", 0), ir.get("proserv_days_required", 0),
                ir.get("one_time_implementation_cost_usd", 0),
                1 if "SOC2" in flags else 0,
                1 if "PCI-DSS" in flags else 0,
                1 if "HIPAA" in flags else 0,
            ])

    print(f"  Summary:   {SUMMARY_FILE}")
    print(f"  CSV:       {CSV_FILE}")


# ── Final report ──────────────────────────────────────────────────────────────

def print_final_report(progress: dict) -> None:
    print("\n" + "=" * 50)
    print("RESOURCE ESTIMATION COMPLETE")
    print("=" * 50)
    total = progress.get("total_deals", 0)
    est   = progress.get("estimated", 0)
    skip  = progress.get("skipped_existing", 0)
    fail  = progress.get("failed", 0)
    hi    = progress.get("high_confidence", 0)
    med   = progress.get("medium_confidence", 0)
    lo    = progress.get("low_confidence", 0)
    print(f"\nDeals processed:      {total}")
    print(f"Estimated:            {est}")
    print(f"Skipped (existing):   {skip}")
    print(f"Failed:               {fail}")
    print(f"\nConfidence distribution:")
    print(f"  HIGH:    {hi} deals ({hi/max(est,1)*100:.0f}%)")
    print(f"  MEDIUM:  {med} deals ({med/max(est,1)*100:.0f}%)")
    print(f"  LOW:     {lo} deals ({lo/max(est,1)*100:.0f}%)")
    print(f"\nPipeline consumption estimates:")
    print(f"  Total monthly:    ${progress.get('total_monthly_consumption_usd', 0):,.0f}")
    print(f"  Total annual:     ${progress.get('total_annual_consumption_usd', 0):,.0f}")
    print(f"\nFiles saved:")
    print(f"  {OUTPUT_DIR}/")
    count = len(list(OUTPUT_DIR.glob("*.json")))
    print(f"    {count} individual estimate files + rollup/summary/progress")
    print("=" * 50 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Deal Desk Resource Estimation Generator")
    parser.add_argument("--test",  action="store_true", help="Run 3 test deals only")
    parser.add_argument("--reset", action="store_true", help="Ignore existing estimates and re-run all")
    args = parser.parse_args()

    if not TOGETHER_API_KEY:
        print("ERROR: TOGETHER_API_KEY environment variable not set")
        sys.exit(1)

    # Ensure output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load all data
    print("\n" + "=" * 50)
    print("RESOURCE ESTIMATION — LOADING DATA")
    print("=" * 50)

    pipeline    = load_pipeline()
    opp_lookup  = load_opportunities()
    prod_map    = load_product_mapping()
    cap_files   = list_capability_files()
    prof_files  = list(PROFILES_DIR.glob("*.md"))

    print(f"Deals loaded:         {len(pipeline)}")
    print(f"Company profiles:     {len(prof_files)}")
    print(f"KB capabilities:      {len(cap_files)}")
    print(f"Product mappings:     {len(prod_map)}")
    print(f"\nProcessing order: largest deals first")
    print(f"Output: {OUTPUT_DIR}")
    print("=" * 50)

    # Sort by amount descending
    pipeline_sorted = sorted(pipeline, key=lambda d: float(d.get("amount") or 0), reverse=True)

    if args.test:
        run_tests(pipeline_sorted, opp_lookup, prod_map)
        return

    # Load or init progress
    progress = load_progress()
    progress["total_deals"] = len(pipeline_sorted)

    # Run tests first
    print("\nRunning 3-deal validation before full run...")
    passed = run_tests(pipeline_sorted, opp_lookup, prod_map)
    if not passed:
        print("Tests failed. Fix issues and retry.")
        sys.exit(1)

    print(f"\nStarting full run — {len(pipeline_sorted)} deals...")
    started = datetime.now(timezone.utc)

    for i, deal in enumerate(pipeline_sorted):
        ctx = build_context(deal, opp_lookup, prod_map)
        out_path = output_path_for(ctx)

        # Skip existing unless --reset
        if not args.reset and out_path.exists():
            progress["skipped_existing"] = progress.get("skipped_existing", 0) + 1
            progress["last_deal_processed"] = deal["deal_name"]
            save_progress(progress)
            print(f"  [{i+1}/{len(pipeline_sorted)}] SKIP  {deal['deal_name'][:50]}")
            continue

        print(f"  [{i+1}/{len(pipeline_sorted)}] {deal['deal_name'][:55]:<55} ${ctx['amount']:>10,.0f}", end="", flush=True)

        estimate = estimate_deal(ctx, retry=True)

        if estimate is None:
            print(" → FAILED (no response)")
            log_failure(deal["deal_name"], "No response from Together AI after retry")
            progress["failed"] = progress.get("failed", 0) + 1
            progress["last_deal_processed"] = deal["deal_name"]
            save_progress(progress)
            time.sleep(1)
            continue

        ok, errs, estimate = validate_and_fix(estimate, ctx)

        if not ok:
            reason = "; ".join(errs)
            print(f" → FAILED ({reason[:60]})")
            log_failure(deal["deal_name"], reason)
            progress["failed"] = progress.get("failed", 0) + 1
            progress["last_deal_processed"] = deal["deal_name"]
            save_progress(progress)
            time.sleep(1)
            continue

        # Save individual file
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(estimate, f, indent=2)

        # Append to rollup
        append_to_rollup(estimate)

        # Update progress
        conf = estimate.get("confidence", "MEDIUM")
        monthly = estimate.get("monthly_consumption", {}).get("total_monthly_usd", 0)
        annual  = estimate.get("monthly_consumption", {}).get("total_annual_usd", 0)

        progress["estimated"] = progress.get("estimated", 0) + 1
        if conf == "HIGH":
            progress["high_confidence"] = progress.get("high_confidence", 0) + 1
        elif conf == "MEDIUM":
            progress["medium_confidence"] = progress.get("medium_confidence", 0) + 1
        else:
            progress["low_confidence"] = progress.get("low_confidence", 0) + 1
        progress["total_monthly_consumption_usd"] = progress.get("total_monthly_consumption_usd", 0) + monthly
        progress["total_annual_consumption_usd"]  = progress.get("total_annual_consumption_usd", 0) + annual
        progress["last_deal_processed"] = deal["deal_name"]
        save_progress(progress)

        print(f" → {conf:6} ${monthly:>8,.0f}/mo")

        # Rate limit courtesy delay
        time.sleep(1)

        # Handle rate limit signal
        if (i + 1) % 50 == 0:
            elapsed = (datetime.now(timezone.utc) - started).seconds
            print(f"\n  -- {i+1} deals processed in {elapsed}s --\n")

    # Generate summary
    print("\nGenerating summary analytics...")
    generate_summary()

    print_final_report(progress)


if __name__ == "__main__":
    main()
