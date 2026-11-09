"""
financial_inputs.py
Gathers all inputs for the financial model and scores confidence.

Sources (in priority order):
  1. knowledge_base/notes/{company}/{deal}/_meddpicc.json  — scored MEDDPICC data
  2. knowledge_base/kb/cost_sheets/{product}.md            — KB rate + resource tables
  3. LLM extraction from note files                        — only when confidence < 50
"""

from __future__ import annotations

try:
    from tools.llm_client import TOGETHER_CHAT_URL, TOGETHER_MODEL, request_completion
except ModuleNotFoundError:
    from llm_client import TOGETHER_CHAT_URL, TOGETHER_MODEL, request_completion

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent.parent
NOTES_DIR  = BASE_DIR / "knowledge_base" / "notes"
KB_DIR     = BASE_DIR / "knowledge_base" / "kb" / "cost_sheets"

# ── Together AI (optional — only called for low-confidence extraction) ──

# ── Product → cost sheet slug map ─────────────────────────────────────────
# Maps the product prefix found in deal_name to the cost sheet file stem.
PRODUCT_SLUG_MAP: dict[str, str] = {
    "CloudCore":         "cloud_infrastructure_management",
    "RiskEngine":        "ml_risk_scoring_platform",
    "SecureVault":       "data_security_encryption",
    "SecurityO":         "data_security_encryption",
    "InsightIQ":         "business_intelligence_analytics",
    "AuditTrail":        "observability_audit_logging",
    "FraudD":            "observability_audit_logging",
    "RegTrack":          "regulatory_workflow_automation",
    "TreasuryM":         "regulatory_workflow_automation",
    "RegTechP":          "ai_compliance_automation",
    "ComplianceAI":      "ai_compliance_automation",
    "ComplianceS":       "ai_compliance_automation",
    "StreamlineAML":     "ai_powered_process_automation",
    "AI/MLI":            "ai_powered_process_automation",
    "CapitalConnect":    "api_integration_platform",
    "LendingOS":         "decisioning_origination_platform",
    "CoreB":             "decisioning_origination_platform",
    "CoreP":             "decisioning_origination_platform",
    "Nexus Platform":    "enterprise_integration_platform",
    "ReportingCloud":    "cloud_native_reporting",
    "CloudM":            "cloud_native_reporting",
    "DataFabric":        "data_mesh_fabric",
    "DataF":             "data_mesh_fabric",
    "PaymentsI":         "data_pipeline_integration",
    "DataBridge":        "data_pipeline_integration",
    "DataVault":         "data_warehousing_storage",
    "SmartLedger":       "immutable_record_keeping",
    "TreasuryLink":      "realtime_data_forecasting",
    "FraudGuard":        "realtime_fraud_detection",
    "AnalyticsHub":      "unified_analytics_platform",
    "WealthM":           "business_intelligence_analytics",
    "WealthPortal":      "client_portal_digital_experience",
    "RiskA":             "ml_risk_scoring_platform",
}

# ── Confidence weights ──────────────────────────────────────────────────
CONF_WEIGHTS = {
    "pain_usd_annual":        30,
    "company_employees":      15,
    "deal_amount":            15,
    "implementation_pattern": 20,
    "time_savings_signal":    10,
    "budget_signal":          10,
}
CONF_MULTIPLIERS = {"stated": 1.0, "inferred": 0.6, "assumed": 0.3, "unknown": 0.0}

# ── Default KB rates ───────────────────────────────────────────────────
DEFAULT_SA_RATE        = 2_500
DEFAULT_PROSERV_RATE   = 3_000
DEFAULT_PARTNER_RATE   = 1_800
DEFAULT_LOADED_HOURLY  = 85   # loaded hourly rate for customer internal resources


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _extract_product_from_deal_name(deal_name: str) -> str:
    """Return the product prefix from 'ProductName — Deal Type'."""
    if " — " in deal_name:
        return deal_name.split(" — ")[0].strip()
    if " - " in deal_name:
        return deal_name.split(" - ")[0].strip()
    return deal_name.strip()


def _find_cost_sheet_path(product: str) -> Optional[Path]:
    slug = PRODUCT_SLUG_MAP.get(product)
    if slug:
        p = KB_DIR / f"{slug}.md"
        if p.exists():
            return p
    # fallback: fuzzy search by product fragment
    product_lower = product.lower().replace(" ", "_")
    for f in KB_DIR.glob("*.md"):
        if product_lower in f.stem or f.stem in product_lower:
            return f
    return None


def _parse_dollar_amount(text: str) -> Optional[float]:
    """Extract the first dollar amount from text, returning float or None."""
    # Matches $750,000 or $750k or $1.5M or $1.5 million
    m = re.search(
        r"\$\s*([\d,]+(?:\.\d+)?)\s*([kmb]illion|[kmb]|million|thousand)?",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    raw = float(m.group(1).replace(",", ""))
    suffix = (m.group(2) or "").lower()
    if suffix in ("k", "thousand"):
        raw *= 1_000
    elif suffix in ("m", "million"):
        raw *= 1_000_000
    elif suffix in ("b", "billion"):
        raw *= 1_000_000_000
    return raw


def _parse_percentage(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) / 100 if m else None


def _parse_hours(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*hours?\s*(?:per\s*(?:month|week|day))?", text, re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    context = text[m.start() - 20 : m.end() + 10].lower()
    if "month" in context:
        val = val / 4.33  # convert monthly to weekly
    elif "day" in context:
        val = val * 5
    return val


def _parse_headcount(text: str) -> Optional[float]:
    patterns = [
        r"(\d+)\s*(?:FTEs?|full.time|employees|headcount|people|staff)",
        r"free\s+up\s+(\d+)\s*(?:FTEs?|people|staff|employees)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def _parse_kb_rates(cost_sheet_text: str) -> dict:
    """Extract SA/ProServ/Partner day rates and resource days from KB cost sheet."""
    rates = {
        "sa_day_rate":      DEFAULT_SA_RATE,
        "proserv_day_rate": DEFAULT_PROSERV_RATE,
        "partner_day_rate": DEFAULT_PARTNER_RATE,
    }
    # Look for explicit rate mentions
    for pat, key in [
        (r"SA\s*(?:day\s*)?rate[:\s]+\$?([\d,]+)", "sa_day_rate"),
        (r"ProServ\s*(?:day\s*)?rate[:\s]+\$?([\d,]+)", "proserv_day_rate"),
        (r"Partner\s*(?:day\s*)?rate[:\s]+\$?([\d,]+)", "partner_day_rate"),
        (r"professional\s*services.*?\$\s*([\d,]+)\s*per\s*day", "proserv_day_rate"),
        (r"solutions?\s*architect.*?\$\s*([\d,]+)\s*per\s*day", "sa_day_rate"),
    ]:
        m = re.search(pat, cost_sheet_text, re.IGNORECASE)
        if m:
            rates[key] = float(m.group(1).replace(",", ""))
    return rates


def _resource_days_for_pattern(impl_pattern: str) -> dict:
    """Return default SA/ProServ/Partner/timeline days per implementation pattern."""
    patterns = {
        "Quick Start":        {"sa_days": 5,  "proserv_days": 10, "partner_days": 5,  "timeline_weeks": 6},
        "Production Grade":   {"sa_days": 15, "proserv_days": 30, "partner_days": 15, "timeline_weeks": 12},
        "Enterprise Scale":   {"sa_days": 30, "proserv_days": 60, "partner_days": 30, "timeline_weeks": 20},
    }
    return patterns.get(impl_pattern, patterns["Production Grade"])


def _assign_pattern(deal_amount: float, notes_text: str = "") -> str:
    """Assign implementation pattern based on deal amount and complexity signals."""
    if deal_amount < 100_000:
        pattern = "Quick Start"
    elif deal_amount <= 500_000:
        pattern = "Production Grade"
    else:
        pattern = "Enterprise Scale"

    # Complexity overrides
    notes_lower = notes_text.lower()
    legacy_signals = ["legacy system", "migration", "complex integration", "mainframe"]
    simple_signals = ["simple integration", "quick start", "proof of concept", "poc"]
    tier_order = ["Quick Start", "Production Grade", "Enterprise Scale"]

    if any(s in notes_lower for s in legacy_signals):
        idx = tier_order.index(pattern)
        pattern = tier_order[min(idx + 1, 2)]
    elif any(s in notes_lower for s in simple_signals):
        idx = tier_order.index(pattern)
        pattern = tier_order[max(idx - 1, 0)]

    return pattern


def _read_note_section(note_path: Path, section_keywords: list[str]) -> str:
    """Read a markdown note file and return text near section headers."""
    if not note_path.exists():
        return ""
    text = note_path.read_text(encoding="utf-8")
    result_parts = []
    lines = text.split("\n")
    capture = False
    for line in lines:
        if any(kw.lower() in line.lower() for kw in section_keywords) and line.startswith("#"):
            capture = True
            result_parts.append(line)
        elif capture:
            if line.startswith("#") and not any(kw.lower() in line.lower() for kw in section_keywords):
                capture = False
            else:
                result_parts.append(line)
    return "\n".join(result_parts)


def _llm_extract_financials(note_sections: str, api_key: str) -> dict:
    """
    Call Together AI to extract financial signals from note text.
    Returns a dict with extracted fields and their confidence.
    """

    prompt = f"""Read these note sections from a B2B sales deal.
Extract ONLY numbers and financial signals that appear explicitly in the text.

For each field found return:
  value: the number (numeric only, no currency symbols)
  confidence: stated / inferred
  source_note: which note file
  quote: exact text that contains the signal

Fields to find:
  annual_cost_of_problem_usd
  hours_lost_per_week
  headcount_affected
  error_rate_pct
  transactions_per_month
  current_vendor_cost_usd
  budget_approved_usd
  growth_rate_signal_pct

If a field is not mentioned return null for value and unknown for confidence.

Return only valid JSON. No preamble. No explanation.

NOTE SECTIONS:
{note_sections[:3000]}
"""
    payload = {
        "model": TOGETHER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 800,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    try:
        response = request_completion(TOGETHER_CHAT_URL, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        print(f"[financial_inputs] LLM extraction failed: {e}", file=sys.stderr)
        return {}


def _score_inputs(raw: dict) -> tuple[int, str]:
    """
    Compute confidence score from raw input dict.
    Each key must have a 'confidence_level' of: stated / inferred / assumed / unknown
    Returns (score_0_to_100, level_string).
    """
    score = 0.0
    for field, weight in CONF_WEIGHTS.items():
        level = raw.get(field, {}).get("confidence_level", "unknown")
        score += weight * CONF_MULTIPLIERS.get(level, 0.0)

    score = round(score)
    if score >= 80:
        level = "HIGH"
    elif score >= 50:
        level = "MEDIUM"
    else:
        level = "LOW"
    return score, level


def gather_inputs(deal: dict, api_key: str = "") -> dict:
    """
    Main entry point. Returns a structured inputs dict:
    {
      "deal_name": ...,
      "company": ...,
      "product": ...,
      "deal_amount": ...,
      "assigned_rep": ...,
      "overall_score": ...,
      "impl_pattern": ...,
      "inputs": {
          "pain_usd_annual":        {"value": float|None, "confidence_level": str, "source": str, "quote": str},
          "company_employees":      {...},
          "deal_amount":            {...},
          "implementation_pattern": {...},
          "time_savings_signal":    {...},
          "budget_signal":          {...},
          # raw operational inputs
          "hours_lost_per_week":    {"value": float|None, ...},
          "headcount_affected":     {"value": float|None, ...},
          "error_rate_pct":         {"value": float|None, ...},
          "transactions_per_month": {"value": float|None, ...},
          "current_vendor_cost_usd":{"value": float|None, ...},
          "growth_rate_pct":        {"value": float|None, ...},
      },
      "rates": {"sa_day_rate": ..., "proserv_day_rate": ..., "partner_day_rate": ...},
      "resources": {"sa_days": ..., "proserv_days": ..., "partner_days": ..., "timeline_weeks": ...},
      "confidence_score": int,
      "confidence_level": str,
      "extraction_triggered": bool,
    }
    """
    deal_name    = deal.get("deal_name", "")
    company      = deal.get("company", "")
    amount       = float(deal.get("amount") or 0)
    assigned_rep = deal.get("assigned_rep", "")
    overall_score = deal.get("overall_score", "")
    elements     = deal.get("elements", {})
    flags        = deal.get("flags", {})

    product = _extract_product_from_deal_name(deal_name)

    # ── Locate note files ────────────────────────────────────────────────
    company_slug = slugify(company)
    deal_slug    = slugify(deal_name)
    notes_deal_dir = NOTES_DIR / company_slug / deal_slug

    # Aggregate raw note text for pattern detection
    raw_notes_text = ""
    for note_file in ["01_discovery.md", "02_technical_review.md", "03_stakeholder_meeting.md"]:
        p = notes_deal_dir / note_file
        if p.exists():
            raw_notes_text += p.read_text(encoding="utf-8")

    # ── Locate KB cost sheet ─────────────────────────────────────────────
    cost_sheet_path = _find_cost_sheet_path(product)
    cost_sheet_text = cost_sheet_path.read_text(encoding="utf-8") if cost_sheet_path else ""
    rates = _parse_kb_rates(cost_sheet_text)

    # ── Implementation pattern ───────────────────────────────────────────
    impl_pattern = _assign_pattern(amount, raw_notes_text)
    resources    = _resource_days_for_pattern(impl_pattern)

    # ── Extract inputs from MEDDPICC elements ────────────────────────────
    def _el(name: str) -> dict:
        return elements.get(name, {})

    # pain_usd_annual
    pain_evidence = _el("metrics").get("evidence", "") + " " + _el("identify_pain").get("evidence", "")
    pain_val      = _parse_dollar_amount(pain_evidence)
    if pain_val and pain_val > 0:
        pain_conf = "stated" if _el("metrics").get("status") == "confirmed" else "inferred"
    elif amount > 0:
        pain_val  = amount * 2.5   # benchmark: annual pain ~2.5× deal size
        pain_conf = "assumed"
    else:
        pain_val  = None
        pain_conf = "unknown"

    # time_savings_signal
    ts_evidence = _el("metrics").get("evidence", "") + " " + pain_evidence
    hours_val   = _parse_hours(ts_evidence)
    if hours_val:
        ts_conf = "stated" if _el("metrics").get("status") == "confirmed" else "inferred"
    else:
        hours_val = None
        ts_conf   = "unknown"

    # headcount_affected
    hc_val  = _parse_headcount(ts_evidence + " " + raw_notes_text[:2000])
    hc_conf = "stated" if hc_val else "assumed"
    if not hc_val:
        hc_val = max(3, int(amount / 50_000))  # benchmark

    # error rate
    err_pct = _parse_percentage(ts_evidence)
    err_conf = "stated" if err_pct else "assumed"
    if not err_pct:
        err_pct = 0.05   # benchmark 5%

    # transactions per month
    txn_m = re.search(r"(\d[\d,]*)\s*transactions?\s*(?:per\s*month)?", ts_evidence, re.IGNORECASE)
    txn_val  = float(txn_m.group(1).replace(",", "")) if txn_m else None
    txn_conf = "stated" if txn_val else "assumed"
    if not txn_val:
        txn_val = 5_000  # benchmark

    # current_vendor_cost_usd
    vendor_evidence = _el("economic_buyer").get("evidence", "") + " " + raw_notes_text[:3000]
    vendor_val      = None
    for pat in [r"current\s+vendor.*?\$([\d,]+)", r"replacing.*?\$([\d,]+)", r"existing.*?cost.*?\$([\d,]+)"]:
        m = re.search(pat, vendor_evidence, re.IGNORECASE)
        if m:
            vendor_val = float(m.group(1).replace(",", ""))
            break
    vendor_conf = "stated" if vendor_val else "unknown"

    # budget_signal
    budget_evidence = _el("economic_buyer").get("evidence", "")
    budget_val      = _parse_dollar_amount(budget_evidence)
    if budget_val:
        budget_conf = "stated"
    elif _el("economic_buyer").get("status") == "confirmed":
        budget_val  = amount * 1.2
        budget_conf = "inferred"
    elif _el("economic_buyer").get("status") == "partial":
        budget_val  = amount
        budget_conf = "assumed"
    else:
        budget_val  = None
        budget_conf = "unknown"

    # growth rate
    growth_evidence = raw_notes_text[:3000]
    growth_pct      = _parse_percentage(growth_evidence)
    # Only use if it looks like a company growth rate (5–50%)
    if growth_pct and 0.05 <= growth_pct <= 0.5:
        growth_conf = "inferred"
    else:
        growth_pct  = 0.15  # benchmark: 15% growth
        growth_conf = "assumed"

    # company_employees — from CRM notes if mentioned
    emp_val  = None
    emp_m    = re.search(r"(\d[\d,]+)\s*employees", raw_notes_text, re.IGNORECASE)
    if emp_m:
        emp_val  = float(emp_m.group(1).replace(",", ""))
        emp_conf = "stated"
    else:
        emp_val  = max(200, int(amount / 500))  # rough benchmark
        emp_conf = "assumed"

    # deal_amount — always available
    deal_amount_conf = "stated" if amount > 0 else "unknown"

    # impl_pattern confidence
    impl_conf = "inferred" if amount > 0 else "assumed"

    # ── Build raw inputs dict ────────────────────────────────────────────
    raw = {
        "pain_usd_annual": {
            "value": pain_val, "confidence_level": pain_conf,
            "source": "meddpicc.metrics.evidence", "quote": pain_evidence[:200],
        },
        "company_employees": {
            "value": emp_val, "confidence_level": emp_conf,
            "source": "note text" if emp_conf == "stated" else "benchmark",
            "quote": f"{int(emp_val)} employees (estimated)" if emp_val else "",
        },
        "deal_amount": {
            "value": amount, "confidence_level": deal_amount_conf,
            "source": "crm.amount", "quote": f"${amount:,.0f}",
        },
        "implementation_pattern": {
            "value": impl_pattern, "confidence_level": impl_conf,
            "source": "deal_amount + complexity signals", "quote": impl_pattern,
        },
        "time_savings_signal": {
            "value": hours_val, "confidence_level": ts_conf,
            "source": "meddpicc.metrics.evidence", "quote": ts_evidence[:150],
        },
        "budget_signal": {
            "value": budget_val, "confidence_level": budget_conf,
            "source": "meddpicc.economic_buyer.evidence", "quote": budget_evidence[:150],
        },
        # Extended operational inputs
        "hours_lost_per_week": {
            "value": hours_val or 20.0, "confidence_level": ts_conf,
            "source": "meddpicc.metrics", "quote": "",
        },
        "headcount_affected": {
            "value": hc_val, "confidence_level": hc_conf,
            "source": "note text" if hc_conf == "stated" else "benchmark",
            "quote": "",
        },
        "error_rate_pct": {
            "value": err_pct, "confidence_level": err_conf,
            "source": "meddpicc.metrics" if err_conf == "stated" else "benchmark",
            "quote": "",
        },
        "transactions_per_month": {
            "value": txn_val, "confidence_level": txn_conf,
            "source": "note text" if txn_conf == "stated" else "benchmark",
            "quote": "",
        },
        "current_vendor_cost_usd": {
            "value": vendor_val, "confidence_level": vendor_conf,
            "source": "note text" if vendor_conf == "stated" else "unknown",
            "quote": "",
        },
        "growth_rate_pct": {
            "value": growth_pct, "confidence_level": growth_conf,
            "source": "note text" if growth_conf == "inferred" else "benchmark",
            "quote": "",
        },
    }

    conf_score, conf_level = _score_inputs(raw)
    extraction_triggered = False

    # ── LLM extraction when confidence is LOW ───────────────────────────
    if conf_score < 50 and api_key:
        extraction_triggered = True
        print(f"[financial_inputs] Confidence LOW ({conf_score}/100) — extracting from notes via LLM...")

        sections = []
        for note_file, kws in [
            ("01_discovery.md",       ["business pain", "real business", "impact", "cost"]),
            ("02_technical_review.md", ["technical", "operational", "integration"]),
            ("03_stakeholder_meeting.md", ["stakeholder", "budget", "economic"]),
        ]:
            text = _read_note_section(notes_deal_dir / note_file, kws)
            if text:
                sections.append(f"# {note_file}\n{text}")

        if sections:
            extracted = _llm_extract_financials("\n\n".join(sections), api_key)
            # Merge extracted values back into raw if they improve confidence
            field_map = {
                "annual_cost_of_problem_usd": ("pain_usd_annual", "hours_lost_per_week"),
                "hours_lost_per_week":        ("hours_lost_per_week", "time_savings_signal"),
                "headcount_affected":         ("headcount_affected",),
                "error_rate_pct":             ("error_rate_pct",),
                "transactions_per_month":     ("transactions_per_month",),
                "current_vendor_cost_usd":    ("current_vendor_cost_usd",),
                "growth_rate_signal_pct":     ("growth_rate_pct",),
            }
            for extracted_key, raw_keys in field_map.items():
                info = extracted.get(extracted_key, {})
                if isinstance(info, dict) and info.get("value") is not None:
                    for rk in raw_keys:
                        if rk in raw and raw[rk]["confidence_level"] in ("assumed", "unknown"):
                            raw[rk]["value"] = float(info["value"])
                            raw[rk]["confidence_level"] = info.get("confidence", "inferred")
                            raw[rk]["quote"] = info.get("quote", "")[:150]

            # Re-score after extraction
            if "annual_cost_of_problem_usd" in extracted:
                info = extracted["annual_cost_of_problem_usd"]
                if isinstance(info, dict) and info.get("value"):
                    raw["pain_usd_annual"]["value"] = float(info["value"])
                    raw["pain_usd_annual"]["confidence_level"] = info.get("confidence", "inferred")

            conf_score, conf_level = _score_inputs(raw)
            print(f"[financial_inputs] After extraction: {conf_score}/100 → {conf_level}")

    # If still LOW, proceed with MEDIUM bands — never block
    if conf_score < 50:
        conf_level = "MEDIUM"

    return {
        "deal_name":    deal_name,
        "company":      company,
        "product":      product,
        "deal_amount":  amount,
        "assigned_rep": assigned_rep,
        "overall_score": overall_score,
        "impl_pattern": impl_pattern,
        "inputs":       raw,
        "rates":        rates,
        "resources":    resources,
        "confidence_score": conf_score,
        "confidence_level": conf_level,
        "extraction_triggered": extraction_triggered,
    }
