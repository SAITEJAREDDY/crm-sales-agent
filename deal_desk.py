"""
Deal Desk Agent — Nexus Solutions
6-agent pipeline powered by Together AI LLM + Twenty CRM GraphQL API

Agents:
  1. DataFetcher       — pulls deal + company + contacts from Twenty CRM
  2. RiskAnalyst       — identifies deal risks
  3. OpportunityAnalyst— identifies strengths and upsell angles
  4. DevilsAdvocate    — challenges both analysts
  5. DealArbitrator    — weighs debate, makes recommendation
  6. MemoWriter        — produces polished deal memo, writes back to CRM
"""

from tools.llm_client import TOGETHER_MODEL, create_completion

import os
import sys
import json
import requests
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
from langsmith import traceable
from together import Together as _TogetherClient

# ── knowledge_lookup import ───────────────────────────────────────────────
sys.path.append(os.path.join(os.path.dirname(__file__), "knowledge_base"))
try:
    from knowledge_lookup import knowledge_lookup as _knowledge_lookup
    _KB_AVAILABLE = True
except Exception as _kb_err:
    print(f"[KB] knowledge_lookup unavailable: {_kb_err}")
    _KB_AVAILABLE = False

    def _knowledge_lookup(*args, **kwargs):
        return ""

sys.path.append(os.path.join(os.path.dirname(__file__), "tools"))
try:
    from commercial_economics import build_commercial_economics_table
except Exception as _ce_err:
    print(f"[CommercialEconomics] unavailable: {_ce_err}")

    def build_commercial_economics_table(deal):
        amount = float(deal.get("amount") or 0)
        return "\n".join([
            "3. COMMERCIAL ECONOMICS",
            "",
            "| Metric | Value | Notes |",
            "|---|---:|---|",
            f"| ARR | ${amount:,.0f} | Annual recurring value |",
            f"| 3-Year Project Value | ${amount * 3:,.0f} | ARR over 3 years |",
            f"| Net Annual Price | ${amount:,.0f} | After current discount assumptions |",
            "| Annual Discount Savings | $0 | Savings from current Azure EA tier |",
            f"| Expansion Potential | ${amount * 0.20:,.0f} | Estimated upside if adoption succeeds |",
            "| Model Confidence | LOW, 0/100 | Based on available financial inputs |",
        ])

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── CONFIG ────────────────────────────────────────────────────────────────
TWENTY_URL   = "http://localhost:3000/graphql"
TWENTY_TOKEN = os.environ.get("TWENTY_API_TOKEN", "")
TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")

TWENTY_HEADERS = {
    "Authorization": f"Bearer {TWENTY_TOKEN}",
    "Content-Type": "application/json",
}

_together = _TogetherClient(api_key=TOGETHER_API_KEY)

# ── HELPERS ───────────────────────────────────────────────────────────────

def gql(query: str, variables: dict = {}) -> dict:
    """Execute a GraphQL query against Twenty CRM."""
    resp = requests.post(
        TWENTY_URL,
        headers=TWENTY_HEADERS,
        json={"query": query, "variables": variables},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise ValueError(f"GraphQL error: {data['errors']}")
    return data["data"]


@traceable(name="llm-call", run_type="llm", metadata={"provider": "together", "model": TOGETHER_MODEL})
def llm(prompt: str, system: str = "", max_tokens: int = 1500) -> str:
    """Call Together AI LLM (non-streaming)."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = create_completion(_together,
        model=TOGETHER_MODEL,
        messages=messages,
        temperature=0.3,
        max_tokens=max_tokens,
        stream=False,
    )
    return resp.choices[0].message.content.strip()


def divider(title: str):
    print(f"\n{'═'*60}")
    print(f"  {title}")
    print(f"{'═'*60}")


# ── KNOWLEDGE CONTEXT BUILDER ─────────────────────────────────────────────

def build_knowledge_context(deal: dict) -> str:
    """
    Run targeted knowledge_lookup() queries for a specific deal.
    Returns a formatted context block ready to inject into agent prompts.
    Built once per pipeline run and passed to all agents.

    Customer isolation:
    - Queries 1 (company history) and 2 (MEDDPICC) are hard-filtered to the
      current company so notes from other customers never bleed in.
    - Queries 3-5 (cross-deal patterns) explicitly EXCLUDE the current company
      so they surface only genuinely comparable third-party deals, and their
      section headers make this clear to the LLM.
    - Query 6 (product KB) searches only kb_documents — no customer notes.
    """
    if not _KB_AVAILABLE:
        return "No institutional memory loaded (ChromaDB unavailable)."

    # Prefer the CRM's companyName field; fall back to parsing the deal name
    company = (deal.get("companyName") or
               (deal.get("name", "").split("—")[0].strip() if "—" in deal.get("name", "") else deal.get("name", "")))
    product = deal.get("name", "").split("—")[-1].strip() if "—" in deal.get("name", "") else ""
    stage   = deal.get("stage", "")
    amount  = deal.get("amount", 0) or 0

    if amount > 500_000:
        segment = "enterprise"
    elif amount > 100_000:
        segment = "mid-market"
    else:
        segment = "SMB"

    # ChromaDB where-clause filters
    only_this_company    = {"company_name": {"$eq": company}}
    exclude_this_company = {"company_name": {"$ne": company}}

    sections = []

    # ── Tier 1: company-specific — only this customer's own data ──────────
    company_queries = [
        (
            f"{company} deal history stakeholders relationship",
            ["deal_notes", "company_profiles", "meddpicc_scores"],
            5, only_this_company,
            f"This Customer — {company} — Deal History & Stakeholders",
        ),
        (
            f"{company} MEDDPICC score champion economic buyer qualification",
            ["meddpicc_scores"],
            3, only_this_company,
            f"This Customer — {company} — MEDDPICC Scores",
        ),
    ]

    # ── Tier 2: cross-deal patterns — explicitly OTHER companies only ──────
    # These help the LLM spot patterns but must never be mistaken for facts
    # about the current customer.
    pattern_queries = [
        (
            f"risk factors {product} {segment} deals stalled lost economic buyer",
            ["deal_notes", "meddpicc_scores"],
            4, exclude_this_company,
            f"Pattern: Risk Signals in Similar {segment.title()} {product or 'Deals'} — OTHER Customers",
        ),
        (
            f"win themes strengths opportunity {product} {segment} champion",
            ["deal_notes"],
            4, exclude_this_company,
            f"Pattern: Win Themes in Similar {segment.title()} {product or 'Deals'} — OTHER Customers",
        ),
        (
            f"lost deals {product} {segment} objections competition failure",
            ["deal_notes", "meddpicc_scores"],
            3, exclude_this_company,
            f"Pattern: Loss Reasons in Similar {segment.title()} {product or 'Deals'} — OTHER Customers",
        ),
    ]

    # ── Tier 3: product KB — no customer notes, no filter needed ──────────
    kb_queries = [
        (
            f"{product} capabilities strengths competitors ICP",
            ["kb_documents"],
            5, None,
            f"Product Knowledge — {product or 'KB'}",
        ),
    ]

    for query_str, cols, n, filters, heading in company_queries + pattern_queries + kb_queries:
        try:
            result = _knowledge_lookup(
                query=query_str,
                collections=cols,
                n_results=n,
                metadata_filters=filters,
            )
            if result and result.strip() and "[No relevant knowledge found]" not in result:
                sections.append(f"## {heading}\n{result}")
        except Exception as e:
            print(f"[KB] query failed ({heading}): {e}")

    if not sections:
        return "No institutional memory found for this deal."

    body = "\n\n---\n\n".join(sections)
    return f"""
========================================
INSTITUTIONAL MEMORY
========================================

IMPORTANT — CUSTOMER DATA ISOLATION RULES:
- Sections labelled "This Customer — {company}" contain ONLY {company}'s own
  notes, profiles, and MEDDPICC scores. Cite these freely.
- Sections labelled "Pattern: ... — OTHER Customers" are from DIFFERENT
  companies included only to show market patterns. DO NOT attribute any
  names, amounts, or facts from these sections to {company}.
- Sections labelled "Product Knowledge" are internal KB docs, not customer data.

{body}

========================================
END OF INSTITUTIONAL MEMORY
========================================
"""


CITATION_INSTRUCTION = """MANDATORY — READ BEFORE ANALYSING:

You have been provided INSTITUTIONAL MEMORY below.
This memory contains real notes, deal history, company
profiles, battlecards, and MEDDPICC scores.

Rules:
1. Every risk must reference a specific name, number, or fact from the memory.
2. Every opportunity must reference a win theme or pattern from the memory.
3. Every recommendation must name a specific person, action, and deadline.
4. If a fact exists in the memory — use it. Do not substitute general knowledge.
5. Generic analysis that could apply to any deal will be rejected.

"""


def customer_health_summary(company_name: str) -> str:
    """Return compact account-health context for agent prompts."""
    if not company_name:
        return ""
    path = os.path.join(os.path.dirname(__file__), "knowledge_base", "customer_health_data.json")
    if not os.path.exists(path):
        return ""
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return ""

    target = company_name.strip().lower()
    rec = next(
        (r for r in data.get("customers", [])
         if (r.get("account_info", {}).get("account_name", "")).lower() == target),
        None,
    )
    if not rec:
        return ""

    health = rec.get("health", {})
    portfolio = rec.get("portfolio", {})
    enablement = rec.get("enablement", {})
    revenue = rec.get("revenue", {})
    flags = rec.get("flags", [])[:5]
    actions = rec.get("recommended_actions", [])[:4]
    flag_lines = "\n".join(
        f"- {f.get('severity', 'MEDIUM')}: {f.get('reason')} Action: {f.get('recommended_action')}"
        for f in flags
    ) or "- No active customer-health flags."
    action_lines = "\n".join(f"- {a}" for a in actions) or "- No recommended health actions."

    return f"""
========================================
CUSTOMER HEALTH CONTEXT
========================================
Customer: {rec.get("account_info", {}).get("account_name")}
Health: {health.get("status", "").upper()} ({health.get("score")}/100)
Churn risk: {health.get("churn_risk", "").upper()} ({health.get("churn_risk_score")}/100)
Deals: {portfolio.get("deal_count")} total, {portfolio.get("active_deal_count")} active, {portfolio.get("won_deal_count")} won
Pipeline value: ${portfolio.get("total_pipeline_value", 0):,.0f}
ARR at risk: ${revenue.get("arr_at_risk", 0):,.0f}
Enablement coverage: {enablement.get("coverage_pct", 0)}%
Skills-gap risk: {enablement.get("skills_gap_risk_score", 0)}/100
Days to renewal: {enablement.get("days_to_renewal", "unknown")}

Customer-health flags:
{flag_lines}

Recommended health actions:
{action_lines}
========================================
END CUSTOMER HEALTH CONTEXT
========================================
"""


# ── AGENT 1: DATA FETCHER ─────────────────────────────────────────────────

def fetch_deal_data(opportunity_name_filter: Optional[str] = None) -> dict:
    """Pull deal data from Twenty CRM."""
    divider("AGENT 1 — DATA FETCHER")

    if opportunity_name_filter:
        filter_q = f"""
        query GetOpportunities {{
          opportunities(
            first: 10,
            orderBy: {{ amount: DescNullsLast }},
            filter: {{ name: {{ like: "%{opportunity_name_filter}%" }} }}
          ) {{
            edges {{
              node {{ id name stage amount closeDate createdAt companyName }}
            }}
          }}
        }}
        """
    else:
        filter_q = """
        query GetOpportunities {
          opportunities(
            first: 10,
            orderBy: { amount: DescNullsLast },
            filter: { stage: { in: [PROPOSAL, NEGOTIATION, MEETING] } }
          ) {
            edges {
              node { id name stage amount closeDate createdAt companyName }
            }
          }
        }
        """

    print("  → Fetching opportunities from Twenty CRM...")
    try:
        data = gql(filter_q)
        opps = [e["node"] for e in data["opportunities"]["edges"]]
        print(f"  ✓ Found {len(opps)} active opportunities")
    except Exception as e:
        print(f"  ✗ GraphQL fetch failed: {e}")
        print("  → Using mock data for demo...")
        opps = _mock_opportunities()

    if not opps:
        print("  ✗ No opportunities found")
        sys.exit(1)

    deal = opps[0]
    company_name = deal.get("companyName") or ""
    print(f"\n  Selected deal: {deal['name']}")
    print(f"  Customer     : {company_name or '(not set)'}")
    print(f"  Stage        : {deal['stage']}")
    print(f"  Amount       : ${deal.get('amount', 0):,.0f}")
    print(f"  Close date   : {deal.get('closeDate', 'N/A')}")

    people_query = """
    query GetPeople {
      people(first: 20) {
        edges {
          node {
            id
            name { firstName lastName }
            emails { primaryEmail }
            phones { primaryPhoneNumber }
            jobTitle city
            company { name }
          }
        }
      }
    }
    """
    print("\n  → Fetching contacts from Twenty CRM...")
    try:
        pdata = gql(people_query)
        all_people = [e["node"] for e in pdata["people"]["edges"]]
        if company_name:
            contacts = [p for p in all_people if
                        isinstance(p.get("company"), dict) and
                        p["company"].get("name") == company_name]
            if not contacts:
                # Fallback: partial case-insensitive match
                contacts = [p for p in all_people if
                            company_name.lower() in
                            (p.get("company") or {}).get("name", "").lower()]
        else:
            contacts = all_people[:5]
        print(f"  ✓ Found {len(contacts)} contacts for '{company_name or 'unknown company'}'")
    except Exception as e:
        print(f"  ✗ People fetch failed: {e}")
        contacts = []

    related_query = """
    query GetRelatedDeals {
      opportunities(
        first: 5,
        filter: { stage: { in: [WON, LOST] } },
        orderBy: { amount: DescNullsLast }
      ) {
        edges {
          node { id name stage amount closeDate }
        }
      }
    }
    """
    print("  → Fetching deal history...")
    try:
        rdata = gql(related_query)
        related_deals = [e["node"] for e in rdata["opportunities"]["edges"]]
        print(f"  ✓ Found {len(related_deals)} historical deals")
    except Exception as e:
        print(f"  ✗ Related deals fetch failed: {e}")
        related_deals = []

    context = {
        "deal": deal,
        "company_name": company_name,
        "contacts": contacts,
        "related_deals": related_deals,
        "all_active_deals": opps,
        "fetched_at": datetime.now().isoformat(),
    }

    print(f"\n  ✓ Data package assembled — customer: {company_name or '(unknown)'}, "
          f"{len(contacts)} contacts, {len(related_deals)} historical deals")
    return context


def _mock_opportunities():
    return [{
        "id": "mock-001",
        "name": "ComplianceAI — Enterprise License",
        "stage": "NEGOTIATION",
        "amount": 485000,
        "closeDate": "2025-05-15",
        "createdAt": "2024-11-01",
    }]


# ── POLICY RULES ENGINE ───────────────────────────────────────────────────

def check_policy_rules(deal: dict, contacts: list = None) -> list:
    if contacts is None:
        contacts = []

    amount        = deal.get("amount") or 0
    discount      = deal.get("discount") or 0
    custom_terms  = bool(deal.get("customTerms") or deal.get("custom_terms"))
    contact_count = len(contacts)

    rules = []

    rules.append({
        "rule_name":       "Standard Review",
        "triggered":       True,
        "triggered_by":    "All deals",
        "action_required": "Deal Desk completes standard review checklist",
        "level":           "Deal Desk",
    })

    senior_escalation = amount > 300_000
    rules.append({
        "rule_name":       "Senior Escalation",
        "triggered":       senior_escalation,
        "triggered_by":    f"Deal value ${amount:,.0f} exceeds $300,000 threshold",
        "action_required": "CVP approval required before deal terms are finalised",
        "level":           "CVP",
    })

    discount_flag = discount > 20
    rules.append({
        "rule_name":       "Discount Flag",
        "triggered":       discount_flag,
        "triggered_by":    f"Discount of {discount}% exceeds 20% policy limit",
        "action_required": "DM must provide written justification for discount",
        "level":           "Deal Manager",
    })

    rules.append({
        "rule_name":       "Delivery Risk",
        "triggered":       custom_terms,
        "triggered_by":    "Deal contains custom contractual terms",
        "action_required": "DM must attach a delivery plan before contract execution",
        "level":           "Deal Manager",
    })

    single_stakeholder = contact_count <= 1
    rules.append({
        "rule_name":       "Single Stakeholder",
        "triggered":       single_stakeholder,
        "triggered_by":    f"Only {contact_count} contact(s) on record for this deal",
        "action_required": "Pursuit Lead must flag and expand stakeholder map",
        "level":           "Pursuit Lead",
    })

    return rules


# ── AGENT 2: RISK ANALYST ─────────────────────────────────────────────────

def risk_analyst(context: dict, knowledge_context: str = "") -> str:
    divider("AGENT 2 — RISK ANALYST")
    print("  → Analyzing deal risks...")

    deal     = context["deal"]
    contacts = context["contacts"]
    related  = context["related_deals"]

    deal_summary = f"""
DEAL: {deal['name']}
Stage: {deal['stage']}
Amount: ${deal.get('amount', 0):,.0f}
Close Date: {deal.get('closeDate', 'Unknown')}
Contacts on file: {len(contacts)}
Historical deals (WON/LOST): {len(related)}
Lost deals in history: {sum(1 for d in related if d.get('stage') == 'LOST')}
"""

    contact_summary = ""
    for c in contacts[:5]:
        name = f"{c['name']['firstName']} {c['name']['lastName']}" if isinstance(c.get('name'), dict) else str(c.get('name', ''))
        contact_summary += f"  - {name} | {c.get('jobTitle', 'N/A')}\n"

    kb_block = f"\n{CITATION_INSTRUCTION}\n{knowledge_context}\n" if knowledge_context else ""

    prompt = f"""{kb_block}You are a senior deal risk analyst at Nexus Solutions, a B2B fintech software company.

DEAL CONTEXT:
{deal_summary}

KEY CONTACTS:
{contact_summary if contact_summary else '  No contacts on file'}

Analyze this deal and identify ALL risks. Structure your response as:

🔴 HIGH RISKS:
[List 2-3 major risks with specific reasoning]

🟡 MEDIUM RISKS:
[List 2-3 moderate risks]

⚠️ FLAGS TO WATCH:
[List 2-3 early warning signals]

RISK VERDICT: [ONE sentence summary of overall risk level: LOW / MEDIUM / HIGH / CRITICAL]

Be specific, direct, and concise. Every risk must reference a specific fact from the institutional memory above."""

    response = llm(prompt, max_tokens=800)
    print(response)
    return response


# ── AGENT 3: OPPORTUNITY ANALYST ─────────────────────────────────────────

def opportunity_analyst(context: dict, knowledge_context: str = "") -> str:
    divider("AGENT 3 — OPPORTUNITY ANALYST")
    print("  → Analyzing deal strengths and upside...")

    deal    = context["deal"]
    contacts = context["contacts"]
    related  = context["related_deals"]

    won_deals = [d for d in related if d.get("stage") == "WON"]
    total_won_value = sum(d.get("amount", 0) for d in won_deals)

    kb_block = f"\n{CITATION_INSTRUCTION}\n{knowledge_context}\n" if knowledge_context else ""

    prompt = f"""{kb_block}You are a senior opportunity analyst at Nexus Solutions, a B2B fintech software company.

DEAL CONTEXT:
Deal: {deal['name']}
Stage: {deal['stage']}
Amount: ${deal.get('amount', 0):,.0f}
Close Date: {deal.get('closeDate', 'Unknown')}
Contacts engaged: {len(contacts)}
Previous WON deals with this account: {len(won_deals)} (total value: ${total_won_value:,.0f})

Analyze the STRENGTHS and OPPORTUNITIES for this deal:

✅ KEY STRENGTHS:
[List 2-3 specific deal strengths grounded in the institutional memory]

🚀 UPSELL / EXPANSION OPPORTUNITIES:
[List 2-3 realistic expansion angles]

🤝 RELATIONSHIP SIGNALS:
[Assess stakeholder engagement based on notes and contact count]

💡 WIN STRATEGY:
[ONE paragraph — the clearest path to closing this deal]

OPPORTUNITY VERDICT: [ONE sentence — confidence level: LOW / MEDIUM / HIGH]

Every opportunity must reference a specific signal from the institutional memory above."""

    response = llm(prompt, max_tokens=800)
    print(response)
    return response


# ── AGENT 4: DEVIL'S ADVOCATE ─────────────────────────────────────────────

def devils_advocate(context: dict, risk_analysis: str, opportunity_analysis: str, knowledge_context: str = "") -> str:
    divider("AGENT 4 — DEVIL'S ADVOCATE")
    print("  → Challenging both analyses...")

    deal = context["deal"]

    kb_block = f"\n{CITATION_INSTRUCTION}\n{knowledge_context}\n" if knowledge_context else ""

    prompt = f"""{kb_block}You are a skeptical deal committee member at Nexus Solutions. Your job is to challenge assumptions and stress-test reasoning.

THE DEAL: {deal['name']} | ${deal.get('amount', 0):,.0f} | Stage: {deal['stage']}

RISK ANALYST SAID:
{risk_analysis}

OPPORTUNITY ANALYST SAID:
{opportunity_analysis}

Challenge both analyses using specific contradictions from the institutional memory above:

❓ CHALLENGING THE RISK ANALYST:
[2 points where risks may be overstated or missing bigger picture — cite specific memory facts]

❓ CHALLENGING THE OPPORTUNITY ANALYST:
[2 points where optimism may be unwarranted — cite specific lost deal patterns from memory]

🎯 THE REAL QUESTION:
[The single most important unresolved question that determines if this deal closes]

DEVIL'S VERDICT: [Should we invest more resources? YES / CONDITIONAL / NO — one sentence why, citing a specific memory fact]"""

    response = llm(prompt, max_tokens=700)
    print(response)
    return response


# ── AGENT 5: DEAL ARBITRATOR ──────────────────────────────────────────────

def deal_arbitrator(context: dict, risk: str, opportunity: str, devils: str, knowledge_context: str = "") -> str:
    divider("AGENT 5 — DEAL ARBITRATOR")
    print("  → Synthesizing debate and making recommendation...")

    deal   = context["deal"]
    amount = deal.get("amount", 0)

    kb_block = f"\n{CITATION_INSTRUCTION}\n{knowledge_context}\n" if knowledge_context else ""

    prompt = f"""{kb_block}You are the VP of Sales at Nexus Solutions chairing the deal review committee.

Deal: {deal['name']}
Amount: ${amount:,.0f}
Stage: {deal['stage']}
Close Date: {deal.get('closeDate', 'Unknown')}

RISK ANALYST:
{risk}

OPPORTUNITY ANALYST:
{opportunity}

DEVIL'S ADVOCATE:
{devils}

Make the final call. Reference MEDDPICC scores from the institutional memory when justifying confidence.

COMMITTEE DECISION: [PURSUE AGGRESSIVELY / PURSUE WITH CONDITIONS / HOLD / WALK AWAY]

REASONING:
[2-3 sentences, citing specific people and facts from the institutional memory]

CONDITIONS (if applicable):
[List any conditions that must be met to proceed]

RECOMMENDED NEXT ACTIONS:
1. [Immediate action — this week — name a specific person]
2. [Short-term action — next 30 days]
3. [Escalation trigger — when to re-review]

DEAL SCORE: [X/10] — [one line rationale]"""

    response = llm(prompt, max_tokens=700)
    print(response)
    return response


# ── AGENT 6: MEMO WRITER ──────────────────────────────────────────────────

def memo_writer(context: dict, risk: str, opportunity: str, devils: str, arbitration: str, knowledge_context: str = "") -> str:
    divider("AGENT 6 — MEMO WRITER")
    print("  → Drafting executive deal memo...")

    deal     = context["deal"]
    contacts = context["contacts"]
    now      = datetime.now().strftime("%B %d, %Y")

    contact_block = ""
    for c in contacts[:4]:
        name  = f"{c['name']['firstName']} {c['name']['lastName']}" if isinstance(c.get('name'), dict) else str(c.get('name', ''))
        email = c.get('emails', {}).get('primaryEmail', '') if isinstance(c.get('emails'), dict) else ''
        contact_block += f"  • {name} — {c.get('jobTitle', 'N/A')} | {email}\n"

    kb_block = f"\n{CITATION_INSTRUCTION}\n{knowledge_context}\n" if knowledge_context else ""
    commercial_economics_table = build_commercial_economics_table(deal)

    prompt = f"""{kb_block}You are a senior deal desk analyst at Nexus Solutions. Write a professional, concise executive deal memo.
Every section must reference specific facts from the institutional memory above. Name real people. Cite real numbers.
The COMMERCIAL ECONOMICS section below is generated by code from the financial model. Keep that table exactly as provided: do not change metric names, values, formatting, or notes. Reference those values in narrative only when relevant.

DEAL DATA:
Name: {deal['name']}
Stage: {deal['stage']}
Amount: ${deal.get('amount', 0):,.0f}
Close Date: {deal.get('closeDate', 'Unknown')}
Date: {now}

KEY CONTACTS:
{contact_block if contact_block else '  Not on file'}

RISK ANALYSIS SUMMARY:
{risk}

OPPORTUNITY SUMMARY:
{opportunity}

DEVIL'S ADVOCATE:
{devils}

COMMITTEE DECISION:
{arbitration}

Write the memo now. Do not paste the prior agents verbatim; synthesize them into a crisp executive artifact.

DEAL DESK MEMO - NEXUS SOLUTIONS
Date: {now}
Deal: {deal['name']} | ${deal.get('amount', 0):,.0f} | {deal['stage']} | Close: {deal.get('closeDate', 'Unknown')}

1. EXECUTIVE DECISION
- Recommendation: [PURSUE AGGRESSIVELY / PURSUE WITH CONDITIONS / HOLD / WALK AWAY]
- Deal score: [X/10]
- Confidence: [LOW / MEDIUM / HIGH]
- One-line rationale: [the clearest reason for the decision]

2. BOARD-LEVEL SUMMARY
[3-4 sentences max. Explain what this deal is, why it matters, what the institutional memory reveals that CRM fields alone do not, and the specific condition that determines whether we should invest.]

{commercial_economics_table}

4. DECISION DRIVERS
| Driver | Evidence | Impact |
|---|---|---|
| Commercial value | [specific amount, stage, close date, or expansion signal] | [why it matters] |
| Buyer readiness | [specific stakeholder / MEDDPICC / contact evidence] | [why it matters] |
| Execution risk | [specific risk or gap] | [why it matters] |

5. RISK REGISTER
| Risk | Evidence | Severity | Mitigation |
|---|---|---|---|
| [risk] | [specific fact/citation] | [High/Med/Low] | [owner + action] |
| [risk] | [specific fact/citation] | [High/Med/Low] | [owner + action] |
| [risk] | [specific fact/citation] | [High/Med/Low] | [owner + action] |

6. OPPORTUNITY CASE
- Primary win path: [the concrete path to close]
- Expansion angle: [realistic upsell/cross-sell motion]
- Differentiator to emphasize: [product/competitive/customer-specific evidence]

7. COMMITTEE SYNTHESIS
[Resolve the tension between Risk, Opportunity, and Devil's Advocate. Name where they agree, where they conflict, and which evidence should carry the most weight.]

8. STAKEHOLDER AND ENABLEMENT PLAN
- Economic buyer / approver: [name or "not confirmed"]
- Champion: [name or "not confirmed"]
- Stakeholder gap: [missing person, role, or decision-process issue]
- Enablement motion: [training/adoption action that would reduce risk or improve conversion]

9. APPROVAL CONDITIONS
[List the conditions required before approval, or "None". Make each condition testable.]

10. NEXT ACTIONS
1. [owner] - [action] - [deadline]
2. [owner] - [action] - [deadline]
3. [owner] - [action] - [deadline]

11. FINAL CALL
[One decisive paragraph. State the recommendation, the non-negotiable condition, and what leadership should inspect on the next review.]

12. ENABLEMENT ROADMAP, RECOMMENDATIONS, AND ACTIONS
| Phase | Objective | Recommended Actions | Owner | Timing | Success Measure |
|---|---|---|---|---|---|
| Pre-close | [reduce buyer/stakeholder risk before signature] | [2-3 concrete enablement actions, such as skills assessment, exec workshop, champion enablement, architecture review, or adoption plan] | [AE/SA/CSM/Partner] | [date or time window] | [measurable outcome] |
| First 30 days | [turn purchase intent into adoption readiness] | [training, onboarding, governance, migration-readiness, or stakeholder alignment actions] | [owner] | [time window] | [measurable outcome] |
| 60-90 days | [protect consumption, renewal confidence, and expansion path] | [advanced enablement, usage review, optimization, or expansion play] | [owner] | [time window] | [measurable outcome] |

Enablement recommendation:
[One paragraph explaining which enablement motion should be attached to this deal, why it reduces the top risk, and how it supports the committee recommendation.]

Immediate enablement actions:
1. [owner] - [specific action] - [deadline]
2. [owner] - [specific action] - [deadline]
3. [owner] - [specific action] - [deadline]"""

    response = llm(prompt, max_tokens=2200)
    print(response)
    return response


def write_memo_to_crm(deal_id: str, memo: str) -> bool:
    """Write the deal memo back to Twenty CRM as a note on the opportunity."""
    divider("WRITING MEMO BACK TO CRM")
    print(f"  → Attaching memo to deal ID: {deal_id}")

    mutation = """
    mutation CreateNote($input: NoteCreateInput!) {
      createNote(data: $input) {
        id title body createdAt
      }
    }
    """

    note_input = {
        "title": f"Deal Desk Memo — {datetime.now().strftime('%Y-%m-%d')}",
        "body": memo,
    }

    try:
        result = gql(mutation, {"input": note_input})
        note_id = result.get("createNote", {}).get("id", "")
        print(f"  ✓ Memo written to CRM — Note ID: {note_id}")
        return True
    except Exception as e:
        print(f"  ⚠ Could not write to CRM: {e}")
        print("  → Memo saved locally instead")
        return False


# ── MAIN PIPELINE ─────────────────────────────────────────────────────────

def run_pipeline(deal_data: dict, use_knowledge_base: bool = True) -> dict:
    """
    Run the 6-agent pipeline on a pre-fetched deal context dict.
    Returns dict with all agent outputs.
    """
    deal = deal_data["deal"]

    if use_knowledge_base and _KB_AVAILABLE:
        print(f"\n  [KB] Building knowledge context for {deal['name']}...")
        knowledge_context = build_knowledge_context(deal)
        print(f"  [KB] Context built — {len(knowledge_context):,} chars")
    else:
        knowledge_context = ""

    health_context = customer_health_summary(deal.get("companyName") or deal.get("company") or "")
    if health_context:
        knowledge_context = f"{knowledge_context}\n\n{health_context}" if knowledge_context else health_context
        print(f"  [Health] Customer health context added — {len(health_context):,} chars")

    risk         = risk_analyst(deal_data, knowledge_context)
    opportunity  = opportunity_analyst(deal_data, knowledge_context)
    devils       = devils_advocate(deal_data, risk, opportunity, knowledge_context)
    arbitration  = deal_arbitrator(deal_data, risk, opportunity, devils, knowledge_context)
    memo         = memo_writer(deal_data, risk, opportunity, devils, arbitration, knowledge_context)

    return {
        "risk": risk,
        "opportunity": opportunity,
        "devils": devils,
        "arbitration": arbitration,
        "memo": memo,
        "knowledge_context_size": len(knowledge_context),
        "use_knowledge_base": use_knowledge_base,
    }


def run_deal_desk(opportunity_filter: Optional[str] = None, use_knowledge_base: bool = True):
    print("\n" + "█"*60)
    print("  NEXUS SOLUTIONS — DEAL DESK AGENT")
    print(f"  Powered by Together AI ({TOGETHER_MODEL}) + Twenty CRM")
    print(f"  Knowledge Base: {'ON' if use_knowledge_base else 'OFF'}")
    print("█"*60)

    start = datetime.now()

    # Agent 1: Fetch
    context = fetch_deal_data(opportunity_filter)

    # Agents 2-6 via unified pipeline
    results = run_pipeline(context, use_knowledge_base=use_knowledge_base)

    # Write back to CRM
    deal_id = context["deal"].get("id", "")
    write_memo_to_crm(deal_id, results["memo"])

    elapsed = (datetime.now() - start).seconds
    divider(f"COMPLETE — {elapsed}s")
    print(f"  Deal   : {context['deal']['name']}")
    print(f"  Stage  : {context['deal']['stage']}")
    print(f"  Amount : ${context['deal'].get('amount', 0):,.0f}")
    print(f"  KB used: {results['use_knowledge_base']} ({results['knowledge_context_size']:,} chars)")
    print(f"  Time   : {elapsed}s\n")

    # Short summary
    divider("DEAL DESK SUMMARY")
    risk        = results["risk"]
    opportunity = results["opportunity"]
    arbitration = results["arbitration"]
    summary_prompt = f"""Based on this deal analysis, give a 5-line executive summary:
Deal: {context['deal']['name']} | ${context['deal'].get('amount',0):,.0f} | {context['deal']['stage']}

Risk verdict: {risk.split('RISK VERDICT:')[-1].strip()[:200] if 'RISK VERDICT:' in risk else risk[-200:]}
Opportunity verdict: {opportunity.split('OPPORTUNITY VERDICT:')[-1].strip()[:200] if 'OPPORTUNITY VERDICT:' in opportunity else opportunity[-200:]}
Committee decision: {arbitration.split('COMMITTEE DECISION:')[-1].strip()[:200] if 'COMMITTEE DECISION:' in arbitration else arbitration[:200]}

Format:
🎯 DEAL: [name and amount]
⚡ VERDICT: [committee decision in one line]
🔴 TOP RISK: [single biggest risk]
✅ TOP STRENGTH: [single biggest opportunity]
📋 NEXT ACTION: [most important next step]"""

    summary = llm(summary_prompt, max_tokens=300)
    print(summary)

    print()
    save = input("  Save full memo to file? [y/N]: ").strip().lower()
    if save == "y":
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        deal_name_safe = context["deal"]["name"].replace(" ", "_").replace("—", "-")[:40]
        mode_tag = "with_kb" if use_knowledge_base else "without_kb"
        filename = f"memo_{deal_name_safe}_{mode_tag}_{timestamp}.txt"
        with open(filename, "w") as f:
            f.write(f"DEAL DESK MEMO\n")
            f.write(f"Generated: {datetime.now().isoformat()}\n")
            f.write(f"Deal: {context['deal']['name']}\n")
            f.write(f"Mode: {'WITH' if use_knowledge_base else 'WITHOUT'} institutional memory\n")
            f.write(f"Knowledge context size: {results['knowledge_context_size']:,} chars\n\n")
            f.write("="*60 + "\nRISK ANALYSIS\n" + "="*60 + "\n")
            f.write(risk + "\n\n")
            f.write("="*60 + "\nOPPORTUNITY ANALYSIS\n" + "="*60 + "\n")
            f.write(opportunity + "\n\n")
            f.write("="*60 + "\nDEVIL'S ADVOCATE\n" + "="*60 + "\n")
            f.write(results["devils"] + "\n\n")
            f.write("="*60 + "\nCOMMITTEE DECISION\n" + "="*60 + "\n")
            f.write(arbitration + "\n\n")
            f.write("="*60 + "\nFINAL MEMO\n" + "="*60 + "\n")
            f.write(results["memo"] + "\n")
        print(f"\n  ✓ Full memo saved: {filename}\n")
    else:
        print("  Memo not saved.\n")

    return results["memo"]


if __name__ == "__main__":
    filter_name         = sys.argv[1] if len(sys.argv) > 1 else None
    use_kb              = "--no-kb" not in sys.argv
    run_deal_desk(filter_name, use_knowledge_base=use_kb)
