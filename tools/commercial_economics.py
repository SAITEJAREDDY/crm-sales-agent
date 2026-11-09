#!/usr/bin/env python3
"""Memo-ready commercial economics table generation."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
PIPELINE_JSON = BASE_DIR / "knowledge_base" / "meddpicc_pipeline.json"
TOOLS_DIR = BASE_DIR / "tools"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

try:
    from financial_inputs import gather_inputs
    from financial_model import compute as compute_model
except Exception:
    gather_inputs = None
    compute_model = None


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _usd(value: float) -> str:
    return f"${float(value or 0):,.0f}"


def _load_pipeline_records() -> list[dict[str, Any]]:
    if not PIPELINE_JSON.exists():
        return []
    try:
        with PIPELINE_JSON.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _normalize_deal(deal: dict[str, Any]) -> dict[str, Any]:
    name = deal.get("deal_name") or deal.get("name") or ""
    company = deal.get("company") or deal.get("companyName") or ""
    amount = deal.get("amount") or deal.get("deal_amount") or 0
    if isinstance(amount, dict):
        amount = (amount.get("amountMicros") or 0) / 1_000_000

    return {
        "deal_id": deal.get("deal_id") or deal.get("id") or "",
        "deal_name": name,
        "company": company,
        "amount": float(amount or 0),
        "assigned_rep": deal.get("assigned_rep") or "",
        "overall_score": deal.get("overall_score") or "",
        "elements": deal.get("elements") or {},
        "flags": deal.get("flags") or {},
    }


def _find_pipeline_match(normalized: dict[str, Any]) -> dict[str, Any]:
    records = _load_pipeline_records()
    deal_id = normalized.get("deal_id")
    if deal_id:
        for record in records:
            if record.get("deal_id") == deal_id:
                return record

    deal_slug = _slugify(normalized.get("deal_name", ""))
    company_slug = _slugify(normalized.get("company", ""))
    for record in records:
        record_deal_slug = _slugify(record.get("deal_name", ""))
        record_company_slug = _slugify(record.get("company", ""))
        if deal_slug and record_deal_slug == deal_slug:
            if not company_slug or company_slug == record_company_slug:
                return record
    return {}


def _deal_for_financial_model(deal: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_deal(deal)
    match = _find_pipeline_match(normalized)
    if match:
        merged = dict(match)
        if normalized["amount"] > 0:
            merged["amount"] = normalized["amount"]
        elif not merged.get("amount"):
            merged["amount"] = normalized["amount"]
        if normalized["company"]:
            merged["company"] = normalized["company"]
        elif not merged.get("company"):
            merged["company"] = normalized["company"]
        if normalized["deal_name"]:
            merged["deal_name"] = normalized["deal_name"]
        return merged
    return normalized


def build_commercial_economics(deal: dict[str, Any]) -> dict[str, Any]:
    """Return raw commercial metrics plus a filled Markdown table for memo prompts."""
    model_deal = _deal_for_financial_model(deal)
    arr = float(model_deal.get("amount") or 0)
    project_value_3yr = arr * 3
    expansion_potential = arr * 0.20

    metrics = {
        "arr": arr,
        "project_value_3yr": project_value_3yr,
        "net_annual_price": arr,
        "annual_discount_savings": 0.0,
        "expansion_potential": expansion_potential,
        "confidence_level": "LOW",
        "confidence_score": 0,
        "source": "fallback",
        "error": "",
    }

    if gather_inputs and compute_model and arr > 0:
        try:
            # Keep memo generation deterministic and local. Do not trigger LLM extraction.
            gathered = gather_inputs(model_deal, api_key="")
            model = compute_model(gathered)
            metrics.update({
                "net_annual_price": float(model.get("net_price") or arr),
                "annual_discount_savings": float(model.get("edp_savings") or 0),
                "confidence_level": gathered.get("confidence_level") or "LOW",
                "confidence_score": int(gathered.get("confidence_score") or 0),
                "source": "financial_model",
            })
        except Exception as exc:
            metrics["error"] = str(exc)

    confidence = f"{metrics['confidence_level']}, {metrics['confidence_score']}/100"
    table = "\n".join([
        "3. COMMERCIAL ECONOMICS",
        "",
        "| Metric | Value | Notes |",
        "|---|---:|---|",
        f"| ARR | {_usd(metrics['arr'])} | Annual recurring value |",
        f"| 3-Year Project Value | {_usd(metrics['project_value_3yr'])} | ARR over 3 years |",
        f"| Net Annual Price | {_usd(metrics['net_annual_price'])} | After current discount assumptions |",
        f"| Annual Discount Savings | {_usd(metrics['annual_discount_savings'])} | Savings from current Azure EA tier |",
        f"| Expansion Potential | {_usd(metrics['expansion_potential'])} | Estimated upside if adoption succeeds |",
        f"| Model Confidence | {confidence} | Based on available financial inputs |",
    ])
    metrics["table"] = table
    return metrics


def build_commercial_economics_table(deal: dict[str, Any]) -> str:
    return build_commercial_economics(deal)["table"]
