"""
Deal Desk — Note Generator
Generates MEDDPICC-structured sales notes for every deal, written by the
assigned persona, grounded in actual CRM data, saved as markdown files.
"""

from tools.llm_client import TOGETHER_BASE_URL, TOGETHER_MODEL, create_completion

import json
import os
import re
import sys
from datetime import datetime, timezone
from collections import defaultdict

import requests
from openai import OpenAI
from dotenv import load_dotenv
from langsmith import traceable

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
PROGRESS_FILE  = os.path.join(NOTES_DIR, "progress.json")
FAILED_FILE    = os.path.join(NOTES_DIR, "_failed.json")
ASSIGNMENTS_F  = os.path.join(KB_DIR, "personas", "deal_assignments.json")
PERSONAS_F     = os.path.join(KB_DIR, "personas", "personas.json")

os.makedirs(NOTES_DIR, exist_ok=True)

TODAY = datetime.now(timezone.utc)

# ── STAGE → NOTES MAPPING ─────────────────────────────────────────────────────
# Map actual Twenty CRM stages to note types
STAGE_NOTES = {
    "MEETING":   ["01_discovery"],
    "NEW":       ["01_discovery", "02_technical_review"],
    "SCREENING": ["01_discovery", "02_technical_review"],
    "PROPOSAL":  ["01_discovery", "02_technical_review", "03_stakeholder_meeting", "04_risk_review"],
    "CUSTOMER":  ["01_discovery", "02_technical_review", "03_stakeholder_meeting", "04_risk_review", "05_close_plan", "_summary", "06_win_analysis"],
    "LOST":      ["01_discovery", "02_technical_review", "03_stakeholder_meeting", "04_risk_review", "05_close_plan", "_summary", "06_loss_analysis"],
}

NOTE_TYPE_LABELS = {
    "01_discovery":           "Discovery Call",
    "02_technical_review":    "Technical Review",
    "03_stakeholder_meeting": "Stakeholder Meeting",
    "04_risk_review":         "Risk Review",
    "05_close_plan":          "Close Plan",
    "_summary":               "Executive Summary",
    "06_win_analysis":        "Win Analysis",
    "06_loss_analysis":       "Loss Analysis",
}



# ── HELPERS ───────────────────────────────────────────────────────────────────
def slugify(text):
    text = text.lower()
    text = re.sub(r"[—–\-]+", "_", text)
    text = re.sub(r"[^a-z0-9_]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def note_length_config(amount):
    if amount > 500_000:
        return "comprehensive", "800 to 1200 words. Cover every section in depth."
    elif amount >= 100_000:
        return "detailed", "500 to 800 words. Solid coverage of all sections."
    else:
        return "concise", "300 to 500 words. Tight and actionable."


def health_signals(deal):
    prob   = deal.get("probability") or 0
    days   = deal.get("daysinpipeline") or 0
    amount = deal.get("amount") or 0
    stage  = deal.get("stage", "")
    signals = []

    if prob < 30:
        signals.append(
            "This deal is at serious risk. Tone should reflect genuine concern, "
            "specific obstacles, and an honest assessment of whether it is still winnable."
        )
    elif prob <= 60:
        signals.append(
            "This deal is uncertain. Reflect mixed signals — some positive momentum "
            "but real obstacles not yet resolved."
        )
    else:
        signals.append(
            "This deal has strong momentum. Reflect confidence, clear next steps, "
            "and urgency to close on time."
        )

    if days > 180:
        signals.append(
            "This deal has been open over 6 months. Something significant has stalled it. "
            "Reflect what caused the delay — budget freeze, champion went quiet, "
            "competitor got in, or internal priorities shifted."
        )
    elif days > 90:
        signals.append(
            "This deal has been open longer than expected. Note what caused the stall "
            "and what re-energised it — or whether it is still stuck."
        )

    close_date = deal.get("close_date") or deal.get("closeDate") or ""
    if close_date:
        try:
            cd = datetime.fromisoformat(close_date.replace("Z", "+00:00"))
            if cd < TODAY and stage != "CUSTOMER":
                signals.append(
                    "This deal's close date has already passed. Reflect slippage — "
                    "what caused it and what the new realistic timeline is."
                )
        except Exception:
            pass

    if amount > 500_000:
        signals.append(
            "This is a large, complex deal. Reflect committee dynamics, procurement "
            "involvement, security review requirements, multiple stakeholders with "
            "competing agendas, and executive sponsorship requirements."
        )
    elif amount >= 100_000:
        signals.append(
            "Mid-size deal. Reflect a small buying committee with both a technical "
            "and a business buyer involved."
        )
    else:
        signals.append(
            "Smaller deal with a faster, simpler dynamic. Likely a single decision maker "
            "or small team. Less process, faster cycle."
        )

    if stage == "LOST":
        signals.append(
            "This deal was lost. The notes are retrospectives. Be brutally honest "
            "about what really went wrong — not the official CRM reason."
        )

    return " ".join(signals)


NOTE_TYPE_INSTRUCTIONS = {
    "01_discovery": """
This is your first or second real conversation with this prospect.
You are learning far more than you are selling.
The most important output: what did you learn AND what are the 2-3 critical things
you still don't know that could make or break this deal?
Flag those unknowns explicitly at the end of the note.
""",
    "02_technical_review": """
You just went deep on their architecture, technical requirements, and implementation complexity.
Focus heavily on: their current technical environment in detail, exactly where AWS fits (and where it doesn't),
what integration work really involves, security and compliance constraints,
and whether their internal engineering team can actually run what we would build for them.
Be specific about services, APIs, data flows, and migration steps.
""",
    "03_stakeholder_meeting": """
You just met with senior leadership — possibly your first time in front of the economic buyer.
Focus on dynamics beyond the content: who spoke most and who went quiet,
where you sensed alignment vs. resistance, whether the business case actually landed
or was politely acknowledged, objections from leadership that didn't come from the technical team,
and whether you left with a stronger or weaker sense that this deal will close.
""",
    "04_risk_review": """
This is an internal note — you are talking to yourself and your manager, not the customer.
Remove all optimism bias. Pretend your bonus depends on being right, not sounding positive.
What are the real reasons this deal might not close? What are you not saying in customer meetings?
What would you do differently if starting over? What does your gut tell you the CRM data doesn't show?
""",
    "05_close_plan": """
Map every remaining step between today and a signed contract.
Every step needs an owner, a deadline, and a dependency.
Cover: remaining technical validation, security review timeline, legal and procurement steps,
executive sign-off requirements, contract terms still open, implementation kickoff prerequisites,
and what the customer still needs from AWS before they can say yes.
Identify the single biggest risk to the timeline.

(If this deal is LOST, write an honest retrospective instead: what you missed in discovery,
where you lost momentum, who you didn't get in front of, what the competitor did better,
and three lessons for the next rep who touches this account.)
""",
    "_summary": """
Write a one-page executive summary of the entire deal history.
Reader: a new rep who has never touched this account, a sales manager doing pipeline review,
and an AI agent doing pattern analysis.

Cover:
- What this company does and why they came to AWS
- The full arc of the deal — how it progressed through each stage
- Key people and their roles
- The central business problem being solved
- Why we won (CUSTOMER) or why we really lost (LOST)
- Current relationship status and expansion potential
- The three most important things anyone working this account needs to know

For LOST deals: end with an honest assessment of whether and when to re-engage.
""",
    "06_win_analysis": """
This is a structured win debrief written after the deal closed.
Reader: sales leadership, deal desk, and the AI agent pattern-matching across the portfolio.

Be analytically rigorous — not a victory lap. Cover:

WHAT ACTUALLY WON IT
- The one or two things that truly differentiated us (not the marketing answer — the real reason)
- What the customer said vs. what you believe actually tipped the decision
- Which competitor(s) we beat and specifically why we won against each

DEAL MECHANICS
- Where in the cycle the deal became real (the moment you knew it would close)
- The economic buyer's actual decision criteria vs. what was stated at the start
- Which objections almost killed the deal and how they were resolved
- What could have gone differently that would have lost us this deal

TEAM & PROCESS
- What worked well in how we ran this deal (worth repeating)
- What slowed us down unnecessarily
- Were there moments where a different rep might have lost this?

EXPANSION INTELLIGENCE
- What are the 2-3 most likely expansion plays in the next 12 months?
- Who are the internal champions to cultivate for the next phase?
- What did we learn about this account that isn't in the CRM?

PATTERN SIGNAL (for the AI agent)
- In one sentence: what type of deal is this, and what made it winnable?
- What does a rep need to know in the first meeting with a similar prospect?
""",
    "06_loss_analysis": """
This is a structured loss debrief. Write it as if you're testifying in front of a deal review board.
No spin. No excuses. Maximum honesty.

Reader: your manager, deal desk, and an AI agent that will use this to identify loss patterns.

ROOT CAUSE (be specific — not "pricing" or "timing")
- What was the single primary reason we lost? Get specific: which feature was missing,
  which relationship we didn't have, which competitor promise we couldn't match,
  which internal process slowed us too much
- What were the secondary factors?
- What did we tell ourselves vs. what actually happened?

WHERE IT WENT WRONG
- At what stage did we actually lose this? (Often it's not where it officially died)
- What signals did we miss or ignore in the first two meetings?
- Which stakeholder did we never get in front of that we should have?
- What did the competitor do that we didn't, couldn't, or chose not to?

COMPETITIVE INTELLIGENCE
- Who won this deal?
- What specific things did they offer (pricing model, feature, relationship, implementation promise)
  that we couldn't or didn't match?
- What will this customer say to the next AWS rep who calls in 18 months?

RE-ENGAGEMENT POTENTIAL
- Is this account worth pursuing again? When and under what conditions?
- What would have to change (on our side or theirs) to make this winnable?
- Who is the right person to call first when re-engaging?

LESSONS (name names — which behavior, not which person)
- Three concrete things a rep should do differently when they see a similar deal
- One thing deal desk or product should know from this loss
""",
}

MASTER_PROMPT_TEMPLATE = """You are {persona_name}, {persona_title} at AWS with {years_experience} years of enterprise sales experience.

Your personality: {communication_style}
Your strengths: {strengths}
Your blind spots: {blind_spots}

You are writing a {note_type_label} note for the deal "{deal_name}" at {company_name}.

DEAL CONTEXT:
- Product / Category: {product_category}
- Deal Value: ${amount:,.0f}
- ARR: ${arr:,.0f} | TCV: ${tcv:,.0f}
- Stage: {stage}
- Probability: {probability}%
- Days in Pipeline: {days_in_pipeline}
- Deal Score: {deal_score}
- Deal Source: {deal_source}
- Close Date: {close_date}{overdue_flag}
- Company Size: {company_size} employees
- Primary Contact: {contact_name} ({contact_title})
- Win Theme: {win_theme}
- Primary Competitor: {competitor}
- Risk Flag: {risk_flag}
- Deal Outcome: {deal_outcome}
- Loss Reason (if applicable): {loss_reason}
- Assignment Reason: {assignment_reason}

HEALTH SIGNALS — weave these naturally into the note, do not list them explicitly:
{health_signals}

---

Write your {note_type_label} note now.

Your notes are your strategic map — not a transcript.
Future-you should read this cold in 3 weeks and know exactly what to do next.

Structure in these 6 sections:

## 1. The Real Business Pain
- The Problem: specific and concrete, not generic
- The Impact: quantified wherever possible — %, $, hours, headcount
- The Value of Solving It: what does good look like for them?

## 2. Stakeholders & Power Dynamics
- Champion: full name, role, why they want this, how strong is their internal influence
- Economic Buyer: who holds the budget, approval threshold, have we met them yet
- Decision Maker: same as economic buyer or different
- Who is NOT in the room yet and why that gap matters
- Competition: who else are they talking to, specific capabilities they seemed to like/dislike

## 3. Why Now — The Compelling Event
- Hard deadline: contract renewal, board meeting, regulatory deadline, fiscal year end
- Trigger event: merger, leadership change, failed project with current vendor
- What happens if they do nothing for 6 months

## 4. Technical & Operational Environment
- Current stack: tools, cloud, data infrastructure, what they're moving away from
- Integration requirements and complexity
- Security and compliance constraints
- Internal technical capability — can their team actually run what we'd build

## 5. Objections & Red Flags
Be brutally honest. Do not sugarcoat for your manager.
- Stated objections: exactly what they said
- Unstated concerns: what you sensed but they didn't say directly
- Political dynamics: who internally might be against this and why
- Deal risk rating: HIGH / MEDIUM / LOW with one specific sentence explaining why

## 6. Mutual Action Plan
Never write "will follow up" — always say exactly what, by when, who owns it.
- My next action with a specific deadline
- Their commitment with a specific deadline
- Next meeting: proposed date, who needs to be in the room, agenda
- What single thing could stall this deal before we meet again

---

TONE RULES:
- Write in first person as {persona_name}
- Sound authentically like {persona_name} — not a generic template
- Be specific: real names, numbers, product names
- Every next step must have a specific date, not "next week"
- Length: {note_length} — {length_guidance}

Note-type specific guidance:
{note_type_instruction}
"""


@traceable(name="together-call", run_type="llm", metadata={"provider": "together", "model": TOGETHER_MODEL})
def call_together(prompt, max_tokens=1200):
    last_err = None
    for attempt in range(3):
        try:
            resp = create_completion(together_client,
                model=TOGETHER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content
        except Exception as e:
            last_err = e
            err_str = str(e)
            if "429" in err_str or "rate" in err_str.lower():
                import time; time.sleep(10 * (attempt + 1))
            elif attempt < 2:
                import time; time.sleep(3)

    raise RuntimeError(f"Qwen generation failed after 3 attempts: {last_err}") from last_err


# ── DATA FETCHING ─────────────────────────────────────────────────────────────
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
        arr
        tcv
        daysinpipeline
        dealscore
        dealsource
        productcategory
        riskflag
        wintheme
        primarycompetetior
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
    try:
        resp = requests.post(DATA_URL, headers=HEADERS, json={"query": DEAL_QUERY}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise ValueError(data["errors"])
        edges = data["data"]["opportunities"]["edges"]
        deals = []
        for edge in edges:
            n = edge["node"]
            amt = (int(n["amount"]["amountMicros"]) / 1_000_000) if n.get("amount") else 0
            poc = n.get("pocname") or {}
            contact_name = f"{poc.get('firstName','') or ''} {poc.get('lastName','') or ''}".strip() or "Unknown"
            deals.append({
                "id":               n["id"],
                "name":             n.get("name", ""),
                "amount":           amt,
                "stage":            n.get("stage", "NEW"),
                "close_date":       n.get("closeDate", ""),
                "probability":      n.get("probability") or 0,
                "assigned_rep":     n.get("assignedRep", ""),
                "arr":              n.get("arr") or 0,
                "tcv":              n.get("tcv") or 0,
                "days_in_pipeline": n.get("daysinpipeline") or 0,
                "deal_score":       n.get("dealscore") or 0,
                "deal_source":      n.get("dealsource") or "Unknown",
                "product_category": n.get("productcategory") or "AWS Services",
                "risk_flag":        n.get("riskflag") or "None identified",
                "win_theme":        n.get("wintheme") or "Not defined",
                "competitor":       n.get("primarycompetetior") or "None identified",
                "contact_name":     contact_name,
                "contact_title":    n.get("poctitle") or "Unknown",
                "company_name":     n.get("companyName") or "Unknown Company",
                "company_size":     int(n.get("companysize") or 0),
                "deal_outcome":     n.get("dealOutcome") or "",
                "loss_reason":      n.get("lossreason") or "",
                "created_at":       n.get("createdAt", ""),
            })
        print(f"  Fetched {len(deals)} deals from Twenty CRM (sorted by amount desc).")
        return deals
    except Exception as e:
        print(f"  ❌ Failed to fetch deals: {e}")
        sys.exit(1)


# ── PROGRESS TRACKING ─────────────────────────────────────────────────────────
def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {
        "total_deals":       0,
        "deals_processed":   0,
        "notes_generated":   0,
        "notes_skipped":     0,
        "notes_failed":      0,
        "last_deal_processed": "",
        "started_at":        TODAY.isoformat(),
        "updated_at":        TODAY.isoformat(),
    }


def save_progress(progress):
    progress["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def load_failures():
    if os.path.exists(FAILED_FILE):
        with open(FAILED_FILE) as f:
            return json.load(f)
    return []


def save_failures(failures):
    with open(FAILED_FILE, "w") as f:
        json.dump(failures, f, indent=2)


# ── NOTE GENERATION ───────────────────────────────────────────────────────────
def build_prompt(deal, persona, note_type, assignment_reason):
    note_length, length_guidance = note_length_config(deal["amount"])
    signals = health_signals(deal)

    close_date_str = ""
    if deal["close_date"]:
        try:
            cd = datetime.fromisoformat(deal["close_date"].replace("Z", "+00:00"))
            close_date_str = cd.strftime("%Y-%m-%d")
        except Exception:
            close_date_str = deal["close_date"][:10] if deal["close_date"] else ""

    overdue = ""
    if close_date_str:
        try:
            cd = datetime.fromisoformat(deal["close_date"].replace("Z", "+00:00"))
            if cd < TODAY and deal["stage"] != "CUSTOMER":
                overdue = " [OVERDUE]"
        except Exception:
            pass

    return MASTER_PROMPT_TEMPLATE.format(
        persona_name        = persona["name"],
        persona_title       = persona["title"],
        years_experience    = persona["years_experience"],
        communication_style = persona["communication_style"],
        strengths           = ", ".join(persona["strengths"]),
        blind_spots         = ", ".join(persona["blind_spots"]),
        note_type_label     = NOTE_TYPE_LABELS[note_type],
        deal_name           = deal["name"],
        company_name        = deal["company_name"],
        product_category    = deal["product_category"],
        amount              = deal["amount"],
        arr                 = deal["arr"],
        tcv                 = deal["tcv"],
        stage               = deal["stage"],
        probability         = deal["probability"],
        days_in_pipeline    = deal["days_in_pipeline"],
        deal_score          = deal["deal_score"],
        deal_source         = deal["deal_source"],
        close_date          = close_date_str,
        overdue_flag        = overdue,
        company_size        = deal["company_size"],
        contact_name        = deal["contact_name"],
        contact_title       = deal["contact_title"],
        win_theme           = deal["win_theme"],
        competitor          = deal["competitor"],
        risk_flag           = deal["risk_flag"],
        deal_outcome        = deal.get("deal_outcome") or "ACTIVE",
        loss_reason         = deal.get("loss_reason") or "N/A",
        assignment_reason   = assignment_reason,
        health_signals      = signals,
        note_length         = note_length,
        length_guidance     = length_guidance,
        note_type_instruction = NOTE_TYPE_INSTRUCTIONS[note_type],
    )


@traceable(
    name="generate-note",
    run_type="chain",
    metadata={},
    tags=["note-generator"],
)
def generate_note(deal, persona, note_type, assignment_reason):
    from langsmith import get_current_run_tree
    rt = get_current_run_tree()
    if rt:
        rt.metadata.update({
            "deal_id":      deal["id"],
            "deal_name":    deal["name"],
            "company":      deal["company_name"],
            "stage":        deal["stage"],
            "deal_outcome": deal.get("deal_outcome") or "ACTIVE",
            "note_type":    note_type,
            "persona":      persona["name"],
            "amount":       deal["amount"],
        })
        rt.tags = [note_type, deal["stage"], deal.get("deal_outcome") or "ACTIVE"]

    prompt    = build_prompt(deal, persona, note_type, assignment_reason)
    max_tok   = 1200 if deal["amount"] > 500_000 else (900 if deal["amount"] >= 100_000 else 600)
    content = call_together(prompt, max_tokens=max_tok)
    model_used = TOGETHER_MODEL

    header = (
        f"# {NOTE_TYPE_LABELS[note_type]}\n"
        f"**Deal:** {deal['name']}  \n"
        f"**Company:** {deal['company_name']}  \n"
        f"**Rep:** {persona['name']}  \n"
        f"**Stage:** {deal['stage']}  \n"
        f"**Amount:** ${deal['amount']:,.0f}  \n"
        f"**Generated:** {TODAY.strftime('%Y-%m-%d')}  \n"
        f"**Model:** {model_used}  \n\n"
        f"---\n\n"
    )
    return header + content


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("\n=== Deal Desk Note Generator ===\n")

    # Load reference data
    print("[1/4] Loading assignments and personas...")
    if not os.path.exists(ASSIGNMENTS_F):
        print("❌ deal_assignments.json not found. Run generate_personas.py first.")
        sys.exit(1)
    with open(ASSIGNMENTS_F) as f:
        assignments_list = json.load(f)
    with open(PERSONAS_F) as f:
        personas_list = json.load(f)

    assignments_by_deal = {a["deal_id"]: a for a in assignments_list}
    personas_by_name    = {p["name"]: p for p in personas_list}
    personas_by_id      = {p["id"]: p for p in personas_list}

    # Fetch deals
    print("[2/4] Fetching deals from Twenty CRM...")
    deals = fetch_deals()

    # Load progress
    print("[3/4] Loading progress (will resume if interrupted)...")
    progress = load_progress()
    failures = load_failures()
    progress["total_deals"] = len(deals)

    # Count stats by note type and stage
    note_type_counts  = defaultdict(int)
    stage_counts      = defaultdict(int)
    generated_in_run  = 0

    print(f"[4/4] Generating notes for {len(deals)} deals...\n")

    for deal_idx, deal in enumerate(deals):
        deal_id    = deal["id"]
        deal_name  = deal["name"]
        stage      = deal["stage"]
        assignment = assignments_by_deal.get(deal_id, {})
        persona_id = assignment.get("assigned_persona_id", "")
        persona    = personas_by_id.get(persona_id) or personas_by_name.get(deal.get("assigned_rep", ""))

        if not persona:
            # Fallback: first persona
            persona = personas_list[0]

        assignment_reason = assignment.get("assignment_reason", "default assignment")

        # Route note types by outcome, not just stage
        deal_outcome = deal.get("deal_outcome", "")
        if deal_outcome == "LOST":
            effective_stage = "LOST"
        elif deal_outcome == "WON" or stage == "CUSTOMER":
            effective_stage = "CUSTOMER"
        else:
            effective_stage = stage
        note_types = STAGE_NOTES.get(effective_stage, ["01_discovery"])

        company_slug = slugify(deal["company_name"])
        deal_slug    = slugify(deal_name)
        deal_dir     = os.path.join(NOTES_DIR, company_slug, deal_slug)
        os.makedirs(deal_dir, exist_ok=True)

        stage_counts[stage] += 1
        deal_had_new_note = False

        for note_type in note_types:
            note_path = os.path.join(deal_dir, f"{note_type}.md")

            if os.path.exists(note_path):
                progress["notes_skipped"] += 1
                note_type_counts[note_type] += 1
                continue

            try:
                content = generate_note(deal, persona, note_type, assignment_reason)
                with open(note_path, "w") as f:
                    f.write(content)

                progress["notes_generated"] += 1
                note_type_counts[note_type] += 1
                generated_in_run += 1
                deal_had_new_note = True

                print(
                    f"  [{deal_idx+1}/{len(deals)}] {deal_name[:38]:<38} "
                    f"| {note_type:<22} ✅"
                )

            except Exception as e:
                progress["notes_failed"] += 1
                err_entry = {
                    "deal_id":   deal_id,
                    "deal_name": deal_name,
                    "note_type": note_type,
                    "error":     str(e),
                }
                failures.append(err_entry)
                save_failures(failures)
                print(
                    f"  [{deal_idx+1}/{len(deals)}] {deal_name[:38]:<38} "
                    f"| {note_type:<22} ❌ {str(e)[:60]}"
                )


        if deal_had_new_note:
            progress["deals_processed"]   += 1
            progress["last_deal_processed"] = deal_name
            save_progress(progress)

    # Final summary
    save_progress(progress)

    total_notes = progress["notes_generated"] + progress["notes_skipped"]
    print("\n==========================================")
    print("NOTE GENERATION COMPLETE")
    print("==========================================\n")
    print(f"Total deals processed:     {len(deals)}")
    print(f"Notes generated this run:  {generated_in_run}")
    print(f"Notes generated (total):   {progress['notes_generated']}")
    print(f"Notes skipped (existed):   {progress['notes_skipped']}")
    print(f"Notes failed:              {progress['notes_failed']}")
    print("\nBy note type:")
    for nt in ["01_discovery", "02_technical_review", "03_stakeholder_meeting", "04_risk_review", "05_close_plan", "_summary"]:
        count = note_type_counts.get(nt, 0)
        if count:
            print(f"  {nt:<28} {count:>4}")
    print("\nBy stage:")
    for stage, count in sorted(stage_counts.items()):
        notes_per = len(STAGE_NOTES.get(stage, ["01_discovery"]))
        print(f"  {stage:<12} {count:>4} deals  →  {notes_per} note(s) each")
    if failures:
        print(f"\n⚠️  {len(failures)} failures logged to: knowledge_base/notes/_failed.json")
    print("\n==========================================")
    print("Next step: Run company_profile_generator.py")
    print("to synthesise company profiles from notes.")
    print("==========================================\n")


if __name__ == "__main__":
    main()
