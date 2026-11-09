"""Commercial data access layer for Phase 1 Commercial Analysis.

This module reads the canonical commercial data generated under
knowledge_base/commercial_strategy. It does not call an LLM, change the UI, or
replace the existing deterministic financial model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
COMMERCIAL_DIR = BASE_DIR / "knowledge_base" / "commercial_strategy"
DEAL_INPUTS_PATH = COMMERCIAL_DIR / "canonical_deal_inputs.json"
HISTORICAL_PATH = COMMERCIAL_DIR / "historical_comparable_deals.json"
GUARDRAILS_PATH = COMMERCIAL_DIR / "synthetic_commercial_guardrails.json"
APPROVAL_MATRIX_PATH = COMMERCIAL_DIR / "synthetic_approval_matrix.json"


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def get_deal_input(deal_id: str) -> dict | None:
    """Return the canonical DealInput object for one opportunity/deal ID."""
    for item in _load_json(DEAL_INPUTS_PATH):
        if item.get("deal_id") == deal_id or item.get("opportunity_id") == deal_id:
            return item
    return None


def get_account_commercial_profile(account_id: str) -> dict | None:
    """Return account-level commercial context for an account ID or account name."""
    needle = account_id.lower()
    for item in _load_json(DEAL_INPUTS_PATH):
        account = item.get("account", {})
        if account.get("account_id", "").lower() == needle or account.get("name", "").lower() == needle:
            return {
                "account": account,
                "active_deals": [
                    {
                        "deal_id": d.get("deal_id"),
                        "opportunity": d.get("opportunity"),
                        "commercial": d.get("commercial"),
                        "strategic_context": d.get("strategic_context"),
                    }
                    for d in _load_json(DEAL_INPUTS_PATH)
                    if d.get("account", {}).get("account_id") == account.get("account_id")
                ],
            }
    return None


def get_deal_products(deal_id: str) -> list[dict]:
    """Return product/SKU lines for a deal."""
    deal = get_deal_input(deal_id)
    return deal.get("products", []) if deal else []


def get_deal_commitment(deal_id: str) -> dict | None:
    """Return the synthetic consumption commitment block for a deal."""
    deal = get_deal_input(deal_id)
    return deal.get("consumption_commitment") if deal else None


def get_historical_deals() -> list[dict]:
    """Return normalized historical comparable deals."""
    return _load_json(HISTORICAL_PATH)


def get_commercial_guardrails() -> dict:
    """Return synthetic demo commercial guardrails."""
    return _load_json(GUARDRAILS_PATH)


def get_approval_matrix() -> dict:
    """Return synthetic demo approval matrix."""
    return _load_json(APPROVAL_MATRIX_PATH)


def get_comparable_deal_candidates(deal_id: str, limit: int = 8) -> list[dict]:
    """Return simple deterministic comparable candidates for a DealInput.

    Legacy Phase 1 helper. Phase 6 supersedes this with
    tools.comparable_deal_intelligence.find_comparable_deals, which adds
    configurable weights, candidate filtering, benchmarks, and traceability.
    """
    deal = get_deal_input(deal_id)
    if not deal:
        return []
    account = deal.get("account", {})
    commercial = deal.get("commercial", {})
    products = {p.get("product") for p in deal.get("products", [])}
    amount = float(commercial.get("tcv") or commercial.get("proposed_net_value") or 0)
    scored = []
    for hist in get_historical_deals():
        score = 0
        if hist.get("industry") == account.get("industry"):
            score += 30
        if hist.get("account_segment") == account.get("segment"):
            score += 20
        if products.intersection(hist.get("products") or []):
            score += 20
        hist_amount = float(hist.get("tcv") or hist.get("list_value") or 0)
        if amount and hist_amount:
            ratio = min(amount, hist_amount) / max(amount, hist_amount)
            score += int(ratio * 20)
        if hist.get("strategic_account") == account.get("strategic_account"):
            score += 10
        candidate = dict(hist)
        candidate["similarity_score"] = score
        scored.append(candidate)
    return sorted(scored, key=lambda x: x["similarity_score"], reverse=True)[:limit]


def validate_deal_input_object(deal_input: dict) -> list[dict]:
    """Validate commercially important DealInput fields."""
    issues = []
    commercial = deal_input.get("commercial", {})
    for field in ("requested_discount_pct", "proposed_discount_pct"):
        value = commercial.get(field)
        if value is None or float(value) < 0 or float(value) > 100:
            issues.append({"field": f"commercial.{field}", "message": "Discount must be between 0 and 100 percent."})
    if int(commercial.get("contract_term_months") or 0) <= 0:
        issues.append({"field": "commercial.contract_term_months", "message": "Contract term must be positive."})
    if not commercial.get("currency"):
        issues.append({"field": "commercial.currency", "message": "Currency must be defined."})
    if not deal_input.get("products"):
        issues.append({"field": "products", "message": "Product list cannot be empty."})
    commitment = deal_input.get("consumption_commitment", {})
    if float(commitment.get("commitment_amount") or 0) < 0:
        issues.append({"field": "consumption_commitment.commitment_amount", "message": "Commitment cannot be negative."})
    if not deal_input.get("account", {}).get("account_id"):
        issues.append({"field": "account.account_id", "message": "Opportunity must reference a valid account."})
    try:
        from tools.commercial_validation import numeric_reconciliation_issues
    except ModuleNotFoundError:
        from commercial_validation import numeric_reconciliation_issues
    issues.extend(numeric_reconciliation_issues(deal_input))
    return issues


