"""Phase 6 Comparable Deal Intelligence.

Deterministic retrieval and benchmarking over the historical comparable-deal
dataset. Historical deals are evidence for the AI Commercial Strategy Agent;
they are never policy authority and never set current-deal financial metrics.

Architecture: Stage A deterministic candidate filtering, Stage B weighted
commercial similarity ranking, then deterministic benchmark statistics. No
LLM is involved anywhere in this module.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from statistics import mean

try:
    from tools.commercial_data import get_deal_input, get_historical_deals
except ModuleNotFoundError:
    from commercial_data import get_deal_input, get_historical_deals


BASE_DIR = Path(__file__).resolve().parent.parent
COMPARABLE_CONFIG_PATH = BASE_DIR / "knowledge_base" / "commercial_strategy" / "comparable_deal_config.json"

INSUFFICIENT_MESSAGE = "Not enough comparable data"
INSUFFICIENT_SAMPLE_MESSAGE = "Insufficient comparable-deal sample for reliable benchmarking."


class ComparableDealError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def get_comparable_deal_config() -> dict:
    with COMPARABLE_CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


# ── current-deal profile ──────────────────────────────────────────────────────

def _current_deal_profile(deal_id: str) -> dict:
    deal = get_deal_input(deal_id)
    if not deal:
        raise ComparableDealError("MISSING_DEAL_INPUT", f"No DealInput found for {deal_id}.")
    account = deal.get("account", {})
    commercial = deal.get("commercial", {})
    commitment = deal.get("consumption_commitment", {})
    return {
        "deal_id": deal_id,
        "industry": account.get("industry"),
        "segment": account.get("segment"),
        "region": account.get("region"),
        "strategic_account": account.get("strategic_account"),
        "products": [p.get("product") for p in deal.get("products", []) if p.get("product")],
        "tcv": _to_float(commercial.get("tcv") or commercial.get("proposed_net_value")),
        "list_value": _to_float(commercial.get("list_value")),
        "contract_term_months": _to_float(commercial.get("contract_term_months")),
        "commitment_amount": _to_float(commitment.get("commitment_amount")),
        "competitor": (deal.get("competitive_context") or {}).get("primary_competitor"),
        "requested_discount_pct": _to_float(commercial.get("requested_discount_pct")),
        "proposed_discount_pct": _to_float(commercial.get("proposed_discount_pct")),
    }


def _to_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── similarity components (each returns score in [0, 1] plus explanation) ─────

def _norm(value) -> str:
    return str(value or "").strip().lower().replace(" ", "-")


def _industry_similarity(current: str | None, historical: str | None, config: dict) -> tuple[float, str]:
    if not current or not historical:
        return 0.0, "Industry unknown on one side"
    if current == historical:
        return 1.0, f"Same industry: {current}"
    for group in config.get("industry_affinity_groups", {}).values():
        if current in group and historical in group:
            return float(config.get("industry_affinity_score", 0.5)), f"Related industry: {historical} (vs {current})"
    return 0.0, f"Different industry: {historical}"


def _segment_similarity(current: str | None, historical: str | None, config: dict) -> tuple[float, str]:
    cur, hist = _norm(current), _norm(historical)
    if not cur or not hist:
        return 0.0, "Segment unknown on one side"
    if cur == hist:
        return 1.0, f"Same {historical} segment"
    for pair in config.get("segment_adjacency", []):
        if cur in pair and hist in pair:
            return float(config.get("segment_adjacency_score", 0.4)), f"Adjacent segment: {historical} (vs {current})"
    return 0.0, f"Different segment: {historical}"


def _product_families(products: list[str], config: dict) -> set[str]:
    lookup = {}
    for family, names in config.get("product_families", {}).items():
        for name in names:
            lookup[name.lower()] = family
    return {lookup[p.lower()] for p in products or [] if p and p.lower() in lookup}


def _product_similarity(current_products: list[str], historical_products: list[str], config: dict) -> tuple[float, str]:
    cur_exact = {p.lower() for p in current_products or []}
    hist_exact = {p.lower() for p in historical_products or []}
    if not cur_exact or not hist_exact:
        return 0.0, "No product data on one side"
    cur_fam = _product_families(current_products, config)
    hist_fam = _product_families(historical_products, config)

    def blended(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        if not inter:
            return 0.0
        weights = config.get("product_similarity", {})
        jaccard = inter / len(a | b)
        containment = inter / min(len(a), len(b))
        return float(weights.get("jaccard_weight", 0.5)) * jaccard + float(weights.get("containment_weight", 0.5)) * containment

    exact_score = blended(cur_exact, hist_exact)
    family_score = blended(cur_fam, hist_fam)
    score = max(exact_score, family_score)
    if exact_score >= family_score and exact_score > 0:
        return exact_score, f"Product overlap ({round(exact_score * 100)}%)"
    if family_score > 0:
        shared = sorted(cur_fam & hist_fam)
        return family_score, f"Product-family overlap: {', '.join(shared)} ({round(family_score * 100)}%)"
    return 0.0, "No product overlap"


def _size_similarity(current: float | None, historical: float | None, config: dict, label: str) -> tuple[float, str]:
    if not current or not historical or current <= 0 or historical <= 0:
        return 0.0, f"{label} unavailable on one side"
    span = float(config.get("numeric_similarity", {}).get("size_log_decade_span", 1.5))
    distance = abs(math.log10(current / historical))
    score = max(0.0, 1.0 - distance / span)
    pct_diff = abs(current - historical) / current * 100
    if pct_diff <= 25:
        return score, f"{label} within {round(pct_diff)}%"
    return score, f"{label} similarity {round(score * 100)}%"


def _term_similarity(current: float | None, historical: float | None) -> tuple[float, str]:
    if not current or not historical:
        return 0.0, "Contract term unavailable on one side"
    if current == historical:
        return 1.0, f"Same {int(historical)}-month term"
    score = min(current, historical) / max(current, historical)
    return score, f"{int(historical)}-month term (vs {int(current)})"


def _competitor_similarity(current: str | None, historical: str | None, config: dict) -> tuple[float, str]:
    scores = config.get("competitor_scores", {})
    missing = {"", "none", "unknown"}
    cur_missing = _norm(current) in missing
    hist_missing = _norm(historical) in missing
    if cur_missing and hist_missing:
        return float(scores.get("both_none", 1.0)), "Both non-competitive deals"
    if cur_missing or hist_missing:
        return float(scores.get("one_missing", 0.15)), "Competitive context differs (one side has no known competitor)"
    if _norm(current) == _norm(historical):
        return float(scores.get("exact", 1.0)), f"Same primary competitor: {historical}"
    for group in config.get("competitor_groups", {}).values():
        normalized = {_norm(g) for g in group}
        if _norm(current) in normalized and _norm(historical) in normalized:
            return float(scores.get("same_group", 0.6)), f"Related competitor: {historical} (vs {current})"
    return float(scores.get("different_competitor", 0.25)), f"Different competitor: {historical}"


def _region_similarity(current: str | None, historical: str | None) -> tuple[float, str]:
    if not current or not historical:
        return 0.0, "Region unknown on one side"
    if _norm(current) == _norm(historical):
        return 1.0, f"Same region: {historical}"
    return 0.0, f"Different region: {historical}"


def _strategic_similarity(current, historical) -> tuple[float, str]:
    if current is None or historical is None:
        return 0.0, "Strategic-account flag unknown on one side"
    if bool(current) == bool(historical):
        return 1.0, "Both strategic accounts" if current else "Both non-strategic accounts"
    return 0.0, "Strategic-account status differs"


def _recency_factor(close_date: str | None, config: dict, today: date | None = None) -> tuple[float, str]:
    recency = config.get("recency", {})
    if not close_date:
        return float(recency.get("missing_close_date_factor", 0.5)), "Close date missing; neutral recency factor applied"
    try:
        closed = datetime.strptime(str(close_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return float(recency.get("missing_close_date_factor", 0.5)), "Close date unparseable; neutral recency factor applied"
    today = today or date.today()
    age_months = max(0.0, (today - closed).days / 30.44)
    half_life = float(recency.get("half_life_months", 24)) or 24.0
    factor = 0.5 ** (age_months / half_life)
    return factor, f"Closed {round(age_months)} months ago"


# ── scoring ───────────────────────────────────────────────────────────────────

def _score_components(profile: dict, hist: dict, config: dict) -> list[dict]:
    weights = config["similarity_weights"]
    raw = [
        ("industry", _industry_similarity(profile["industry"], hist.get("industry"), config),
         profile["industry"], hist.get("industry")),
        ("segment", _segment_similarity(profile["segment"], hist.get("account_segment"), config),
         profile["segment"], hist.get("account_segment")),
        ("product_overlap", _product_similarity(profile["products"], hist.get("products"), config),
         profile["products"], hist.get("products")),
        ("deal_size", _size_similarity(profile["tcv"], _to_float(hist.get("tcv")), config, "TCV"),
         profile["tcv"], hist.get("tcv")),
        ("contract_term", _term_similarity(profile["contract_term_months"], _to_float(hist.get("contract_term_months"))),
         profile["contract_term_months"], hist.get("contract_term_months")),
        ("commitment", _size_similarity(profile["commitment_amount"], _to_float(hist.get("commitment_amount")), config, "Commitment"),
         profile["commitment_amount"], hist.get("commitment_amount")),
        ("competitor", _competitor_similarity(profile["competitor"], hist.get("competitor"), config),
         profile["competitor"], hist.get("competitor")),
        ("region", _region_similarity(profile["region"], hist.get("region")),
         profile["region"], hist.get("region")),
        ("strategic_account", _strategic_similarity(profile["strategic_account"], hist.get("strategic_account")),
         profile["strategic_account"], hist.get("strategic_account")),
    ]
    components = []
    for name, (score, explanation), current_value, historical_value in raw:
        weight = float(weights.get(name, 0.0))
        components.append({
            "component": name,
            "similarity": round(score, 4),
            "weight": weight,
            "weighted_score": round(score * weight, 4),
            "current_value": current_value,
            "historical_value": historical_value,
            "explanation": explanation,
        })
    return components


def _apply_recency(base_score: float, hist: dict, config: dict) -> tuple[float, dict]:
    recency_weight = float(config.get("recency", {}).get("weight", 0.05))
    factor, note = _recency_factor(hist.get("deal_close_date"), config)
    final = base_score * (1.0 - recency_weight) + factor * recency_weight
    return final, {"factor": round(factor, 4), "weight": recency_weight, "note": note}


def _filter_decision(profile: dict, hist: dict, components: list[dict], config: dict) -> dict:
    filters = config.get("candidate_filters", {})
    reasons = []
    max_decades = float(filters.get("max_size_ratio_decades", 2.0))
    cur_tcv, hist_tcv = profile.get("tcv"), _to_float(hist.get("tcv"))
    if cur_tcv and hist_tcv and cur_tcv > 0 and hist_tcv > 0:
        if abs(math.log10(cur_tcv / hist_tcv)) > max_decades:
            reasons.append(f"Deal size differs by more than {max_decades} orders of magnitude")
    if filters.get("require_commercial_anchor", True):
        by_name = {c["component"]: c["similarity"] for c in components}
        if by_name.get("industry", 0) <= 0 and by_name.get("segment", 0) <= 0 and by_name.get("product_overlap", 0) <= 0:
            reasons.append("No industry, segment, or product-family commercial anchor")
    return {"passed": not reasons, "reasons": reasons}


def _match_reasons(components: list[dict]) -> list[str]:
    strong = [c for c in components if c["similarity"] >= 0.5 and c["weight"] > 0]
    strong.sort(key=lambda c: c["weighted_score"], reverse=True)
    return [c["explanation"] for c in strong[:6]]


def _matches_user_filters(hist: dict, filters: dict | None) -> bool:
    if not filters:
        return True
    checks = {
        "industry": lambda h, v: _norm(h.get("industry")) == _norm(v),
        "region": lambda h, v: _norm(h.get("region")) == _norm(v),
        "segment": lambda h, v: _norm(h.get("account_segment")) == _norm(v),
        "outcome": lambda h, v: _norm(h.get("outcome")) == _norm(v),
        "competitor": lambda h, v: _norm(h.get("competitor")) == _norm(v),
        "min_tcv": lambda h, v: (_to_float(h.get("tcv")) or 0) >= float(v),
        "max_tcv": lambda h, v: (_to_float(h.get("tcv")) or 0) <= float(v),
    }
    for key, value in filters.items():
        if value in (None, "") or key not in checks:
            continue
        if not checks[key](hist, value):
            return False
    return True


def _scored_candidates(deal_id: str, filters: dict | None = None) -> tuple[dict, list[dict], list[dict]]:
    """Score every historical deal. Returns (profile, ranked_passing, excluded)."""
    config = get_comparable_deal_config()
    profile = _current_deal_profile(deal_id)
    min_score = float(config.get("candidate_filters", {}).get("min_similarity_score", 0.0))
    passing, excluded = [], []
    for hist in get_historical_deals():
        if not _matches_user_filters(hist, filters):
            excluded.append({"historical_deal_id": hist.get("deal_id"), "reasons": ["Excluded by requested search filters"]})
            continue
        components = _score_components(profile, hist, config)
        decision = _filter_decision(profile, hist, components, config)
        if not decision["passed"]:
            excluded.append({"historical_deal_id": hist.get("deal_id"), "reasons": decision["reasons"]})
            continue
        base_score = sum(c["weighted_score"] for c in components)
        final_score, recency = _apply_recency(base_score, hist, config)
        if final_score < min_score:
            excluded.append({
                "historical_deal_id": hist.get("deal_id"),
                "reasons": [f"Similarity {round(final_score, 2)} below minimum {min_score}"],
            })
            continue
        passing.append({
            "historical": hist,
            "components": components,
            "base_score": round(base_score, 4),
            "recency": recency,
            "similarity_score": round(final_score, 4),
        })
    passing.sort(key=lambda c: (c["similarity_score"], c["historical"].get("deal_id", "")), reverse=True)
    return profile, passing, excluded


def _comparable_row(candidate: dict) -> dict:
    hist = candidate["historical"]
    return {
        "historical_deal_id": hist.get("deal_id"),
        "similarity_score": candidate["similarity_score"],
        "match_reasons": _match_reasons(candidate["components"]),
        "customer": (hist.get("source_reference") or {}).get("company"),
        "industry": hist.get("industry"),
        "region": hist.get("region"),
        "segment": hist.get("account_segment"),
        "products": hist.get("products"),
        "product_category": (hist.get("source_reference") or {}).get("product_category"),
        "list_value": hist.get("list_value"),
        "tcv": hist.get("tcv"),
        "final_discount_pct": hist.get("final_discount_pct"),
        "contract_term_months": hist.get("contract_term_months"),
        "commitment_amount": hist.get("commitment_amount"),
        "competitor": hist.get("competitor"),
        "strategic_account": hist.get("strategic_account"),
        "outcome": hist.get("outcome"),
        "approval_level": hist.get("approval_level"),
        "approval_functions": hist.get("approval_functions") or [],
        "major_concessions": hist.get("major_negotiated_concessions") or [],
        "deal_close_date": hist.get("deal_close_date") or None,
        "negotiation_summary": hist.get("negotiation_summary"),
    }


# ── public retrieval API ──────────────────────────────────────────────────────

def find_comparable_deals(deal_id: str, limit: int = 10, filters: dict | None = None) -> dict:
    config = get_comparable_deal_config()
    profile, passing, excluded = _scored_candidates(deal_id, filters)
    top = passing[:limit]
    comparables = [_comparable_row(c) for c in top]
    return {
        "current_deal_id": deal_id,
        "current_deal_profile": profile,
        "comparables": comparables,
        "comparable_count": len(comparables),
        "candidates_considered": len(passing) + len(excluded),
        "candidates_excluded": len(excluded),
        "excluded": excluded,
        "filters_applied": filters or {},
        "retrieval_confidence": _retrieval_confidence(top, config),
        "retrieval_method": "deterministic_weighted_similarity",
        "config_version": config.get("config_version"),
    }


def explain_comparable_match(deal_id: str, historical_deal_id: str) -> dict:
    trace = get_comparable_deal_trace(deal_id, historical_deal_id)
    strong = [c for c in trace["similarity_components"] if c["similarity"] >= 0.5]
    weak = [c for c in trace["similarity_components"] if c["similarity"] < 0.5]
    return {
        "current_deal_id": deal_id,
        "historical_deal_id": historical_deal_id,
        "similarity_score": trace["final_score"],
        "similarity_pct": round(trace["final_score"] * 100),
        "match_reasons": _match_reasons(trace["similarity_components"]),
        "strong_components": [c["component"] for c in strong],
        "weak_components": [c["component"] for c in weak],
        "filter_decision": trace["filter_decision"],
        "trace": trace,
    }


def get_comparable_deal_trace(deal_id: str, historical_deal_id: str) -> dict:
    config = get_comparable_deal_config()
    profile = _current_deal_profile(deal_id)
    hist = next((h for h in get_historical_deals() if h.get("deal_id") == historical_deal_id), None)
    if not hist:
        raise ComparableDealError("MISSING_HISTORICAL_DEAL", f"No historical deal found for {historical_deal_id}.")
    components = _score_components(profile, hist, config)
    decision = _filter_decision(profile, hist, components, config)
    base_score = sum(c["weighted_score"] for c in components)
    final_score, recency = _apply_recency(base_score, hist, config)
    return {
        "current_deal_id": deal_id,
        "historical_deal_id": historical_deal_id,
        "similarity_components": components,
        "weights": config["similarity_weights"],
        "base_score": round(base_score, 4),
        "recency_adjustment": recency,
        "final_score": round(final_score, 4),
        "filter_decision": decision,
        "score_ledger": [
            f"{c['component']}: {round(c['weighted_score'] * 100, 1)}/{round(c['weight'] * 100)}"
            for c in components
        ] + [f"total (pre-recency): {round(base_score * 100, 1)}/100"],
        "deterministic": True,
    }


# ── deterministic statistics helpers ──────────────────────────────────────────

def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default)."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("empty values")
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _numeric_values(rows: list[dict], field: str) -> list[float]:
    return [v for v in (_to_float(r.get(field)) for r in rows) if v is not None]


def _stat_block(values: list[float], minimum: int, round_digits: int = 1) -> dict:
    if len(values) < minimum:
        return {"status": "INSUFFICIENT_DATA", "message": INSUFFICIENT_MESSAGE, "sample_size": len(values)}
    r = lambda v: round(v, round_digits)
    return {
        "status": "OK",
        "sample_size": len(values),
        "mean": r(mean(values)),
        "median": r(_percentile(values, 50)),
        "p25": r(_percentile(values, 25)),
        "p75": r(_percentile(values, 75)),
        "min": r(min(values)),
        "max": r(max(values)),
    }


def _frequency(items: list[str], total: int) -> list[dict]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return [
        {"name": name, "count": count, "share_pct": round(count / total * 100) if total else 0}
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def discount_position(ask_discount_pct: float | None, discount_stats: dict) -> dict:
    """Deterministic position of the customer ask against comparable discounts."""
    if ask_discount_pct is None:
        return {"status": "INSUFFICIENT_DATA", "message": "No customer-ask discount on the current deal"}
    if discount_stats.get("status") != "OK":
        return {"status": "INSUFFICIENT_DATA", "message": INSUFFICIENT_MESSAGE}
    if ask_discount_pct < discount_stats["min"]:
        label = "BELOW_RANGE"
    elif ask_discount_pct <= discount_stats["median"]:
        label = "WITHIN_RANGE"
    elif ask_discount_pct <= discount_stats["p75"]:
        label = "ABOVE_MEDIAN"
    elif ask_discount_pct <= discount_stats["max"]:
        label = "ABOVE_P75"
    else:
        label = "ABOVE_RANGE"
    return {
        "status": "OK",
        "position": label,
        "customer_ask_discount_pct": ask_discount_pct,
        "discount_vs_median_pts": round(ask_discount_pct - discount_stats["median"], 1),
        "discount_vs_p75_pts": round(ask_discount_pct - discount_stats["p75"], 1),
        "discount_vs_max_pts": round(ask_discount_pct - discount_stats["max"], 1),
    }


def _segment_benchmarks(rows: list[dict], minimum: int) -> dict:
    return {
        "count": len(rows),
        "discount": _stat_block(_numeric_values(rows, "final_discount_pct"), minimum),
        "median_tcv": _median_or_insufficient(rows, "tcv", minimum),
        "median_commitment": _median_or_insufficient(rows, "commitment_amount", minimum),
        "median_term_months": _median_or_insufficient(rows, "contract_term_months", minimum, round_digits=0),
    }


def _median_or_insufficient(rows: list[dict], field: str, minimum: int, round_digits: int = 0) -> dict:
    values = _numeric_values(rows, field)
    if len(values) < minimum:
        return {"status": "INSUFFICIENT_DATA", "message": INSUFFICIENT_MESSAGE, "sample_size": len(values)}
    return {"status": "OK", "sample_size": len(values), "value": round(_percentile(values, 50), round_digits)}


def get_comparable_deal_benchmarks(deal_id: str, comparable_deals: list[dict] | None = None, limit: int = 10) -> dict:
    config = get_comparable_deal_config()
    minimum = int(config.get("minimum_benchmark_sample", 5))
    seg_minimum = int(config.get("minimum_segment_sample", 3))
    profile = _current_deal_profile(deal_id)

    if comparable_deals is None:
        retrieval = find_comparable_deals(deal_id, limit=limit)
        comparables = retrieval["comparables"]
        confidence = retrieval["retrieval_confidence"]
    else:
        comparables = comparable_deals
        confidence = None

    result: dict = {
        "current_deal_id": deal_id,
        "comparable_count": len(comparables),
        "minimum_benchmark_sample": minimum,
        "benchmark_status": "OK" if len(comparables) >= minimum else "INSUFFICIENT_SAMPLE",
        "data_quality": validate_historical_records(comparables),
    }
    if confidence is not None:
        result["retrieval_confidence"] = confidence
    if len(comparables) < minimum:
        result["message"] = INSUFFICIENT_SAMPLE_MESSAGE
        result["comparables_shown_anyway"] = [
            {"historical_deal_id": c.get("historical_deal_id"), "similarity_score": c.get("similarity_score")}
            for c in comparables
        ]
        return result

    won = [c for c in comparables if c.get("outcome") == "WON"]
    lost = [c for c in comparables if c.get("outcome") == "LOST"]
    decided = len(won) + len(lost)

    discount_stats = _stat_block(_numeric_values(comparables, "final_discount_pct"), minimum)
    won_discounts = _numeric_values(won, "final_discount_pct")
    max_won_discount = round(max(won_discounts), 1) if won_discounts else None

    buckets = sorted(int(b) for b in config.get("term_distribution_buckets", [12, 24, 36]))

    def term_buckets(terms: list[float]) -> dict:
        distribution = {}
        for i, bucket in enumerate(buckets):
            is_last = i == len(buckets) - 1
            label = f"{bucket}+" if is_last else str(bucket)
            upper = None if is_last else buckets[i + 1]
            distribution[label] = sum(1 for t in terms if t >= bucket and (upper is None or t < upper))
        return distribution

    term_distribution = term_buckets(_numeric_values(comparables, "contract_term_months"))
    won_term_distribution = term_buckets(_numeric_values(won, "contract_term_months"))

    commitment_values = _numeric_values(comparables, "commitment_amount")
    ratio_rows = [
        (_to_float(c.get("commitment_amount")), _to_float(c.get("tcv")), _to_float(c.get("list_value")))
        for c in comparables
    ]
    tcv_ratios = [cm / tcv for cm, tcv, _ in ratio_rows if cm and tcv]
    list_ratios = [cm / lv for cm, _, lv in ratio_rows if cm and lv]

    current_commitment = profile.get("commitment_amount")
    current_tcv = profile.get("tcv")
    current_list = profile.get("list_value")

    concessions = [c for row in comparables for c in (row.get("major_concessions") or [])]
    approvals = [row.get("approval_level") for row in comparables if row.get("approval_level")]
    approval_functions = [f for row in comparables for f in (row.get("approval_functions") or [])]
    deals_with_function_data = sum(1 for row in comparables if row.get("approval_functions"))
    ask = profile.get("requested_discount_pct")

    result.update({
        "discount": {
            **discount_stats,
            "max_won_discount_pct": max_won_discount,
            "customer_ask_discount_pct": ask,
            "position": discount_position(ask, discount_stats),
        },
        "deal_size": {
            "median_tcv": _median_or_insufficient(comparables, "tcv", minimum),
            "current_tcv": current_tcv,
        },
        "contract_term": {
            "median_term_months": _median_or_insufficient(comparables, "contract_term_months", minimum, round_digits=0),
            "term_distribution": term_distribution,
            "won_term_distribution": won_term_distribution if len(won) >= seg_minimum else {"status": "INSUFFICIENT_DATA", "message": INSUFFICIENT_MESSAGE},
            "current_term_months": profile.get("contract_term_months"),
        },
        "commitment": {
            "median_commitment": _median_or_insufficient(comparables, "commitment_amount", minimum),
            "median_commitment_to_tcv_ratio_pct": round(_percentile(tcv_ratios, 50) * 100) if len(tcv_ratios) >= minimum else None,
            "median_commitment_to_list_ratio_pct": round(_percentile(list_ratios, 50) * 100) if len(list_ratios) >= minimum else None,
            "current_commitment": current_commitment,
            "current_commitment_to_tcv_ratio_pct": round(current_commitment / current_tcv * 100) if current_commitment and current_tcv else None,
            "current_commitment_to_list_ratio_pct": round(current_commitment / current_list * 100) if current_commitment and current_list else None,
        },
        "outcomes": {
            "won": len(won),
            "lost": len(lost),
            "no_decision": len(comparables) - decided,
            "win_rate_pct": round(len(won) / decided * 100) if decided else None,
            "win_rate_basis": "decided deals only (WON + LOST)",
        },
        "won_vs_lost": {
            "won": _segment_benchmarks(won, seg_minimum),
            "lost": _segment_benchmarks(lost, seg_minimum),
            "note": "Descriptive historical patterns only; correlation is not causation.",
        },
        "approval_patterns": {
            "frequency": _frequency(approvals, len(comparables)),
            "function_frequency": _frequency(approval_functions, deals_with_function_data),
            "deals_with_function_data": deals_with_function_data,
            "note": "Historical evidence only. Current-deal approvals come from the deterministic policy engine.",
        },
        "concessions": {
            "frequency": _frequency(concessions, len(comparables)),
            "deals_with_concession_data": sum(1 for c in comparables if c.get("major_concessions")),
        },
    })
    return result


# ── confidence, data quality, evidence package ────────────────────────────────

def _retrieval_confidence(top_candidates: list[dict], config: dict) -> dict:
    thresholds = config.get("confidence_thresholds", {})
    critical = config.get("critical_benchmark_fields", [])
    count = len(top_candidates)
    if count < int(thresholds.get("insufficient_below_count", 3)):
        return {"level": "INSUFFICIENT_DATA", "comparable_count": count,
                "basis": "Too few comparable deals passed deterministic filtering."}
    avg_similarity = round(mean(c["similarity_score"] for c in top_candidates), 4)
    product_scores = [
        next(comp["similarity"] for comp in c["components"] if comp["component"] == "product_overlap")
        for c in top_candidates
    ]
    avg_product = round(mean(product_scores), 4)
    completeness_values = [
        sum(1 for f in critical if c["historical"].get(f) not in (None, "", [])) / len(critical)
        for c in top_candidates
    ] if critical else [1.0]
    avg_completeness = round(mean(completeness_values), 4)

    def meets(tier: dict) -> bool:
        return (count >= int(tier.get("min_count", 0))
                and avg_similarity >= float(tier.get("min_avg_similarity", 0))
                and avg_product >= float(tier.get("min_avg_product_similarity", 0))
                and avg_completeness >= float(tier.get("min_field_completeness", 0)))

    level = "HIGH" if meets(thresholds.get("high", {})) else "MEDIUM" if meets(thresholds.get("medium", {})) else "LOW"
    return {
        "level": level,
        "comparable_count": count,
        "avg_similarity": avg_similarity,
        "avg_product_similarity": avg_product,
        "avg_field_completeness": avg_completeness,
        "basis": "Deterministic: count, average similarity, product overlap, and field completeness.",
    }


DATA_QUALITY_FIELDS = {
    "final_discount_pct": "MISSING_FINAL_DISCOUNT",
    "tcv": "MISSING_TCV",
    "outcome": "MISSING_OUTCOME",
    "contract_term_months": "MISSING_CONTRACT_TERM",
    "commitment_amount": "MISSING_COMMITMENT",
    "deal_close_date": "MISSING_CLOSE_DATE",
    "approval_functions": "MISSING_APPROVAL_FUNCTIONS",
}


def validate_historical_records(records: list[dict] | None = None) -> dict:
    """Flag records missing benchmarking fields. Incomplete records are kept and
    used opportunistically; flags describe which benchmarks they cannot serve."""
    rows = records if records is not None else get_historical_deals()
    flagged = []
    flag_counts: dict[str, int] = {}
    for row in rows:
        row_id = row.get("historical_deal_id") or row.get("deal_id")
        flags = [flag for field, flag in DATA_QUALITY_FIELDS.items() if row.get(field) in (None, "", [])]
        concessions = row.get("major_concessions", row.get("major_negotiated_concessions"))
        if not concessions:
            flags.append("MISSING_CONCESSIONS")
        for flag in flags:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
        if flags:
            flagged.append({"historical_deal_id": row_id, "flags": flags})
    return {
        "records_checked": len(rows),
        "records_flagged": len(flagged),
        "flag_counts": dict(sorted(flag_counts.items(), key=lambda kv: -kv[1])),
        "flagged_records": flagged,
        "policy": "Incomplete records are retained and used where their fields allow.",
    }


def get_comparable_deal_evidence(deal_id: str, limit: int = 5, benchmark_limit: int = 10) -> dict:
    """Concise deterministic evidence package for the AI strategy agent.

    The agent consumes these precomputed statistics; it must not calculate its
    own benchmarks or treat this evidence as policy authority.
    """
    config = get_comparable_deal_config()
    minimum = int(config.get("minimum_benchmark_sample", 5))
    retrieval = find_comparable_deals(deal_id, limit=benchmark_limit)
    benchmarks = get_comparable_deal_benchmarks(deal_id, comparable_deals=retrieval["comparables"])

    evidence: dict = {
        "current_deal_id": deal_id,
        "retrieval_confidence": retrieval["retrieval_confidence"],
        "minimum_benchmark_sample": minimum,
        "benchmark_status": benchmarks["benchmark_status"],
        "evidence_basis": "Deterministic comparable-deal engine. Historical evidence, not policy.",
    }
    if benchmarks["benchmark_status"] != "OK":
        evidence["message"] = INSUFFICIENT_SAMPLE_MESSAGE
        evidence["comparable_summary"] = {"count": len(retrieval["comparables"])}
        evidence["top_comparables"] = retrieval["comparables"][:limit]
        return evidence

    discount = benchmarks["discount"]
    evidence["comparable_summary"] = {
        "count": benchmarks["comparable_count"],
        "median_discount_pct": discount.get("median"),
        "p25_discount_pct": discount.get("p25"),
        "p75_discount_pct": discount.get("p75"),
        "max_won_discount_pct": discount.get("max_won_discount_pct"),
        "win_rate_pct": benchmarks["outcomes"]["win_rate_pct"],
        "outcome_counts": {k: benchmarks["outcomes"][k] for k in ("won", "lost", "no_decision")},
        "median_term_months": benchmarks["contract_term"]["median_term_months"].get("value"),
        "median_tcv": benchmarks["deal_size"]["median_tcv"].get("value"),
        "median_commitment_amount": benchmarks["commitment"]["median_commitment"].get("value"),
        "median_commitment_to_tcv_ratio_pct": benchmarks["commitment"]["median_commitment_to_tcv_ratio_pct"],
    }
    evidence["current_position"] = discount.get("position")
    evidence["won_vs_lost"] = benchmarks["won_vs_lost"]
    evidence["common_concessions"] = benchmarks["concessions"]["frequency"][:6]
    evidence["approval_patterns"] = benchmarks["approval_patterns"]
    evidence["top_comparables"] = [
        {
            "historical_deal_id": c["historical_deal_id"],
            "similarity_score": c["similarity_score"],
            "match_reasons": c["match_reasons"][:4],
            "outcome": c["outcome"],
            "final_discount_pct": c["final_discount_pct"],
            "tcv": c["tcv"],
            "contract_term_months": c["contract_term_months"],
            "commitment_amount": c["commitment_amount"],
            "competitor": c["competitor"],
            "major_concessions": c["major_concessions"],
        }
        for c in retrieval["comparables"][:limit]
    ]
    return evidence


