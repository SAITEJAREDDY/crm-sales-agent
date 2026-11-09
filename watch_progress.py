"""
Live progress monitor — Phase 2 KB Builder + Profile Generator
Run: python watch_progress.py
"""

import json
import os
import time
from datetime import datetime, timezone

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
KB_DIR       = os.path.join(BASE_DIR, "knowledge_base")
KB_SUB_DIR   = os.path.join(KB_DIR, "kb")
CHROMA_DIR   = os.path.join(KB_DIR, "chromadb")

PHASE2_PROGRESS  = os.path.join(KB_SUB_DIR, "_generation_progress.json")
PHASE2_FAILED    = os.path.join(KB_SUB_DIR, "_failed_generation.json")

DOC_TYPES = [
    ("battlecards",  "Battlecards "),
    ("capabilities", "Capabilities"),
    ("icp_profiles", "ICP Profiles"),
    ("cost_sheets",  "Cost Sheets "),
]

W = 64


# ── HELPERS ────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    if default is None:
        default = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def bar(filled, total, width=40, fill="█", empty="░"):
    if not total:
        return empty * width
    n = int(width * min(filled / total, 1.0))
    return fill * n + empty * (width - n)


def pct(n, total):
    return f"{n / total * 100:.0f}%" if total else "0%"


def elapsed_eta(started_at, done, total):
    if not started_at or started_at == "—":
        return "—", "—", "—"
    try:
        start = datetime.fromisoformat(started_at)
        elapsed_sec = (datetime.now(timezone.utc) - start).total_seconds()
        h, rem = divmod(int(elapsed_sec), 3600)
        m, s = divmod(rem, 60)
        elapsed_str = f"{h:02d}:{m:02d}:{s:02d}"
        if done > 0 and elapsed_sec > 0:
            rate = done / (elapsed_sec / 60)
            rate_str = f"{rate:.1f}/min"
            remaining = max(total - done, 0)
            eta_sec = int(remaining / rate * 60) if rate > 0 else 0
            eh, er = divmod(eta_sec, 3600)
            em, es = divmod(er, 60)
            eta_str = f"{eh:02d}:{em:02d}:{es:02d}"
        else:
            rate_str = "—"
            eta_str  = "—"
        return elapsed_str, rate_str, eta_str
    except Exception:
        return "—", "—", "—"


def count_files(directory, ext=".md"):
    if not os.path.isdir(directory):
        return 0
    return sum(1 for f in os.listdir(directory) if f.endswith(ext) and not f.startswith("_"))


def chroma_ready():
    return os.path.isdir(CHROMA_DIR) and any(
        f.endswith(".bin") or f.endswith(".parquet") or f == "chroma.sqlite3"
        for root, _, files in os.walk(CHROMA_DIR)
        for f in files
    )


# ── BOX DRAWING ────────────────────────────────────────────────────────────

def line(content=""):
    inner = W - 2
    s = str(content)
    if len(s) > inner:
        s = s[:inner - 3] + "…  "
    return "║" + s.ljust(inner) + "║"


def divider():
    return "╠" + "═" * (W - 2) + "╣"


def thin_div():
    return "╟" + "─" * (W - 2) + "╢"


def header(title=""):
    inner = W - 2
    if title:
        pad = (inner - len(title) - 2) // 2
        top = "╔" + "═" * pad + " " + title + " " + "═" * (inner - pad - len(title) - 2) + "╗"
        return top
    return "╔" + "═" * inner + "╗"


def footer():
    return "╚" + "═" * (W - 2) + "╝"


# ── RENDER ─────────────────────────────────────────────────────────────────

def render():
    os.system("clear")
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    # ── Phase 2 data ───────────────────────────────────────────────────────
    p2 = load_json(PHASE2_PROGRESS)
    p2f = load_json(PHASE2_FAILED)

    p2_total    = p2.get("total_documents_expected", 80)
    p2_gen      = p2.get("documents_generated", 0)
    p2_skip     = p2.get("documents_skipped", 0)
    p2_fail     = p2.get("documents_failed", 0)
    p2_done     = p2_gen + p2_skip + p2_fail
    p2_chunks   = p2.get("chromadb_chunks_ingested", 0)
    p2_product  = p2.get("current_product", "—")
    p2_doctype  = p2.get("current_document_type", "—")
    p2_start    = p2.get("started_at", "—")
    p2_updated  = p2.get("updated_at", "—")

    # Per doc-type file counts on disk
    disk_counts = {
        dt: count_files(os.path.join(KB_SUB_DIR, dt))
        for dt, _ in DOC_TYPES
    }
    disk_total = sum(disk_counts.values())

    elapsed2, rate2, eta2 = elapsed_eta(p2_start, p2_done, p2_total)

    # ── Print ──────────────────────────────────────────────────────────────

    print(header("NEXUS SOLUTIONS — PHASE 2 KB BUILDER"))
    print(line(f"  Refreshed: {now}"))
    print(divider())

    # Progress bar
    pct_done = p2_done / p2_total * 100 if p2_total else 0
    print(line(f"  [{bar(p2_done, p2_total, width=40)}] {pct_done:5.1f}%"))
    print(line(f"  {p2_done} / {p2_total} documents complete"))
    print(line())

    # Counters
    print(line(f"  ✅  Generated        {p2_gen:>4}  │  📁 Files on disk   {disk_total:>4}"))
    print(line(f"  ⏭   Skipped          {p2_skip:>4}  │  ❌ Failed           {p2_fail:>4}"))
    print(thin_div())

    # Per doc-type breakdown
    print(line(f"  {'Document Type':<16}  {'Progress':>8}  {'On Disk':>9}"))
    print(line(f"  {'─'*16}  {'─'*8}  {'─'*9}"))
    for dt, label in DOC_TYPES:
        on_disk = disk_counts[dt]
        mini = bar(on_disk, 20, width=12)
        print(line(f"  {label}    [{mini}]  {on_disk:>2}/20"))

    print(thin_div())

    # Current activity
    if p2.get("current_product"):
        print(line(f"  ⚙  Now:  {p2_product}  →  {p2_doctype}"))
    else:
        print(line(f"  ⚙  Status: waiting / not started"))

    # Last update
    if p2_updated and p2_updated != "—":
        try:
            upd = datetime.fromisoformat(p2_updated)
            ago = int((datetime.now(timezone.utc) - upd).total_seconds())
            print(line(f"  🕐 Last update: {ago}s ago"))
        except Exception:
            pass

    print(thin_div())

    # Timing
    print(line(f"  Elapsed   {elapsed2}"))
    print(line(f"  Rate      {rate2}"))
    print(line(f"  ETA       {eta2}"))

    print(thin_div())

    # ChromaDB status
    chroma_status = "✅ Populated" if p2_chunks > 0 else ("📂 Ready" if chroma_ready() else "⏳ Pending")
    print(line(f"  ChromaDB  {chroma_status}  ({p2_chunks:,} chunks ingested)"))

    if p2f:
        print(line(f"  ⚠  {len(p2f)} failed → kb/_failed_generation.json"))

    print(divider())

    print(footer())
    print("\n  Ctrl+C to stop  •  refreshes every 5s\n")


def main():
    print("Starting monitor... (refreshes every 5s)")
    try:
        while True:
            render()
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")


if __name__ == "__main__":
    main()
