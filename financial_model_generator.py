"""
financial_model_generator.py
Reads resource_estimates JSON per deal, computes financial metrics,
generates a 6-tab openpyxl Excel workbook, and saves to financial_models/.

Usage:
    python financial_model_generator.py          # full run (skips existing)
    python financial_model_generator.py --test   # 3-deal validation only
    python financial_model_generator.py --reset  # overwrite existing
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

import openpyxl
from openpyxl.styles import (
    Alignment, Border, Font, PatternFill, Side
)
from openpyxl.utils import get_column_letter

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).resolve().parent
KB_DIR         = BASE_DIR / "knowledge_base"
ESTIMATES_DIR  = KB_DIR / "resource_estimates"
PIPELINE_JSON  = KB_DIR / "meddpicc_pipeline.json"
PRODUCT_MAP    = KB_DIR / "kb" / "product_mapping.json"
COST_SHEETS    = KB_DIR / "kb" / "cost_sheets"
NOTES_DIR      = KB_DIR / "notes"
OUTPUT_DIR     = KB_DIR / "financial_models"
PROGRESS_FILE  = OUTPUT_DIR / "_progress.json"
FAILED_FILE    = OUTPUT_DIR / "_failed_models.json"

TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")
MODEL = TOGETHER_MODEL

# ── Colors ────────────────────────────────────────────────────────────────────
C = {
    "input_blue":    "0000FF",
    "formula_black": "000000",
    "link_green":    "008000",
    "warning_red":   "FF0000",
    "header_dark":   "1F3864",
    "header_light":  "D6E4F0",
    "positive_bg":   "E2EFDA",
    "negative_bg":   "FCE4D6",
    "input_bg":      "EBF3FB",
    "assumed_bg":    "FFFACD",
    "section_bg":    "F2F2F2",
    "white":         "FFFFFF",
    "tab_blue":      "1F3864",
    "tab_green":     "375623",
    "tab_amber":     "7F6000",
    "tab_purple":    "3F1F78",
    "tab_grey":      "404040",
    "tab_teal":      "17375E",
    "score_green":   "375623",
    "score_amber":   "7F6000",
    "score_red":     "9C0006",
    "purple_light":  "EAE0F5",
    "teal_light":    "E2F0EF",
}

FMT_USD   = '$#,##0'
FMT_USDNEG= '($#,##0);($#,##0)'
FMT_PCT   = '0.0%'
FMT_NUM   = '#,##0'
FMT_DATE  = 'DD-MMM-YYYY'


# ── EDP tier table ────────────────────────────────────────────────────────────
EDP_TIERS = [
    (0,         100_000,   0.00),
    (100_000,   250_000,   0.05),
    (250_000,   500_000,   0.08),
    (500_000,   1_000_000, 0.12),
    (1_000_000, 2_000_000, 0.15),
    (2_000_000, float("inf"), 0.18),
]


def edp_discount(amount: float) -> float:
    for lo, hi, rate in EDP_TIERS:
        if lo <= amount < hi:
            return rate
    return 0.18


def next_edp_tier(amount: float) -> tuple[float, float]:
    for i, (lo, hi, rate) in enumerate(EDP_TIERS):
        if lo <= amount < hi:
            if i + 1 < len(EDP_TIERS):
                nlo, _, nrate = EDP_TIERS[i + 1]
                return nlo, nrate
    return amount, edp_discount(amount)


def npv(rate: float, cashflows: list[float]) -> float:
    return sum(cf / (1 + rate) ** (i + 1) for i, cf in enumerate(cashflows))


# ── Slugify helpers ───────────────────────────────────────────────────────────

def slugify(s: str) -> str:
    """Underscored slug — for resource estimate filenames and output files."""
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def profile_slug(s: str) -> str:
    """Alphanumeric only — for notes company directory."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def notes_deal_slug(deal_name: str) -> str:
    """Split on em-dash, lowercase, hyphen→underscore, remove spaces."""
    parts = re.split(r"\s*[—–]\s*", deal_name, maxsplit=1)
    result = []
    for part in parts:
        p = part.lower().replace("-", "_")
        p = re.sub(r"[^a-z0-9_]", "", p)
        result.append(p)
    return "_".join(result)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Data loaders ──────────────────────────────────────────────────────────────

def load_pipeline() -> dict[str, dict]:
    """Index pipeline by deal_id."""
    with open(PIPELINE_JSON, encoding="utf-8") as f:
        data = json.load(f)
    return {d["deal_id"]: d for d in data}


def load_product_mapping() -> dict[str, dict]:
    with open(PRODUCT_MAP, encoding="utf-8") as f:
        data = json.load(f)
    mappings = data.get("mappings", data) if isinstance(data, dict) else data
    if isinstance(mappings, list):
        return {m["crm_product"]: m for m in mappings}
    return mappings


_NON_DEAL_FILES = {"resource_pipeline.json", "resource_pipeline.csv"}

def list_resource_estimates() -> list[Path]:
    return sorted(
        [p for p in ESTIMATES_DIR.glob("*.json")
         if not p.name.startswith("_") and p.name not in _NON_DEAL_FILES],
        key=lambda p: p.stat().st_size,
        reverse=True,  # largest first (proxy for deal amount)
    )


def load_estimate(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_notes_meddpicc(company: str, deal_name: str) -> dict | None:
    cslug = profile_slug(company)
    dslug = notes_deal_slug(deal_name)
    path  = NOTES_DIR / cslug / dslug / "_meddpicc.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def load_cost_sheet(kb_slug: str) -> str:
    path = COST_SHEETS / f"{kb_slug}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ── Growth rate defaults by pattern ──────────────────────────────────────────
GROWTH_DEFAULTS = {
    "Quick Start":      (0.15, 0.20),
    "Production Grade": (0.25, 0.30),
    "Enterprise Scale": (0.35, 0.40),
}


# ── Financial computations ────────────────────────────────────────────────────

def compute_financials(est: dict, pipeline_rec: dict) -> dict:
    mc   = est["monthly_consumption"]
    ir   = est["implementation_resources"]
    impl = est["implementation_pattern"]

    # Rates
    sa_day_rate      = 2_500.0
    proserv_day_rate = 3_000.0
    partner_day_rate = 1_800.0

    # Implementation cost
    impl_cost = float(ir.get("one_time_implementation_cost_usd") or 0)
    if impl_cost <= 0:
        sa_cost      = float(ir.get("sa_days_required", 0)) * sa_day_rate
        proserv_cost = float(ir.get("proserv_days_required", 0)) * proserv_day_rate
        partner_cost = float(ir.get("partner_days_required", 0)) * partner_day_rate
        internal     = float(ir.get("estimated_timeline_weeks", 0)) * 2 * 150 * 40
        impl_cost    = sa_cost + proserv_cost + partner_cost + internal
    else:
        sa_cost      = float(ir.get("sa_days_required", 0)) * sa_day_rate
        proserv_cost = float(ir.get("proserv_days_required", 0)) * proserv_day_rate
        partner_cost = float(ir.get("partner_days_required", 0)) * partner_day_rate
        internal     = float(ir.get("estimated_timeline_weeks", 0)) * 2 * 150 * 40

    total_implementation = impl_cost

    # Consumption
    year1_consumption = float(mc.get("total_annual_usd") or 0)

    # Growth rates
    y2g, y3g = GROWTH_DEFAULTS.get(impl, (0.25, 0.30))
    year2_consumption = year1_consumption * (1 + y2g)
    year3_consumption = year2_consumption * (1 + y3g)

    # Costs
    year1_total_cost = total_implementation + year1_consumption
    year2_total_cost = year2_consumption
    year3_total_cost = year3_consumption

    # Revenue
    deal_amount   = float(pipeline_rec.get("amount") or 0)
    year1_revenue = deal_amount * 0.40
    year2_revenue = deal_amount * 0.35
    year3_revenue = deal_amount * 0.25
    total_revenue = deal_amount

    # Net
    net_year1     = year1_revenue - year1_total_cost
    net_year2     = year2_revenue - year2_total_cost
    net_year3     = year3_revenue - year3_total_cost
    cumulative    = net_year1 + net_year2 + net_year3

    # Key metrics
    payback = total_implementation / (year1_revenue / 12) if year1_revenue > 0 else 999
    payback = min(payback, 120)
    roi_3yr = (cumulative / total_implementation * 100) if total_implementation > 0 else 0
    npv_val = npv(0.10, [net_year1, net_year2, net_year3])

    # EDP
    edp_pct          = edp_discount(deal_amount)
    edp_savings      = deal_amount * edp_pct
    net_price        = deal_amount - edp_savings
    next_thresh, next_rate = next_edp_tier(deal_amount)
    next_tier_savings    = next_thresh * next_rate
    incr_savings         = next_tier_savings - edp_savings
    if incr_savings > 0:
        breakeven = (next_thresh - deal_amount) / (incr_savings / 36)
    else:
        breakeven = 0

    # Commitment options
    oneyear_3yr    = deal_amount * (1 - edp_pct) * 3
    threeyear_pct  = min(edp_pct + 0.02, 0.20)
    threeyear_3yr  = deal_amount * (1 - threeyear_pct) * 3

    # Scenarios
    scen = {
        "conservative": 0.60,
        "base":         1.00,
        "optimistic":   1.30,
    }

    return {
        "deal_amount":         deal_amount,
        "sa_day_rate":         sa_day_rate,
        "proserv_day_rate":    proserv_day_rate,
        "partner_day_rate":    partner_day_rate,
        "sa_cost":             sa_cost,
        "proserv_cost":        proserv_cost,
        "partner_cost":        partner_cost,
        "internal_cost":       internal,
        "total_implementation": total_implementation,
        "year1_consumption":   year1_consumption,
        "year2_consumption":   year2_consumption,
        "year3_consumption":   year3_consumption,
        "year2_growth":        y2g,
        "year3_growth":        y3g,
        "year1_total_cost":    year1_total_cost,
        "year2_total_cost":    year2_total_cost,
        "year3_total_cost":    year3_total_cost,
        "year1_revenue":       year1_revenue,
        "year2_revenue":       year2_revenue,
        "year3_revenue":       year3_revenue,
        "total_revenue":       total_revenue,
        "net_year1":           net_year1,
        "net_year2":           net_year2,
        "net_year3":           net_year3,
        "cumulative_3yr":      cumulative,
        "payback_months":      payback,
        "roi_3yr_pct":         roi_3yr,
        "npv_10pct":           npv_val,
        "edp_pct":             edp_pct,
        "edp_savings":         edp_savings,
        "net_price":           net_price,
        "next_edp_threshold":  next_thresh,
        "next_edp_rate":       next_rate,
        "next_tier_savings":   next_tier_savings,
        "incremental_savings": incr_savings,
        "breakeven_months":    breakeven,
        "oneyear_3yr_total":   oneyear_3yr,
        "threeyear_pct":       threeyear_pct,
        "threeyear_3yr_total": threeyear_3yr,
        "scenario_conservative": scen["conservative"],
        "scenario_base":         scen["base"],
        "scenario_optimistic":   scen["optimistic"],
    }


# ── Together AI narration ─────────────────────────────────────────────────────

def generate_narration(est: dict, fin: dict, pipeline_rec: dict) -> dict[str, str]:
    prompt = f"""You are a senior financial analyst preparing deal economics summaries for a sales team.

DEAL: {est["deal_name"]} at {est["company"]}
PRODUCT: {est["product"]} ({est["base_category"]})
IMPLEMENTATION PATTERN: {est["implementation_pattern"]}
MEDDPICC SCORE: {pipeline_rec.get("overall_score", "N/A")}
CONFIDENCE: {est["confidence"]} ({est.get("confidence_reasoning", "")})

KEY FINANCIAL METRICS:
Total implementation cost:  ${fin["total_implementation"]:,.0f}
Year 1 consumption:         ${fin["year1_consumption"]:,.0f}
Year 1 net:                 ${fin["net_year1"]:,.0f}
3-year cumulative net:      ${fin["cumulative_3yr"]:,.0f}
Payback period:             {fin["payback_months"]:.0f} months
3-year ROI:                 {fin["roi_3yr_pct"]:.0f}%
EDP discount available:     {fin["edp_pct"]:.0%}
EDP savings:                ${fin["edp_savings"]:,.0f}

ARCHITECTURE CONTEXT:
{est.get("architect_narrative", "")}

PAIN SIGNAL FROM NOTES:
{pipeline_rec.get("elements", {}).get("identify_pain", {}).get("evidence", "Not captured")}

Write these three sections. Be specific and direct. Use the exact numbers above. No jargon.

EXECUTIVE SUMMARY:
Two sentences. The business case for a CFO. Lead with payback period and ROI.

AE TALKING POINTS:
Three bullet points. What to say in the pricing conversation. Reference specific numbers.

EDP RECOMMENDATION:
One sentence. Whether to push for higher commitment and why. Reference the specific savings amount."""

    try:
        import requests
        resp = request_completion(
            TOGETHER_CHAT_URL,
            headers={"Authorization": f"Bearer {TOGETHER_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "max_tokens": 500,
            },
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"    [Narration error] {e}")
        text = ""

    # Parse sections
    def _section(label: str, next_label: str | None, text: str) -> str:
        pattern = rf"{re.escape(label)}:?\s*\n(.*?)"
        if next_label:
            pattern += rf"(?={re.escape(next_label)})"
        else:
            pattern += r"\Z"
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    exec_summary  = _section("EXECUTIVE SUMMARY", "AE TALKING POINTS", text)
    ae_points     = _section("AE TALKING POINTS", "EDP RECOMMENDATION", text)
    edp_rec       = _section("EDP RECOMMENDATION", None, text)

    if not exec_summary:
        exec_summary = (
            f"This {est['implementation_pattern']} implementation delivers a "
            f"{fin['roi_3yr_pct']:.0f}% 3-year ROI with a "
            f"{fin['payback_months']:.0f}-month payback period. "
            f"The ${fin['total_implementation']:,.0f} implementation investment is recovered through "
            f"operational savings and productivity gains."
        )
    if not ae_points:
        ae_points = (
            f"• Implementation investment of ${fin['total_implementation']:,.0f} with payback in {fin['payback_months']:.0f} months.\n"
            f"• 3-year ROI of {fin['roi_3yr_pct']:.0f}% on ${fin['cumulative_3yr']:,.0f} cumulative net position.\n"
            f"• EDP discount of {fin['edp_pct']:.0%} saves ${fin['edp_savings']:,.0f} annually."
        )
    if not edp_rec:
        edp_rec = (
            f"Current EDP tier saves ${fin['edp_savings']:,.0f} annually; "
            f"upgrading to the next tier requires ${fin['next_edp_threshold'] - fin['deal_amount']:,.0f} "
            f"additional commitment for ${fin['incremental_savings']:,.0f} in additional savings."
        )

    return {
        "executive_summary": exec_summary,
        "ae_talking_points": ae_points,
        "edp_recommendation": edp_rec,
        "raw": text,
    }


# ── openpyxl helpers ──────────────────────────────────────────────────────────

def _fill(hex_color: str) -> PatternFill:
    return PatternFill(fill_type="solid", fgColor=hex_color)


def _font(bold=False, italic=False, size=10, color="000000", name="Calibri") -> Font:
    return Font(bold=bold, italic=italic, size=size, color=color, name=name)


def _border_thin() -> Border:
    s = Side(style="thin", color="AAAAAA")
    return Border(left=s, right=s, top=s, bottom=s)


def _border_medium() -> Border:
    s = Side(style="medium", color="000000")
    return Border(left=s, right=s, top=s, bottom=s)


def _align(wrap=False, h="left", v="center") -> Alignment:
    return Alignment(wrap_text=wrap, horizontal=h, vertical=v)


def ws_write(ws, row: int, col: int, value, bold=False, italic=False,
             font_color="000000", fill_color: str | None = None,
             num_fmt: str | None = None, wrap=False, halign="left",
             border=False, size=10) -> None:
    cell = ws.cell(row=row, column=col, value=value)
    cell.font = _font(bold=bold, italic=italic, color=font_color, size=size)
    if fill_color:
        cell.fill = _fill(fill_color)
    if num_fmt:
        cell.number_format = num_fmt
    cell.alignment = _align(wrap=wrap, h=halign)
    if border:
        cell.border = _border_thin()


def section_header(ws, row: int, label: str, ncols: int = 6,
                   fill=None, font_color="FFFFFF") -> None:
    ws_write(ws, row, 1, label, bold=True, font_color=font_color,
             fill_color=fill or C["header_dark"], size=10)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
    ws.row_dimensions[row].height = 16


def label_value(ws, row: int, label: str, value,
                label_fill=None, value_fill=None,
                value_fmt=None, value_color="000000",
                bold_value=False, wrap_value=False) -> None:
    ws_write(ws, row, 1, label, bold=False,
             fill_color=label_fill or C["section_bg"], border=True)
    ws_write(ws, row, 2, value, bold=bold_value,
             fill_color=value_fill, num_fmt=value_fmt,
             font_color=value_color, wrap=wrap_value, border=True)


def currency_cell(ws, row: int, col: int, value: float,
                  bold=False, fill_color: str | None = None) -> None:
    color = C["positive_bg"] if value >= 0 else C["negative_bg"]
    if fill_color:
        color = fill_color
    ws_write(ws, row, col, value, bold=bold,
             fill_color=color, num_fmt=FMT_USD, halign="right", border=True)


# ── Tab 1 — Dashboard ─────────────────────────────────────────────────────────

def build_dashboard(wb: openpyxl.Workbook, est: dict, fin: dict,
                    pipeline_rec: dict, narration: dict) -> None:
    ws = wb.create_sheet("Dashboard")
    ws.sheet_properties.tabColor = C["tab_blue"]
    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = False

    # Column widths
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 18
    ws.column_dimensions["E"].width = 18
    ws.column_dimensions["F"].width = 16

    # ── Row 1: Main header ──
    ws.row_dimensions[1].height = 24
    ws_write(ws, 1, 1, "DEAL DESK — FINANCIAL MODEL", bold=True, size=12,
             font_color=C["white"], fill_color=C["header_dark"])
    ws_write(ws, 1, 3, f"{est['deal_name']}  |  {est['company']}", bold=True, size=10,
             font_color=C["white"], fill_color=C["header_dark"], halign="center")
    ws_write(ws, 1, 6, f"Generated {datetime.now().strftime('%d-%b-%Y')}", italic=True,
             font_color=C["white"], fill_color=C["header_dark"], halign="right")
    ws.merge_cells("A1:B1")
    ws.merge_cells("C1:E1")

    row = 3
    # ── MEDDPICC Score Badge ──
    score = pipeline_rec.get("overall_score", "")
    score_color = {
        "STRONG": C["score_green"],
        "NEEDS ATTENTION": C["score_amber"],
        "AT RISK": C["score_red"],
    }.get(score, C["score_amber"])

    ws.row_dimensions[row].height = 28
    ws_write(ws, row, 1, f"MEDDPICC: {score}", bold=True, size=13,
             font_color=C["white"], fill_color=score_color)
    ws.merge_cells(f"A{row}:C{row}")
    ws_write(ws, row, 4, f"Confidence: {est['confidence']}",
             bold=True, font_color=score_color)
    ws.merge_cells(f"D{row}:F{row}")

    row += 1
    reason = pipeline_rec.get("overall_reason", "")
    ws_write(ws, row, 1, reason, italic=True, wrap=True,
             fill_color=C["section_bg"])
    ws.merge_cells(f"A{row}:F{row}")
    ws.row_dimensions[row].height = 32

    row += 2
    # ── THE BUSINESS CASE ──
    section_header(ws, row, "THE BUSINESS CASE")
    row += 1
    label_value(ws, row, "Implementation Investment",
                fin["total_implementation"], value_fmt=FMT_USD,
                bold_value=True, value_fill=C["input_bg"])
    row += 1
    label_value(ws, row, "Year 1 Consumption",
                fin["year1_consumption"], value_fmt=FMT_USD,
                value_fill=C["input_bg"])
    row += 1
    label_value(ws, row, "Payback Period",
                f"{fin['payback_months']:.0f} months", bold_value=True)
    row += 1
    label_value(ws, row, "3-Year ROI",
                f"{fin['roi_3yr_pct']:.0f}%", bold_value=True,
                value_fill=C["positive_bg"] if fin["roi_3yr_pct"] > 0 else C["negative_bg"])

    row += 2
    # ── THREE YEAR SUMMARY ──
    section_header(ws, row, "THREE YEAR SUMMARY")
    row += 1
    for label, val in [
        ("Year 1 Net Position",   fin["net_year1"]),
        ("Year 2 Net Position",   fin["net_year2"]),
        ("Year 3 Net Position",   fin["net_year3"]),
        ("3-Year Cumulative Net", fin["cumulative_3yr"]),
    ]:
        bold = label.startswith("3-Year")
        fill = C["positive_bg"] if val >= 0 else C["negative_bg"]
        label_value(ws, row, label, val, value_fmt=FMT_USD,
                    value_fill=fill, bold_value=bold)
        row += 1

    row += 1
    # ── ACTIVE RISK FLAGS ──
    section_header(ws, row, "ACTIVE RISK FLAGS", fill=C["score_red"])
    row += 1

    flag_labels = {
        "missing_economic_buyer":  "Missing Economic Buyer",
        "missing_champion":        "No Confirmed Champion",
        "unquantified_pain":       "Pain Not Quantified",
        "competitor_unknown":      "Competitor Unknown",
        "paper_process_unknown":   "Procurement Not Discussed",
        "deal_overdue":            "Close Date Passed",
        "stalled":                 "Deal Stalled 90+ Days",
    }
    flags = pipeline_rec.get("flags", {})
    active_flags = [lbl for k, lbl in flag_labels.items() if flags.get(k)]

    if active_flags:
        for lbl in active_flags:
            ws_write(ws, row, 1, f"\U0001f534 {lbl}", font_color=C["warning_red"],
                     fill_color=C["negative_bg"], border=True)
            ws.merge_cells(f"A{row}:F{row}")
            row += 1
    else:
        ws_write(ws, row, 1, "✅ No active risk flags",
                 font_color=C["score_green"], fill_color=C["positive_bg"], border=True)
        ws.merge_cells(f"A{row}:F{row}")
        row += 1

    row += 1
    # ── REP ACTION REQUIRED ──
    section_header(ws, row, "REP ACTION REQUIRED", fill=C["score_amber"])
    row += 1
    next_prio = pipeline_rec.get("next_priority", "No priority set.")
    ws_write(ws, row, 1, next_prio, italic=True, wrap=True,
             fill_color=C["assumed_bg"], border=True)
    ws.merge_cells(f"A{row}:F{row}")
    ws.row_dimensions[row].height = 40

    row += 2
    # ── EDP OPPORTUNITY ──
    section_header(ws, row, "EDP OPPORTUNITY")
    row += 1
    for label, val in [
        ("Current EDP Discount",      f"{fin['edp_pct']:.0%}"),
        ("Annual EDP Savings",        fin["edp_savings"]),
        ("Next Tier Threshold",       fin["next_edp_threshold"]),
        ("Next Tier Savings",         fin["next_tier_savings"]),
        ("Breakeven on Upgrade",      f"{fin['breakeven_months']:.0f} months"),
    ]:
        fmt = FMT_USD if isinstance(val, float) else None
        label_value(ws, row, label, val, value_fmt=fmt)
        row += 1

    row += 1
    # ── ARCHITECTURE SUMMARY ──
    section_header(ws, row, "ARCHITECTURE SUMMARY", fill=C["tab_teal"])
    row += 1
    wp = est.get("workload_profile", {})
    for label, val in [
        ("Implementation Pattern", est.get("implementation_pattern", "")),
        ("Workload Type",          wp.get("workload_type", "")),
        ("Availability Required",  wp.get("availability_requirement", "")),
        ("Compliance Flags",       ", ".join(wp.get("compliance_flags", []))),
    ]:
        label_value(ws, row, label, val, value_fill=C["teal_light"])
        row += 1

    row += 1
    # ── EXECUTIVE SUMMARY ──
    section_header(ws, row, "EXECUTIVE SUMMARY")
    row += 1
    ws_write(ws, row, 1, narration["executive_summary"], italic=True, wrap=True,
             fill_color=C["section_bg"], border=True)
    ws.merge_cells(f"A{row}:F{row}")
    ws.row_dimensions[row].height = 52

    row += 2
    # ── AE TALKING POINTS ──
    section_header(ws, row, "AE TALKING POINTS")
    row += 1
    for pt in narration["ae_talking_points"].split("\n"):
        pt = pt.strip()
        if not pt:
            continue
        ws_write(ws, row, 1, pt, wrap=True, border=True,
                 fill_color=C["input_bg"])
        ws.merge_cells(f"A{row}:F{row}")
        ws.row_dimensions[row].height = 28
        row += 1


# ── Tab 2 — Assumptions ───────────────────────────────────────────────────────

def build_assumptions(wb: openpyxl.Workbook, est: dict, fin: dict) -> dict[str, str]:
    """Returns a cell map: logical_name -> 'Assumptions!BN'."""
    ws = wb.create_sheet("Assumptions")
    ws.sheet_properties.tabColor = C["tab_grey"]
    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 16

    mc = est["monthly_consumption"]
    ir = est["implementation_resources"]
    compute = est["compute"]
    storage = est["storage"]
    database = est["database"]
    network  = est["network"]
    add_svcs = est.get("additional_services", [])
    primary  = compute.get("primary_instances", {})
    secondary = compute.get("secondary_instances", {})

    cell_map: dict[str, str] = {}

    def cm(name: str, row: int, col: int = 2) -> str:
        addr = f"Assumptions!{get_column_letter(col)}{row}"
        cell_map[name] = addr
        return addr

    row = 1
    ws.row_dimensions[row].height = 20
    ws_write(ws, row, 1, "ASSUMPTIONS", bold=True, size=12,
             font_color=C["white"], fill_color=C["header_dark"])
    ws.merge_cells(f"A{row}:C{row}")

    # ── Section 1: Resource Estimates ──
    row += 2
    section_header(ws, row, "RESOURCE ESTIMATES — from generate_resource_estimates.py", ncols=3,
                   fill=C["tab_teal"], font_color=C["white"])
    row += 1
    ws_write(ws, row, 1, "Source: generate_resource_estimates.py — do not edit",
             italic=True, fill_color=C["section_bg"])
    ws.merge_cells(f"A{row}:C{row}")

    def _ro(r, label, val, fmt=None):
        """Read-only row."""
        ws_write(ws, r, 1, label, fill_color=C["section_bg"], border=True)
        ws_write(ws, r, 2, val, num_fmt=fmt, border=True)
        ws_write(ws, r, 3, "", fill_color=C["section_bg"], border=True)

    row += 1; _ro(row, "Primary Compute Type",     primary.get("type", ""))
    row += 1; _ro(row, "Primary Compute Count",    primary.get("count", 0))
    row += 1; _ro(row, "Primary Compute Monthly",  primary.get("monthly_cost_usd", 0), FMT_USD)
    row += 1; _ro(row, "Secondary Compute Type",   secondary.get("type", ""))
    row += 1; _ro(row, "Secondary Compute Count",  secondary.get("count", 0))
    row += 1; _ro(row, "Secondary Compute Monthly", secondary.get("monthly_cost_usd", 0), FMT_USD)
    row += 1; _ro(row, "Auto Scaling",             str(compute.get("auto_scaling", True)))
    row += 1; _ro(row, "Multi-AZ",                 str(compute.get("multi_az", True)))
    row += 1; _ro(row, "Storage Primary (TB)",     storage.get("primary_storage_tb", 0))
    row += 1; _ro(row, "Storage Type",             storage.get("primary_storage_type", ""))
    row += 1; _ro(row, "Storage Growth Rate",      storage.get("storage_growth_rate_pct", 0) / 100,
                  FMT_PCT)
    row += 1; _ro(row, "Storage Monthly",          storage.get("total_storage_monthly_usd", 0), FMT_USD)
    row += 1; _ro(row, "Database Engine",          database.get("engine", ""))
    row += 1; _ro(row, "Database Tier",            database.get("tier", ""))
    row += 1; _ro(row, "Database Monthly",         database.get("monthly_cost_usd", 0), FMT_USD)
    row += 1; _ro(row, "Network Bandwidth (Gbps)", network.get("bandwidth_gbps", 0))
    row += 1; _ro(row, "Network Monthly",          network.get("monthly_cost_usd", 0), FMT_USD)

    if add_svcs:
        row += 1
        ws_write(ws, row, 1, "Additional Services", bold=True,
                 fill_color=C["header_light"], border=True)
        ws_write(ws, row, 2, "Purpose", bold=True,
                 fill_color=C["header_light"], border=True)
        ws_write(ws, row, 3, "Monthly Cost", bold=True,
                 fill_color=C["header_light"], border=True, halign="right")
        for svc in add_svcs:
            row += 1
            ws_write(ws, row, 1, svc.get("service", ""), border=True)
            ws_write(ws, row, 2, svc.get("purpose", ""), border=True, wrap=True)
            ws_write(ws, row, 3, svc.get("monthly_cost_usd", 0), num_fmt=FMT_USD, border=True, halign="right")

    row += 1
    ws_write(ws, row, 1, "Total Monthly Consumption", bold=True,
             fill_color=C["positive_bg"], border=True)
    ws_write(ws, row, 2, mc.get("total_monthly_usd", 0), bold=True,
             num_fmt=FMT_USD, fill_color=C["positive_bg"], border=True)
    cm("total_monthly_usd", row)

    row += 1
    ws_write(ws, row, 1, "Total Annual Consumption", bold=True,
             fill_color=C["positive_bg"], border=True)
    ws_write(ws, row, 2, mc.get("total_annual_usd", 0), bold=True,
             num_fmt=FMT_USD, fill_color=C["positive_bg"], border=True)
    cm("total_annual_usd", row)

    # ── Section 2: Implementation Resources ──
    row += 2
    section_header(ws, row, "IMPLEMENTATION RESOURCES — editable", ncols=3,
                   fill=C["tab_grey"], font_color=C["white"])

    def _edit(r, label, val, fmt=None, assumed=False):
        ws_write(ws, r, 1, label, fill_color=C["section_bg"], border=True)
        fill = C["assumed_bg"] if assumed else C["input_bg"]
        ws_write(ws, r, 2, val, num_fmt=fmt, fill_color=fill,
                 font_color=C["input_blue"], border=True)
        if assumed:
            ws_write(ws, r, 3, "assumed", italic=True,
                     font_color="888888", fill_color=C["assumed_bg"], border=True)

    row += 1; _edit(row, "SA Days Required",      ir.get("sa_days_required", 0))
    cm("sa_days", row)
    row += 1; _edit(row, "ProServ Days Required", ir.get("proserv_days_required", 0))
    cm("proserv_days", row)
    row += 1; _edit(row, "Partner Days Required", ir.get("partner_days_required", 0))
    cm("partner_days", row)
    row += 1; _edit(row, "Timeline (weeks)",      ir.get("estimated_timeline_weeks", 0))
    cm("timeline_weeks", row)
    row += 1; _edit(row, "SA Day Rate",           fin["sa_day_rate"], FMT_USD, assumed=True)
    cm("sa_day_rate", row)
    row += 1; _edit(row, "ProServ Day Rate",      fin["proserv_day_rate"], FMT_USD, assumed=True)
    cm("proserv_day_rate", row)
    row += 1; _edit(row, "Partner Day Rate",      fin["partner_day_rate"], FMT_USD, assumed=True)
    cm("partner_day_rate", row)
    row += 1
    ws_write(ws, row, 1, "Total Implementation Cost", bold=True,
             fill_color=C["positive_bg"], border=True)
    ws_write(ws, row, 2, fin["total_implementation"], bold=True,
             num_fmt=FMT_USD, fill_color=C["positive_bg"], border=True)
    cm("total_implementation", row)

    # ── Section 3: Growth Rates ──
    row += 2
    section_header(ws, row, "GROWTH RATE ASSUMPTIONS — editable", ncols=3,
                   fill=C["tab_green"], font_color=C["white"])
    row += 1; _edit(row, "Year 2 Growth Rate", fin["year2_growth"], FMT_PCT, assumed=True)
    cm("year2_growth", row)
    row += 1; _edit(row, "Year 3 Growth Rate", fin["year3_growth"], FMT_PCT, assumed=True)
    cm("year3_growth", row)

    # ── Section 4: Scenarios ──
    row += 2
    section_header(ws, row, "SCENARIO MULTIPLIERS — editable", ncols=3,
                   fill=C["tab_amber"], font_color=C["white"])
    row += 1; _edit(row, "Conservative Multiplier", fin["scenario_conservative"], FMT_PCT)
    cm("scenario_conservative", row)
    row += 1; _edit(row, "Base Multiplier",         fin["scenario_base"],         FMT_PCT)
    cm("scenario_base", row)
    row += 1; _edit(row, "Optimistic Multiplier",   fin["scenario_optimistic"],   FMT_PCT)
    cm("scenario_optimistic", row)

    # ── Section 5: Commercial ──
    row += 2
    section_header(ws, row, "COMMERCIAL INPUTS — editable", ncols=3,
                   fill=C["tab_purple"], font_color=C["white"])
    row += 1; _edit(row, "Deal List Price",   fin["deal_amount"],  FMT_USD)
    cm("deal_amount", row)
    row += 1; _edit(row, "EDP Discount %",    fin["edp_pct"],       FMT_PCT, assumed=True)
    cm("edp_pct", row)

    return cell_map


# ── Tab 3 — Revenue Model ─────────────────────────────────────────────────────

def build_revenue_model(wb: openpyxl.Workbook, fin: dict) -> None:
    ws = wb.create_sheet("Revenue Model")
    ws.sheet_properties.tabColor = C["tab_green"]
    ws.freeze_panes = "B2"

    # Column widths
    ws.column_dimensions["A"].width = 32
    for col in ["B", "C", "D", "E"]:
        ws.column_dimensions[col].width = 16

    # Header
    ws.row_dimensions[1].height = 20
    for col, label in enumerate(["Metric", "Year 1", "Year 2", "Year 3", "Total"], 1):
        ws_write(ws, 1, col, label, bold=True, font_color=C["white"],
                 fill_color=C["header_dark"], halign="center" if col > 1 else "left")

    def hdr(r, label):
        section_header(ws, r, label, ncols=5, fill=C["header_light"], font_color="000000")

    def row_data(r, label, y1, y2, y3, total=None, bold=False, fmt=FMT_USD, show_total=True):
        ws_write(ws, r, 1, label, bold=bold, fill_color=C["section_bg"], border=True)
        for col, val in enumerate([y1, y2, y3], 2):
            fill = (C["positive_bg"] if val >= 0 else C["negative_bg"]) if fmt == FMT_USD else None
            ws_write(ws, r, col, val, bold=bold, num_fmt=fmt,
                     fill_color=fill, halign="right", border=True)
        tot = total if total is not None else (y1 + y2 + y3)
        if show_total:
            fill = (C["positive_bg"] if tot >= 0 else C["negative_bg"]) if fmt == FMT_USD else None
            ws_write(ws, r, 5, tot, bold=bold, num_fmt=fmt,
                     fill_color=fill, halign="right", border=True)
        else:
            ws_write(ws, r, 5, "", border=True)

    row = 2
    # SECTION A: Implementation costs
    hdr(row, "SECTION A — IMPLEMENTATION COSTS (one-time, Year 1 only)")
    row += 1
    row_data(row, "SA Fees",                fin["sa_cost"],       0, 0, show_total=False)
    row += 1
    row_data(row, "ProServ Fees",           fin["proserv_cost"],  0, 0, show_total=False)
    row += 1
    row_data(row, "Partner Fees",           fin["partner_cost"],  0, 0, show_total=False)
    row += 1
    row_data(row, "Internal Labor",         fin["internal_cost"], 0, 0, show_total=False)
    row += 1
    row_data(row, "Total Implementation",   fin["total_implementation"], 0, 0,
             total=fin["total_implementation"], bold=True)

    row += 1
    # SECTION B: Consumption
    hdr(row, "SECTION B — CONSUMPTION COSTS (recurring)")
    row += 1
    row_data(row, "Base Consumption", fin["year1_consumption"],
             fin["year2_consumption"], fin["year3_consumption"])
    row += 1
    row_data(row, "Year-over-Year Growth", fin["year2_growth"], fin["year3_growth"], 0,
             fmt=FMT_PCT, show_total=False)

    row += 1
    # SECTION C: Total costs
    hdr(row, "SECTION C — TOTAL COSTS")
    row += 1
    row_data(row, "Total Costs", fin["year1_total_cost"],
             fin["year2_total_cost"], fin["year3_total_cost"],
             total=fin["year1_total_cost"] + fin["year2_total_cost"] + fin["year3_total_cost"],
             bold=True)

    row += 1
    # SECTION D: Revenue
    hdr(row, "SECTION D — REVENUE / VALUE DELIVERED")
    row += 1
    row_data(row, "Revenue (40% / 35% / 25% split)",
             fin["year1_revenue"], fin["year2_revenue"], fin["year3_revenue"],
             total=fin["total_revenue"], bold=True)

    row += 1
    # SECTION E: Net
    hdr(row, "SECTION E — NET POSITION")
    row += 1
    row_data(row, "Net Year 1", fin["net_year1"], 0, 0, show_total=False)
    row += 1
    row_data(row, "Net Year 2", 0, fin["net_year2"], 0, show_total=False)
    row += 1
    row_data(row, "Net Year 3", 0, 0, fin["net_year3"], show_total=False)
    row += 1
    ws.row_dimensions[row].height = 20
    row_data(row, "CUMULATIVE 3-YEAR NET",
             fin["net_year1"], fin["net_year2"], fin["net_year3"],
             total=fin["cumulative_3yr"], bold=True)

    row += 1
    # SECTION F: Key metrics
    hdr(row, "SECTION F — KEY METRICS")
    row += 1
    ws_write(ws, row, 1, "Payback Period", fill_color=C["section_bg"], border=True)
    ws_write(ws, row, 2, f"{fin['payback_months']:.0f} months", bold=True,
             fill_color=C["input_bg"], border=True)
    ws.merge_cells(f"B{row}:E{row}")
    row += 1
    ws_write(ws, row, 1, "3-Year ROI", fill_color=C["section_bg"], border=True)
    ws_write(ws, row, 2, fin["roi_3yr_pct"] / 100, bold=True,
             num_fmt=FMT_PCT, fill_color=C["input_bg"], border=True)
    ws.merge_cells(f"B{row}:E{row}")
    row += 1
    ws_write(ws, row, 1, "NPV at 10%", fill_color=C["section_bg"], border=True)
    ws_write(ws, row, 2, fin["npv_10pct"], bold=True,
             num_fmt=FMT_USD, fill_color=C["input_bg"], border=True)
    ws.merge_cells(f"B{row}:E{row}")
    row += 1
    ws_write(ws, row, 1, "EDP Annual Savings", fill_color=C["section_bg"], border=True)
    ws_write(ws, row, 2, fin["edp_savings"], bold=True,
             num_fmt=FMT_USD, fill_color=C["positive_bg"], border=True)
    ws.merge_cells(f"B{row}:E{row}")


# ── Tab 4 — Scenarios ─────────────────────────────────────────────────────────

def build_scenarios(wb: openpyxl.Workbook, fin: dict) -> None:
    ws = wb.create_sheet("Scenarios")
    ws.sheet_properties.tabColor = C["tab_amber"]
    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 32
    for col in ["B", "C", "D"]:
        ws.column_dimensions[col].width = 18

    ws.row_dimensions[1].height = 20
    for col, label in enumerate(["Metric", "Conservative", "Base Case", "Optimistic"], 1):
        ws_write(ws, 1, col, label, bold=True, font_color=C["white"],
                 fill_color=C["header_dark"], halign="center" if col > 1 else "left")

    row = 2
    section_header(ws, row, "Revenue multipliers applied to base revenue. Cost lines are fixed.",
                   ncols=4, fill=C["section_bg"], font_color="000000")
    row += 1

    # Multipliers
    ws_write(ws, row, 1, "Revenue Multiplier", fill_color=C["section_bg"], border=True)
    for col, mult in enumerate([fin["scenario_conservative"], fin["scenario_base"], fin["scenario_optimistic"]], 2):
        ws_write(ws, row, col, mult, num_fmt=FMT_PCT,
                 fill_color=C["input_bg"], bold=True, halign="center", border=True)

    def scen_row(r, label, y1_base, costs, bold=False):
        ws_write(ws, r, 1, label, bold=bold, fill_color=C["section_bg"], border=True)
        for col, mult in enumerate([fin["scenario_conservative"], fin["scenario_base"], fin["scenario_optimistic"]], 2):
            val = y1_base * mult - costs
            fill = C["positive_bg"] if val >= 0 else C["negative_bg"]
            ws_write(ws, r, col, val, bold=bold, num_fmt=FMT_USD,
                     fill_color=fill, halign="right", border=True)

    def year_section(r, year_label, revenue, costs):
        section_header(ws, r, year_label, ncols=4,
                       fill=C["header_light"], font_color="000000")
        r += 1
        ws_write(ws, r, 1, "Revenue", fill_color=C["section_bg"], border=True)
        for col, mult in enumerate([fin["scenario_conservative"], fin["scenario_base"], fin["scenario_optimistic"]], 2):
            ws_write(ws, r, col, revenue * mult, num_fmt=FMT_USD,
                     fill_color=C["input_bg"], halign="right", border=True)
        r += 1
        ws_write(ws, r, 1, "Total Costs (fixed)", fill_color=C["section_bg"], border=True)
        for col in range(2, 5):
            ws_write(ws, r, col, costs, num_fmt=FMT_USD,
                     fill_color=C["negative_bg"], halign="right", border=True)
        r += 1
        scen_row(r, f"Net {year_label}", revenue, costs, bold=True)
        return r + 1

    row += 1
    row = year_section(row, "YEAR 1", fin["year1_revenue"], fin["year1_total_cost"])
    row += 1
    row = year_section(row, "YEAR 2", fin["year2_revenue"], fin["year2_total_cost"])
    row += 1
    row = year_section(row, "YEAR 3", fin["year3_revenue"], fin["year3_total_cost"])

    row += 1
    section_header(ws, row, "SUMMARY METRICS", ncols=4,
                   fill=C["header_dark"], font_color=C["white"])
    row += 1

    # Cumulative
    ws_write(ws, row, 1, "3-Year Cumulative Net", bold=True,
             fill_color=C["section_bg"], border=True)
    for col, mult in enumerate([fin["scenario_conservative"], fin["scenario_base"], fin["scenario_optimistic"]], 2):
        rev_cum = (fin["year1_revenue"] + fin["year2_revenue"] + fin["year3_revenue"]) * mult
        cost_cum = fin["year1_total_cost"] + fin["year2_total_cost"] + fin["year3_total_cost"]
        net_cum = rev_cum - cost_cum
        fill = C["positive_bg"] if net_cum >= 0 else C["negative_bg"]
        ws_write(ws, row, col, net_cum, bold=True, num_fmt=FMT_USD,
                 fill_color=fill, halign="right", border=True)

    row += 1
    ws_write(ws, row, 1, "3-Year ROI", fill_color=C["section_bg"], border=True)
    for col, mult in enumerate([fin["scenario_conservative"], fin["scenario_base"], fin["scenario_optimistic"]], 2):
        rev_cum = (fin["year1_revenue"] + fin["year2_revenue"] + fin["year3_revenue"]) * mult
        cost_cum = fin["year1_total_cost"] + fin["year2_total_cost"] + fin["year3_total_cost"]
        net_cum = rev_cum - cost_cum
        roi = net_cum / fin["total_implementation"] if fin["total_implementation"] > 0 else 0
        ws_write(ws, row, col, roi, num_fmt=FMT_PCT,
                 fill_color=C["input_bg"], halign="center", border=True)


# ── Tab 5 — Resource Estimation ───────────────────────────────────────────────

def build_resource_estimation(wb: openpyxl.Workbook, est: dict) -> None:
    ws = wb.create_sheet("Resource Estimation")
    ws.sheet_properties.tabColor = C["tab_teal"]
    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 28
    ws.column_dimensions["C"].width = 16

    mc = est["monthly_consumption"]
    ir = est["implementation_resources"]
    wp = est["workload_profile"]
    comp = est["compute"]
    stor = est["storage"]
    db   = est["database"]
    net  = est["network"]
    add_svcs = est.get("additional_services", [])
    primary  = comp.get("primary_instances", {})
    secondary = comp.get("secondary_instances", {})

    ws.row_dimensions[1].height = 20
    ws_write(ws, 1, 1, "RESOURCE ESTIMATION", bold=True, size=12,
             font_color=C["white"], fill_color=C["tab_teal"])
    ws.merge_cells("A1:C1")

    row = 2
    ws_write(ws, row, 1,
             f"Source: generate_resource_estimates.py  |  Estimated: {est.get('estimated_at','')[:10]}",
             italic=True, fill_color=C["section_bg"])
    ws.merge_cells(f"A{row}:C{row}")

    def _disp(r, label, val, fmt=None):
        ws_write(ws, r, 1, label, fill_color=C["teal_light"], border=True)
        ws_write(ws, r, 2, val, num_fmt=fmt, border=True, wrap=True)

    row += 2
    section_header(ws, row, "WORKLOAD PROFILE", ncols=3, fill=C["tab_teal"])
    row += 1
    _disp(row, "Workload Type",          wp.get("workload_type", ""))
    row += 1; _disp(row, "Daily Transactions",      wp.get("estimated_daily_transactions", 0), FMT_NUM)
    row += 1; _disp(row, "Peak RPS",                wp.get("estimated_peak_rps", 0), FMT_NUM)
    row += 1; _disp(row, "Data Volume",             f"{wp.get('estimated_data_volume_tb', 0)} TB")
    row += 1; _disp(row, "Active Users",            wp.get("estimated_active_users", 0), FMT_NUM)
    row += 1; _disp(row, "Availability Required",   wp.get("availability_requirement", ""))
    row += 1; _disp(row, "Compliance Flags",        ", ".join(wp.get("compliance_flags", [])))
    row += 1; _disp(row, "Confidence",              est.get("confidence", ""))
    row += 1
    ws_write(ws, row, 1, "Confidence Reasoning", fill_color=C["teal_light"], border=True)
    ws_write(ws, row, 2, est.get("confidence_reasoning", ""), border=True, wrap=True)
    ws.merge_cells(f"B{row}:C{row}")
    ws.row_dimensions[row].height = 36

    row += 2
    section_header(ws, row, "COMPUTE", ncols=3, fill=C["tab_teal"])
    row += 1; _disp(row, "Primary Instance Type",  primary.get("type", ""))
    row += 1; _disp(row, "Primary Count",          primary.get("count", 0))
    row += 1; _disp(row, "Primary Purpose",        primary.get("purpose", ""))
    row += 1; _disp(row, "Primary Monthly Cost",   primary.get("monthly_cost_usd", 0), FMT_USD)
    row += 1; _disp(row, "Secondary Instance Type", secondary.get("type", ""))
    row += 1; _disp(row, "Secondary Count",        secondary.get("count", 0))
    row += 1; _disp(row, "Secondary Purpose",      secondary.get("purpose", ""))
    row += 1; _disp(row, "Secondary Monthly Cost", secondary.get("monthly_cost_usd", 0), FMT_USD)
    row += 1; _disp(row, "Auto Scaling",           str(comp.get("auto_scaling", True)))
    row += 1; _disp(row, "Multi-AZ",               str(comp.get("multi_az", True)))
    row += 1; _disp(row, "Total Compute Monthly",  comp.get("total_compute_monthly_usd", 0), FMT_USD)

    row += 2
    section_header(ws, row, "STORAGE", ncols=3, fill=C["tab_teal"])
    row += 1; _disp(row, "Primary Storage",       f"{stor.get('primary_storage_tb',0)} TB ({stor.get('primary_storage_type','')})")
    row += 1; _disp(row, "Backup Storage",        f"{stor.get('backup_storage_tb',0)} TB")
    row += 1; _disp(row, "Archive Storage",       f"{stor.get('archive_storage_tb',0)} TB")
    row += 1; _disp(row, "Storage Growth Rate",   f"{stor.get('storage_growth_rate_pct',0)}% per year")
    row += 1; _disp(row, "Total Storage Monthly", stor.get("total_storage_monthly_usd", 0), FMT_USD)

    row += 2
    section_header(ws, row, "DATABASE", ncols=3, fill=C["tab_teal"])
    row += 1; _disp(row, "Engine",           db.get("engine", ""))
    row += 1; _disp(row, "Tier",             db.get("tier", ""))
    row += 1; _disp(row, "Multi-AZ",         str(db.get("multi_az", True)))
    row += 1; _disp(row, "Read Replicas",    db.get("read_replicas", 0))
    row += 1; _disp(row, "Storage",          f"{db.get('storage_gb',0)} GB")
    row += 1; _disp(row, "Monthly Cost",     db.get("monthly_cost_usd", 0), FMT_USD)

    row += 2
    section_header(ws, row, "NETWORK", ncols=3, fill=C["tab_teal"])
    row += 1; _disp(row, "Bandwidth",          f"{net.get('bandwidth_gbps',0)} Gbps")
    row += 1; _disp(row, "CDN Required",       str(net.get("cdn_required", False)))
    row += 1; _disp(row, "VPN/Direct Connect", str(net.get("vpn_or_direct_connect", False)))
    row += 1; _disp(row, "Load Balancer Type", net.get("load_balancer_type", ""))
    row += 1; _disp(row, "Monthly Cost",       net.get("monthly_cost_usd", 0), FMT_USD)

    row += 2
    section_header(ws, row, "ADDITIONAL SERVICES", ncols=3, fill=C["tab_teal"])
    row += 1
    ws_write(ws, row, 1, "Service", bold=True, fill_color=C["header_light"], border=True)
    ws_write(ws, row, 2, "Purpose", bold=True, fill_color=C["header_light"], border=True)
    ws_write(ws, row, 3, "Monthly Cost", bold=True, fill_color=C["header_light"],
             border=True, halign="right")
    svc_total = 0
    for svc in add_svcs:
        row += 1
        ws_write(ws, row, 1, svc.get("service", ""), border=True)
        ws_write(ws, row, 2, svc.get("purpose", ""), border=True, wrap=True)
        ws_write(ws, row, 3, svc.get("monthly_cost_usd", 0), num_fmt=FMT_USD,
                 border=True, halign="right")
        svc_total += svc.get("monthly_cost_usd", 0)
    row += 1
    ws_write(ws, row, 1, "Total Additional Services", bold=True,
             fill_color=C["positive_bg"], border=True)
    ws_write(ws, row, 3, svc_total, bold=True, num_fmt=FMT_USD,
             fill_color=C["positive_bg"], border=True, halign="right")

    row += 2
    section_header(ws, row, "MONTHLY CONSUMPTION SUMMARY", ncols=3, fill=C["tab_teal"])
    row += 1; _disp(row, "Compute",              mc.get("compute_usd", 0), FMT_USD)
    row += 1; _disp(row, "Storage",              mc.get("storage_usd", 0), FMT_USD)
    row += 1; _disp(row, "Database",             mc.get("database_usd", 0), FMT_USD)
    row += 1; _disp(row, "Network",              mc.get("network_usd", 0), FMT_USD)
    row += 1; _disp(row, "Additional Services",  mc.get("additional_services_usd", 0), FMT_USD)
    row += 1
    ws_write(ws, row, 1, "TOTAL MONTHLY", bold=True,
             fill_color=C["positive_bg"], border=True)
    ws_write(ws, row, 2, mc.get("total_monthly_usd", 0), bold=True,
             num_fmt=FMT_USD, fill_color=C["positive_bg"], border=True, halign="right")
    row += 1
    ws_write(ws, row, 1, "TOTAL ANNUAL", bold=True,
             fill_color=C["positive_bg"], border=True)
    ws_write(ws, row, 2, mc.get("total_annual_usd", 0), bold=True,
             num_fmt=FMT_USD, fill_color=C["positive_bg"], border=True, halign="right")

    row += 2
    section_header(ws, row, "IMPLEMENTATION RESOURCES", ncols=3, fill=C["tab_teal"])
    row += 1; _disp(row, "SA Days Required",     ir.get("sa_days_required", 0))
    row += 1; _disp(row, "ProServ Days",         ir.get("proserv_days_required", 0))
    row += 1; _disp(row, "Partner Days",         ir.get("partner_days_required", 0))
    row += 1; _disp(row, "Timeline",             f"{ir.get('estimated_timeline_weeks',0)} weeks")
    row += 1; _disp(row, "Implementation Cost",  ir.get("one_time_implementation_cost_usd", 0), FMT_USD)

    row += 2
    section_header(ws, row, "ARCHITECT NARRATIVE", ncols=3, fill=C["tab_teal"])
    row += 1
    ws_write(ws, row, 1, est.get("architect_narrative", ""), italic=True, wrap=True,
             fill_color=C["section_bg"], border=True)
    ws.merge_cells(f"A{row}:C{row}")
    ws.row_dimensions[row].height = 72


# ── Tab 6 — GTM & Commercial ──────────────────────────────────────────────────

def build_gtm_commercial(wb: openpyxl.Workbook, fin: dict,
                         pipeline_rec: dict, narration: dict) -> None:
    ws = wb.create_sheet("GTM & Commercial")
    ws.sheet_properties.tabColor = C["tab_purple"]
    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 18

    ws.row_dimensions[1].height = 20
    ws_write(ws, 1, 1, "GTM & COMMERCIAL", bold=True, size=12,
             font_color=C["white"], fill_color=C["tab_purple"])
    ws.merge_cells("A1:D1")

    row = 2
    # ── Section 1: Commitment Options ──
    section_header(ws, row, "COMMITMENT OPTION COMPARISON", ncols=4,
                   fill=C["tab_purple"], font_color=C["white"])
    row += 1
    for col, label in enumerate(["", "Month-to-Month", "1-Year Commit", "3-Year Commit"], 1):
        ws_write(ws, row, col, label, bold=True, fill_color=C["header_light"],
                 halign="center" if col > 1 else "left", border=True)

    def commit_row(r, label, mtm, one_yr, three_yr, fmt=FMT_USD, best_col=4):
        ws_write(ws, r, 1, label, fill_color=C["section_bg"], border=True)
        for col, val in enumerate([mtm, one_yr, three_yr], 2):
            fill = C["purple_light"] if col == best_col else None
            ws_write(ws, r, col, val, num_fmt=fmt, fill_color=fill,
                     halign="right", border=True)

    da = fin["deal_amount"]
    edp = fin["edp_pct"]
    three_pct = fin["threeyear_pct"]

    row += 1; commit_row(row, "Annual List Price",   da, da, da)
    row += 1; commit_row(row, "EDP Discount",        0, edp, three_pct, fmt=FMT_PCT)
    row += 1; commit_row(row, "Annual Discount $",   0, da * edp, da * three_pct)
    row += 1; commit_row(row, "Net Annual Price",    da, fin["net_price"], da * (1 - three_pct))
    row += 1; commit_row(row, "3-Year Total Cost",   da * 3, fin["oneyear_3yr_total"], fin["threeyear_3yr_total"])
    row += 1; commit_row(row, "3-Year Savings vs MTM", 0,
                         da * 3 - fin["oneyear_3yr_total"],
                         da * 3 - fin["threeyear_3yr_total"],
                         best_col=4)

    row += 2
    # ── Section 2: EDP Tier Analysis ──
    section_header(ws, row, "EDP TIER ANALYSIS", ncols=4,
                   fill=C["tab_purple"], font_color=C["white"])
    row += 1
    for label, val, fmt in [
        ("Current Commitment",     fin["deal_amount"],    FMT_USD),
        ("Current EDP Tier",       f"{fin['edp_pct']:.0%}", None),
        ("Current Annual Saving",  fin["edp_savings"],    FMT_USD),
        ("Next Tier Threshold",    fin["next_edp_threshold"], FMT_USD),
        ("Additional Needed",      fin["next_edp_threshold"] - fin["deal_amount"], FMT_USD),
        ("Next Tier Discount",     f"{fin['next_edp_rate']:.0%}", None),
        ("Incremental Saving",     fin["incremental_savings"], FMT_USD),
        ("Breakeven on Upgrade",   f"{fin['breakeven_months']:.0f} months", None),
    ]:
        ws_write(ws, row, 1, label, fill_color=C["section_bg"], border=True)
        ws_write(ws, row, 2, val, num_fmt=fmt, fill_color=C["purple_light"],
                 border=True, halign="right")
        ws.merge_cells(f"B{row}:D{row}")
        row += 1

    row += 1
    # ── Section 3: AE Talking Points ──
    section_header(ws, row, "AE TALKING POINTS", ncols=4,
                   fill=C["tab_purple"], font_color=C["white"])
    row += 1
    for pt in narration["ae_talking_points"].split("\n"):
        pt = pt.strip()
        if not pt:
            continue
        ws_write(ws, row, 1, pt, italic=True, wrap=True,
                 fill_color=C["purple_light"], border=True)
        ws.merge_cells(f"A{row}:D{row}")
        ws.row_dimensions[row].height = 28
        row += 1

    row += 1
    # ── Section 4: EDP Recommendation ──
    section_header(ws, row, "EDP RECOMMENDATION", ncols=4,
                   fill=C["tab_purple"], font_color=C["white"])
    row += 1
    ws_write(ws, row, 1, narration["edp_recommendation"], bold=True, wrap=True,
             fill_color=C["purple_light"], border=True, size=11)
    ws.merge_cells(f"A{row}:D{row}")
    ws.row_dimensions[row].height = 40

    row += 2
    # ── Section 5: Competitive Context ──
    section_header(ws, row, "COMPETITIVE CONTEXT", ncols=4,
                   fill=C["tab_purple"], font_color=C["white"])
    row += 1
    comp_evidence = pipeline_rec.get("elements", {}).get("competition", {}).get("evidence", "")
    if comp_evidence:
        ws_write(ws, row, 1, comp_evidence, wrap=True, border=True,
                 fill_color=C["section_bg"])
    else:
        ws_write(ws, row, 1, "No competitive intelligence captured in notes.",
                 italic=True, font_color="888888", border=True)
    ws.merge_cells(f"A{row}:D{row}")
    ws.row_dimensions[row].height = 36

    row += 2
    # ── Section 6: Deal Context ──
    section_header(ws, row, "DEAL CONTEXT", ncols=4,
                   fill=C["tab_purple"], font_color=C["white"])
    row += 1
    elems = pipeline_rec.get("elements", {})

    scored_at = pipeline_rec.get("scored_at", "")[:10]
    days_note = f"Scored {scored_at}" if scored_at else ""

    for label, val in [
        ("MEDDPICC Score",    pipeline_rec.get("overall_score", "")),
        ("Stage",             pipeline_rec.get("stage", "")),
        ("Probability",       f"{pipeline_rec.get('probability', 0)}%"),
        ("Assigned Rep",      pipeline_rec.get("assigned_rep", "")),
        ("Last Scored",       days_note),
        ("Pain Signal",       elems.get("identify_pain", {}).get("evidence", "")),
        ("Economic Buyer",    elems.get("economic_buyer", {}).get("evidence", "")),
    ]:
        ws_write(ws, row, 1, label, fill_color=C["section_bg"], border=True)
        ws_write(ws, row, 2, val, wrap=True, border=True)
        ws.merge_cells(f"B{row}:D{row}")
        ws.row_dimensions[row].height = 24 if "\n" not in str(val) and len(str(val)) < 80 else 40
        row += 1

    row += 1
    section_header(ws, row, "REP NEXT PRIORITY", ncols=4, fill=C["score_amber"])
    row += 1
    ws_write(ws, row, 1, pipeline_rec.get("next_priority", ""), bold=True, wrap=True,
             fill_color=C["assumed_bg"], border=True)
    ws.merge_cells(f"A{row}:D{row}")
    ws.row_dimensions[row].height = 40


# ── Workbook builder ──────────────────────────────────────────────────────────

def build_workbook(est: dict, fin: dict, pipeline_rec: dict,
                   narration: dict, out_path: Path) -> None:
    wb = openpyxl.Workbook()
    # Remove default sheet
    wb.remove(wb.active)

    build_dashboard(wb, est, fin, pipeline_rec, narration)
    build_assumptions(wb, est, fin)
    build_revenue_model(wb, fin)
    build_scenarios(wb, fin)
    build_resource_estimation(wb, est)
    build_gtm_commercial(wb, fin, pipeline_rec, narration)

    # Page setup for all sheets
    safe_name = est["deal_name"].replace("—", "-").replace("–", "-")
    today_str  = datetime.now().strftime("%d-%b-%Y")
    for ws in wb.worksheets:
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToPage   = True
        ws.page_setup.fitToWidth  = 1
        ws.sheet_view.zoomScale   = 90
        try:
            ws.oddHeader.left.text   = safe_name
            ws.oddHeader.right.text  = today_str
            ws.oddFooter.center.text = "Page &P of &N"
        except Exception:
            pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


# ── Progress helpers ──────────────────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "total_deals": 0, "models_generated": 0, "models_skipped": 0,
        "models_failed": 0, "total_implementation_usd": 0,
        "total_3yr_revenue_usd": 0, "payback_months_list": [],
        "roi_pct_list": [], "edp_pct_list": [],
        "started_at": now_iso(), "updated_at": now_iso(),
        "last_deal_processed": "",
    }


def save_progress(p: dict) -> None:
    p["updated_at"] = now_iso()
    # Compute averages
    if p["payback_months_list"]:
        p["avg_payback_months"] = sum(p["payback_months_list"]) / len(p["payback_months_list"])
    if p["roi_pct_list"]:
        p["avg_roi_pct"] = sum(p["roi_pct_list"]) / len(p["roi_pct_list"])
    if p["edp_pct_list"]:
        p["avg_edp_pct"] = sum(p["edp_pct_list"]) / len(p["edp_pct_list"])
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2)


def log_failure(deal_name: str, reason: str) -> None:
    data = json.load(open(FAILED_FILE)) if FAILED_FILE.exists() else []
    data.append({"deal_name": deal_name, "reason": reason, "at": now_iso()})
    with open(FAILED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def save_meta(est: dict, fin: dict, out_path: Path) -> None:
    meta = {
        "deal_id":   est.get("deal_id", ""),
        "deal_name": est.get("deal_name", ""),
        "company":   est.get("company", ""),
        "product":   est.get("product", ""),
        "generated_at": now_iso(),
        "resource_estimate_confidence": est.get("confidence", ""),
        "implementation_pattern": est.get("implementation_pattern", ""),
        "key_metrics": {
            "total_implementation_usd": round(fin["total_implementation"]),
            "year1_consumption_usd":    round(fin["year1_consumption"]),
            "payback_months":           round(fin["payback_months"], 1),
            "roi_3yr_pct":              round(fin["roi_3yr_pct"], 1),
            "npv_usd":                  round(fin["npv_10pct"]),
            "edp_discount_pct":         round(fin["edp_pct"], 4),
            "edp_savings_usd":          round(fin["edp_savings"]),
        },
        "file_path": str(out_path),
    }
    meta_path = out_path.with_suffix("").with_name(out_path.stem + "_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


# ── Test runner ───────────────────────────────────────────────────────────────

def run_tests(pipeline_idx: dict) -> bool:
    print("\n" + "=" * 50)
    print("FINANCIAL MODEL TEST RESULTS")
    print("=" * 50)

    # Test 1 — Alpine Financial / DataFabric POC (the sample)
    alpine_est_path = ESTIMATES_DIR / "alpine_financial_services_datafabric_proof_of_concept.json"
    # Test 2 — Largest deal in estimates
    est_files = list_resource_estimates()
    # Test 3 — Smallest
    est_files_asc = sorted(
        [p for p in ESTIMATES_DIR.glob("*.json") if not p.name.startswith("_")],
        key=lambda p: p.stat().st_size
    )

    tests = [
        ("Test 1 — Alpine / DataFabric POC (sample verification)", alpine_est_path if alpine_est_path.exists() else est_files[0]),
        ("Test 2 — Largest deal (Enterprise Scale)",                est_files[0]),
        ("Test 3 — Smallest deal (Quick Start)",                    est_files_asc[0]),
    ]

    all_passed = True
    for label, est_path in tests:
        print(f"\n{label}")
        try:
            est = load_estimate(est_path)
            deal_id = est.get("deal_id", "")
            pipeline_rec = pipeline_idx.get(deal_id, {
                "deal_id": deal_id, "deal_name": est.get("deal_name", ""),
                "company": est.get("company", ""), "stage": "", "amount": 0,
                "probability": 0, "assigned_rep": "", "overall_score": "NEEDS ATTENTION",
                "overall_reason": "", "next_priority": "", "elements": {}, "flags": {},
            })

            fin = compute_financials(est, pipeline_rec)

            # Verify for Test 1
            if "Alpine" in est.get("company", ""):
                mc = est["monthly_consumption"]
                assert mc["total_monthly_usd"] == 9500, f"Monthly should be 9500, got {mc['total_monthly_usd']}"
                assert mc["total_annual_usd"] == 114000, f"Annual should be 114000, got {mc['total_annual_usd']}"
                ir = est["implementation_resources"]
                assert ir["one_time_implementation_cost_usd"] == 99500, "Impl cost should be 99500"
                print(f"  Monthly consumption verified: ${mc['total_monthly_usd']:,.0f} ✅")
                print(f"  Annual consumption verified: ${mc['total_annual_usd']:,.0f} ✅")
                print(f"  Implementation cost: ${fin['total_implementation']:,.0f} ✅")

            narration = generate_narration(est, fin, pipeline_rec)

            out_path = OUTPUT_DIR / f"_test_{slugify(est['company'])}_{slugify(est['deal_name'])}.xlsx"
            build_workbook(est, fin, pipeline_rec, narration, out_path)

            print(f"  Deal:        {est['deal_name']}")
            print(f"  Company:     {est['company']}")
            print(f"  Pattern:     {est['implementation_pattern']}")
            print(f"  EDP:         {fin['edp_pct']:.0%}")
            print(f"  Payback:     {fin['payback_months']:.0f} months")
            print(f"  Excel:       {out_path.name}")
            print(f"  RESULT:      PASSED ✅")
        except Exception as e:
            import traceback
            print(f"  RESULT:      FAILED — {e}")
            traceback.print_exc()
            all_passed = False

    print("\n" + "=" * 50)
    print(f"Tests {'all passed' if all_passed else 'FAILED — fix before full run'}")
    print("=" * 50)
    return all_passed


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Deal Desk Financial Model Generator")
    parser.add_argument("--test",  action="store_true", help="Run 3 test deals only")
    parser.add_argument("--reset", action="store_true", help="Overwrite existing models")
    args = parser.parse_args()

    if not TOGETHER_API_KEY:
        print("ERROR: TOGETHER_API_KEY environment variable not set")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 50)
    print("FINANCIAL MODEL GENERATOR — LOADING DATA")
    print("=" * 50)

    pipeline_idx = load_pipeline()
    est_files    = list_resource_estimates()

    print(f"Pipeline deals:       {len(pipeline_idx)}")
    print(f"Resource estimates:   {len(est_files)}")
    print(f"Output:               {OUTPUT_DIR}")
    print("=" * 50)

    if args.test:
        run_tests(pipeline_idx)
        return

    # Run tests first
    print("\nRunning 3-deal validation...")
    if not run_tests(pipeline_idx):
        print("Tests failed — aborting full run.")
        sys.exit(1)

    print(f"\nStarting full run — {len(est_files)} deals...")
    progress = load_progress()
    progress["total_deals"] = len(est_files)

    for i, est_path in enumerate(est_files):
        try:
            est = load_estimate(est_path)
        except Exception as e:
            print(f"  [{i+1}/{len(est_files)}] SKIP (bad JSON) {est_path.name[:40]} — {e}")
            continue

        deal_name = est.get("deal_name", est_path.stem)
        company   = est.get("company", "")
        deal_id   = est.get("deal_id", "")

        out_path  = OUTPUT_DIR / f"{slugify(company)}_{slugify(deal_name)}.xlsx"

        if not args.reset and out_path.exists():
            progress["models_skipped"] = progress.get("models_skipped", 0) + 1
            progress["last_deal_processed"] = deal_name
            print(f"  [{i+1}/{len(est_files)}] SKIP  {deal_name[:55]}")
            save_progress(progress)
            continue

        print(f"  [{i+1}/{len(est_files)}] {deal_name[:55]:<55}", end="", flush=True)

        try:
            pipeline_rec = pipeline_idx.get(deal_id, {
                "deal_id": deal_id, "deal_name": deal_name,
                "company": company, "stage": "", "amount": 0,
                "probability": 0, "assigned_rep": "", "overall_score": "NEEDS ATTENTION",
                "overall_reason": "", "next_priority": "", "elements": {}, "flags": {},
            })

            # Try to get richer data from notes _meddpicc.json
            notes_rec = load_notes_meddpicc(company, deal_name)
            if notes_rec and not pipeline_rec.get("next_priority"):
                pipeline_rec["next_priority"] = notes_rec.get("next_priority", "")
            if notes_rec and not pipeline_rec.get("overall_reason"):
                pipeline_rec["overall_reason"] = notes_rec.get("overall_reason", "")

            fin = compute_financials(est, pipeline_rec)
            narration = generate_narration(est, fin, pipeline_rec)
            build_workbook(est, fin, pipeline_rec, narration, out_path)
            save_meta(est, fin, out_path)

            progress["models_generated"] = progress.get("models_generated", 0) + 1
            progress["total_implementation_usd"] = (
                progress.get("total_implementation_usd", 0) + fin["total_implementation"]
            )
            progress["total_3yr_revenue_usd"] = (
                progress.get("total_3yr_revenue_usd", 0) + fin["total_revenue"]
            )
            progress["payback_months_list"].append(fin["payback_months"])
            progress["roi_pct_list"].append(fin["roi_3yr_pct"])
            progress["edp_pct_list"].append(fin["edp_pct"])
            progress["last_deal_processed"] = deal_name
            save_progress(progress)

            print(f" → OK  payback={fin['payback_months']:.0f}mo  roi={fin['roi_3yr_pct']:.0f}%")

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f" → FAILED: {e}")
            log_failure(deal_name, f"{e}\n{tb}")
            progress["models_failed"] = progress.get("models_failed", 0) + 1
            progress["last_deal_processed"] = deal_name
            save_progress(progress)

        time.sleep(1)

    # Final report
    progress = load_progress()
    gen  = progress.get("models_generated", 0)
    skip = progress.get("models_skipped", 0)
    fail = progress.get("models_failed", 0)
    print("\n" + "=" * 50)
    print("FINANCIAL MODEL GENERATION COMPLETE")
    print("=" * 50)
    print(f"\nModels generated:     {gen}")
    print(f"Models skipped:       {skip}")
    print(f"Models failed:        {fail}")
    print(f"\nPortfolio Summary:")
    print(f"  Total implementation investment: ${progress.get('total_implementation_usd', 0):,.0f}")
    print(f"  Total 3-year revenue potential:  ${progress.get('total_3yr_revenue_usd', 0):,.0f}")
    print(f"  Average payback period:          {progress.get('avg_payback_months', 0):.0f} months")
    print(f"  Average 3-year ROI:              {progress.get('avg_roi_pct', 0):.0f}%")
    print(f"  Average EDP discount:            {progress.get('avg_edp_pct', 0):.0%}")
    print(f"\nFiles saved:")
    print(f"  {OUTPUT_DIR}/")
    xlsx_count = len(list(OUTPUT_DIR.glob("*.xlsx")))
    print(f"    {xlsx_count} Excel workbooks")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
