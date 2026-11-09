"""AI commercial strategy agent with deterministic validation.

The LLM proposes narrative strategy only. Financial metrics and policy status
are always replaced by tools.commercial_financial_analysis and
tools.commercial_policy_engine outputs before results are returned.
"""

from __future__ import annotations

try:
    from tools.llm_client import TOGETHER_CHAT_URL, TOGETHER_MODEL, request_completion
except ModuleNotFoundError:
    from llm_client import TOGETHER_CHAT_URL, TOGETHER_MODEL, request_completion

import hashlib
import json
import os
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

try:
    from tools.commercial_data import get_deal_input
    from tools.commercial_financial_analysis import evaluate_financial_scenario, get_commercial_financial_analysis
    from tools.commercial_policy_engine import evaluate_commercial_policy
    from tools.comparable_deal_intelligence import get_comparable_deal_evidence
except ModuleNotFoundError:
    from commercial_data import get_deal_input
    from commercial_financial_analysis import evaluate_financial_scenario, get_commercial_financial_analysis
    from commercial_policy_engine import evaluate_commercial_policy
    from comparable_deal_intelligence import get_comparable_deal_evidence


BASE_DIR = Path(__file__).resolve().parent.parent
COMMERCIAL_DIR = BASE_DIR / "knowledge_base" / "commercial_strategy"
STRATEGY_CACHE_PATH = COMMERCIAL_DIR / "commercial_strategy_cache.json"
STRATEGY_SCHEMA_PATH = COMMERCIAL_DIR / "commercial_strategy_output_schema.json"

_ACTIVE_LLM_PROVIDER: ContextVar[str] = ContextVar("active_llm_provider", default="unknown")
ALLOWED_STRATEGIES = {"ACCEPT", "COUNTER", "RESTRUCTURE", "ESCALATE", "DECLINE"}
SUPPORTED_STRUCTURE_FIELDS = {
    "discount_pct",
    "contract_term_months",
    "commitment_amount",
    "billing_frequency",
    "payment_terms",
}
RISK_CATEGORIES = {"FINANCIAL", "COMPETITIVE", "CONSUMPTION", "CONTRACTUAL", "CUSTOMER", "EXECUTION", "STRATEGIC"}
EVIDENCE_SOURCE_TYPES = {"DEAL_INPUT", "FINANCIAL_ENGINE", "POLICY_ENGINE", "ACCOUNT_NOTE", "SALES_NOTE", "FINANCE_NOTE", "HISTORICAL_COMPARABLE"}
SELLER_GET_PATTERNS = (
    "additional 12 months",
    "additional term",
    "contract term",
    "term extension",
    "longer term",
    "extend",
    "extension",
    "commitment",
    "consumption",
    "upfront",
    "annual billing",
    "faster signature",
    "reference",
    "executive access",
    "reduced scope",
)
SELLER_GIVE_PATTERNS = (
    "discount",
    "price protection",
    "migration support",
    "professional services",
    "free",
    "payment flexibility",
    "net 60",
    "net 90",
    "flexibility",
)


class CommercialStrategyAgentError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json_default(value: Any) -> str:
    return str(value)


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_cache() -> dict:
    if not STRATEGY_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(STRATEGY_CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_cache(cache: dict) -> None:
    COMMERCIAL_DIR.mkdir(parents=True, exist_ok=True)
    STRATEGY_CACHE_PATH.write_text(json.dumps(cache, indent=2, default=_json_default) + "\n", encoding="utf-8")


def _customer_ask_payload(deal_input: dict) -> dict:
    commercial = deal_input.get("commercial", {})
    ask = deal_input.get("negotiation_context", {}).get("customer_requested_commercial_terms", {})
    commitment = deal_input.get("consumption_commitment", {})
    return {
        "name": "customer_ask",
        "discount_pct": commercial.get("requested_discount_pct"),
        "contract_term_months": ask.get("requested_agreement_term_months", commercial.get("contract_term_months")),
        "commitment_amount": ask.get("requested_total_commitment", commitment.get("commitment_amount")),
        "billing_frequency": commercial.get("billing_frequency"),
        "payment_terms": ask.get("requested_payment_terms", commercial.get("payment_terms")),
        "price_protection": ask.get("requested_price_protection"),
    }


def _working_payload(deal_input: dict, working_scenario: dict | None) -> dict:
    commercial = deal_input.get("commercial", {})
    commitment = deal_input.get("consumption_commitment", {})
    try:
        from tools.commercial_financial_analysis import default_working_discount_pct
    except ModuleNotFoundError:
        from commercial_financial_analysis import default_working_discount_pct
    base = {
        "name": "working_scenario",
        "discount_pct": default_working_discount_pct(deal_input),
        "contract_term_months": commercial.get("contract_term_months"),
        "commitment_amount": commitment.get("commitment_amount"),
        "billing_frequency": commercial.get("billing_frequency"),
        "payment_terms": commercial.get("payment_terms"),
    }
    if working_scenario:
        for key in SUPPORTED_STRUCTURE_FIELDS:
            if key in working_scenario:
                base[key] = working_scenario[key]
    return base


def build_commercial_strategy_context(deal_id: str, working_scenario: dict | None = None) -> dict:
    deal_input = get_deal_input(deal_id)
    if not deal_input:
        raise CommercialStrategyAgentError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")

    ask_payload = _customer_ask_payload(deal_input)
    working_payload = _working_payload(deal_input, working_scenario)
    financial = get_commercial_financial_analysis(deal_id)
    customer_policy = evaluate_commercial_policy(deal_id, ask_payload)
    working_financial = evaluate_financial_scenario(deal_id, working_payload)
    working_policy = evaluate_commercial_policy(deal_id, working_payload)
    notes = deal_input.get("negotiation_context", {}).get("negotiation_notes", [])
    comparable_evidence = get_comparable_deal_evidence(deal_id)

    return {
        "deal_id": deal_id,
        "deal": {
            "customer": deal_input.get("account", {}).get("name"),
            "opportunity": deal_input.get("opportunity", {}).get("name"),
            "industry": deal_input.get("account", {}).get("industry"),
            "region": deal_input.get("account", {}).get("region"),
            "segment": deal_input.get("account", {}).get("segment"),
            "deal_stage": deal_input.get("opportunity", {}).get("stage"),
            "products": deal_input.get("products", []),
            "contract_term_months": deal_input.get("commercial", {}).get("contract_term_months"),
        },
        "customer_ask": {
            "structured": ask_payload,
            "raw_asks": deal_input.get("customer_asks", []),
            "financial_validation": financial.get("scenarios", {}).get("customer_ask"),
            "policy_validation": customer_policy,
        },
        "working_scenario": {
            "structured": working_payload,
            "financial_validation": working_financial,
            "policy_validation": working_policy,
        },
        "financial_analysis": financial,
        "comparable_deal_intelligence": comparable_evidence,
        "competitive_context": deal_input.get("competitive_context", {}),
        "strategic_context": deal_input.get("strategic_context", {}),
        "notes": notes,
        "data_constraints": {
            "gross_margin": "NOT_MODELED",
            "historical_comparables_used": True,
            "comparable_evidence_source": "deterministic_comparable_engine",
            "llm_financial_metrics_allowed": False,
            "llm_policy_thresholds_allowed": False,
            "llm_benchmark_calculations_allowed": False,
        },
    }


def parse_strategy_response(text: str) -> dict:
    cleaned = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.S | re.I)
    if fenced:
        cleaned = fenced.group(1).strip()
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start:end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise CommercialStrategyAgentError("INVALID_STRATEGY_JSON", "LLM did not return valid JSON.") from exc


def _unsupported_fields(structure: dict) -> list[str]:
    return sorted(set(structure or {}) - SUPPORTED_STRUCTURE_FIELDS - {"name"})


def _validation_payload(structure: dict) -> dict:
    return {"name": "ai_proposed_strategy", **{k: v for k, v in (structure or {}).items() if k in SUPPORTED_STRUCTURE_FIELDS}}


def validate_commercial_strategy(deal_id: str, proposed_structure: dict) -> dict:
    unsupported = _unsupported_fields(proposed_structure)
    if unsupported:
        return {
            "validated": False,
            "validation_status": "REJECTED_UNSUPPORTED_FIELD",
            "unsupported_fields": unsupported,
            "message": f"Unsupported proposed field(s): {', '.join(unsupported)}",
        }
    payload = _validation_payload(proposed_structure)
    financial = evaluate_financial_scenario(deal_id, payload)
    policy = evaluate_commercial_policy(deal_id, payload)
    npv = financial.get("baseline", {}).get("npv")
    blocked = policy.get("overall_status") == "BLOCKED"
    negative_npv = npv is not None and float(npv) < 0
    return {
        "validated": not blocked and not negative_npv,
        "validation_status": "VALIDATED" if not blocked and not negative_npv else "VALIDATION_FAILED",
        "scenario_overrides": payload,
        "financial_validation": financial,
        "policy_validation": policy,
        "validation_findings": {
            "blocked_by_policy": blocked,
            "negative_npv": negative_npv,
            "gross_margin": "NOT_MODELED",
        },
    }


def _validate_shape(data: dict) -> None:
    position = data.get("recommended_position") or {}
    strategy = position.get("strategy")
    if strategy not in ALLOWED_STRATEGIES:
        raise CommercialStrategyAgentError("INVALID_STRATEGY_VALUE", "Strategy must be one of ACCEPT, COUNTER, RESTRUCTURE, ESCALATE, DECLINE.")
    unsupported = _unsupported_fields(data.get("proposed_structure") or {})
    if unsupported:
        raise CommercialStrategyAgentError("UNSUPPORTED_PROPOSED_FIELD", f"Unsupported proposed field(s): {', '.join(unsupported)}")
    confidence = data.get("confidence", {}).get("level")
    if confidence and confidence not in {"HIGH", "MEDIUM", "LOW"}:
        raise CommercialStrategyAgentError("INVALID_CONFIDENCE", "Confidence must be HIGH, MEDIUM, or LOW.")
    for risk in data.get("commercial_risks", []) or []:
        if risk.get("category") not in RISK_CATEGORIES:
            raise CommercialStrategyAgentError("INVALID_RISK_CATEGORY", f"Unsupported risk category: {risk.get('category')}")


def _evidence(context: dict, llm_evidence: list[dict] | None = None) -> list[dict]:
    evidence = [
        {"source_type": "DEAL_INPUT", "label": "Canonical DealInput", "detail": context["deal"]["customer"]},
        {"source_type": "FINANCIAL_ENGINE", "label": "CommercialFinancialAnalysis", "detail": "TCV, NPV, ROI, payback, commitment economics"},
        {"source_type": "POLICY_ENGINE", "label": "CommercialPolicyEngine", "detail": "Policy status, exceptions, required approvals"},
    ]
    comparable = context.get("comparable_deal_intelligence") or {}
    summary = comparable.get("comparable_summary") or {}
    if comparable.get("benchmark_status") == "OK":
        position = comparable.get("current_position") or {}
        evidence.append({
            "source_type": "HISTORICAL_COMPARABLE",
            "label": "Comparable Deal Intelligence",
            "detail": (
                f"{summary.get('count')} comparable deals: median discount {summary.get('median_discount_pct')}%, "
                f"P75 {summary.get('p75_discount_pct')}%, win rate {summary.get('win_rate_pct')}%; "
                f"customer ask position {position.get('position', 'UNKNOWN')}"
            ),
        })
        for comp in (comparable.get("top_comparables") or [])[:3]:
            evidence.append({
                "source_type": "HISTORICAL_COMPARABLE",
                "label": comp.get("historical_deal_id", "Historical deal"),
                "detail": (
                    f"{round((comp.get('similarity_score') or 0) * 100)}% similarity, {comp.get('outcome')}, "
                    f"{comp.get('final_discount_pct')}% discount, {comp.get('contract_term_months')}-month term"
                ),
            })
    elif comparable:
        evidence.append({
            "source_type": "HISTORICAL_COMPARABLE",
            "label": "Comparable Deal Intelligence",
            "detail": comparable.get("message", "Insufficient comparable-deal sample for reliable benchmarking."),
        })
    for note in context.get("notes", [])[:3]:
        role = str(note.get("author_role", "")).upper()
        source = "FINANCE_NOTE" if "FINANCE" in role else "SALES_NOTE" if "SALES" in role or "AE" in role else "ACCOUNT_NOTE"
        evidence.append({"source_type": source, "label": note.get("author_role", "Account note"), "detail": note.get("note", "")})
    for item in llm_evidence or []:
        if item.get("source_type") in EVIDENCE_SOURCE_TYPES:
            evidence.append(item)
    return evidence


def _prompt(context: dict, repair_feedback: dict | None = None) -> str:
    instructions = {
        "role": "AI Commercial Strategy Agent",
        "rules": [
            "Return only one JSON object.",
            "Use strategy values only: ACCEPT, COUNTER, RESTRUCTURE, ESCALATE, DECLINE.",
            "Use commercial_risks category values only: FINANCIAL, COMPETITIVE, CONSUMPTION, CONTRACTUAL, CUSTOMER, EXECUTION, STRATEGIC.",
            "Use evidence source_type values only: DEAL_INPUT, FINANCIAL_ENGINE, POLICY_ENGINE, ACCOUNT_NOTE, SALES_NOTE, FINANCE_NOTE, HISTORICAL_COMPARABLE.",
            "Proposed structure may include only discount_pct, contract_term_months, commitment_amount, billing_frequency, payment_terms.",
            "Do not invent facts, competitor pricing, policy thresholds, financial metrics, historical precedents, Microsoft internal policy, or gross margin.",
            "Acknowledge gross margin as NOT_MODELED where relevant.",
            "The financial and policy engines are the source of truth; do not calculate TCV, NPV, ROI, payback, approvals, or compliance.",
            "comparable_deal_intelligence contains deterministic historical evidence. It is evidence, not policy; do not blindly copy historical discounts into the recommendation.",
            "Only cite benchmark statistics that appear verbatim in comparable_deal_intelligence; never compute your own benchmarks, percentiles, or variances.",
            "If comparable_deal_intelligence reports benchmark_status other than OK, state that the comparable sample is insufficient and do not claim any benchmark.",
            "Distinguish won from lost precedent: lost and rejected deals are cautionary evidence, not templates. Consider sample size when weighing evidence.",
            "Cite operator-supplied historical deal IDs for major precedent-based claims, using evidence entries with source_type HISTORICAL_COMPARABLE.",
            "Historical approval patterns are descriptive only; current-deal approvals come exclusively from the policy engine.",
            "Give/get options are from the seller's perspective: give is a seller concession to the customer, get is customer consideration back to the seller.",
            "Valid seller gives include additional discount, services, migration support, payment flexibility, price protection, or contractual flexibility.",
            "Valid seller gets include longer term, higher consumption commitment, annual/upfront billing, faster signature, reference rights, executive access, or reduced scope.",
        ],
        "required_json_shape": {'type': 'object',
 'description': 'Required proposal fields; this contract contains no example deal values.',
 'properties': {'executive_summary': {'type': 'string',
                                      'description': 'Narrative summary of the recommended '
                                                     'position.'},
                'recommended_position': {'type': 'object',
                                         'description': 'Recommended negotiation posture.',
                                         'properties': {'strategy': {'type': 'string',
                                                                     'description': 'Negotiation '
                                                                                    'posture.',
                                                                     'enum': ['ACCEPT',
                                                                              'COUNTER',
                                                                              'RESTRUCTURE',
                                                                              'ESCALATE',
                                                                              'DECLINE']},
                                                        'rationale': {'type': 'string',
                                                                      'description': 'Reason for '
                                                                                     'the '
                                                                                     'recommended '
                                                                                     'posture.'}}},
                'proposed_structure': {'type': 'object',
                                       'description': 'Fields the commercial strategy agent may '
                                                      'propose; the engines re-evaluate them.',
                                       'properties': {'discount_pct': {'type': 'number',
                                                                       'description': 'Proposed '
                                                                                      'discount on '
                                                                                      'list value.',
                                                                       'minimum': 0,
                                                                       'maximum': 100,
                                                                       'x-unit': 'percent'},
                                                      'contract_term_months': {'type': 'integer',
                                                                               'description': 'Proposed '
                                                                                              'contract '
                                                                                              'duration.',
                                                                               'minimum': 1,
                                                                               'x-unit': 'months'},
                                                      'commitment_amount': {'type': 'number',
                                                                            'description': 'Proposed '
                                                                                           'total '
                                                                                           'consumption '
                                                                                           'commitment.',
                                                                            'x-unit': 'currency '
                                                                                      'units'},
                                                      'billing_frequency': {'type': 'string',
                                                                            'description': 'Proposed '
                                                                                           'billing '
                                                                                           'cadence.'},
                                                      'payment_terms': {'type': 'string',
                                                                        'description': 'Proposed '
                                                                                       'payment '
                                                                                       'terms.'}}},
                'customer_ask_comparison': {'type': 'array',
                                            'description': 'Comparison of customer asks and '
                                                           'recommendations.',
                                            'items': {'type': 'object',
                                                      'description': 'Comparison item.',
                                                      'properties': {'dimension': {'type': 'string',
                                                                                   'description': 'Term '
                                                                                                  'or '
                                                                                                  'commercial '
                                                                                                  'dimension '
                                                                                                  'being '
                                                                                                  'compared.'},
                                                                     'customer_ask': {'type': 'string',
                                                                                      'description': "Customer's "
                                                                                                     'requested '
                                                                                                     'position.'},
                                                                     'recommendation': {'type': 'string',
                                                                                        'description': 'Recommended '
                                                                                                       'position.'},
                                                                     'reason': {'type': 'string',
                                                                                'description': 'Reason '
                                                                                               'for '
                                                                                               'the '
                                                                                               'recommendation.'}}}},
                'give_get_options': {'type': 'array',
                                     'description': 'Potential exchanges requiring engine '
                                                    'validation.',
                                     'items': {'type': 'object',
                                               'description': "Give/get option from the seller's "
                                                              'perspective.',
                                               'properties': {'name': {'type': 'string',
                                                                       'description': 'Option '
                                                                                      'label.'},
                                                              'give': {'type': 'string',
                                                                       'description': 'Seller '
                                                                                      'concession '
                                                                                      'to the '
                                                                                      'customer.'},
                                                              'get': {'type': 'string',
                                                                      'description': 'Customer '
                                                                                     'consideration '
                                                                                     'returned to '
                                                                                     'the seller.'},
                                                              'proposed_structure': {'type': 'object',
                                                                                     'description': 'Fields '
                                                                                                    'the '
                                                                                                    'commercial '
                                                                                                    'strategy '
                                                                                                    'agent '
                                                                                                    'may '
                                                                                                    'propose; '
                                                                                                    'the '
                                                                                                    'engines '
                                                                                                    're-evaluate '
                                                                                                    'them.',
                                                                                     'properties': {'discount_pct': {'type': 'number',
                                                                                                                     'description': 'Proposed '
                                                                                                                                    'discount '
                                                                                                                                    'on '
                                                                                                                                    'list '
                                                                                                                                    'value.',
                                                                                                                     'minimum': 0,
                                                                                                                     'maximum': 100,
                                                                                                                     'x-unit': 'percent'},
                                                                                                    'contract_term_months': {'type': 'integer',
                                                                                                                             'description': 'Proposed '
                                                                                                                                            'contract '
                                                                                                                                            'duration.',
                                                                                                                             'minimum': 1,
                                                                                                                             'x-unit': 'months'},
                                                                                                    'commitment_amount': {'type': 'number',
                                                                                                                          'description': 'Proposed '
                                                                                                                                         'total '
                                                                                                                                         'consumption '
                                                                                                                                         'commitment.',
                                                                                                                          'x-unit': 'currency '
                                                                                                                                    'units'},
                                                                                                    'billing_frequency': {'type': 'string',
                                                                                                                          'description': 'Proposed '
                                                                                                                                         'billing '
                                                                                                                                         'cadence.'},
                                                                                                    'payment_terms': {'type': 'string',
                                                                                                                      'description': 'Proposed '
                                                                                                                                     'payment '
                                                                                                                                     'terms.'}}}}}},
                'negotiation_plan': {'type': 'array',
                                     'description': 'Proposed negotiation steps.',
                                     'items': {'type': 'object',
                                               'description': 'Negotiation step.',
                                               'properties': {'step': {'type': 'integer',
                                                                       'description': 'One-based '
                                                                                      'step order.',
                                                                       'minimum': 1,
                                                                       'x-unit': 'count'},
                                                              'action': {'type': 'string',
                                                                         'description': 'Proposed '
                                                                                        'action.'},
                                                              'owner': {'type': 'string',
                                                                        'description': 'Role '
                                                                                       'responsible '
                                                                                       'for the '
                                                                                       'action.'},
                                                              'talk_track': {'type': 'string',
                                                                             'description': 'Suggested '
                                                                                            'negotiation '
                                                                                            'language.'}}}},
                'walk_away_guidance': {'type': 'object',
                                       'description': 'Boundaries grounded in validated financial '
                                                      'and policy outputs.',
                                       'properties': {'position': {'type': 'string',
                                                                   'description': 'Recommended '
                                                                                  'boundary or '
                                                                                  'escalation '
                                                                                  'position.'},
                                                      'deterministic_basis': {'type': 'string',
                                                                              'description': 'Supporting '
                                                                                             'financial '
                                                                                             'or '
                                                                                             'policy '
                                                                                             'result.'}}},
                'commercial_risks': {'type': 'array',
                                     'description': 'Risks and proposed mitigations.',
                                     'items': {'type': 'object',
                                               'description': 'Commercial risk.',
                                               'properties': {'category': {'type': 'string',
                                                                           'description': 'Risk '
                                                                                          'category.'},
                                                              'risk': {'type': 'string',
                                                                       'description': 'Risk '
                                                                                      'description.'},
                                                              'mitigation': {'type': 'string',
                                                                             'description': 'Proposed '
                                                                                            'mitigation.'}}}},
                'confidence': {'type': 'object',
                               'description': 'Confidence assessment and explanation.',
                               'properties': {'level': {'type': 'string',
                                                        'description': 'Confidence band.',
                                                        'enum': ['LOW', 'MEDIUM', 'HIGH']},
                                              'rationale': {'type': 'string',
                                                            'description': 'Reason for the '
                                                                           'confidence assessment '
                                                                           'and any missing '
                                                                           'evidence.'}}},
                'evidence': {'type': 'array',
                             'description': 'Supporting source references.',
                             'items': {'type': 'object',
                                       'description': 'Reference to source material; no source '
                                                      'record is included in this repository.',
                                       'properties': {'source_type': {'type': 'string',
                                                                      'description': 'Category of '
                                                                                     'source '
                                                                                     'supporting '
                                                                                     'the '
                                                                                     'statement.'},
                                                      'label': {'type': 'string',
                                                                'description': 'Human-readable '
                                                                               'source label.'},
                                                      'detail': {'type': 'string',
                                                                 'description': 'Supporting '
                                                                                'observation or '
                                                                                'reference '
                                                                                'supplied at '
                                                                                'runtime.'}}}}},
 'required': ['executive_summary',
              'recommended_position',
              'proposed_structure',
              'customer_ask_comparison',
              'give_get_options',
              'negotiation_plan',
              'walk_away_guidance',
              'commercial_risks',
              'confidence',
              'evidence']},
    }
    payload = {"instructions": instructions, "grounded_context": context}
    if repair_feedback:
        payload["repair_feedback"] = repair_feedback
    return json.dumps(payload, indent=2, default=_json_default)


def _llm_context(context: dict) -> dict:
    """Keep the LLM prompt focused; deterministic engines retain full detail."""
    def pick(source: dict, fields: tuple[str, ...]) -> dict:
        return {field: source.get(field) for field in fields if field in source}

    def financial_summary(financial: dict) -> dict:
        return {
            "scenario": financial.get("scenario"),
            "baseline": pick(financial.get("baseline", {}), (
                "discount_pct", "net_contract_value", "contract_term_months", "tcv",
                "npv", "roi_3yr_pct", "payback_months", "gross_margin_pct",
            )),
            "commitment": pick(financial.get("commitment", {}), (
                "commitment_amount", "expected_consumption", "coverage_ratio",
                "expected_shortfall", "annual_targets", "expected_annual_consumption",
                "target_schedule_basis", "expected_consumption_basis",
            )),
            "thresholds": financial.get("thresholds"),
            "flags": financial.get("flags", []),
        }

    def validation_summary(validation: dict) -> dict:
        return financial_summary(validation)

    def policy_summary(policy: dict) -> dict:
        return {
            "scenario": policy.get("scenario"),
            "overall_status": policy.get("overall_status"),
            "summary": policy.get("summary"),
            "policy_checks": [
                {
                    "category": check.get("category"),
                    "status": check.get("status"),
                    "severity": check.get("severity"),
                    "message": check.get("message"),
                }
                for check in policy.get("policy_checks", [])
            ],
            "required_approvals": policy.get("required_approvals", []),
            "exceptions": policy.get("exceptions", []),
        }

    ask = context.get("customer_ask", {})
    working = context.get("working_scenario", {})
    financial = context.get("financial_analysis", {})
    comparable = context.get("comparable_deal_intelligence", {})
    comparable_summary = {
        "current_deal_id": comparable.get("current_deal_id"),
        "benchmark_status": comparable.get("benchmark_status"),
        "evidence_basis": comparable.get("evidence_basis"),
        "comparable_summary": comparable.get("comparable_summary", {}),
        "current_position": comparable.get("current_position", {}),
        "common_concessions": comparable.get("common_concessions", [])[:5],
        "top_comparables": [
            pick(item, ("historical_deal_id", "similarity_score", "outcome", "final_discount_pct", "tcv", "contract_term_months", "commitment_amount"))
            for item in comparable.get("top_comparables", [])[:3]
        ],
    }
    return {
        "deal_id": context.get("deal_id"),
        "deal": context.get("deal", {}),
        "customer_ask": {
            "structured": ask.get("structured", {}),
            "raw_asks": ask.get("raw_asks", []),
            "financial_validation": validation_summary(ask.get("financial_validation", {})),
            "policy_validation": policy_summary(ask.get("policy_validation", {})),
        },
        "working_scenario": {
            "structured": working.get("structured", {}),
            "financial_validation": validation_summary(working.get("financial_validation", {})),
            "policy_validation": policy_summary(working.get("policy_validation", {})),
        },
        "financial_analysis": {
            "currency": financial.get("currency"),
            "flags": financial.get("flags", []),
        },
        "comparable_deal_intelligence": comparable_summary,
        "competitive_context": context.get("competitive_context", {}),
        "strategic_context": context.get("strategic_context", {}),
        "notes": context.get("notes", []),
        "data_constraints": context.get("data_constraints", {}),
    }


def _call_together(prompt: str) -> str:
    api_key = os.environ.get("TOGETHER_API_KEY", "")
    if not api_key:
        raise CommercialStrategyAgentError("LLM_UNAVAILABLE", "TOGETHER_API_KEY is not configured.")
    response = request_completion(
        TOGETHER_CHAT_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": TOGETHER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 8000,
            "response_format": {"type": "json_object"},
        },
        timeout=(5, 150),
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise CommercialStrategyAgentError(
            "TOGETHER_REQUEST_FAILED",
            f"Together request failed ({response.status_code}): {response.text[:500]}",
        ) from exc
    _ACTIVE_LLM_PROVIDER.set("together")
    return response.json()["choices"][0]["message"]["content"].strip()


def _text_matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    text = str(value or "").lower()
    return any(pattern in text for pattern in patterns)


def _normalize_give_get_option(option: dict) -> dict:
    """Keep give/get options in seller perspective before UI rendering."""
    give = str(option.get("give") or "")
    get = str(option.get("get") or "")
    if (
        _text_matches_any(give, SELLER_GET_PATTERNS)
        and _text_matches_any(get, SELLER_GIVE_PATTERNS)
    ):
        return {**option, "give": get, "get": give, "perspective_normalized": True}
    return option


def _validated_options(deal_id: str, base_structure: dict, options: list[dict]) -> list[dict]:
    out = []
    for option in (options or [])[:3]:
        option = _normalize_give_get_option(option)
        structure = {**base_structure, **(option.get("proposed_structure") or {})}
        validation = validate_commercial_strategy(deal_id, structure)
        out.append({**option, "proposed_structure": _validation_payload(structure), "validation": validation})
    return out


def _normalize_cached_give_get_options(options: list[dict]) -> list[dict]:
    return [_normalize_give_get_option(option) for option in options or []]


def _is_repair_needed(validation: dict) -> bool:
    return not validation.get("validated")


def _complete_proposed_structure(parsed: dict, context: dict) -> dict:
    """Fill omitted LLM terms from the already validated working scenario."""
    proposed = parsed.get("proposed_structure") or {}
    working = (context.get("working_scenario") or {}).get("structured") or {}
    return {
        **{key: value for key, value in working.items() if key in SUPPORTED_STRUCTURE_FIELDS},
        **{key: value for key, value in proposed.items() if key in SUPPORTED_STRUCTURE_FIELDS},
    }


def _json_repair_prompt(raw: str, error_message: str) -> str:
    return json.dumps({
        "instruction": "Rewrite the assistant output as exactly one valid JSON object. Return JSON only. Do not add markdown or commentary.",
        "required_shape": _prompt({}, None),
        "parse_error": error_message,
        "assistant_output": (raw or "")[:12000],
    }, indent=2)


def generate_commercial_strategy(
    deal_id: str,
    working_scenario: dict | None = None,
    llm_client: Callable[[str], str] | None = None,
    use_cache: bool = True,
) -> dict:
    context = build_commercial_strategy_context(deal_id, working_scenario)
    cache_key = _stable_hash({"model": TOGETHER_MODEL, "context": context, "working_scenario": working_scenario})
    cache = _load_cache() if use_cache else {}
    if use_cache and cache_key in cache:
        cached = cache[cache_key]
        return {
            **cached,
            "give_get_options": _normalize_cached_give_get_options(cached.get("give_get_options", [])),
            "llm_provider": cached.get("llm_provider", "legacy_cache"),
            "cache": {"hit": True, "key": cache_key},
        }

    caller = llm_client or _call_together
    repair_attempted = False
    try:
        raw = caller(_prompt(_llm_context(context)))
        try:
            parsed = parse_strategy_response(raw)
            _validate_shape(parsed)
        except CommercialStrategyAgentError as parse_exc:
            repair_attempted = True
            raw = caller(_json_repair_prompt(raw, parse_exc.message))
            parsed = parse_strategy_response(raw)
            _validate_shape(parsed)
        proposed = _complete_proposed_structure(parsed, context)
        validation = validate_commercial_strategy(deal_id, proposed)
        if _is_repair_needed(validation):
            repair_attempted = True
            raw = caller(_prompt(_llm_context(context), {"validation": validation, "instruction": "Repair the proposal so it is not blocked and avoids negative NPV. Return the same JSON shape."}))
            parsed = parse_strategy_response(raw)
            _validate_shape(parsed)
            proposed = _complete_proposed_structure(parsed, context)
            validation = validate_commercial_strategy(deal_id, proposed)
    except CommercialStrategyAgentError as exc:
        return {
            "deal_id": deal_id,
            "status": "unavailable" if exc.code == "LLM_UNAVAILABLE" else "error",
            "error": exc.code,
            "message": exc.message,
            "generated_at": _utc_now(),
        }
    except Exception as exc:
        return {
            "deal_id": deal_id,
            "status": "unavailable",
            "error": "LLM_CALL_FAILED",
            "message": str(exc),
            "generated_at": _utc_now(),
        }

    proposed_structure = _validation_payload(proposed)
    result = {
        "deal_id": deal_id,
        "status": "success" if validation.get("validated") else "validation_failed",
        "generated_at": _utc_now(),
        "model": (
            TOGETHER_MODEL if _ACTIVE_LLM_PROVIDER.get() == "together"
            else "unknown"
        ),
        "llm_provider": _ACTIVE_LLM_PROVIDER.get(),
        "strategy_source": "llm_proposal_deterministic_validation",
        "executive_summary": parsed.get("executive_summary", ""),
        "recommended_position": parsed.get("recommended_position", {}),
        "proposed_structure": proposed_structure,
        "validated_financials": validation.get("financial_validation"),
        "validated_policy": validation.get("policy_validation"),
        "validation": validation,
        "customer_ask_comparison": parsed.get("customer_ask_comparison", []),
        "give_get_options": _validated_options(deal_id, proposed_structure, parsed.get("give_get_options", [])),
        "negotiation_plan": parsed.get("negotiation_plan", []),
        "walk_away_guidance": parsed.get("walk_away_guidance", {}),
        "commercial_risks": parsed.get("commercial_risks", []),
        "confidence": parsed.get("confidence", {"level": "LOW", "rationale": "LLM did not provide confidence rationale."}),
        "evidence": _evidence(context, parsed.get("evidence", [])),
        "comparable_evidence": context.get("comparable_deal_intelligence"),
        "data_constraints": context["data_constraints"],
        "required_approvals": (validation.get("policy_validation") or {}).get("required_approvals", []),
        "repair_attempted": repair_attempted,
        "cache": {"hit": False, "key": cache_key},
    }
    if use_cache and result["status"] == "success":
        cache[cache_key] = result
        _write_cache(cache)
    return result


def write_strategy_schema(path: Path = STRATEGY_SCHEMA_PATH) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ValidatedCommercialStrategy",
        "type": "object",
        "required": ["deal_id", "status", "recommended_position", "proposed_structure", "validated_financials", "validated_policy", "evidence"],
        "properties": {
            "status": {"enum": ["success", "validation_failed", "unavailable", "error"]},
            "recommended_position": {"type": "object"},
            "proposed_structure": {"type": "object", "additionalProperties": True},
            "validated_financials": {"type": ["object", "null"]},
            "validated_policy": {"type": ["object", "null"]},
            "give_get_options": {"type": "array"},
            "commercial_risks": {"type": "array"},
            "evidence": {"type": "array"},
        },
    }
    path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
