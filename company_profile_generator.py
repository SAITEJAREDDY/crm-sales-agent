"""
Deal Desk — Company Profile Generator
Reads all deal notes and MEDDPICC scorecards for each company,
synthesises a strategic account profile, saves to
knowledge_base/profiles/{company_slug}/_profile.md
"""

from tools.llm_client import TOGETHER_BASE_URL, TOGETHER_MODEL, create_completion

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

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
KB_DIR         = os.path.join(BASE_DIR, "knowledge_base")
NOTES_DIR      = os.path.join(KB_DIR, "notes")
PROFILES_DIR   = os.path.join(KB_DIR, "profiles")
PROGRESS_FILE  = os.path.join(KB_DIR, "_profile_progress.json")
FAILED_FILE    = os.path.join(KB_DIR, "_failed_profiles.json")

os.makedirs(PROFILES_DIR, exist_ok=True)

NOTE_ORDER = [
    "01_discovery.md",
    "02_technical_review.md",
    "03_stakeholder_meeting.md",
    "04_risk_review.md",
    "05_close_plan.md",
    "_summary.md",
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
COMPANY_QUERY = """
query FetchCompanies {
  companies(first: 500) {
    edges {
      node {
        id
        name
        domainName { primaryLinkUrl }
        employees
        linkedinLink { primaryLinkUrl }
        address {
          addressCity
          addressState
          addressCountry
        }
      }
    }
  }
}
"""

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
        dealscore
        companyName
        dealOutcome
        lossreason
        productcategory
        poctitle
        pocname { firstName lastName }
        createdAt
      }
    }
  }
}
"""


def fetch_companies():
    resp = requests.post(DATA_URL, headers=HEADERS, json={"query": COMPANY_QUERY}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    companies = {}
    for edge in data["data"]["companies"]["edges"]:
        n = edge["node"]
        addr = n.get("address") or {}
        companies[n["name"]] = {
            "id":        n["id"],
            "name":      n["name"],
            "domain":    (n.get("domainName") or {}).get("primaryLinkUrl") or "—",
            "employees": n.get("employees") or 0,
            "linkedin":  (n.get("linkedinLink") or {}).get("primaryLinkUrl") or "—",
            "city":      addr.get("addressCity") or "—",
            "state":     addr.get("addressState") or "—",
            "country":   addr.get("addressCountry") or "—",
        }
    return companies


def fetch_deals():
    resp = requests.post(DATA_URL, headers=HEADERS, json={"query": DEAL_QUERY}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    deals = []
    for edge in data["data"]["opportunities"]["edges"]:
        n = edge["node"]
        amt = (int(n["amount"]["amountMicros"]) / 1_000_000) if n.get("amount") else 0
        poc = n.get("pocname") or {}
        contact = f"{poc.get('firstName','') or ''} {poc.get('lastName','') or ''}".strip() or "Unknown"
        deals.append({
            "id":             n["id"],
            "name":           n.get("name", ""),
            "amount":         amt,
            "stage":          n.get("stage", ""),
            "close_date":     n.get("closeDate") or "—",
            "probability":    n.get("probability") or 0,
            "assigned_rep":   n.get("assignedRep") or "Unknown",
            "days_pipeline":  n.get("daysinpipeline") or 0,
            "deal_score":     n.get("dealscore") or 0,
            "company_name":   n.get("companyName") or "",
            "deal_outcome":   n.get("dealOutcome") or "",
            "loss_reason":    n.get("lossreason") or "",
            "product":        n.get("productcategory") or "AWS Services",
            "contact":        contact,
            "contact_title":  n.get("poctitle") or "Unknown",
        })
    return deals


# ── GROUP BY COMPANY ──────────────────────────────────────────────────────────
def group_deals_by_company(deals):
    by_company = {}
    for d in deals:
        cn = d["company_name"]
        if not cn:
            continue
        by_company.setdefault(cn, []).append(d)
    return by_company


# ── COMPANY LOOKUP ───────────────────────────────────────────────────────────
def find_crm_company(slug, companies):
    for name, data in companies.items():
        if slugify(name) == slug:
            return name, data
    return None, None


# ── NOTE LOADING ──────────────────────────────────────────────────────────────
# Send only _summary.md per deal — already distilled, fits 8K context window.
# Fallback to 01_discovery.md for deals that have no summary (MEETING stage).
MAX_NOTES_CHARS = 12_000  # safe ceiling for 8B-Lite 8K context


def load_company_notes(company_slug):
    """Load summary notes for all deals under a company slug."""
    company_dir = os.path.join(NOTES_DIR, company_slug)
    if not os.path.isdir(company_dir):
        return "", []

    all_parts = []
    deal_dirs = sorted(os.listdir(company_dir))
    total_chars = 0

    for deal_dir_name in deal_dirs:
        deal_dir = os.path.join(company_dir, deal_dir_name)
        if not os.path.isdir(deal_dir):
            continue
        # Prefer summary, fall back to discovery
        for fname in ("_summary.md", "01_discovery.md"):
            fpath = os.path.join(deal_dir, fname)
            if os.path.exists(fpath):
                with open(fpath) as f:
                    text = f.read().strip()
                remaining = MAX_NOTES_CHARS - total_chars
                if remaining <= 0:
                    break
                if len(text) > remaining:
                    text = text[:remaining] + "\n[truncated]"
                all_parts.append(f"### {deal_dir_name.replace('_', ' ').title()}\n\n{text}")
                total_chars += len(text)
                break
        if total_chars >= MAX_NOTES_CHARS:
            break

    return "\n\n===\n\n".join(all_parts), deal_dirs


def load_meddpicc_summary(company_slug):
    """Load MEDDPICC scores for all deals under a company, return brief text."""
    company_dir = os.path.join(NOTES_DIR, company_slug)
    if not os.path.isdir(company_dir):
        return ""
    lines = []
    for deal_dir_name in sorted(os.listdir(company_dir)):
        path = os.path.join(company_dir, deal_dir_name, "_meddpicc.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    sc = json.load(f)
                overall = sc.get("overall_score", "—")
                elements = sc.get("elements", {})
                el_summary = ", ".join(
                    f"{k}={v.get('status','?')}"
                    for k, v in elements.items()
                )
                lines.append(f"- {sc.get('deal_name','?')} → {overall} | {el_summary}")
            except Exception:
                pass
    return "\n".join(lines)


# ── PROMPT ────────────────────────────────────────────────────────────────────
def build_prompt(company, deals, notes_text, meddpicc_text):
    deals_summary = ""
    total_pipeline = 0
    for d in deals:
        outcome = d["deal_outcome"]
        stage_display = f"LOST ({d['loss_reason']})" if outcome == "LOST" else (
            "WON" if outcome == "WON" else d["stage"]
        )
        deals_summary += (
            f"- {d['name']} | {d['product']} | ${d['amount']:,.0f} | "
            f"{stage_display} | {d['probability']}% | "
            f"{d['days_pipeline']}d in pipeline | Rep: {d['assigned_rep']} | "
            f"Contact: {d['contact']}, {d['contact_title']}\n"
        )
        total_pipeline += d["amount"]

    meddpicc_section = f"\nMEDDPICC SCORES ACROSS DEALS:\n{meddpicc_text}\n" if meddpicc_text else ""

    return f"""You are a senior GTM intelligence analyst at AWS.

Your job is to synthesise everything known about a company into a single strategic profile that any rep, agent, or sales leader can read in 2 minutes and know exactly how to engage this account.

COMPANY DATA FROM CRM:
- Company Name: {company['name']}
- Domain: {company['domain']}
- Employees: {company['employees']}
- Location: {company['city']}, {company['state']}, {company['country']}
- LinkedIn: {company['linkedin']}
- Total AWS Pipeline Value: ${total_pipeline:,.0f}

DEALS WITH THIS COMPANY:
{deals_summary}
{meddpicc_section}
ALL NOTES FROM THIS COMPANY'S DEALS:
{notes_text}

---

Write a comprehensive company profile covering these sections exactly. Be specific. Use real names from the notes. Do not invent information not present in the source material. Where information is absent, say so explicitly — gaps are useful intelligence.

## Company Overview
Who they are, what they do, what market they operate in, their business model, and why they matter as an AWS prospect. Two to three paragraphs.

## Size & Structure
Employee count, likely team structure, headquarters and any known office locations, growth signals visible from deal history.

## Technology Profile
What is known about their current technology stack from all deal notes combined. Cloud provider, key SaaS tools, data infrastructure, on-premise legacy systems, integration complexity. Flag gaps where stack is unknown.

## Business Challenges & Pain Points
Every pain point mentioned across all deals and all notes combined. Deduplicated and ranked by frequency and severity. Include quantified impacts where stated.

## AWS Relationship History
Full deal history summary. What products were discussed, which reps engaged them, what stages deals reached, what happened to each deal. Overall relationship strength: NEW / DEVELOPING / ESTABLISHED / AT RISK.

## Key Stakeholders
Every person mentioned across all notes. For each person: name and title, role in buying decisions, relationship strength with AWS, any notes on their priorities or concerns.

## Competitive Landscape
Every competitor mentioned across all notes for this company. What the customer said about them. Where AWS is stronger and where competitors have an edge with this specific account.

## Commercial Profile
Budget signals, approval thresholds, procurement process hints, contract preferences. Total pipeline value with AWS to date. Average deal size. Best and worst deals by outcome.

## Why They Buy — And Why They Don't
Synthesise across all deals: what has moved them forward, what has stalled them, what objections keep appearing, what triggers action at this account.

## Recommended Next Actions
Top 3 specific actions for the assigned rep or account team. Be concrete — name the person to call, the product to propose, the question to ask."""


# ── LLM CALL ──────────────────────────────────────────────────────────────────
def call_together(prompt, attempt=1):
    try:
        resp = create_completion(together_client,
            model=TOGETHER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=1000,
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


# ── PROGRESS ──────────────────────────────────────────────────────────────────
def load_progress(total):
    p = load_json(PROGRESS_FILE, {})
    p.setdefault("total_companies", total)
    p.setdefault("profiles_generated", 0)
    p.setdefault("profiles_skipped", 0)
    p.setdefault("profiles_failed", 0)
    p.setdefault("last_company", "")
    p.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    p["updated_at"] = datetime.now(timezone.utc).isoformat()
    return p


def save_progress(p):
    p["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(PROGRESS_FILE, p)


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("\n=== Company Profile Generator ===\n")

    print("[1/4] Fetching CRM data...")
    companies = fetch_companies()
    deals     = fetch_deals()
    print(f"  {len(companies)} companies, {len(deals)} deals fetched.")

    deals_by_company = group_deals_by_company(deals)

    # Build list of companies that have notes on disk, sorted by total pipeline desc
    note_companies = [
        d for d in os.listdir(NOTES_DIR)
        if os.path.isdir(os.path.join(NOTES_DIR, d)) and not d.startswith("_")
    ]

    def total_pipeline(slug):
        cname, _ = find_crm_company(slug, companies)
        if not cname:
            return 0
        return sum(d["amount"] for d in deals_by_company.get(cname, []))

    note_companies_sorted = sorted(note_companies, key=total_pipeline, reverse=True)

    print(f"[2/4] {len(note_companies_sorted)} companies have notes on disk.")

    print("[3/4] Loading progress (will resume if interrupted)...")
    progress = load_progress(len(note_companies_sorted))
    failed   = load_json(FAILED_FILE, [])

    print(f"[4/4] Generating profiles...\n")

    for i, slug in enumerate(note_companies_sorted, 1):
        profile_path = os.path.join(PROFILES_DIR, f"{slug}.md")
        prefix = f"  [{i}/{len(note_companies_sorted)}] {slug[:40]:<40}"

        if os.path.exists(profile_path):
            print(f"{prefix} | ⏭  already exists")
            progress["profiles_skipped"] += 1
            progress["last_company"] = slug
            save_progress(progress)
            continue

        company_name, company_data = find_crm_company(slug, companies)
        if not company_data:
            # Build minimal company data from notes
            company_data = {
                "name": slug.replace("_", " ").title(),
                "domain": "—", "employees": 0,
                "linkedin": "—", "city": "—", "state": "—", "country": "—",
            }
            company_name = company_data["name"]

        company_deals = deals_by_company.get(company_name, [])
        notes_text, deal_dirs = load_company_notes(slug)
        meddpicc_text = load_meddpicc_summary(slug)

        if not notes_text.strip():
            print(f"{prefix} | ⚠️  no notes — skipping")
            progress["profiles_skipped"] += 1
            progress["last_company"] = slug
            save_progress(progress)
            continue

        note_kb = len(notes_text) // 1024
        print(f"{prefix} | {len(company_deals)} deals, ~{note_kb}KB notes...", end="", flush=True)

        try:
            prompt  = build_prompt(company_data, company_deals, notes_text, meddpicc_text)
            profile = call_together(prompt)

            # Build the markdown file with a header
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            header = f"""# {company_data['name']} — Account Profile
**Generated:** {now}
**Deals on record:** {len(company_deals)}
**Total pipeline:** ${sum(d['amount'] for d in company_deals):,.0f}
**Model:** {TOGETHER_MODEL}

---

"""
            with open(profile_path, "w") as f:
                f.write(header + profile)

            print(f" ✅")
            progress["profiles_generated"] += 1

        except Exception as e:
            print(f" ❌ {e}")
            failed.append({"slug": slug, "error": str(e)})
            save_json(FAILED_FILE, failed)
            progress["profiles_failed"] += 1

        progress["last_company"] = slug
        save_progress(progress)
        time.sleep(1)

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    p = progress
    print(f"""
==========================================
COMPANY PROFILE GENERATION COMPLETE
==========================================

Companies processed: {p['total_companies']}
  ✅ Generated : {p['profiles_generated']}
  ⏭  Skipped  : {p['profiles_skipped']} (already existed)
  ❌ Failed    : {p['profiles_failed']}

Profiles saved to: knowledge_base/profiles/

==========================================
Next step: Run pattern_analyst.py to surface
pipeline-level insights.
==========================================
""")


if __name__ == "__main__":
    main()
