"""
generate_deck(output_path) — creates a 3-slide pipeline intelligence deck.

Slide 1: Title
Slide 2: Pipeline Health (score distribution + top flags)
Slide 3: Rep Performance table
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

sys.path.insert(0, str(Path(__file__).parent))
from analyze_pipeline import analyze_pipeline

# ── Palette ───────────────────────────────────────────────────────────────────
BG      = RGBColor(0x0F, 0x17, 0x2A)
CARD    = RGBColor(0x1E, 0x29, 0x3B)
ACCENT  = RGBColor(0x63, 0x66, 0xF1)
GREEN   = RGBColor(0x22, 0xC5, 0x5E)
AMBER   = RGBColor(0xF5, 0x9E, 0x0B)
RED     = RGBColor(0xEF, 0x44, 0x44)
WHITE   = RGBColor(0xF8, 0xFA, 0xFC)
MUTED   = RGBColor(0x94, 0xA3, 0xB8)
BORDER  = RGBColor(0x33, 0x4E, 0x68)

W = Inches(13.33)
H = Inches(7.5)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _blank_slide(prs):
    layout = prs.slide_layouts[6]  # completely blank
    return prs.slides.add_slide(layout)


def _bg(slide, color=BG):
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = color


def _box(slide, x, y, w, h, fill=None, line=None):
    shape = slide.shapes.add_shape(1, x, y, w, h)  # MSO_SHAPE_TYPE.RECTANGLE = 1
    shape.fill.solid() if fill else shape.fill.background()
    if fill:
        shape.fill.fore_color.rgb = fill
    shape.line.fill.background() if not line else None
    if line:
        shape.line.color.rgb = line
        shape.line.width = Pt(0.5)
    else:
        shape.line.fill.background()
    return shape


def _text(slide, text, x, y, w, h, size=12, bold=False, color=WHITE,
          align=PP_ALIGN.LEFT, wrap=True):
    txb = slide.shapes.add_textbox(x, y, w, h)
    tf = txb.text_frame
    tf.word_wrap = wrap
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    run.font.name = "Helvetica Neue"
    return txb


def _hbar(slide, x, y, w, h, pct, fill_color, bg_color=CARD):
    _box(slide, x, y, w, h, fill=bg_color)
    if pct > 0:
        _box(slide, x, y, int(w * min(pct, 100) / 100), h, fill=fill_color)


def _footer(slide, period):
    today = datetime.now().strftime("%b %d, %Y")
    _box(slide, Inches(0), Inches(7.3), W, Inches(0.2), fill=CARD)
    _text(slide, f"Deal Desk Intelligence  ·  {period}  ·  Generated {today}",
          Inches(0.5), Inches(7.32), Inches(12), Inches(0.18),
          size=7, color=MUTED, align=PP_ALIGN.LEFT)


# ── Slide 1: Title ────────────────────────────────────────────────────────────

def _slide_title(prs, period, overview):
    slide = _blank_slide(prs)
    _bg(slide)

    # Accent bar left edge
    _box(slide, Inches(0), Inches(0), Inches(0.08), H, fill=ACCENT)

    # Title
    _text(slide, "Pipeline Intelligence Report",
          Inches(0.5), Inches(1.8), Inches(8), Inches(1.0),
          size=40, bold=True, color=WHITE)

    # Subtitle / period
    _text(slide, period,
          Inches(0.5), Inches(2.85), Inches(6), Inches(0.5),
          size=18, color=MUTED)

    # 3 stat cards
    cards = [
        ("TOTAL DEALS",       str(overview["total_deals"]),          ACCENT),
        ("PIPELINE VALUE",    f"${overview['total_pipeline_value']:,.0f}", GREEN),
        ("AVG PROBABILITY",   f"{overview['average_probability']}%",  AMBER),
    ]
    card_x = Inches(0.5)
    card_w = Inches(3.6)
    card_h = Inches(1.4)
    card_y = Inches(4.5)
    gap    = Inches(0.35)

    for label, value, color in cards:
        _box(slide, card_x, card_y, card_w, card_h, fill=CARD, line=BORDER)
        _text(slide, label,
              card_x + Inches(0.2), card_y + Inches(0.15),
              card_w - Inches(0.4), Inches(0.3),
              size=9, color=MUTED)
        _text(slide, value,
              card_x + Inches(0.2), card_y + Inches(0.5),
              card_w - Inches(0.4), Inches(0.6),
              size=26, bold=True, color=color)
        card_x += card_w + gap

    _footer(slide, period)


# ── Slide 2: Pipeline Health ──────────────────────────────────────────────────

def _slide_health(prs, scores, flags, period):
    slide = _blank_slide(prs)
    _bg(slide)
    _box(slide, Inches(0), Inches(0), Inches(0.08), H, fill=ACCENT)

    # Title
    _text(slide, "Pipeline Health",
          Inches(0.5), Inches(0.3), Inches(8), Inches(0.55),
          size=22, bold=True, color=WHITE)
    _box(slide, Inches(0.5), Inches(0.9), Inches(12.5), Pt(1), fill=BORDER)

    # ── Health score cards (left column) ──────────────────────────────────────
    buckets = [
        ("STRONG",          scores.get("strong", {}),          GREEN),
        ("NEEDS ATTENTION", scores.get("needs_attention", {}),  AMBER),
        ("AT RISK",         scores.get("at_risk", {}),          RED),
    ]
    card_x = Inches(0.5)
    card_y = Inches(1.1)
    card_w = Inches(2.5)
    card_h = Inches(1.55)
    gap    = Inches(0.18)

    for label, bucket, color in buckets:
        _box(slide, card_x, card_y, card_w, card_h, fill=CARD, line=BORDER)
        _text(slide, label,
              card_x + Inches(0.15), card_y + Inches(0.15),
              card_w - Inches(0.3), Inches(0.3),
              size=8, bold=True, color=color)
        _text(slide, str(bucket.get("count", 0)),
              card_x + Inches(0.15), card_y + Inches(0.5),
              card_w - Inches(0.3), Inches(0.55),
              size=28, bold=True, color=color)
        _text(slide, f"{bucket.get('percentage', 0)}%  ·  ${bucket.get('total_value', 0):,.0f}",
              card_x + Inches(0.15), card_y + Inches(1.1),
              card_w - Inches(0.3), Inches(0.3),
              size=9, color=MUTED)
        card_y += card_h + gap

    # ── Stacked health bar ─────────────────────────────────────────────────────
    bar_x = Inches(3.6)
    bar_y = Inches(1.1)
    bar_w = Inches(4.5)
    bar_h = Inches(0.35)
    total = sum(b.get("count", 0) for _, b, _ in buckets) or 1
    cx = bar_x
    for _, bucket, color in buckets:
        seg_w = int(bar_w * bucket.get("count", 0) / total)
        if seg_w > 0:
            _box(slide, cx, bar_y, seg_w, bar_h, fill=color)
            cx += seg_w
    _text(slide, "Health distribution",
          bar_x, bar_y + Inches(0.4), bar_w, Inches(0.25), size=8, color=MUTED)

    # ── Top flags (right side) ─────────────────────────────────────────────────
    _text(slide, "TOP PIPELINE FLAGS",
          Inches(8.7), Inches(1.1), Inches(4.3), Inches(0.3),
          size=9, bold=True, color=ACCENT)

    top_flags = sorted(flags.items(), key=lambda x: x[1]["count"], reverse=True)[:5]
    max_count = top_flags[0][1]["count"] if top_flags else 1

    fy = Inches(1.5)
    for flag_key, fdata in top_flags:
        label = flag_key.replace("_", " ").title()
        count = fdata["count"]
        pct   = fdata["percentage"]
        exp   = fdata["pipeline_value_exposed"]

        _text(slide, f"{label}",
              Inches(8.7), fy, Inches(4.0), Inches(0.22),
              size=9, bold=False, color=WHITE)
        fy += Inches(0.24)

        bar_fill_w = int(Inches(3.8) * count / max_count)
        _hbar(slide, Inches(8.7), fy, Inches(3.8), Inches(0.18),
              count * 100 // max_count, RED)

        _text(slide, f"{count} deals ({pct}%)  ·  ${exp:,.0f} exposed",
              Inches(8.7), fy + Inches(0.2), Inches(4.3), Inches(0.22),
              size=8, color=MUTED)
        fy += Inches(0.58)

    _footer(slide, period)


# ── Slide 3: Rep Performance ──────────────────────────────────────────────────

def _slide_reps(prs, reps, period):
    slide = _blank_slide(prs)
    _bg(slide)
    _box(slide, Inches(0), Inches(0), Inches(0.08), H, fill=ACCENT)

    _text(slide, "Rep Performance",
          Inches(0.5), Inches(0.3), Inches(8), Inches(0.55),
          size=22, bold=True, color=WHITE)
    _box(slide, Inches(0.5), Inches(0.9), Inches(12.5), Pt(1), fill=BORDER)

    headers = ["Rep", "Deals", "Pipeline Value", "MEDDPICC %", "AT RISK", "Top Gap"]
    col_w   = [Inches(2.2), Inches(0.7), Inches(1.8), Inches(1.4), Inches(1.1), Inches(2.5)]
    col_x   = [Inches(0.5)]
    for cw in col_w[:-1]:
        col_x.append(col_x[-1] + cw + Inches(0.05))

    row_h = Inches(0.48)
    ty    = Inches(1.05)

    # Header row
    for i, hdr in enumerate(headers):
        _text(slide, hdr, col_x[i], ty, col_w[i], row_h,
              size=8, bold=True, color=ACCENT, align=PP_ALIGN.LEFT)
    ty += row_h + Inches(0.05)
    _box(slide, Inches(0.5), ty - Inches(0.05), Inches(12.5), Pt(1), fill=BORDER)

    # Data rows — sort by MEDDPICC completion desc
    sorted_reps = sorted(reps, key=lambda r: r["meddpicc_completion_rate"], reverse=True)
    for rep in sorted_reps[:10]:
        at_risk = rep["score_distribution"].get("at_risk", 0)
        ar_color = RED if at_risk > 3 else AMBER if at_risk > 1 else GREEN
        gap = rep["coaching_priority"].replace("_", " ").title()

        row_vals = [
            (rep["rep_name"],                               WHITE,    False),
            (str(rep["deal_count"]),                        MUTED,    False),
            (f"${rep['total_pipeline_value']:,.0f}",        MUTED,    False),
            (f"{rep['meddpicc_completion_rate']}%",         WHITE,    True),
            (str(at_risk),                                  ar_color, True),
            (gap,                                           MUTED,    False),
        ]
        for i, (val, color, bold) in enumerate(row_vals):
            _text(slide, val, col_x[i], ty, col_w[i], row_h,
                  size=9, bold=bold, color=color, align=PP_ALIGN.LEFT)
        ty += row_h

        if ty > Inches(7.0):
            break

    _footer(slide, period)
    _text(slide,
          "MEDDPICC % = share of elements scored Confirmed  ·  AT RISK = deals with AT RISK overall score",
          Inches(0.5), Inches(7.1), Inches(10.0), Inches(0.18),
          size=7, color=MUTED)


# ── Main ──────────────────────────────────────────────────────────────────────

def generate_deck(output_path: str = None, period: str = None) -> str:
    if not period:
        period = datetime.now().strftime("%B %Y")

    data = analyze_pipeline(focus="full")["data"]
    overview = data["overview"]
    scores   = data["score_distribution"]
    flags    = data["flag_frequency"]
    reps     = data["rep_performance"]

    prs = Presentation()
    prs.slide_width  = W
    prs.slide_height = H

    _slide_title(prs, period, overview)
    _slide_health(prs, scores, flags, period)
    _slide_reps(prs, reps, period)

    if not output_path:
        mbr_dir = Path(__file__).parent.parent / "knowledge_base" / "mbr"
        mbr_dir.mkdir(parents=True, exist_ok=True)
        slug = period.lower().replace(" ", "_")
        output_path = str(mbr_dir / f"pipeline_deck_{slug}.pptx")

    prs.save(output_path)
    print(f"Deck saved: {output_path}")
    return output_path


if __name__ == "__main__":
    path = generate_deck()
    print(f"Done: {path}")
