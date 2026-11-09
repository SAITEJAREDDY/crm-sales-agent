"""Phase 7 Deal Builder and Agreement Package generation.

Turns a validated working scenario into a structured deal package:
deal approval brief, commercial term sheet, consumption commitment schedule,
the existing Excel financial model, and a synthetic draft agreement.

Boundaries preserved:
- Financial numbers come only from tools.commercial_financial_analysis
  (which itself runs tools.financial_model.compute).
- Policy status and approvals come only from tools.commercial_policy_engine.
- Historical evidence comes only from tools.comparable_deal_intelligence.
- The optional LLM may polish prose; it never calculates or changes terms.
- Customer-facing documents are built from whitelisted fields and are
  additionally scanned for internal-data leakage before packaging.

All generated legal-style documents are synthetic demonstration artifacts,
clearly labeled as such. They are not Microsoft agreements.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

try:
    from tools.commercial_data import get_deal_input
    from tools.commercial_financial_analysis import (
        _scenario_from_deal_input,
        _to_existing_financial_model_input,
        evaluate_financial_scenario,
    )
    from tools.commercial_policy_engine import evaluate_commercial_policy
    from tools.commercial_strategy_agent import _stable_hash
    from tools.comparable_deal_intelligence import get_comparable_deal_evidence
    from tools.financial_model import compute as compute_financial_model
    from tools.financial_inputs import _assign_pattern
    from tools.excel_builder import build_workbook
except ModuleNotFoundError:
    from commercial_data import get_deal_input
    from commercial_financial_analysis import (
        _scenario_from_deal_input,
        _to_existing_financial_model_input,
        evaluate_financial_scenario,
    )
    from commercial_policy_engine import evaluate_commercial_policy
    from commercial_strategy_agent import _stable_hash
    from comparable_deal_intelligence import get_comparable_deal_evidence
    from financial_model import compute as compute_financial_model
    from financial_inputs import _assign_pattern
    from excel_builder import build_workbook


BASE_DIR = Path(__file__).resolve().parent.parent
PACKAGES_DIR = BASE_DIR / "knowledge_base" / "commercial_strategy" / "deal_packages"
COMMON_PAPER_SAMPLE_PATH = BASE_DIR / "templates" / "cloud_service_agreement.docx"

PROVIDER_NAME = os.environ.get("COMMERCIAL_PROVIDER_NAME", "")
SYNTHETIC_BANNER = [
    "SYNTHETIC COMMERCIAL DOCUMENT",
    "DEMONSTRATION ONLY",
    "NOT AN OFFICIAL PROVIDER AGREEMENT",
    "NOT FOR EXECUTION",
]

MICROSOFT_REFERENCE_LINKS = [
    {"label": "Microsoft Customer Agreement (public reference)",
     "url": "https://www.microsoft.com/licensing/docs/customeragreement"},
    {"label": "Microsoft Product Terms (public reference)",
     "url": "https://www.microsoft.com/licensing/terms"},
    {"label": "Microsoft Azure Consumption Commitment (public reference)",
     "url": "https://learn.microsoft.com/en-us/marketplace/azure-consumption-commitment-benefit"},
]

COMMON_PAPER_TEMPLATE_REFERENCE = {
    "label": "Common Paper Cloud Service Agreement + SLA v2.1 sample",
    "source": str(COMMON_PAPER_SAMPLE_PATH),
    "usage": "Structural reference only; generated terms remain synthetic and deal-specific.",
}

SUPPORTED_SCENARIO_FIELDS = {
    "discount_pct",
    "contract_term_months",
    "commitment_amount",
    "billing_frequency",
    "payment_terms",
    "price_protection",
}
REQUIRED_SCENARIO_FIELDS = ["discount_pct", "contract_term_months", "commitment_amount", "billing_frequency", "payment_terms"]

READINESS_STATUSES = {"READY_TO_PACKAGE", "REQUIRES_APPROVAL", "BLOCKED", "INCOMPLETE"}

# Field-level classification. Customer-facing documents may only be built
# from CUSTOMER_SHAREABLE material; INTERNAL_ONLY material may appear only in
# the internal deal brief and the validation snapshot.
INTERNAL_ONLY_FIELDS = {
    "npv", "roi_3yr_pct", "payback_months", "gross_margin",
    "approval_thresholds", "required_approvals", "approval_sequence",
    "policy_checks", "exceptions", "historical_benchmarks",
    "comparable_deals", "negotiation_strategy", "walk_away_guidance",
    "commercial_risks", "ai_confidence", "coverage_ratio",
    "expected_consumption",
}
CUSTOMER_SHAREABLE_FIELDS = {
    "customer", "provider", "products", "contract_term_months",
    "discount_pct", "list_value", "tcv", "commitment_amount",
    "commitment_period", "billing_frequency", "payment_terms",
    "price_protection", "concessions", "special_terms",
    "effective_date", "validity", "assumptions", "dependencies",
}

# Hard leakage rule for customer-facing documents (word-boundary matched).
FORBIDDEN_CUSTOMER_TOKENS = [
    "npv", "roi", "payback", "gross margin",
    "p25", "p75", "median", "percentile", "benchmark", "comparable",
    "hist-comm", "historical deal",
    "approval", "approvals", "approver", "policy threshold",
    "negotiation", "walk-away", "walk away", "confidence",
]

AGREEMENT_PROMPT = """You are drafting a synthetic enterprise cloud commercial agreement for a Deal Desk demonstration.
Use only the supplied validated commercial terms.
Do not calculate financial values.
Do not change commercial terms.
Do not invent legal obligations.
Do not reproduce Microsoft contract language.
Do not claim this is a Microsoft agreement.
Where detailed legal terms are unavailable, reference the applicable executed agreement or product terms rather than inventing clauses.
Never expose internal financial analysis, approval logic, comparable deals, negotiation strategy, or walk-away guidance.
Return one JSON object with optional string fields "purpose" and "general_terms" only."""


class DealPackageError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _money(value) -> str:
    if value is None:
        return "Not specified"
    return f"${float(value):,.0f}"


def _pct(value) -> str:
    if value is None:
        return "Not specified"
    v = float(value)
    return f"{v:.1f}%" if v % 1 else f"{v:.0f}%"


# ── scenario normalization ────────────────────────────────────────────────────

def normalize_package_scenario(deal_id: str, scenario: dict | None) -> tuple[dict, list[str]]:
    """Fill a package scenario from the DealInput, reject unsupported fields,
    and report missing required fields. Returns (scenario, missing_fields)."""
    deal_input = get_deal_input(deal_id)
    if not deal_input:
        raise DealPackageError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")
    scenario = dict(scenario or {})
    scenario.pop("name", None)
    unsupported = sorted(set(scenario) - SUPPORTED_SCENARIO_FIELDS)
    if unsupported:
        raise DealPackageError(
            "UNSUPPORTED_SCENARIO_FIELD",
            f"Unsupported scenario field(s): {', '.join(unsupported)}. "
            "Calculated outputs such as TCV are derived, not editable.",
        )
    commercial = deal_input.get("commercial", {})
    commitment = deal_input.get("consumption_commitment", {})
    defaults = {
        "discount_pct": commercial.get("proposed_discount_pct"),
        "contract_term_months": commercial.get("contract_term_months"),
        "commitment_amount": commitment.get("commitment_amount"),
        "billing_frequency": commercial.get("billing_frequency"),
        "payment_terms": commercial.get("payment_terms"),
    }
    for key, value in defaults.items():
        scenario.setdefault(key, value)
    missing = [f for f in REQUIRED_SCENARIO_FIELDS if scenario.get(f) in (None, "")]
    return scenario, missing


# ── readiness + validation snapshot ───────────────────────────────────────────

def _agreement_input_gaps(deal_input: dict) -> list[str]:
    gaps = []
    if not deal_input.get("account", {}).get("name"):
        gaps.append("customer_name")
    if not deal_input.get("products"):
        gaps.append("products")
    return gaps


def compute_deal_readiness(financial: dict, policy: dict, missing_fields: list[str], agreement_gaps: list[str]) -> dict:
    npv = (financial.get("baseline") or {}).get("npv")
    financial_pass = npv is not None and float(npv) >= 0
    policy_status = policy.get("overall_status", "UNKNOWN")
    approvals_required = bool(policy.get("required_approvals"))

    if missing_fields or agreement_gaps:
        status = "INCOMPLETE"
    elif policy_status == "BLOCKED" or not financial_pass:
        status = "BLOCKED"
    elif approvals_required or policy_status in {"REQUIRES_APPROVAL", "REQUIRES_REVIEW"}:
        status = "REQUIRES_APPROVAL"
    else:
        status = "READY_TO_PACKAGE"

    return {
        "status": status,
        "checks": {
            "financial_validation": "PASS" if financial_pass else "FAIL",
            "commercial_policy": policy_status,
            "required_fields": "COMPLETE" if not missing_fields else "INCOMPLETE",
            "agreement_inputs": "COMPLETE" if not agreement_gaps else "INCOMPLETE",
        },
        "missing_fields": missing_fields,
        "agreement_input_gaps": agreement_gaps,
        "approval_status": "REQUIRED_NOT_OBTAINED" if approvals_required else "NOT_REQUIRED",
        "note": "Approvals are requirements from the policy engine; this system does not track approval completion.",
    }


def validate_deal_package(deal_id: str, scenario: dict | None = None) -> dict:
    """Fresh deterministic validation snapshot used by package generation."""
    deal_input = get_deal_input(deal_id)
    if not deal_input:
        raise DealPackageError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")
    normalized, missing = normalize_package_scenario(deal_id, scenario)
    payload = {"name": "deal_builder_final", **{k: v for k, v in normalized.items() if v is not None}}
    financial = evaluate_financial_scenario(deal_id, payload)
    policy = evaluate_commercial_policy(deal_id, payload)
    readiness = compute_deal_readiness(financial, policy, missing, _agreement_input_gaps(deal_input))
    return {
        "deal_id": deal_id,
        "validated_at": _utc_now(),
        "scenario": normalized,
        "scenario_hash": _stable_hash(normalized),
        "financial_analysis": financial,
        "policy_evaluation": policy,
        "deal_readiness": readiness,
        "gross_margin": "NOT_MODELED",
    }


def check_package_staleness(package: dict, scenario: dict | None) -> dict:
    """A package is stale when the current scenario no longer matches the one
    it was generated from."""
    deal_id = package.get("deal_id")
    normalized, _ = normalize_package_scenario(deal_id, scenario)
    current_hash = _stable_hash(normalized)
    package_hash = package.get("scenario_hash")
    return {
        "package_id": package.get("package_id"),
        "stale": current_hash != package_hash,
        "status": "PACKAGE_OUTDATED" if current_hash != package_hash else "PACKAGE_CURRENT",
        "package_scenario_hash": package_hash,
        "current_scenario_hash": current_hash,
    }


# ── leakage protection ────────────────────────────────────────────────────────

def assert_no_internal_leakage(document: dict) -> None:
    """Hard rule: customer-facing documents must not contain internal data."""
    serialized = json.dumps(document, default=str).lower()
    hits = []
    for token in FORBIDDEN_CUSTOMER_TOKENS:
        if re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", serialized):
            hits.append(token)
    if hits:
        raise DealPackageError(
            "INTERNAL_DATA_LEAKAGE",
            f"Customer-facing document '{document.get('document_type')}' contains internal token(s): {', '.join(sorted(hits))}",
        )


# ── document rendering ────────────────────────────────────────────────────────

def _render_text(doc: dict) -> str:
    lines = [doc["title"], ""]
    for banner in doc.get("banner", []):
        lines.append(banner)
    if doc.get("banner"):
        lines.append("")
    for section in doc["sections"]:
        lines.append(section["heading"].upper())
        for label, value in section.get("fields", []):
            lines.append(f"  {label}: {value}")
        if section.get("text"):
            lines.append(f"  {section['text']}")
        for item in section.get("items", []):
            lines.append(f"  - {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _finish_document(doc: dict) -> dict:
    doc["text"] = _render_text(doc)
    if doc.get("audience") == "CUSTOMER":
        assert_no_internal_leakage(doc)
    return doc


def _normalize_concessions(concessions: list | None) -> list[dict]:
    out = []
    for item in concessions or []:
        if isinstance(item, dict):
            out.append({"concession": item.get("concession") or item.get("give") or "",
                        "in_exchange_for": item.get("in_exchange_for") or item.get("get") or ""})
        else:
            out.append({"concession": str(item), "in_exchange_for": ""})
    return [c for c in out if c["concession"]]


def _concession_lines(concessions: list[dict]) -> list[str]:
    lines = []
    for c in concessions:
        if c["in_exchange_for"]:
            lines.append(f"{c['concession']} (in exchange for: {c['in_exchange_for']})")
        else:
            lines.append(c["concession"])
    return lines or ["None"]


# ── Output 1: internal deal approval brief ────────────────────────────────────

def generate_deal_brief(deal_id: str, snapshot: dict, concessions: list[dict],
                        special_terms: list[str], strategy: dict | None = None) -> dict:
    deal_input = get_deal_input(deal_id)
    account = deal_input.get("account", {})
    opportunity = deal_input.get("opportunity", {})
    scenario = snapshot["scenario"]
    baseline = snapshot["financial_analysis"]["baseline"]
    commitment = snapshot["financial_analysis"]["commitment"]
    policy = snapshot["policy_evaluation"]
    readiness = snapshot["deal_readiness"]

    comparable = get_comparable_deal_evidence(deal_id)
    if comparable.get("benchmark_status") == "OK":
        summary = comparable["comparable_summary"]
        position = comparable.get("current_position") or {}
        historical_fields = [
            ("Comparable Deals", summary.get("count")),
            ("Comparable Median Discount", _pct(summary.get("median_discount_pct"))),
            ("Comparable P75 Discount", _pct(summary.get("p75_discount_pct"))),
            ("Comparable Win Rate", _pct(summary.get("win_rate_pct"))),
            ("Customer Ask Position", position.get("position", "UNKNOWN")),
            ("Top Comparables", ", ".join(c["historical_deal_id"] for c in comparable.get("top_comparables", [])[:3])),
        ]
    else:
        historical_fields = [("Comparable Evidence", comparable.get("message", "Insufficient comparable-deal sample."))]

    if strategy and strategy.get("status") == "success":
        strategy_fields = [
            ("Recommended Position", (strategy.get("recommended_position") or {}).get("strategy", "—")),
            ("Rationale", (strategy.get("recommended_position") or {}).get("rationale", "—")),
            ("AI Confidence", (strategy.get("confidence") or {}).get("level", "—")),
        ]
        risks = [f"{r.get('category')}: {r.get('risk')}" for r in (strategy.get("commercial_risks") or [])[:5]]
    else:
        strategy_fields = [("AI Strategy", "Not generated for this package; deterministic validation only.")]
        risks = []
    risks.extend(f"Financial flag: {f}" for f in snapshot["financial_analysis"].get("flags", []))

    approvals = [
        f"{a.get('function')} — {a.get('level')} (REQUIRED, NOT OBTAINED)"
        for a in policy.get("required_approvals", [])
    ]
    exceptions = [
        f"{e.get('exception_type')}: {e.get('resolution_status')}"
        for e in policy.get("exceptions", [])
    ]

    doc = {
        "document_type": "deal_brief",
        "audience": "INTERNAL",
        "title": "Deal Approval Brief — Internal Only",
        "banner": ["INTERNAL DEAL DESK DOCUMENT — NOT FOR CUSTOMER DISTRIBUTION", "Synthetic Deal Desk demonstration data"],
        "sections": [
            {"heading": "Customer & Opportunity", "fields": [
                ("Customer", account.get("name")),
                ("Opportunity", opportunity.get("name")),
                ("Industry", account.get("industry")),
                ("Region", account.get("region")),
                ("Segment", account.get("segment")),
                ("Strategic Account", "Yes" if account.get("strategic_account") else "No"),
                ("Deal Stage", opportunity.get("stage")),
            ]},
            {"heading": "Products", "items": [
                f"{p.get('product')} ({p.get('sku')}) — list {_money(p.get('list_value'))}"
                for p in deal_input.get("products", [])
            ]},
            {"heading": "Commercial Structure", "fields": [
                ("List Value", _money(baseline.get("list_value"))),
                ("Discount", _pct(scenario.get("discount_pct"))),
                ("TCV", _money(baseline.get("tcv"))),
                ("Term", f"{scenario.get('contract_term_months')} months"),
                ("Commitment", _money(scenario.get("commitment_amount"))),
                ("Billing", scenario.get("billing_frequency")),
                ("Payment Terms", scenario.get("payment_terms")),
                ("Price Protection", scenario.get("price_protection") or "None specified"),
            ]},
            {"heading": "Financial Analysis (Deterministic)", "fields": [
                ("NPV", _money(baseline.get("npv"))),
                ("ROI (3yr)", _pct(baseline.get("roi_3yr_pct"))),
                ("Payback", f"{baseline.get('payback_months')} months"),
                ("Expected Consumption", _money(commitment.get("expected_consumption"))),
                ("Commitment Coverage", _pct((commitment.get("coverage_ratio") or 0) * 100) if commitment.get("coverage_ratio") is not None else "Not modeled"),
                ("Gross Margin", "NOT_MODELED"),
            ]},
            {"heading": "Commercial Policy (Deterministic)", "fields": [
                ("Status", policy.get("overall_status")),
                ("Readiness", readiness["status"]),
            ], "items": (exceptions or ["No open exceptions"]) },
            {"heading": "Required Approvals", "items": approvals or ["No approvals required"]},
            {"heading": "Historical Context (Comparable Engine)", "fields": historical_fields},
            {"heading": "AI Commercial Strategy", "fields": strategy_fields},
            {"heading": "Key Risks", "items": risks or ["No flagged risks"]},
            {"heading": "Concessions", "items": _concession_lines(concessions)},
            {"heading": "Special Terms", "items": special_terms or ["None"]},
        ],
    }
    return _finish_document(doc)


# ── Output 2: customer commercial term sheet ──────────────────────────────────

def generate_commercial_term_sheet(deal_id: str, snapshot: dict, concessions: list[dict],
                                   special_terms: list[str]) -> dict:
    deal_input = get_deal_input(deal_id)
    account = deal_input.get("account", {})
    scenario = snapshot["scenario"]
    baseline = snapshot["financial_analysis"]["baseline"]
    commitment_input = deal_input.get("consumption_commitment", {})
    generated = datetime.now(timezone.utc).date()

    doc = {
        "document_type": "commercial_term_sheet",
        "audience": "CUSTOMER",
        "classification": "CUSTOMER_SHAREABLE",
        "title": "Commercial Term Sheet — Synthetic Deal Desk Demonstration",
        "banner": SYNTHETIC_BANNER,
        "sections": [
            {"heading": "Parties", "fields": [
                ("Customer", account.get("name")),
                ("Provider", PROVIDER_NAME),
            ]},
            {"heading": "Term", "fields": [
                ("Effective Date", commitment_input.get("start_date") or "To be agreed at execution"),
                ("Contract Term", f"{scenario.get('contract_term_months')} months"),
            ]},
            {"heading": "Products / Services", "items": [
                f"{p.get('product')} — list value {_money(p.get('list_value'))}"
                for p in deal_input.get("products", [])
            ]},
            {"heading": "Commercial Pricing", "fields": [
                ("Total List Value", _money(baseline.get("list_value"))),
                ("Commercial Discount", _pct(scenario.get("discount_pct"))),
                ("Total Contract Value", _money(baseline.get("tcv"))),
            ]},
            {"heading": "Consumption Commitment", "fields": [
                ("Committed Amount", _money(scenario.get("commitment_amount"))),
                ("Commitment Period", f"{scenario.get('contract_term_months')} months"),
            ]},
            {"heading": "Billing & Payment", "fields": [
                ("Billing Frequency", scenario.get("billing_frequency")),
                ("Payment Terms", scenario.get("payment_terms")),
            ]},
            {"heading": "Price Protection", "text": scenario.get("price_protection") or "No price protection specified in this term sheet."},
            {"heading": "Commercial Concessions", "items": _concession_lines(concessions)},
            {"heading": "Special Commercial Terms", "items": special_terms or ["None"]},
            {"heading": "Expiration / Validity", "text": f"This term sheet is valid until {(generated + timedelta(days=30)).isoformat()} and is subject to execution of a definitive agreement."},
            {"heading": "Dependencies", "items": [
                "Execution of a definitive commercial agreement.",
                "Completion of the provider's internal review process.",
            ]},
            {"heading": "Assumptions", "items": [
                "Commercial terms apply to the products and services listed above.",
                "Consumption commitments apply to eligible services as defined in the definitive agreement.",
                "This is a synthetic demonstration document; it creates no obligations.",
            ]},
        ],
    }
    return _finish_document(doc)


# ── Output 3: consumption commitment schedule ─────────────────────────────────

def _commitment_milestones(deal_input: dict, scenario: dict) -> tuple[list[dict], str]:
    """Use explicit annual targets only when they belong to the scenario's
    commitment amount; otherwise produce an even, clearly-illustrative split."""
    commitment_input = deal_input.get("consumption_commitment", {})
    amount = float(scenario.get("commitment_amount") or 0)
    term_months = int(scenario.get("contract_term_months") or 0)
    years = max(1, round(term_months / 12)) if term_months else 1

    targets = [float(commitment_input.get(f"year_{i}_target") or 0) for i in range(1, years + 1)]
    canonical_amount = float(commitment_input.get("commitment_amount") or 0)
    targets_valid = (
        all(t > 0 for t in targets)
        and abs(sum(targets) - amount) <= max(1000.0, amount * 0.01)
        and abs(amount - canonical_amount) <= 1.0
    )
    if targets_valid:
        milestones = [{"period": f"Year {i + 1}", "amount": t, "basis": "Specified annual target"}
                      for i, t in enumerate(targets)]
        return milestones, "Annual targets specified in the deal commitment structure."

    if amount <= 0:
        return [], "No consumption commitment specified."
    even = round(amount / years, -3)
    amounts = [even] * (years - 1) + [round(amount - even * (years - 1))]
    milestones = [{"period": f"Year {i + 1}", "amount": a, "basis": "Illustrative allocation"}
                  for i, a in enumerate(amounts)]
    return milestones, ("Illustrative allocation only — annual milestones are evenly distributed for "
                        "demonstration and are not contractual milestones.")


def generate_commitment_schedule(deal_id: str, snapshot: dict) -> dict:
    deal_input = get_deal_input(deal_id)
    account = deal_input.get("account", {})
    scenario = snapshot["scenario"]
    commitment_input = deal_input.get("consumption_commitment", {})
    milestones, milestone_note = _commitment_milestones(deal_input, scenario)
    scope = commitment_input.get("eligible_spend_categories") or [p.get("product") for p in deal_input.get("products", [])]

    doc = {
        "document_type": "consumption_commitment_schedule",
        "audience": "CUSTOMER",
        "classification": "CUSTOMER_SHAREABLE",
        "title": "Cloud Consumption Commitment Schedule — Synthetic Deal Desk Demonstration",
        "banner": SYNTHETIC_BANNER,
        "milestone_basis": milestone_note,
        "sections": [
            {"heading": "Parties", "fields": [
                ("Customer", account.get("name")),
                ("Provider", PROVIDER_NAME),
            ]},
            {"heading": "Commitment", "fields": [
                ("Total Commitment", _money(scenario.get("commitment_amount"))),
                ("Commitment Period", f"{scenario.get('contract_term_months')} months"),
                ("Commitment Start", commitment_input.get("start_date") or "To be agreed at execution"),
                ("Commitment End", commitment_input.get("end_date") or "Determined by commitment period from the effective date"),
            ]},
            {"heading": "Eligible Product Scope", "items": list(scope) or ["To be defined in the definitive agreement"]},
            {"heading": "Milestones", "text": milestone_note, "items": [
                f"{m['period']}: {_money(m['amount'])} ({m['basis']})" for m in milestones
            ] or ["Annual milestones unspecified"]},
            {"heading": "Consumption Measurement", "text": "Eligible consumption is measured against the committed amount as defined in the applicable executed agreement."},
            {"heading": "Commitment Tracking", "text": "Remaining commitment is tracked over the commitment period; tracking mechanics are governed by the applicable executed agreement."},
            {"heading": "Shortfall Treatment", "text": "See the final executed agreement and applicable commercial terms."},
            {"heading": "Marketplace Eligibility", "text": "Subject to applicable eligibility requirements."},
            {"heading": "General Terms", "text": "This schedule is a synthetic demonstration artifact and does not modify or replace an executed agreement."},
        ],
        "milestones": milestones,
    }
    return _finish_document(doc)


# ── Output 4: existing Excel financial model (reused, not rebuilt) ────────────

def build_financial_model_workbook(deal_id: str, snapshot: dict, output_path: Path) -> dict:
    """Populate the existing Excel builder from the validated scenario.

    Reuses the exact adapter path the Phase 2 engine used for the snapshot
    (_scenario_from_deal_input → _to_existing_financial_model_input →
    financial_model.compute → excel_builder.build_workbook), so the workbook
    is driven by the same deterministic model as the validation snapshot.
    """
    deal_input = get_deal_input(deal_id)
    payload = {"name": "deal_builder_final", **{k: v for k, v in snapshot["scenario"].items() if v is not None}}
    scenario = _scenario_from_deal_input(deal_input, payload)
    gathered = _to_existing_financial_model_input(deal_input, scenario)
    # Smallest adapter gap: the workbook builder needs the implementation
    # pattern, which the Phase 2 adapter does not carry. Reuse the existing
    # deterministic assignment rather than inventing one.
    gathered.setdefault("impl_pattern", _assign_pattern(gathered["deal_amount"]))
    model = compute_financial_model(gathered)
    consistency = verify_excel_consistency(snapshot, scenario, model)
    build_workbook(gathered, model, output_path)
    return {
        "document_type": "financial_model",
        "audience": "INTERNAL",
        "title": "Financial Model Workbook (existing Deal Desk Excel builder)",
        "file": output_path.name,
        "builder": "tools.excel_builder.build_workbook",
        "consistency_check": consistency,
    }


def verify_excel_consistency(snapshot: dict, scenario: dict, model: dict) -> dict:
    """Key workbook-model values must match the validation snapshot exactly
    (both derive from the same deterministic engine). Mismatch fails the package."""
    baseline = snapshot["financial_analysis"]["baseline"]
    checks = [
        ("discount_pct", float(snapshot["scenario"]["discount_pct"]), float(scenario["discount_pct"]), 0.01),
        ("tcv", float(baseline["tcv"]), round(float(scenario["tcv"])), 1.0),
        ("commitment_amount", float(snapshot["scenario"]["commitment_amount"]), float(scenario["commitment"]["commitment_amount"]), 1.0),
        ("npv", float(baseline["npv"]), round(float(model["npv_10pct"])), 1.0),
        ("roi_3yr_pct", float(baseline["roi_3yr_pct"]), round(float(model["roi_3yr_pct"]), 2), 0.01),
        ("payback_months", float(baseline["payback_months"]), round(float(model["payback_months"]), 2), 0.01),
    ]
    results = []
    mismatches = []
    for metric, snapshot_value, excel_value, tolerance in checks:
        match = abs(snapshot_value - excel_value) <= tolerance
        results.append({"metric": metric, "snapshot_value": snapshot_value, "excel_value": excel_value, "match": match})
        if not match:
            mismatches.append(metric)
    if mismatches:
        raise DealPackageError(
            "PACKAGE_VALIDATION_FAILED",
            f"Excel workbook values diverge from the validation snapshot: {', '.join(mismatches)}",
        )
    return {"status": "CONSISTENT", "checks": results,
            "note": "Workbook and snapshot are driven by the same deterministic financial model."}


# ── Output 5: synthetic draft agreement ───────────────────────────────────────

def _default_agreement_prose(account_name: str, scenario: dict) -> dict:
    return {
        "purpose": (
            f"This synthetic agreement records the demonstration commercial structure under which "
            f"{PROVIDER_NAME} would provide the listed cloud products and services to {account_name}, "
            f"including a consumption commitment of {_money(scenario.get('commitment_amount'))} over "
            f"{scenario.get('contract_term_months')} months."
        ),
        "general_terms": (
            "This document is a synthetic demonstration artifact generated by a Deal Desk application. "
            "It is not an offer, not a Microsoft agreement, and creates no legal obligations. "
            "All detailed legal terms are governed by the applicable executed agreement."
        ),
    }


def _llm_agreement_prose(llm_client: Callable[[str], str], account_name: str, scenario: dict) -> dict | None:
    """Optional prose polish. The LLM receives only customer-shareable terms and
    may return 'purpose'/'general_terms' strings. Anything invalid, oversized, or
    leaking internal data falls back to the deterministic templates."""
    terms = {
        "customer": account_name,
        "provider": PROVIDER_NAME,
        "discount_pct": scenario.get("discount_pct"),
        "contract_term_months": scenario.get("contract_term_months"),
        "commitment_amount": scenario.get("commitment_amount"),
        "billing_frequency": scenario.get("billing_frequency"),
        "payment_terms": scenario.get("payment_terms"),
        "price_protection": scenario.get("price_protection"),
    }
    try:
        raw = llm_client(AGREEMENT_PROMPT + "\n\nVALIDATED TERMS:\n" + json.dumps(terms, indent=2))
        parsed = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        prose = {}
        for key in ("purpose", "general_terms"):
            value = parsed.get(key)
            if isinstance(value, str) and 0 < len(value) <= 1500:
                assert_no_internal_leakage({"document_type": f"agreement_{key}", "text": value})
                prose[key] = value
        return prose or None
    except Exception:
        return None


def generate_synthetic_agreement(deal_id: str, snapshot: dict, concessions: list[dict],
                                 special_terms: list[str],
                                 llm_client: Callable[[str], str] | None = None) -> dict:
    deal_input = get_deal_input(deal_id)
    account = deal_input.get("account", {})
    scenario = snapshot["scenario"]
    baseline = snapshot["financial_analysis"]["baseline"]
    commitment_input = deal_input.get("consumption_commitment", {})

    prose = _default_agreement_prose(account.get("name", "the customer"), scenario)
    narrative_source = "deterministic_template"
    if llm_client is not None:
        llm_prose = _llm_agreement_prose(llm_client, account.get("name", "the customer"), scenario)
        if llm_prose:
            prose.update(llm_prose)
            narrative_source = "llm_with_deterministic_fallback"

    reference = "governed by the applicable executed agreement"
    doc = {
        "document_type": "synthetic_agreement",
        "audience": "CUSTOMER",
        "classification": "CUSTOMER_SHAREABLE",
        "title": "Cloud Service Agreement Order Form — Synthetic Deal Desk Demonstration",
        "banner": SYNTHETIC_BANNER,
        "narrative_source": narrative_source,
        "template_reference": COMMON_PAPER_TEMPLATE_REFERENCE,
        "sections": [
            {"heading": "1. Order Form", "text": (
                "This synthetic Order Form records the customer-specific business terms for a demonstration cloud service transaction. "
                "It follows a Common Paper-style cloud service agreement layout as a structural reference only."
            )},
            {"heading": "2. Parties", "fields": [("Customer", account.get("name")), ("Provider", PROVIDER_NAME)]},
            {"heading": "3. Cloud Service", "items": [
                f"{p.get('product')} ({p.get('sku')})" for p in deal_input.get("products", [])
            ]},
            {"heading": "4. Order Date, Effective Date, and Term", "fields": [
                ("Effective Date", commitment_input.get("start_date") or "To be agreed at execution"),
                ("Term", f"{scenario.get('contract_term_months')} months from the effective date"),
            ]},
            {"heading": "5. Subscription Details", "text": prose["purpose"]},
            {"heading": "6. Cloud Service Fees", "fields": [
                ("Total List Value", _money(baseline.get("list_value"))),
                ("Commercial Discount", _pct(scenario.get("discount_pct"))),
                ("Total Contract Value", _money(baseline.get("tcv"))),
            ]},
            {"heading": "7. Consumption Commitment", "text": (
                f"The customer commits to {_money(scenario.get('commitment_amount'))} of eligible consumption over "
                f"{scenario.get('contract_term_months')} months, as detailed in the Cloud Consumption Commitment Schedule."
            )},
            {"heading": "8. Payment Process", "fields": [
                ("Billing Frequency", scenario.get("billing_frequency")),
                ("Payment Terms", scenario.get("payment_terms")),
            ]},
            {"heading": "9. Auto-Renewal", "text": "Renewal mechanics are to be defined in the definitive agreement or applicable ordering document."},
            {"heading": "10. Use Limitations", "text": "Use limitations, if any, are governed by the applicable executed agreement and product terms."},
            {"heading": "11. Technical Support", "text": "Support channels and response commitments are governed by the applicable support policy or service-level documentation."},
            {"heading": "12. Service Level Agreement", "text": "Service levels are referenced for convenience only and must be confirmed in the definitive service-level agreement."},
            {"heading": "13. Professional Services", "text": "Professional services, migration assistance, or implementation support must be documented in an applicable statement of work or services exhibit."},
            {"heading": "14. Commercial Concessions", "items": _concession_lines(concessions)},
            {"heading": "15. Price Protection", "text": scenario.get("price_protection") or "No price protection is specified in this synthetic agreement."},
            {"heading": "16. Framework Terms Reference", "text": (
                "Detailed legal terms, including privacy, security, confidentiality, warranties, indemnity, liability limits, termination, "
                f"and governing law, are {reference}. This document does not restate or replace those terms."
            )},
            {"heading": "17. Order of Precedence", "text": "The definitive executed agreement would control over this synthetic document and any illustrative schedules."},
            {"heading": "18. Other Changes to Standard Terms", "items": special_terms or ["No additional synthetic modifications specified."]},
            {"heading": "19. General Terms", "text": prose["general_terms"]},
            {"heading": "20. Signature Placeholder", "text": "NOT FOR EXECUTION — signature fields intentionally omitted for this synthetic demonstration document."},
        ],
    }
    return _finish_document(doc)


# ── package assembly ──────────────────────────────────────────────────────────

def _next_package_version(deal_id: str) -> int:
    existing = list_deal_packages(deal_id)
    return max((p.get("version", 0) for p in existing), default=0) + 1


def generate_deal_package(deal_id: str, scenario: dict | None = None,
                          concessions: list | None = None,
                          special_terms: list[str] | None = None,
                          strategy: dict | None = None,
                          llm_client: Callable[[str], str] | None = None) -> dict:
    """Final validation, all five documents, manifest, and persistence."""
    snapshot = validate_deal_package(deal_id, scenario)
    readiness = snapshot["deal_readiness"]
    if readiness["status"] == "INCOMPLETE":
        raise DealPackageError(
            "PACKAGE_INCOMPLETE",
            f"Missing required inputs: {', '.join(readiness['missing_fields'] + readiness['agreement_input_gaps'])}",
        )
    if readiness["status"] == "BLOCKED":
        raise DealPackageError(
            "PACKAGE_BLOCKED",
            "The scenario is blocked by policy or fails financial validation; resolve it before packaging.",
        )

    concession_list = _normalize_concessions(concessions)
    terms = [str(t) for t in (special_terms or []) if str(t).strip()]
    version = _next_package_version(deal_id)
    package_id = f"PKG-{deal_id}-{version:03d}"
    packages_dir = PACKAGES_DIR
    packages_dir.mkdir(parents=True, exist_ok=True)

    excel_meta = build_financial_model_workbook(deal_id, snapshot, packages_dir / f"{package_id}.xlsx")
    documents = {
        "deal_brief": generate_deal_brief(deal_id, snapshot, concession_list, terms, strategy),
        "commercial_term_sheet": generate_commercial_term_sheet(deal_id, snapshot, concession_list, terms),
        "consumption_commitment_schedule": generate_commitment_schedule(deal_id, snapshot),
        "financial_model": excel_meta,
        "synthetic_agreement": generate_synthetic_agreement(deal_id, snapshot, concession_list, terms, llm_client),
    }

    package = {
        "package_id": package_id,
        "deal_id": deal_id,
        "version": version,
        "package_version": f"{version}.0",
        "generated_at": _utc_now(),
        "validated_at": snapshot["validated_at"],
        "deal_readiness": readiness,
        "scenario": snapshot["scenario"],
        "scenario_hash": snapshot["scenario_hash"],
        "concessions": concession_list,
        "special_terms": terms,
        "documents": list(documents.keys()),
        "document_contents": documents,
        "validation_snapshot": snapshot,
        "excel_file": excel_meta["file"],
        "reference_links": MICROSOFT_REFERENCE_LINKS,
        "disclaimer": "All documents in this package are synthetic Deal Desk demonstration artifacts, not Microsoft agreements, and not for execution.",
    }
    (packages_dir / f"{package_id}.json").write_text(json.dumps(package, indent=2, default=str) + "\n", encoding="utf-8")
    return package


def get_package_manifest(package: dict) -> dict:
    return {k: package[k] for k in (
        "package_id", "deal_id", "version", "package_version", "generated_at",
        "validated_at", "deal_readiness", "scenario", "scenario_hash",
        "documents", "excel_file", "reference_links", "disclaimer",
    )}


def list_deal_packages(deal_id: str) -> list[dict]:
    if not PACKAGES_DIR.exists():
        return []
    packages = []
    for path in sorted(PACKAGES_DIR.glob(f"PKG-{deal_id}-*.json")):
        try:
            packages.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return sorted(packages, key=lambda p: p.get("version", 0))


def get_deal_package(deal_id: str, package_id: str) -> dict | None:
    path = PACKAGES_DIR / f"{package_id}.json"
    if not path.exists() or not package_id.startswith(f"PKG-{deal_id}-"):
        return None
    return json.loads(path.read_text(encoding="utf-8"))


