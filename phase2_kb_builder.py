#!/usr/bin/env python3
"""
Phase 2 KB Builder — Deal Desk Agent
Step 1: Load product mapping (20 products)
Step 2: Generate 80 KB documents via Together AI (4 per product)
Step 3: Generate 4 index files
Step 4: Ingest all content into ChromaDB (4 collections)
Step 5: Generate knowledge_lookup.py
Step 6: Progress tracking (throughout)
"""

from tools.llm_client import TOGETHER_CHAT_URL, TOGETHER_MODEL, request_completion

import os
import sys
import json
import time
import re
import hashlib
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ─────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
KB_DIR       = BASE_DIR / "knowledge_base" / "kb"
NOTES_DIR    = BASE_DIR / "knowledge_base" / "notes"
PROFILES_DIR = BASE_DIR / "knowledge_base" / "profiles"
CHROMA_DIR   = BASE_DIR / "knowledge_base" / "chromadb"

PROGRESS_FILE = KB_DIR / "_generation_progress.json"
FAILED_FILE   = KB_DIR / "_failed_generation.json"

TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")
TOGETHER_URL     = TOGETHER_CHAT_URL

DOC_TYPES = ["battlecards", "capabilities", "icp_profiles", "cost_sheets"]
DOC_TYPE_NAMES = {
    "battlecards":  "Battlecard",
    "capabilities": "Capability Reference",
    "icp_profiles": "ICP Profile",
    "cost_sheets":  "Cost and Resource Sheet",
}


# ── TOGETHER AI ────────────────────────────────────────────────────────────

def llm(prompt: str, max_tokens: int = 2500) -> str | None:
    headers = {
        "Authorization": f"Bearer {TOGETHER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": TOGETHER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": max_tokens,
    }
    for attempt in range(3):
        try:
            resp = request_completion(TOGETHER_URL, headers=headers, json=payload, timeout=120)
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            elif resp.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"    ⚠ Rate limit — waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"    ✗ Together AI {resp.status_code}: {resp.text[:200]}")
                return None
        except Exception as e:
            print(f"    ✗ Request error: {e}")
            if attempt < 2:
                time.sleep(10)
    return None


# ── PROGRESS ───────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {
        "total_products": 20,
        "total_documents_expected": 80,
        "documents_generated": 0,
        "documents_skipped": 0,
        "documents_failed": 0,
        "chromadb_chunks_ingested": 0,
        "current_product": "",
        "current_document_type": "",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": "",
    }


def save_progress(p: dict):
    p["updated_at"] = datetime.now(timezone.utc).isoformat()
    PROGRESS_FILE.write_text(json.dumps(p, indent=2))


def log_failed(slug: str, doc_type: str, reason: str):
    failed = {}
    if FAILED_FILE.exists():
        try:
            failed = json.loads(FAILED_FILE.read_text())
        except Exception:
            pass
    key = f"{slug}/{doc_type}"
    failed[key] = {
        "slug": slug, "doc_type": doc_type, "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    FAILED_FILE.write_text(json.dumps(failed, indent=2))


# ── PROMPTS ────────────────────────────────────────────────────────────────

def build_battlecard_prompt(p: dict) -> str:
    return f"""You are a senior solutions architect and competitive intelligence analyst with deep expertise across AWS, Azure, and GCP.

Write a comprehensive sales battlecard for the following technology category:

Category: {p['base_category']}
Core Capability: {p['core_capability']}
AWS Implementation: {p['aws_equivalent']}
Azure Implementation: {p['azure_equivalent']}
GCP Implementation: {p['gcp_equivalent']}
Deals in Pipeline: {p['deal_count']}

This battlecard is used by field reps in live customer conversations and by AI agents analysing deals in this category. Write it vendor-neutral at the category level but specific at the implementation level.

## {p['base_category']} — Sales Battlecard

### What This Technology Does — In Plain English
Two sentences maximum. What problem does it solve and who buys it. Write for a business buyer not an engineer.

### Market Context
How mature is this technology category. Who are the main buyers right now. What is driving adoption in the market. Two to three sentences.

### Where You Win
Five specific situations where a well-implemented solution in this category beats the status quo or competition. For each:
- The situation
- Why this approach wins
- The proof point or differentiator

### Where You Struggle
Three honest situations where this category faces real resistance. What the objections are. How to reframe.

### Cloud Provider Comparison

| Capability | AWS ({p['aws_equivalent']}) | Azure ({p['azure_equivalent']}) | GCP ({p['gcp_equivalent']}) |
|---|---|---|---|
| Core strength | ... | ... | ... |
| Pricing model | ... | ... | ... |
| Integration depth | ... | ... | ... |
| ML/AI capability | ... | ... | ... |
| Enterprise maturity | ... | ... | ... |

Fill in all table cells with specific, accurate information for this category.

### Top 5 Objections and Responses

For each objection write:
Objection: exactly what the customer says
What they really mean: the underlying concern
Response: specific not generic
Proof point: something concrete to reference

### Discovery Questions
Five questions that reveal if this technology is the right fit for this prospect. Questions that separate genuine need from vague interest.

### Red Flags — Qualify Out When You See These
Three signals that this is not the right category for this customer right now. Better to qualify out early.

### Deal Accelerators
Three things that typically accelerate deals in this category. POCs, pilots, reference customers, free tiers, migration incentives.

### Key Metrics to Capture in Discovery
Five specific numbers the rep needs to build a compelling business case. Include the question to ask to get each number."""


def build_capability_prompt(p: dict) -> str:
    return f"""You are a principal solutions architect with hands-on experience implementing {p['base_category']} solutions using {p['aws_equivalent']}, {p['azure_equivalent']}, and {p['gcp_equivalent']}.

Write a comprehensive capability reference document for {p['base_category']} that sales reps and AI agents can use to understand what can be built, how long it takes, what resources are needed, and what risks to expect.

## {p['base_category']} — Capability Reference

### Core Capabilities
Ten specific things this technology category can do. For each capability write:
- Capability name
- What it does technically in one sentence
- Business outcome it enables
- Typical use case example

### Reference Architectures

Describe three implementation patterns from simple to complex:

Pattern 1 — Quick Start
- Who it is for: company size and technical maturity
- What it includes: services and components
- Build time: X to Y weeks
- Complexity: LOW
- When to recommend this pattern

Pattern 2 — Production Grade
- Who it is for
- What it includes
- Build time: X to Y weeks
- Complexity: MEDIUM
- When to recommend this pattern

Pattern 3 — Enterprise Scale
- Who it is for
- What it includes
- Build time: X to Y weeks
- Complexity: HIGH
- When to recommend this pattern

### Implementation Resource Requirements

Provide realistic resource requirements for {p['base_category']} specifically:

| Role | Small (<$100k) | Medium ($100-500k) | Large (>$500k) |
|---|---|---|---|
| Solutions Architect | ... | ... | ... |
| Professional Services | ... | ... | ... |
| Partner SA | ... | ... | ... |
| Specialist Overlay | ... | ... | ... |
| Timeline | ... | ... | ... |

### Customer Prerequisites
What must be true on the customer side before implementation can begin successfully. Technical requirements, data readiness, team capability, access and permissions.

### Top 5 Implementation Risks
For each risk write:
- Risk name
- Why it happens
- Early warning signs
- Mitigation approach

### Success Metrics
How do you measure that the implementation succeeded. Five measurable outcomes with realistic benchmark ranges for {p['base_category']} specifically.

### Certifications and Compliance Support
What compliance frameworks this technology category typically supports. Regulated industries it is approved for. Certifications customers should ask about."""


def build_icp_prompt(p: dict) -> str:
    return f"""You are a senior GTM strategist who has analysed hundreds of {p['base_category']} deals across AWS, Azure, and GCP.

Write a detailed Ideal Customer Profile that helps field reps quickly qualify or disqualify prospects and helps AI agents assess deal fit for {p['base_category']} solutions.

## {p['base_category']} — Ideal Customer Profile

### The Sweet Spot Customer
Two paragraphs describing the perfect customer for this technology category. Specific enough that a rep can look at a prospect and immediately know yes or no.

### Firmographic Green Flags

Company Size:
What employee ranges indicate good fit and why size matters for this category specifically.

Best Fit Industries:
Top five industries ranked by fit. For each write:
- Industry name
- Why they buy this technology
- What pain drives them
- Typical deal size range

Poor Fit Industries:
Three industries that rarely buy this and why.

Growth Stage:
What growth stage correlates with buying this technology. What triggers the buying decision at each stage.

Technology Stack Signals:
What existing tools in their stack suggest readiness. What stack signals suggest they are not ready yet.

### Buyer Personas

Primary Buyer:
Title, background, what they care about, what language resonates, what they are afraid of, how to get their attention.

Technical Evaluator:
Title, background, what they need to see in a POC, typical objections they raise, how to win them over.

Economic Buyer:
Title, what drives their approval, what ROI framing resonates, typical budget threshold for this category.

The Blocker:
Who typically tries to kill deals in this category and why. How to neutralise them before they derail the deal.

### Behavioural Buying Signals
Eight specific observable things that indicate a prospect is in active buying mode for {p['base_category']}. Things a rep can actually observe or ask about.

### Disqualification Signals — Walk Away When
Six specific signals that this prospect is not a good fit regardless of deal size. Qualify out early and move on.

### Typical Deal Profile

Provide realistic numbers for {p['base_category']} specifically:

| Metric | Small Deal | Medium Deal | Large Deal |
|---|---|---|---|
| Amount | Under $100k | $100k-$500k | Over $500k |
| Sales cycle | ... | ... | ... |
| Stakeholders | ... | ... | ... |
| POC required | ... | ... | ... |
| Partner needed | ... | ... | ... |

### Qualification Questions
Ten questions that quickly reveal if this prospect fits the ICP. Questions that separate genuine fit from marginal fit in the first conversation."""


def build_cost_prompt(p: dict) -> str:
    return f"""You are a senior commercial specialist who structures deals and models financial outcomes for {p['base_category']} engagements across AWS, Azure, and GCP.

Write a comprehensive cost and resource reference document that sales reps and AI agents can use to model deal economics, estimate resources, and structure commercial conversations.

## {p['base_category']} — Cost and Resource Reference

### Pricing Model Overview
How solutions in this category are typically priced. Consumption-based, per-seat, tiered, project-based, or hybrid. Key pricing dimensions — what drives cost up or down. Written for a rep explaining pricing to a CFO in plain language.

### Typical Deal Structures

Land Deal — Getting Started:
Typical scope, what is included, price range, what makes it easy to say yes, common first 90-day structure.

Expand Deal — Growing the Footprint:
What triggers expansion, price range, when to have the expansion conversation, how to position it.

Transform Deal — Full Commitment:
Enterprise-wide scope, price range, commercial structure, multi-year commitment considerations.

### Cloud Provider Pricing Comparison
How AWS ({p['aws_equivalent']}), Azure ({p['azure_equivalent']}), and GCP ({p['gcp_equivalent']}) typically price this category differently. Where one provider is cheaper. Where pricing is roughly equivalent. What hidden costs to watch for on each platform.

### Resource Requirements by Deal Size

Provide realistic resource requirements for {p['base_category']} specifically:

| Role | Small (<$100k) | Medium ($100-500k) | Large (>$500k) |
|---|---|---|---|
| Account Executive | ... | ... | ... |
| Solutions Architect | ... | ... | ... |
| Professional Services | ... | ... | ... |
| Partner Resources | ... | ... | ... |
| Specialist Overlay | ... | ... | ... |

### ROI Framework

Cost Avoidance Model:
What costs does this technology eliminate or reduce. Typical ranges based on company size.

Productivity Model:
What productivity gains can be quantified. Hours saved, headcount redeployed, cycle time reduced.

Revenue Impact Model:
How does this technology impact top line where applicable. Only include where it genuinely applies to {p['base_category']}.

Payback Period:
Typical payback by deal size and implementation pattern. What drives faster or slower payback.

### Business Case — Key Numbers to Capture
Five specific numbers to get from the customer to build a compelling ROI model for {p['base_category']}. Include the question to ask and the typical range to expect.

### Commercial Negotiation Guidance
What customers push back on in this category. What has room to move and what does not. What value-adds to offer instead of discounting. Where to hold firm and why.

### Common Commercial Mistakes
Five pricing or commercial mistakes reps make in this category. For each: the mistake, why it happens, how to avoid it."""


PROMPT_BUILDERS = {
    "battlecards":  build_battlecard_prompt,
    "capabilities": build_capability_prompt,
    "icp_profiles": build_icp_prompt,
    "cost_sheets":  build_cost_prompt,
}


# ── STEP 1: LOAD PRODUCT MAPPING ───────────────────────────────────────────

def load_product_mapping() -> list:
    mapping_file = KB_DIR / "product_mapping.json"
    data = json.loads(mapping_file.read_text())
    products = data["mappings"]
    print(f"\nProducts loaded: {len(products)}")
    print(f"Starting document generation — {len(products) * 4} documents total\n")
    for i, p in enumerate(products, 1):
        print(f"  {i:2d}. {p['crm_product']:<22} → {p['base_category']}")
    print()
    return products


# ── STEP 2: GENERATE 80 KB DOCUMENTS ──────────────────────────────────────

def generate_documents(products: list, progress: dict):
    total = len(products) * 4
    doc_num = 0

    for p in products:
        progress["current_product"] = p["crm_product"]

        for doc_type in DOC_TYPES:
            doc_num += 1
            out_dir = KB_DIR / doc_type
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / f"{p['kb_slug']}.md"

            progress["current_document_type"] = doc_type

            if out_file.exists():
                print(f"  [{doc_num:2d}/{total}] {p['crm_product']} — {DOC_TYPE_NAMES[doc_type]} ⏭")
                progress["documents_skipped"] += 1
                save_progress(progress)
                continue

            prompt = PROMPT_BUILDERS[doc_type](p)
            content = llm(prompt)

            if content and len(content) > 200:
                out_file.write_text(content, encoding="utf-8")
                progress["documents_generated"] += 1
                print(f"  [{doc_num:2d}/{total}] {p['crm_product']} — {DOC_TYPE_NAMES[doc_type]} ✓")
            else:
                # Retry once
                print(f"    ↺ Retrying {p['crm_product']} / {doc_type}...")
                time.sleep(5)
                content = llm(prompt)
                if content and len(content) > 200:
                    out_file.write_text(content, encoding="utf-8")
                    progress["documents_generated"] += 1
                    print(f"  [{doc_num:2d}/{total}] {p['crm_product']} — {DOC_TYPE_NAMES[doc_type]} ✓ (retry)")
                else:
                    progress["documents_failed"] += 1
                    log_failed(p["kb_slug"], doc_type, "Empty response after retry")
                    print(f"  [{doc_num:2d}/{total}] {p['crm_product']} — {DOC_TYPE_NAMES[doc_type]} ✗")

            save_progress(progress)
            time.sleep(1)


# ── STEP 3: GENERATE INDEX FILES ──────────────────────────────────────────

def generate_indexes(products: list):
    print("\n[Step 3] Generating index files...")
    now = datetime.now(timezone.utc).isoformat()

    for doc_type in DOC_TYPES:
        index = {
            "document_type": doc_type.rstrip("s"),
            "total_documents": len(products),
            "generated_at": now,
            "products": [
                {
                    "crm_product": p["crm_product"],
                    "base_category": p["base_category"],
                    "kb_slug": p["kb_slug"],
                    "file_path": f"knowledge_base/kb/{doc_type}/{p['kb_slug']}.md",
                    "deal_count": p["deal_count"],
                }
                for p in products
            ],
        }
        out = KB_DIR / doc_type / "_index.json"
        out.write_text(json.dumps(index, indent=2))
        print(f"  ✓ {out.relative_to(BASE_DIR)}")


# ── CHUNKING ───────────────────────────────────────────────────────────────

def chunk_markdown(text: str, title: str, max_chunk: int = 1400, overlap: int = 100) -> list[dict]:
    """Split markdown on ## headings into chunks of max_chunk chars."""
    chunks = []

    # Split on lines that start a new ## section
    raw_sections = re.split(r'(?m)^(?=## )', text)

    for section in raw_sections:
        section = section.strip()
        if not section:
            continue

        lines = section.split('\n')
        if lines[0].startswith('## '):
            section_name = lines[0][3:].strip()
            body = '\n'.join(lines[1:]).strip()
        else:
            section_name = "Overview"
            body = section

        if len(body) < 100:
            continue

        prefix = f"Document: {title}\nSection: {section_name}\n\n"
        full = prefix + body

        if len(full) <= max_chunk:
            chunks.append({"section": section_name, "content": full})
        else:
            # Split body into overlapping sub-chunks
            start = 0
            while start < len(body):
                end = start + max_chunk - len(prefix)
                sub = body[start:end]
                if len(sub) >= 100:
                    chunks.append({"section": section_name, "content": prefix + sub})
                start += max_chunk - len(prefix) - overlap
                if start >= len(body):
                    break

    return chunks


def make_id(*parts) -> str:
    return hashlib.md5("|".join(str(x) for x in parts).encode()).hexdigest()


# ── STEP 4: CHROMADB INGESTION ─────────────────────────────────────────────

def ingest_chromadb(products: list, progress: dict) -> dict:
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    print("\n[Step 4] Setting up ChromaDB...")
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")

    def upsert_batch(collection, ids, docs, metas):
        if ids:
            collection.upsert(ids=ids, documents=docs, metadatas=metas)
            return len(ids)
        return 0

    total_chunks = 0

    # ── Collection 1: deal_notes ──────────────────────────────────────────
    print("  [1/4] deal_notes — indexing AE notes...")
    col_notes = client.get_or_create_collection("deal_notes", embedding_function=ef)

    # Build metadata index keyed by (company_slug, deal_slug)
    meddpicc_index: dict[tuple, dict] = {}
    for mf in NOTES_DIR.rglob("_meddpicc.json"):
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            key = (mf.parent.parent.name, mf.parent.name)
            raw_name = data.get("deal_name", "")
            product = re.split(r" [——] ", raw_name)[0].strip() if raw_name else ""
            meddpicc_index[key] = {
                "company_name": str(data.get("company", mf.parent.parent.name)),
                "deal_name": str(raw_name),
                "assigned_rep": str(data.get("assigned_rep", "")),
                "stage": str(data.get("stage", "")),
                "amount": str(data.get("amount", "")),
                "product": product,
            }
        except Exception:
            pass

    md_files = list(NOTES_DIR.rglob("*.md"))
    print(f"    {len(md_files)} note markdown files found")

    ids, docs, metas = [], [], []
    for md_file in md_files:
        company_slug = md_file.parent.parent.name
        deal_slug = md_file.parent.name
        note_type = md_file.stem
        meta_base = meddpicc_index.get(
            (company_slug, deal_slug),
            {"company_name": company_slug, "deal_name": deal_slug,
             "assigned_rep": "", "stage": "", "amount": "", "product": ""},
        )
        try:
            text = md_file.read_text(encoding="utf-8")
            title = f"{meta_base['deal_name']} — {note_type}"
            for i, chunk in enumerate(chunk_markdown(text, title)):
                ids.append(make_id("notes", str(md_file), i))
                docs.append(chunk["content"])
                metas.append({
                    "company_name": meta_base["company_name"],
                    "deal_name": meta_base["deal_name"],
                    "note_type": note_type,
                    "assigned_rep": meta_base["assigned_rep"],
                    "stage": meta_base["stage"],
                    "amount": meta_base["amount"],
                    "product": meta_base["product"],
                    "chunk_type": "note",
                    "section": chunk["section"],
                })
                if len(ids) >= 100:
                    total_chunks += upsert_batch(col_notes, ids, docs, metas)
                    ids, docs, metas = [], [], []
        except Exception as e:
            pass

    total_chunks += upsert_batch(col_notes, ids, docs, metas)
    print(f"    ✓ deal_notes: {col_notes.count()} chunks")

    # ── Collection 2: kb_documents ────────────────────────────────────────
    print("  [2/4] kb_documents — indexing KB documents...")
    col_kb = client.get_or_create_collection("kb_documents", embedding_function=ef)

    slug_map = {p["kb_slug"]: p for p in products}
    ids, docs, metas = [], [], []

    for doc_type in DOC_TYPES:
        doc_dir = KB_DIR / doc_type
        if not doc_dir.exists():
            continue
        for md_file in sorted(doc_dir.glob("*.md")):
            if md_file.stem.startswith("_"):
                continue
            slug = md_file.stem
            p = slug_map.get(slug, {})
            try:
                text = md_file.read_text(encoding="utf-8")
                title = f"{p.get('base_category', slug)} — {DOC_TYPE_NAMES.get(doc_type, doc_type)}"
                for i, chunk in enumerate(chunk_markdown(text, title)):
                    ids.append(make_id("kb", str(md_file), i))
                    docs.append(chunk["content"])
                    metas.append({
                        "crm_product": p.get("crm_product", ""),
                        "base_category": p.get("base_category", ""),
                        "document_type": doc_type,
                        "section_name": chunk["section"],
                        "kb_slug": slug,
                        "chunk_type": "kb",
                    })
                    if len(ids) >= 100:
                        total_chunks += upsert_batch(col_kb, ids, docs, metas)
                        ids, docs, metas = [], [], []
            except Exception as e:
                pass

    total_chunks += upsert_batch(col_kb, ids, docs, metas)
    print(f"    ✓ kb_documents: {col_kb.count()} chunks")

    # ── Collection 3: company_profiles ───────────────────────────────────
    print("  [3/4] company_profiles — indexing profiles...")
    col_profiles = client.get_or_create_collection("company_profiles", embedding_function=ef)

    ids, docs, metas = [], [], []
    for md_file in sorted(PROFILES_DIR.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8")
            first_line = text.split('\n')[0]
            company_name = first_line[2:].split('—')[0].strip() if first_line.startswith('# ') else md_file.stem
            for i, chunk in enumerate(chunk_markdown(text, company_name)):
                ids.append(make_id("profiles", str(md_file), i))
                docs.append(chunk["content"])
                metas.append({
                    "company_name": company_name,
                    "section_name": chunk["section"],
                    "chunk_type": "company_profile",
                })
                if len(ids) >= 100:
                    total_chunks += upsert_batch(col_profiles, ids, docs, metas)
                    ids, docs, metas = [], [], []
        except Exception:
            pass

    total_chunks += upsert_batch(col_profiles, ids, docs, metas)
    print(f"    ✓ company_profiles: {col_profiles.count()} chunks")

    # ── Collection 4: meddpicc_scores ─────────────────────────────────────
    print("  [4/4] meddpicc_scores — indexing MEDDPICC scores...")
    col_meddpicc = client.get_or_create_collection("meddpicc_scores", embedding_function=ef)

    ids, docs, metas = [], [], []
    for jf in sorted(NOTES_DIR.rglob("_meddpicc.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            elements = data.get("elements", {})

            missing_flags = [k for k, v in elements.items()
                             if isinstance(v, dict) and v.get("status") == "missing"]

            el_lines = []
            for el_name, el_data in elements.items():
                if isinstance(el_data, dict):
                    status = el_data.get("status", "")
                    gap = el_data.get("gap", "")
                    el_lines.append(f"  {el_name}: {status}" + (f" — Gap: {gap}" if gap else ""))

            raw_name = data.get("deal_name", "")
            product = re.split(r" [——] ", raw_name)[0].strip() if raw_name else ""
            amount_val = data.get("amount", 0) or 0

            text = (
                f"Deal: {raw_name} | Company: {data.get('company', '')} | "
                f"Score: {data.get('overall_score', '')}\n"
                f"Stage: {data.get('stage', '')} | Amount: ${amount_val:,.0f} | "
                f"Rep: {data.get('assigned_rep', '')}\n"
                f"Overall: {data.get('overall_reason', '')}\n"
                f"MEDDPICC Elements:\n{chr(10).join(el_lines)}\n"
                f"Missing flags: {', '.join(missing_flags) if missing_flags else 'none'}\n"
                f"Next Priority: {data.get('next_priority', '')}"
            )

            ids.append(make_id("meddpicc", str(jf)))
            docs.append(text)
            metas.append({
                "company_name": str(data.get("company", "")),
                "deal_name": str(raw_name),
                "overall_score": str(data.get("overall_score", "")),
                "stage": str(data.get("stage", "")),
                "amount": str(amount_val),
                "assigned_rep": str(data.get("assigned_rep", "")),
                "product": product,
                "chunk_type": "meddpicc",
            })

            if len(ids) >= 100:
                total_chunks += upsert_batch(col_meddpicc, ids, docs, metas)
                ids, docs, metas = [], [], []
        except Exception:
            pass

    total_chunks += upsert_batch(col_meddpicc, ids, docs, metas)
    print(f"    ✓ meddpicc_scores: {col_meddpicc.count()} chunks")

    progress["chromadb_chunks_ingested"] = total_chunks
    save_progress(progress)

    return {
        "deal_notes": col_notes.count(),
        "kb_documents": col_kb.count(),
        "company_profiles": col_profiles.count(),
        "meddpicc_scores": col_meddpicc.count(),
        "total": total_chunks,
    }


# ── STEP 5: GENERATE knowledge_lookup.py ──────────────────────────────────

KNOWLEDGE_LOOKUP_SRC = '''#!/usr/bin/env python3
"""
knowledge_lookup(query) — semantic search across 4 ChromaDB collections.
Returns a formatted string ready to inject into an agent prompt.
"""

from pathlib import Path
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

_CHROMA_DIR = Path(__file__).parent / "chromadb"
_client = None
_ef = None
_collections: dict = {}


def _init():
    global _client, _ef, _collections
    if _client is not None:
        return
    _ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    _client = chromadb.PersistentClient(path=str(_CHROMA_DIR))
    for name in ["deal_notes", "kb_documents", "company_profiles", "meddpicc_scores"]:
        try:
            _collections[name] = _client.get_collection(name, embedding_function=_ef)
        except Exception:
            pass


def knowledge_lookup(
    query: str,
    collections: list[str] | None = None,
    n_results: int = 3,
    metadata_filters: dict | None = None,
) -> str:
    """
    Search ChromaDB collections and return formatted results for prompt injection.

    Args:
        query:            Natural language search query
        collections:      List of collection names to search (default: all 4)
        n_results:        Results per collection
        metadata_filters: Optional ChromaDB where clause (applied to all collections)

    Returns:
        Formatted string with SOURCE headers for each result chunk
    """
    _init()

    if collections is None:
        collections = ["deal_notes", "kb_documents", "company_profiles", "meddpicc_scores"]

    source_labels = {
        "deal_notes":       lambda m: f"Deal Note — {m.get(\'company_name\', \'\')} / {m.get(\'deal_name\', \'\')} / {m.get(\'note_type\', \'\')}",
        "kb_documents":     lambda m: f"KB {m.get(\'document_type\', \'\').replace(\'_\', \' \').title()} — {m.get(\'base_category\', \'\')}",
        "company_profiles": lambda m: f"Company Profile — {m.get(\'company_name\', \'\')}",
        "meddpicc_scores":  lambda m: f"MEDDPICC — {m.get(\'company_name\', \'\')} / {m.get(\'deal_name\', \'\')}",
    }

    seen_ids: set[str] = set()
    result_parts: list[str] = []

    for col_name in collections:
        col = _collections.get(col_name)
        if col is None:
            continue
        try:
            kwargs = {"query_texts": [query], "n_results": min(n_results, col.count())}
            if metadata_filters:
                kwargs["where"] = metadata_filters
            results = col.query(**kwargs)

            ids = results.get("ids", [[]])[0]
            docs = results.get("documents", [[]])[0]
            meta_list = results.get("metadatas", [[]])[0]

            for chunk_id, doc, meta in zip(ids, docs, meta_list):
                if chunk_id in seen_ids:
                    continue
                seen_ids.add(chunk_id)
                label = source_labels.get(col_name, lambda m: col_name)(meta)
                result_parts.append(f"[SOURCE: {label}]\\n{doc}")
        except Exception as e:
            result_parts.append(f"[SOURCE: {col_name} — error: {e}]")

    return "\\n\\n".join(result_parts) if result_parts else "[No relevant knowledge found]"


# ── SELF-TEST ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_queries = [
        ("real time fraud detection strengths versus competitors", None),
        ("<account name> deal history and key stakeholders", None),
        ("deals where economic buyer was never engaged", ["meddpicc_scores"]),
        ("ideal customer profile for data pipeline integration", ["kb_documents"]),
        ("ML risk scoring platform implementation requirements", ["kb_documents", "deal_notes"]),
    ]

    print("\\n" + "="*60)
    print("  knowledge_lookup() — Validation Queries")
    print("="*60)

    passed = 0
    for i, (query, cols) in enumerate(test_queries, 1):
        print(f"\\nQuery {i}: {query}")
        kwargs = {"query": query, "n_results": 2}
        if cols:
            kwargs["collections"] = cols
        result = knowledge_lookup(**kwargs)
        lines = result.strip().split("\\n")
        sources = [l for l in lines if l.startswith("[SOURCE:")]
        print(f"  → {len(sources)} result(s) found")
        for s in sources:
            print(f"    {s}")
        if result != "[No relevant knowledge found]":
            passed += 1

    print(f"\\n{'='*60}")
    print(f"  Validation: {passed}/{len(test_queries)} queries returned results")
    print(f"  knowledge_lookup() is ready")
    print("="*60)
'''


def generate_knowledge_lookup():
    out = BASE_DIR / "knowledge_base" / "knowledge_lookup.py"
    out.write_text(KNOWLEDGE_LOOKUP_SRC, encoding="utf-8")
    print(f"\n[Step 5] Generated {out.relative_to(BASE_DIR)}")


# ── MAIN ───────────────────────────────────────────────────────────────────

def main():
    print("\n" + "█" * 60)
    print("  NEXUS SOLUTIONS — PHASE 2 KB BUILDER")
    print("  80 KB Documents + ChromaDB + knowledge_lookup()")
    print("█" * 60)

    if not TOGETHER_API_KEY:
        print("ERROR: TOGETHER_API_KEY not set in .env")
        sys.exit(1)

    start = datetime.now()

    # Step 1
    products = load_product_mapping()

    # Load/init progress
    progress = load_progress()

    # Step 2
    print("[Step 2] Generating 80 KB documents via Together AI...\n")
    generate_documents(products, progress)

    gen_total = progress["documents_generated"] + progress["documents_skipped"]
    print(f"\n  ✓ Step 2 complete — {progress['documents_generated']} generated, "
          f"{progress['documents_skipped']} skipped, {progress['documents_failed']} failed")

    # Step 3
    generate_indexes(products)

    # Step 4
    chroma_counts = ingest_chromadb(products, progress)

    # Step 5
    generate_knowledge_lookup()

    # Run validation
    print("\n[Step 6] Running knowledge_lookup() validation...")
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "knowledge_base" / "knowledge_lookup.py")],
            capture_output=True, text=True, timeout=120,
        )
        print(result.stdout)
        if result.returncode != 0:
            print("  ⚠ Validation stderr:", result.stderr[:300])
    except Exception as e:
        print(f"  ⚠ Validation skipped: {e}")

    elapsed = int((datetime.now() - start).total_seconds())

    print("\n" + "=" * 50)
    print("PHASE 2 COMPLETE")
    print("=" * 50)
    cnt = {dt: sum(1 for p in products if (KB_DIR / dt / (p["kb_slug"] + ".md")).exists())
           for dt in DOC_TYPES}
    print(f"\nProducts processed:       {len(products)}")
    print(f"Documents generated:      {progress['documents_generated']}")
    print(f"  Battlecards:            {cnt['battlecards']}")
    print(f"  Capability docs:        {cnt['capabilities']}")
    print(f"  ICP profiles:           {cnt['icp_profiles']}")
    print(f"  Cost sheets:            {cnt['cost_sheets']}")
    print(f"Documents skipped:        {progress['documents_skipped']}")
    print(f"Documents failed:         {progress['documents_failed']}")
    print(f"\nChromaDB collections:     4")
    print(f"Total chunks ingested:    {chroma_counts['total']}")
    print(f"  Deal notes:             {chroma_counts['deal_notes']}")
    print(f"  KB documents:           {chroma_counts['kb_documents']}")
    print(f"  Company profiles:       {chroma_counts['company_profiles']}")
    print(f"  MEDDPICC scores:        {chroma_counts['meddpicc_scores']}")
    print(f"\nRuntime:                  {elapsed}s ({elapsed//60}m {elapsed%60}s)")
    print(f"\nknowledge_lookup() ready at:")
    print(f"  knowledge_base/knowledge_lookup.py")
    print("\n" + "=" * 50)
    print("Next step: Wire knowledge_lookup() into server.py")
    print("  — call it before each agent with the deal context as query")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
