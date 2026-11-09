"""
analyze_pipeline(filter_by, focus) — pure-Python MEDDPICC pipeline statistics.

Reads meddpicc_pipeline.json directly. No LLM. No ChromaDB.
Covers all records — not just semantic search chunks.
"""

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

_JSON_PATH = Path(__file__).parent.parent / "knowledge_base" / "meddpicc_pipeline.json"

_ELEMENTS = [
    "metrics", "economic_buyer", "decision_criteria", "decision_process",
    "paper_process", "identify_pain", "champion", "competition",
]
_FLAGS = [
    "missing_economic_buyer", "missing_champion", "unquantified_pain",
    "competitor_unknown", "paper_process_unknown", "deal_overdue", "stalled",
]
_SCORE_NORM = {
    "at_risk": "AT RISK", "at risk": "AT RISK",
    "needs_attention": "NEEDS ATTENTION", "needs attention": "NEEDS ATTENTION",
    "strong": "STRONG",
}


def _load() -> list:
    if not _JSON_PATH.exists():
        raise FileNotFoundError(
            f"meddpicc_pipeline.json not found at {_JSON_PATH}. "
            "Run meddpicc_scorer.py first."
        )
    with open(_JSON_PATH) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("meddpicc_pipeline.json must be a JSON array.")
    return data


def _extract_product(deal_name: str) -> str:
    for sep in [" — ", " — ", " - "]:
        if sep in deal_name:
            return deal_name.split(sep)[0].strip()
    return deal_name.strip()


def _normalize_score(s: str) -> str:
    return _SCORE_NORM.get((s or "").lower().strip(), s or "UNKNOWN")


def _apply_filters(data: list, filter_by: dict) -> list:
    if not filter_by:
        return data
    result = []
    for d in data:
        match = True
        for key, val in filter_by.items():
            val_norm = str(val).strip()
            if key == "stage":
                if d.get("stage", "").upper() != val_norm.upper():
                    match = False
            elif key == "overall_score":
                target = _normalize_score(val_norm)
                if _normalize_score(d.get("overall_score", "")) != target:
                    match = False
            elif key == "assigned_rep":
                if val_norm.lower() not in (d.get("assigned_rep") or "").lower():
                    match = False
            elif key == "product":
                if val_norm.lower() not in _extract_product(d.get("deal_name", "")).lower():
                    match = False
            elif key == "company":
                if val_norm.lower() not in (d.get("company") or "").lower():
                    match = False
        if match:
            result.append(d)
    return result


def _pct(part, total):
    return round(100 * part / total, 1) if total else 0


def _overview(data: list) -> dict:
    total = len(data)
    total_value = sum(d.get("amount") or 0 for d in data)
    avg_size = round(total_value / total, 0) if total else 0
    avg_prob = round(sum(d.get("probability") or 0 for d in data) / total, 1) if total else 0
    return {
        "total_deals": total,
        "total_pipeline_value": round(total_value, 2),
        "average_deal_size": avg_size,
        "average_probability": avg_prob,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _score_distribution(data: list) -> dict:
    buckets = {"STRONG": [], "NEEDS ATTENTION": [], "AT RISK": []}
    for d in data:
        s = _normalize_score(d.get("overall_score", ""))
        if s in buckets:
            buckets[s].append(d.get("deal_name", ""))
    total = len(data)
    out = {}
    for score, deals in buckets.items():
        key = score.lower().replace(" ", "_")
        total_val = sum(
            d.get("amount") or 0 for d in data
            if _normalize_score(d.get("overall_score", "")) == score
        )
        out[key] = {
            "count": len(deals),
            "percentage": _pct(len(deals), total),
            "total_value": round(total_val, 2),
            "deals": deals[:20],
        }
    return out


def _element_breakdown(data: list) -> dict:
    total = len(data)
    result = {}
    for elem in _ELEMENTS:
        counts = Counter()
        for d in data:
            status = (d.get("elements") or {}).get(elem, {}).get("status", "unknown")
            counts[status] += 1
        result[elem] = {
            s: {"count": counts[s], "percentage": _pct(counts[s], total)}
            for s in ["confirmed", "partial", "at_risk", "unknown"]
        }
    return result


def _flag_frequency(data: list) -> dict:
    total = len(data)
    stage_names = ["NEW", "MEETING", "PROPOSAL", "CUSTOMER"]
    result = {}
    for flag in _FLAGS:
        affected = [d for d in data if (d.get("flags") or {}).get(flag)]
        value_exposed = sum(d.get("amount") or 0 for d in affected)
        by_stage = {s: sum(1 for d in affected if d.get("stage") == s) for s in stage_names}
        result[flag] = {
            "count": len(affected),
            "percentage": _pct(len(affected), total),
            "pipeline_value_exposed": round(value_exposed, 2),
            "by_stage": by_stage,
        }
    return result


def _rep_performance(data: list) -> list:
    by_rep = defaultdict(list)
    for d in data:
        by_rep[d.get("assigned_rep") or "Unknown"].append(d)
    rows = []
    for rep, deals in sorted(by_rep.items()):
        total = len(deals)
        score_dist = Counter(_normalize_score(d.get("overall_score", "")) for d in deals)
        # MEDDPICC completion = % of elements that are "confirmed"
        confirmed_sum = sum(
            sum(1 for e in _ELEMENTS if (d.get("elements") or {}).get(e, {}).get("status") == "confirmed")
            for d in deals
        )
        max_possible = total * len(_ELEMENTS)
        completion = _pct(confirmed_sum, max_possible)
        # Most common flag
        flag_counts = Counter(
            flag for d in deals
            for flag, val in (d.get("flags") or {}).items() if val
        )
        most_common_flag = flag_counts.most_common(1)[0][0] if flag_counts else "none"
        total_value = sum(d.get("amount") or 0 for d in deals)
        rows.append({
            "rep_name": rep,
            "deal_count": total,
            "total_pipeline_value": round(total_value, 2),
            "avg_deal_size": round(total_value / total, 0) if total else 0,
            "score_distribution": {
                "strong": score_dist.get("STRONG", 0),
                "needs_attention": score_dist.get("NEEDS ATTENTION", 0),
                "at_risk": score_dist.get("AT RISK", 0),
            },
            "meddpicc_completion_rate": completion,
            "coaching_priority": most_common_flag,
        })
    rows.sort(key=lambda r: r["meddpicc_completion_rate"])
    return rows


def _product_performance(data: list) -> list:
    by_product = defaultdict(list)
    for d in data:
        by_product[_extract_product(d.get("deal_name", "Unknown"))].append(d)
    rows = []
    for product, deals in sorted(by_product.items()):
        total = len(deals)
        at_risk = sum(1 for d in deals if _normalize_score(d.get("overall_score", "")) == "AT RISK")
        won = sum(1 for d in deals if d.get("stage") == "CUSTOMER")
        active = sum(1 for d in deals if d.get("stage") not in ("CUSTOMER",))
        win_rate = _pct(won, total)
        avg_prob = round(sum(d.get("probability") or 0 for d in deals) / total, 1) if total else 0
        flag_counts = Counter(
            flag for d in deals
            for flag, val in (d.get("flags") or {}).items() if val
        )
        most_common_gap = flag_counts.most_common(1)[0][0] if flag_counts else "none"
        total_value = sum(d.get("amount") or 0 for d in deals)
        rows.append({
            "product": product,
            "deal_count": total,
            "total_pipeline_value": round(total_value, 2),
            "at_risk_percentage": _pct(at_risk, total),
            "avg_probability": avg_prob,
            "most_common_gap": most_common_gap,
            "win_rate": win_rate,
        })
    rows.sort(key=lambda r: r["at_risk_percentage"], reverse=True)
    return rows


def _stage_analysis(data: list) -> list:
    by_stage = defaultdict(list)
    for d in data:
        by_stage[d.get("stage") or "UNKNOWN"].append(d)
    rows = []
    for stage, deals in sorted(by_stage.items()):
        total = len(deals)
        score_dist = Counter(_normalize_score(d.get("overall_score", "")) for d in deals)
        avg_prob = round(sum(d.get("probability") or 0 for d in deals) / total, 1) if total else 0
        total_value = sum(d.get("amount") or 0 for d in deals)
        flag_counts = Counter(
            flag for d in deals
            for flag, val in (d.get("flags") or {}).items() if val
        )
        most_common_flag = flag_counts.most_common(1)[0][0] if flag_counts else "none"
        rows.append({
            "stage": stage,
            "deal_count": total,
            "total_value": round(total_value, 2),
            "avg_probability": avg_prob,
            "score_distribution": {
                "strong": score_dist.get("STRONG", 0),
                "needs_attention": score_dist.get("NEEDS ATTENTION", 0),
                "at_risk": score_dist.get("AT RISK", 0),
            },
            "most_common_flag": most_common_flag,
        })
    return rows


def _win_analysis(data: list) -> dict:
    won = [d for d in data if d.get("stage") == "CUSTOMER"]
    total_won = len(won)
    total_value = sum(d.get("amount") or 0 for d in won)
    elem_confirmed = {
        e: sum(1 for d in won if (d.get("elements") or {}).get(e, {}).get("status") == "confirmed")
        for e in _ELEMENTS
    }
    by_product = Counter(_extract_product(d.get("deal_name", "")) for d in won)
    by_rep = Counter(d.get("assigned_rep", "Unknown") for d in won)
    return {
        "total_won_deals": total_won,
        "total_won_value": round(total_value, 2),
        "win_patterns": {
            e: {"count": c, "percentage": _pct(c, total_won)}
            for e, c in elem_confirmed.items()
        },
        "win_by_product": dict(by_product.most_common(10)),
        "win_by_rep": dict(by_rep.most_common()),
    }


def _forecast(data: list) -> dict:
    active = [d for d in data if d.get("stage") not in ("CUSTOMER",)]
    weighted = sum((d.get("amount") or 0) * (d.get("probability") or 0) / 100 for d in active)
    strong_value = sum(
        d.get("amount") or 0 for d in active
        if _normalize_score(d.get("overall_score", "")) == "STRONG"
    )
    at_risk_value = sum(
        d.get("amount") or 0 for d in active
        if _normalize_score(d.get("overall_score", "")) == "AT RISK"
    )
    at_risk_count = sum(
        1 for d in active
        if _normalize_score(d.get("overall_score", "")) == "AT RISK"
    )
    # Confidence based on AT RISK ratio
    at_risk_ratio = at_risk_count / len(active) if active else 0
    if at_risk_ratio > 0.3:
        confidence, reason = "LOW", f"{round(at_risk_ratio*100)}% of active deals are AT RISK"
    elif at_risk_ratio > 0.15:
        confidence, reason = "MEDIUM", f"{round(at_risk_ratio*100)}% of active deals are AT RISK"
    else:
        confidence, reason = "HIGH", f"Only {round(at_risk_ratio*100)}% of active deals are AT RISK"
    return {
        "active_deal_count": len(active),
        "weighted_pipeline": round(weighted, 2),
        "strong_deals_value": round(strong_value, 2),
        "at_risk_deals_value": round(at_risk_value, 2),
        "at_risk_deal_count": at_risk_count,
        "forecast_confidence": confidence,
        "forecast_confidence_reason": reason,
    }


def _build_summary(overview, scores, flags, reps, products, stage_data, win, forecast_data) -> str:
    total = overview["total_deals"]
    val = overview["total_pipeline_value"]

    strong_pct = scores.get("strong", {}).get("percentage", 0)
    na_pct = scores.get("needs_attention", {}).get("percentage", 0)
    ar_pct = scores.get("at_risk", {}).get("percentage", 0)
    strong_c = scores.get("strong", {}).get("count", 0)
    na_c = scores.get("needs_attention", {}).get("count", 0)
    ar_c = scores.get("at_risk", {}).get("count", 0)

    top_flags = sorted(flags.items(), key=lambda x: x[1]["count"], reverse=True)[:3]
    flag_lines = "\n".join(
        f"  - {f.replace('_', ' ').title()}: {v['count']} deals ({v['percentage']}%) "
        f"— ${v['pipeline_value_exposed']:,.0f} exposed"
        for f, v in top_flags
    )

    top_ar_product = max(products, key=lambda p: p["at_risk_percentage"], default={})
    coaching_rep = reps[0] if reps else {}

    win_total = win.get("total_won_deals", 0)
    won_champ = win.get("win_patterns", {}).get("champion", {})
    won_champ_pct = won_champ.get("percentage", 0) if win_total else 0

    return f"""PIPELINE ANALYSIS — {total} deals | ${val:,.0f} total pipeline value

HEALTH: {strong_c} STRONG ({strong_pct}%) | {na_c} NEEDS ATTENTION ({na_pct}%) | {ar_c} AT RISK ({ar_pct}%)

TOP FLAGS:
{flag_lines}

BIGGEST AT-RISK PRODUCT:
{top_ar_product.get('product', 'N/A')}: {top_ar_product.get('at_risk_percentage', 0)}% AT RISK rate ({top_ar_product.get('deal_count', 0)} deals)

REP NEEDING MOST COACHING:
{coaching_rep.get('rep_name', 'N/A')}: {coaching_rep.get('meddpicc_completion_rate', 0)}% MEDDPICC completion — top gap: {coaching_rep.get('coaching_priority', 'N/A')}

WIN PATTERN ({win_total} won deals):
{won_champ_pct}% of won deals had confirmed champion

FORECAST:
Weighted pipeline (active deals): ${forecast_data['weighted_pipeline']:,.0f}
AT RISK value: ${forecast_data['at_risk_deals_value']:,.0f} across {forecast_data['at_risk_deal_count']} deals
Confidence: {forecast_data['forecast_confidence']} — {forecast_data['forecast_confidence_reason']}"""


def analyze_pipeline(filter_by: dict = None, focus: str = "full") -> dict:
    """
    Reads meddpicc_pipeline.json and computes pipeline statistics.

    Args:
        filter_by: Optional filters, e.g. {"stage": "PROPOSAL"}, {"assigned_rep": "<representative identifier>"},
                   {"overall_score": "AT_RISK"}, {"product": "FraudGuard"}
        focus: "full" | "health" | "flags" | "reps" | "products" | "win_analysis" | "forecast"

    Returns:
        {"data": {...computed stats...}, "summary": "pre-formatted narrative string"}
    """
    try:
        raw = _load()
    except FileNotFoundError as e:
        return {"error": str(e), "data": {}, "summary": str(e)}

    valid, failed = [], []
    for i, rec in enumerate(raw):
        if isinstance(rec, dict) and rec.get("deal_name"):
            valid.append(rec)
        else:
            failed.append(i)
    if failed:
        print(f"[analyze_pipeline] skipped {len(failed)} malformed records: indices {failed[:10]}")

    data = _apply_filters(valid, filter_by)
    if not data:
        msg = f"No deals matched filters: {filter_by}"
        return {"error": msg, "data": {}, "summary": msg}

    overview = _overview(data)
    scores   = _score_distribution(data)
    elements = _element_breakdown(data)
    flags    = _flag_frequency(data)
    reps     = _rep_performance(data)
    products = _product_performance(data)
    stages   = _stage_analysis(data)
    win      = _win_analysis(data)
    forecast = _forecast(data)

    stats = {
        "overview":           overview,
        "score_distribution": scores,
        "element_breakdown":  elements,
        "flag_frequency":     flags,
        "rep_performance":    reps,
        "product_performance": products,
        "stage_analysis":     stages,
        "win_analysis":       win,
        "forecast":           forecast,
        "filter_applied":     filter_by or {},
        "records_analysed":   len(data),
    }

    # Trim to focus if requested
    focus_map = {
        "health":       ["overview", "score_distribution"],
        "flags":        ["overview", "flag_frequency"],
        "reps":         ["overview", "rep_performance"],
        "products":     ["overview", "product_performance"],
        "win_analysis": ["overview", "win_analysis"],
        "forecast":     ["overview", "forecast"],
    }
    if focus != "full" and focus in focus_map:
        stats = {k: stats[k] for k in focus_map[focus]}
        stats["records_analysed"] = len(data)

    summary = _build_summary(overview, scores, flags, reps, products, stages, win, forecast)

    return {"data": stats, "summary": summary}
