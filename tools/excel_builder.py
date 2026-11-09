"""
excel_builder.py
Builds a 5-tab Excel financial model using openpyxl.

Tab layout:
  1. Summary        — deal scorecard + 3 key numbers + model confidence
  2. Assumptions    — all editable inputs, colour-coded by confidence
  3. 3-Year Model   — Excel-formula driven cost / savings / net table
  4. Scenarios      — conservative / base / optimistic from Assumptions multipliers
  5. Commercial     — EDP tier analysis + commitment option comparison

Colour conventions (industry standard):
  Blue  font  = hardcoded input the user can change
  Black font  = formula / derived cell
  Green font  = cross-sheet reference formula
  Yellow bg   = assumed benchmark (mark for review)
  Light-blue  bg = input section headers
  Light-green bg = output / results sections
  Light-red   bg = at-risk flags
"""

from __future__ import annotations

try:
    from tools.llm_client import TOGETHER_CHAT_URL, TOGETHER_MODEL, request_completion
except ModuleNotFoundError:
    from llm_client import TOGETHER_CHAT_URL, TOGETHER_MODEL, request_completion

import re
from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import (
    Alignment, Border, Font, PatternFill, Side, numbers
)
from openpyxl.utils import get_column_letter

# ── Colour palette ────────────────────────────────────────────────────────
# Fonts
FONT_INPUT   = Font(name="Arial", color="000099", size=10)          # blue input
FONT_FORMULA = Font(name="Arial", color="000000", size=10)          # black formula
FONT_XREF    = Font(name="Arial", color="006400", size=10)          # green cross-sheet ref
FONT_HEADER  = Font(name="Arial", bold=True, color="FFFFFF", size=10)
FONT_SECTION = Font(name="Arial", bold=True, color="1F2937", size=10)
FONT_LABEL   = Font(name="Arial", color="4B5563", size=10)
FONT_TITLE   = Font(name="Arial", bold=True, color="000000", size=14)
FONT_BADGE   = Font(name="Arial", bold=True, color="FFFFFF", size=12)
FONT_DIM     = Font(name="Arial", color="9CA3AF", size=9, italic=True)

# Fills
FILL_HEADER     = PatternFill("solid", fgColor="1E3A5F")   # dark navy header
FILL_SECTION    = PatternFill("solid", fgColor="EFF6FF")   # light-blue section bg
FILL_OUTPUT     = PatternFill("solid", fgColor="F0FDF4")   # light-green output
FILL_ASSUMED    = PatternFill("solid", fgColor="FFFBEB")   # yellow — assumed
FILL_AT_RISK    = PatternFill("solid", fgColor="FEF2F2")   # light-red
FILL_STRONG     = PatternFill("solid", fgColor="DCFCE7")   # strong/positive
FILL_NEUTRAL    = PatternFill("solid", fgColor="F9FAFB")   # neutral grey
FILL_HIGHLIGHT  = PatternFill("solid", fgColor="DBEAFE")   # highlight blue
FILL_BADGE_GREEN = PatternFill("solid", fgColor="166534")
FILL_BADGE_AMBER = PatternFill("solid", fgColor="92400E")
FILL_BADGE_RED   = PatternFill("solid", fgColor="991B1B")
FILL_NARRATION  = PatternFill("solid", fgColor="F3F4F6")   # light grey narration

# Tab colours (Excel sheet tab colours via RGB hex)
TAB_SUMMARY    = "1E3A5F"
TAB_ASSUMPTIONS = "374151"
TAB_MODEL      = "166534"
TAB_SCENARIOS  = "92400E"
TAB_COMMERCIAL    = "5B21B6"
TAB_AZURE_ESTIMATE = "0078D4"   # Azure blue

# Azure service allocation by implementation pattern
# (service_name, description, unit_rate_or_None, unit_label, pct_of_y1_consumption)
_AZURE_SERVICE_MIX: dict[str, list[tuple]] = {
    "Quick Start": [
        ("Azure Virtual Machines", "D4s v3 (4 vCPU, 16 GB)",              0.192,  "$/hr",     0.40),
        ("Azure SQL Database",     "General Purpose, 2 vCores, LRS",      0.362,  "$/hr",     0.25),
        ("Azure Blob Storage",     "LRS Standard, ~2 TB/yr",              0.018,  "$/GB-mo",  0.12),
        ("Azure Data Transfer",    "Outbound, ~2 TB/mo",                  0.087,  "$/GB",     0.08),
        ("Other Azure Services",   "Azure Monitor, Entra ID, misc",       None,   "—",        0.15),
    ],
    "Production Grade": [
        ("Azure Virtual Machines", "D8s v3 (8 vCPU, 32 GB)",              0.384,  "$/hr",     0.40),
        ("Azure SQL Database",     "Business Critical, 4 vCores",         0.571,  "$/hr",     0.25),
        ("Azure Blob Storage",     "LRS + Cool tier",                     0.018,  "$/GB-mo",  0.10),
        ("Azure Functions",        "Approx 10M req/mo",                   None,   "—",        0.08),
        ("Azure Data Transfer",    "Outbound, ~10 TB/mo",                 0.087,  "$/GB",     0.07),
        ("Other Azure Services",   "Load Balancer, App Gateway, Monitor, misc", None, "—",   0.10),
    ],
    "Enterprise Scale": [
        ("Azure Virtual Machines", "D32s v3 cluster (×3 nodes)",          1.536,  "$/hr",     0.38),
        ("Azure SQL Managed Inst.", "Business Critical, 16 vCores",       2.240,  "$/hr",     0.22),
        ("Azure Cache for Redis",  "P3 Premium, 2-node cluster",          0.556,  "$/hr",     0.08),
        ("Azure Blob Storage",     "LRS + Archive tier",                  0.023,  "$/GB-mo",  0.08),
        ("Azure Functions",        "High-vol event processing",           None,   "—",        0.07),
        ("Azure Data Transfer",    "Outbound, ~50 TB/mo",                 0.087,  "$/GB",     0.07),
        ("Other Azure Services",   "AKS, DDoS Protection, App Gateway, misc", None, "—",    0.10),
    ],
}

# Number formats
FMT_USD  = '$#,##0'
FMT_USD2 = '$#,##0.00'
FMT_PCT  = '0.0%'
FMT_NUM  = '#,##0'
FMT_DASH = '#,##0;(#,##0);"-"'   # dash for zeros


def _thin_border() -> Border:
    s = Side(style="thin", color="D1D5DB")
    return Border(left=s, right=s, top=s, bottom=s)


def _bottom_border() -> Border:
    b = Side(style="thin", color="D1D5DB")
    return Border(bottom=b)


def _set_col_widths(ws, widths: dict[str, float]) -> None:
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def _section_row(ws, row: int, text: str, n_cols: int = 6, fill=FILL_SECTION) -> None:
    """Write a section header row spanning n_cols columns."""
    cell = ws.cell(row=row, column=1)
    cell.value = text
    cell.font  = FONT_SECTION
    cell.fill  = fill
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    for c in range(2, n_cols + 1):
        ws.cell(row=row, column=c).fill = fill
    ws.row_dimensions[row].height = 16


def _header_row(ws, row: int, labels: list[str], fills=None) -> None:
    fills = fills or [FILL_HEADER] * len(labels)
    for i, label in enumerate(labels):
        c = ws.cell(row=row, column=i + 1)
        c.value = label
        c.font  = FONT_HEADER
        c.fill  = fills[i]
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = _thin_border()
    ws.row_dimensions[row].height = 18


def _write_input_row(ws, row: int, label: str, col_b_value, fmt: str,
                     confidence: str, note: str = "", assumed=False) -> None:
    """Write a labelled input row with value in col B and confidence badge in col C."""
    # Label
    lc = ws.cell(row=row, column=1)
    lc.value     = label
    lc.font      = FONT_LABEL
    lc.alignment = Alignment(horizontal="left", vertical="center", indent=2)

    # Value cell
    vc = ws.cell(row=row, column=2)
    vc.value      = col_b_value
    vc.number_format = fmt
    vc.font       = FONT_INPUT
    vc.alignment  = Alignment(horizontal="right", vertical="center")
    if assumed:
        vc.fill = FILL_ASSUMED

    # Confidence badge
    cc = ws.cell(row=row, column=3)
    cc.value     = confidence.upper()
    cc.font      = Font(name="Arial", size=9, bold=True,
                        color="166534" if confidence == "stated"
                        else "92400E" if confidence == "inferred"
                        else "6B7280")
    cc.alignment = Alignment(horizontal="center", vertical="center")

    # Note
    if note:
        nc = ws.cell(row=row, column=4)
        nc.value     = note
        nc.font      = FONT_DIM
        nc.alignment = Alignment(horizontal="left", vertical="center")


def _write_formula_row(ws, row: int, label: str, formulas: list[str],
                       fmt: str, bold: bool = False,
                       section_fill=None, font_override=None) -> None:
    """Write a formula row: label in col A, Excel formulas in cols B–E."""
    lc = ws.cell(row=row, column=1)
    lc.value = label
    lc.font  = Font(name="Arial", bold=bold, color="1F2937", size=10)
    lc.alignment = Alignment(horizontal="left", vertical="center", indent=2)
    if section_fill:
        lc.fill = section_fill

    for i, formula in enumerate(formulas):
        c = ws.cell(row=row, column=i + 2)
        c.value          = formula
        c.number_format  = fmt
        _is_xref = isinstance(formula, str) and "!" in formula
        c.font           = font_override or (FONT_XREF if _is_xref else FONT_FORMULA)
        c.alignment      = Alignment(horizontal="right", vertical="center")
        if section_fill:
            c.fill = section_fill
        if bold:
            c.font = Font(name="Arial", bold=True,
                          color="006400" if _is_xref else "000000", size=10)


# ══════════════════════════════════════════════════════════════════════════
#   TAB 1 — SUMMARY
# ══════════════════════════════════════════════════════════════════════════

def _build_summary(ws, gathered: dict, model: dict, narration: dict) -> None:
    ws.sheet_properties.tabColor = TAB_SUMMARY
    ws.freeze_panes = "A2"
    _set_col_widths(ws, {"A": 32, "B": 20, "C": 18, "D": 22})

    deal_name     = gathered["deal_name"]
    company       = gathered["company"]
    product       = gathered["product"]
    rep           = gathered["assigned_rep"]
    score_label   = gathered["overall_score"]
    conf_score    = gathered["confidence_score"]
    conf_level    = gathered["confidence_level"]

    # Row 1 — title
    t = ws.cell(row=1, column=1)
    t.value = f"{company} — Deal Economics"
    t.font  = FONT_TITLE
    t.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 26

    d = ws.cell(row=1, column=3)
    d.value = f"Generated: {date.today().strftime('%B %d, %Y')}"
    d.font  = FONT_DIM
    d.alignment = Alignment(horizontal="right", vertical="center")

    # Row 2 — deal meta
    m = ws.cell(row=2, column=1)
    m.value = f"{deal_name}  ·  Rep: {rep}  ·  Product: {product}"
    m.font  = Font(name="Arial", size=10, color="4B5563")
    ws.row_dimensions[2].height = 14

    # Row 4 — MEDDPICC badge
    _section_row(ws, 4, "MEDDPICC QUALIFICATION SCORE", fill=FILL_SECTION)
    badge_fill = (FILL_BADGE_GREEN if "STRONG" in score_label.upper()
                  else FILL_BADGE_RED if "RISK" in score_label.upper()
                  else FILL_BADGE_AMBER)
    bc = ws.cell(row=5, column=1)
    bc.value = score_label.upper()
    bc.font  = FONT_BADGE
    bc.fill  = badge_fill
    bc.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[5].height = 22

    # Row 7 — THREE KEY NUMBERS
    ws.row_dimensions[6].height = 6
    _section_row(ws, 7, "THE BUSINESS CASE IN THREE NUMBERS", fill=FILL_HIGHLIGHT)

    rows_3 = [
        ("Annual cost of problem",   model["pain_annual"],     FMT_USD, "3-Year Model!B5"),
        ("Investment required Yr 1", model["y1_cost"],         FMT_USD, "3-Year Model!B17"),
        ("Payback period (months)",  model["payback_months"],  FMT_NUM, "3-Year Model!B34"),
    ]
    for i, (label, val, fmt, ref) in enumerate(rows_3):
        r = 8 + i
        lc = ws.cell(row=r, column=1)
        lc.value     = label
        lc.font      = FONT_LABEL
        lc.alignment = Alignment(horizontal="left", vertical="center", indent=2)

        vc = ws.cell(row=r, column=2)
        vc.value     = f"='{ref.split('!')[0]}'!{ref.split('!')[1]}"
        vc.number_format = fmt
        vc.font      = FONT_XREF
        vc.alignment = Alignment(horizontal="right", vertical="center")

    # Row 12 — THREE YEAR VIEW
    ws.row_dimensions[11].height = 6
    _section_row(ws, 12, "THREE YEAR NET POSITION", fill=FILL_OUTPUT)
    rows_yr = [
        ("Year 1 net",  "='3-Year Model'!B30", FMT_DASH),
        ("Year 2 net",  "='3-Year Model'!C30", FMT_DASH),
        ("Year 3 net",  "='3-Year Model'!D30", FMT_DASH),
        ("3-year ROI",  "='3-Year Model'!B35", FMT_PCT),
    ]
    for i, (label, formula, fmt) in enumerate(rows_yr):
        r = 13 + i
        lc = ws.cell(row=r, column=1)
        lc.value     = label
        lc.font      = FONT_LABEL
        lc.alignment = Alignment(horizontal="left", vertical="center", indent=2)
        vc = ws.cell(row=r, column=2)
        vc.value     = formula
        vc.number_format = fmt
        vc.font      = FONT_XREF
        vc.alignment = Alignment(horizontal="right", vertical="center")

    # Row 18 — MODEL CONFIDENCE
    ws.row_dimensions[17].height = 6
    _section_row(ws, 18, "MODEL CONFIDENCE", fill=FILL_SECTION)
    conf_data = [
        ("Confidence score",  f"{conf_score}/100", ""),
        ("Confidence level",  conf_level,           ""),
    ]
    for i, (label, val, _) in enumerate(conf_data):
        r = 19 + i
        lc = ws.cell(row=r, column=1)
        lc.value     = label
        lc.font      = FONT_LABEL
        lc.alignment = Alignment(horizontal="left", vertical="center", indent=2)
        vc = ws.cell(row=r, column=2)
        vc.value     = val
        badge_color = ("166534" if conf_level == "HIGH"
                       else "92400E" if conf_level == "MEDIUM"
                       else "991B1B")
        vc.font = Font(name="Arial", bold=True, color=badge_color, size=10)
        vc.alignment = Alignment(horizontal="right", vertical="center")

    # Input source breakdown
    inp = gathered["inputs"]
    ws.row_dimensions[21].height = 6
    _section_row(ws, 22, "DATA SOURCES", fill=FILL_SECTION)
    r = 23
    for key in ["pain_usd_annual", "deal_amount", "implementation_pattern",
                "time_savings_signal", "budget_signal", "company_employees"]:
        info = inp.get(key, {})
        conf_badge = info.get("confidence_level", "unknown")
        lc = ws.cell(row=r, column=1)
        lc.value     = key.replace("_", " ").title()
        lc.font      = FONT_LABEL
        lc.alignment = Alignment(horizontal="left", vertical="center", indent=2)
        sc = ws.cell(row=r, column=2)
        sc.value     = info.get("source", "—")
        sc.font      = FONT_DIM
        cc = ws.cell(row=r, column=3)
        cc.value     = conf_badge.upper()
        cc.font      = Font(name="Arial", size=9, bold=True,
                            color="166534" if conf_badge == "stated"
                            else "92400E" if conf_badge == "inferred"
                            else "6B7280")
        r += 1

    # Executive summary narration
    ws.row_dimensions[r].height = 6
    r += 1
    _section_row(ws, r, "EXECUTIVE SUMMARY", fill=FILL_NARRATION)
    r += 1
    exec_text = narration.get("executive_summary", "Financial model generated. See 3-Year Model tab for detailed projections.")
    nc = ws.cell(row=r, column=1)
    nc.value     = exec_text
    nc.font      = Font(name="Arial", italic=True, size=10, color="374151")
    nc.fill      = FILL_NARRATION
    nc.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[r].height = 55
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)

    # AE commercial story
    r += 2
    _section_row(ws, r, "AE COMMERCIAL STORY", fill=FILL_NARRATION)
    r += 1
    ae_text = narration.get("ae_commercial_story", "Reference payback period and ROI in pricing conversation.")
    ac = ws.cell(row=r, column=1)
    ac.value     = ae_text
    ac.font      = Font(name="Arial", italic=True, size=10, color="374151")
    ac.fill      = FILL_NARRATION
    ac.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[r].height = 35
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)


# ══════════════════════════════════════════════════════════════════════════
#   TAB 2 — ASSUMPTIONS
# ══════════════════════════════════════════════════════════════════════════

def _build_assumptions(ws, gathered: dict, model: dict) -> None:
    """
    Writes all editable input assumptions.
    Cell references used by Tab 3 (keep in sync with _MODEL_REFS dict below).
    """
    ws.sheet_properties.tabColor = TAB_ASSUMPTIONS
    ws.freeze_panes = "A2"
    _set_col_widths(ws, {"A": 35, "B": 18, "C": 14, "D": 40})

    _header_row(ws, 1, ["Input", "Value", "Confidence", "Source / Note"])

    inp = gathered["inputs"]

    def conf(key):
        return inp.get(key, {}).get("confidence_level", "assumed")

    def src(key):
        return inp.get(key, {}).get("source", "")

    r = 2

    # ── SECTION 1: CUSTOMER INPUTS ──
    _section_row(ws, r, "SECTION 1 — CUSTOMER INPUTS", n_cols=4)
    r += 1

    rows_cust = [
        ("Annual cost of problem ($)", model["pain_annual"],     FMT_USD, "pain_usd_annual",        False),
        ("Hours lost per week (hrs)",  model["hours_per_week"],  FMT_NUM, "hours_lost_per_week",     False),
        ("Headcount affected (FTE)",   model["headcount"],       FMT_NUM, "headcount_affected",      False),
        ("Error rate (%)",             model["error_rate_pct"],  FMT_PCT, "error_rate_pct",          False),
        ("Transactions per month",     model["txn_per_month"],   FMT_NUM, "transactions_per_month",  False),
        ("Current vendor cost ($)",    model["vendor_cost"],     FMT_USD, "current_vendor_cost_usd", model["vendor_cost"] == 0),
        ("Growth rate next 3yr (%)",   model["growth_rate_pct"], FMT_PCT, "growth_rate_pct",         False),
    ]
    for label, val, fmt, key, is_assumed in rows_cust:
        _write_input_row(ws, r, label, val, fmt, conf(key), src(key), assumed=is_assumed)
        r += 1

    r += 1  # blank

    # ── SECTION 2: IMPLEMENTATION INPUTS ──
    _section_row(ws, r, "SECTION 2 — IMPLEMENTATION INPUTS", n_cols=4)
    r += 1

    impl_pattern = gathered["impl_pattern"]
    rows_impl = [
        ("Implementation pattern",    impl_pattern,               "@",     "implementation_pattern", False),
        ("SA days",                   model["sa_days"],           FMT_NUM, "implementation_pattern", False),
        ("ProServ days",              model["proserv_days"],      FMT_NUM, "implementation_pattern", False),
        ("Partner days",              model["partner_days"],      FMT_NUM, "implementation_pattern", False),
        ("Timeline (weeks)",          model["timeline_weeks"],    FMT_NUM, "implementation_pattern", False),
        ("SA day rate ($)",           model["sa_day_rate"],       FMT_USD, "implementation_pattern", False),
        ("ProServ day rate ($)",      model["proserv_day_rate"],  FMT_USD, "implementation_pattern", False),
        ("Partner day rate ($)",      model["partner_day_rate"],  FMT_USD, "implementation_pattern", False),
        ("Loaded hourly rate ($)",    model["loaded_hourly"],     FMT_USD, "implementation_pattern", False),
        ("Migration cost ($)",        model["migration_cost"],    FMT_USD, "implementation_pattern", True),
        ("Training cost per head ($)", model["training_cost_per_head"], FMT_USD, "implementation_pattern", True),
    ]
    for label, val, fmt, key, is_assumed in rows_impl:
        _write_input_row(ws, r, label, val, fmt, "inferred", src(key), assumed=is_assumed)
        r += 1

    r += 1

    # ── SECTION 3: AZURE CONSUMPTION INPUTS ──
    _section_row(ws, r, "SECTION 3 — AZURE CONSUMPTION INPUTS", n_cols=4)
    r += 1

    rows_aws = [
        ("Year 1 consumption ($)",    model["y1_consumption"],  FMT_USD),
        ("Year 2 growth rate (%)",    model["y2_growth_rate"],  FMT_PCT),
        ("Year 3 growth rate (%)",    model["y3_growth_rate"],  FMT_PCT),
    ]
    for label, val, fmt in rows_aws:
        _write_input_row(ws, r, label, val, fmt, "inferred", "deal_amount × 60%")
        r += 1

    r += 1

    # ── SECTION 4: CONFIDENCE BAND INPUTS ──
    _section_row(ws, r, "SECTION 4 — CONFIDENCE BAND INPUTS", n_cols=4)
    r += 1

    rows_band = [
        ("Conservative multiplier", model["conservative_mult"], FMT_PCT),
        ("Base multiplier",         model["base_mult"],         FMT_PCT),
        ("Optimistic multiplier",   model["optimistic_mult"],   FMT_PCT),
    ]
    for label, val, fmt in rows_band:
        _write_input_row(ws, r, label, val, fmt, "assumed", f"confidence={gathered['confidence_level']}")
        r += 1

    r += 1

    # ── SECTION 5: DEAL INPUTS ──
    _section_row(ws, r, "SECTION 5 — DEAL INPUTS", n_cols=4)
    r += 1

    rows_deal = [
        ("Deal amount ($)",      model["deal_amount"], FMT_USD, "stated"),
        ("Azure EA discount rate (%)", model["edp_rate"],   FMT_PCT, "inferred"),
    ]
    for label, val, fmt, c in rows_deal:
        _write_input_row(ws, r, label, val, fmt, c, "crm.amount")
        r += 1

    # Store a note about the cell layout for reference
    ws.cell(row=r + 1, column=1).value = "← Blue cells are editable. Yellow cells use benchmark assumptions."
    ws.cell(row=r + 1, column=1).font  = FONT_DIM


# Track assumption cell addresses — the 3-Year Model tab formulas reference these.
# Format: {logical_name: "Assumptions!BN"}
def _build_assumption_cell_map() -> dict[str, str]:
    """
    Returns a mapping from logical name to cell address in the Assumptions sheet.
    Must stay in sync with _build_assumptions() row layout.

    Layout:
      Row 2  = SECTION 1 header; rows 3–9   = customer inputs (7 rows); row 10 = blank
      Row 11 = SECTION 2 header; row 12     = implementation pattern (text, not referenced by formulas)
               rows 13–22 = numerical impl inputs (10 rows); row 23 = blank
      Row 24 = SECTION 3 header; rows 25–27 = AWS consumption inputs; row 28 = blank
      Row 29 = SECTION 4 header; rows 30–32 = confidence band inputs; row 33 = blank
      Row 34 = SECTION 5 header; rows 35–36 = deal inputs
    """
    return {
        "pain_annual":          "Assumptions!B3",
        "hours_per_week":       "Assumptions!B4",
        "headcount":            "Assumptions!B5",
        "error_rate_pct":       "Assumptions!B6",
        "txn_per_month":        "Assumptions!B7",
        "vendor_cost":          "Assumptions!B8",
        "growth_rate_pct":      "Assumptions!B9",
        # impl — row 12 is the text "Implementation pattern", numerical inputs start at 13
        "sa_days":              "Assumptions!B13",
        "proserv_days":         "Assumptions!B14",
        "partner_days":         "Assumptions!B15",
        "timeline_weeks":       "Assumptions!B16",
        "sa_day_rate":          "Assumptions!B17",
        "proserv_day_rate":     "Assumptions!B18",
        "partner_day_rate":     "Assumptions!B19",
        "loaded_hourly":        "Assumptions!B20",
        "migration_cost":       "Assumptions!B21",
        "training_per_head":    "Assumptions!B22",
        # aws
        "y1_consumption":       "Assumptions!B25",
        "y2_growth":            "Assumptions!B26",
        "y3_growth":            "Assumptions!B27",
        # bands
        "conservative_mult":    "Assumptions!B30",
        "base_mult":            "Assumptions!B31",
        "optimistic_mult":      "Assumptions!B32",
        # deal
        "deal_amount":          "Assumptions!B35",
        "edp_rate":             "Assumptions!B36",
    }


# ══════════════════════════════════════════════════════════════════════════
#   TAB 3 — 3-YEAR MODEL
# ══════════════════════════════════════════════════════════════════════════

def _build_model(ws, model: dict) -> None:
    ws.sheet_properties.tabColor = TAB_MODEL
    ws.freeze_panes = "B4"
    _set_col_widths(ws, {"A": 36, "B": 18, "C": 18, "D": 18, "E": 18})

    # ── Column headers ───────────────────────────────────────────────────
    _header_row(ws, 1, ["", "Year 1", "Year 2", "Year 3", "3-Year Total"])
    ws.cell(row=1, column=1).value = "FINANCIAL MODEL"
    ws.cell(row=1, column=1).font  = FONT_HEADER

    # ── Helper: write a totals / sub-section row ─────────────────────────
    A = "Assumptions"   # short alias for sheet reference

    def formula(col: str, formula_str: str) -> str:
        return formula_str

    # Row reference map for Assumptions cells
    am = _build_assumption_cell_map()

    # ── SECTION A: COSTS ─────────────────────────────────────────────────
    _section_row(ws, 2, "SECTION A — COSTS", n_cols=5, fill=FILL_AT_RISK)

    # Implementation (one-time — only in Year 1)
    _section_row(ws, 3, "  Implementation (one-time)", n_cols=5, fill=FILL_NEUTRAL)

    cost_rows = [
        ("SA fees",                  [f"={am['sa_days']}*{am['sa_day_rate']}",           0, 0]),
        ("ProServ fees",             [f"={am['proserv_days']}*{am['proserv_day_rate']}",  0, 0]),
        ("Partner fees",             [f"={am['partner_days']}*{am['partner_day_rate']}",  0, 0]),
        ("Internal resource cost",   [f"={am['timeline_weeks']}*2*{am['loaded_hourly']}*40", 0, 0]),
        ("Migration cost",           [f"={am['migration_cost']}",                         0, 0]),
        ("Training cost",            [f"={am['training_per_head']}*{am['headcount']}",    0, 0]),
    ]
    start_impl_row = 4
    for i, (label, formulas) in enumerate(cost_rows):
        r = start_impl_row + i
        formulas.append(f"=B{r}")   # 3-year total = Year 1 only (impl is one-time)
        _write_formula_row(ws, r, f"    {label}", formulas, FMT_USD)

    impl_end = start_impl_row + len(cost_rows) - 1  # e.g. row 9
    total_impl_row = impl_end + 1                    # row 10
    _write_formula_row(ws, total_impl_row, "  Total Implementation",
                       [f"=SUM(B{start_impl_row}:B{impl_end})",
                        f"=SUM(C{start_impl_row}:C{impl_end})",
                        f"=SUM(D{start_impl_row}:D{impl_end})",
                        f"=B{total_impl_row}+C{total_impl_row}+D{total_impl_row}"],
                       FMT_USD, bold=True, section_fill=FILL_NEUTRAL)

    # Azure Consumption (recurring)
    aws_row = total_impl_row + 2   # blank gap + Azure row
    _section_row(ws, aws_row - 1, "  Azure Consumption (recurring)", n_cols=5, fill=FILL_NEUTRAL)
    _write_formula_row(ws, aws_row, "    Azure Consumption",
                       [f"={am['y1_consumption']}",
                        f"=B{aws_row}*(1+{am['y2_growth']})",
                        f"=C{aws_row}*(1+{am['y3_growth']})",
                        f"=B{aws_row}+C{aws_row}+D{aws_row}"],
                       FMT_USD)

    # Total Costs
    total_cost_row = aws_row + 2
    _write_formula_row(ws, total_cost_row, "TOTAL COSTS",
                       [f"=B{total_impl_row}+B{aws_row}",
                        f"=C{total_impl_row}+C{aws_row}",
                        f"=D{total_impl_row}+D{aws_row}",
                        f"=B{total_cost_row}+C{total_cost_row}+D{total_cost_row}"],
                       FMT_USD, bold=True, section_fill=FILL_AT_RISK)

    # ── SECTION B: SAVINGS ───────────────────────────────────────────────
    sav_start = total_cost_row + 2
    _section_row(ws, sav_start, "SECTION B — SAVINGS / BENEFITS", n_cols=5, fill=FILL_STRONG)

    _section_row(ws, sav_start + 1, "  Cost Avoidance", n_cols=5, fill=FILL_OUTPUT)
    pain_row   = sav_start + 2
    vendor_row = pain_row + 1
    error_row  = vendor_row + 1

    _write_formula_row(ws, pain_row, "    Pain reduction (70% / 95% / growth)",
                       [f"={am['pain_annual']}*0.7",
                        f"={am['pain_annual']}*0.95",
                        f"=C{pain_row}*(1+{am['growth_rate_pct']})",
                        f"=B{pain_row}+C{pain_row}+D{pain_row}"],
                       FMT_USD)
    _write_formula_row(ws, vendor_row, "    Vendor replacement savings",
                       [f"={am['vendor_cost']}",
                        f"={am['vendor_cost']}",
                        f"={am['vendor_cost']}",
                        f"=B{vendor_row}+C{vendor_row}+D{vendor_row}"],
                       FMT_USD)
    _write_formula_row(ws, error_row, "    Error reduction",
                       [f"={am['error_rate_pct']}*100*{am['txn_per_month']}*12*0.5",
                        f"=B{error_row}*(1+{am['growth_rate_pct']})",
                        f"=C{error_row}*(1+{am['growth_rate_pct']})",
                        f"=B{error_row}+C{error_row}+D{error_row}"],
                       FMT_USD)

    _section_row(ws, error_row + 1, "  Productivity Gains", n_cols=5, fill=FILL_OUTPUT)
    time_row = error_row + 2
    # If pain_annual is provided as an aggregate metric, time savings is already inside it — suppress to avoid double-count
    if model["pain_annual"] > 0 and model["time_savings_y1"] > 0:
        _write_formula_row(ws, time_row, "    Time savings (included in Pain $)",
                           [0, 0, 0, 0], FMT_USD)
    else:
        _write_formula_row(ws, time_row, "    Time savings",
                           [f"={am['hours_per_week']}*52*{am['loaded_hourly']}*{am['headcount']}",
                            f"=B{time_row}*(1+{am['growth_rate_pct']})",
                            f"=C{time_row}*(1+{am['growth_rate_pct']})",
                            f"=B{time_row}+C{time_row}+D{time_row}"],
                           FMT_USD)

    total_sav_row = time_row + 2
    _write_formula_row(ws, total_sav_row, "TOTAL SAVINGS",
                       [f"=B{pain_row}+B{vendor_row}+B{error_row}+B{time_row}",
                        f"=C{pain_row}+C{vendor_row}+C{error_row}+C{time_row}",
                        f"=D{pain_row}+D{vendor_row}+D{error_row}+D{time_row}",
                        f"=B{total_sav_row}+C{total_sav_row}+D{total_sav_row}"],
                       FMT_USD, bold=True, section_fill=FILL_STRONG)

    # ── SECTION C: NET POSITION ──────────────────────────────────────────
    net_start = total_sav_row + 2
    _section_row(ws, net_start, "SECTION C — NET POSITION", n_cols=5, fill=FILL_HIGHLIGHT)

    net_row  = net_start + 1
    cum_row  = net_row + 1

    _write_formula_row(ws, net_row, "  Net (Savings − Costs)",
                       [f"=B{total_sav_row}-B{total_cost_row}",
                        f"=C{total_sav_row}-C{total_cost_row}",
                        f"=D{total_sav_row}-D{total_cost_row}",
                        f"=B{net_row}+C{net_row}+D{net_row}"],
                       FMT_DASH, bold=True, section_fill=FILL_HIGHLIGHT)

    _write_formula_row(ws, cum_row, "  Cumulative net",
                       [f"=B{net_row}",
                        f"=B{cum_row}+C{net_row}",
                        f"=C{cum_row}+D{net_row}",
                        f"=D{cum_row}"],
                       FMT_DASH, section_fill=FILL_HIGHLIGHT)

    # ── SECTION D: KEY METRICS ───────────────────────────────────────────
    met_start = cum_row + 2
    _section_row(ws, met_start, "SECTION D — KEY METRICS", n_cols=5, fill=FILL_SECTION)

    pay_row = met_start + 1
    roi_row = pay_row + 1
    npv_row = roi_row + 1

    # Payback = total_impl / ((Y1 savings − Y1 consumption) / 12)
    _write_formula_row(ws, pay_row, "  Payback period (months)",
                       [f"=IFERROR(B{total_cost_row}/((B{total_sav_row}-B{aws_row})/12),999)",
                        "", "", ""],
                       FMT_NUM, bold=True, section_fill=FILL_SECTION)

    _write_formula_row(ws, roi_row, "  3-Year ROI (%)",
                       [f"=IFERROR(E{net_row}/E{total_cost_row},0)",
                        "", "", ""],
                       FMT_PCT, bold=True, section_fill=FILL_SECTION)

    _write_formula_row(ws, npv_row, "  NPV at 10% discount rate",
                       [f"=NPV(0.1,B{net_row},C{net_row},D{net_row})",
                        "", "", ""],
                       FMT_USD, bold=True, section_fill=FILL_SECTION)

    # Expose row numbers for other tabs to reference
    ws._deal_rows = {
        "total_impl_row":  total_impl_row,
        "total_cost_row":  total_cost_row,
        "total_sav_row":   total_sav_row,
        "net_row":         net_row,
        "cum_row":         cum_row,
        "pay_row":         pay_row,
        "roi_row":         roi_row,
        "npv_row":         npv_row,
        "aws_row":         aws_row,
    }


# ══════════════════════════════════════════════════════════════════════════
#   TAB 4 — SCENARIOS
# ══════════════════════════════════════════════════════════════════════════

def _build_scenarios(ws, model: dict, model_ws_rows: dict) -> None:
    ws.sheet_properties.tabColor = TAB_SCENARIOS
    ws.freeze_panes = "A3"
    _set_col_widths(ws, {"A": 42, "B": 18, "C": 18, "D": 18})

    am = _build_assumption_cell_map()
    mr = model_ws_rows
    total_sav_row  = mr["total_sav_row"]
    total_cost_row = mr["total_cost_row"]
    aws_row        = mr["aws_row"]

    # ── Row 1: Header ────────────────────────────────────────────────────
    _header_row(ws, 1, ["SCENARIO ANALYSIS", "Conservative", "Base Case", "Optimistic"])
    ws.cell(row=1, column=1).font = FONT_HEADER

    # ── Row 2-3: Multipliers (pull from Assumptions) ─────────────────────
    _section_row(ws, 2, "Savings multipliers (adjust in Assumptions tab)", n_cols=4, fill=FILL_NEUTRAL)
    ws.cell(row=3, column=1).value = "Multiplier"
    ws.cell(row=3, column=1).font  = FONT_LABEL
    for i, cell in enumerate([am["conservative_mult"], am["base_mult"], am["optimistic_mult"]]):
        c = ws.cell(row=3, column=i + 2)
        c.value         = f"={cell}"
        c.number_format = FMT_PCT
        c.font          = FONT_XREF
        c.alignment     = Alignment(horizontal="center")

    # ── Row 5-12: Values used in calculations ────────────────────────────
    _section_row(ws, 5, "VALUES USED IN CALCULATIONS", n_cols=4, fill=FILL_SECTION)
    lbl = ws.cell(row=6, column=1)
    lbl.value = "  From '3-Year Model' Sheet:"
    lbl.font  = FONT_DIM
    ws.cell(row=6, column=3).value = "Formula Used"
    ws.cell(row=6, column=3).font  = FONT_DIM

    src_rows = [
        ("  Year 1 Savings", f"='3-Year Model'!B{total_sav_row}",  "← Total Savings Year 1"),
        ("  Year 2 Savings", f"='3-Year Model'!C{total_sav_row}",  "← Total Savings Year 2"),
        ("  Year 3 Savings", f"='3-Year Model'!D{total_sav_row}",  "← Total Savings Year 3"),
        ("  Year 1 Costs",   f"='3-Year Model'!B{total_cost_row}", "← Total Costs Y1 (Impl + Azure)"),
        ("  Year 2 Costs",   f"='3-Year Model'!C{total_cost_row}", "← Total Costs Y2 (Azure only)"),
        ("  Year 3 Costs",   f"='3-Year Model'!D{total_cost_row}", "← Total Costs Y3 (Azure only)"),
    ]
    for i, (label, formula, note_text) in enumerate(src_rows):
        r = 7 + i
        lc = ws.cell(row=r, column=1)
        lc.value = label
        lc.font  = FONT_LABEL
        lc.alignment = Alignment(horizontal="left", vertical="center")
        vc = ws.cell(row=r, column=2)
        vc.value         = formula
        vc.number_format = FMT_USD
        vc.font          = FONT_XREF
        vc.alignment     = Alignment(horizontal="right", vertical="center")
        nc = ws.cell(row=r, column=3)
        nc.value = note_text
        nc.font  = FONT_DIM
        nc.alignment = Alignment(horizontal="left", vertical="center")

    # ── Row 14-18: Annual Net Position ────────────────────────────────────
    # Net = Savings (col B only) × Multiplier (row 3 per column) − Costs (col B only)
    _section_row(ws, 14, "ANNUAL NET POSITION BY SCENARIO", n_cols=4, fill=FILL_HIGHLIGHT)
    calc_lbl = ws.cell(row=15, column=1)
    calc_lbl.value = "  Calculation: Savings × Multiplier − Costs"
    calc_lbl.font  = FONT_DIM
    ws.merge_cells(start_row=15, start_column=1, end_row=15, end_column=4)

    for i, (label, sav_r, cost_r) in enumerate([
        ("Year 1 net", 7,  10),
        ("Year 2 net", 8,  11),
        ("Year 3 net", 9,  12),
    ]):
        r = 16 + i
        lc = ws.cell(row=r, column=1)
        lc.value = label
        lc.font  = FONT_LABEL
        lc.alignment = Alignment(horizontal="left", vertical="center", indent=2)
        for j, col_letter in enumerate(["B", "C", "D"]):
            c = ws.cell(row=r, column=j + 2)
            c.value         = f"=B{sav_r}*{col_letter}3-B{cost_r}"
            c.number_format = FMT_DASH
            c.font          = FONT_XREF
            c.alignment     = Alignment(horizontal="right", vertical="center")

    # ── Row 19-21: Key Metrics ────────────────────────────────────────────
    _section_row(ws, 19, "KEY METRICS BY SCENARIO", n_cols=4, fill=FILL_SECTION)
    aws_ref       = f"'3-Year Model'!B{aws_row}"
    total_cost_e  = f"'3-Year Model'!E{total_cost_row}"
    for i, (label, fmt) in enumerate([
        ("Payback period (months)", FMT_NUM),
        ("3-year ROI (%)",          FMT_PCT),
    ]):
        r = 20 + i
        lc = ws.cell(row=r, column=1)
        lc.value = label
        lc.font  = FONT_LABEL
        lc.alignment = Alignment(horizontal="left", vertical="center", indent=2)
        for j, col_letter in enumerate(["B", "C", "D"]):
            c = ws.cell(row=r, column=j + 2)
            c.font          = FONT_XREF
            c.number_format = fmt
            c.alignment     = Alignment(horizontal="right", vertical="center")
            if "Payback" in label:
                # numerator = Y1 total costs (B10); denominator = (Y1 savings × mult − AWS) / 12
                c.value = f"=IFERROR(B10/((B7*{col_letter}3-{aws_ref})/12),999)"
            else:
                # ROI = sum of 3-yr scenario net / total 3-yr costs
                c.value = f"=IFERROR(SUM({col_letter}16:{col_letter}18)/{total_cost_e},0)"

    # ── Row 23: Footer note ───────────────────────────────────────────────
    nc = ws.cell(row=23, column=1)
    nc.value = "Adjust Conservative / Optimistic multipliers in Assumptions tab → all scenario columns update automatically."
    nc.font  = FONT_DIM
    ws.merge_cells(start_row=23, start_column=1, end_row=23, end_column=4)


# ══════════════════════════════════════════════════════════════════════════
#   TAB 5 — AZURE ESTIMATE
# ══════════════════════════════════════════════════════════════════════════

def _build_azure_estimate(ws, gathered: dict, model: dict) -> None:
    """
    SKU-level Azure consumption breakdown that ties to Assumptions!B25 (y1_consumption).
    Monthly costs are derived proportionally; unit rates are editable reference pricing.
    """
    ws.sheet_properties.tabColor = TAB_AZURE_ESTIMATE
    ws.freeze_panes = "A4"
    _set_col_widths(ws, {"A": 26, "B": 34, "C": 14, "D": 10, "E": 16, "F": 16, "G": 10})

    am           = _build_assumption_cell_map()
    impl_pattern = gathered.get("impl_pattern", "Production Grade")
    services     = _AZURE_SERVICE_MIX.get(impl_pattern, _AZURE_SERVICE_MIX["Production Grade"])

    # ── Row 1-2: Header ──────────────────────────────────────────────────
    _header_row(ws, 1, ["AZURE COST ESTIMATE", "SKU / Description", "Unit Rate", "Unit",
                         "Monthly Cost ($)", "Annual Cost ($)", "% of Y1"])
    ws.cell(row=1, column=1).font = FONT_HEADER
    _section_row(ws, 2,
                 f"Pattern: {impl_pattern}  ·  Unit rates are reference Azure list pricing (blue = editable).",
                 n_cols=7, fill=FILL_SECTION)

    # ── Row 3: Column sub-headers ────────────────────────────────────────
    for col, label in enumerate(["Service", "SKU / Description", "Unit Rate", "Unit",
                                  "Monthly Cost ($)", "Annual Cost ($)", "% of Y1"], 1):
        c = ws.cell(row=3, column=col)
        c.value     = label
        c.font      = Font(name="Arial", bold=True, size=9, color="FFFFFF")
        c.fill      = FILL_HEADER
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border    = _thin_border()
    ws.row_dimensions[3].height = 16

    # ── Rows 4+: Service breakdown ───────────────────────────────────────
    data_start = 4
    for i, (svc, desc, unit_rate, unit_lbl, pct) in enumerate(services):
        r = data_start + i

        lc = ws.cell(row=r, column=1)
        lc.value     = svc
        lc.font      = FONT_LABEL
        lc.alignment = Alignment(horizontal="left", vertical="center", indent=1)

        bc = ws.cell(row=r, column=2)
        bc.value     = desc
        bc.font      = FONT_DIM
        bc.alignment = Alignment(horizontal="left", vertical="center")

        # Unit rate — blue (user-editable) when a numeric reference price exists
        cc = ws.cell(row=r, column=3)
        if unit_rate is not None:
            cc.value         = unit_rate
            cc.number_format = FMT_USD2
            cc.font          = FONT_INPUT
        else:
            cc.value = "varies"
            cc.font  = FONT_DIM
        cc.alignment = Alignment(horizontal="right", vertical="center")

        dc = ws.cell(row=r, column=4)
        dc.value     = unit_lbl
        dc.font      = FONT_DIM
        dc.alignment = Alignment(horizontal="center", vertical="center")

        # Monthly cost derived from Assumptions y1_consumption × pct allocation
        ec = ws.cell(row=r, column=5)
        ec.value         = f"={am['y1_consumption']}/12*{pct}"
        ec.number_format = FMT_USD
        ec.font          = FONT_FORMULA
        ec.alignment     = Alignment(horizontal="right", vertical="center")

        fc = ws.cell(row=r, column=6)
        fc.value         = f"=E{r}*12"
        fc.number_format = FMT_USD
        fc.font          = FONT_FORMULA
        fc.alignment     = Alignment(horizontal="right", vertical="center")

        gc = ws.cell(row=r, column=7)
        gc.value         = pct
        gc.number_format = FMT_PCT
        gc.font          = FONT_DIM
        gc.alignment     = Alignment(horizontal="center", vertical="center")

    data_end  = data_start + len(services) - 1
    total_row = data_end + 1

    # ── Total row ─────────────────────────────────────────────────────────
    tc = ws.cell(row=total_row, column=1)
    tc.value = "TOTAL"
    tc.font  = Font(name="Arial", bold=True, size=10)
    for col, formula, fmt in [
        (5, f"=SUM(E{data_start}:E{data_end})", FMT_USD),
        (6, f"=SUM(F{data_start}:F{data_end})", FMT_USD),
        (7, f"=SUM(G{data_start}:G{data_end})", FMT_PCT),
    ]:
        c = ws.cell(row=total_row, column=col)
        c.value          = formula
        c.number_format  = fmt
        c.font           = Font(name="Arial", bold=True, size=10)
        c.alignment      = Alignment(horizontal="right")

    # ── Validation: ties to Assumptions B25 ──────────────────────────────
    r = total_row + 2
    _section_row(ws, r, "VALIDATES AGAINST ASSUMPTIONS — SECTION 3 (Azure Consumption)", n_cols=7, fill=FILL_OUTPUT)
    r += 1
    for label, formula in [
        ("Assumptions Y1 Consumption (B25)", f"={am['y1_consumption']}"),
        ("This estimate  (annual total)",     f"=F{total_row}"),
        ("Variance  (should be $0)",          f"=F{total_row}-{am['y1_consumption']}"),
    ]:
        lc = ws.cell(row=r, column=1)
        lc.value     = label
        lc.font      = FONT_LABEL
        lc.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        vc = ws.cell(row=r, column=6)
        vc.value         = formula
        vc.number_format = FMT_USD
        vc.font          = FONT_XREF
        vc.alignment     = Alignment(horizontal="right")
        r += 1

    # ── 3-Year consumption projection ─────────────────────────────────────
    r += 1
    _section_row(ws, r, "3-YEAR CONSUMPTION PROJECTION", n_cols=7, fill=FILL_STRONG)
    r += 1
    _header_row(ws, r, ["", "Year 1", "Year 2", "Year 3", "3-Year Total", "", ""])
    r += 1

    lc = ws.cell(row=r, column=1)
    lc.value = "  Azure Consumption ($)"
    lc.font  = FONT_LABEL
    y1_f = f"={am['y1_consumption']}"
    y2_f = f"={am['y1_consumption']}*(1+{am['y2_growth']})"
    y3_f = f"={am['y1_consumption']}*(1+{am['y2_growth']})*(1+{am['y3_growth']})"
    for j, formula in enumerate([y1_f, y2_f, y3_f]):
        c = ws.cell(row=r, column=j + 2)
        c.value         = formula
        c.number_format = FMT_USD
        c.font          = FONT_XREF
        c.alignment     = Alignment(horizontal="right")
    tot = ws.cell(row=r, column=5)
    tot.value         = f"=B{r}+C{r}+D{r}"
    tot.number_format = FMT_USD
    tot.font          = Font(name="Arial", bold=True, size=10)
    tot.alignment     = Alignment(horizontal="right")

    r += 1
    lg = ws.cell(row=r, column=1)
    lg.value = "  Growth rate"
    lg.font  = FONT_LABEL
    ws.cell(row=r, column=2).value = "—"
    ws.cell(row=r, column=2).font  = FONT_DIM
    for j, formula in enumerate([f"={am['y2_growth']}", f"={am['y3_growth']}"]):
        c = ws.cell(row=r, column=j + 3)
        c.value         = formula
        c.number_format = FMT_PCT
        c.font          = FONT_XREF
        c.alignment     = Alignment(horizontal="right")

    r += 2
    note = ws.cell(row=r, column=1)
    note.value = ("Note: Monthly costs are derived proportionally from the Y1 Consumption figure in Assumptions!B25 "
                  "and update automatically when inputs change. Unit rates shown are Azure list pricing (eastus region) for reference — "
                  "adjust blue cells if EA/contract rates differ.")
    note.font      = FONT_DIM
    note.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[r].height = 32
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)


# ══════════════════════════════════════════════════════════════════════════
#   TAB 6 — COMMERCIAL
# ══════════════════════════════════════════════════════════════════════════

def _build_commercial(ws, model: dict, narration: dict) -> None:
    ws.sheet_properties.tabColor = TAB_COMMERCIAL
    ws.freeze_panes = "A2"
    _set_col_widths(ws, {"A": 36, "B": 20, "C": 20, "D": 20})

    am = _build_assumption_cell_map()

    _header_row(ws, 1, ["", "Month-to-Month", "1-Year Commit", "3-Year Commit"])
    ws.cell(row=1, column=1).value = "COMMERCIAL OPTIONS"
    ws.cell(row=1, column=1).font  = FONT_HEADER

    # ── SECTION 1: COMMITMENT COMPARISON ───────────────────────────────
    _section_row(ws, 2, "SECTION 1 — COMMITMENT OPTION COMPARISON", n_cols=4, fill=FILL_HIGHLIGHT)

    # MTM = no discount; 1yr = EDP discount; 3yr = EDP + 3%
    rows_commit = [
        ("Annual price (list $)",         [f"={am['deal_amount']}",
                                          f"={am['deal_amount']}",
                                          f"={am['deal_amount']}"]),
        ("Azure EA discount applied (%)", [f"=0",
                                          f"={am['edp_rate']}",
                                          f"=MIN({am['edp_rate']}+0.03,0.20)"]),
        ("Discount (%)",            [f"=B4",
                                     f"=C4",
                                     f"=D4"]),
        ("Net annual price ($)",    [f"={am['deal_amount']}*(1-B4)",
                                     f"={am['deal_amount']}*(1-C4)",
                                     f"={am['deal_amount']}*(1-D4)"]),
        ("3-year total cost ($)",   [f"=B6*3", f"=C6*3", f"=D6*3"]),
        ("3-year savings vs MTM ($)",[f"=0",   f"=B7-C7", f"=B7-D7"]),
    ]

    for i, (label, formulas) in enumerate(rows_commit):
        r = 3 + i
        lc = ws.cell(row=r, column=1)
        lc.value     = label
        lc.font      = FONT_LABEL
        lc.alignment = Alignment(horizontal="left", vertical="center", indent=2)
        for j, f in enumerate(formulas):
            c = ws.cell(row=r, column=j + 2)
            c.value         = f
            c.number_format = FMT_USD if "$" in label else FMT_PCT
            c.font          = FONT_FORMULA
            c.alignment     = Alignment(horizontal="right")

    # ── SECTION 2: EDP TIER ANALYSIS ────────────────────────────────────
    _section_row(ws, 10, "SECTION 2 — AZURE EA TIER ANALYSIS", n_cols=4, fill=FILL_SECTION)

    # Section 2 rows B11:B20 — formulas from Assumptions where possible;
    # next-tier threshold and rate are tier-table lookups written as values
    edp_rows = [
        ("Current annual commitment ($)",      f"={am['deal_amount']}",         FMT_USD),  # B11
        ("Current Azure EA discount (%)",      f"={am['edp_rate']}",            FMT_PCT),  # B12
        ("Current annual EA savings ($)",      "=B11*B12",                      FMT_USD),  # B13
        ("Next tier threshold ($)",            model["next_edp_threshold"],     FMT_USD),  # B14
        ("Additional commitment needed ($)",   "=MAX(0,B14-B11)",               FMT_USD),  # B15
        ("Next tier EA discount (%)",           model["next_edp_rate"],          FMT_PCT),  # B16
        ("Additional annual saving ($)",       "=B11*(B16-B12)",                FMT_USD),  # B17
        ("Savings at next tier ($)",           "=B14*B16",                      FMT_USD),  # B18
        ("Incremental savings vs current ($)", "=(B14*B16)-(B11*B12)",          FMT_USD),  # B19
        ("% of next tier already committed",   "=B11/B14",                      FMT_PCT),  # B20
    ]
    for i, (label, val, fmt) in enumerate(edp_rows):
        r = 11 + i
        lc = ws.cell(row=r, column=1)
        lc.value     = label
        lc.font      = FONT_LABEL
        lc.alignment = Alignment(horizontal="left", vertical="center", indent=2)
        vc = ws.cell(row=r, column=2)
        vc.value          = val
        vc.number_format  = fmt
        vc.font           = FONT_FORMULA if isinstance(val, str) and val.startswith("=") else FONT_INPUT
        vc.alignment      = Alignment(horizontal="right")

    # ── SECTION 3: EDP RECOMMENDATION ───────────────────────────────────
    rec_row = 22  # rows 11-20 = data, 21 = blank, 22 = section header
    _section_row(ws, rec_row, "SECTION 3 — AZURE EA RECOMMENDATION", n_cols=4, fill=FILL_NARRATION)
    rec_text = narration.get("edp_recommendation",
                             "Evaluate commitment tier upgrade based on additional savings and breakeven timeline.")
    nc = ws.cell(row=rec_row + 1, column=1)
    nc.value     = rec_text
    nc.font      = Font(name="Arial", italic=True, size=10, color="374151")
    nc.fill      = FILL_NARRATION
    nc.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[rec_row + 1].height = 45
    ws.merge_cells(start_row=rec_row + 1, start_column=1, end_row=rec_row + 1, end_column=4)


# ══════════════════════════════════════════════════════════════════════════
#   NARRATION (Together AI)
# ══════════════════════════════════════════════════════════════════════════

def generate_narration(gathered: dict, model: dict, api_key: str) -> dict:
    """
    Call Together AI once to generate Summary, AE story, and EDP recommendation.
    Returns dict with keys: executive_summary, ae_commercial_story, edp_recommendation.
    Falls back to template text on any failure.
    """

    inp_summary = []
    for key in ["pain_usd_annual", "deal_amount", "implementation_pattern", "time_savings_signal"]:
        info = gathered["inputs"].get(key, {})
        val  = info.get("value", "N/A")
        conf = info.get("confidence_level", "unknown")
        inp_summary.append(f"  {key}: {val} [{conf}]")

    prompt = f"""You are a senior financial analyst preparing a deal economics summary for a sales team.

DEAL: {gathered['deal_name']} at {gathered['company']}
PRODUCT: {gathered['product']}
REP: {gathered['assigned_rep']}
MEDDPICC SCORE: {gathered['overall_score']}

FINANCIAL MODEL RESULTS:
Year 1 net:         ${model['net_y1']:,.0f}
Year 2 net:         ${model['net_y2']:,.0f}
Year 3 net:         ${model['net_y3']:,.0f}
3-year ROI:         {model['roi_3yr_pct']:.1f}%
Payback period:     {model['payback_months']:.1f} months
Model confidence:   {gathered['confidence_level']} ({gathered['confidence_score']}/100)

KEY INPUTS USED:
{chr(10).join(inp_summary)}

Write three sections:

1. EXECUTIVE SUMMARY (3 sentences max)
The business case in plain language for a CFO.
Lead with the payback period and ROI.
Reference the specific pain identified in discovery.
Do not use jargon.

2. AE COMMERCIAL STORY (2 sentences max)
What the AE should say walking into the pricing conversation.
Reference the payback period, any regulatory or business deadline if one exists, and the ROI multiple.

3. AZURE EA RECOMMENDATION (2 sentences max)
Whether the customer should commit to a higher Azure EA tier and why.
Reference the specific dollar savings and breakeven timeline.

Label each section clearly with the section name on its own line.
Be specific. Use real numbers from the model.
"""

    try:
        payload = {
            "model": TOGETHER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 600,
            "temperature": 0.4,
        }

        response = request_completion(TOGETHER_CHAT_URL, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"].strip()

        def _extract(text: str, section: str, next_section: str = None) -> str:
            pat = rf"{re.escape(section)}[:\s]*\n?(.*?)"
            end = rf"\n\s*{re.escape(next_section)}" if next_section else r"$"
            m = re.search(pat + end, text, re.DOTALL | re.IGNORECASE)
            return m.group(1).strip() if m else ""

        exec_sum  = _extract(content, "EXECUTIVE SUMMARY", "AE COMMERCIAL STORY")
        ae_story  = _extract(content, "AE COMMERCIAL STORY", "AZURE EA RECOMMENDATION")
        edp_rec   = _extract(content, "AZURE EA RECOMMENDATION")

        return {
            "executive_summary":   exec_sum  or content[:400],
            "ae_commercial_story": ae_story  or "",
            "edp_recommendation":  edp_rec   or "",
        }
    except Exception as e:
        print(f"[excel_builder] Narration generation failed: {e}")
        roi  = model["roi_3yr_pct"]
        pay  = model["payback_months"]
        edp  = model["incremental_benefit"]
        return {
            "executive_summary": (
                f"This deal delivers a {roi:.0f}% 3-year ROI with a payback period of {pay:.0f} months. "
                f"The primary value driver is pain reduction of ${model['pain_annual']:,.0f} annually. "
                f"Immediate action is recommended to capitalise on identified business pain."
            ),
            "ae_commercial_story": (
                f"Enter the pricing conversation anchored on a {pay:.0f}-month payback and {roi:.0f}% ROI. "
                f"Tie urgency to the cost of inaction at ${model['pain_annual']:,.0f} per year."
            ),
            "edp_recommendation": (
                f"You're {model['deal_amount']/model['next_edp_threshold']*100:.0f}% of the way to the next Azure EA tier "
                f"(${model['next_edp_threshold']:,.0f}). Reaching that threshold would unlock "
                f"${model['next_edp_threshold']*model['next_edp_rate']:,.0f}/year in savings "
                f"(vs ${model['edp_savings']:,.0f} today) — an additional ${edp:,.0f}/year. "
                f"Consider consolidating Azure workloads to maximise EA discount."
            ),
        }


# ══════════════════════════════════════════════════════════════════════════
#   MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

def build_workbook(gathered: dict, model: dict, output_path: Path,
                   api_key: str = "") -> Path:
    """
    Build the full 5-tab workbook and save to output_path.
    Returns the path to the saved file.
    """
    # Generate narration (gracefully falls back to templates)
    narration = generate_narration(gathered, model, api_key) if api_key else {
        "executive_summary":   f"3-year ROI: {model['roi_3yr_pct']:.0f}%. Payback: {model['payback_months']:.0f} months.",
        "ae_commercial_story": "Present payback period and ROI to anchor pricing conversation.",
        "edp_recommendation":  f"Incremental Azure EA benefit: ${model['incremental_benefit']:,.0f}.",
    }

    wb = Workbook()

    # Rename default sheet to Summary and add others
    ws_summary      = wb.active
    ws_summary.title = "Summary"
    ws_assumptions  = wb.create_sheet("Assumptions")
    ws_model        = wb.create_sheet("3-Year Model")
    ws_scenarios    = wb.create_sheet("Scenarios")
    ws_azure_estimate = wb.create_sheet("Azure Estimate")
    ws_commercial     = wb.create_sheet("Commercial")

    # Build each tab
    _build_summary(ws_summary, gathered, model, narration)
    _build_assumptions(ws_assumptions, gathered, model)
    _build_model(ws_model, model)

    # Pass model row map from Tab 3 to Tab 4
    model_rows = getattr(ws_model, "_deal_rows", {})
    _build_scenarios(ws_scenarios, model, model_rows)
    _build_azure_estimate(ws_azure_estimate, gathered, model)
    _build_commercial(ws_commercial, model, narration)

    # Validate: openpyxl writes formulas as strings; no compute errors at write time.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return output_path
