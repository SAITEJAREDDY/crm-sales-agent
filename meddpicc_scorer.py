"""
Deal Desk — MEDDPICC Scorecard Generator
Reads deal notes, scores each deal against all 8 MEDDPICC elements via
Together AI (Qwen3.8 Flash), writes _meddpicc.json + prepends scorecard
to _summary.md, then rolls up to meddpicc_pipeline.json / .csv.
"""

from tools.llm_client import TOGETHER_BASE_URL, TOGETHER_MODEL, create_completion

import csv
import json
import os
import re
import time
from datetime import datetime, timezone

import requests
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWENTY_TOKEN = os.environ.get("TWENTY_API_TOKEN", "")
TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")

together_client = OpenAI(
    api_key=TOGETHER_API_KEY,
    base_url=TOGETHER_BASE_URL,
)

DATA_URL = "http://localhost:3000/graphql"
HEADERS  = {"Authorization": f"Bearer {TWENTY_TOKEN}", "Content-Type": "application/json"}

BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
KB_DIR            = os.path.join(BASE_DIR, "knowledge_base")
NOTES_DIR         = os.path.join(KB_DIR, "notes")
PROGRESS_FILE     = os.path.join(KB_DIR, "_scoring_progress.json")
FAILED_FILE       = os.path.join(KB_DIR, "_failed_scoring.json")
PIPELINE_JSON     = os.path.join(KB_DIR, "meddpicc_pipeline.json")
PIPELINE_CSV      = os.path.join(KB_DIR, "meddpicc_pipeline.csv")

NOTE_FILES = [
    "01_discovery.md",
    "02_technical_review.md",
    "03_stakeholder_meeting.md",
    "04_risk_review.md",
    "05_close_plan.md",
]

STATUS_EMOJI = {
    "confirmed": "✅",
    "partial":   "⚠️",
    "at_risk":   "🔴",
    "unknown":   "❌",
}

ELEMENTS = [
    "metrics", "economic_buyer", "decision_criteria", "decision_process",
    "paper_process", "identify_pain", "champion", "competition",
]

ELEMENT_LABELS = {
    "metrics":            "Metrics",
    "economic_buyer":     "Economic Buyer",
    "decision_criteria":  "Decision Criteria",
    "decision_process":   "Decision Process",
    "paper_process":      "Paper Process",
    "identify_pain":      "Identify Pain",
    "champion":           "Champion",
    "competition":        "Competition",
}

CSV_COLUMNS = [
    "deal_id", "deal_name", "company", "stage", "amount", "probability",
    "assigned_rep", "overall_score",
    "metrics", "economic_buyer", "decision_criteria", "decision_process",
    "paper_process", "identify_pain", "champion", "competition",
    "missing_economic_buyer", "missing_champion", "unquantified_pain",
    "competitor_unknown", "paper_process_unknown", "deal_overdue", "stalled",
]


# ── HELPERS ───────────────────────────────────────────────────────────────────
def slugify(text):
    text = text.lower()
    text = re.sub(r"[—–\-]+", "_", text)
    text = re.sub(r"[^a-z0-9_]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── CRM FETCH ─────────────────────────────────────────────────────────────────
DEAL_QUERY = """
query FetchAllDeals {
  opportunities(
    filter: {}
    orderBy: { amount: { amountMicros: DescNullsLast } }
    first: 500
  ) {
    edges {
      node {
        id
        name
        amount { amountMicros }
        stage
        closeDate
        probability
        assignedRep
        daysinpipeline
        pocname { firstName lastName }
        poctitle
        companyName
        companysize
        dealOutcome
        lossreason
        createdAt
      }
    }
  }
}
"""


def fetch_deals():
    resp = requests.post(DATA_URL, headers=HEADERS, json={"query": DEAL_QUERY}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise ValueError(data["errors"])
    deals = []
    for edge in data["data"]["opportunities"]["edges"]:
        n = edge["node"]
        amt = (int(n["amount"]["amountMicros"]) / 1_000_000) if n.get("amount") else 0
        poc = n.get("pocname") or {}
        contact_name = f"{poc.get('firstName','') or ''} {poc.get('lastName','') or ''}".strip() or "Unknown"
        deals.append({
            "id":               n["id"],
            "name":             n.get("name", ""),
            "amount":           amt,
            "stage":            n.get("stage", "NEW"),
            "close_date":       n.get("closeDate") or "",
            "probability":      n.get("probability") or 0,
            "assigned_rep":     n.get("assignedRep") or "Unknown",
            "days_in_pipeline": n.get("daysinpipeline") or 0,
            "contact_name":     contact_name,
            "contact_title":    n.get("poctitle") or "Unknown",
            "company_name":     n.get("companyName") or "Unknown",
            "company_size":     int(n.get("companysize") or 0),
            "deal_outcome":     n.get("dealOutcome") or "",
            "loss_reason":      n.get("lossreason") or "",
            "created_at":       n.get("createdAt", ""),
        })
    print(f"  Fetched {len(deals)} deals from Twenty CRM.")
    return deals


# ── NOTE LOADING ──────────────────────────────────────────────────────────────
def find_deal_dir(deal):
    """Find the notes directory for a deal by trying slug combinations."""
    company_slug = slugify(deal["company_name"])
    deal_slug    = slugify(deal["name"])
    path = os.path.join(NOTES_DIR, company_slug, deal_slug)
    if os.path.isdir(path):
        return path
    # Fallback: search all company dirs for matching deal slug
    for company_dir in os.listdir(NOTES_DIR):
        candidate = os.path.join(NOTES_DIR, company_dir, deal_slug)
        if os.path.isdir(candidate):
            return candidate
    return None


def load_notes(deal_dir):
    """Concatenate all scoring note files that exist."""
    parts = []
    included = []
    for fname in NOTE_FILES:
        fpath = os.path.join(deal_dir, fname)
        if os.path.exists(fpath):
            with open(fpath) as f:
                parts.append(f.read().strip())
            included.append(fname[:-3])  # strip .md
    return "\n\n---\n\n".join(parts), included


# ── SCORING PROMPT ────────────────────────────────────────────────────────────
def build_prompt(deal, notes_text):
    # Determine effective stage for display
    outcome = deal.get("deal_outcome", "")
    stage = deal["stage"]
    if outcome == "LOST":
        display_stage = "LOST"
    elif outcome == "WON" or stage == "CUSTOMER":
        display_stage = "CUSTOMER (WON)"
    else:
        display_stage = stage

    return f"""You are a senior sales operations analyst reviewing deal notes for {deal['name']} at {deal['company_name']}.

DEAL CONTEXT:
- Amount: ${deal['amount']:,.0f}
- Stage: {display_stage}
- Probability: {deal['probability']}%
- Days in Pipeline: {deal['days_in_pipeline']}
- Assigned Rep: {deal['assigned_rep']}
- Contact: {deal['contact_name']}, {deal['contact_title']}
- Company Size: {deal['company_size']} employees

NOTES TO ANALYSE:
{notes_text}

Score this deal against all 8 MEDDPICC elements.
For each element provide:
- status: exactly one of confirmed / partial / at_risk / unknown
- evidence: one specific sentence from the notes that justifies the status — quote or paraphrase directly from the notes
- source_note: which note file the evidence came from (01_discovery / 02_technical_review / 03_stakeholder_meeting / 04_risk_review / 05_close_plan / none)
- gap: if status is not confirmed, what specifically is missing or needs to happen next — one sentence, actionable

Be strict. Do not mark anything confirmed unless there is explicit evidence in the notes. Partial is not failure — it is honest. Unknown is not failure — it is useful information.

Also provide:
- overall_score: STRONG / NEEDS ATTENTION / AT RISK
- overall_reason: two to three sentences explaining the score — what is working, what the biggest gap is, what the rep should focus on next
- next_priority: the single most important action the rep should take before the next customer interaction

Rules for overall_score:
- AT RISK if any of economic_buyer, identify_pain, or champion is unknown or at_risk
- NEEDS ATTENTION if mandatory elements are confirmed but two or more of metrics/decision_criteria/decision_process/competition are partial/at_risk/unknown
- STRONG if all mandatory confirmed and no more than one important element is partial or unknown

Return ONLY valid JSON. No text before or after the JSON. No markdown code fences. Just the raw JSON object.

JSON structure:
{{
  "deal_id": "",
  "deal_name": "",
  "company": "",
  "stage": "",
  "amount": 0,
  "probability": 0,
  "assigned_rep": "",
  "overall_score": "",
  "overall_reason": "",
  "next_priority": "",
  "scored_at": "",
  "elements": {{
    "metrics": {{"status": "", "evidence": "", "source_note": "", "gap": ""}},
    "economic_buyer": {{"status": "", "evidence": "", "source_note": "", "gap": ""}},
    "decision_criteria": {{"status": "", "evidence": "", "source_note": "", "gap": ""}},
    "decision_process": {{"status": "", "evidence": "", "source_note": "", "gap": ""}},
    "paper_process": {{"status": "", "evidence": "", "source_note": "", "gap": ""}},
    "identify_pain": {{"status": "", "evidence": "", "source_note": "", "gap": ""}},
    "champion": {{"status": "", "evidence": "", "source_note": "", "gap": ""}},
    "competition": {{"status": "", "evidence": "", "source_note": "", "gap": ""}}
  }},
  "flags": {{
    "missing_economic_buyer": false,
    "missing_champion": false,
    "unquantified_pain": false,
    "competitor_unknown": false,
    "paper_process_unknown": false,
    "deal_overdue": false,
    "stalled": false
  }}
}}"""


# ── LLM CALL ──────────────────────────────────────────────────────────────────
def call_together(prompt, attempt=1):
    try:
        resp = create_completion(together_client,
            model=TOGETHER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content
    except Exception as e:
        err = str(e)
        if ("429" in err or "rate" in err.lower()) and attempt == 1:
            print(f"    ⏳ Rate limited — waiting 30s...", end="", flush=True)
            time.sleep(30)
            print(" retrying.")
            return call_together(prompt, attempt=2)
        raise


def parse_json_response(raw):
    """Extract JSON from response, stripping any accidental markdown fences."""
    raw = raw.strip()
    # Strip ```json ... ``` fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def score_deal(deal, notes_text, notes_included):
    prompt = build_prompt(deal, notes_text)
    now    = datetime.now(timezone.utc).isoformat()

    raw = call_together(prompt)
    try:
        result = parse_json_response(raw)
    except json.JSONDecodeError:
        # Retry once with explicit JSON-only instruction
        print(f"    ⚠️  JSON parse failed — retrying with stricter instruction...")
        strict_prompt = prompt + "\n\nCRITICAL: Your previous response was not valid JSON. Return ONLY the raw JSON object. No other text."
        raw = call_together(strict_prompt)
        result = parse_json_response(raw)  # raises if still broken

    # Inject metadata
    result["deal_id"]        = deal["id"]
    result["deal_name"]      = deal["name"]
    result["company"]        = deal["company_name"]
    result["stage"]          = deal["stage"]
    result["amount"]         = deal["amount"]
    result["probability"]    = deal["probability"]
    result["assigned_rep"]   = deal["assigned_rep"]
    result["scored_at"]      = now
    result["notes_analysed"] = notes_included

    # Compute deal_overdue and stalled flags
    flags = result.setdefault("flags", {})
    close_date = deal.get("close_date") or ""
    if close_date:
        try:
            cd = datetime.fromisoformat(close_date.replace("Z", "+00:00"))
            if cd < datetime.now(timezone.utc) and deal["stage"] not in ("CUSTOMER",):
                flags["deal_overdue"] = True
        except Exception:
            pass
    if (deal.get("days_in_pipeline") or 0) > 90:
        flags["stalled"] = True

    return result


# ── SCORECARD SECTION ─────────────────────────────────────────────────────────
def build_scorecard_md(result):
    scored_date = result.get("scored_at", "")[:10]
    overall     = result.get("overall_score", "UNKNOWN")
    elements    = result.get("elements", {})
    flags       = result.get("flags", {})

    rows = []
    for key in ELEMENTS:
        el     = elements.get(key, {})
        status = el.get("status", "unknown")
        emoji  = STATUS_EMOJI.get(status, "❓")
        label  = ELEMENT_LABELS[key]
        evid   = el.get("evidence", "—")
        # Truncate evidence for table readability
        if len(evid) > 80:
            evid = evid[:77] + "..."
        rows.append(f"| {label:<17} | {emoji} {status:<13} | {evid:<53} |")

    flag_lines = []
    flag_names = {
        "missing_economic_buyer": "Economic Buyer not yet engaged",
        "missing_champion":       "No confirmed internal champion",
        "unquantified_pain":      "Pain not yet quantified in numbers",
        "competitor_unknown":     "Competitive landscape unknown",
        "paper_process_unknown":  "Procurement process not discussed",
        "deal_overdue":           "Close date has passed",
        "stalled":                "Deal open more than 90 days without stage progression",
    }
    for k, label in flag_names.items():
        if flags.get(k):
            flag_lines.append(f"- {label}")

    scorecard = f"""## MEDDPICC Scorecard
**Overall: {overall}**
**Scored: {scored_date}**

| Element           | Status          | Evidence                                                  |
|-------------------|-----------------|-----------------------------------------------------------|
""" + "\n".join(rows) + f"""

**Why this score:**
{result.get('overall_reason', '—')}

**Next Priority:**
{result.get('next_priority', '—')}

**Active Flags:**
{chr(10).join(flag_lines) if flag_lines else 'None.'}

---

"""
    return scorecard


def update_summary(deal_dir, scorecard_md):
    summary_path = os.path.join(deal_dir, "_summary.md")
    if os.path.exists(summary_path):
        with open(summary_path) as f:
            existing = f.read()
        # Remove old scorecard section if present (idempotent)
        if "## MEDDPICC Scorecard" in existing:
            existing = re.sub(
                r"## MEDDPICC Scorecard.*?---\s*\n",
                "",
                existing,
                flags=re.DOTALL,
            )
        new_content = scorecard_md + existing
    else:
        new_content = scorecard_md

    with open(summary_path, "w") as f:
        f.write(new_content)


# ── PROGRESS ──────────────────────────────────────────────────────────────────
def load_progress():
    return load_json(PROGRESS_FILE, {
        "total_deals": 0,
        "deals_scored": 0,
        "deals_skipped": 0,
        "deals_failed": 0,
        "strong_count": 0,
        "needs_attention_count": 0,
        "at_risk_count": 0,
        "last_deal_processed": "",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def save_progress(p):
    p["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(PROGRESS_FILE, p)


# ── ROLLUP ────────────────────────────────────────────────────────────────────
def generate_rollup():
    all_results = []
    for company_dir in sorted(os.listdir(NOTES_DIR)):
        company_path = os.path.join(NOTES_DIR, company_dir)
        if not os.path.isdir(company_path):
            continue
        for deal_dir_name in sorted(os.listdir(company_path)):
            meddpicc_path = os.path.join(company_path, deal_dir_name, "_meddpicc.json")
            if os.path.exists(meddpicc_path):
                try:
                    with open(meddpicc_path) as f:
                        all_results.append(json.load(f))
                except Exception:
                    pass

    save_json(PIPELINE_JSON, all_results)

    with open(PIPELINE_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in all_results:
            row = {c: r.get(c, "") for c in CSV_COLUMNS}
            elements = r.get("elements", {})
            for el in ELEMENTS:
                row[el] = elements.get(el, {}).get("status", "unknown")
            flags = r.get("flags", {})
            for flag in ["missing_economic_buyer", "missing_champion", "unquantified_pain",
                         "competitor_unknown", "paper_process_unknown", "deal_overdue", "stalled"]:
                row[flag] = flags.get(flag, False)
            writer.writerow(row)

    return all_results


# ── COMPLETION SUMMARY ────────────────────────────────────────────────────────
def print_summary(all_results, progress):
    total    = len(all_results)
    strong   = sum(1 for r in all_results if r.get("overall_score") == "STRONG")
    needs    = sum(1 for r in all_results if r.get("overall_score") == "NEEDS ATTENTION")
    at_risk  = sum(1 for r in all_results if r.get("overall_score") == "AT RISK")

    flag_counts = {}
    flag_names = [
        "missing_economic_buyer", "missing_champion", "unquantified_pain",
        "competitor_unknown", "paper_process_unknown", "deal_overdue", "stalled",
    ]
    for r in all_results:
        flags = r.get("flags", {})
        for f in flag_names:
            if flags.get(f):
                flag_counts[f] = flag_counts.get(f, 0) + 1

    el_status_counts = {el: {"confirmed": 0, "partial": 0, "at_risk": 0, "unknown": 0} for el in ELEMENTS}
    for r in all_results:
        elements = r.get("elements", {})
        for el in ELEMENTS:
            st = elements.get(el, {}).get("status", "unknown")
            if st in el_status_counts[el]:
                el_status_counts[el][st] += 1

    pct = lambda n: f"{n/total*100:.0f}%" if total else "0%"

    print("\n==========================================")
    print("MEDDPICC SCORING COMPLETE")
    print("==========================================")
    print(f"\nDeals scored:          {progress['deals_scored']}")
    print(f"Deals skipped:         {progress['deals_skipped']}")
    print(f"Deals failed:          {progress['deals_failed']}")
    print(f"\nOverall Score Distribution:")
    print(f"  STRONG              {strong:>5}  ({pct(strong)})")
    print(f"  NEEDS ATTENTION     {needs:>5}  ({pct(needs)})")
    print(f"  AT RISK             {at_risk:>5}  ({pct(at_risk)})")
    print(f"\nMost Common Flags:")
    flag_labels = {
        "missing_economic_buyer": "Missing Economic Buyer",
        "missing_champion":       "Missing Champion",
        "unquantified_pain":      "Unquantified Pain",
        "competitor_unknown":     "Competitor Unknown",
        "paper_process_unknown":  "Paper Process Unknown",
        "deal_overdue":           "Deal Overdue",
        "stalled":                "Deal Stalled",
    }
    for f in flag_names:
        c = flag_counts.get(f, 0)
        print(f"  {flag_labels[f]:<28} {c:>5} deals  ({pct(c)})")
    print(f"\nElement Status Breakdown:")
    for el in ELEMENTS:
        c = el_status_counts[el]
        label = ELEMENT_LABELS[el]
        print(f"  {label:<20} confirmed {pct(c['confirmed']):<6} partial {pct(c['partial']):<6} at_risk {pct(c['at_risk']):<6} unknown {pct(c['unknown'])}")
    print(f"\nPipeline rollup saved to:")
    print(f"  knowledge_base/meddpicc_pipeline.json")
    print(f"  knowledge_base/meddpicc_pipeline.csv")
    print("\n==========================================")
    print("Next step: Run pattern_analyst.py to surface")
    print("pipeline-level insights from scorecard data.")
    print("==========================================\n")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("\n=== MEDDPICC Scorecard Generator ===\n")

    print("[1/4] Fetching deals from Twenty CRM...")
    deals = fetch_deals()

    print("[2/4] Loading progress (will resume if interrupted)...")
    progress = load_progress()
    progress["total_deals"] = len(deals)

    failed = load_json(FAILED_FILE, [])

    print(f"[3/4] Scoring {len(deals)} deals...\n")

    for i, deal in enumerate(deals, 1):
        deal_label = f"{deal['company_name'][:12]:<12} — {deal['name'][:30]:<30}"
        prefix     = f"  [{i}/{len(deals)}] {deal_label}"

        deal_dir = find_deal_dir(deal)
        if not deal_dir:
            print(f"{prefix} | ⚠️  no notes dir found — skipping")
            progress["deals_skipped"] += 1
            progress["last_deal_processed"] = deal["name"]
            save_progress(progress)
            continue

        meddpicc_path = os.path.join(deal_dir, "_meddpicc.json")
        if os.path.exists(meddpicc_path):
            print(f"{prefix} | ⏭  already scored")
            progress["deals_skipped"] += 1
            progress["last_deal_processed"] = deal["name"]
            save_progress(progress)
            continue

        notes_text, notes_included = load_notes(deal_dir)
        if not notes_text.strip():
            print(f"{prefix} | ⚠️  no note content — skipping")
            progress["deals_skipped"] += 1
            progress["last_deal_processed"] = deal["name"]
            save_progress(progress)
            continue

        print(f"{prefix} | scoring...", end="", flush=True)

        try:
            result      = score_deal(deal, notes_text, notes_included)
            scorecard   = build_scorecard_md(result)
            overall     = result.get("overall_score", "UNKNOWN")

            save_json(meddpicc_path, result)
            update_summary(deal_dir, scorecard)

            score_icon = {"STRONG": "💚", "NEEDS ATTENTION": "🟡", "AT RISK": "🔴"}.get(overall, "❓")
            print(f" {score_icon} {overall}")

            progress["deals_scored"] += 1
            if overall == "STRONG":
                progress["strong_count"] += 1
            elif overall == "NEEDS ATTENTION":
                progress["needs_attention_count"] += 1
            else:
                progress["at_risk_count"] += 1

        except Exception as e:
            print(f" ❌ FAILED: {e}")
            failed.append({"deal_id": deal["id"], "deal_name": deal["name"], "error": str(e)})
            save_json(FAILED_FILE, failed)
            progress["deals_failed"] += 1

        progress["last_deal_processed"] = deal["name"]
        save_progress(progress)
        time.sleep(1)  # brief pause between calls

    print("\n[4/4] Generating pipeline rollup...")
    all_results = generate_rollup()

    print_summary(all_results, progress)


if __name__ == "__main__":
    main()
