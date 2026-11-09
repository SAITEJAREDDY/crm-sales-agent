"""
Deal Desk Agent — Flask API Server
Serves the UI and proxies agent pipeline calls
"""

from tools.llm_client import TOGETHER_MODEL, create_completion

from flask import Flask, jsonify, request, Response, stream_with_context, send_from_directory, send_file
from flask_cors import CORS
import requests
import json
import threading
import queue
from datetime import datetime, timedelta
from typing import Optional
import sys
import os
import sqlite3
import uuid
import statistics
from contextlib import contextmanager
from io import BytesIO
from dotenv import load_dotenv
from langsmith import traceable
from langsmith import Client as _LSClient

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── Knowledge base ────────────────────────────────────────────────────────
import re
import re as _re
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_base"))
try:
    from deal_desk import build_knowledge_context, CITATION_INSTRUCTION
    _KB_AVAILABLE = True
except Exception as _kb_import_err:
    print(f"[KB] unavailable: {_kb_import_err}")
    _KB_AVAILABLE = False
    def build_knowledge_context(_d): return ""
    CITATION_INSTRUCTION = ""

try:
    from knowledge_lookup import knowledge_lookup as _kb_lookup
    _KB_LOOKUP_AVAILABLE = True
except Exception as _kbl_err:
    print(f"[KB Lookup] unavailable: {_kbl_err}")
    _KB_LOOKUP_AVAILABLE = False
    def _kb_lookup(query, collections=None, n_results=3, **kw): return "[Knowledge base unavailable]"

# ── Pipeline analysis tool ────────────────────────────────────────────────────
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
try:
    from analyze_pipeline import analyze_pipeline as _analyze_pipeline
    _PIPELINE_AVAILABLE = True
except Exception as _pa_err:
    print(f"[Pipeline] unavailable: {_pa_err}")
    _PIPELINE_AVAILABLE = False
    def _analyze_pipeline(filter_by=None, focus="full"): return {"error": "Pipeline tool unavailable", "data": {}, "summary": "Pipeline tool unavailable"}

try:
    import importlib.util as _ilu
    _fm_spec = _ilu.spec_from_file_location(
        "financial_model_runner",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "financial_model_runner.py"),
    )
    _fm_mod = _ilu.module_from_spec(_fm_spec)
    _fm_spec.loader.exec_module(_fm_mod)
    _fm_run  = _fm_mod.run
    _fm_find = _fm_mod.find_deal
    _FM_AVAILABLE = True
except Exception as _fm_err:
    print(f"[FinancialModel] unavailable: {_fm_err}")
    _FM_AVAILABLE = False
    def _fm_run(deal, api_key=""): return {"status": "error", "error": "financial_model module unavailable"}
    def _fm_find(deal_name="", deal_id=""): return None

try:
    from commercial_economics import build_commercial_economics_table
except Exception as _ce_err:
    print(f"[CommercialEconomics] unavailable: {_ce_err}")

    def build_commercial_economics_table(deal):
        try:
            amount = float(deal.get("amount") or 0)
        except Exception:
            amount = 0.0
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

try:
    from eval_judge import judge_run as _judge_run
    _EVAL_JUDGE_AVAILABLE = True
except Exception as _ej_err:
    print(f"[EvalJudge] unavailable: {_ej_err}")
    _EVAL_JUDGE_AVAILABLE = False
    def _judge_run(deal, outputs): return {}

try:
    from enablement_agent import build_enablement_recommendation, recommend_for_deal
    from tools.enablement_rules import evaluate_enablement_triggers, highest_severity
    from tools.enablement_eval import evaluate_recommendation
    _ENABLEMENT_AVAILABLE = True
except Exception as _en_err:
    print(f"[Enablement] unavailable: {_en_err}")
    _ENABLEMENT_AVAILABLE = False
    def build_enablement_recommendation(*args, **kwargs): return {}
    def recommend_for_deal(*args, **kwargs): return {}
    def evaluate_enablement_triggers(_r): return []
    def highest_severity(_t): return "LOW"
    def evaluate_recommendation(*args, **kwargs): return {"passed": False}

def _slugify(s: str) -> str:
    return _re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── SERVICE CATALOG ───────────────────────────────────────────────────────────
_SERVICE_CATALOG_PATH = os.path.join(BASE_DIR, "service_catalog.json")
try:
    with open(_SERVICE_CATALOG_PATH) as _sc_f:
        SERVICE_CATALOG = json.load(_sc_f)
except Exception as _sc_err:
    print(f"[ServiceCatalog] unavailable: {_sc_err}")
    SERVICE_CATALOG = {"azure_services": [], "open_source": []}

app = Flask(__name__)
CORS(app)

# ── CONFIG ────────────────────────────────────────────────────────────────
TWENTY_URL   = "http://localhost:3000/graphql"
TWENTY_TOKEN = os.environ.get("TWENTY_API_TOKEN", "")
TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")
# TOGETHER_MODEL comes from the shared client configuration for all workflows.

TWENTY_HEADERS = {
    "Authorization": f"Bearer {TWENTY_TOKEN}",
    "Content-Type": "application/json",
}

# ── METRICS DB ────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics.db")

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id TEXT, deal_id TEXT, deal_name TEXT,
            deal_stage TEXT, deal_amount REAL,
            started_at TIMESTAMP, completed_at TIMESTAMP,
            total_latency_ms INTEGER, success BOOLEAN,
            failure_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT, agent_num INTEGER, agent_name TEXT,
            started_at TIMESTAMP, completed_at TIMESTAMP,
            latency_ms INTEGER, tokens_generated INTEGER,
            tokens_per_sec REAL, success BOOLEAN,
            output_length INTEGER
        );
        CREATE TABLE IF NOT EXISTS crm_operations (
            run_id TEXT, operation TEXT, deal_id TEXT,
            latency_ms INTEGER, success BOOLEAN,
            fields_written TEXT, error TEXT, timestamp TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS financial_runs (
            run_id TEXT, deal_id TEXT, deal_name TEXT,
            win_rate REAL, discount REAL,
            forecast_worst REAL, forecast_avg REAL,
            forecast_best REAL, risk_level TEXT,
            written_to_crm BOOLEAN, refinement_count INTEGER,
            timestamp TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS arch_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_name TEXT,
            verdict TEXT,
            confidence TEXT,
            sow_text TEXT,
            created_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_name TEXT,
            recipient_email TEXT,
            sent_at TIMESTAMP,
            success BOOLEAN
        );
        CREATE TABLE IF NOT EXISTS eval_runs (
            run_id TEXT PRIMARY KEY,
            timestamp TIMESTAMP,
            dataset_version TEXT,
            deals_evaluated INTEGER,
            average_final_score REAL,
            pass_count INTEGER,
            fail_count INTEGER,
            langsmith_experiment_url TEXT
        );
        CREATE TABLE IF NOT EXISTS eval_results (
            run_id TEXT,
            deal_id TEXT,
            deal_name TEXT,
            stage TEXT,
            risk_analyst_score REAL,
            devil_advocate_score REAL,
            financial_simulation_score REAL,
            arbitrator_score REAL,
            sow_writer_score REAL,
            pipeline_score REAL,
            raw_total REAL,
            final_score REAL,
            passed BOOLEAN,
            full_json TEXT
        );
        """)

@contextmanager
def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

def record_pipeline_run(run_id, deal_id, deal_name, deal_stage, deal_amount,
                        started_at, completed_at, latency_ms, success, failure_reason=None):
    with get_db() as con:
        con.execute(
            "INSERT INTO pipeline_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, deal_id, deal_name, deal_stage, deal_amount,
             started_at.isoformat(), completed_at.isoformat(),
             latency_ms, success, failure_reason)
        )

def record_agent_run(run_id, agent_num, agent_name, started_at, completed_at,
                     latency_ms, tokens, tokens_per_sec, success, output_length):
    with get_db() as con:
        con.execute(
            "INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, agent_num, agent_name,
             started_at.isoformat(), completed_at.isoformat(),
             latency_ms, tokens, tokens_per_sec, success, output_length)
        )

def record_crm_op(run_id, operation, deal_id, latency_ms, success,
                  fields_written=None, error=None):
    with get_db() as con:
        con.execute(
            "INSERT INTO crm_operations VALUES (?,?,?,?,?,?,?,?)",
            (run_id, operation, deal_id, latency_ms, success,
             fields_written, error, datetime.now().isoformat())
        )

def record_financial_run(run_id, deal_id, deal_name, win_rate, discount,
                         worst, avg, best, risk_level, written, refinements):
    with get_db() as con:
        con.execute(
            "INSERT INTO financial_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, deal_id, deal_name, win_rate, discount,
             worst, avg, best, risk_level, written, refinements,
             datetime.now().isoformat())
        )

# Initialise on import
init_db()

# ── IN-MEMORY APPROVAL STATE ──────────────────────────────────────────────
# Keyed by deal_name — populated after each pipeline run.
active_approvals: dict = {}










# ── GQL + LLM HELPERS ────────────────────────────────────────────────────

def gql(query: str) -> dict:
    resp = requests.post(TWENTY_URL, headers=TWENTY_HEADERS,
                         json={"query": query}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise ValueError(f"GraphQL error: {data['errors']}")
    return data["data"]


from together import Together as _TogetherClient
_together = _TogetherClient(api_key=TOGETHER_API_KEY)


@traceable(name="llm-stream", run_type="llm", metadata={"provider": "together", "model": TOGETHER_MODEL})
def _trace_llm_stream(_prompt: str, response: str) -> str:
    """LangSmith trace sink."""
    return response


def llm_stream(prompt: str, max_tokens: int = 1000):
    """Stream tokens from Together AI; logs to LangSmith after completion."""
    stream = create_completion(_together,
        model=TOGETHER_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        # Reasoning model: thinking tokens draw from the same budget, so small
        # caps produce empty output. Floor the budget to leave room for content.
        max_tokens=max(max_tokens, 4000),
        stream=True,
    )
    tokens = []
    for chunk in stream:
        if not getattr(chunk, "choices", None):
            continue  # keep-alive / usage chunks have no choices
        delta = chunk.choices[0].delta
        token = (getattr(delta, "content", None) if delta else None) or ""
        if token:
            tokens.append(token)
            yield token

    threading.Thread(
        target=_trace_llm_stream,
        args=(prompt, "".join(tokens)),
        daemon=True,
    ).start()


@traceable(name="llm-call", run_type="llm", metadata={"provider": "together", "model": TOGETHER_MODEL})
def llm(prompt: str, max_tokens: int = 1000) -> str:
    """Non-streaming Together AI call."""
    resp = create_completion(_together,
        model=TOGETHER_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=max(max_tokens, 4000),
        stream=False,
    )
    if not resp.choices:
        return ""
    return (resp.choices[0].message.content or "").strip()


def _section_items(text: str, heading: str, limit: int = 3) -> list[str]:
    match = re.search(
        rf"{re.escape(heading)}\s*:?\s*(.*?)(?:\n[A-Z][A-Z /'’&-]+:|\Z)",
        text or "",
        flags=re.S,
    )
    block = match.group(1) if match else ""
    items = []
    for line in block.splitlines():
        cleaned = re.sub(r"^[\s\-*•\d.)]+", "", line).strip()
        if cleaned and not cleaned.startswith("["):
            items.append(cleaned)
        if len(items) >= limit:
            break
    return items


def _line_after_label(text: str, label: str) -> str:
    match = re.search(rf"{re.escape(label)}\s*:?\s*(.+)", text or "", flags=re.I)
    return match.group(1).strip() if match else ""


def build_pipeline_enablement_packet(
    risk_text: str = "",
    opp_text: str = "",
    devils_text: str = "",
    arb_text: str = "",
    memo_text: str = "",
) -> dict:
    """Compact deal-committee handoff for the enablement engine."""
    top_risks = (
        _section_items(risk_text, "HIGH RISKS")
        or _section_items(risk_text, "MEDIUM RISKS")
        or _section_items(memo_text, "RISK ASSESSMENT")
    )
    next_actions = (
        _section_items(arb_text, "NEXT ACTIONS")
        or _section_items(memo_text, "NEXT ACTIONS")
    )
    stakeholder_gaps = (
        _section_items(risk_text, "FLAGS TO WATCH")
        + _section_items(devils_text, "THE REAL QUESTION", limit=1)
    )[:3]
    return {
        "risk_verdict": _line_after_label(risk_text, "RISK VERDICT"),
        "top_risks": top_risks[:3],
        "committee_decision": _line_after_label(arb_text, "COMMITTEE DECISION"),
        "next_actions": next_actions[:3],
        "stakeholder_gaps": stakeholder_gaps,
    }


CRM_SCHEMA = """
## Twenty CRM — Field Definitions

### Opportunities (Deals)
| Field        | Meaning |
|--------------|---------|
| name         | "ProductName — DealType" — the PRODUCT being sold plus deal motion (e.g. "WealthM — Strategic Partnership"). This is NOT the customer name. |
| companyName  | The CUSTOMER / ACCOUNT name (e.g. "<account name>"). Always use this to identify who the deal is with. |
| stage        | Deal stage: NEW → MEETING → PROPOSAL → CUSTOMER (won). CUSTOMER means closed-won. |
| amount       | Deal value in USD (stored as amountMicros ÷ 1,000,000). |
| arr          | Annual Recurring Revenue — the annualised subscription value. |
| tcv          | Total Contract Value — full lifetime value of the contract. |
| closeDate    | Expected or actual close date. |
| probability  | Win probability % (0–100). |
| assignedRep  | The AE / sales rep responsible for this deal. |
| companyName  | The customer company name. Use this — NOT the deal name prefix — to find all deals for a customer. |

### Key Rules for the LLM
1. "WealthM", "CloudCore", "RiskEngine" etc. in a deal name are PRODUCTS, not customers.
2. To find all deals for a customer → filter by `companyName`, not by `name`.
3. Two deals with the same `name` (e.g. two "WealthM — Annual Subscription") belong to DIFFERENT customers — check `companyName` to distinguish them.
4. CUSTOMER stage = won/closed. PROPOSAL = active negotiation. NEW/MEETING = early pipeline.
5. Never confuse the product name with the company/customer name.
"""


def extract_deal_name_simple(message: str) -> Optional[str]:
    """
    Pure string parsing — no LLM.
    Returns a deal name if one is found in quotes or after common keywords.
    """
    import re as _re
    # 1. Quoted text (straight or curly quotes)
    m = _re.search(r'["\u201c\u201d\u2018\u2019]([^"\']{3,})["\u201c\u201d\u2018\u2019]', message)
    if m:
        return m.group(1).strip()
    # 2. After action/question keywords — grab until punctuation or end
    patterns = [
        r'(?:opportunity|deal|about|for|on|analyze|open|navigate\s+to)\s+([A-Z][^\?\.,]{3,})(?:[?\.,]|$)',
        r'(?:amount|stage|close\s*date|value|status|probability)\s+(?:of|for)\s+([A-Z][^\?\.,]{3,})(?:[?\.,]|$)',
        r'what\s+is\s+(?:the\s+)?(?:amount|value|stage|status|probability)\s+(?:of|for)\s+([A-Z][^\?\.,]{3,})(?:[?\.,]|$)',
    ]
    for pat in patterns:
        m = _re.search(pat, message, _re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def fetch_deal_direct(deal_name: str, fields: list = None) -> Optional[dict]:
    """
    Query Twenty CRM with a name LIKE filter — no LLM, no full-table scan.
    Returns the best matching deal node with all key fields, or None.
    """
    if not fields:
        fields = ["id", "name", "stage", "closeDate", "probability", "amount { amountMicros currencyCode }"]
    fields_str = " ".join(fields)
    
    query = f"""
    query {{
      opportunities(
        filter: {{ name: {{ like: "%{deal_name}%" }} }}
        first: 1
      ) {{
        edges {{
          node {{
            {fields_str}
          }}
        }}
      }}
    }}
    """
    try:
        data = gql(query)
        edges = data["opportunities"]["edges"]
        if not edges:
            return None
        node = edges[0]["node"]
        node["amount"] = ((node.get("amount") or {}).get("amountMicros") or 0) / 1_000_000
        return node
    except Exception:
        return None


@traceable(name="route-query", run_type="chain", tags=["chat"])
def route_query(message: str) -> dict:
    """
    Pure Python routing — no LLM. Classifies the query and returns a structured
    intent dict compatible with fetch_crm_for_intent().
    """
    import re as _re
    m = message.lower()

    # ── Top-N deals ────────────────────────────────────────────────────────
    _top_num_match = _re.search(r'\btop\s*(\d+)\b', m)
    _top_no_num    = bool(_re.search(r'\btop\s+(deal|opportunit)', m))
    if _top_num_match or _top_no_num:
        top_n_val = int(_top_num_match.group(1)) if _top_num_match else 10
        if any(w in m for w in ("deal", "opportunit", "pipeline", "value", "amount", "top")):
            stage = None
            for s in ("proposal", "customer", "meeting", "new", "won", "lost", "screening"):
                if s in m:
                    stage = s.upper()
                    break
            return {
                "intent": "list_deals",
                "dealName": None,
                "fields": ["name", "stage", "amount { amountMicros }", "closeDate"],
                "stage": stage,
                "topN": top_n_val,
                "sortDesc": True,
            }

    # ── Pipeline stats ─────────────────────────────────────────────────────
    if any(kw in m for kw in ("total pipeline", "pipeline value", "pipeline total",
                               "how many deals", "total value", "total amount",
                               "deal stats", "deal summary", "pipeline stats",
                               "pipeline overview", "revenue summary", "forecast summary",
                               "pipeline breakdown", "pipeline report")):
        return {"intent": "stats", "dealName": None, "fields": [],
                "stage": None, "topN": None, "sortDesc": None}

    # ── Biggest/largest deals (no explicit N — default top 10) ────────────
    if any(kw in m for kw in ("biggest deal", "largest deal", "highest value deal",
                               "most valuable", "highest amount", "largest amount",
                               "biggest opportunit", "largest opportunit")):
        return {
            "intent": "list_deals",
            "dealName": None,
            "fields": ["name", "stage", "amount { amountMicros }", "closeDate"],
            "stage": None,
            "topN": 10,
            "sortDesc": True,
        }

    # ── List deals ─────────────────────────────────────────────────────────
    list_kws = ("all deals", "list deals", "show deals", "all opportunities",
                "list opportunities", "show opportunities", "deals in stage",
                "opportunities in", "give me deals", "give me opportunities",
                "my pipeline", "show pipeline", "view pipeline", "open pipeline")
    if any(kw in m for kw in list_kws):
        stage = None
        for s in ("proposal", "customer", "meeting", "new", "won", "lost", "screening"):
            if s in m:
                stage = s.upper()
                break
        return {
            "intent": "list_deals",
            "dealName": None,
            "fields": ["name", "stage", "amount { amountMicros }", "closeDate"],
            "stage": stage,
            "topN": None,
            "sortDesc": any(w in m for w in ("largest", "highest", "biggest", "sort")),
        }

    # ── Stage-scoped deal lists ("won deals", "proposal stage", etc.) ──────
    stage_map = {"won": "WON", "lost": "LOST", "new": "NEW",
                 "proposal": "PROPOSAL", "customer": "CUSTOMER",
                 "meeting": "MEETING", "screening": "SCREENING"}
    for kw, stage_val in stage_map.items():
        if (f"{kw} deal" in m or f"{kw} opportunit" in m
                or f"deals in {kw}" in m or f"stage {kw}" in m
                or m.strip().startswith(kw)):
            return {
                "intent": "list_deals",
                "dealName": None,
                "fields": ["name", "stage", "amount { amountMicros }", "closeDate"],
                "stage": stage_val,
                "topN": None,
                "sortDesc": any(w in m for w in ("largest", "highest", "sort")),
            }

    # ── Related deals for a company (MUST be before list_deals) ─────────────
    import re as _re2
    _no_anchor_triggers = ("what other", "what else", "other deal", "related deal",
                            "other opportunit", "related opportunit", "more deal")
    _anchor_triggers = ("deals for", "deals from", "deals with", "deals of",
                        "deals under", "deals by", "deals at",
                        "all deals for", "all deals of", "all deals from",
                        "all deals under", "all deals by")
    _has_company_anchor = any(kw in m for kw in (
        "customer", "company", "account", "client",
        "for ", "from ", "with ", "of ", "under ", "by "
    ))
    _is_related = (
        any(kw in m for kw in _no_anchor_triggers) or
        (any(kw in m for kw in _anchor_triggers) and _has_company_anchor)
    )
    if _is_related:
        # 1. Quoted name first
        cq = _re2.search(r'["""\']([ A-Za-z0-9&\-\.]{2,})["""\']', message)
        if cq:
            company_name = cq.group(1).strip()
        else:
            # 2. After anchor keyword: "under customer name X", "for X", "does X have"
            cq2 = _re2.search(
                r'(?:for|from|with|of|under|by|customer(?:\s+name)?|company|account|client|does)\s+([A-Z][A-Za-z0-9 &\-\.]{2,}?)(?:\s*[?.,]|\s+have|\s+has|\s+deal|$)',
                message, _re2.IGNORECASE
            )
            company_name = cq2.group(1).strip() if cq2 else None
        return {
            "intent": "related_deals",
            "companyName": company_name,
            "fields": ["name", "stage", "amount { amountMicros }", "closeDate", "companyName"],
            "stage": None, "topN": None, "sortDesc": None, "dealName": None,
        }

    # ── "who is the customer / company for deal X" → get_deal_info with companyName ──
    if any(kw in m for kw in ("who is the customer", "who is the company", "what company",
                               "which company", "what is the customer", "which customer",
                               "find the customer", "find customer")):
        name = extract_deal_name_simple(message)
        return {
            "intent": "get_deal_info",
            "dealName": name,
            "fields": ["name", "companyName", "stage", "amount { amountMicros }", "assignedRep"],
            "stage": None, "topN": None, "sortDesc": None,
        }

    # ── Contacts / people ──────────────────────────────────────────────────
    if any(kw in m for kw in ("contact", "people", "person", "who is",
                               "email of", "phone of", " poc ")):
        name = extract_deal_name_simple(message)
        if name:
            # Always include companyName so the LLM knows the customer
            return {
                "intent": "get_deal_info",
                "dealName": name,
                "fields": ["name", "companyName", "pocname { firstName lastName }",
                           "poctitle", "pocemail",
                           "pocphone { primaryPhoneNumber }"],
                "stage": None, "topN": None, "sortDesc": None,
            }
        return {"intent": "contacts", "dealName": None, "fields": [],
                "stage": None, "topN": None, "sortDesc": None}

    # ── Specific deal by name ──────────────────────────────────────────────
    name = extract_deal_name_simple(message)
    if name:
        fields = ["id", "name", "stage", "amount { amountMicros }", "closeDate", "probability", "companyName", "assignedRep"]
        if any(w in m for w in ("arr", "annual recurring", "recurring revenue")):
            fields.append("arr")
        if any(w in m for w in ("tcv", "total contract")):
            fields.append("tcv")
        if any(w in m for w in ("forecast", "best case", "worst case")):
            fields += ["forecastbestcase { amountMicros }", "forecastworstcase { amountMicros }"]
        if any(w in m for w in ("company", "account", "companyname", "customer", "client", "who is")):
            fields.append("companyName")
        if any(w in m for w in ("contact", "poc", "email", "phone")):
            fields += ["pocname { firstName lastName }", "poctitle", "pocemail",
                       "pocphone { primaryPhoneNumber }"]
        return {
            "intent": "get_deal_info",
            "dealName": name,
            "fields": fields,
            "stage": None, "topN": None, "sortDesc": None,
        }

    return {"intent": "general", "dealName": None, "fields": [],
            "stage": None, "topN": None, "sortDesc": None}


def find_deal_by_name(name: str, fields: list = None) -> Optional[dict]:
    """Fetch all deals and return the best name match (longest substring)."""
    if not fields:
        fields = ["id", "name", "stage", "amount { amountMicros }", "closeDate"]
    else:
        # Ensure name and id are always there for the matching logic
        if "id" not in fields: fields.append("id")
        if "name" not in fields: fields.append("name")
    fields_str = " ".join(fields)
    try:
        q = f"""query {{ opportunities(first: 200) {{ edges {{ node {{ {fields_str} }} }} }} }}"""
        data = gql(q)
        import re as _re
        # Strip punctuation/em-dashes so "SmartLedger Data Migration" matches "SmartLedger — Data Migration"
        def _words(s):
            return set(_re.sub(r'[^a-z0-9 ]', ' ', s.lower()).split())

        query_words = _words(name)
        # Reset best match variables
        best, best_score = None, 0
        for e in data["opportunities"]["edges"]:
            n = e["node"]
            node_name = n["name"]
            node_words = _words(node_name)
            overlap = len(query_words & node_words)
            # Favor more specific matches if scores tie
            score = overlap + (0.1 if len(node_words) == len(query_words) else 0)
            if overlap > 0 and overlap >= len(query_words) * 0.5 and score > best_score:
                best, best_score = n, score
        if best:
            # Recursively flatten if needed, but here we just handle the root amount for legacy compat
            amt_obj = best.get("amount") or {}
            best["amount"] = (amt_obj.get("amountMicros") or 0) / 1_000_000
        return best
    except Exception as e:
        with open("/tmp/error.log", "a") as f:
            f.write(f"GraphQL Error in find_deal_by_name: {e}\n")
        return None


def _flatten_node(node: dict) -> dict:
    """Flatten nested CRM fields (FullName, Currency, etc.) into plain strings."""
    flat = {}
    for k, v in node.items():
        if isinstance(v, dict):
            if "amountMicros" in v:
                flat[k] = (v.get("amountMicros") or 0) / 1_000_000
            elif "firstName" in v or "lastName" in v:
                parts = [v.get("firstName"), v.get("lastName")]
                flat[k] = " ".join([p for p in parts if p]).strip() or None
            elif "primaryPhoneNumber" in v:
                flat[k] = v.get("primaryPhoneNumber")
            else:
                # Fallback for any other dict: just join values
                parts = [str(sv) for sv in v.values() if sv]
                flat[k] = " ".join(parts).strip() or None
        else:
            flat[k] = v
    return flat


def fetch_crm_for_intent(intent: dict) -> tuple:
    """
    Step 2 — Flask queries Twenty CRM based on the structured intent.
    Returns (crm_context_str, extra_actions_dict).
    The LLM is never involved here — pure data retrieval.
    """
    intent_type = intent.get("intent", "general")
    deal_name   = (intent.get("dealName") or "").strip()
    stage       = intent.get("stage")
    top_n       = intent.get("topN")
    sort_desc   = intent.get("sortDesc")
    extra: dict = {}

    # ── Specific deal lookup ───────────────────────────────────────────
    if intent_type in ("get_deal_info", "navigate", "run_analysis") and deal_name:
        req_fields = intent.get("fields") or ["id", "name", "stage", "amount { amountMicros }", "closeDate"]
        deal = find_deal_by_name(deal_name, fields=req_fields)
        if not deal:
            return f"No deal found in Twenty CRM matching '{deal_name}'.", extra
        deal = _flatten_node(deal)
        extra["navigate_deal"] = deal
        if intent_type == "run_analysis":
            extra["run_agent"] = "full"
        
        ctx_lines = []
        for k, v in deal.items():
            if k == "amount_val": continue
            val_str = v if v is not None else "Unknown"
            if k == "amount" and isinstance(v, (int, float)):
                ctx_lines.append(f"Amount: ${v:,.0f}")
            else:
                ctx_lines.append(f"{k}: {val_str}")
        ctx = "DEAL DETAILS:\n" + "\n".join(ctx_lines)
        return ctx, extra

    # ── Deal list ─────────────────────────────────────────────────────
    if intent_type == "list_deals":
        try:
            req_fields = intent.get("fields") or ["id", "name", "stage", "amount { amountMicros }", "closeDate"]
            if "name" not in req_fields: req_fields.append("name")
            fields_str = " ".join(req_fields)
            
            fetch_limit = 200 if top_n else 100
            if stage:
                q = f"""query {{
                  opportunities(first: {fetch_limit}, filter: {{ stage: {{ eq: {stage} }} }}) {{
                    edges {{ node {{ {fields_str} }} }}
                  }}
                }}"""
            else:
                q = f"""query {{
                  opportunities(first: {fetch_limit}) {{
                    edges {{ node {{ {fields_str} }} }}
                  }}
                }}"""
            d = gql(q)
            rows_raw = []
            for e in d["opportunities"]["edges"]:
                n = e["node"]
                amt = ((n.get("amount") or {}).get("amountMicros") or 0) / 1_000_000
                n["amount_val"] = amt
                rows_raw.append(n)

            if sort_desc is True:
                rows_raw.sort(key=lambda x: x.get("amount_val", 0), reverse=True)
            elif sort_desc is False:
                rows_raw.sort(key=lambda x: x.get("amount_val", 0))

            limit = int(top_n) if top_n else 30
            rows = []
            for i, n in enumerate(rows_raw[:limit], 1):
                amt = n.get("amount_val", 0)
                amt_str = f"${amt:,.0f}" if amt else "not set"
                parts = [f"#{i}"]
                for k, v in n.items():
                    if k in ("amount_val", "amount", "id"): continue
                    if v is not None and v != "":
                        parts.append(f"{k}: {v}")
                parts.append(f"amount: {amt_str}")
                rows.append(" | ".join(parts))
            label = f"LIVE CRM — TOP {limit} {stage or 'ALL'} DEALS (sorted by amount, {len(rows_raw)} total fetched):"
            return f"{label}\n" + "\n".join(rows), extra
        except Exception as ex:
            return f"CRM fetch error: {ex}", extra

    # ── Contacts ──────────────────────────────────────────────────────
    if intent_type == "contacts":
        try:
            q = """query {
              people(first: 50) {
                edges {
                  node {
                    name { firstName lastName }
                    jobTitle
                    emails { primaryEmail }
                    phones { primaryPhoneNumber }
                    city
                    company { name }
                  }
                }
              }
            }"""
            d = gql(q)
            rows = []
            for e in d["people"]["edges"]:
                n = e["node"]
                full  = f"{n['name']['firstName']} {n['name']['lastName']}".strip()
                co    = (n.get("company") or {}).get("name", "—")
                email = (n.get("emails") or {}).get("primaryEmail", "—")
                rows.append(f"- {full} | {n.get('jobTitle') or '?'} | {co} | {email}")
            return f"LIVE CRM — CONTACTS ({len(rows)} total):\n" + "\n".join(rows[:20]), extra
        except Exception as ex:
            return f"CRM fetch error: {ex}", extra

    # ── Related deals by companyName (fuzzy word match) ───────────────
    if intent_type == "related_deals":
        company_name = (intent.get("companyName") or "").strip()
        if not company_name:
            return "Please specify a company name to find related deals.", extra
        try:
            import re as _re3
            # Fetch all deals and do fuzzy word-level matching in Python
            # Match account names using shared words from the operator-supplied CRM records.
            q = """query {
              opportunities(first: 500) {
                edges { node { name stage amount { amountMicros } closeDate companyName } }
              }
            }"""
            d = gql(q)

            def _words(s):
                return set(_re3.sub(r'[^a-z0-9]', ' ', (s or "").lower()).split())

            query_words = _words(company_name)
            matched = []
            matched_company = None
            for e in d["opportunities"]["edges"]:
                n = e["node"]
                cn = n.get("companyName") or ""
                if not cn:
                    continue
                overlap = len(query_words & _words(cn))
                if overlap >= max(1, len(query_words) * 0.5):
                    matched.append(n)
                    if matched_company is None:
                        matched_company = cn

            if not matched:
                return f"No deals found for company matching '{company_name}'.", extra

            rows = []
            for i, n in enumerate(matched, 1):
                amt = ((n.get("amount") or {}).get("amountMicros") or 0) / 1_000_000
                amt_str = f"${amt:,.0f}" if amt else "not set"
                rows.append(
                    f"#{i} {n['name']} | Customer: {n.get('companyName','?')} | "
                    f"Stage: {n.get('stage','?')} | Amount: {amt_str} | Close: {n.get('closeDate','?')}"
                )
            label = f"LIVE CRM — ALL DEALS FOR '{matched_company}' ({len(rows)} found):"
            return f"{label}\n" + "\n".join(rows), extra
        except Exception as ex:
            return f"CRM fetch error: {ex}", extra

    # ── Pipeline stats ─────────────────────────────────────────────────
    if intent_type == "stats":
        try:
            q = """query {
              opportunities(first: 500) {
                edges { node { stage amount { amountMicros } } }
              }
            }"""
            d = gql(q)
            by_stage: dict = {}
            for e in d["opportunities"]["edges"]:
                n = e["node"]
                st = n.get("stage", "UNKNOWN")
                amt = ((n.get("amount") or {}).get("amountMicros") or 0) / 1_000_000
                if st not in by_stage:
                    by_stage[st] = {"count": 0, "value": 0.0}
                by_stage[st]["count"] += 1
                by_stage[st]["value"] += amt
            total = sum(v["value"] for v in by_stage.values())
            active = sum(v["value"] for s, v in by_stage.items() if s != "LOST")
            lines = [f"PIPELINE STATS ({sum(v['count'] for v in by_stage.values())} total deals):"]
            for st, sv in sorted(by_stage.items()):
                lines.append(f"  {st}: {sv['count']} deals | ${sv['value']:,.0f}")
            lines.append(f"Total (all stages): ${total:,.0f}")
            lines.append(f"Active pipeline (excl. LOST): ${active:,.0f}")
            return "\n".join(lines), extra
        except Exception as ex:
            return f"CRM fetch error: {ex}", extra

    # ── General / no specific fetch needed ────────────────────────────
    return "", extra


# ── LLM-powered data analyst ──────────────────────────────────────────────

_ANALYST_SCHEMA = """
ENTITY: opportunities (deals)
Fields you may request:
  name            — "ProductName — DealType" (product sold, NOT the customer)
  companyName     — Customer / account name
  stage           — Enum: NEW | MEETING | PROPOSAL | CUSTOMER (won/closed)
  amount { amountMicros }  — Deal value (micros ÷ 1e6 = USD)
  arr             — Annual Recurring Revenue (USD number)
  tcv             — Total Contract Value (USD number)
  closeDate       — Expected close date (ISO string)
  probability     — Win probability 0-100
  assignedRep     — Sales rep name
  forecastbestcase { amountMicros }
  forecastworstcase { amountMicros }

Filterable fields and operators (GQL where clause):
  companyName     — like "%keyword%"  |  eq "Exact Name"
  name            — like "%keyword%"
  stage           — eq NEW | eq MEETING | eq PROPOSAL | eq CUSTOMER
  closeDate       — gte "2025-01-01"  |  lte "2026-12-31"
  probability     — gte 50  |  lte 80
  arr             — gte 100000  |  lte 500000
  tcv             — gte 100000

ENTITY: people (contacts)
Fields: name { firstName lastName } | jobTitle | emails { primaryEmail } | phones { primaryPhoneNumber } | city | company { name }

FIELD SYNONYM MAP (map user's words → actual field names):
  revenue / value / deal size / amount → amount { amountMicros }
  annual / arr / recurring → arr
  total contract / lifetime / tcv → tcv
  customer / account / client / company → companyName
  won / closed / existing customer → stage eq CUSTOMER
  open / active / in progress / pipeline → stage not CUSTOMER
  lost → stage eq LOST
  rep / ae / owner / assigned / salesperson → assignedRep
  close date / due date / expected → closeDate
  probability / likelihood / win rate / odds → probability
  contact / person / poc → people entity
  product / solution → first part of name before " — "
  deal type / motion / engagement → second part of name after " — "
"""


def llm_generate_query_intent(message: str) -> dict:
    """
    Use the LLM to parse a natural language CRM question into a structured query intent.
    Returns a dict with keys: is_crm_query, entity, fields, filters, sort_by, sort_desc, limit.
    """
    prompt = f"""You are a CRM query parser. Convert the user's question into a structured query.

CRM SCHEMA:
{_ANALYST_SCHEMA}

User question: "{message}"

Respond ONLY with valid JSON — no markdown fences, no explanation:
{{
  "is_crm_query": true or false,
  "entity": "opportunities" or "people",
  "fields": ["list of GQL field names to return"],
  "filters": {{"fieldName": {{"operator": "value"}}}},
  "sort_by": "field_name or null",
  "sort_desc": true or false,
  "limit": integer (default 30, max 100)
}}

Rules:
- For stage filters use enum values without quotes: {{ "eq": "PROPOSAL" }}
- For string fields use quotes: {{ "like": "%keyword%" }}
- For numeric fields use numbers: {{ "gte": 100000 }}
- Always include name, companyName, stage in fields for opportunities
- If not a CRM data question, set is_crm_query false and all other fields null"""

    try:
        resp = llm(prompt, max_tokens=350)
        import re as _re2
        resp = _re2.sub(r'```(?:json)?\s*|\s*```', '', resp).strip()
        return json.loads(resp)
    except Exception as e:
        return {"is_crm_query": False, "parse_error": str(e)}


def execute_analyst_query(intent: dict) -> str:
    """
    Build and execute a GQL query from a structured analyst intent.
    Returns a formatted string ready to inject into the LLM system prompt.
    """
    entity   = intent.get("entity", "opportunities")
    fields   = intent.get("fields") or ["name", "companyName", "stage", "amount { amountMicros }", "closeDate"]
    filters  = intent.get("filters") or {}
    sort_by  = intent.get("sort_by")
    sort_desc = intent.get("sort_desc", True)
    limit    = min(int(intent.get("limit") or 30), 100)

    # Ensure essential fields are always present
    if entity == "opportunities":
        for f in ["name", "companyName", "stage"]:
            if f not in fields:
                fields.append(f)

    # Build GQL filter clause
    filter_parts = []
    for field, condition in filters.items():
        if not isinstance(condition, dict):
            continue
        for op, val in condition.items():
            if field == "stage":
                filter_parts.append(f'{field}: {{ {op}: {val} }}')
            elif isinstance(val, str):
                filter_parts.append(f'{field}: {{ {op}: "{val}" }}')
            elif isinstance(val, list):
                vals = ", ".join([f'"{v}"' if isinstance(v, str) else str(v) for v in val])
                filter_parts.append(f'{field}: {{ {op}: [{vals}] }}')
            else:
                filter_parts.append(f'{field}: {{ {op}: {val} }}')

    filter_clause = f", filter: {{ {', '.join(filter_parts)} }}" if filter_parts else ""
    fields_str = " ".join(fields)

    q = f"query {{ {entity}(first: {limit}{filter_clause}) {{ edges {{ node {{ {fields_str} }} }} }} }}"

    try:
        d = gql(q)
        rows_raw = d[entity]["edges"]
    except Exception as e:
        # Retry without filters if GQL fails (LLM may have used wrong syntax)
        try:
            q_nf = f"query {{ {entity}(first: {limit}) {{ edges {{ node {{ {fields_str} }} }} }} }}"
            d = gql(q_nf)
            rows_raw = d[entity]["edges"]
        except Exception:
            return f"CRM query failed: {e}"

    # Flatten and optionally sort in Python
    def _get_sort_val(node):
        v = node.get(sort_by, 0) if sort_by else 0
        if isinstance(v, dict) and "amountMicros" in v:
            return (v.get("amountMicros") or 0) / 1_000_000
        return v or 0

    nodes = [e["node"] for e in rows_raw]
    if sort_by:
        nodes.sort(key=_get_sort_val, reverse=bool(sort_desc))

    # Format rows
    rows = []
    for i, n in enumerate(nodes, 1):
        parts = [f"#{i}"]
        for k, v in n.items():
            if isinstance(v, dict):
                if "amountMicros" in v:
                    usd = (v.get("amountMicros") or 0) / 1_000_000
                    parts.append(f"{k}: ${usd:,.0f}")
                elif "firstName" in v or "lastName" in v:
                    parts.append(f"{k}: {v.get('firstName','')} {v.get('lastName','')}".strip())
                elif "primaryEmail" in v:
                    parts.append(f"email: {v.get('primaryEmail','')}")
                elif "primaryPhoneNumber" in v:
                    parts.append(f"phone: {v.get('primaryPhoneNumber','')}")
                elif "name" in v:
                    parts.append(f"company: {v.get('name','')}")
            elif v is not None and v != "":
                parts.append(f"{k}: {v}")
        rows.append(" | ".join(parts))

    label = f"LIVE CRM — {entity.upper()} ({len(rows)} results):"
    return f"{label}\n" + "\n".join(rows)


# ── DATA ANALYST AGENT ───────────────────────────────────────────────────

_ALL_DEAL_FIELDS = [
    "name", "companyName", "stage",
    "amount { amountMicros }", "arr", "tcv",
    "closeDate", "probability", "assignedRep",
    "pocemail", "poctitle",
]

_DATA_QUERY_KEYWORDS = (
    "deal", "opportunit", "customer", "company", "account", "client",
    "revenue", "amount", "arr", "tcv", "pipeline", "stage", "close",
    "how many", "total", "average", "sum", "count",
    "which", "show me", "list", "find", "get me", "give me", "fetch",
    "look up", "search", "who is", "what is", "tell me about",
    "top ", "biggest", "largest", "smallest", "highest", "lowest",
    "won", "lost", "proposal", "meeting", "renewal", "upsell",
    "rep", "assigned", "probability", "forecast",
)


def is_data_query(message: str) -> bool:
    m = message.lower()
    # Slash commands and navigation are NOT data queries
    if m.startswith("/"):
        return False
    return any(kw in m for kw in _DATA_QUERY_KEYWORDS)


def fetch_crm_json(filters: dict = None, limit: int = 100) -> list:
    """
    Fetch opportunities from CRM as a clean flat JSON-serializable list.
    Flattens nested fields (amount, pocname, etc.) into plain values.
    """
    fields_str = " ".join(_ALL_DEAL_FIELDS)

    # Build GQL filter clause
    filter_parts = []
    for field, condition in (filters or {}).items():
        if not isinstance(condition, dict):
            continue
        for op, val in condition.items():
            if field == "stage":
                filter_parts.append(f"{field}: {{ {op}: {val} }}")
            elif isinstance(val, str):
                filter_parts.append(f'{field}: {{ {op}: "{val}" }}')
            else:
                filter_parts.append(f"{field}: {{ {op}: {val} }}")
    filter_clause = f", filter: {{ {', '.join(filter_parts)} }}" if filter_parts else ""

    q = f"query {{ opportunities(first: {limit}{filter_clause}) {{ edges {{ node {{ {fields_str} }} }} }} }}"
    try:
        d = gql(q)
        edges = d["opportunities"]["edges"]
    except Exception:
        # Retry without filters
        q_nf = f"query {{ opportunities(first: {limit}) {{ edges {{ node {{ {fields_str} }} }} }} }}"
        d = gql(q_nf)
        edges = d["opportunities"]["edges"]

    records = []
    for e in edges:
        n = e["node"]
        flat = {}
        for k, v in n.items():
            if isinstance(v, dict) and "amountMicros" in v:
                flat[k] = round((v.get("amountMicros") or 0) / 1_000_000, 2)
            elif v is not None:
                flat[k] = v
        records.append(flat)
    return records


def data_analyst_agent(message: str, history: list, deal_ctx: dict = None):
    """
    Data Analyst Agent — streams an LLM response grounded in live CRM JSON.

    Step 1: Parse user intent → determine filters + limit
    Step 2: Fetch targeted CRM data as flat JSON
    Step 3: LLM reasons directly over the JSON and streams its answer
    """
    import re as _re_da

    # ── Step 1: Determine what to fetch ──────────────────────────────────
    intent = llm_generate_query_intent(message)
    filters = intent.get("filters") or {}
    # Cap limit: targeted queries can go higher, general queries stay smaller
    has_filters = bool(filters)
    limit = min(int(intent.get("limit") or (200 if has_filters else 80)), 200)

    # ── Step 2: Fetch CRM JSON ────────────────────────────────────────────
    try:
        records = fetch_crm_json(filters, limit)
    except Exception as e:
        yield f"data: {json.dumps({'text': f'CRM fetch error: {e}'})}\n\n"
        return

    if not records:
        yield f"data: {json.dumps({'text': 'No records found in CRM matching your query.'})}\n\n"
        return

    # Emit a metadata event so the UI knows the data analyst ran
    yield f"data: {json.dumps({'analyst': True, 'records': len(records), 'filtered': has_filters})}\n\n"

    # ── Step 3: Build analyst prompt ──────────────────────────────────────
    dataset_str = json.dumps(records, indent=2)

    # Trim if too large (keep under ~6000 chars of JSON)
    if len(dataset_str) > 6000:
        records_trimmed = records[:40]
        dataset_str = json.dumps(records_trimmed, indent=2)
        dataset_str += f"\n... ({len(records) - 40} more records not shown — summarise from visible data)"

    selected_ctx = ""
    if deal_ctx and deal_ctx.get("name"):
        selected_ctx = (
            f"\nCURRENTLY SELECTED DEAL: {deal_ctx.get('name')} | "
            f"Customer: {deal_ctx.get('companyName','?')} | "
            f"Stage: {deal_ctx.get('stage','?')} | "
            f"Amount: ${deal_ctx.get('amount',0):,.0f}\n"
        )

    system = f"""You are a senior CRM Data Analyst at Nexus Solutions.

{CRM_SCHEMA}
{selected_ctx}
You have been given live CRM data as a JSON array. Each object is one deal/opportunity.
Use ONLY this data to answer the user's question.

Key rules:
- "name" field = Product + DealType (e.g. "WealthM — Strategic Partnership"). NOT the customer.
- "companyName" = the actual customer. Use this to identify and group deals by customer.
- To find all deals for a customer: match on companyName, not on the name prefix.
- For counts/totals/averages: compute from the data. Show your working.
- If companyName is null for a deal, say so — do not infer from email domains.
- Be precise. Reference actual values from the JSON.

CRM DATASET ({len(records)} deals):
{dataset_str}"""

    prompt = system + "\n\n"
    for h in (history or [])[-4:]:
        role = "User" if h["role"] == "user" else "Assistant"
        prompt += f"{role}: {h['content']}\n"
    prompt += f"User: {message}\nAssistant:"

    for token in llm_stream(prompt, max_tokens=800):
        yield f"data: {json.dumps({'text': token})}\n\n"


# ── AGENTIC CHAT — Tool-calling loop ─────────────────────────────────────────

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_crm",
            "description": (
                "Query the Twenty CRM for deals (opportunities). Use to find, filter, sort, or count deals. "
                "Key fields: name (Product+DealType, NOT customer), companyName (customer), "
                "stage (NEW/MEETING/PROPOSAL/CUSTOMER=won), amount, arr, tcv, closeDate, probability, assignedRep. "
                "Pass empty filters {} to get all deals."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "object",
                        "description": (
                            "Field filters as {field: {op: value}}. "
                            "Ops: eq, like, gt, lt, gte, lte. "
                            "Stage values: NEW, MEETING, PROPOSAL, CUSTOMER. "
                            'Example: {"stage": {"eq": "CUSTOMER"}, "companyName": {"like": "Apex"}}'
                        ),
                    },
                    "sort_by": {"type": "string", "description": "Field to sort by, e.g. amount, closeDate, probability"},
                    "sort_desc": {"type": "boolean", "description": "Sort descending if true"},
                    "limit": {"type": "integer", "description": "Max records (default 50, max 200)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_lookup",
            "description": (
                "Semantic search over the knowledge base. Covers: "
                "deal_notes (AE call notes, discovery, stakeholder meetings, win/loss reasons), "
                "kb_documents (product battlecards, capability references, ICP profiles, cost sheets), "
                "company_profiles (customer company background), "
                "meddpicc_scores (deal qualification scores). "
                "Use for: product capabilities, tech requirements, deal history, competitive intel, "
                "loss reasons, company background, MEDDPICC data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language search query"},
                    "collections": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["deal_notes", "kb_documents", "company_profiles", "meddpicc_scores"],
                        },
                        "description": "Collections to search. Omit to search all 4.",
                    },
                    "n_results": {"type": "integer", "description": "Results per collection (default 3, max 8)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_deal",
            "description": (
                "Fetch full CRM details for a specific deal AND retrieve all related knowledge "
                "(deal notes, company profile, MEDDPICC scores, product KB docs). "
                "Use when asked about a specific deal in depth."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "deal_name": {"type": "string", "description": "Deal name supplied by the configured CRM"},
                    "company_name": {"type": "string", "description": "Customer/company name if known"},
                },
                "required": ["deal_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_pipeline",
            "description": (
                "Analyzes the full pipeline using MEDDPICC scorecard data across all deals. "
                "Computes exact statistical aggregations — not semantic search chunks. "
                "Use this when the user asks about: pipeline health, how many deals have a gap or flag, "
                "rep performance comparisons, product win/loss rates, patterns that correlate with winning "
                "or losing, pipeline forecast, weighted value, MBR data, or any question using words like: "
                "across all deals, pipeline, how many deals, which rep, what percentage, overall, patterns, "
                "correlate, MBR, business review, coaching. "
                "Do NOT use for single-deal questions — use analyze_deal or knowledge_lookup for those."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter_by": {
                        "type": "object",
                        "description": (
                            "Optional filters to slice the pipeline before aggregating. "
                            'Examples: {"stage": "PROPOSAL"}, {"assigned_rep": "<representative identifier>"}, '
                            '{"overall_score": "AT RISK"}, {"product": "FraudGuard"}'
                        ),
                    },
                    "focus": {
                        "type": "string",
                        "description": (
                            "What to compute: full (default), health, flags, reps, "
                            "products, win_analysis, forecast"
                        ),
                    },
                },
            },
        },
    },
]

_TOOL_LABELS = {
    "fetch_crm": "Fetching CRM data",
    "knowledge_lookup": "Searching knowledge base",
    "analyze_deal": "Analyzing deal",
    "analyze_pipeline": "Analyzing full pipeline",
}


def _normalize_filters(raw: dict) -> dict:
    out = {}
    for field, condition in (raw or {}).items():
        if isinstance(condition, dict):
            out[field] = condition
        elif isinstance(condition, str):
            out[field] = {"eq": condition} if field == "stage" else {"like": condition}
        elif isinstance(condition, (int, float)):
            out[field] = {"gte": condition}
    return out


def execute_tool(name: str, args: dict) -> str:
    if name == "fetch_crm":
        filters = _normalize_filters(args.get("filters") or {})
        limit = min(int(args.get("limit") or 50), 200)
        sort_by = args.get("sort_by")
        sort_desc = bool(args.get("sort_desc", True))
        try:
            records = fetch_crm_json(filters, limit)
        except Exception as e:
            return json.dumps({"error": str(e), "records": []})
        if sort_by and records:
            records.sort(key=lambda r: r.get(sort_by) or 0, reverse=sort_desc)
        return json.dumps({"count": len(records), "records": records})

    if name == "knowledge_lookup":
        query = args.get("query", "")
        collections = args.get("collections") or None
        n_results = min(int(args.get("n_results") or 3), 8)
        try:
            return _kb_lookup(query, collections=collections, n_results=n_results)
        except Exception as e:
            return f"[Knowledge lookup error: {e}]"

    if name == "analyze_deal":
        deal_name = args.get("deal_name", "")
        company_name = args.get("company_name", "")
        try:
            records = fetch_crm_json({"name": {"like": deal_name}}, limit=10)
        except Exception:
            records = []
        kb_query = f"{deal_name} {company_name}".strip()
        try:
            kb = _kb_lookup(kb_query, n_results=4)
        except Exception as e:
            kb = f"[KB unavailable: {e}]"
        return json.dumps({"crm_records": records, "knowledge": kb})

    if name == "analyze_pipeline":
        filter_by = args.get("filter_by") or None
        focus = args.get("focus") or "full"
        try:
            result = _analyze_pipeline(filter_by=filter_by, focus=focus)
        except Exception as e:
            return json.dumps({"error": str(e), "summary": str(e)})
        # Return the pre-formatted summary + compact stats for the LLM
        return json.dumps({
            "summary": result.get("summary", ""),
            "records_analysed": result.get("data", {}).get("records_analysed", 0),
            "overview": result.get("data", {}).get("overview", {}),
            "score_distribution": result.get("data", {}).get("score_distribution", {}),
            "flag_frequency": result.get("data", {}).get("flag_frequency", {}),
            "forecast": result.get("data", {}).get("forecast", {}),
            "rep_performance": result.get("data", {}).get("rep_performance", [])[:10],
            "product_performance": result.get("data", {}).get("product_performance", [])[:10],
        })

    return json.dumps({"error": f"Unknown tool: {name}"})


_MBR_TRIGGERS = (
    "generate mbr", "create mbr", "write mbr", "produce mbr",
    "monthly business review", "mbr for this month", "mbr report",
    "pipeline report", "generate the mbr", "run mbr",
)

_DECK_TRIGGERS = (
    "generate deck", "create deck", "make deck", "build deck",
    "create presentation", "make presentation", "generate presentation",
    "create ppt", "make ppt", "generate ppt", "build ppt",
    "create slides", "make slides", "generate slides",
)


def _is_mbr_intent(message: str) -> bool:
    m = message.lower().strip()
    return any(t in m for t in _MBR_TRIGGERS)


def _is_deck_intent(message: str) -> bool:
    m = message.lower().strip()
    return any(t in m for t in _DECK_TRIGGERS)


def _generate_deck_stream():
    period = datetime.now().strftime("%B %Y")
    yield f"data: {json.dumps({'tool_call': 'analyze_pipeline', 'label': 'Building pipeline deck'})}\n\n"
    try:
        path = _generate_deck(period=period)
        yield f"data: {json.dumps({'tool_result': 'analyze_pipeline', 'summary': '3 slides built'})}\n\n"
        yield f"data: {json.dumps({'text': f'Pipeline deck ready for {period}.'})}\n\n"
        yield f"data: {json.dumps({'deck_ready': True, 'period': period})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'text': f'Deck generation failed: {e}'})}\n\n"


def _save_mbr(mbr_content: str, pipeline_data: dict, period: str) -> dict:
    import csv
    mbr_dir = os.path.join(BASE_DIR, "knowledge_base", "mbr")
    os.makedirs(mbr_dir, exist_ok=True)
    slug = period.lower().replace(" ", "_")
    md_path  = os.path.join(mbr_dir, f"mbr_{slug}.md")
    json_path = os.path.join(mbr_dir, f"mbr_{slug}_data.json")
    csv_path  = os.path.join(mbr_dir, f"mbr_{slug}_summary.csv")

    with open(md_path, "w") as f:
        f.write(mbr_content)

    with open(json_path, "w") as f:
        json.dump(pipeline_data, f, indent=2, default=str)

    ov = pipeline_data.get("overview", {})
    sd = pipeline_data.get("score_distribution", {})
    ff = pipeline_data.get("flag_frequency", {})
    fc = pipeline_data.get("forecast", {})
    pp = pipeline_data.get("product_performance", [])
    rp = pipeline_data.get("rep_performance", [])
    top_product = pp[0]["product"] if pp else ""
    top_rep = rp[0]["rep_name"] if rp else ""

    row = {
        "period": period,
        "total_deals": ov.get("total_deals", 0),
        "total_value": ov.get("total_pipeline_value", 0),
        "strong_count": sd.get("strong", {}).get("count", 0),
        "strong_value": sd.get("strong", {}).get("total_value", 0),
        "needs_attention_count": sd.get("needs_attention", {}).get("count", 0),
        "needs_attention_value": sd.get("needs_attention", {}).get("total_value", 0),
        "at_risk_count": sd.get("at_risk", {}).get("count", 0),
        "at_risk_value": sd.get("at_risk", {}).get("total_value", 0),
        "weighted_forecast": fc.get("weighted_pipeline", 0),
        "missing_eb_count": ff.get("missing_economic_buyer", {}).get("count", 0),
        "missing_champion_count": ff.get("missing_champion", {}).get("count", 0),
        "stalled_count": ff.get("stalled", {}).get("count", 0),
        "overdue_count": ff.get("deal_overdue", {}).get("count", 0),
        "top_product_at_risk": top_product,
        "top_rep_coaching_needed": top_rep,
    }
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        w.writeheader()
        w.writerow(row)

    return {"md": md_path, "json": json_path, "csv": csv_path}


def _build_mbr_prompt(pipeline_summary: str, win_themes: str, loss_themes: str,
                       coaching_signals: str, crm_json: str, period: str) -> str:
    today = datetime.now().strftime("%B %d, %Y")
    return f"""You are a senior GTM analyst preparing a Monthly Business Review for sales leadership.
Write a complete, boardroom-ready MBR document using only the data provided below.

PIPELINE DATA:
{pipeline_summary}

WIN THEMES FROM DEAL NOTES:
{win_themes}

LOSS THEMES FROM DEAL NOTES:
{loss_themes}

COACHING SIGNALS:
{coaching_signals}

CRM SNAPSHOT (active deals):
{crm_json[:3000]}

Write the MBR in this exact structure:

# GTM Acceleration — Monthly Business Review
**Period:** {period}
**Generated:** {today}
**Prepared by:** Deal Desk Intelligence System

---

## Executive Summary
Three sentences maximum. State of the pipeline, single biggest risk, what needs leadership attention this week.

---

## Pipeline Health Overview

| Metric | Value |
|--------|-------|
| Total Deals | |
| Total Pipeline Value | |
| Weighted Forecast | |
| STRONG | |
| NEEDS ATTENTION | |
| AT RISK | |

Brief narrative on what the health distribution tells us.

---

## Top Pipeline Risks

### Risk 1 — [Name it]
- Deals affected, pipeline value exposed, which products/reps, recommended action this week.

### Risk 2 — [Name it]
[same structure]

### Risk 3 — [Name it]
[same structure]

---

## Win / Loss Analysis

### What Is Actually Winning Deals
Three specific MEDDPICC factors with percentages from won deals.

### What Is Actually Losing Deals
Three specific factors from lost/at-risk deals.

### Single Biggest Coaching Opportunity
One paragraph. The pattern that, if fixed, would have the highest win-rate impact.

---

## Rep Performance Scorecard

| Rep | Deals | Pipeline Value | MEDDPICC Completion | Top Gap | Coaching Priority |
|-----|-------|---------------|---------------------|---------|------------------|
[one row per rep]

---

## Product Performance

| Product | Deals | Pipeline Value | AT RISK Rate | Win Rate | Top Gap |
|---------|-------|---------------|-------------|---------|---------|
[one row per product — top 10 by pipeline value]

---

## Forecast

| Scenario | Value |
|----------|-------|
| Weighted pipeline | |
| AT RISK value | |
| Forecast confidence | |

---

## Recommended Leadership Actions

Three specific actions for this week. Each: what to do, who owns it, deadline, expected outcome.

---

RULES: Every number must come from the pipeline data. No invented figures. Be direct — cut filler.
"""


def generate_mbr(period: str = None):
    """
    Orchestrates MBR generation: pipeline stats + KB themes + CRM snapshot → LLM → save.
    Yields SSE events for the chat stream.
    """
    if not period:
        period = datetime.now().strftime("%B %Y")

    # ── Step 1: Full pipeline analysis ───────────────────────────────────────
    yield f"data: {json.dumps({'tool_call': 'analyze_pipeline', 'label': 'Running full pipeline analysis'})}\n\n"
    try:
        pipeline_result = _analyze_pipeline(focus="full")
        pipeline_summary = pipeline_result.get("summary", "")
        pipeline_data = pipeline_result.get("data", {})
    except Exception as e:
        pipeline_summary = f"Pipeline analysis failed: {e}"
        pipeline_data = {}
    _pa_count = pipeline_data.get("overview", {}).get("total_deals", 0)
    yield f"data: {json.dumps({'tool_result': 'analyze_pipeline', 'summary': str(_pa_count) + ' deals analysed'})}\n\n"

    # ── Step 2: Qualitative KB context ───────────────────────────────────────
    yield f"data: {json.dumps({'tool_call': 'knowledge_lookup', 'label': 'Fetching win/loss themes'})}\n\n"
    try:
        win_themes = _kb_lookup(
            "deals won successfully champion confirmed economic buyer engaged pain quantified",
            collections=["deal_notes", "meddpicc_scores"], n_results=5,
        )
        loss_themes = _kb_lookup(
            "deals lost at risk stalled no economic buyer competition unknown",
            collections=["deal_notes", "meddpicc_scores"], n_results=5,
        )
        coaching_signals = _kb_lookup(
            "rep coaching gaps missing economic buyer unquantified pain no champion",
            collections=["meddpicc_scores"], n_results=5,
        )
    except Exception as e:
        win_themes = loss_themes = coaching_signals = f"[KB unavailable: {e}]"
    yield f"data: {json.dumps({'tool_result': 'knowledge_lookup', 'summary': 'win/loss themes fetched'})}\n\n"

    # ── Step 3: CRM snapshot ─────────────────────────────────────────────────
    yield f"data: {json.dumps({'tool_call': 'fetch_crm', 'label': 'Fetching CRM snapshot'})}\n\n"
    try:
        crm_records = fetch_crm_json(filters={}, limit=100)
        crm_json_str = json.dumps(crm_records, indent=2)
    except Exception as e:
        crm_json_str = f"[CRM unavailable: {e}]"
    yield f"data: {json.dumps({'tool_result': 'fetch_crm', 'summary': f'{len(crm_records) if isinstance(crm_records, list) else 0} deals'})}\n\n"

    # ── Step 4: Build prompt and generate MBR ────────────────────────────────
    _mbr_msg = f"Generating MBR for {period}...\n\n"
    yield f"data: {json.dumps({'text': _mbr_msg})}\n\n"
    mbr_prompt = _build_mbr_prompt(pipeline_summary, win_themes, loss_themes,
                                    coaching_signals, crm_json_str, period)
    mbr_content = ""
    try:
        for token in llm_stream(mbr_prompt, max_tokens=2500):
            mbr_content += token
            yield f"data: {json.dumps({'text': token})}\n\n"
    except Exception as e:
        err = f"\n\n[MBR generation error: {e}]"
        mbr_content += err
        yield f"data: {json.dumps({'text': err})}\n\n"

    # ── Step 5: Save outputs ─────────────────────────────────────────────────
    try:
        saved = _save_mbr(mbr_content, pipeline_data, period)
        yield f"data: {json.dumps({'mbr_saved': True, 'files': saved, 'period': period})}\n\n"
    except Exception as e:
        _save_err = f"\n\n[Save failed: {e}]"
        yield f"data: {json.dumps({'text': _save_err})}\n\n"


def agentic_chat(message: str, history: list, deal_ctx: dict = None):
    """
    Agentic loop: up to 2 rounds of tool-calling, then a forced text answer.
    Yields SSE events: {tool_call, label}, {tool_result, summary}, {text}.
    MBR intent bypasses the loop and calls generate_mbr() directly.
    """
    if _is_mbr_intent(message):
        yield from generate_mbr()
        return
    if _is_deck_intent(message):
        yield from _generate_deck_stream()
        return
    deal_note = ""
    if deal_ctx and deal_ctx.get("name"):
        deal_note = (
            f"\nCURRENTLY SELECTED DEAL: {deal_ctx['name']} | "
            f"Customer: {deal_ctx.get('companyName', '?')} | "
            f"Stage: {deal_ctx.get('stage', '?')} | "
            f"Amount: ${deal_ctx.get('amount', 0):,.0f}\n"
        )

    system_prompt = f"""You are a senior Deal Desk AI at Nexus Solutions with access to live CRM data and a knowledge base.
{CRM_SCHEMA}{deal_note}
Use tools to answer questions accurately. Rules:
- Call fetch_crm for any question about deals, pipeline, customers, stages, amounts, reps, or counts.
- Call knowledge_lookup for product capabilities, tech requirements, deal notes, loss reasons, competitive intel, company background, or MEDDPICC scores.
- For questions needing both (e.g. "why did top lost deals fail"): call BOTH tools.
- Never invent data. If tools return nothing relevant, say so clearly.
- After tools return results, give a precise, structured answer grounded in that data."""

    messages = [{"role": "system", "content": system_prompt}]
    for h in (history or [])[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    for round_num in range(3):
        offer_tools = round_num < 2  # rounds 0+1 can call tools; round 2 forces text
        kwargs = dict(
            model=TOGETHER_MODEL,
            messages=messages,
            temperature=0.3,
            max_tokens=1500,
            stream=False,
        )
        if offer_tools:
            kwargs["tools"] = AGENT_TOOLS
            kwargs["tool_choice"] = "auto"

        try:
            resp = create_completion(_together,**kwargs)
        except Exception as e:
            yield f"data: {json.dumps({'text': f'LLM error: {e}'})}\n\n"
            return

        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                tool_name = tc.function.name
                label = _TOOL_LABELS.get(tool_name, tool_name)
                yield f"data: {json.dumps({'tool_call': tool_name, 'label': label})}\n\n"
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = {}
                result = execute_tool(tool_name, args)
                if tool_name == "fetch_crm":
                    try:
                        summary = f"{json.loads(result).get('count', 0)} deals"
                    except Exception:
                        summary = "done"
                elif tool_name == "knowledge_lookup":
                    summary = f"{result.count('[SOURCE:')} sources"
                else:
                    summary = "done"
                yield f"data: {json.dumps({'tool_result': tool_name, 'summary': summary})}\n\n"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            answer = (msg.content or "").strip()
            if not answer:
                answer = "I couldn't find relevant information for that query. Please try rephrasing."
            for word in answer.split(" "):
                yield f"data: {json.dumps({'text': word + ' '})}\n\n"
            return

    yield f"data: {json.dumps({'text': 'Maximum reasoning rounds reached. Please try a more specific question.'})}\n\n"


# ── API ROUTES ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


ALL_STAGES   = {"NEW", "MEETING", "PROPOSAL", "CUSTOMER"}
ALL_OUTCOMES = {"WON", "LOST"}

_DEAL_FIELDS = "id name stage dealOutcome amount { amountMicros currencyCode } closeDate createdAt"


def _gql_deals(filter_clause: str) -> list:
    if filter_clause:
        q = f"""
        query {{
          opportunities(first: 500, filter: {{ {filter_clause} }}) {{
            edges {{ node {{ {_DEAL_FIELDS} }} }}
          }}
        }}
        """
    else:
        q = f"""
        query {{
          opportunities(first: 500) {{
            edges {{ node {{ {_DEAL_FIELDS} }} }}
          }}
        }}
        """
    data = gql(q)
    return data["opportunities"]["edges"]


@app.route("/api/deals")
def get_deals():
    """Fetch deals from Twenty CRM.

    ?stage=NEW,MEETING,PROPOSAL,CUSTOMER  — filter by stage (pipeline position)
    ?outcome=WON,LOST                     — filter by dealOutcome
    Both params can be combined; results are unioned.
    """
    stage_param   = request.args.get("stage", "")
    outcome_param = request.args.get("outcome", "")

    stages   = [s.strip().upper() for s in stage_param.split(",")   if s.strip()]
    outcomes = [o.strip().upper() for o in outcome_param.split(",") if o.strip()]

    # Validate — ignore unknown values
    stages   = [s for s in stages   if s in ALL_STAGES]
    outcomes = [o for o in outcomes if o in ALL_OUTCOMES]

    fetch_all_stages   = ALL_STAGES.issubset(set(stages))
    fetch_all_outcomes = ALL_OUTCOMES.issubset(set(outcomes))
    fetch_everything   = fetch_all_stages and fetch_all_outcomes and not (stages or outcomes)

    try:
        edges = []

        # No filters at all — return everything
        if not stages and not outcomes:
            edges = _gql_deals("")

        # Only outcome filter (WON / LOST)
        elif not stages and outcomes:
            outcome_gql = ", ".join(outcomes)
            edges = _gql_deals(f"dealOutcome: {{ in: [{outcome_gql}] }}")

        # Only stage filter
        elif stages and not outcomes:
            if fetch_all_stages:
                edges = _gql_deals("")
            else:
                stage_gql = ", ".join(stages)
                edges = _gql_deals(f"stage: {{ in: [{stage_gql}] }}")

        # Both — fetch stage deals + outcome deals, union by id
        else:
            seen = set()
            if fetch_all_stages:
                edges = _gql_deals("")
                seen = {e["node"]["id"] for e in edges}
            else:
                stage_gql = ", ".join(stages)
                stage_edges = _gql_deals(f"stage: {{ in: [{stage_gql}] }}")
                seen = {e["node"]["id"] for e in stage_edges}
                edges = stage_edges

            outcome_gql = ", ".join(outcomes)
            for e in _gql_deals(f"dealOutcome: {{ in: [{outcome_gql}] }}"):
                if e["node"]["id"] not in seen:
                    edges.append(e)
                    seen.add(e["node"]["id"])

        deals = []
        for e in edges:
            node = e["node"]
            node["amount"] = (node.get("amount") or {}).get("amountMicros", 0) / 1_000_000
            deals.append(node)

        return jsonify({"deals": deals, "count": len(deals)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/deal/<deal_id>")
def get_deal(deal_id):
    """Fetch single deal details."""
    query = f"""
    query {{
      opportunity(filter: {{ id: {{ eq: "{deal_id}" }} }}) {{
        id name stage amount {{ amountMicros currencyCode }} closeDate createdAt
      }}
    }}
    """
    try:
        data = gql(query)
        deal = data.get("opportunity", {})
        deal["amount"] = (deal.get("amount") or {}).get("amountMicros", 0) / 1_000_000
        return jsonify(deal)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/people")
def get_people():
    """Fetch people from Twenty CRM."""
    query = """
    query {
      people(first: 30) {
        edges {
          node {
            id
            name { firstName lastName }
            emails { primaryEmail }
            phones { primaryPhoneNumber }
            jobTitle
            city
            company { name }
          }
        }
      }
    }
    """
    try:
        data = gql(query)
        people = [e["node"] for e in data["people"]["edges"]]
        return jsonify({"people": people})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pipeline/stats")
def pipeline_stats():
    """Get pipeline summary stats."""
    query = """
    query {
      opportunities(first: 500) {
        edges {
          node { stage amount { amountMicros } }
        }
      }
    }
    """
    try:
        data = gql(query)
        deals = [e["node"] for e in data["opportunities"]["edges"]]

        stats = {}
        for d in deals:
            stage = d["stage"]
            amt = ((d.get("amount") or {}).get("amountMicros") or 0) / 1_000_000
            if stage not in stats:
                stats[stage] = {"count": 0, "value": 0}
            stats[stage]["count"] += 1
            stats[stage]["value"] += amt

        total_active = sum(
            v["value"] for s, v in stats.items() if s not in ("LOST",)
        )
        return jsonify({"by_stage": stats, "total_active": total_active, "total_deals": len(deals)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analyze/<deal_id>", methods=["GET", "POST"])
def analyze_deal(deal_id):
    """
    Run the full 6-agent pipeline on a deal.
    Streams server-sent events back to the UI.
    POST body: { "use_knowledge_base": true|false }  (default true)
    """
    body = request.get_json(silent=True) or {}
    use_knowledge_base = body.get("use_knowledge_base", True)

    def generate():
        def send(event: str, data: dict):
            yield f"event: {event}\ndata: {json.dumps(data)}\n\n"

        run_id = str(uuid.uuid4())
        pipeline_start = datetime.now()
        deal = {}
        knowledge_context = ""

        try:
            # ── Fetch deal context ────────────────────────────────────────
            yield from send("agent_start", {"agent": 1, "name": "Data Fetcher", "status": "running"})
            crm_t0 = datetime.now()

            query = f"""
            query {{
              opportunity(filter: {{ id: {{ eq: "{deal_id}" }} }}) {{
                id name stage amount {{ amountMicros currencyCode }} closeDate createdAt companyName
              }}
            }}
            """
            data = gql(query)
            deal = data.get("opportunity", {})
            deal["amount"] = ((deal.get("amount") or {}).get("amountMicros") or 0) / 1_000_000
            company_name = deal.get("companyName") or ""
            crm_fetch_ms = int((datetime.now() - crm_t0).total_seconds() * 1000)
            record_crm_op(run_id, "fetch_deal", deal_id, crm_fetch_ms, True)

            pdata = gql("""query { people(first: 50) { edges { node {
                id name { firstName lastName } emails { primaryEmail }
                phones { primaryPhoneNumber } jobTitle
                company { name } } } } }""")
            all_people = [e["node"] for e in pdata["people"]["edges"]]
            if company_name:
                contacts = [p for p in all_people if
                            isinstance(p.get("company"), dict) and
                            p["company"].get("name") == company_name]
                if not contacts:
                    contacts = [p for p in all_people if
                                company_name.lower() in
                                (p.get("company") or {}).get("name", "").lower()]
            else:
                contacts = all_people[:10]

            hdata = gql("""query { opportunities(first: 5,
                filter: { stage: { in: [CUSTOMER] } }) {
                edges { node { id name stage amount { amountMicros } } } } }""")
            history = []
            for e in hdata["opportunities"]["edges"]:
                node = e["node"]
                node["amount"] = ((node.get("amount") or {}).get("amountMicros") or 0) / 1_000_000
                history.append(node)

            yield from send("agent_done", {
                "agent": 1, "name": "Data Fetcher",
                "result": f"Customer: {company_name or '(unknown)'} — {len(contacts)} contacts, {len(history)} historical deals",
                "company_name": company_name,
                "status": "done"
            })

            # ── Build KB context once for all agents ──────────────────────
            if use_knowledge_base and _KB_AVAILABLE:
                try:
                    knowledge_context = build_knowledge_context(deal)
                    yield from send("kb_context", {
                        "size": len(knowledge_context),
                        "enabled": True,
                    })
                except Exception as _kb_err:
                    knowledge_context = ""
                    yield from send("kb_context", {"size": 0, "enabled": False, "error": str(_kb_err)})
            else:
                yield from send("kb_context", {"size": 0, "enabled": False})

            health_context = _customer_health_summary(deal.get("companyName") or deal.get("company") or "")
            kb_prefix = f"\n{CITATION_INSTRUCTION}\n{knowledge_context}\n{health_context}\n" if (knowledge_context or health_context) else ""

            # ── Risk Analyst ──────────────────────────────────────────────
            yield from send("agent_start", {"agent": 2, "name": "Risk Analyst", "status": "running"})
            contact_list = "\n".join([
                f"- {c['name']['firstName']} {c['name']['lastName']} | {c.get('jobTitle','N/A')}"
                for c in contacts[:5]
            ])
            risk_prompt = f"""{kb_prefix}You are a senior deal risk analyst at Nexus Solutions, a B2B fintech company.

DEAL: {deal.get('name')}
Stage: {deal.get('stage')} | Amount: ${deal.get('amount', 0):,.0f} | Close: {deal.get('closeDate','Unknown')}
Contacts on file: {len(contacts)}
Historical WON/LOST deals: {len(history)}

KEY CONTACTS:
{contact_list}

Identify all deal risks. Every risk must reference a specific fact from the institutional memory above.

🔴 HIGH RISKS:
[2-3 major risks — cite memory sources]

🟡 MEDIUM RISKS:
[2-3 moderate risks]

⚠️ FLAGS TO WATCH:
[2-3 early warning signals]

RISK VERDICT: [LOW / MEDIUM / HIGH / CRITICAL — one sentence]"""

            risk_text = ""
            _a2_t0 = datetime.now()
            _a2_tokens = 0
            yield from send("agent_token_start", {"agent": 2})
            for token in llm_stream(risk_prompt, 600):
                risk_text += token
                _a2_tokens += 1
                yield from send("token", {"agent": 2, "text": token})
            _a2_t1 = datetime.now(); _a2_ms = int((_a2_t1-_a2_t0).total_seconds()*1000)
            record_agent_run(run_id, 2, "Risk Analyst", _a2_t0, _a2_t1, _a2_ms, _a2_tokens,
                             _a2_tokens/max((_a2_t1-_a2_t0).total_seconds(),0.001), True, len(risk_text))
            yield from send("agent_done", {"agent": 2, "name": "Risk Analyst",
                                           "result": risk_text, "status": "done"})

            # ── Opportunity Analyst ───────────────────────────────────────
            yield from send("agent_start", {"agent": 3, "name": "Opportunity Analyst", "status": "running"})
            won = [d for d in history if d.get("stage") == "WON"]
            opp_prompt = f"""{kb_prefix}You are a senior opportunity analyst at Nexus Solutions.

DEAL: {deal.get('name')}
Stage: {deal.get('stage')} | Amount: ${deal.get('amount', 0):,.0f}
Contacts engaged: {len(contacts)}
Previous WON deals: {len(won)} (value: ${sum(d.get('amount',0) for d in won):,.0f})

Identify strengths and opportunities grounded in the institutional memory above.

✅ KEY STRENGTHS:
[2-3 deal strengths — cite win themes from memory]

🚀 UPSELL / EXPANSION:
[2-3 realistic expansion angles — cite ICP signals from memory]

🤝 RELATIONSHIP SIGNALS:
[assess stakeholder engagement using notes from memory]

💡 WIN STRATEGY:
[one paragraph — clearest path to close, citing specific people and facts]

OPPORTUNITY VERDICT: [LOW / MEDIUM / HIGH confidence — one sentence]"""

            opp_text = ""
            _a3_t0 = datetime.now()
            _a3_tokens = 0
            yield from send("agent_token_start", {"agent": 3})
            for token in llm_stream(opp_prompt, 600):
                opp_text += token
                _a3_tokens += 1
                yield from send("token", {"agent": 3, "text": token})
            _a3_t1 = datetime.now(); _a3_ms = int((_a3_t1-_a3_t0).total_seconds()*1000)
            record_agent_run(run_id, 3, "Opportunity Analyst", _a3_t0, _a3_t1, _a3_ms, _a3_tokens,
                             _a3_tokens/max((_a3_t1-_a3_t0).total_seconds(),0.001), True, len(opp_text))
            yield from send("agent_done", {"agent": 3, "name": "Opportunity Analyst",
                                           "result": opp_text, "status": "done"})

            # ── Devil's Advocate ──────────────────────────────────────────
            yield from send("agent_start", {"agent": 4, "name": "Devil's Advocate", "status": "running"})
            devils_prompt = f"""{kb_prefix}You are a skeptical deal committee member at Nexus Solutions.

THE DEAL: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')}

RISK ANALYST:
{risk_text}

OPPORTUNITY ANALYST:
{opp_text}

Challenge both analyses using specific contradictions from the institutional memory above:

❓ CHALLENGING THE RISK ANALYST:
[2 points where risks may be overstated — cite specific memory facts]

❓ CHALLENGING THE OPPORTUNITY ANALYST:
[2 points where optimism is unwarranted — cite lost deal patterns from memory]

🎯 THE REAL QUESTION:
[single most important unresolved question, derived from memory]

DEVIL'S VERDICT: [YES / CONDITIONAL / NO — one sentence, citing a specific memory fact]"""

            devils_text = ""
            _a4_t0 = datetime.now()
            _a4_tokens = 0
            yield from send("agent_token_start", {"agent": 4})
            for token in llm_stream(devils_prompt, 500):
                devils_text += token
                _a4_tokens += 1
                yield from send("token", {"agent": 4, "text": token})
            _a4_t1 = datetime.now(); _a4_ms = int((_a4_t1-_a4_t0).total_seconds()*1000)
            record_agent_run(run_id, 4, "Devil's Advocate", _a4_t0, _a4_t1, _a4_ms, _a4_tokens,
                             _a4_tokens/max((_a4_t1-_a4_t0).total_seconds(),0.001), True, len(devils_text))
            yield from send("agent_done", {"agent": 4, "name": "Devil's Advocate",
                                           "result": devils_text, "status": "done"})

            # ── Arbitrator ────────────────────────────────────────────────
            yield from send("agent_start", {"agent": 5, "name": "Deal Arbitrator", "status": "running"})
            arb_prompt = f"""{kb_prefix}You are VP of Sales at Nexus Solutions chairing the deal committee.

Deal: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')} | Close: {deal.get('closeDate','Unknown')}

RISK ANALYST:
{risk_text}

OPPORTUNITY ANALYST:
{opp_text}

DEVIL'S ADVOCATE:
{devils_text}

CONFLICT RESOLUTION:
Risk Analyst verdict: [quote their RISK VERDICT line]
Devil's Advocate verdict: [quote their DEVIL'S VERDICT line]
Where they agree: [1 sentence]
Where they conflict: [1 sentence — the core tension]
How I resolve it: [1 sentence — which view you weight more and why, citing institutional memory]

COMMITTEE DECISION: [PURSUE AGGRESSIVELY / PURSUE WITH CONDITIONS / HOLD / WALK AWAY]

REASONING:
[2-3 sentences referencing the conflict resolution above and MEDDPICC score]

CONDITIONS (if applicable):
[list conditions or "None"]

NEXT ACTIONS:
1. [This week — name a specific person]
2. [Next 30 days]
3. [Escalation trigger]

DEAL SCORE: [X/10] — [one line rationale]"""

            arb_text = ""
            _a5_t0 = datetime.now()
            _a5_tokens = 0
            yield from send("agent_token_start", {"agent": 5})
            for token in llm_stream(arb_prompt, 500):
                arb_text += token
                _a5_tokens += 1
                yield from send("token", {"agent": 5, "text": token})
            _a5_t1 = datetime.now(); _a5_ms = int((_a5_t1-_a5_t0).total_seconds()*1000)
            record_agent_run(run_id, 5, "Deal Arbitrator", _a5_t0, _a5_t1, _a5_ms, _a5_tokens,
                             _a5_tokens/max((_a5_t1-_a5_t0).total_seconds(),0.001), True, len(arb_text))
            yield from send("agent_done", {"agent": 5, "name": "Deal Arbitrator",
                                           "result": arb_text, "status": "done"})

            # ── Memo Writer ───────────────────────────────────────────────
            yield from send("agent_start", {"agent": 6, "name": "Memo Writer", "status": "running"})
            now = datetime.now().strftime("%B %d, %Y")
            commercial_economics_table = build_commercial_economics_table(deal)
            memo_prompt = f"""{kb_prefix}You are a senior VP writing an executive deal memo. Every section must reference specific facts from the institutional memory above. Name real people. Cite real numbers.
The COMMERCIAL ECONOMICS section below is generated by code from the financial model. Keep that table exactly as provided: do not change metric names, values, formatting, or notes. Reference those values in narrative only when relevant.

DEAL: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')} | Close: {deal.get('closeDate','Unknown')}
Date: {now}

RISK SUMMARY:
{risk_text}

OPPORTUNITY SUMMARY:
{opp_text}

DEVIL'S ADVOCATE:
{devils_text}

COMMITTEE DECISION:
{arb_text}

Write the memo now. Do not paste the prior agents verbatim; synthesize them into a crisp executive artifact.

DEAL DESK MEMO - NEXUS SOLUTIONS
Date: {now}
Deal: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')} | Close: {deal.get('closeDate','Unknown')}

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

            memo_text = ""
            _a6_t0 = datetime.now()
            _a6_tokens = 0
            yield from send("agent_token_start", {"agent": 6})
            for token in llm_stream(memo_prompt, 2200):
                memo_text += token
                _a6_tokens += 1
                yield from send("token", {"agent": 6, "text": token})
            _a6_t1 = datetime.now(); _a6_ms = int((_a6_t1-_a6_t0).total_seconds()*1000)
            record_agent_run(run_id, 6, "Memo Writer", _a6_t0, _a6_t1, _a6_ms, _a6_tokens,
                             _a6_tokens/max((_a6_t1-_a6_t0).total_seconds(),0.001), True, len(memo_text))
            yield from send("agent_done", {"agent": 6, "name": "Memo Writer",
                                           "result": memo_text, "status": "done"})

            # ── Final summary + record pipeline ──────────────────────────
            pipeline_end = datetime.now()
            total_ms = int((pipeline_end - pipeline_start).total_seconds() * 1000)
            record_pipeline_run(run_id, deal_id, deal.get("name",""), deal.get("stage",""),
                                deal.get("amount", 0), pipeline_start, pipeline_end,
                                total_ms, True)

            enablement_packet = build_pipeline_enablement_packet(
                risk_text=risk_text,
                opp_text=opp_text,
                devils_text=devils_text,
                arb_text=arb_text,
                memo_text=memo_text,
            )

            yield from send("complete", {
                "deal": deal,
                "memo": memo_text,
                "risk": risk_text,
                "opportunity": opp_text,
                "devils": devils_text,
                "arbitration": arb_text,
                "enablement_packet": enablement_packet,
                "kb_enabled": use_knowledge_base and _KB_AVAILABLE,
                "kb_size": len(knowledge_context),
            })

            # ── Step 9: Save comparison output ───────────────────────────
            try:
                deal_name_raw  = deal.get("name", deal_id)
                company_slug   = _slugify(deal_name_raw.split("—")[0].strip() if "—" in deal_name_raw else deal_name_raw)
                deal_slug      = _slugify(deal_name_raw)
                out_dir        = os.path.join(BASE_DIR, "analysis_outputs", company_slug, deal_slug)
                os.makedirs(out_dir, exist_ok=True)
                mode_label     = "WITH" if (use_knowledge_base and _KB_AVAILABLE) else "WITHOUT"
                fname          = "_analysis_with_kb.md" if (use_knowledge_base and _KB_AVAILABLE) else "_analysis_without_kb.md"
                out_path       = os.path.join(out_dir, fname)
                header = (
                    f"# Deal Analysis\n"
                    f"Mode: {mode_label} institutional memory\n"
                    f"Company: {deal_name_raw.split('—')[0].strip() if '—' in deal_name_raw else deal_name_raw}\n"
                    f"Deal: {deal_name_raw}\n"
                    f"Generated: {datetime.now().isoformat()}\n"
                    f"Knowledge context size: {len(knowledge_context):,} characters\n"
                    f"Collections queried: deal_notes, company_profiles, kb_documents, meddpicc_scores\n\n"
                    f"---\n\n"
                )
                body_md = (
                    f"## Risk Analysis\n\n{risk_text}\n\n"
                    f"## Opportunity Analysis\n\n{opp_text}\n\n"
                    f"## Devil's Advocate\n\n{devils_text}\n\n"
                    f"## Committee Decision\n\n{arb_text}\n\n"
                    f"## Final Memo\n\n{memo_text}\n"
                )
                with open(out_path, "w", encoding="utf-8") as _f:
                    _f.write(header + body_md)
                yield from send("analysis_saved", {"path": out_path, "mode": mode_label})
            except Exception as _save_err:
                yield from send("analysis_saved", {"error": str(_save_err)})

            # ── POST-PIPELINE: APPROVAL INITIALIZATION ───────────────────
            try:
                from deal_desk import check_policy_rules

                deal_value   = deal.get("amount", 0) or 0
                deal_name    = deal.get("name", deal_id)

                # 1. Evaluate policy rules against this deal + contacts
                triggered_rules = [
                    r for r in check_policy_rules(
                        {"amount": deal_value},
                        contacts if "contacts" in dir() else []
                    )
                    if r["triggered"]
                ]

                # 2. Build approval chain based on deal value
                base_chain = [
                    {"level": "L1",  "role": "Pursuit Lead"},
                    {"level": "L2",  "role": "Account Executive"},
                    {"level": "L3",  "role": "Delivery Manager"},
                    {"level": "L4",  "role": "Area Leader"},
                ]
                if deal_value > 300_000:
                    base_chain.append({"level": "CVP", "role": "CVP"})

                # 3. Extract AI decision from arbitrator output
                ai_decision = ""
                if "COMMITTEE DECISION:" in arb_text:
                    ai_decision = arb_text.split("COMMITTEE DECISION:")[-1].split("\n")[0].strip()

                # 4. Build and store approval state
                approval_state = {
                    "deal_name":      deal_name,
                    "deal_value":     deal_value,
                    "ai_decision":    ai_decision,
                    "current_level":  "L1",
                    "current_role":   "Pursuit Lead",
                    "policy_rules":   triggered_rules,
                    "approval_chain": base_chain,
                    "status":         "pending_approval",
                }
                active_approvals[deal_name] = approval_state

                # 5. Log initial state to approval_audit table
                policy_rule_str = ", ".join(r["rule_name"] for r in triggered_rules)
                with get_approval_db() as con:
                    con.execute(
                        """INSERT INTO approval_audit
                           (timestamp, deal_name, deal_value, current_level, role_name,
                            ai_decision, policy_rule_triggered, action, actioned_by, notes, memo_file)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            datetime.now().isoformat(),
                            deal_name,
                            deal_value,
                            "L1",
                            "Pursuit Lead",
                            ai_decision,
                            policy_rule_str,
                            "initialized",
                            "system",
                            f"Auto-initialized after pipeline run {run_id}",
                            "",
                        )
                    )

                # 6. Stream approval state to UI
                yield from send("approval_initialized", approval_state)

            except Exception as appr_err:
                # Approval init failure must never break the pipeline response
                yield from send("approval_initialized", {
                    "error": str(appr_err),
                    "status": "approval_init_failed",
                })

        except Exception as e:
            pipeline_end = datetime.now()
            total_ms = int((pipeline_end - pipeline_start).total_seconds() * 1000)
            record_pipeline_run(run_id, deal_id, deal.get("name",""), deal.get("stage",""),
                                deal.get("amount", 0), pipeline_start, pipeline_end,
                                total_ms, False, str(e))
            yield from send("error", {"message": str(e)})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@app.route("/api/chat", methods=["POST"])
def chat():
    """
    4-step pipeline:
      1. LLM extracts structured intent (no data retrieval).
      2. Flask fetches exact CRM data based on intent.
      3. Build grounded prompt: pass real CRM data to LLM with strict instructions.
      4. LLM formats the final response only — never answers data questions from memory.
    Slash commands (/risk, /opportunity, etc.) bypass the pipeline and run analysis agents.
    """
    body     = request.json
    message  = body.get("message", "").strip()
    deal     = body.get("deal", {})
    history  = body.get("history", [])
    analysis = body.get("analysis", {})

    cmd = message.lower().split()[0] if message else ""

    # ── Slash commands: analysis agents, no CRM data needed ──────────
    agent_prompts = {
        "/risk": f"""You are a senior deal risk analyst. Analyze this deal for risks.
DEAL: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')}
List HIGH RISKS, MEDIUM RISKS, and FLAGS TO WATCH. End with a RISK VERDICT.""",
        "/opportunity": f"""You are a senior opportunity analyst. Analyze this deal for strengths and opportunities.
DEAL: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')}
List KEY STRENGTHS, UPSELL/EXPANSION angles, and WIN STRATEGY. End with OPPORTUNITY VERDICT.""",
        "/devil": f"""You are a skeptical deal committee member challenging assumptions.
DEAL: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')}
Prior analysis: {analysis.get('risk','')[:200]} | {analysis.get('opportunity','')[:200]}
Challenge the optimistic view. List concerns and end with DEVIL'S VERDICT: YES/CONDITIONAL/NO.""",
        "/arb": f"""You are VP of Sales chairing the deal committee.
DEAL: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')}
Risk: {analysis.get('risk','')[:200]} | Opportunity: {analysis.get('opportunity','')[:200]}
Give COMMITTEE DECISION, REASONING, CONDITIONS, NEXT ACTIONS, and DEAL SCORE X/10.""",
        "/memo": f"""You are a senior VP writing an executive deal memo.
DEAL: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')} | Close: {deal.get('closeDate','Unknown')}
Risk summary: {analysis.get('risk','')[:200]}
Opportunity summary: {analysis.get('opportunity','')[:200]}
Decision: {analysis.get('arbitration','')[:200]}
Write a complete DEAL DESK MEMO with EXECUTIVE SUMMARY, RISK ASSESSMENT, OPPORTUNITY ASSESSMENT, COMMITTEE RECOMMENDATION, NEXT ACTIONS, DEAL SCORE.""",
    }

    if cmd in agent_prompts:
        prompt = agent_prompts[cmd]
        def stream_slash():
            for token in llm_stream(prompt, 600):
                yield f"data: {json.dumps({'text': token})}\n\n"
        return Response(stream_with_context(stream_slash()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ── Agentic Chat — tool-calling loop handles all non-slash queries ──
    return Response(
        stream_with_context(agentic_chat(message, history, deal)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )



@app.route("/api/financial-sim", methods=["POST"])
def financial_sim():
    """Agent 7 — Financial Simulation: multi-turn conversational scenario modelling."""
    import re
    body = request.json
    message = (body.get("message") or "").strip().lower()
    deal = body.get("deal", {})
    analysis = body.get("analysis", {})
    sim_state = body.get("sim_state", {})

    def get_risk_level():
        txt = (analysis.get("risk", "") + " " + analysis.get("arbitration", "")).upper()
        for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            if level in txt:
                return level
        return "MEDIUM"

    def has_upsell():
        return any(w in analysis.get("opportunity", "").lower()
                   for w in ["upsell", "expansion", "expand", "additional", "cross-sell"])

    def get_deal_score():
        m = re.search(r'(\d+)\s*/\s*10', analysis.get("arbitration", ""))
        return int(m.group(1)) if m else 5

    def get_days_in_pipeline():
        try:
            from datetime import timezone
            created = datetime.fromisoformat(deal["createdAt"].replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - created).days
        except Exception:
            return 30

    def parse_pct(text):
        m = re.search(r'(\d+(?:\.\d+)?)\s*%?', text)
        if m:
            v = float(m.group(1))
            return v / 100 if v > 1 else v
        return None

    def calculate(win_rate, discount, risk_level, upsell, deal_score, days):
        amt = deal.get("amount", 0) or 0
        worst = amt * max(win_rate - 0.15, 0.05) * 0.85 * 0.75
        avg   = amt * win_rate * (1 - discount)
        best  = amt * (win_rate + 0.15) * 1.20 * (1 - discount * 0.5)
        adj = []
        if risk_level == "CRITICAL":
            worst *= 0.70; avg *= 0.70; best *= 0.70
            adj.append({"label": "CRITICAL risk penalty", "value": "-30% all scenarios"})
        elif risk_level == "HIGH":
            worst *= 0.85; avg *= 0.85; best *= 0.85
            adj.append({"label": "HIGH risk penalty", "value": "-15% all scenarios"})
        elif risk_level == "LOW":
            best *= 1.15
            adj.append({"label": "LOW risk bonus", "value": "best case +15%"})
        if upsell:
            best *= 1.25
            adj.append({"label": "Upsell signal detected", "value": "best case +25%"})
        if days > 90:
            worst *= 0.60
            adj.append({"label": f"Pipeline stale ({days} days)", "value": "worst case -40%"})
        if deal_score >= 8:
            avg *= 1.10
            adj.append({"label": f"Deal score {deal_score}/10", "value": "avg case +10%"})
        wp = int(win_rate * 100)
        dp = int(discount * 100)
        return {
            "worst": round(worst), "avg": round(avg), "best": round(best),
            "deal_name": deal.get("name", ""),
            "adjustments": adj,
            "assumptions": {
                "worst": f"Win {max(wp-15,5)}%, -{dp+15}% adj.",
                "avg":   f"Win {wp}%, {dp}% disc.",
                "best":  f"Win {min(wp+15,95)}%, +20% upside",
            }
        }

    _fin_run_id = str(uuid.uuid4())

    def generate():
        def se(event, data):
            yield f"event: {event}\ndata: {json.dumps(data)}\n\n"

        phase = sim_state.get("phase", "init")

        # ── INIT ──────────────────────────────────────────────────────────
        if phase == "init":
            risk_level = get_risk_level()
            days = get_days_in_pipeline()
            deal_score = get_deal_score()
            upsell = has_upsell()
            inferred = []
            if risk_level in ("HIGH", "CRITICAL"):
                inferred.append(f"{risk_level} risk → conservative penalties will apply")
            if days > 90:
                inferred.append(f"{days} days in pipeline → worst case stale penalty")
            if upsell:
                inferred.append("Upsell signals found → best case bonus will apply")
            if deal_score >= 8:
                inferred.append(f"Deal score {deal_score}/10 → avg case bonus will apply")
            if not inferred:
                inferred.append("No strong signals — standard assumptions will be used")
            bullet = "\n".join(f"• {i}" for i in inferred)
            msg = (f"AGENT 7 — FINANCIAL SIMULATION\n\n"
                   f"Deal: {deal.get('name')} | ${deal.get('amount',0):,.0f}\n\n"
                   f"Inferred from analysis:\n{bullet}\n\n"
                   f"What win rate are you targeting? (press Enter for 30% default)")
            yield from se("text", {"text": msg})
            yield from se("state", {"phase": "ask_winrate", "risk_level": risk_level,
                                     "days": days, "deal_score": deal_score, "upsell": upsell})

        # ── ASK WIN RATE ──────────────────────────────────────────────────
        elif phase == "ask_winrate":
            win_rate = parse_pct(message) if message else 0.30
            if win_rate is None:
                win_rate = 0.30
            win_rate = min(max(win_rate, 0.05), 0.95)
            yield from se("text", {"text": f"Win rate set to {int(win_rate*100)}%.\n\nExpected discount %? (press Enter for 10% default)"})
            yield from se("state", {**sim_state, "phase": "ask_discount", "win_rate": win_rate})

        # ── ASK DISCOUNT ──────────────────────────────────────────────────
        elif phase == "ask_discount":
            discount = parse_pct(message) if message else 0.10
            if discount is None:
                discount = 0.10
            discount = min(max(discount, 0.0), 0.50)
            win_rate   = sim_state.get("win_rate", 0.30)
            risk_level = sim_state.get("risk_level", get_risk_level())
            upsell     = sim_state.get("upsell", has_upsell())
            deal_score = sim_state.get("deal_score", get_deal_score())
            days       = sim_state.get("days", get_days_in_pipeline())
            scenarios  = calculate(win_rate, discount, risk_level, upsell, deal_score, days)
            yield from se("text", {"text": "Calculating scenarios…"})
            yield from se("table", scenarios)
            yield from se("text", {"text": "\nWrite these forecasts to Twenty CRM? (yes / no / or adjust — e.g. 'make discount 20%')"})
            yield from se("state", {**sim_state, "phase": "confirm_save",
                                     "discount": discount, "win_rate": win_rate, "scenarios": scenarios})

        # ── CONFIRM / REFINE ──────────────────────────────────────────────
        elif phase in ("confirm_save", "saved"):
            save_words = ["yes", "save", "write", "confirm", "do it", "go ahead", "yep", "y"]
            refine_words = ["make", "change", "set", "assume", "use", "adjust",
                            "discount", "win rate", "winrate", "recalculate", "redo"]
            is_save   = any(w in message for w in save_words)
            is_refine = any(w in message for w in refine_words)

            if is_refine:
                win_rate  = sim_state.get("win_rate", 0.30)
                discount  = sim_state.get("discount", 0.10)
                d_m = re.search(r'discount\s+(\d+(?:\.\d+)?)\s*%?', message)
                w_m = re.search(r'win\s*rate\s+(\d+(?:\.\d+)?)\s*%?', message)
                if d_m: discount  = float(d_m.group(1)) / 100
                if w_m: win_rate  = float(w_m.group(1)) / 100
                if not d_m and not w_m:
                    p = parse_pct(message)
                    if p:
                        discount = p if "discount" in message else win_rate
                        if "win" in message: win_rate = p
                risk_level = sim_state.get("risk_level", get_risk_level())
                upsell     = sim_state.get("upsell", has_upsell())
                deal_score = sim_state.get("deal_score", get_deal_score())
                days       = sim_state.get("days", get_days_in_pipeline())
                scenarios  = calculate(win_rate, discount, risk_level, upsell, deal_score, days)
                yield from se("text", {"text": "Recalculated:"})
                yield from se("table", scenarios)
                yield from se("text", {"text": "\nWrite to Twenty CRM? (yes / no)"})
                yield from se("state", {**sim_state, "phase": "confirm_save",
                                         "win_rate": win_rate, "discount": discount, "scenarios": scenarios,
                                         "refinement_count": sim_state.get("refinement_count", 0) + 1})

            elif is_save:
                scenarios  = sim_state.get("scenarios", {})
                risk_level = sim_state.get("risk_level", "MEDIUM")
                deal_id    = deal.get("id", "")
                worst_m    = int((scenarios.get("worst", 0)) * 1_000_000)
                avg_m      = int((scenarios.get("avg",   0)) * 1_000_000)
                best_m     = int((scenarios.get("best",  0)) * 1_000_000)
                mutation = f"""mutation {{
  updateOpportunity(
    id: "{deal_id}",
    data: {{
      forecastworstcase: {{ amountMicros: {worst_m}, currencyCode: "USD" }}
      forecastavgcase:   {{ amountMicros: {avg_m},   currencyCode: "USD" }}
      forecastbestcase:  {{ amountMicros: {best_m},  currencyCode: "USD" }}
      risklevel: {risk_level}
    }}
  ) {{ id }}
}}"""
                try:
                    _crm_t0 = datetime.now()
                    resp = requests.post(TWENTY_URL, headers=TWENTY_HEADERS,
                                         json={"query": mutation}, timeout=15)
                    _crm_ms = int((datetime.now() - _crm_t0).total_seconds() * 1000)
                    data = resp.json()
                    if "errors" in data:
                        raise ValueError(str(data["errors"]))
                    refinements = sim_state.get("refinement_count", 0)
                    record_crm_op(_fin_run_id, "write_forecast", deal.get("id",""),
                                  _crm_ms, True, "forecastworstcase,forecastavgcase,forecastbestcase,risklevel")
                    record_financial_run(_fin_run_id, deal.get("id",""), deal.get("name",""),
                                         sim_state.get("win_rate", 0.30), sim_state.get("discount", 0.10),
                                         scenarios.get("worst",0), scenarios.get("avg",0), scenarios.get("best",0),
                                         risk_level, True, refinements)
                    yield from se("crm_result", {"success": True, "worst": scenarios.get("worst"),
                                                  "avg": scenarios.get("avg"), "best": scenarios.get("best"),
                                                  "risk_level": risk_level})
                    yield from se("state", {**sim_state, "phase": "saved"})
                except Exception as e:
                    record_crm_op(_fin_run_id, "write_forecast", deal.get("id",""), 0, False, error=str(e))
                    yield from se("text", {"text": f"✗ CRM write failed: {e}\n\nYou may need to check the field names in Twenty."})
            else:
                yield from se("text", {"text": "Say 'yes' to save to CRM, or adjust parameters (e.g. 'make discount 20%' or 'win rate 40%')."})

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/financial-model", methods=["POST"])
def generate_financial_model():
    """Generate a 5-tab Excel financial model for a deal and return the file."""
    if not _FM_AVAILABLE:
        return jsonify({"status": "error", "error": "financial_model module unavailable"}), 500

    body        = request.json or {}
    deal_id     = body.get("deal_id",   "").strip()
    deal_name_q = body.get("deal_name", "").strip()

    # Try pipeline JSON first; fall back to a synthetic record built from UI context
    deal = _fm_find(deal_name=deal_name_q, deal_id=deal_id)
    if not deal:
        # Build a minimal deal record so the model can still run
        if not deal_name_q:
            return jsonify({"status": "error", "error": "No deal name provided"}), 400
        deal = {
            "deal_id":      deal_id,
            "deal_name":    deal_name_q,
            "company":      body.get("company", ""),
            "stage":        body.get("stage", ""),
            "amount":       float(body.get("amount") or 0),
            "probability":  float(body.get("probability") or 50),
            "assigned_rep": body.get("assigned_rep", ""),
            "overall_score": "NEEDS ATTENTION",
            "elements":     {},
            "flags":        {},
        }

    api_key = TOGETHER_API_KEY or os.environ.get("TOGETHER_API_KEY", "")
    result  = _fm_run(deal, api_key=api_key)

    if result["status"] != "success":
        return jsonify(result), 500

    # Stream the file back as a download
    file_path = result.get("file_path", "")
    if file_path and os.path.exists(file_path):
        from flask import send_file
        return send_file(
            file_path,
            as_attachment=True,
            download_name=os.path.basename(file_path),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    # Fallback: return JSON summary if file is missing
    return jsonify(result)


@app.route("/api/metrics", methods=["GET"])
def get_metrics():
    """Return observability metrics from the SQLite database."""
    try:
        with get_db() as con:
            # ── Pipeline summary ──────────────────────────────────────────
            rows = con.execute("SELECT total_latency_ms, success FROM pipeline_runs").fetchall()
            total_runs = len(rows)
            if total_runs:
                latencies = [r["total_latency_ms"] for r in rows if r["total_latency_ms"]]
                successes = sum(1 for r in rows if r["success"])
                success_rate = round(successes / total_runs, 4)
                avg_ms = int(statistics.mean(latencies)) if latencies else 0
                sl = sorted(latencies)
                def pct(p): return sl[max(0, int(len(sl) * p / 100) - 1)] if sl else 0
                summary = {
                    "total_runs": total_runs,
                    "success_rate": success_rate,
                    "avg_latency_ms": avg_ms,
                    "p50": pct(50), "p90": pct(90), "p95": pct(95), "p99": pct(99),
                }
            else:
                summary = {"total_runs": 0, "success_rate": 0, "avg_latency_ms": 0,
                           "p50": 0, "p90": 0, "p95": 0, "p99": 0}

            # ── Per-agent stats ───────────────────────────────────────────
            agent_rows = con.execute("""
                SELECT agent_name,
                       AVG(latency_ms) avg_ms,
                       SUM(CASE WHEN success THEN 1 ELSE 0 END) * 1.0 / COUNT(*) sr,
                       AVG(tokens_generated) avg_tokens,
                       COUNT(*) cnt
                FROM agent_runs GROUP BY agent_name ORDER BY agent_num
            """).fetchall()
            agents = [{"name": r["agent_name"], "avg_latency_ms": int(r["avg_ms"] or 0),
                       "success_rate": round(r["sr"] or 0, 4),
                       "avg_tokens": int(r["avg_tokens"] or 0),
                       "runs": r["cnt"]} for r in agent_rows]

            # ── CRM stats ─────────────────────────────────────────────────
            crm_rows = con.execute("""
                SELECT operation,
                       AVG(latency_ms) avg_ms,
                       SUM(CASE WHEN success THEN 1 ELSE 0 END) * 1.0 / COUNT(*) sr
                FROM crm_operations GROUP BY operation
            """).fetchall()
            crm_by_op = {r["operation"]: {"avg_ms": int(r["avg_ms"] or 0),
                                           "success_rate": round(r["sr"] or 0, 4)}
                         for r in crm_rows}
            fetch_ops = con.execute(
                "SELECT AVG(latency_ms), SUM(CASE WHEN success THEN 1 ELSE 0 END)*1.0/COUNT(*) "
                "FROM crm_operations WHERE operation LIKE 'fetch%'"
            ).fetchone()
            write_ops = con.execute(
                "SELECT AVG(latency_ms), SUM(CASE WHEN success THEN 1 ELSE 0 END)*1.0/COUNT(*) "
                "FROM crm_operations WHERE operation LIKE 'write%'"
            ).fetchone()
            crm = {
                "fetch_success_rate": round((fetch_ops[1] or 0), 4),
                "write_success_rate": round((write_ops[1] or 0), 4),
                "avg_fetch_ms": int(fetch_ops[0] or 0),
                "avg_write_ms": int(write_ops[0] or 0),
                "by_operation": crm_by_op,
            }

            # ── Business metrics ──────────────────────────────────────────
            deals_analyzed = con.execute(
                "SELECT COUNT(DISTINCT deal_id) FROM pipeline_runs WHERE success=1"
            ).fetchone()[0]
            risk_rows = con.execute(
                "SELECT risk_level FROM financial_runs"
            ).fetchall()
            high_risk = sum(1 for r in risk_rows if (r["risk_level"] or "").upper() in ("HIGH","CRITICAL"))
            forecasts_written = con.execute(
                "SELECT COUNT(*) FROM financial_runs WHERE written_to_crm=1"
            ).fetchone()[0]
            # Extract deal scores from arbitration text stored in pipeline_runs
            arb_rows = con.execute(
                "SELECT deal_name, deal_stage, deal_amount FROM pipeline_runs WHERE success=1"
            ).fetchall()
            top_stage = None
            if arb_rows:
                from collections import Counter
                stage_counts = Counter(r["deal_stage"] for r in arb_rows if r["deal_stage"])
                top_stage = stage_counts.most_common(1)[0][0] if stage_counts else None
            business = {
                "deals_analyzed": deals_analyzed,
                "high_risk_count": high_risk,
                "forecasts_written": forecasts_written,
                "top_stage": top_stage,
            }

            # ── Recent runs ───────────────────────────────────────────────
            recent_rows = con.execute("""
                SELECT deal_name, total_latency_ms, success, started_at
                FROM pipeline_runs ORDER BY started_at DESC LIMIT 10
            """).fetchall()
            recent = [{"deal_name": r["deal_name"], "latency_ms": r["total_latency_ms"],
                       "success": bool(r["success"]), "started_at": r["started_at"]}
                      for r in recent_rows]

            # ── Pipeline timeline (all runs ordered chronologically) ──────
            tl_rows = con.execute("""
                SELECT total_latency_ms, success, deal_name, deal_stage
                FROM pipeline_runs ORDER BY started_at ASC
            """).fetchall()
            pipeline_timeline = [
                {"latency_ms": r["total_latency_ms"], "success": bool(r["success"]),
                 "deal_name": r["deal_name"], "stage": r["deal_stage"]}
                for r in tl_rows
            ]

            # ── Financial analyst performance ─────────────────────────────
            fin_rows = con.execute("""
                SELECT risk_level, win_rate, discount,
                       forecast_worst, forecast_avg, forecast_best, refinement_count
                FROM financial_runs WHERE written_to_crm=1
            """).fetchall()
            fin_by_risk: dict = {}
            for r in fin_rows:
                rl = (r["risk_level"] or "UNKNOWN").upper()
                if rl not in fin_by_risk:
                    fin_by_risk[rl] = {"count": 0, "win_rates": [],
                                       "worsts": [], "avgs": [], "bests": []}
                fin_by_risk[rl]["count"] += 1
                fin_by_risk[rl]["win_rates"].append(r["win_rate"] or 0)
                fin_by_risk[rl]["worsts"].append(r["forecast_worst"] or 0)
                fin_by_risk[rl]["avgs"].append(r["forecast_avg"] or 0)
                fin_by_risk[rl]["bests"].append(r["forecast_best"] or 0)
            fin_summary = {}
            for rl, d in fin_by_risk.items():
                n = d["count"]
                fin_summary[rl] = {
                    "count": n,
                    "avg_win_rate": round(sum(d["win_rates"]) / n, 3),
                    "avg_worst":    round(sum(d["worsts"]) / n),
                    "avg_avg":      round(sum(d["avgs"]) / n),
                    "avg_best":     round(sum(d["bests"]) / n),
                }
            ref_rows = con.execute("""
                SELECT refinement_count, COUNT(*) cnt
                FROM financial_runs GROUP BY refinement_count ORDER BY refinement_count
            """).fetchall()
            refinements: dict = {}
            for r in ref_rows:
                key = str(r["refinement_count"]) if (r["refinement_count"] or 0) < 3 else "3+"
                refinements[key] = refinements.get(key, 0) + r["cnt"]
            financial_perf = {"by_risk": fin_summary, "refinements": refinements}

        return jsonify({
            "summary": summary,
            "agents": agents,
            "crm": crm,
            "business": business,
            "recent_runs": recent,
            "pipeline_timeline": pipeline_timeline,
            "financial_perf": financial_perf,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _is_md_table_separator(line: str) -> bool:
    return bool(_re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", line or ""))


def _split_md_table_row(line: str) -> list[str]:
    value = (line or "").strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [cell.strip() for cell in value.split("|")]


def _add_markdown_memo_to_docx(document, memo: str) -> None:
    lines = (memo or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" in line and i + 1 < len(lines) and _is_md_table_separator(lines[i + 1]):
            headers = _split_md_table_row(line)
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_md_table_row(lines[i]))
                i += 1
            table = document.add_table(rows=1, cols=len(headers))
            table.style = "Table Grid"
            for idx, header in enumerate(headers):
                table.rows[0].cells[idx].text = header
            for row in rows:
                cells = table.add_row().cells
                for idx in range(len(headers)):
                    cells[idx].text = row[idx] if idx < len(row) else ""
            document.add_paragraph()
            continue

        stripped = line.strip()
        if not stripped:
            document.add_paragraph()
        elif _re.match(r"^#{1,4}\s+", stripped):
            level = min(stripped.count("#", 0, stripped.find(" ")), 3)
            document.add_heading(_re.sub(r"^#{1,4}\s+", "", stripped), level=level)
        elif _re.match(r"^\d+\.\s+[A-Z][A-Z /,&-]+$", stripped):
            document.add_heading(stripped, level=2)
        else:
            document.add_paragraph(stripped)
        i += 1


def _memo_docx_bytes(memo: str) -> BytesIO:
    from docx import Document

    document = Document()
    document.add_heading("Deal Desk Memo", level=1)
    _add_markdown_memo_to_docx(document, memo)
    output = BytesIO()
    document.save(output)
    output.seek(0)
    return output


def _docx_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(_docx_text(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_docx_text(v)}" for k, v in value.items())
    return str(value)


def _docx_add_kv_table(document, headers: list[str], rows: list[dict], keys: list[str]) -> None:
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for idx, header in enumerate(headers):
        table.rows[0].cells[idx].text = header
    for row in rows or []:
        cells = table.add_row().cells
        for idx, key in enumerate(keys):
            value = row.get(key, "") if isinstance(row, dict) else ""
            cells[idx].text = _docx_text(value)
    document.add_paragraph()


def _enablement_report_docx_bytes(recommendation: dict, company: str = "", deal_name: str = "") -> BytesIO:
    from docx import Document

    document = Document()
    document.add_heading("Enablement Impact Report", level=1)
    if company or deal_name:
        document.add_paragraph(" / ".join([x for x in [company, deal_name] if x]))

    document.add_heading("Executive Summary", level=2)
    summary = recommendation.get("executive_summary") or []
    if summary:
        for item in summary:
            document.add_paragraph(str(item), style="List Bullet")
    else:
        document.add_paragraph(recommendation.get("account_health_rationale") or "No account-health rationale provided.")

    document.add_heading("Business Metrics", level=2)
    _docx_add_kv_table(
        document,
        ["Metric", "Value", "Notes"],
        recommendation.get("business_metrics") or [],
        ["metric", "value", "notes"],
    )

    document.add_heading("Impact Pillars", level=2)
    _docx_add_kv_table(
        document,
        ["Pillar", "Risk Level", "Metric Impacted", "Evidence", "Enablement Response"],
        recommendation.get("impact_pillars") or [],
        ["pillar", "risk_level", "metric_impacted", "evidence", "enablement_response"],
    )

    document.add_heading("Predictive Signals", level=2)
    _docx_add_kv_table(
        document,
        ["Signal", "Status", "Evidence", "Recommended Action"],
        recommendation.get("predictive_signals") or [],
        ["signal", "status", "evidence", "recommended_action"],
    )

    document.add_heading("Manager Coaching Toolkit", level=2)
    toolkit = recommendation.get("manager_coaching_toolkit") or {}
    if toolkit.get("coaching_focus"):
        document.add_paragraph(str(toolkit.get("coaching_focus")))
    _docx_add_kv_table(
        document,
        ["Area", "Guidance"],
        [
            {"area": "Behaviors to Inspect", "guidance": toolkit.get("behaviors_to_inspect") or []},
            {"area": "Manager Questions", "guidance": toolkit.get("manager_questions") or []},
            {"area": "Evidence to Review", "guidance": toolkit.get("evidence_to_review") or []},
        ],
        ["area", "guidance"],
    )

    document.add_heading("Success Measures", level=2)
    _docx_add_kv_table(
        document,
        ["Metric", "Current", "Target", "Owner", "Time Horizon"],
        recommendation.get("success_measures") or [],
        ["metric", "current", "target", "owner", "time_horizon"],
    )

    document.add_heading("Recommendation", level=2)
    content = recommendation.get("recommended_content") or {}
    document.add_paragraph(f"Course Path: {recommendation.get('recommended_course_path') or content.get('course_path') or ''}")
    personas = recommendation.get("target_personas") or content.get("target_personas") or []
    document.add_paragraph(f"Target Personas: {', '.join(personas)}")
    free_paid = recommendation.get("free_to_paid_opportunity") or {}
    document.add_paragraph(f"Motion Type: {free_paid.get('motion_type', '')}")
    document.add_paragraph(f"Pitch: {free_paid.get('pitch_narrative', '')}")
    if recommendation.get("recommendation_narrative"):
        document.add_paragraph(f"Recommendation: {recommendation.get('recommendation_narrative')}")
    if content.get("sales_play"):
        document.add_paragraph(f"Sales Play: {content.get('sales_play')}")
    document.add_paragraph(f"CTA: {recommendation.get('cta_for_account_team', '')}")

    document.add_heading("Quality Gates", level=2)
    ev = recommendation.get("evaluation") or {}
    _docx_add_kv_table(
        document,
        ["Gate", "Value"],
        [
            {"gate": "Groundedness", "value": ev.get("groundedness_score", "")},
            {"gate": "Specificity", "value": ev.get("specificity_score", "")},
            {"gate": "Actionability", "value": ev.get("actionability_score", "")},
            {"gate": "Passed", "value": ev.get("passed", "")},
        ],
        ["gate", "value"],
    )

    output = BytesIO()
    document.save(output)
    output.seek(0)
    return output


def _enablement_hydrate_report_sections(recommendation: dict, deal_amount: float = 0) -> dict:
    rec = dict(recommendation or {})
    triggers = rec.get("trigger_flags") or []
    opp = rec.get("free_to_paid_opportunity") or {}
    content = rec.get("recommended_content") or {}
    text = " ".join([
        str(rec.get("account_health_rationale") or ""),
        str(opp.get("pitch_narrative") or ""),
        str(rec.get("cta_for_account_team") or ""),
    ])
    text = _re.sub(r"\*\*", "", text)
    text = _re.sub(r"\(Source:[^)]+\)", "", text, flags=_re.I)
    text = _re.sub(r"\s+", " ", text).strip()
    rec["account_health_rationale"] = text or rec.get("account_health_rationale") or ""

    coverage_match = _re.search(r"(\d+(?:\.\d+)?)%\s+trained-user coverage", text, _re.I)
    consumption_match = _re.search(r"\$([0-9,]+)\s+annual Azure consumption", text, _re.I)
    renewal_match = _re.search(r"renewal window in\s+(\d+)\s+days", text, _re.I) or _re.search(r"Renewal is in\s+(\d+)\s+days", text, _re.I)
    risk_match = None
    for trigger in triggers:
        risk_match = _re.search(r"skills risk score\s+(\d+)", str(trigger.get("reason") or ""), _re.I)
        if risk_match:
            break

    coverage = coverage_match.group(1) if coverage_match else ""
    consumption = float((consumption_match.group(1) if consumption_match else "0").replace(",", "") or 0)
    account_value = float(deal_amount or consumption or 0)
    renewal = renewal_match.group(1) if renewal_match else ""
    risk = risk_match.group(1) if risk_match else ""
    personas = rec.get("target_personas") or content.get("target_personas") or []
    course = rec.get("recommended_course_path") or content.get("course_path") or "Role-based enablement path"
    primary = personas[0] if personas else "Account technical owner"

    if not rec.get("business_metrics"):
        rec["business_metrics"] = [
            {"metric": "Account Value / ARR Proxy", "value": f"${account_value:,.0f}", "notes": "Fallback from selected deal or annual consumption"},
            {"metric": "Azure Annual Consumption", "value": f"${consumption:,.0f}" if consumption else "-", "notes": "Parsed from enablement rationale when source metric is unavailable"},
            {"metric": "Estimated Expansion Potential", "value": f"${account_value * 0.20:,.0f}", "notes": "Proxy using 20% of account value or annual consumption"},
            {"metric": "Enablement Coverage", "value": f"{coverage}%" if coverage else "-", "notes": "Parsed from enablement rationale"},
            {"metric": "Skills-Gap Risk", "value": f"{risk}/100" if risk else "-", "notes": "Parsed from trigger evidence"},
            {"metric": "Days to Renewal", "value": renewal or "-", "notes": "Parsed from CTA or trigger evidence"},
            {"metric": "Active Trigger Count", "value": str(len(triggers)), "notes": "Current enablement trigger flags"},
        ]

    if not rec.get("impact_pillars"):
        critical = any(t.get("severity") == "CRITICAL" for t in triggers)
        stalled = next((t for t in triggers if t.get("code") == "STALLED_ADOPTION"), {})
        rec["impact_pillars"] = [
            {"pillar": "Performance", "risk_level": "High" if critical else "Medium", "metric_impacted": "Renewal confidence, consumption protection, expansion readiness", "evidence": f"{len(triggers)} active trigger flags; ${account_value:,.0f} value context.", "enablement_response": "Tie enablement milestones to account action plan and renewal review."},
            {"pillar": "Proficiency", "risk_level": "High" if int(risk or 0) >= 75 else "Medium", "metric_impacted": "Role readiness and competency improvement", "evidence": f"Coverage {coverage or 'unknown'}%; skills risk {risk or 'unknown'}/100.", "enablement_response": f"Run {course} for {', '.join(personas) or primary}."},
            {"pillar": "Productivity", "risk_level": "High" if stalled else "Watch", "metric_impacted": "Meeting readiness and adoption follow-through", "evidence": stalled.get("reason") or "No stalled-adoption trigger found.", "enablement_response": "Give the field team one sales play, one content pack, and one CTA."},
        ]

    if not rec.get("predictive_signals"):
        rec["predictive_signals"] = [{
            "signal": t.get("code") or "Trigger",
            "status": t.get("severity") or "Watch",
            "evidence": t.get("reason") or "",
            "recommended_action": "Attach the relevant enablement action to the account plan.",
        } for t in triggers]

    if not (rec.get("manager_coaching_toolkit") or {}).get("coaching_focus"):
        rec["manager_coaching_toolkit"] = {
            "coaching_focus": f"Inspect whether {primary} can apply {course} to the active account risk.",
            "behaviors_to_inspect": [
                "Champion can explain the business outcome and next adoption milestone.",
                "Technical owner can name the next implementation blocker and owner.",
                "AE/CSM has tied training progress to renewal or consumption review.",
            ],
            "manager_questions": [
                f"What evidence shows {primary} is ready for the next milestone?",
                "Which skill gap could slow renewal confidence or expansion?",
                "What proof point should be reviewed in the next customer meeting?",
            ],
            "evidence_to_review": [
                f"Enablement coverage: {coverage + '%' if coverage else 'unknown'}",
                f"Skills risk: {risk + '/100' if risk else 'unknown'}",
                f"Active triggers: {', '.join([t.get('code', '') for t in triggers]) or 'none'}",
            ],
        }

    if not rec.get("success_measures"):
        rec["success_measures"] = [
            {"metric": "Enablement Coverage", "current": f"{coverage}%" if coverage else "Unknown", "target": "70%+", "owner": "CSM / Enablement", "time_horizon": "30-45 days"},
            {"metric": "Skills-Gap Risk", "current": f"{risk}/100" if risk else "Unknown", "target": "<50/100", "owner": "SA / Manager", "time_horizon": "Before renewal review"},
            {"metric": "Trigger Closure", "current": str(len(triggers)), "target": "0 critical triggers", "owner": "AE / CSM", "time_horizon": "This month"},
        ]

    if not content.get("sales_play"):
        rec["recommended_content"] = {
            **content,
            "course_path": content.get("course_path") or course,
            "target_personas": content.get("target_personas") or personas,
            "sales_play": f"Use a {course} sprint to protect adoption and convert progress into the account-team CTA.",
        }
    return rec


def _safe_money_float(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("amount", "value", "amountMicros"):
            if key in value:
                parsed = _safe_money_float(value.get(key))
                return parsed / 1_000_000 if key == "amountMicros" else parsed
        return 0.0
    cleaned = _re.sub(r"[^0-9.\-]+", "", str(value))
    try:
        return float(cleaned or 0)
    except Exception:
        return 0.0


def _enablement_llm_writer(recommendation: dict, deal_amount: float = 0) -> dict:
    """Use the LLM for enablement narrative only; keep computed metrics unchanged."""
    rec = _enablement_hydrate_report_sections(recommendation, deal_amount)
    locked = {
        "business_metrics": rec.get("business_metrics") or [],
        "impact_pillars": rec.get("impact_pillars") or [],
        "predictive_signals": rec.get("predictive_signals") or [],
        "success_measures": rec.get("success_measures") or [],
        "trigger_flags": rec.get("trigger_flags") or [],
        "recommended_course_path": rec.get("recommended_course_path"),
        "target_personas": rec.get("target_personas") or [],
    }
    prompt = f"""
You are the Enablement Impact Agent for a sales field team.

Write polished, concise enablement narrative using ONLY the structured inputs below.
The numeric tables and trigger values are locked by code. Do not invent, alter, remove, or recalculate any metric.
Do not paste raw internal notes. Convert risks into clear field-facing language.

Return ONLY valid JSON with this shape:
{{
  "executive_summary": ["bullet 1", "bullet 2", "bullet 3"],
  "recommendation_narrative": "one short paragraph",
  "manager_coaching_toolkit": {{
    "coaching_focus": "one sentence",
    "behaviors_to_inspect": ["3 bullets"],
    "manager_questions": ["3 questions"],
    "evidence_to_review": ["3 evidence items"]
  }},
  "sales_play": "one sentence"
}}

LOCKED STRUCTURED INPUTS:
{json.dumps({
    "account_health_rationale": rec.get("account_health_rationale"),
    "business_metrics": locked["business_metrics"],
    "impact_pillars": locked["impact_pillars"],
    "predictive_signals": locked["predictive_signals"],
    "success_measures": locked["success_measures"],
    "trigger_flags": locked["trigger_flags"],
    "recommended_course_path": locked["recommended_course_path"],
    "target_personas": locked["target_personas"],
    "motion": rec.get("free_to_paid_opportunity") or {},
    "cta": rec.get("cta_for_account_team"),
    "recommended_content": rec.get("recommended_content") or {},
}, indent=2)}
"""
    try:
        raw = llm(prompt, max_tokens=900)
        match = _re.search(r"\{[\s\S]*\}", raw)
        narrative = json.loads(match.group(0) if match else raw)
        if isinstance(narrative.get("executive_summary"), list):
            rec["executive_summary"] = [str(x).strip() for x in narrative["executive_summary"] if str(x).strip()][:4]
        if narrative.get("recommendation_narrative"):
            rec["recommendation_narrative"] = str(narrative["recommendation_narrative"]).strip()
        toolkit = narrative.get("manager_coaching_toolkit")
        if isinstance(toolkit, dict) and toolkit.get("coaching_focus"):
            rec["manager_coaching_toolkit"] = {
                "coaching_focus": str(toolkit.get("coaching_focus") or "").strip(),
                "behaviors_to_inspect": [str(x).strip() for x in toolkit.get("behaviors_to_inspect") or [] if str(x).strip()][:3],
                "manager_questions": [str(x).strip() for x in toolkit.get("manager_questions") or [] if str(x).strip()][:3],
                "evidence_to_review": [str(x).strip() for x in toolkit.get("evidence_to_review") or [] if str(x).strip()][:3],
            }
        if narrative.get("sales_play"):
            content = rec.get("recommended_content") or {}
            rec["recommended_content"] = {**content, "sales_play": str(narrative["sales_play"]).strip()}
        rec["llm_writer"] = {"enabled": True, "status": "used", "model": TOGETHER_MODEL}
    except Exception as exc:
        rec["llm_writer"] = {"enabled": bool(TOGETHER_API_KEY), "status": "fallback", "error": str(exc)}

    rec["business_metrics"] = locked["business_metrics"]
    rec["impact_pillars"] = locked["impact_pillars"]
    rec["predictive_signals"] = locked["predictive_signals"]
    rec["success_measures"] = locked["success_measures"]
    rec["trigger_flags"] = locked["trigger_flags"]
    rec["recommended_course_path"] = locked["recommended_course_path"]
    rec["target_personas"] = locked["target_personas"]
    return rec


@app.route("/api/save-memo", methods=["POST"])
def save_memo():
    """Save memo to file."""
    body = request.get_json(silent=True) or {}
    memo = (body.get("memo") or "").strip()
    if not memo:
        return jsonify({"saved": False, "error": "No memo content provided"}), 400

    raw_deal_name = body.get("dealName") or "deal"
    deal_name = _re.sub(r"[^a-zA-Z0-9_-]+", "_", str(raw_deal_name).replace("—", "-")).strip("_")[:60] or "deal"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"memo_{deal_name}_{timestamp}.docx"

    filepath = os.path.join(os.path.dirname(__file__), filename)
    try:
        docx_bytes = _memo_docx_bytes(memo)
        with open(filepath, "wb") as f:
            f.write(docx_bytes.getvalue())
    except Exception as exc:
        return jsonify({"saved": False, "error": str(exc)}), 500

    docx_bytes.seek(0)
    return send_file(
        docx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=filename,
    )


# ── APPROVAL STATE MACHINE ────────────────────────────────────────────────

APPROVAL_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "approval_audit.db")

# Full approval chain — CVP level is conditionally included for deals > $300k
APPROVAL_CHAIN = [
    {"level": "L1",  "role": "Pursuit Lead"},
    {"level": "L2",  "role": "Account Executive"},
    {"level": "L3",  "role": "Delivery Manager"},
    {"level": "L4",  "role": "Area Leader"},
    {"level": "CVP", "role": "CVP"},
]

def init_approval_db():
    with sqlite3.connect(APPROVAL_DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS approval_audit (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp            TEXT,
            deal_name            TEXT,
            deal_value           REAL,
            current_level        TEXT,
            role_name            TEXT,
            ai_decision          TEXT,
            policy_rule_triggered TEXT,
            action               TEXT,
            actioned_by          TEXT,
            notes                TEXT,
            memo_file            TEXT
        )
        """)

init_approval_db()


@contextmanager
def get_approval_db():
    con = sqlite3.connect(APPROVAL_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _build_chain_for_deal(deal_value: float) -> list:
    """Return the approval chain, dropping CVP level when value <= $300k."""
    if deal_value > 300_000:
        return APPROVAL_CHAIN
    return [s for s in APPROVAL_CHAIN if s["level"] != "CVP"]


def _current_state(deal_name: str) -> dict:
    """
    Derive the current approval state for a deal from the audit log.
    Returns a dict: { current_level, role, fully_approved, history,
                      approval_history, status }
    """
    with get_approval_db() as con:
        rows = con.execute(
            "SELECT * FROM approval_audit WHERE deal_name = ? ORDER BY id",
            (deal_name,)
        ).fetchall()

    if not rows:
        return {
            "current_level": None, "role": None,
            "fully_approved": False, "history": [],
            "approval_history": [], "status": "not_found",
        }

    history    = [dict(r) for r in rows]
    deal_value = rows[-1]["deal_value"] or 0
    chain      = _build_chain_for_deal(deal_value)

    # Structured history for the thread view (skip "initialized" system rows)
    approval_history = [
        {
            "level":     r["current_level"],
            "role":      r["role_name"],
            "action":    r["action"],
            "notes":     r["notes"] or "",
            "actioned_by": r["actioned_by"] or "",
            "timestamp": r["timestamp"],
        }
        for r in history if r["action"] not in ("initialized",)
    ]

    # Check if deal was rejected
    rejected = any(r["action"] == "rejected" for r in history)
    if rejected:
        rej = next(r for r in reversed(history) if r["action"] == "rejected")
        return {
            "current_level":    rej["current_level"],
            "role":             rej["role_name"],
            "fully_approved":   False,
            "history":          history,
            "approval_history": approval_history,
            "status":           "rejected",
        }

    approved_count = sum(1 for r in history if r["action"] == "approved")

    if approved_count >= len(chain):
        last = chain[-1]
        return {
            "current_level":    last["level"],
            "role":             last["role"],
            "fully_approved":   True,
            "history":          history,
            "approval_history": approval_history,
            "status":           "fully_approved",
        }

    current = chain[approved_count]
    return {
        "current_level":    current["level"],
        "role":             current["role"],
        "fully_approved":   False,
        "history":          history,
        "approval_history": approval_history,
        "status":           "pending_approval",
    }


@app.route("/deal/<deal_name>/approval_status", methods=["GET"])
def approval_status(deal_name: str):
    """Return current approval level, triggered policy rules, and full history."""
    try:
        from deal_desk import check_policy_rules
        policy_available = True
    except ImportError:
        policy_available = False

    state = _current_state(deal_name)

    # Resolve deal value from history (latest record) for policy rule evaluation
    deal_value = 0
    if state["history"]:
        deal_value = state["history"][-1].get("deal_value") or 0

    policy_rules = []
    if policy_available:
        deal_stub = {"amount": deal_value}
        policy_rules = check_policy_rules(deal_stub)

    chain = _build_chain_for_deal(deal_value)

    # Pull ai_decision and level_notes from memory if available
    mem          = active_approvals.get(deal_name, {})
    ai_decision  = mem.get("ai_decision", "")
    level_notes  = mem.get("level_notes", {})
    deal_stage   = mem.get("deal_stage", "")

    return jsonify({
        "deal_name":        deal_name,
        "deal_value":       deal_value,
        "deal_stage":       deal_stage,
        "ai_decision":      ai_decision,
        "current_level":    state["current_level"],
        "current_role":     state["role"],
        "fully_approved":   state["fully_approved"],
        "approval_chain":   chain,
        "policy_rules":     [r for r in policy_rules if r["triggered"]],
        "history":          state["history"],
        "approval_history": state["approval_history"],
        "level_notes":      level_notes,
        "status":           state["status"],
    })


@app.route("/deal/<deal_name>/approve", methods=["POST"])
def approve_deal(deal_name: str):
    """
    Advance the deal one step through the approval chain.
    Body: { "actioned_by": str, "notes": str, "deal_value": float (optional),
            "ai_decision": str (optional), "memo_file": str (optional) }
    """
    body         = request.get_json(force=True) or {}
    actioned_by  = body.get("actioned_by", "").strip()
    notes        = body.get("notes", "")
    deal_value   = float(body.get("deal_value") or 0)
    ai_decision  = body.get("ai_decision", "")
    memo_file    = body.get("memo_file", "")

    if not actioned_by:
        return jsonify({"error": "actioned_by is required"}), 400

    state = _current_state(deal_name)

    if state["fully_approved"]:
        return jsonify({"error": "Deal is already fully approved", "deal_name": deal_name}), 409

    # If first approval action, use provided deal_value; otherwise inherit from history
    if state["history"]:
        deal_value = state["history"][-1].get("deal_value") or deal_value

    chain = _build_chain_for_deal(deal_value)

    # Current step is the one being approved right now
    approved_count = sum(1 for r in state["history"] if r["action"] == "approved")
    if approved_count >= len(chain):
        return jsonify({"error": "Approval chain already complete"}), 409

    current_step = chain[approved_count]

    # Collect triggered policy rules to log
    try:
        from deal_desk import check_policy_rules
        triggered_rules = [
            r["rule_name"]
            for r in check_policy_rules({"amount": deal_value})
            if r["triggered"]
        ]
        policy_rule_str = ", ".join(triggered_rules) if triggered_rules else ""
    except ImportError:
        policy_rule_str = ""

    # Write audit record
    with get_approval_db() as con:
        con.execute(
            """INSERT INTO approval_audit
               (timestamp, deal_name, deal_value, current_level, role_name,
                ai_decision, policy_rule_triggered, action, actioned_by, notes, memo_file)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(),
                deal_name,
                deal_value,
                current_step["level"],
                current_step["role"],
                ai_decision,
                policy_rule_str,
                "approved",
                actioned_by,
                notes,
                memo_file,
            )
        )

    # Determine new state after this approval
    new_approved_count = approved_count + 1
    fully_approved = new_approved_count >= len(chain)

    if fully_approved:
        next_level = current_step["level"]
        next_role  = current_step["role"]
        message    = "Deal fully approved through all levels"
    else:
        next_step  = chain[new_approved_count]
        next_level = next_step["level"]
        next_role  = next_step["role"]
        message    = f"Approved at {current_step['level']}. Awaiting {next_role} ({next_level})"

    # Advance in-memory approval state
    if deal_name in active_approvals:
        mem = active_approvals[deal_name]
        if "level_notes" not in mem:
            mem["level_notes"] = {}
        mem["level_notes"][current_step["level"]] = notes
        mem["current_level"] = next_level
        mem["current_role"]  = next_role
        mem["status"]        = "fully_approved" if fully_approved else "pending_approval"

    return jsonify({
        "deal_name":       deal_name,
        "approved_level":  current_step["level"],
        "approved_role":   current_step["role"],
        "next_level":      next_level,
        "next_role":       next_role,
        "fully_approved":  fully_approved,
        "message":         message,
    })


@app.route("/deal/<deal_name>/init-approval", methods=["POST"])
def init_approval(deal_name: str):
    """
    Initialize approval queue entry for a deal from the UI.
    Called by the 'Send to Approval Queue' button in the Memo tab.
    Body: { deal_value, deal_stage, ai_decision, memo }
    """
    body        = request.get_json(force=True) or {}
    deal_value  = float(body.get("deal_value") or 0)
    deal_stage  = body.get("deal_stage", "")
    ai_decision = body.get("ai_decision", "")

    # If already pending, return current state
    if deal_name in active_approvals and active_approvals[deal_name].get("status") == "pending_approval":
        return jsonify({"status": "already_queued", "deal_name": deal_name})

    try:
        from deal_desk import check_policy_rules
        triggered = [r for r in check_policy_rules({"amount": deal_value}) if r["triggered"]]
    except ImportError:
        triggered = []

    chain = _build_chain_for_deal(deal_value)

    state = {
        "deal_name":      deal_name,
        "deal_value":     deal_value,
        "deal_stage":     deal_stage,
        "ai_decision":    ai_decision,
        "current_level":  "L1",
        "current_role":   "Pursuit Lead",
        "policy_rules":   triggered,
        "approval_chain": chain,
        "level_notes":    {},
        "status":         "pending_approval",
    }
    active_approvals[deal_name] = state

    policy_rule_str = ", ".join(r["rule_name"] for r in triggered)

    # Only write init record if one doesn't already exist
    with get_approval_db() as con:
        exists = con.execute(
            "SELECT 1 FROM approval_audit WHERE deal_name=? AND action='initialized'",
            (deal_name,)
        ).fetchone()
        if not exists:
            con.execute(
                """INSERT INTO approval_audit
                   (timestamp, deal_name, deal_value, current_level, role_name,
                    ai_decision, policy_rule_triggered, action, actioned_by, notes, memo_file)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now().isoformat(),
                    deal_name, deal_value, "L1", "Pursuit Lead",
                    ai_decision, policy_rule_str,
                    "initialized", "system",
                    "Sent to approval queue from Memo tab", "",
                )
            )

    return jsonify({"status": "queued", "deal_name": deal_name,
                    "current_level": "L1", "current_role": "Pursuit Lead"})


@app.route("/deal/<deal_name>/reject", methods=["POST"])
def reject_deal(deal_name: str):
    """
    Reject a deal at the current approval level.
    Body: { "actioned_by": str, "notes": str }
    Logs to audit, marks active_approvals status as rejected.
    """
    body        = request.get_json(force=True) or {}
    actioned_by = body.get("actioned_by", "").strip()
    notes       = body.get("notes", "").strip()

    if not actioned_by:
        return jsonify({"error": "actioned_by is required"}), 400
    if not notes:
        return jsonify({"error": "notes (rejection reason) is required"}), 400

    state      = _current_state(deal_name)
    deal_value = 0
    if state["history"]:
        deal_value = state["history"][-1].get("deal_value") or 0

    if state["status"] == "rejected":
        return jsonify({"error": "Deal already rejected"}), 409
    if state["fully_approved"]:
        return jsonify({"error": "Deal is already fully approved"}), 409

    current_level = state["current_level"] or "L1"
    current_role  = state["role"]         or "Unknown"

    with get_approval_db() as con:
        con.execute(
            """INSERT INTO approval_audit
               (timestamp, deal_name, deal_value, current_level, role_name,
                ai_decision, policy_rule_triggered, action, actioned_by, notes, memo_file)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(),
                deal_name, deal_value, current_level, current_role,
                active_approvals.get(deal_name, {}).get("ai_decision", ""),
                "",
                "rejected",
                actioned_by,
                notes,
                "",
            )
        )

    # Mark in memory
    if deal_name in active_approvals:
        active_approvals[deal_name]["status"] = "rejected"

    return jsonify({
        "deal_name":   deal_name,
        "rejected_at": current_level,
        "rejected_by": actioned_by,
        "notes":       notes,
        "message":     f"Deal rejected by {actioned_by} at {current_level}",
    })


@app.route("/api/approval-queue", methods=["GET"])
def approval_queue():
    """
    Return all deals currently pending approval.
    Primary source: active_approvals (in-memory).
    Fallback: reconstruct from audit DB for deals that survived a server restart.
    """
    queue = []

    # 1. In-memory deals
    memory_names = set()
    for deal_name, state in active_approvals.items():
        if state.get("status") not in ("pending_approval",):
            continue
        memory_names.add(deal_name)
        queue.append({
            "deal_name":     deal_name,
            "deal_value":    state.get("deal_value", 0),
            "deal_stage":    state.get("deal_stage", ""),
            "current_level": state.get("current_level", "L1"),
            "current_role":  state.get("current_role", "Pursuit Lead"),
            "policy_rules":  state.get("policy_rules", []),
            "ai_decision":   state.get("ai_decision", ""),
            "status":        "pending_approval",
        })

    # 2. DB fallback — deals in audit but not in memory (post server-restart)
    with get_approval_db() as con:
        db_deals = con.execute(
            """SELECT DISTINCT deal_name FROM approval_audit
               WHERE action = 'initialized'"""
        ).fetchall()

    for row in db_deals:
        name = row["deal_name"]
        if name in memory_names:
            continue
        state = _current_state(name)
        if state["status"] != "pending_approval":
            continue
        dv = state["history"][-1].get("deal_value", 0) if state["history"] else 0
        queue.append({
            "deal_name":     name,
            "deal_value":    dv,
            "deal_stage":    "",
            "current_level": state["current_level"],
            "current_role":  state["role"],
            "policy_rules":  [],
            "ai_decision":   "",
            "status":        "pending_approval",
        })

    queue.sort(key=lambda x: x["deal_value"], reverse=True)
    return jsonify({"pending": queue, "count": len(queue)})


@app.route("/audit/recent", methods=["GET"])
def audit_recent():
    """Return the last 10 records from the approval audit log."""
    with get_approval_db() as con:
        rows = con.execute(
            "SELECT * FROM approval_audit ORDER BY id DESC LIMIT 10"
        ).fetchall()
    return jsonify([dict(r) for r in rows])




# ── END APPROVAL STATE MACHINE ────────────────────────────────────────────


# ── DECK GENERATION ──────────────────────────────────────────────────────────

try:
    from generate_deck import generate_deck as _generate_deck
    _DECK_AVAILABLE = True
except Exception as _deck_err:
    print(f"[Deck] unavailable: {_deck_err}")
    _DECK_AVAILABLE = False


@app.route("/api/mbr/deck", methods=["POST"])
def mbr_deck():
    """Generate a 3-slide pipeline intelligence deck and return it as a download."""
    from flask import send_file
    if not _DECK_AVAILABLE:
        return jsonify({"error": "python-pptx not available"}), 500
    body = request.json or {}
    period = body.get("period") or datetime.now().strftime("%B %Y")
    try:
        path = _generate_deck(period=period)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return send_file(path, as_attachment=True,
                     download_name=f"pipeline_deck_{period.lower().replace(' ', '_')}.pptx",
                     mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation")


# ── PHASE 5 DASHBOARD ────────────────────────────────────────────────────────

_PIPELINE_JSON = os.path.join(BASE_DIR, "knowledge_base", "meddpicc_pipeline.json")

_FLAG_NAMES = {
    "missing_economic_buyer":  "Missing Economic Buyer",
    "missing_champion":        "No Confirmed Champion",
    "unquantified_pain":       "Pain Not Quantified",
    "competitor_unknown":      "Competitor Unknown",
    "paper_process_unknown":   "Procurement Not Discussed",
    "deal_overdue":            "Close Date Passed",
    "stalled":                 "Deal Stalled 90+ Days",
}

_ELEMENT_ORDER = [
    "metrics", "economic_buyer", "decision_criteria", "decision_process",
    "paper_process", "identify_pain", "champion", "competition",
]
_ELEMENT_LABELS = {
    "metrics":            "Metrics",
    "economic_buyer":     "Economic Buyer",
    "decision_criteria":  "Decision Criteria",
    "decision_process":   "Decision Process",
    "paper_process":      "Paper Process",
    "identify_pain":      "Identify Pain",
    "champion":           "Champion",
    "competition":        "Competition",
}
_STATUS_SCORE = {"confirmed": 3, "partial": 2, "at_risk": 1, "unknown": 0}


def _load_pipeline():
    if not os.path.exists(_PIPELINE_JSON):
        return None
    with open(_PIPELINE_JSON) as f:
        return json.load(f)


def _extract_product(deal_name: str) -> str:
    for sep in [" — ", " – ", " - "]:
        if sep in deal_name:
            return deal_name.split(sep)[0].strip()
    return deal_name.split()[0]


@app.route("/dashboard")
def dashboard():
    return send_from_directory(BASE_DIR, "dashboard.html")


@app.route("/gtm-intelligence")
def gtm_intelligence():
    return send_from_directory(BASE_DIR, "gtm_intelligence.html")

@app.route("/commercial-analysis")
def commercial_analysis():
    return send_from_directory(BASE_DIR, "commercial_analysis.html")

@app.route("/consumption-analysis")
def consumption_analysis():
    return send_from_directory(BASE_DIR, "consumption_analysis.html")


def _commercial_deal_selector_rows():
    from tools.commercial_data import DEAL_INPUTS_PATH, _load_json

    deals = _load_json(DEAL_INPUTS_PATH)
    rows = []
    for item in deals:
        opp = item.get("opportunity", {})
        account = item.get("account", {})
        if opp.get("stage") != "PROPOSAL" and not str(item.get("deal_id", "")).startswith("GOLDEN-"):
            continue
        rows.append({
            "deal_id": item.get("deal_id"),
            "account": account.get("name"),
            "opportunity": opp.get("name"),
            "stage": opp.get("stage"),
            "strategic_account": account.get("strategic_account"),
            "competitive_deal": (item.get("competitive_context") or {}).get("competitive_deal"),
        })
    rows.sort(key=lambda r: r.get("account") or "")
    default_deal_id = rows[0]["deal_id"] if rows else None
    return rows, default_deal_id


def _commercial_error_response(exc):
    code = getattr(exc, "code", "COMMERCIAL_ANALYSIS_ERROR")
    message = getattr(exc, "message", str(exc))
    return jsonify({"error": code, "message": message}), 400


@app.route("/api/commercial-analysis/deals")
def commercial_analysis_deals():
    try:
        rows, default_deal_id = _commercial_deal_selector_rows()
        return jsonify({"deals": rows, "count": len(rows), "default_deal_id": default_deal_id})
    except Exception as exc:
        return jsonify({"error": "COMMERCIAL_DEALS_LOAD_ERROR", "message": str(exc)}), 500


@app.route("/api/consumption-analysis/deals")
def consumption_analysis_deals():
    try:
        rows, default_deal_id = _commercial_deal_selector_rows()
        return jsonify({"deals": rows, "count": len(rows), "default_deal_id": default_deal_id})
    except Exception as exc:
        return jsonify({"error": "CONSUMPTION_DEALS_LOAD_ERROR", "message": str(exc)}), 500


@app.route("/api/commercial-analysis/<deal_id>")
def commercial_analysis_data(deal_id):
    try:
        from tools.commercial_data import get_deal_input
        from tools.commercial_financial_analysis import get_commercial_financial_analysis

        deal_input = get_deal_input(deal_id)
        if not deal_input:
            return jsonify({"error": "MISSING_DEAL_INPUT", "message": f"No DealInput found for {deal_id}"}), 404
        analysis = get_commercial_financial_analysis(deal_id)
        return jsonify({"deal_input": deal_input, "financial_analysis": analysis})
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>")
def consumption_analysis_data(deal_id):
    try:
        from tools.consumption_profiles import get_consumption_analysis_record

        record = get_consumption_analysis_record(deal_id)
        if not record:
            return jsonify({"error": "MISSING_DEAL_INPUT", "message": f"No DealInput found for {deal_id}"}), 404
        return jsonify(record)
    except Exception as exc:
        return jsonify({"error": "CONSUMPTION_DEAL_LOAD_ERROR", "message": str(exc)}), 500


@app.route("/api/consumption-analysis/<deal_id>/profile", methods=["GET", "PUT"])
def consumption_analysis_profile(deal_id):
    try:
        from tools.commercial_data import get_deal_input
        from tools.consumption_profiles import get_consumption_profile, save_consumption_profile, ConsumptionProfileValidationError

        if not get_deal_input(deal_id):
            return jsonify({"error": "MISSING_DEAL_INPUT", "message": f"No DealInput found for {deal_id}"}), 404
        if request.method == "PUT":
            body = request.get_json(silent=True) or {}
            updates = body.get("profile") or body
            if not isinstance(updates, dict):
                return jsonify({"error": "invalid_profile", "message": "Profile must be an object."}), 400
            profile = save_consumption_profile(deal_id, updates)
            return jsonify({"deal_id": deal_id, "consumption_profile": profile})
        return jsonify({"deal_id": deal_id, "consumption_profile": get_consumption_profile(deal_id)})
    except ConsumptionProfileValidationError as exc:
        return jsonify({"error": "invalid_profile", "message": str(exc)}), 422
    except Exception as exc:
        return jsonify({"error": "CONSUMPTION_PROFILE_LOAD_ERROR", "message": str(exc)}), 500


@app.route("/api/consumption-analysis/service-catalog/refresh", methods=["POST"])
def consumption_service_catalog_refresh():
    try:
        from tools.azure_service_catalog import refresh_catalog
        body = request.get_json(silent=True) or {}
        filter_str = body.get("filter") or "priceType eq 'Consumption' and armRegionName eq 'eastus'"
        return jsonify(refresh_catalog(filter_str))
    except Exception as exc:
        return jsonify({"error": "service_catalog_refresh_failed", "message": str(exc)}), 502


@app.route("/api/consumption-analysis/service-catalog")
def consumption_service_catalog():
    from tools.azure_service_catalog import load_catalog
    return jsonify(load_catalog())


@app.route("/api/consumption-analysis/pricing-snapshot/refresh", methods=["POST"])
def consumption_pricing_snapshot_refresh():
    """Phase 8.4.5: download + normalize the East US master pricing snapshot,
    then rebuild the curated architecture catalog from it. Atomic -- a failed
    refresh never destroys a previously valid snapshot."""
    try:
        from tools.azure_pricing_snapshot import refresh_snapshot, load_snapshot
        from tools.azure_enterprise_catalog import refresh_catalog

        result = refresh_snapshot()
        if result.get("refresh_status") in ("SUCCESS", "PARTIAL"):
            try:
                catalog = refresh_catalog(load_snapshot())
                result["architecture_catalog"] = catalog["metadata"]
            except Exception as catalog_exc:
                result["architecture_catalog_error"] = str(catalog_exc)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": "pricing_snapshot_refresh_failed", "message": str(exc)}), 502


@app.route("/api/consumption-analysis/pricing-snapshot")
def consumption_pricing_snapshot_info():
    """Phase 8.4.5: snapshot metadata only -- never the full meter list
    unless explicitly requested via ?include_records=true."""
    from tools.azure_pricing_snapshot import load_snapshot, snapshot_status

    snapshot = load_snapshot()
    info = snapshot_status(snapshot)
    if request.args.get("include_records") == "true" and snapshot:
        info = {**info, "records": snapshot.get("records", [])}
    return jsonify(info)


@app.route("/api/consumption-analysis/architecture-catalog")
def consumption_architecture_catalog():
    """Phase 8.4.5 curated enterprise architecture catalog (metadata + entries),
    distinct from the meter-level pricing snapshot."""
    from tools.azure_enterprise_catalog import load_catalog

    catalog = load_catalog()
    if not catalog:
        return jsonify({"error": "architecture_catalog_missing", "message": "No architecture catalog has been built yet."}), 404
    return jsonify(catalog)


def _architecture_error_response(exc):
    code = getattr(exc, "code", "CONSUMPTION_ARCHITECTURE_ERROR")
    message = getattr(exc, "message", str(exc))
    status = {
        "MISSING_DEAL_INPUT": 404,
        "CONSUMPTION_PROFILE_REQUIRED": 409,
        "LLM_UNAVAILABLE": 503,
        "LLM_TIMEOUT": 504,
        "LLM_CONNECTION_ERROR": 502,
        "LLM_REQUEST_FAILED": 502,
        "MALFORMED_ARCHITECTURE_JSON": 400,
        "INVALID_ARCHITECTURE_JSON": 400,
        "ARCHITECTURE_VALIDATION_FAILED": 422,
        "CATALOG_CONFORMANCE_FAILED": 422,
    }.get(code, 500)
    return jsonify({"error": code.lower(), "message": message}), status


def _pricing_error_response(exc):
    code = getattr(exc, "code", "AZURE_PRICING_ERROR")
    message = getattr(exc, "message", str(exc))
    status = {
        "MISSING_DEAL_INPUT": 404,
        "CONSUMPTION_PROFILE_REQUIRED": 409,
        "ARCHITECTURE_REQUIRED": 409,
        "PRICING_REQUIRED": 409,
        "SERVICE_NOT_FOUND": 404,
        "INCOMPATIBLE_UNITS": 422,
    }.get(code, 500)
    return jsonify({"error": code.lower(), "message": message}), status


def _forecast_error_response(exc):
    code = getattr(exc, "code", "CONSUMPTION_FORECAST_ERROR")
    message = getattr(exc, "message", str(exc))
    status = {
        "MISSING_DEAL_INPUT": 404,
        "CONSUMPTION_PROFILE_REQUIRED": 409,
        "ARCHITECTURE_REQUIRED": 409,
        "PRICING_REQUIRED": 409,
        "PRICING_STALE": 409,
        "NO_RESOLVED_SERVICES": 409,
        "FORECAST_RECONCILIATION_FAILED": 422,
    }.get(code, 500)
    return jsonify({"error": code.lower(), "message": message}), status


def _consumption_intelligence_error_response(exc):
    code = getattr(exc, "code", "CONSUMPTION_COMMERCIAL_INTELLIGENCE_ERROR")
    message = getattr(exc, "message", str(exc))
    status = {
        "MISSING_DEAL_INPUT": 404,
        "FORECAST_REQUIRED": 409,
        "FORECAST_STALE": 409,
        "COMMITMENT_REQUIRED": 409,
        "INVALID_INPUT": 422,
        "INVALID_DISCOUNT_RATE": 422,
        "MISSING_BUSINESS_CASE_ASSUMPTION": 422,
        "BUSINESS_CASE_ASSUMPTIONS_REQUIRED": 409,
        "ZERO_PROPOSED_COST": 422,
        "CURRENCY_MISMATCH": 409,
    }.get(code, 500)
    return jsonify({"error": code.lower(), "message": message}), status


@app.route("/api/consumption-analysis/<deal_id>/generate-architecture", methods=["POST"])
def consumption_analysis_generate_architecture(deal_id):
    try:
        from tools.consumption_architecture_planner import generate_architecture

        body = request.get_json(silent=True) or {}
        return jsonify(generate_architecture(deal_id, architecture_mode=body.get("architecture_mode", "EXPECTED")))
    except Exception as exc:
        return _architecture_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/architecture")
def consumption_analysis_get_architecture(deal_id):
    try:
        from tools.commercial_data import get_deal_input
        from tools.consumption_architecture_planner import get_architecture, get_architecture_for_mode

        if not get_deal_input(deal_id):
            return jsonify({"error": "missing_deal_input", "message": f"No DealInput found for {deal_id}"}), 404
        mode = (request.args.get("mode") or "").upper()
        record = get_architecture_for_mode(deal_id, mode) if mode else get_architecture(deal_id)
        return jsonify({"deal_id": deal_id, "architecture_record": record})
    except Exception as exc:
        return _architecture_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/architecture", methods=["PUT"])
def consumption_analysis_put_architecture(deal_id):
    try:
        from tools.consumption_architecture_planner import update_architecture

        body = request.get_json(silent=True) or {}
        architecture = body.get("architecture") or body
        return jsonify(update_architecture(deal_id, architecture))
    except Exception as exc:
        return _architecture_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/price-architecture", methods=["POST"])
def consumption_analysis_price_architecture(deal_id):
    try:
        from tools.azure_retail_pricing import price_architecture

        body = request.get_json(silent=True) or {}
        return jsonify(price_architecture(deal_id, currency=body.get("currency", "USD"), refresh=bool(body.get("refresh"))))
    except Exception as exc:
        return _pricing_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/pricing")
def consumption_analysis_get_pricing(deal_id):
    try:
        from tools.commercial_data import get_deal_input
        from tools.azure_retail_pricing import get_pricing

        if not get_deal_input(deal_id):
            return jsonify({"error": "missing_deal_input", "message": f"No DealInput found for {deal_id}"}), 404
        return jsonify({"deal_id": deal_id, "pricing": get_pricing(deal_id)})
    except Exception as exc:
        return _pricing_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/pricing/<service_id>", methods=["PUT"])
def consumption_analysis_put_service_pricing(deal_id, service_id):
    try:
        from tools.azure_retail_pricing import apply_manual_meter

        body = request.get_json(silent=True) or {}
        meter = body.get("meter") or body
        return jsonify(apply_manual_meter(deal_id, service_id, meter))
    except Exception as exc:
        return _pricing_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/generate-forecast", methods=["POST"])
def consumption_analysis_generate_forecast(deal_id):
    try:
        from tools.consumption_forecast import generate_forecast

        return jsonify(generate_forecast(deal_id))
    except Exception as exc:
        return _forecast_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/forecast")
def consumption_analysis_get_forecast(deal_id):
    try:
        from tools.commercial_data import get_deal_input
        from tools.consumption_forecast import get_forecast

        if not get_deal_input(deal_id):
            return jsonify({"error": "missing_deal_input", "message": f"No DealInput found for {deal_id}"}), 404
        return jsonify({"deal_id": deal_id, "forecast": get_forecast(deal_id)})
    except Exception as exc:
        return _forecast_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/forecast/<scenario>")
def consumption_analysis_get_forecast_scenario(deal_id, scenario):
    try:
        from tools.consumption_forecast import get_forecast

        forecast = get_forecast(deal_id)
        if not forecast:
            return jsonify({"deal_id": deal_id, "scenario": scenario, "forecast": None}), 200
        selected = forecast.get("scenarios", {}).get(scenario.upper())
        if not selected:
            return jsonify({"error": "missing_forecast_scenario", "message": f"No forecast scenario found for {scenario}"}), 404
        return jsonify({"deal_id": deal_id, "scenario": scenario.upper(), "forecast": selected, "forecast_status": forecast.get("forecast_status")})
    except Exception as exc:
        return _forecast_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/commercial-intelligence")
def consumption_analysis_get_commercial_intelligence(deal_id):
    try:
        from tools.commercial_data import get_deal_input
        from tools.consumption_commercial_intelligence import get_commercial_intelligence

        if not get_deal_input(deal_id):
            return jsonify({"error": "missing_deal_input", "message": f"No DealInput found for {deal_id}"}), 404
        return jsonify({"deal_id": deal_id, "commercial_intelligence": get_commercial_intelligence(deal_id)})
    except Exception as exc:
        return _consumption_intelligence_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/commercial-intelligence", methods=["POST"])
def consumption_analysis_calculate_commercial_intelligence(deal_id):
    try:
        from tools.consumption_commercial_intelligence import calculate_commercial_intelligence

        return jsonify(calculate_commercial_intelligence(deal_id))
    except Exception as exc:
        return _consumption_intelligence_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/business-case")
def consumption_analysis_get_business_case(deal_id):
    try:
        from tools.commercial_data import get_deal_input
        from tools.consumption_commercial_intelligence import get_business_case, get_business_case_assumptions

        if not get_deal_input(deal_id):
            return jsonify({"error": "missing_deal_input", "message": f"No DealInput found for {deal_id}"}), 404
        return jsonify({
            "deal_id": deal_id,
            "business_case": get_business_case(deal_id),
            "assumptions": get_business_case_assumptions(deal_id),
        })
    except Exception as exc:
        return _consumption_intelligence_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/business-case", methods=["PUT"])
def consumption_analysis_put_business_case_assumptions(deal_id):
    try:
        from tools.commercial_data import get_deal_input
        from tools.consumption_commercial_intelligence import save_business_case_assumptions

        if not get_deal_input(deal_id):
            return jsonify({"error": "missing_deal_input", "message": f"No DealInput found for {deal_id}"}), 404
        body = request.get_json(silent=True) or {}
        assumptions = body.get("assumptions") or body
        assumptions["deal_id"] = deal_id
        return jsonify(save_business_case_assumptions(assumptions))
    except Exception as exc:
        return _consumption_intelligence_error_response(exc)


@app.route("/api/consumption-analysis/<deal_id>/calculate-business-case", methods=["POST"])
def consumption_analysis_calculate_business_case(deal_id):
    try:
        from tools.consumption_commercial_intelligence import calculate_business_case

        return jsonify(calculate_business_case(deal_id))
    except Exception as exc:
        return _consumption_intelligence_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/scenario", methods=["POST"])
def commercial_analysis_scenario(deal_id):
    try:
        from tools.commercial_financial_analysis import evaluate_financial_scenario

        payload = request.get_json(silent=True) or {}
        result = evaluate_financial_scenario(deal_id, payload)
        return jsonify(result)
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/trace")
def commercial_analysis_trace(deal_id):
    try:
        from tools.commercial_financial_analysis import get_financial_metric_trace

        scenario_name = request.args.get("scenario")
        return jsonify(get_financial_metric_trace(deal_id, scenario_name=scenario_name))
    except Exception as exc:
        return _commercial_error_response(exc)


def _default_policy_scenarios_for_deal(deal_id):
    from tools.commercial_data import get_deal_input
    from tools.commercial_financial_analysis import (
        commercial_floor_discount_pct as _commercial_floor_discount,
        default_working_discount_pct as _default_working_discount,
    )

    deal_input = get_deal_input(deal_id) or {}
    commercial = deal_input.get("commercial", {})
    commitment = deal_input.get("consumption_commitment", {})
    ask = deal_input.get("negotiation_context", {}).get("customer_requested_commercial_terms", {})
    return [
        {
            "name": "customer_ask",
            "discount_pct": commercial.get("requested_discount_pct"),
            "contract_term_months": ask.get("requested_agreement_term_months", commercial.get("contract_term_months")),
            "commitment_amount": ask.get("requested_total_commitment", commitment.get("commitment_amount")),
            "payment_terms": ask.get("requested_payment_terms", commercial.get("payment_terms")),
            "price_protection": ask.get("requested_price_protection"),
        },
        {
            "name": "recommended_test",
            "discount_pct": _default_working_discount(deal_input),
            "contract_term_months": commercial.get("contract_term_months") or 36,
            "commitment_amount": commitment.get("commitment_amount"),
            "payment_terms": commercial.get("payment_terms"),
        },
        {
            "name": "commercial_floor_test",
            "discount_pct": _commercial_floor_discount(deal_input),
            "contract_term_months": commercial.get("contract_term_months") or 36,
            "commitment_amount": commitment.get("commitment_amount"),
            "payment_terms": commercial.get("payment_terms"),
        },
    ]


@app.route("/api/commercial-analysis/<deal_id>/policy", methods=["GET", "POST"])
def commercial_analysis_policy(deal_id):
    try:
        from tools.commercial_policy_engine import evaluate_commercial_policy

        overrides = request.get_json(silent=True) if request.method == "POST" else None
        if not overrides:
            overrides = _default_policy_scenarios_for_deal(deal_id)[0]
        return jsonify(evaluate_commercial_policy(deal_id, overrides))
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/policy/compare", methods=["POST"])
def commercial_analysis_policy_compare(deal_id):
    try:
        from tools.commercial_policy_engine import compare_scenario_policy

        payload = request.get_json(silent=True) or {}
        scenarios = payload.get("scenarios") or _default_policy_scenarios_for_deal(deal_id)
        return jsonify(compare_scenario_policy(deal_id, scenarios))
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/policy/trace", methods=["GET", "POST"])
def commercial_analysis_policy_trace(deal_id):
    try:
        from tools.commercial_policy_engine import get_policy_trace

        overrides = request.get_json(silent=True) if request.method == "POST" else None
        if not overrides:
            name = request.args.get("scenario", "customer_ask")
            scenarios = {s["name"]: s for s in _default_policy_scenarios_for_deal(deal_id)}
            overrides = scenarios.get(name, scenarios["customer_ask"])
        return jsonify(get_policy_trace(deal_id, overrides))
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/strategy", methods=["POST"])
def commercial_analysis_strategy(deal_id):
    try:
        from tools.commercial_strategy_agent import generate_commercial_strategy

        payload = request.get_json(silent=True) or {}
        result = generate_commercial_strategy(deal_id, working_scenario=payload.get("working_scenario"))
        return jsonify(result)
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/strategy/validate", methods=["POST"])
def commercial_analysis_strategy_validate(deal_id):
    try:
        from tools.commercial_strategy_agent import validate_commercial_strategy

        payload = request.get_json(silent=True) or {}
        proposed = payload.get("proposed_structure", payload)
        return jsonify(validate_commercial_strategy(deal_id, proposed))
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/comparables")
def commercial_analysis_comparables(deal_id):
    try:
        from tools.comparable_deal_intelligence import find_comparable_deals

        limit = request.args.get("limit", default=10, type=int)
        return jsonify(find_comparable_deals(deal_id, limit=limit))
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/comparables/benchmarks")
def commercial_analysis_comparable_benchmarks(deal_id):
    try:
        from tools.comparable_deal_intelligence import get_comparable_deal_benchmarks

        limit = request.args.get("limit", default=10, type=int)
        return jsonify(get_comparable_deal_benchmarks(deal_id, limit=limit))
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/comparables/evidence")
def commercial_analysis_comparable_evidence(deal_id):
    try:
        from tools.comparable_deal_intelligence import get_comparable_deal_evidence

        limit = request.args.get("limit", default=5, type=int)
        return jsonify(get_comparable_deal_evidence(deal_id, limit=limit))
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/comparables/search", methods=["POST"])
def commercial_analysis_comparable_search(deal_id):
    try:
        from tools.comparable_deal_intelligence import find_comparable_deals

        payload = request.get_json(silent=True) or {}
        return jsonify(find_comparable_deals(deal_id, limit=int(payload.get("limit", 10)), filters=payload.get("filters")))
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/comparables/<historical_deal_id>")
def commercial_analysis_comparable_detail(deal_id, historical_deal_id):
    try:
        from tools.comparable_deal_intelligence import explain_comparable_match

        return jsonify(explain_comparable_match(deal_id, historical_deal_id))
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/deal-package/validate", methods=["POST"])
def commercial_analysis_deal_package_validate(deal_id):
    try:
        from tools.deal_package_builder import validate_deal_package

        payload = request.get_json(silent=True) or {}
        return jsonify(validate_deal_package(deal_id, payload.get("scenario")))
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/deal-package", methods=["POST"])
def commercial_analysis_deal_package_generate(deal_id):
    try:
        from tools.deal_package_builder import generate_deal_package

        payload = request.get_json(silent=True) or {}
        llm_client = None
        if payload.get("use_ai"):
            from tools.commercial_strategy_agent import _call_with_fallback

            llm_client = _call_with_fallback
        package = generate_deal_package(
            deal_id,
            scenario=payload.get("scenario"),
            concessions=payload.get("concessions"),
            special_terms=payload.get("special_terms"),
            strategy=payload.get("strategy"),
            llm_client=llm_client,
        )
        return jsonify(package)
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/deal-packages")
def commercial_analysis_deal_packages(deal_id):
    try:
        from tools.deal_package_builder import get_package_manifest, list_deal_packages

        return jsonify({"deal_id": deal_id, "packages": [get_package_manifest(p) for p in list_deal_packages(deal_id)]})
    except Exception as exc:
        return _commercial_error_response(exc)


def _load_deal_package_or_404(deal_id, package_id):
    from tools.deal_package_builder import get_deal_package

    package = get_deal_package(deal_id, package_id)
    if not package:
        return None, (jsonify({"error": "MISSING_DEAL_PACKAGE", "message": f"No package {package_id} for {deal_id}"}), 404)
    return package, None


@app.route("/api/commercial-analysis/<deal_id>/deal-package/<package_id>")
def commercial_analysis_deal_package_get(deal_id, package_id):
    try:
        package, error = _load_deal_package_or_404(deal_id, package_id)
        return error if error else jsonify(package)
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/deal-package/<package_id>/manifest")
def commercial_analysis_deal_package_manifest(deal_id, package_id):
    try:
        from tools.deal_package_builder import get_package_manifest

        package, error = _load_deal_package_or_404(deal_id, package_id)
        return error if error else jsonify(get_package_manifest(package))
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/commercial-analysis/<deal_id>/deal-package/<package_id>/excel")
def commercial_analysis_deal_package_excel(deal_id, package_id):
    try:
        from tools.deal_package_builder import PACKAGES_DIR

        package, error = _load_deal_package_or_404(deal_id, package_id)
        if error:
            return error
        return send_file(
            PACKAGES_DIR / package["excel_file"],
            as_attachment=True,
            download_name=f"FinancialModel_{package_id}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        return _commercial_error_response(exc)


@app.route("/api/dashboard/data")
def dashboard_data():
    deals = _load_pipeline()
    if deals is None:
        return jsonify({"error": "meddpicc_pipeline.json not found",
                        "hint": "Expected at knowledge_base/meddpicc_pipeline.json"}), 404

    total = len(deals)
    total_value = sum(d.get("amount", 0) or 0 for d in deals)
    weighted_forecast = sum(
        (d.get("amount", 0) or 0) * (d.get("probability", 0) or 0) / 100
        for d in deals
    )
    avg_deal_size = total_value / total if total else 0
    avg_probability = (
        sum(d.get("probability", 0) or 0 for d in deals) / total if total else 0
    )

    # Score distribution
    score_buckets = {"STRONG": [], "NEEDS ATTENTION": [], "AT RISK": []}
    for d in deals:
        s = (d.get("overall_score") or "").strip().upper()
        if s in score_buckets:
            score_buckets[s].append(d.get("amount", 0) or 0)

    def _bucket(key):
        vals = score_buckets.get(key, [])
        count = len(vals)
        value = sum(vals)
        pct = round(count * 100 / total, 1) if total else 0
        return {"count": count, "value": value, "percentage": pct}

    score_distribution = {
        "strong":           _bucket("STRONG"),
        "needs_attention":  _bucket("NEEDS ATTENTION"),
        "at_risk":          _bucket("AT RISK"),
    }

    # Flag frequency
    flag_counts = {k: {"count": 0, "value": 0} for k in _FLAG_NAMES}
    for d in deals:
        for fk in _FLAG_NAMES:
            if d.get("flags", {}).get(fk):
                flag_counts[fk]["count"] += 1
                flag_counts[fk]["value"] += d.get("amount", 0) or 0

    flag_frequency = sorted([
        {
            "flag": _FLAG_NAMES[fk],
            "count": v["count"],
            "percentage": round(v["count"] * 100 / total, 1) if total else 0,
            "value_exposed": v["value"],
        }
        for fk, v in flag_counts.items()
    ], key=lambda x: x["count"], reverse=True)

    # Rep scorecard
    rep_map = {}
    for d in deals:
        rep = d.get("assigned_rep", "Unknown")
        if rep not in rep_map:
            rep_map[rep] = {
                "deals": [], "strong": 0, "needs_attention": 0, "at_risk": 0,
                "element_scores": {e: [] for e in _ELEMENT_ORDER},
            }
        r = rep_map[rep]
        r["deals"].append(d)
        s = (d.get("overall_score") or "").strip().upper()
        if s == "STRONG":
            r["strong"] += 1
        elif s == "NEEDS ATTENTION":
            r["needs_attention"] += 1
        elif s == "AT RISK":
            r["at_risk"] += 1
        for el in _ELEMENT_ORDER:
            st = (d.get("elements", {}).get(el) or {}).get("status", "unknown")
            r["element_scores"][el].append(_STATUS_SCORE.get(st, 0))

    def _coaching_priority(r):
        scores = {el: (sum(v) / len(v) if v else 0) for el, v in r["element_scores"].items()}
        worst = min(scores, key=scores.get)
        return _ELEMENT_LABELS.get(worst, worst)

    rep_scorecard = []
    for rep, r in rep_map.items():
        deal_count = len(r["deals"])
        tv = sum(d.get("amount", 0) or 0 for d in r["deals"])
        confirmed_per_deal = []
        for d in r["deals"]:
            elems = d.get("elements", {})
            confirmed = sum(
                1 for el in _ELEMENT_ORDER
                if (elems.get(el) or {}).get("status") == "confirmed"
            )
            confirmed_per_deal.append(confirmed / len(_ELEMENT_ORDER) * 100)
        meddpicc_rate = round(sum(confirmed_per_deal) / deal_count, 1) if deal_count else 0
        rep_scorecard.append({
            "rep":                     rep,
            "total_deals":             deal_count,
            "total_value":             tv,
            "strong_count":            r["strong"],
            "needs_attention_count":   r["needs_attention"],
            "at_risk_count":           r["at_risk"],
            "at_risk_percentage":      round(r["at_risk"] * 100 / deal_count, 1) if deal_count else 0,
            "meddpicc_completion_rate": meddpicc_rate,
            "coaching_priority":       _coaching_priority(r),
        })
    rep_scorecard.sort(key=lambda x: x["total_value"], reverse=True)

    # Product performance
    prod_map = {}
    for d in deals:
        prod = _extract_product(d.get("deal_name", ""))
        if prod not in prod_map:
            prod_map[prod] = {"deals": [], "at_risk": 0, "won": 0}
        prod_map[prod]["deals"].append(d)
        s = (d.get("overall_score") or "").strip().upper()
        if s == "AT RISK":
            prod_map[prod]["at_risk"] += 1
        if (d.get("stage") or "").upper() == "CUSTOMER":
            prod_map[prod]["won"] += 1

    def _most_common_gap(pdeal_list):
        gap_count = {}
        for d in pdeal_list:
            for el in _ELEMENT_ORDER:
                gap = (d.get("elements", {}).get(el) or {}).get("gap", "")
                if gap and len(gap) > 10:
                    key = _ELEMENT_LABELS.get(el, el)
                    gap_count[key] = gap_count.get(key, 0) + 1
        return max(gap_count, key=gap_count.get) if gap_count else "Unknown"

    product_performance = []
    for prod, pm in prod_map.items():
        dc = len(pm["deals"])
        tv = sum(d.get("amount", 0) or 0 for d in pm["deals"])
        product_performance.append({
            "product":           prod,
            "deal_count":        dc,
            "total_value":       tv,
            "at_risk_percentage": round(pm["at_risk"] * 100 / dc, 1) if dc else 0,
            "win_rate":          round(pm["won"] * 100 / dc, 1) if dc else 0,
            "most_common_gap":   _most_common_gap(pm["deals"]),
        })
    product_performance.sort(key=lambda x: x["at_risk_percentage"], reverse=True)

    # MEDDPICC heatmap — avg score per rep × element
    meddpicc_heatmap = []
    for rep, r in rep_map.items():
        for el in _ELEMENT_ORDER:
            scores = r["element_scores"][el]
            avg = round(sum(scores) / len(scores), 2) if scores else 0
            confirmed_rate = round(
                sum(1 for s in scores if s == 3) * 100 / len(scores), 1
            ) if scores else 0
            meddpicc_heatmap.append({
                "rep":            rep,
                "element":        _ELEMENT_LABELS[el],
                "avg_score":      avg,
                "confirmed_rate": confirmed_rate,
            })

    return jsonify({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pipeline_overview": {
            "total_deals":        total,
            "total_value":        total_value,
            "weighted_forecast":  round(weighted_forecast),
            "avg_deal_size":      round(avg_deal_size),
            "avg_probability":    round(avg_probability, 1),
        },
        "score_distribution":  score_distribution,
        "flag_frequency":      flag_frequency,
        "rep_scorecard":       rep_scorecard,
        "product_performance": product_performance,
        "meddpicc_heatmap":    meddpicc_heatmap,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard v2 — Three-View Support
# ─────────────────────────────────────────────────────────────────────────────

_REP_LOADED_COMP = {}  # Supply operator-owned inputs at runtime.

_BENCHMARK_CHURN = {"Enterprise": 0.07, "Mid-Market": 0.12, "SMB": 0.22}
_RESOURCE_PIPELINE_JSON = os.path.join(BASE_DIR, "knowledge_base", "resource_estimates", "resource_pipeline.json")
_PERSONAS_JSON = os.path.join(BASE_DIR, "knowledge_base", "personas", "personas.json")
_ENABLEMENT_JSON = os.path.join(BASE_DIR, "knowledge_base", "enablement_data.json")
_CUSTOMER_HEALTH_JSON = os.path.join(BASE_DIR, "knowledge_base", "customer_health_data.json")
_GTM_FEEDBACK_HISTORY_JSON = os.path.join(BASE_DIR, "knowledge_base", "gtm_feedback_history.json")
_RESOURCE_ESTIMATES_DIR = os.path.join(BASE_DIR, "knowledge_base", "resource_estimates")


def _get_segment(amount: float) -> str:
    if amount > 500_000:
        return "Enterprise"
    if amount > 100_000:
        return "Mid-Market"
    return "SMB"


def _load_resource_pipeline_data():
    if not os.path.exists(_RESOURCE_PIPELINE_JSON):
        return []
    with open(_RESOURCE_PIPELINE_JSON) as f:
        return json.load(f)


def _load_personas_data():
    if not os.path.exists(_PERSONAS_JSON):
        return []
    with open(_PERSONAS_JSON) as f:
        return json.load(f)


def _load_enablement_data():
    if not os.path.exists(_ENABLEMENT_JSON):
        return []
    with open(_ENABLEMENT_JSON) as f:
        return json.load(f)


def _load_customer_health_data():
    if not os.path.exists(_CUSTOMER_HEALTH_JSON):
        return {}
    with open(_CUSTOMER_HEALTH_JSON) as f:
        return json.load(f)


def _load_gtm_feedback_history():
    if not os.path.exists(_GTM_FEEDBACK_HISTORY_JSON):
        return {}
    with open(_GTM_FEEDBACK_HISTORY_JSON) as f:
        return json.load(f)


def _customer_health_records() -> list:
    data = _load_customer_health_data()
    return data.get("customers", []) if isinstance(data, dict) else []


def _customer_health_summary(company_name: str) -> str:
    if not company_name:
        return ""
    records = _customer_health_records()
    target = company_name.strip().lower()
    rec = next((r for r in records if (r.get("account_info", {}).get("account_name", "")).lower() == target), None)
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


def _load_resource_estimate(company_slug: str, deal_slug: str) -> dict:
    path = os.path.join(_RESOURCE_ESTIMATES_DIR, f"{company_slug}_{deal_slug}.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _days_since(iso_str: str) -> int:
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return max(0, (now - dt).days)
    except Exception:
        return 0


def _active_flag_names(deal: dict) -> list:
    flags = deal.get("flags") or {}
    return [_FLAG_NAMES[k] for k, v in flags.items() if v and k in _FLAG_NAMES]


def _enrich_deals(deals: list, resource_map: dict) -> list:
    out = []
    for d in deals:
        deal_id = d.get("deal_id", "")
        out.append({
            **d,
            "product":          resource_map.get(deal_id) or _extract_product(d.get("deal_name", "")),
            "days_in_pipeline": _days_since(d.get("scored_at", "")),
            "active_flags":     _active_flag_names(d),
        })
    return out


def _overall_stats(deals: list) -> dict:
    total = len(deals)
    total_value = sum(d.get("amount", 0) or 0 for d in deals)
    weighted = sum((d.get("amount", 0) or 0) * (d.get("probability", 0) or 0) / 100 for d in deals)
    avg_size = total_value / total if total else 0
    avg_prob = sum(d.get("probability", 0) or 0 for d in deals) / total if total else 0
    avg_days = sum(d.get("days_in_pipeline", 0) for d in deals) / total if total else 0

    buckets = {"STRONG": [], "NEEDS ATTENTION": [], "AT RISK": []}
    for d in deals:
        s = (d.get("overall_score") or "").strip().upper()
        if s in buckets:
            buckets[s].append(d.get("amount", 0) or 0)

    def _bkt(key):
        vals = buckets.get(key, [])
        c = len(vals)
        return {"count": c, "value": sum(vals), "percentage": round(c * 100 / total, 1) if total else 0}

    return {
        "pipeline_overview": {
            "total_deals":        total,
            "total_value":        total_value,
            "weighted_forecast":  round(weighted),
            "avg_deal_size":      round(avg_size),
            "avg_probability":    round(avg_prob, 1),
            "avg_days_in_pipeline": round(avg_days),
        },
        "score_distribution": {
            "strong":          _bkt("STRONG"),
            "needs_attention": _bkt("NEEDS ATTENTION"),
            "at_risk":         _bkt("AT RISK"),
        },
    }


def _flag_frequency(deals: list) -> list:
    total = len(deals)
    fc = {k: {"count": 0, "value": 0} for k in _FLAG_NAMES}
    for d in deals:
        for fk in _FLAG_NAMES:
            if (d.get("flags") or {}).get(fk):
                fc[fk]["count"] += 1
                fc[fk]["value"] += d.get("amount", 0) or 0
    return sorted([
        {"flag": _FLAG_NAMES[fk], "count": v["count"],
         "percentage": round(v["count"] * 100 / total, 1) if total else 0,
         "value_exposed": v["value"]}
        for fk, v in fc.items()
    ], key=lambda x: x["count"], reverse=True)


def _rep_scorecard(deals: list) -> list:
    rep_map = {}
    for d in deals:
        rep = d.get("assigned_rep", "Unknown")
        if rep not in rep_map:
            rep_map[rep] = {"deals": [], "strong": 0, "needs": 0, "risk": 0,
                            "elem": {e: [] for e in _ELEMENT_ORDER}}
        r = rep_map[rep]
        r["deals"].append(d)
        s = (d.get("overall_score") or "").upper()
        if "STRONG" in s:  r["strong"] += 1
        elif "NEEDS" in s: r["needs"]  += 1
        elif "RISK" in s:  r["risk"]   += 1
        for el in _ELEMENT_ORDER:
            st = (d.get("elements", {}).get(el) or {}).get("status", "unknown")
            r["elem"][el].append(_STATUS_SCORE.get(st, 0))

    out = []
    for rep, r in rep_map.items():
        dc = len(r["deals"])
        tv = sum(d.get("amount", 0) or 0 for d in r["deals"])
        cpd = [sum(1 for el in _ELEMENT_ORDER if (d.get("elements", {}).get(el) or {}).get("status") == "confirmed")
               / len(_ELEMENT_ORDER) * 100 for d in r["deals"]]
        mrate = round(sum(cpd) / dc, 1) if dc else 0
        scores = {el: (sum(v) / len(v) if v else 0) for el, v in r["elem"].items()}
        worst = min(scores, key=scores.get)
        best  = max(scores, key=scores.get)
        out.append({
            "rep":                     rep,
            "total_deals":             dc,
            "total_value":             tv,
            "strong_count":            r["strong"],
            "needs_attention_count":   r["needs"],
            "at_risk_count":           r["risk"],
            "at_risk_percentage":      round(r["risk"] * 100 / dc, 1) if dc else 0,
            "meddpicc_completion_rate": mrate,
            "coaching_priority":       _ELEMENT_LABELS.get(worst, worst),
            "strength_element":        _ELEMENT_LABELS.get(best, best),
        })
    out.sort(key=lambda x: x["total_value"], reverse=True)
    return out


def _product_perf(deals: list) -> list:
    pm = {}
    for d in deals:
        prod = d.get("product") or _extract_product(d.get("deal_name", ""))
        if prod not in pm:
            pm[prod] = {"deals": [], "risk": 0, "won": 0}
        pm[prod]["deals"].append(d)
        s = (d.get("overall_score") or "").upper()
        if "RISK" in s: pm[prod]["risk"] += 1
        if (d.get("stage") or "").upper() == "CUSTOMER": pm[prod]["won"] += 1
    out = []
    for prod, p in pm.items():
        dc = len(p["deals"])
        tv = sum(d.get("amount", 0) or 0 for d in p["deals"])
        gc = {}
        for d in p["deals"]:
            for el in _ELEMENT_ORDER:
                gap = (d.get("elements", {}).get(el) or {}).get("gap", "")
                if gap and len(gap) > 10:
                    k = _ELEMENT_LABELS.get(el, el)
                    gc[k] = gc.get(k, 0) + 1
        out.append({"product": prod, "deal_count": dc, "total_value": tv,
                    "at_risk_percentage": round(p["risk"] * 100 / dc, 1) if dc else 0,
                    "win_rate": round(p["won"] * 100 / dc, 1) if dc else 0,
                    "most_common_gap": max(gc, key=gc.get) if gc else "Unknown"})
    out.sort(key=lambda x: x["at_risk_percentage"], reverse=True)
    return out


def _meddpicc_heatmap(deals: list) -> list:
    rm = {}
    for d in deals:
        rep = d.get("assigned_rep", "Unknown")
        if rep not in rm:
            rm[rep] = {e: [] for e in _ELEMENT_ORDER}
        for el in _ELEMENT_ORDER:
            st = (d.get("elements", {}).get(el) or {}).get("status", "unknown")
            rm[rep][el].append(_STATUS_SCORE.get(st, 0))
    out = []
    for rep, es in rm.items():
        for el in _ELEMENT_ORDER:
            v = es[el]
            out.append({"rep": rep, "element": _ELEMENT_LABELS[el],
                        "avg_score": round(sum(v) / len(v), 2) if v else 0})
    return out


def _enablement_services(resource_estimate: dict) -> list:
    services = []
    for item in resource_estimate.get("additional_services") or []:
        if item.get("service"):
            services.append(item["service"])
    for field in ("product", "base_category", "implementation_pattern"):
        if resource_estimate.get(field):
            services.append(resource_estimate[field])
    return list(dict.fromkeys(services))


def _customer_health_portfolio_trend(records: list) -> list:
    buckets = {}
    for rec in records:
        for snap in rec.get("monthly_health_trend", []):
            key = snap.get("month_label")
            if not key:
                continue
            item = buckets.setdefault(key, {
                "month_label": key,
                "month_index": snap.get("month_index", 0),
                "health_scores": [],
                "churn_scores": [],
                "coverage": [],
                "arr_at_risk": 0,
                "open_actions": 0,
                "high_flags": 0,
                "adoption_impacts": [],
            })
            item["health_scores"].append(float(snap.get("health_score") or 0))
            item["churn_scores"].append(float(snap.get("churn_risk_score") or 0))
            item["coverage"].append(float(snap.get("enablement_coverage_pct") or 0))
            item["arr_at_risk"] += float(snap.get("arr_at_risk") or 0)
            item["open_actions"] += int(snap.get("open_action_count") or 0)
            item["high_flags"] += int(snap.get("high_severity_flag_count") or 0)
            item["adoption_impacts"].append(float(snap.get("adoption_impact_pct") or 0))

    out = []
    for item in sorted(buckets.values(), key=lambda x: x["month_index"]):
        out.append({
            "month_label": item["month_label"],
            "month_index": item["month_index"],
            "avg_health_score": round(sum(item["health_scores"]) / len(item["health_scores"]), 1) if item["health_scores"] else 0,
            "avg_churn_risk_score": round(sum(item["churn_scores"]) / len(item["churn_scores"]), 1) if item["churn_scores"] else 0,
            "avg_enablement_coverage_pct": round(sum(item["coverage"]) / len(item["coverage"]), 1) if item["coverage"] else 0,
            "arr_at_risk": round(item["arr_at_risk"]),
            "open_action_count": item["open_actions"],
            "high_severity_flag_count": item["high_flags"],
            "avg_adoption_impact_pct": round(sum(item["adoption_impacts"]) / len(item["adoption_impacts"]), 1) if item["adoption_impacts"] else 0,
        })
    return out


def _gtm_history_portfolio_summary(data: dict) -> dict:
    accounts = data.get("accounts", []) if isinstance(data, dict) else []
    recs = data.get("recommendations", []) if isinstance(data, dict) else []
    segment_counts = {}
    movement_counts = {}
    journey_counts = {}
    priority_accounts = []
    totals_by_month = {}

    for account in accounts:
        monthly = account.get("monthly_observations") or []
        if not monthly:
            continue
        jan = monthly[0]
        jul = monthly[-1]
        segment = jul.get("segment", "Unknown")
        segment_counts[segment] = segment_counts.get(segment, 0) + 1
        movement = f"{jan.get('segment', 'Unknown')} -> {segment}"
        movement_counts[movement] = movement_counts.get(movement, 0) + 1
        journey = account.get("journey_type", "unknown")
        journey_counts[journey] = journey_counts.get(journey, 0) + 1

        for row in monthly:
            bucket = totals_by_month.setdefault(row.get("month_label"), {
                "month": row.get("month"),
                "month_label": row.get("month_label"),
                "accounts": 0,
                "health": [],
                "coverage": [],
                "active_rate": [],
                "consumption": 0,
                "support_escalations": 0,
                "pipeline_value": 0,
            })
            bucket["accounts"] += 1
            bucket["health"].append(float(row.get("account_health_score") or 0))
            bucket["coverage"].append(float(row.get("enablement_coverage_pct") or 0))
            bucket["active_rate"].append(float(row.get("active_user_rate_pct") or 0))
            bucket["consumption"] += float(row.get("cloud_platform_consumption") or 0)
            bucket["support_escalations"] += int(row.get("support_escalation_count") or 0)
            bucket["pipeline_value"] += float(row.get("pipeline_value") or 0)

        if (
            jul.get("segment") in {"High Consumption / Low Training", "Low Consumption / Low Training"}
            or float(jul.get("churn_risk_score") or 0) >= 65
            or int(jul.get("days_to_renewal") or 999) <= 90
        ):
            priority_accounts.append({
                "account_slug": account.get("account_slug"),
                "account_name": account.get("account_name"),
                "primary_rep": account.get("primary_rep"),
                "journey_type": account.get("journey_type"),
                "july_segment": jul.get("segment"),
                "health_score": jul.get("account_health_score"),
                "churn_risk_score": jul.get("churn_risk_score"),
                "days_to_renewal": jul.get("days_to_renewal"),
                "consumption": jul.get("cloud_platform_consumption"),
                "coverage_pct": jul.get("enablement_coverage_pct"),
                "outcome": (account.get("journey_summary") or {}).get("outcome"),
            })

    response_counts = {"Accepted": 0, "Modified": 0, "Rejected": 0}
    execution_counts = {}
    rejection_reasons = {}
    for rec in recs:
        response = rec.get("seller_response", "Unknown")
        if response in response_counts:
            response_counts[response] += 1
        status = rec.get("execution_status", "Unknown")
        execution_counts[status] = execution_counts.get(status, 0) + 1
        if response == "Rejected":
            reason = rec.get("seller_response_reason", "Unknown")
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

    monthly_trend = []
    for item in sorted(totals_by_month.values(), key=lambda x: x.get("month") or ""):
        n = item["accounts"] or 1
        monthly_trend.append({
            "month": item["month"],
            "month_label": item["month_label"],
            "accounts": item["accounts"],
            "avg_health_score": round(sum(item["health"]) / n, 1),
            "avg_enablement_coverage_pct": round(sum(item["coverage"]) / n, 1),
            "avg_active_user_rate_pct": round(sum(item["active_rate"]) / n, 1),
            "total_consumption": round(item["consumption"]),
            "support_escalation_count": item["support_escalations"],
            "pipeline_value": round(item["pipeline_value"]),
        })

    priority_accounts.sort(key=lambda x: (
        x.get("health_score") or 100,
        x.get("days_to_renewal") or 999,
        -(x.get("consumption") or 0),
    ))
    return {
        "historical_period": data.get("historical_period", {}),
        "portfolio": {
            "accounts": len(accounts),
            "recommendations": len(recs),
            "segment_counts_july": segment_counts,
            "journey_counts": journey_counts,
        },
        "monthly_trend": monthly_trend,
        "segment_movements": sorted(
            [{"movement": k, "count": v} for k, v in movement_counts.items()],
            key=lambda x: x["count"],
            reverse=True,
        )[:20],
        "recommendation_feedback": {
            "seller_response_counts": response_counts,
            "execution_status_counts": execution_counts,
            "rejection_reasons": rejection_reasons,
        },
        "july_priority_accounts": priority_accounts[:30],
    }


def _gtm_rule_recommendation(account: dict) -> dict:
    monthly = account.get("monthly_observations") or []
    latest = monthly[-1] if monthly else {}
    summary = account.get("journey_summary") or {}
    segment = latest.get("segment", "")
    coverage_delta = float(summary.get("coverage_delta") or 0)
    consumption_delta = float(summary.get("consumption_delta") or 0)
    active_delta = float(summary.get("active_user_delta") or 0)
    prior = account.get("recommendations") or []
    prior_text = " ".join(
        f"{r.get('seller_response', '')} {r.get('seller_response_reason', '')} {r.get('outcome', '')}"
        for r in prior
    )
    if (
        segment == "Low Consumption / High Training"
        or (coverage_delta > 20 and consumption_delta < 25_000)
        or "technical" in prior_text.lower()
    ):
        return {
            "priority": "High" if float(latest.get("account_health_score") or 0) < 55 else "Medium",
            "signal": "Training is not converting into platform adoption.",
            "intervention_family": "Adoption",
            "recommended_intervention": "Technical adoption review",
            "target_persona": "Solutions Architect / Platform Owner",
            "why_this_account": "Training coverage improved, but consumption and active usage did not move proportionally. The likely constraint is technical, implementation, use-case, or organizational rather than another generic training gap.",
            "why_now": "July signals show adoption conversion risk that should be diagnosed before the next account review.",
            "objective": "Identify why trained users are not converting skills into production adoption and unblock the next workload.",
            "confidence": 0.78,
            "evidence": [
                f"July segment: {segment}",
                f"Coverage delta: {coverage_delta} points",
                f"Consumption delta: ${consumption_delta:,.0f}",
            ],
            "avoid_repeating": "Do not recommend more training until technical and use-case blockers are inspected.",
        }
    if segment == "High Consumption / Low Training":
        return {
            "priority": "High",
            "signal": "High consumption with low trained-user coverage.",
            "intervention_family": "Enablement",
            "recommended_intervention": "Persona-specific enablement and champion development",
            "target_persona": "Data Engineer / Platform Owner",
            "why_this_account": "The account is commercially active, but usage depends on a limited trained population. That creates adoption concentration and renewal protection risk.",
            "why_now": "July consumption is high while training coverage remains low, so the team should broaden skilled usage before risk accumulates.",
            "objective": "Broaden adoption, reduce concentration risk, and create customer champions around active workloads.",
            "confidence": 0.82,
            "evidence": [
                f"July segment: {segment}",
                f"July consumption: ${float(latest.get('cloud_platform_consumption') or 0):,.0f}",
                f"Training coverage: {latest.get('enablement_coverage_pct')}%",
            ],
            "avoid_repeating": "Use prior seller feedback to adjust persona, timing, and scope.",
        }
    if segment == "Low Consumption / Low Training":
        return {
            "priority": "High" if float(latest.get("churn_risk_score") or 0) >= 65 else "Medium",
            "signal": "Low consumption and low enablement coverage.",
            "intervention_family": "Partner",
            "recommended_intervention": "Account investigation with foundational enablement sequence",
            "target_persona": "CSM / Partner Lead",
            "why_this_account": "The account lacks both usage momentum and trained-user coverage, so the correct first move is diagnosis before committing to a single enablement motion.",
            "why_now": "July status shows account risk with limited adoption evidence.",
            "objective": "Find the account blocker, activate one concrete workload, and attach enablement only where it supports that workload.",
            "confidence": 0.74,
            "evidence": [f"July segment: {segment}", f"Health: {latest.get('account_health_score')}", f"Coverage: {latest.get('enablement_coverage_pct')}%"],
            "avoid_repeating": "Do not treat this as training-only until account ownership and workload fit are clear.",
        }
    return {
        "priority": "Medium",
        "signal": "Healthy adoption pattern with potential next commercial step.",
        "intervention_family": "Commercial",
        "recommended_intervention": "Expansion readiness review",
        "target_persona": "Economic Buyer / Champion",
        "why_this_account": "The account has comparatively strong consumption and training coverage, so the next action should focus on advanced value and expansion readiness.",
        "why_now": "July state indicates room to convert adoption health into a qualified commercial motion.",
        "objective": "Validate expansion fit and identify the next workload or stakeholder group.",
        "confidence": 0.72,
        "evidence": [f"July segment: {segment}", f"Active-user delta: {active_delta}", f"Consumption delta: ${consumption_delta:,.0f}"],
        "avoid_repeating": "Avoid basic training unless a new persona gap appears.",
    }


def _gtm_extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        pass
    match = _re.search(r"\{.*\}", text or "", flags=_re.S)
    if not match:
        raise ValueError("No JSON object found in LLM response")
    return json.loads(match.group(0))


def _gtm_account_context(account_slug: str, data: dict) -> dict | None:
    account = next((a for a in data.get("accounts", []) if a.get("account_slug") == account_slug), None)
    if not account:
        return None
    rec_ids = set(account.get("recommendation_ids") or [])
    return {
        **account,
        "recommendations": [
            r for r in data.get("recommendations", [])
            if r.get("recommendation_id") in rec_ids or r.get("account_slug") == account_slug
        ],
    }


def _gtm_compact_account(account: dict) -> dict:
    monthly = account.get("monthly_observations") or []
    compact_monthly = [{
        "month": m.get("month_label"),
        "segment": m.get("segment"),
        "trained_users": m.get("trained_users"),
        "active_users": m.get("active_users"),
        "coverage_pct": m.get("enablement_coverage_pct"),
        "consumption": m.get("cloud_platform_consumption"),
        "health": m.get("account_health_score"),
        "support_escalations": m.get("support_escalation_count"),
        "days_to_renewal": m.get("days_to_renewal"),
        "stage": m.get("opportunity_stage"),
        "observation": m.get("observation"),
    } for m in monthly]
    prior_recs = [{
        "date": r.get("date"),
        "trigger": r.get("trigger"),
        "recommended_action": r.get("recommended_action"),
        "target_persona": r.get("target_persona"),
        "seller_response": r.get("seller_response"),
        "seller_response_reason": r.get("seller_response_reason"),
        "execution_status": r.get("execution_status"),
        "outcome": r.get("outcome"),
    } for r in account.get("recommendations", [])]
    return {
        "account_name": account.get("account_name"),
        "primary_rep": account.get("primary_rep"),
        "primary_product": account.get("primary_product"),
        "journey_type": account.get("journey_type"),
        "journey_summary": account.get("journey_summary"),
        "monthly_observations": compact_monthly,
        "previous_interventions": prior_recs,
    }


def _gtm_fallback_plan(account: dict, intervention: dict | None = None) -> dict:
    rec = intervention or _gtm_rule_recommendation(account)
    family = rec.get("intervention_family", "Adoption")
    action = rec.get("recommended_intervention", "Targeted account intervention")
    target = rec.get("target_persona", "Account team")
    return {
        "analysis_source": "fallback_plan",
        "model": None,
        "plan_goal": rec.get("objective", "Move the account to the next measurable adoption or commercial outcome."),
        "sequence_rationale": "Start with diagnosis, then execute the smallest intervention that can move a measurable account signal.",
        "action_plan": [
            {"step": "Confirm account signal", "owner": "AE / CSM", "timing": "Week 1", "details": "Review July state, January-July trend, prior seller feedback, and the customer's active workload context."},
            {"step": action, "owner": target, "timing": "Weeks 1-2", "details": f"Run the {family.lower()} motion with a named customer owner and one explicit success measure."},
            {"step": "Measure post-intervention signals", "owner": "CSM", "timing": "Weeks 3-4", "details": "Track coverage, active users, consumption, support escalations, and opportunity movement without claiming causation."},
        ],
        "success_measures": [
            {"metric": "Active users", "target": "Positive movement within 30 days"},
            {"metric": "Consumption", "target": "Stabilize or improve trend"},
            {"metric": "Account-team feedback", "target": "Accept, modify, or reject captured with rationale"},
        ],
        "risks_to_watch": ["Wrong stakeholder", "Technical blocker", "Timing conflict"],
    }


def _gtm_fallback_trend_analysis(account: dict) -> dict:
    monthly = account.get("monthly_observations") or []
    jan = monthly[0] if monthly else {}
    jul = monthly[-1] if monthly else {}
    summary = account.get("journey_summary") or {}
    return {
        "analysis_source": "fallback_analysis",
        "model": None,
        "summary": (
            f"{account.get('account_name')} moved from {jan.get('segment', 'unknown')} to "
            f"{jul.get('segment', 'unknown')} between January and July. Health changed by "
            f"{summary.get('health_delta', 0)} points, active users changed by "
            f"{summary.get('active_user_delta', 0)}, and consumption changed by "
            f"${float(summary.get('consumption_delta') or 0):,.0f}."
        ),
        "attention_reason": "Review the account because July segment and January-July trajectory show a GTM action point.",
        "drop_rebound_explanation": "Inspect month-to-month active user and consumption changes around intervention markers before assuming causation.",
        "likely_constraint": "Needs LLM review to distinguish enablement, adoption, technical, commercial, or partner constraint.",
        "intervention_read": "Historical recommendations are context for later behavior, not proof of causal impact.",
        "key_observations": [
            f"January segment: {jan.get('segment', 'unknown')}",
            f"July segment: {jul.get('segment', 'unknown')}",
            f"Latest health: {jul.get('account_health_score', 'unknown')}",
        ],
    }


@app.route("/api/gtm-feedback/history")
def gtm_feedback_history():
    data = _load_gtm_feedback_history()
    if not data:
        return jsonify({"error": "gtm_feedback_history.json not found. Run generate_gtm_feedback_history.py"}), 404
    return jsonify(_gtm_history_portfolio_summary(data))


@app.route("/api/gtm-feedback/history/accounts")
def gtm_feedback_accounts():
    data = _load_gtm_feedback_history()
    if not data:
        return jsonify({"error": "gtm_feedback_history.json not found. Run generate_gtm_feedback_history.py"}), 404
    accounts = data.get("accounts", [])
    rows = []
    for account in accounts:
        monthly = account.get("monthly_observations") or []
        latest = monthly[-1] if monthly else {}
        rows.append({
            "account_slug": account.get("account_slug"),
            "account_name": account.get("account_name"),
            "primary_rep": account.get("primary_rep"),
            "primary_product": account.get("primary_product"),
            "journey_type": account.get("journey_type"),
            "july_segment": latest.get("segment"),
            "health_score": latest.get("account_health_score"),
            "coverage_pct": latest.get("enablement_coverage_pct"),
            "active_users": latest.get("active_users"),
            "consumption": latest.get("cloud_platform_consumption"),
            "recommendation_count": len(account.get("recommendation_ids") or []),
            "journey_summary": account.get("journey_summary"),
        })
    return jsonify({
        "generated_at": data.get("generated_at"),
        "historical_period": data.get("historical_period", {}),
        "accounts": rows,
    })


@app.route("/api/gtm-feedback/history/accounts/<account_slug>")
def gtm_feedback_account_detail(account_slug):
    data = _load_gtm_feedback_history()
    if not data:
        return jsonify({"error": "gtm_feedback_history.json not found. Run generate_gtm_feedback_history.py"}), 404
    account = next((a for a in data.get("accounts", []) if a.get("account_slug") == account_slug), None)
    if not account:
        return jsonify({"error": f"GTM feedback history not found: {account_slug}"}), 404
    rec_ids = set(account.get("recommendation_ids") or [])
    recs = [r for r in data.get("recommendations", []) if r.get("recommendation_id") in rec_ids or r.get("account_slug") == account_slug]
    return jsonify({
        **account,
        "recommendations": recs,
        "historical_period": data.get("historical_period", {}),
    })


@app.route("/api/gtm-feedback/analyze/<account_slug>", methods=["POST", "GET"])
def gtm_feedback_analyze_account(account_slug):
    data = _load_gtm_feedback_history()
    if not data:
        return jsonify({"error": "gtm_feedback_history.json not found. Run generate_gtm_feedback_history.py"}), 404
    account = _gtm_account_context(account_slug, data)
    if not account:
        return jsonify({"error": f"GTM feedback history not found: {account_slug}"}), 404
    fallback = _gtm_rule_recommendation(account)
    if not TOGETHER_API_KEY:
        return jsonify({**fallback, "analysis_source": "fallback_rules", "model": None})

    prompt = f"""
You are a GTM Intelligence analyst for a B2B SaaS / cloud platform company.

Analyze this account's January-July trend and recommend exactly ONE clean targeted next intervention.
The intervention must be specific, actionable, and account-appropriate.

Do not recommend training by default.
If training increased but consumption or active usage did not improve, recommend an adoption/technical/use-case intervention instead.
If a prior recommendation was rejected or modified, do not repeat the same action unless you explicitly adapt it to the seller feedback.
Do not claim causation. Use language like "following", "associated with", or "subsequent signals".

Return ONLY valid JSON with these exact keys:
{{
  "priority": "High|Medium|Low",
  "signal": "one concise current signal",
  "intervention_family": "Enablement|Adoption|Commercial|Partner",
  "recommended_intervention": "one clean action, 5-12 words",
  "target_persona": "specific stakeholder/persona",
  "why_this_account": "2 concise sentences using current state plus Jan-Jul trend",
  "why_now": "1 concise sentence",
  "objective": "1 concise sentence",
  "confidence": 0.0,
  "evidence": ["3 short evidence bullets grounded in the data"],
  "avoid_repeating": "what prior feedback means for this next action"
}}

Account:
{json.dumps(_gtm_compact_account(account), indent=2)}
"""
    try:
        raw = llm(prompt, max_tokens=750)
        parsed = _gtm_extract_json(raw)
        confidence = parsed.get("confidence", fallback["confidence"])
        try:
            confidence = float(confidence)
            if confidence > 1:
                confidence = confidence / 100
        except Exception:
            confidence = fallback["confidence"]
        parsed["confidence"] = round(max(0, min(1, confidence)), 2)
        parsed["analysis_source"] = "llm"
        parsed["model"] = TOGETHER_MODEL
        parsed["fallback_available"] = fallback
        return jsonify(parsed)
    except Exception as exc:
        return jsonify({**fallback, "analysis_source": "fallback_rules", "model": TOGETHER_MODEL, "llm_error": str(exc)})


@app.route("/api/gtm-feedback/trend-analysis/<account_slug>", methods=["POST", "GET"])
def gtm_feedback_trend_analysis(account_slug):
    data = _load_gtm_feedback_history()
    if not data:
        return jsonify({"error": "gtm_feedback_history.json not found. Run generate_gtm_feedback_history.py"}), 404
    account = _gtm_account_context(account_slug, data)
    if not account:
        return jsonify({"error": f"GTM feedback history not found: {account_slug}"}), 404
    fallback = _gtm_fallback_trend_analysis(account)
    if not TOGETHER_API_KEY:
        return jsonify(fallback)

    prompt = f"""
You are a GTM account analyst. Explain why this account deserves attention by reading the
January-July account journey, not by repeating generic risk language.

Focus on:
- Health and training trend lines.
- Active-user bars.
- Consumption bars.
- Sudden drops, rebounds, stalls, or mismatches.
- Whether intervention timing is merely associated with later behavior, not causal proof.
- Why a fall may have recovered, or why a recovery may still not be enough.

Return ONLY valid JSON with these exact keys:
{{
  "summary": "3 concise sentences explaining the trend story",
  "attention_reason": "one concrete reason this account needs or does not need attention now",
  "drop_rebound_explanation": "explain any sudden fall/rebound in active users or consumption; say if none is meaningful",
  "likely_constraint": "Enablement|Adoption|Technical|Commercial|Partner|Mixed plus short explanation",
  "intervention_read": "how to interpret historical interventions without claiming causation",
  "key_observations": ["4 short bullets grounded in specific months/metrics"]
}}

Account context:
{json.dumps(_gtm_compact_account(account), indent=2)}
"""
    try:
        raw = llm(prompt, max_tokens=850)
        parsed = _gtm_extract_json(raw)
        parsed["analysis_source"] = "llm"
        parsed["model"] = TOGETHER_MODEL
        parsed["fallback_available"] = fallback
        return jsonify(parsed)
    except Exception as exc:
        return jsonify({**fallback, "model": TOGETHER_MODEL, "llm_error": str(exc)})


@app.route("/api/gtm-feedback/plan/<account_slug>", methods=["POST", "GET"])
def gtm_feedback_plan_account(account_slug):
    data = _load_gtm_feedback_history()
    if not data:
        return jsonify({"error": "gtm_feedback_history.json not found. Run generate_gtm_feedback_history.py"}), 404
    account = _gtm_account_context(account_slug, data)
    if not account:
        return jsonify({"error": f"GTM feedback history not found: {account_slug}"}), 404
    body = request.json or {}
    intervention = body.get("intervention") if isinstance(body.get("intervention"), dict) else None
    trend_analysis = body.get("trend_analysis") if isinstance(body.get("trend_analysis"), dict) else None
    plan_inputs = str(body.get("plan_inputs") or "").strip()
    fallback = _gtm_fallback_plan(account, intervention)
    if not TOGETHER_API_KEY:
        return jsonify(fallback)

    prompt = f"""
You are a GTM intervention planner for a B2B SaaS / cloud platform account team.

Use the complete account context, January-July trend, generated trend analysis, seller plan inputs,
previous recommendations, seller feedback, previous outcomes, and the proposed intervention to
generate a practical execution plan.

The plan must be specific to the account. Do not create a generic training checklist.
If the trend suggests training is not the limiting factor, sequence technical/adoption diagnosis before enablement.
Do not claim causation. Use "following", "associated with", and "subsequent signals".
Treat seller plan inputs as important constraints. If they conflict with the proposed intervention,
adapt the plan while preserving the intervention objective.

Return ONLY valid JSON with these exact keys:
{{
  "plan_goal": "one sentence",
  "sequence_rationale": "2 concise sentences explaining the order of actions",
  "action_plan": [
    {{"step": "specific action", "owner": "role/persona", "timing": "when", "details": "what to do and what account context it uses"}}
  ],
  "success_measures": [
    {{"metric": "metric name", "target": "specific target or directional target"}}
  ],
  "risks_to_watch": ["risk 1", "risk 2", "risk 3"]
}}

Proposed intervention:
{json.dumps(intervention or _gtm_rule_recommendation(account), indent=2)}

Generated trend analysis:
{json.dumps(trend_analysis or _gtm_fallback_trend_analysis(account), indent=2)}

Seller plan inputs / constraints:
{plan_inputs or "No additional seller inputs provided."}

Account context:
{json.dumps(_gtm_compact_account(account), indent=2)}
"""
    try:
        raw = llm(prompt, max_tokens=1100)
        parsed = _gtm_extract_json(raw)
        parsed["analysis_source"] = "llm"
        parsed["model"] = TOGETHER_MODEL
        parsed["fallback_available"] = fallback
        return jsonify(parsed)
    except Exception as exc:
        return jsonify({**fallback, "model": TOGETHER_MODEL, "llm_error": str(exc)})


@app.route("/api/gtm-feedback/chat/<account_slug>", methods=["POST"])
def gtm_feedback_account_chat(account_slug):
    data = _load_gtm_feedback_history()
    if not data:
        return jsonify({"error": "gtm_feedback_history.json not found. Run generate_gtm_feedback_history.py"}), 404
    account = _gtm_account_context(account_slug, data)
    if not account:
        return jsonify({"error": f"GTM feedback history not found: {account_slug}"}), 404
    body = request.json or {}
    question = str(body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400
    if not TOGETHER_API_KEY:
        return jsonify({
            "answer": "LLM account chat is unavailable because TOGETHER_API_KEY is not configured.",
            "analysis_source": "unavailable",
            "model": None,
        })

    prompt = f"""
You are answering a seller/CSM question about one account in GTM Intelligence.

Use only the account context below. Be specific, concise, and grounded in January-July trend data,
previous recommendations, seller responses, and subsequent account behavior.
If the user asks about causation, do not claim causation; explain association and uncertainty.
If the answer depends on a month-to-month movement, name the relevant months and metrics.

Question:
{question}

Account context:
{json.dumps(_gtm_compact_account(account), indent=2)}

Return a direct answer in 1-3 short paragraphs. No markdown table.
"""
    try:
        answer = llm(prompt, max_tokens=650)
        return jsonify({"answer": answer, "analysis_source": "llm", "model": TOGETHER_MODEL})
    except Exception as exc:
        return jsonify({"answer": f"LLM account chat failed: {exc}", "analysis_source": "error", "model": TOGETHER_MODEL})


@app.route("/api/gtm-feedback/recommendations")
def gtm_feedback_recommendations():
    data = _load_gtm_feedback_history()
    if not data:
        return jsonify({"error": "gtm_feedback_history.json not found. Run generate_gtm_feedback_history.py"}), 404
    return jsonify({
        "generated_at": data.get("generated_at"),
        "historical_period": data.get("historical_period", {}),
        "recommendations": data.get("recommendations", []),
    })


@app.route("/api/gtm-feedback/feedback", methods=["POST"])
def gtm_feedback_capture():
    body = request.json or {}
    action = body.get("feedback_action")
    if action not in {"Accepted", "Modified", "Rejected"}:
        return jsonify({"error": "feedback_action must be Accepted, Modified, or Rejected"}), 400
    with sqlite3.connect(APPROVAL_DB_PATH) as con:
        con.execute(
            """INSERT INTO approval_audit
               (timestamp, deal_name, deal_value, current_level, role_name,
                ai_decision, policy_rule_triggered, action, actioned_by, notes, memo_file)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(),
                body.get("deal_name") or body.get("account_name") or body.get("account_slug", ""),
                0,
                "GTM_INTELLIGENCE",
                "Seller/CSM",
                action.lower(),
                "gtm_intervention_feedback",
                "gtm_intervention_feedback",
                body.get("actor", "field_user"),
                json.dumps({
                    "account_slug": body.get("account_slug"),
                    "account_name": body.get("account_name"),
                    "deal_id": body.get("deal_id"),
                    "deal_name": body.get("deal_name"),
                    "feedback_action": action,
                    "reason": body.get("reason", ""),
                }),
                "",
            ),
        )
    return jsonify({"status": "ok"})


@app.route("/api/dashboard/customer-health")
def dashboard_customer_health():
    data = _load_customer_health_data()
    records = data.get("customers", []) if isinstance(data, dict) else []
    if not records:
        return jsonify({"error": "customer_health_data.json not found. Run generate_customer_health_data.py"}), 404

    total = len(records)
    counts = {"green": 0, "amber": 0, "red": 0}
    for r in records:
        status = r.get("health", {}).get("status", "amber")
        counts[status] = counts.get(status, 0) + 1

    total_pipeline = sum(r.get("portfolio", {}).get("total_pipeline_value", 0) or 0 for r in records)
    arr_at_risk = sum(r.get("revenue", {}).get("arr_at_risk", 0) or 0 for r in records)
    avg_health = round(sum(r.get("health", {}).get("score", 0) or 0 for r in records) / total, 1) if total else 0
    avg_churn = round(sum(r.get("health", {}).get("churn_risk_score", 0) or 0 for r in records) / total, 1) if total else 0
    avg_coverage = round(sum(r.get("enablement", {}).get("coverage_pct", 0) or 0 for r in records) / total, 1) if total else 0

    stage_counts = {}
    trend_counts = {"improving": 0, "flat": 0, "declining": 0}
    for r in records:
        stage = r.get("program", {}).get("stage_name", "Unknown")
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        direction = r.get("trend_summary", {}).get("direction", "flat")
        trend_counts[direction] = trend_counts.get(direction, 0) + 1

    at_risk = sorted(records, key=lambda r: (
        r.get("health", {}).get("score", 0),
        -(r.get("revenue", {}).get("arr_at_risk", 0) or 0),
    ))[:20]

    feed = []
    for r in records:
        for flag in r.get("flags", [])[:3]:
            feed.append({
                "customer": r.get("account_info", {}).get("account_name"),
                "status": r.get("health", {}).get("status"),
                "health_score": r.get("health", {}).get("score"),
                "trend": r.get("trend_summary", {}).get("direction", "flat"),
                "health_delta": r.get("trend_summary", {}).get("health_delta", 0),
                "severity": flag.get("severity"),
                "reason": flag.get("reason"),
                "owner": flag.get("owner"),
                "recommended_action": flag.get("recommended_action"),
                "arr_at_risk": r.get("revenue", {}).get("arr_at_risk", 0),
            })
    feed.sort(key=lambda x: (
        0 if x.get("severity") == "HIGH" else 1,
        0 if x.get("status") == "red" else 1,
        -(x.get("arr_at_risk") or 0),
    ))

    return jsonify({
        "generated_at": data.get("generated_at"),
        "portfolio": {
            "customers": total,
            "green": counts.get("green", 0),
            "amber": counts.get("amber", 0),
            "red": counts.get("red", 0),
            "avg_health_score": avg_health,
            "avg_churn_risk_score": avg_churn,
            "avg_enablement_coverage_pct": avg_coverage,
            "total_pipeline_value": total_pipeline,
            "arr_at_risk": arr_at_risk,
        },
        "stage_counts": stage_counts,
        "trend_counts": trend_counts,
        "portfolio_trend": _customer_health_portfolio_trend(records),
        "at_risk_accounts": [{
            "customer": r.get("account_info", {}).get("account_name"),
            "segment": r.get("account_info", {}).get("segment"),
            "primary_rep": r.get("account_info", {}).get("primary_rep"),
            "health_score": r.get("health", {}).get("score"),
            "status": r.get("health", {}).get("status"),
            "trend": r.get("trend_summary", {}).get("direction", "flat"),
            "health_delta": r.get("trend_summary", {}).get("health_delta", 0),
            "churn_risk": r.get("health", {}).get("churn_risk"),
            "arr_at_risk": r.get("revenue", {}).get("arr_at_risk", 0),
            "deal_count": r.get("portfolio", {}).get("deal_count", 0),
            "top_action": (r.get("recommended_actions") or [""])[0],
        } for r in at_risk],
        "action_feed": feed[:30],
    })


@app.route("/api/dashboard/customer-health/<customer_slug>")
def dashboard_customer_health_detail(customer_slug):
    records = _customer_health_records()
    rec = next((r for r in records if r.get("customer_slug") == customer_slug), None)
    if not rec:
        return jsonify({"error": f"Customer health record not found: {customer_slug}"}), 404
    return jsonify(rec)


@app.route("/api/dashboard/enablement")
def dashboard_enablement():
    records = _load_enablement_data()
    if not records:
        return jsonify({"error": "enablement_data.json not found. Run generate_enablement_data.py"}), 404

    total_licensed = sum(int(r.get("licensed_users") or 0) for r in records)
    total_trained = sum(int(r.get("trained_users") or 0) for r in records)
    coverage = round(total_trained * 100 / total_licensed, 1) if total_licensed else 0
    avg_gap = round(sum(int(r.get("skills_gap_risk_score") or 0) for r in records) / len(records), 1)

    trigger_counts = {}
    pillar_counts = {"Performance": 0, "Proficiency": 0, "Productivity": 0}
    heatmap = {}
    lift_points = []
    feed = []
    critical_accounts = 0
    coverage_gaps = []
    consumption_at_risk_total = 0
    renewal_risk_accounts = 0

    for r in records:
        triggers = r.get("trigger_flags") or evaluate_enablement_triggers(r)
        for t in triggers:
            trigger_counts[t["code"]] = trigger_counts.get(t["code"], 0) + 1
        trigger_codes = {t.get("code") for t in triggers}
        severity = highest_severity(triggers)
        if severity == "CRITICAL":
            critical_accounts += 1
        if "RENEWAL_RISK" in trigger_codes:
            renewal_risk_accounts += 1

        coverage_pct = float(r.get("enablement_coverage_pct") or 0)
        risk_score = int(r.get("skills_gap_risk_score") or 0)
        consumption = float(r.get("azure_annual_consumption") or 0)
        coverage_gap = max(0.0, 70.0 - coverage_pct)
        coverage_gaps.append(coverage_gap)
        consumption_at_risk = round(consumption * max(risk_score / 100, min(coverage_gap / 70, 1)) * 0.35)
        consumption_at_risk_total += consumption_at_risk
        if severity in ("HIGH", "CRITICAL") or consumption_at_risk > 0:
            pillar_counts["Performance"] += 1
        if risk_score >= 50 or coverage_gap > 20:
            pillar_counts["Proficiency"] += 1
        if float(r.get("attendance_rate_pct") or 0) < 60 or int(r.get("days_inactive") or 0) > 30:
            pillar_counts["Productivity"] += 1

        resource = _load_resource_estimate(r.get("company_slug", ""), r.get("deal_slug", ""))
        services = _enablement_services(resource) or [r.get("deal_name", "Unknown")]
        for service in services[:4]:
            item = heatmap.setdefault(service, {"service": service, "accounts": 0, "risk_total": 0, "consumption": 0})
            item["accounts"] += 1
            item["risk_total"] += int(r.get("skills_gap_risk_score") or 0)
            item["consumption"] += float(r.get("azure_annual_consumption") or 0)

        lift = round((float(r.get("enablement_coverage_pct") or 0) - 25) * 0.42 + (100 - int(r.get("skills_gap_risk_score") or 0)) * 0.08, 1)
        lift_points.append({
            "company": r.get("company"),
            "coverage_pct": r.get("enablement_coverage_pct"),
            "consumption_lift_pct": max(-10, min(45, lift)),
            "trained_users": r.get("trained_users"),
            "licensed_users": r.get("licensed_users"),
        })

        if triggers:
            rec = build_enablement_recommendation(r, resource_estimate=resource, run_eval=False)
            feed.append({
                "company": r.get("company"),
                "deal_name": r.get("deal_name"),
                "company_slug": r.get("company_slug"),
                "deal_slug": r.get("deal_slug"),
                "severity": severity,
                "trigger_flags": [t["code"] for t in triggers],
                "confidence_score": round((100 - int(r.get("skills_gap_risk_score") or 0)) / 100, 2),
                "recommended_course_path": rec.get("recommended_course_path"),
                "cta_for_account_team": rec.get("cta_for_account_team"),
                "motion_type": (rec.get("free_to_paid_opportunity") or {}).get("motion_type"),
            })

    heatmap_rows = []
    for item in heatmap.values():
        heatmap_rows.append({
            **item,
            "avg_risk_score": round(item["risk_total"] / item["accounts"], 1) if item["accounts"] else 0,
        })
    heatmap_rows.sort(key=lambda x: (x["avg_risk_score"], x["consumption"]), reverse=True)
    feed.sort(key=lambda x: (0 if x["severity"] == "CRITICAL" else 1, -len(x["trigger_flags"])))
    feedback_counts = {"accepted": 0, "modified": 0, "rejected": 0, "total": 0}
    try:
        with sqlite3.connect(APPROVAL_DB_PATH) as con:
            rows = con.execute(
                "SELECT ai_decision, COUNT(*) FROM approval_audit WHERE action='enablement_feedback' GROUP BY ai_decision"
            ).fetchall()
        for decision, count in rows:
            key = str(decision or "").lower()
            if key in feedback_counts:
                feedback_counts[key] = int(count)
            feedback_counts["total"] += int(count)
    except Exception:
        pass

    return jsonify({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "portfolio": {
            "accounts": len(records),
            "licensed_users": total_licensed,
            "trained_users": total_trained,
            "enablement_coverage_pct": coverage,
            "avg_skills_gap_risk_score": avg_gap,
            "critical_accounts": critical_accounts,
            "avg_coverage_gap_pct": round(sum(coverage_gaps) / len(coverage_gaps), 1) if coverage_gaps else 0,
            "estimated_consumption_at_risk": round(consumption_at_risk_total),
            "renewal_risk_accounts": renewal_risk_accounts,
        },
        "trigger_counts": trigger_counts,
        "impact_pillar_risk_counts": pillar_counts,
        "feedback_counts": feedback_counts,
        "skills_gap_heatmap": heatmap_rows[:20],
        "training_consumption_lift": lift_points[:120],
        "ae_feed": feed[:25],
    })


@app.route("/api/enablement/recommend", methods=["POST"])
def enablement_recommend():
    if not _ENABLEMENT_AVAILABLE:
        return jsonify({"error": "Enablement engine unavailable"}), 500
    body = request.json or {}
    try:
        if body.get("feedback_action"):
            with sqlite3.connect(APPROVAL_DB_PATH) as con:
                con.execute(
                    """INSERT INTO approval_audit
                       (timestamp, deal_name, deal_value, current_level, role_name,
                        ai_decision, policy_rule_triggered, action, actioned_by, notes, memo_file)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        datetime.now().isoformat(),
                        body.get("deal_name") or body.get("deal_slug", ""),
                        0,
                        "ENABLEMENT",
                        "AE/CSM",
                        body.get("feedback_action"),
                        "enablement_recommendation_feedback",
                        "enablement_feedback",
                        body.get("actor", "field_user"),
                        json.dumps({
                            "feedback_action": body.get("feedback_action"),
                            "reason": body.get("reason", ""),
                            "company_slug": body.get("company_slug"),
                            "deal_slug": body.get("deal_slug"),
                        }),
                        "",
                    ),
                )
            return jsonify({"status": "ok"})

        rec = recommend_for_deal(
            company=body.get("company", ""),
            deal=body.get("deal_name", ""),
            company_slug=body.get("company_slug", ""),
            deal_slug=body.get("deal_slug", ""),
            pipeline_packet=body.get("pipeline_packet") or body.get("enablement_packet") or {},
        )
        rec = _enablement_llm_writer(rec, _safe_money_float(body.get("deal_amount") or body.get("amount")))
        return jsonify(rec)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/enablement/report", methods=["POST"])
def enablement_report():
    if not _ENABLEMENT_AVAILABLE:
        return jsonify({"error": "Enablement engine unavailable"}), 500
    body = request.get_json(silent=True) or {}
    try:
        recommendation = body.get("recommendation")
        if not isinstance(recommendation, dict) or not recommendation:
            recommendation = recommend_for_deal(
                company=body.get("company", ""),
                deal=body.get("deal_name", ""),
                company_slug=body.get("company_slug", ""),
                deal_slug=body.get("deal_slug", ""),
                pipeline_packet=body.get("pipeline_packet") or body.get("enablement_packet") or {},
            )

        recommendation = _enablement_hydrate_report_sections(
            recommendation,
            _safe_money_float(body.get("deal_amount") or body.get("amount")),
        )
        company = body.get("company") or body.get("company_slug") or "account"
        deal_name = body.get("deal_name") or body.get("deal_slug") or "deal"
        safe_company = _re.sub(r"[^a-zA-Z0-9_-]+", "_", str(company)).strip("_")[:50] or "account"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"enablement_report_{safe_company}_{timestamp}.docx"
        docx_bytes = _enablement_report_docx_bytes(recommendation, company=str(company), deal_name=str(deal_name))
        return send_file(
            docx_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        print(f"[enablement_report] failed: {type(e).__name__}: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/api/dashboard/overall")
def dashboard_overall():
    deals = _load_pipeline()
    if deals is None:
        return jsonify({"error": "meddpicc_pipeline.json not found"}), 404

    resource_map = {r["deal_id"]: r.get("product", "") for r in _load_resource_pipeline_data()}
    deals = _enrich_deals(deals, resource_map)

    stats = _overall_stats(deals)

    proposal = [d for d in deals if (d.get("stage") or "").upper() == "PROPOSAL"]
    proposal.sort(key=lambda d: (0 if "RISK" in (d.get("overall_score") or "").upper() else 1,
                                 -(d.get("amount") or 0)))
    closing_this_month = [{"deal_name": d["deal_name"], "company": d["company"],
                           "rep": d["assigned_rep"], "amount": d.get("amount", 0) or 0,
                           "score": d.get("overall_score", ""),
                           "days_in_pipeline": d.get("days_in_pipeline", 0)} for d in proposal[:15]]

    prio = [d for d in deals if d.get("next_priority")]
    prio.sort(key=lambda d: (0 if "RISK" in (d.get("overall_score") or "").upper() else 1,
                              -len(d.get("active_flags", [])), -(d.get("amount") or 0)))
    top_priorities = [{"deal_name": d["deal_name"], "company": d["company"],
                       "rep": d["assigned_rep"], "score": d.get("overall_score", ""),
                       "next_priority": d.get("next_priority", ""),
                       "days_in_pipeline": d.get("days_in_pipeline", 0)} for d in prio[:5]]

    return jsonify({
        "generated_at":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **stats,
        "flag_frequency":      _flag_frequency(deals),
        "rep_scorecard":       _rep_scorecard(deals),
        "product_performance": _product_perf(deals),
        "meddpicc_heatmap":    _meddpicc_heatmap(deals),
        "closing_this_month":  closing_this_month,
        "top_priorities":      top_priorities,
    })


@app.route("/api/dashboard/rep")
def dashboard_rep_view():
    rep_name = (request.args.get("name") or "").strip()
    if not rep_name:
        return jsonify({"error": "name parameter required"}), 400

    all_deals = _load_pipeline()
    if all_deals is None:
        return jsonify({"error": "meddpicc_pipeline.json not found"}), 404

    resource_map = {r["deal_id"]: r.get("product", "") for r in _load_resource_pipeline_data()}
    all_deals = _enrich_deals(all_deals, resource_map)

    deals = [d for d in all_deals if (d.get("assigned_rep") or "").strip().lower() == rep_name.lower()]
    if not deals:
        return jsonify({"error": f"No deals found for rep: {rep_name}"}), 404

    personas = _load_personas_data()
    persona = next((p for p in personas if p.get("name", "").lower() == rep_name.lower()), {})

    stats = _overall_stats(deals)

    prod_count, prod_value = {}, {}
    for d in deals:
        prod = d.get("product", "Unknown")
        prod_count[prod] = prod_count.get(prod, 0) + 1
        prod_value[prod] = prod_value.get(prod, 0) + (d.get("amount") or 0)
    product_mix = sorted([{"product": p, "count": c, "value": prod_value[p]}
                           for p, c in prod_count.items()],
                         key=lambda x: x["count"], reverse=True)

    deals_sorted = sorted(deals, key=lambda d: (
        0 if "RISK" in (d.get("overall_score") or "").upper() else 1,
        d.get("scored_at", "")))
    deals_list = [{
        "deal_name":      d["deal_name"],
        "company":        d["company"],
        "product":        d.get("product", ""),
        "stage":          d.get("stage", ""),
        "amount":         d.get("amount", 0) or 0,
        "probability":    d.get("probability", 0) or 0,
        "overall_score":  d.get("overall_score", ""),
        "days_in_pipeline": d.get("days_in_pipeline", 0),
        "next_priority":  (d.get("next_priority") or "")[:120],
        "active_flags":   d.get("active_flags", []),
        "meddpicc": {el: {"status": (d.get("elements", {}).get(el) or {}).get("status", "unknown"),
                          "score":  _STATUS_SCORE.get((d.get("elements", {}).get(el) or {}).get("status", "unknown"), 0)}
                    for el in _ELEMENT_ORDER},
    } for d in deals_sorted]

    elem_scores = {e: [] for e in _ELEMENT_ORDER}
    for d in deals:
        for el in _ELEMENT_ORDER:
            st = (d.get("elements", {}).get(el) or {}).get("status", "unknown")
            elem_scores[el].append(_STATUS_SCORE.get(st, 0))
    meddpicc_elements = [{"element": _ELEMENT_LABELS[el],
                          "avg_score": round(sum(v) / len(v), 2) if v else 0} for el, v in elem_scores.items()]

    scores = {el: (sum(v) / len(v) if v else 0) for el, v in elem_scores.items()}
    worst_el = min(scores, key=scores.get)
    best_el  = max(scores, key=scores.get)

    all_flags = [f for d in deals for f in d.get("active_flags", [])]
    fc = {}
    for f in all_flags:
        fc[f] = fc.get(f, 0) + 1
    biggest_gap = max(fc, key=fc.get) if fc else "No flags"

    closing_soon   = [d for d in deals if (d.get("stage") or "").upper() == "PROPOSAL"]
    at_risk_closing = sum(1 for d in closing_soon if "RISK" in (d.get("overall_score") or "").upper())
    urgent_deal    = next((d for d in deals_sorted if d.get("next_priority")), {})
    won_count      = sum(1 for d in deals if (d.get("stage") or "").upper() == "CUSTOMER")
    win_rate       = round(won_count * 100 / len(deals), 1) if deals else 0

    return jsonify({
        "rep_name":  rep_name,
        "rep_title": persona.get("title", "Account Executive"),
        "pipeline_overview": {**stats["pipeline_overview"], "win_rate": win_rate},
        "score_distribution": stats["score_distribution"],
        "deals":             deals_list,
        "meddpicc_elements": meddpicc_elements,
        "flag_frequency":    _flag_frequency(deals),
        "product_mix":       product_mix,
        "coaching_card": {
            "biggest_gap":        biggest_gap,
            "closing_this_month": len(closing_soon),
            "at_risk_closing":    at_risk_closing,
            "strength":           _ELEMENT_LABELS.get(best_el, best_el),
            "next_priority":      (urgent_deal.get("next_priority") or "")[:150],
        },
    })


@app.route("/api/dashboard/leadership")
def dashboard_leadership():
    all_deals = _load_pipeline()
    if all_deals is None:
        return jsonify({"error": "meddpicc_pipeline.json not found"}), 404

    resource_map = {r["deal_id"]: r.get("product", "") for r in _load_resource_pipeline_data()}
    deals = _enrich_deals(all_deals, resource_map)

    total     = len(deals)
    total_val = sum(d.get("amount", 0) or 0 for d in deals)
    avg_val   = total_val / total if total else 0

    customer_deals = [d for d in deals if (d.get("stage") or "").upper() == "CUSTOMER"]
    won_count      = len(customer_deals)
    win_rate       = won_count / total if total else 0

    stage_counts = {"NEW": 0, "MEETING": 0, "PROPOSAL": 0, "CUSTOMER": 0}
    stage_values = {"NEW": 0, "MEETING": 0, "PROPOSAL": 0, "CUSTOMER": 0}
    stage_days   = {"NEW": [], "MEETING": [], "PROPOSAL": [], "CUSTOMER": []}
    for d in deals:
        s = (d.get("stage") or "").upper()
        if s in stage_counts:
            stage_counts[s] += 1
            stage_values[s] += d.get("amount", 0) or 0
            stage_days[s].append(d.get("days_in_pipeline", 0))

    def _savg(s):
        lst = stage_days.get(s, [])
        return round(sum(lst) / len(lst)) if lst else 0

    def _conv(a, b):
        return round(b * 100 / a, 1) if a else 0

    n_new, n_meet, n_prop, n_cust = (stage_counts[s] for s in ("NEW", "MEETING", "PROPOSAL", "CUSTOMER"))

    avg_cycle = sum(d.get("days_in_pipeline", 0) for d in customer_deals) / won_count if won_count else 90
    velocity  = (total * avg_val * win_rate) / avg_cycle if avg_cycle else 0

    rep_won = {}
    for d in customer_deals:
        rep = d.get("assigned_rep", "Unknown")
        rep_won[rep] = rep_won.get(rep, 0) + 1

    total_sales_cost = sum(_REP_LOADED_COMP.get(r, 165_000)
                           for r in set(d.get("assigned_rep", "") for d in deals))
    sales_cac_overall = round(total_sales_cost / max(won_count, 1))

    sales_cac_by_rep = sorted([
        {"rep": rep, "cac": round(_REP_LOADED_COMP.get(rep, 165_000) / max(w, 1)), "deals_won": w}
        for rep, w in rep_won.items()
    ], key=lambda x: x["cac"])

    prod_won = {}
    for d in customer_deals:
        prod = d.get("product", "Unknown")
        prod_won[prod] = prod_won.get(prod, 0) + 1
    avg_rep_cost = sum(_REP_LOADED_COMP.values()) / len(_REP_LOADED_COMP)
    sales_cac_by_product = sorted([
        {"product": prod, "cac": round(avg_rep_cost / max(w, 1)), "deals_won": w}
        for prod, w in prod_won.items()
    ], key=lambda x: x["cac"])

    seg_deals = {"Enterprise": [], "Mid-Market": [], "SMB": []}
    for d in deals:
        seg = _get_segment(d.get("amount", 0) or 0)
        seg_deals[seg].append(d.get("amount", 0) or 0)

    ltv_by_segment = []
    for seg, amounts in seg_deals.items():
        if not amounts:
            continue
        avg_arr    = sum(amounts) / len(amounts)
        churn      = _BENCHMARK_CHURN[seg]
        ltv        = avg_arr / churn
        seg_won    = sum(1 for d in customer_deals if _get_segment(d.get("amount", 0) or 0) == seg)
        seg_cac    = round(avg_rep_cost / max(seg_won, 1))
        ltv_by_segment.append({"segment": seg, "avg_arr": round(avg_arr), "churn_rate": churn,
                                "ltv": round(ltv), "ltv_cac_ratio": round(ltv / seg_cac, 1) if seg_cac else 0})

    avg_churn  = 0.10
    est_ltv    = avg_val / avg_churn
    ltv_cac_r  = round(est_ltv / sales_cac_overall, 1) if sales_cac_overall else 0
    ltv_health = "HEALTHY" if ltv_cac_r >= 3.0 else "WARNING" if ltv_cac_r >= 1.5 else "CRITICAL"

    rep_funnel = {}
    for d in deals:
        rep = d.get("assigned_rep", "Unknown")
        if rep not in rep_funnel:
            rep_funnel[rep] = {"total": 0, "won": 0, "days": []}
        rep_funnel[rep]["total"] += 1
        if (d.get("stage") or "").upper() == "CUSTOMER":
            rep_funnel[rep]["won"]  += 1
            rep_funnel[rep]["days"].append(d.get("days_in_pipeline", 0))
    funnel_by_rep = sorted([
        {"rep": rep, "win_rate": round(v["won"] * 100 / v["total"], 1) if v["total"] else 0,
         "avg_cycle_days": round(sum(v["days"]) / len(v["days"])) if v["days"] else 0}
        for rep, v in rep_funnel.items()
    ], key=lambda x: x["win_rate"], reverse=True)

    prod_funnel = {}
    for d in deals:
        prod = d.get("product", "Unknown")
        if prod not in prod_funnel:
            prod_funnel[prod] = {"total": 0, "won": 0, "days": []}
        prod_funnel[prod]["total"] += 1
        if (d.get("stage") or "").upper() == "CUSTOMER":
            prod_funnel[prod]["won"]  += 1
            prod_funnel[prod]["days"].append(d.get("days_in_pipeline", 0))
    funnel_by_product = sorted([
        {"product": prod, "win_rate": round(v["won"] * 100 / v["total"], 1) if v["total"] else 0,
         "avg_cycle_days": round(sum(v["days"]) / len(v["days"])) if v["days"] else 0}
        for prod, v in prod_funnel.items()
    ], key=lambda x: x["win_rate"], reverse=True)[:12]

    drops = {"NEW→MEETING": n_new - n_meet, "MEETING→PROPOSAL": n_meet - n_prop,
             "PROPOSAL→CUSTOMER": n_prop - n_cust}
    biggest_drop = max(drops, key=drops.get)

    at_risk_cust  = [d for d in customer_deals if "RISK" in (d.get("overall_score") or "").upper()]
    expansion_pot = sum((d.get("amount", 0) or 0) * 0.20
                        for d in customer_deals if "RISK" not in (d.get("overall_score") or "").upper())

    sens_win  = total * avg_val * 0.10
    sens_size = total * avg_val * 0.15 * win_rate
    sens_vel  = velocity * 0.20
    sens_cac  = total_sales_cost * 0.15
    levers    = {"win_rate": sens_win, "deal_size": sens_size, "velocity": sens_vel, "cac": sens_cac}
    big_lever = max(levers, key=levers.get)
    lever_desc = {
        "win_rate":  "STRONG MEDDPICC deals close at much higher rates — invest in qualification coaching",
        "deal_size": "Average deal size is below potential — focus on multi-product and expansion motions",
        "velocity":  "Reducing sales cycle length by 20% adds equivalent pipeline velocity",
        "cac":       "Optimizing rep capacity and territory coverage reduces cost per acquisition",
    }

    roi_pct = round((total_val * win_rate - total_sales_cost) / total_sales_cost * 100, 1) if total_sales_cost else 0

    return jsonify({
        "dupont": {
            "roi_pct":                   roi_pct,
            "avg_deal_size":             round(avg_val),
            "win_rate":                  round(win_rate * 100, 1),
            "pipeline_velocity":         round(velocity),
            "sales_cac":                 sales_cac_overall,
            "biggest_lever":             big_lever,
            "biggest_lever_impact_usd":  round(levers[big_lever]),
            "biggest_lever_description": lever_desc[big_lever],
        },
        "customer_acquisition": {
            "sales_cac_overall":    sales_cac_overall,
            "sales_cac_by_rep":     sales_cac_by_rep[:5],
            "sales_cac_by_product": sales_cac_by_product[:5],
        },
        "ltv": {
            "avg_arr":            round(avg_val),
            "benchmark_churn_rate": avg_churn,
            "estimated_ltv":      round(est_ltv),
            "ltv_cac_ratio":      ltv_cac_r,
            "ltv_cac_health":     ltv_health,
            "by_segment":         ltv_by_segment,
        },
        "conversion_funnel": {
            "meeting_to_opportunity":  _conv(n_new,  n_meet),
            "opportunity_to_proposal": _conv(n_meet, n_prop),
            "proposal_to_customer":    _conv(n_prop, n_cust),
            "overall_win_rate":        round(win_rate * 100, 1),
            "avg_days_new":            _savg("NEW"),
            "avg_days_meeting":        _savg("MEETING"),
            "avg_days_proposal":       _savg("PROPOSAL"),
            "avg_total_cycle":         round(avg_cycle),
            "biggest_drop_stage":      biggest_drop,
            "stage_counts":            stage_counts,
            "stage_values":            stage_values,
            "by_rep":                  funnel_by_rep,
            "by_product":              funnel_by_product,
        },
        "retention_risk": {
            "total_customer_deals":    won_count,
            "at_risk_customers":       len(at_risk_cust),
            "at_risk_customer_value":  round(sum(d.get("amount", 0) or 0 for d in at_risk_cust)),
            "churn_risk_pct":          round(len(at_risk_cust) * 100 / won_count, 1) if won_count else 0,
            "expansion_potential_usd": round(expansion_pot),
        },
        "sensitivity": {
            "win_rate_plus_10pct":  {"revenue_impact": round(sens_win)},
            "deal_size_plus_15pct": {"revenue_impact": round(sens_size)},
            "cycle_minus_20pct":    {"revenue_impact": round(sens_vel)},
            "cac_minus_15pct":      {"savings": round(sens_cac)},
        },
        "editable_inputs": {
            "marketing_spend": 0, "avg_contract_length_years": 3,
            "annual_quota": 0, "target_churn_rate": 0.10,
        },
    })


# ── ARCHITECTURE REVIEW AGENTS ───────────────────────────────────────────────

def arch_assessment_agent(deal_notes: str) -> str:
    """
    Senior Microsoft Solutions Architect running cloud enterprise triage.
    Returns structured assessment with CUSTOMER NAME, CORE PROBLEM STATEMENT,
    INFRASTRUCTURE SIGNALS, INDUSTRY VERTICAL.
    """
    prompt = f"""You are a Senior Microsoft Solutions Architect conducting an enterprise cloud triage.

DEAL NOTES:
{deal_notes}

Analyze these notes and produce a structured assessment in EXACTLY this format:

CUSTOMER NAME:
[Customer or organization name from the notes]

CORE PROBLEM STATEMENT:
[2-3 sentences describing the main business or technical problem]

INFRASTRUCTURE SIGNALS:
- [Signal 1: existing system, technology, or infrastructure hint]
- [Signal 2]
- [Signal 3]

INDUSTRY VERTICAL:
[Industry sector — e.g. Financial Services, Healthcare, Retail, Manufacturing, Government, etc.]"""
    try:
        return llm(prompt, max_tokens=600)
    except Exception as e:
        return f"[Assessment error: {e}]"


def greenfield_advocate_agent(assessment: str, deal_notes: str) -> str:
    """
    Cloud-native SA advocating for a clean-slate greenfield modernization.
    Returns 3-5 markdown bullet points signalling greenfield suitability.
    """
    prompt = f"""You are a cloud-native Solutions Architect advocating for a greenfield modernization strategy.

ASSESSMENT:
{assessment}

DEAL NOTES:
{deal_notes}

Identify signals that warrant a greenfield (new build) approach. Respond with EXACTLY 3 to 5 markdown bullet points.
Focus on: legacy debt indicators, scalability blockers, competitive differentiation opportunities, and time-to-value advantages of a clean slate.

- [Signal 1]
- [Signal 2]
- [Signal 3]
(add up to 2 more if strongly evidenced)"""
    try:
        return llm(prompt, max_tokens=400)
    except Exception as e:
        return f"[Greenfield advocate error: {e}]"


def brownfield_advocate_agent(assessment: str, deal_notes: str) -> str:
    """
    Risk-mitigating Enterprise Integration Specialist protecting legacy investments.
    Returns 3-5 markdown bullet points highlighting brownfield integration needs.
    """
    prompt = f"""You are an Enterprise Integration Specialist advocating for a brownfield migration strategy that protects existing investments.

ASSESSMENT:
{assessment}

DEAL NOTES:
{deal_notes}

Identify signals that support a brownfield (integrate and extend) approach. Respond with EXACTLY 3 to 5 markdown bullet points.
Focus on: existing system dependencies, data migration complexity, business continuity risks, sunk cost investments, and integration touchpoints.

- [Signal 1]
- [Signal 2]
- [Signal 3]
(add up to 2 more if strongly evidenced)"""
    try:
        return llm(prompt, max_tokens=400)
    except Exception as e:
        return f"[Brownfield advocate error: {e}]"


def arch_arbitrator_agent(assessment: str, greenfield_case: str, brownfield_case: str) -> str:
    """
    Principal Technical Architect making the final classification decision.
    Returns VERDICT, CONFIDENCE, KEY SIGNAL, and DISCOVERY QUESTIONS.
    """
    prompt = f"""You are a Principal Technical Architect making the final architectural classification for a customer engagement.

ASSESSMENT:
{assessment}

GREENFIELD CASE:
{greenfield_case}

BROWNFIELD CASE:
{brownfield_case}

Weigh both cases and deliver your verdict in EXACTLY this format (no extra text):

VERDICT: [GREENFIELD or BROWNFIELD]
CONFIDENCE: [X%]
KEY SIGNAL: [One sentence rationale referencing the strongest deciding factor]
DISCOVERY QUESTIONS:
1. [Most important clarifying question to validate this verdict]
2. [Second clarifying question]"""
    try:
        return llm(prompt, max_tokens=350)
    except Exception as e:
        return f"[Arbitrator error: {e}]"


def architecture_agent(assessment: str, verdict: str, service_catalog: dict) -> str:
    """
    Core Platform Blueprinting Systems Engineer.
    Selects 4-7 services from SERVICE_CATALOG and maps them to PLATFORM and APPLICATION layers.
    """
    azure_names = [s["name"] for s in service_catalog.get("azure_services", [])]
    oss_names = [s["name"] for s in service_catalog.get("open_source", [])]

    catalog_summary = "AZURE SERVICES: " + ", ".join(azure_names) + "\nOPEN SOURCE OPTIONS: " + ", ".join(oss_names)

    is_greenfield = "GREENFIELD" in verdict.upper()
    guardrail = (
        "This is a GREENFIELD engagement. Start with platform foundations: Networking, Microsoft Entra ID, Azure Key Vault, Azure Monitor BEFORE application-layer services."
        if is_greenfield else
        "This is a BROWNFIELD engagement. Skip platform layer setup assumptions. Focus on migration gap layers, integration bridges, and modernisation targets."
    )

    prompt = f"""You are a Core Platform Blueprinting Systems Engineer designing an Azure reference architecture.

ASSESSMENT:
{assessment}

VERDICT: {verdict}

{guardrail}

AVAILABLE SERVICES:
{catalog_summary}

Select EXACTLY 4 to 7 services from the list above and map them to the correct layers.
Use EXACTLY this block template (include PLATFORM LAYER only if verdict is GREENFIELD):

PLATFORM LAYER (Greenfield only):
- [Azure Service Name]: [Why it fits this specific customer scenario]

APPLICATION LAYER:
- [Azure Service Name]: [Why it fits]
- [Open Source Option]: [When to prefer this alternative over the native Azure service]

Be specific to the customer's problem. Do not list services not in the catalog."""
    try:
        return llm(prompt, max_tokens=700)
    except Exception as e:
        return f"[Architecture designer error: {e}]"


def sow_writer_agent(assessment: str, verdict: str, architecture: str, deal_notes: str) -> str:
    """
    Senior Technical Delivery Director writing a formal Statement of Work.
    Uses [X weeks] and [X] placeholders for timelines and staffing.
    """
    prompt = f"""You are a Senior Technical Delivery Director writing a formal Statement of Work (SOW) for a Microsoft Azure cloud engagement.

ASSESSMENT:
{assessment}

VERDICT: {verdict}

PROPOSED ARCHITECTURE:
{architecture}

DEAL NOTES:
{deal_notes}

STRICT CONSTRAINT: Use [X weeks] for ALL timeline figures and [X] for ALL resource/headcount figures. Never write actual numbers for schedules or staffing.

Write a complete, formal SOW in markdown using this structure. Follow the numbered section format exactly.

# Statement of Work

## 1.0 Purpose
[One paragraph: describe the cloud services being delivered, the application/workload, and the goal of this engagement. Reference the department/agency and what problem it solves.]

## 2.0 Background and Objective
[Two paragraphs: (1) business context — why cloud, what the customer organisation does, and the driver for this engagement; (2) what the department/agency seeks to achieve and any relevant project history or constraints.]

## 3.0 Scope
### 3.1 In Scope
[Bullet list of all cloud activities, services, and deliverables included in this engagement]

### 3.2 Out of Scope
[Bullet list of items explicitly excluded — on-prem migrations unless stated, third-party SaaS integrations not listed, end-user training beyond admin onboarding, etc.]

## 4.0 Proposed Architecture
[Full narrative description of the technical approach — layers, integration points, data flow, key design decisions, and how this architecture addresses the requirements. This section must stand alone as the architectural narrative.]

## 5.0 Azure Services

| Service | Purpose | Layer |
|---------|---------|-------|
[One row per Azure service selected — be specific with SKU tiers where relevant]

## 6.0 Requirements
### 6.1 General Cloud Computing Requirements

| Cloud Characteristic | Definition | Requirement |
|---|---|---|
| On-Demand Self-Service | Resources provisioned without human interaction | Automated provisioning via ARM/Bicep templates |
| Broad Network Access | Available over standard network mechanisms | HTTPS endpoints with Azure Front Door or API Gateway |
| Resource Pooling | Multi-tenant shared infrastructure | Azure shared resource pools with dedicated tiers as required |
| Rapid Elasticity | Scale up/down on demand | Auto-scale policies on all compute and storage layers |
| Measured Service | Usage monitored and billed | Azure Cost Management + Billing with tagging strategy |

### 6.2 Service Management and Provisioning Requirements

| Characteristic | Requirements |
|---|---|
| Service Provisioning | Infrastructure-as-Code via Bicep/ARM; CI/CD pipeline for automated deployment |
| SLA Management | 99.9% uptime SLA; monthly SLA reports; escalation procedure for outages >1 hour |
| Operational Management | Azure Monitor alerts; runbook documentation; defined escalation paths |
| DR and Business Continuity | RPO [X hours] / RTO [X hours]; geo-redundant backups; failover tested quarterly |
| Data Management | Data residency in [region]; encryption at rest (AES-256) and in transit (TLS 1.2+) |
| Billing and Invoice Tracking | Monthly itemised Azure invoice; cost anomaly alerts; reserved instance recommendations |
| Utilisation Monitoring | Azure Monitor dashboards; monthly utilisation reports per service |

### 6.3 Specific Application Technical Requirements
[List application-specific requirements: storage tiers, throughput, latency SLAs, API rate limits, authentication method (AAD/B2C), network topology (VNet peering, private endpoints), etc.]

## 7.0 Compliance Requirements
### 7.1 Security Requirements
[Key security controls: RBAC, Azure Policy, Defender for Cloud, network segmentation, identity management, audit logging, key vault usage]

### 7.2 Privacy and Data Management
[Data classification, PII handling, retention policies, right-to-erasure compliance, data sovereignty constraints]

### 7.3 Accessibility Requirements
[UI/portal accessibility standards, WCAG compliance for any customer-facing interfaces]

## 8.0 Reporting Deliverables

| Report / Deliverable | Description | Frequency |
|---|---|---|
| Service Level Agreement (SLA) | Uptime % (Total Uptime / Total Hours); description of any outages >1 hour including root cause and fix | Monthly |
| Help Desk / Trouble Tickets | Tickets opened, closed, mean time to respond, mean time to resolve | Monthly |
| Cost and Utilisation | Itemised Azure spend by service and resource group; anomaly flags; reserved instance opportunities | Monthly |
| Security and Compliance | Defender for Cloud score; open policy violations; remediation status | Monthly |
| Change Management | Planned changes, deployments completed, rollback events | Monthly |

## 9.0 Pricing and Timeline
### 9.1 Implementation Timeline

| Phase | Description | Duration |
|-------|-------------|----------|
| Phase 1 — Foundation | [Landing zone, networking, IAM, core infrastructure] | [X weeks] |
| Phase 2 — Build | [Application deployment, data migration, integrations] | [X weeks] |
| Phase 3 — Validate | [UAT, performance testing, security review, go-live] | [X weeks] |

### 9.2 Pricing Summary

| Cloud Service | Description | Estimated Monthly Cost |
|---|---|---|
[One row per major service category — use [X] for all figures]

Vendor shall provide detailed pricing breakdown per Azure service as a separate pricing schedule. All prices are subject to Azure Retail Prices at time of provisioning.

## 10.0 Terms and Conditions
[Key assumptions, acceptance criteria, and any additional terms. Include: data ownership (customer retains ownership of all data and applications), change control process, IP ownership, termination for convenience clause, and any DIR master contract references.]

---
Customer Agency: ___________________________  Date: ___________

Cloud Service Provider (Vendor): ___________________________  Date: ___________"""
    try:
        return llm(prompt, max_tokens=2500)
    except Exception as e:
        return f"[SOW writer error: {e}]"


# ── ARCHITECTURE REVIEW API ROUTES ───────────────────────────────────────────

@app.route("/api/architecture/analyze", methods=["POST"])
def architecture_analyze():
    """
    Runs the 6-agent Architecture Review pipeline with SSE streaming.
    Body: {"deal_notes": "...", "deal_name": "..."}
    """
    body = request.get_json(silent=True) or {}
    deal_notes = (body.get("deal_notes") or "").strip()
    deal_name  = (body.get("deal_name") or "Architecture Review").strip()

    if not deal_notes:
        return jsonify({"error": "deal_notes is required"}), 400

    def generate():
        def send_event(etype: str, data: dict):
            yield f"event: {etype}\ndata: {json.dumps(data)}\n\n"

        assessment = greenfield_case = brownfield_case = ""
        arbitration = architecture = sow_text = ""
        verdict = confidence = ""

        # ── Agent 1: Assessment ────────────────────────────────────────────
        try:
            yield from send_event("arch_agent_start", {"agent": 1, "name": "Assessment Agent"})
            assessment = ""
            prompt1 = f"""You are a Senior Microsoft Solutions Architect conducting an enterprise cloud triage.

DEAL NOTES:
{deal_notes}

Analyze these notes and produce a structured assessment in EXACTLY this format:

CUSTOMER NAME:
[Customer or organization name from the notes]

CORE PROBLEM STATEMENT:
[2-3 sentences describing the main business or technical problem]

INFRASTRUCTURE SIGNALS:
- [Signal 1]
- [Signal 2]
- [Signal 3]

INDUSTRY VERTICAL:
[Industry sector]"""
            for token in llm_stream(prompt1, max_tokens=600):
                assessment += token
                yield f"data: {json.dumps({'type': 'arch_token', 'agent': 1, 'text': token})}\n\n"
            yield from send_event("arch_agent_done", {"agent": 1, "name": "Assessment Agent", "status": "done"})
        except Exception as e:
            assessment = f"[Assessment error: {e}]"
            yield from send_event("arch_agent_done", {"agent": 1, "name": "Assessment Agent",
                                                       "status": "error", "error": str(e)})

        # ── Agent 2: Greenfield Advocate ───────────────────────────────────
        try:
            yield from send_event("arch_agent_start", {"agent": 2, "name": "Greenfield Advocate"})
            greenfield_case = ""
            prompt2 = f"""You are a cloud-native Solutions Architect advocating for a greenfield modernization strategy.

ASSESSMENT:
{assessment}

DEAL NOTES:
{deal_notes}

Identify signals that warrant a greenfield (new build) approach. Respond with EXACTLY 3 to 5 markdown bullet points.

- [Signal 1]
- [Signal 2]
- [Signal 3]"""
            for token in llm_stream(prompt2, max_tokens=400):
                greenfield_case += token
                yield f"data: {json.dumps({'type': 'arch_token', 'agent': 2, 'text': token})}\n\n"
            yield from send_event("arch_agent_done", {"agent": 2, "name": "Greenfield Advocate", "status": "done"})
        except Exception as e:
            greenfield_case = f"[Greenfield error: {e}]"
            yield from send_event("arch_agent_done", {"agent": 2, "name": "Greenfield Advocate",
                                                       "status": "error", "error": str(e)})

        # ── Agent 3: Brownfield Advocate ───────────────────────────────────
        try:
            yield from send_event("arch_agent_start", {"agent": 3, "name": "Brownfield Advocate"})
            brownfield_case = ""
            prompt3 = f"""You are an Enterprise Integration Specialist advocating for a brownfield migration strategy.

ASSESSMENT:
{assessment}

DEAL NOTES:
{deal_notes}

Identify signals that support a brownfield (integrate and extend) approach. Respond with EXACTLY 3 to 5 markdown bullet points.

- [Signal 1]
- [Signal 2]
- [Signal 3]"""
            for token in llm_stream(prompt3, max_tokens=400):
                brownfield_case += token
                yield f"data: {json.dumps({'type': 'arch_token', 'agent': 3, 'text': token})}\n\n"
            yield from send_event("arch_agent_done", {"agent": 3, "name": "Brownfield Advocate", "status": "done"})
        except Exception as e:
            brownfield_case = f"[Brownfield error: {e}]"
            yield from send_event("arch_agent_done", {"agent": 3, "name": "Brownfield Advocate",
                                                       "status": "error", "error": str(e)})

        # ── Agent 4: Arbitrator ────────────────────────────────────────────
        try:
            yield from send_event("arch_agent_start", {"agent": 4, "name": "Arbitrator"})
            arbitration = ""
            prompt4 = f"""You are a Principal Technical Architect making the final architectural classification.

ASSESSMENT:
{assessment}

GREENFIELD CASE:
{greenfield_case}

BROWNFIELD CASE:
{brownfield_case}

Deliver your verdict in EXACTLY this format:

VERDICT: [GREENFIELD or BROWNFIELD]
CONFIDENCE: [X%]
KEY SIGNAL: [One sentence rationale]
DISCOVERY QUESTIONS:
1. [Question]
2. [Question]"""
            for token in llm_stream(prompt4, max_tokens=350):
                arbitration += token
                yield f"data: {json.dumps({'type': 'arch_token', 'agent': 4, 'text': token})}\n\n"
            # Extract verdict and confidence for downstream agents
            import re as _re_arch
            v_match = _re_arch.search(r'VERDICT:\s*(GREENFIELD|BROWNFIELD)', arbitration, _re_arch.IGNORECASE)
            c_match = _re_arch.search(r'CONFIDENCE:\s*(\d+%?)', arbitration)
            verdict    = v_match.group(1).upper() if v_match else "BROWNFIELD"
            confidence = c_match.group(1) if c_match else "N/A"
            yield from send_event("arch_agent_done", {"agent": 4, "name": "Arbitrator",
                                                       "verdict": verdict, "confidence": confidence, "status": "done"})
        except Exception as e:
            arbitration = f"[Arbitrator error: {e}]"
            verdict = "BROWNFIELD"
            yield from send_event("arch_agent_done", {"agent": 4, "name": "Arbitrator",
                                                       "status": "error", "error": str(e)})

        # ── Agent 5: Architecture Designer ────────────────────────────────
        try:
            yield from send_event("arch_agent_start", {"agent": 5, "name": "Arch Designer"})
            architecture = ""
            azure_names = [s["name"] for s in SERVICE_CATALOG.get("azure_services", [])]
            oss_names   = [s["name"] for s in SERVICE_CATALOG.get("open_source", [])]
            catalog_summary = "AZURE: " + ", ".join(azure_names) + "\nOPEN SOURCE: " + ", ".join(oss_names)
            is_gf = "GREENFIELD" in verdict.upper()
            guardrail = (
                "GREENFIELD: Start with platform foundations (Entra ID, Key Vault, Monitor) before app-layer services."
                if is_gf else
                "BROWNFIELD: Skip platform setup. Focus on migration layers and integration bridges."
            )
            prompt5 = f"""You are a Core Platform Blueprinting Systems Engineer.

ASSESSMENT:
{assessment}

VERDICT: {verdict}
{guardrail}

AVAILABLE SERVICES:
{catalog_summary}

Select EXACTLY 4 to 7 services and map them using this template:

PLATFORM LAYER (include only if GREENFIELD):
- [Azure Service Name]: [Why it fits]

APPLICATION LAYER:
- [Azure Service Name]: [Why it fits]
- [Open Source Option]: [When to prefer this over Azure native]"""
            for token in llm_stream(prompt5, max_tokens=700):
                architecture += token
                yield f"data: {json.dumps({'type': 'arch_token', 'agent': 5, 'text': token})}\n\n"
            yield from send_event("arch_agent_done", {"agent": 5, "name": "Arch Designer", "status": "done"})
        except Exception as e:
            architecture = f"[Architecture error: {e}]"
            yield from send_event("arch_agent_done", {"agent": 5, "name": "Arch Designer",
                                                       "status": "error", "error": str(e)})

        # ── Architecture Diagram (Together AI image generation) ────────────
        arch_diagram_b64 = ""
        try:
            import base64 as _b64_img
            yield f"data: {json.dumps({'type': 'arch_diagram_loading'})}\n\n"
            diagram_prompt = f"""Microsoft Azure enterprise cloud architecture diagram, technical blueprint style.

Services and components:
{architecture}

Style: clean white background, Microsoft Azure color scheme (blue #0078D4, teal, purple), labeled swim lanes (Client Layer, API Layer, Application Layer, Data Layer, Security Layer), directional arrows showing data flow, professional labeled service boxes with drop shadows, Segoe UI font, Azure Architecture Center reference diagram style, high contrast, no decorative illustrations, purely technical enterprise diagram."""
            _img_resp = _together.images.generate(
                model="openai/gpt-image-1.5",
                prompt=diagram_prompt,
                n=1,
                width=1536,
                height=1024,
            )
            _img_data = _img_resp.data[0]
            if _img_data.b64_json:
                arch_diagram_b64 = _img_data.b64_json
            elif _img_data.url:
                _dl = requests.get(_img_data.url, timeout=30)
                _dl.raise_for_status()
                arch_diagram_b64 = _b64_img.b64encode(_dl.content).decode("utf-8")
            yield f"data: {json.dumps({'type': 'arch_diagram', 'image_b64': arch_diagram_b64})}\n\n"
        except Exception as _img_err:
            yield f"data: {json.dumps({'type': 'arch_diagram_error', 'message': str(_img_err)})}\n\n"

        # ── Agent 6: SOW Writer ────────────────────────────────────────────
        try:
            yield from send_event("arch_agent_start", {"agent": 6, "name": "SOW Writer"})
            sow_text = ""
            prompt6 = f"""You are a Senior Technical Delivery Director writing a formal Statement of Work (SOW) for a Microsoft Azure cloud engagement.

ASSESSMENT:
{assessment}

VERDICT: {verdict}

PROPOSED ARCHITECTURE:
{architecture}

DEAL NOTES:
{deal_notes}

STRICT CONSTRAINT: Use [X weeks] for ALL timeline figures. Use [X] for ALL resource/headcount figures. Never write actual numbers for schedules or staffing.

Write a complete formal SOW in markdown using this exact structure:

# Statement of Work

## 1.0 Purpose
[One paragraph: cloud services being delivered, the application/workload, and the goal. Reference the department/agency and what problem it solves.]

## 2.0 Background and Objective
[Two paragraphs: business context, why cloud, customer organisation background, what the agency seeks to achieve.]

## 3.0 Scope
### 3.1 In Scope
[Bullet list of cloud activities, services, and deliverables included]

### 3.2 Out of Scope
[Bullet list of items explicitly excluded]

## 4.0 Proposed Architecture
[Full narrative of technical approach — layers, integration points, data flow, key design decisions]

## 5.0 Azure Services

| Service | Purpose | Layer |
|---------|---------|-------|
[One row per Azure service]

## 6.0 Requirements
### 6.1 General Cloud Computing Requirements

| Cloud Characteristic | Definition | Requirement |
|---|---|---|
| On-Demand Self-Service | Resources provisioned without human interaction | Automated provisioning via ARM/Bicep |
| Broad Network Access | Available over standard mechanisms | HTTPS endpoints with Azure Front Door or API Gateway |
| Rapid Elasticity | Scale on demand | Auto-scale policies on compute and storage |
| Measured Service | Usage monitored and billed | Azure Cost Management with tagging strategy |

### 6.2 Service Management and Provisioning Requirements

| Characteristic | Requirements |
|---|---|
| Service Provisioning | Infrastructure-as-Code via Bicep/ARM; CI/CD pipeline |
| SLA Management | 99.9% uptime SLA; monthly SLA reports; escalation for outages >1 hour |
| Operational Management | Azure Monitor alerts; runbook documentation; escalation paths |
| DR and Business Continuity | RPO [X hours] / RTO [X hours]; geo-redundant backups; quarterly failover tests |
| Data Management | Encryption at rest (AES-256) and in transit (TLS 1.2+) |
| Billing and Invoice Tracking | Monthly itemised Azure invoice; cost anomaly alerts |
| Utilisation Monitoring | Azure Monitor dashboards; monthly utilisation reports |

### 6.3 Specific Application Technical Requirements
[Application-specific: storage tiers, throughput, latency SLAs, authentication (AAD/B2C), network topology]

## 7.0 Compliance Requirements
### 7.1 Security Requirements
[RBAC, Azure Policy, Defender for Cloud, network segmentation, audit logging, key vault]

### 7.2 Privacy and Data Management
[Data classification, PII handling, retention policies, data sovereignty]

### 7.3 Accessibility Requirements
[WCAG compliance for any customer-facing interfaces]

## 8.0 Reporting Deliverables

| Report / Deliverable | Description | Frequency |
|---|---|---|
| Service Level Agreement (SLA) | Uptime %; description of outages >1 hour with root cause | Monthly |
| Help Desk / Trouble Tickets | Tickets opened, closed, mean time to respond and resolve | Monthly |
| Cost and Utilisation | Itemised Azure spend by service; anomaly flags; RI opportunities | Monthly |
| Security and Compliance | Defender for Cloud score; open policy violations; remediation status | Monthly |

## 9.0 Pricing and Timeline
### 9.1 Implementation Timeline

| Phase | Description | Duration |
|-------|-------------|----------|
| Phase 1 — Foundation | [Landing zone, networking, IAM, core infrastructure] | [X weeks] |
| Phase 2 — Build | [Application deployment, data migration, integrations] | [X weeks] |
| Phase 3 — Validate | [UAT, performance testing, security review, go-live] | [X weeks] |

### 9.2 Pricing Summary

| Cloud Service | Description | Estimated Monthly Cost |
|---|---|---|
[One row per major service category — use [X] for all figures]

## 10.0 Terms and Conditions
[Key assumptions, acceptance criteria, data ownership (customer retains all data and applications), change control process, IP ownership, termination for convenience clause]

---
Customer Agency: ___________________________  Date: ___________

Cloud Service Provider (Vendor): ___________________________  Date: ___________"""
            for token in llm_stream(prompt6, max_tokens=2500):
                sow_text += token
                yield f"data: {json.dumps({'type': 'arch_token', 'agent': 6, 'text': token})}\n\n"
            yield from send_event("arch_agent_done", {"agent": 6, "name": "SOW Writer", "status": "done"})
        except Exception as e:
            sow_text = f"[SOW error: {e}]"
            yield from send_event("arch_agent_done", {"agent": 6, "name": "SOW Writer",
                                                       "status": "error", "error": str(e)})

        # ── Persist to DB ──────────────────────────────────────────────────
        try:
            with get_db() as con:
                con.execute(
                    "INSERT INTO arch_assessments (deal_name, verdict, confidence, sow_text, created_at) VALUES (?,?,?,?,?)",
                    (deal_name, verdict, confidence, sow_text, datetime.now().isoformat())
                )
        except Exception as _db_err:
            pass  # non-fatal

        yield from send_event("arch_complete", {
            "deal_name":    deal_name,
            "verdict":      verdict,
            "confidence":   confidence,
            "sow_text":     sow_text,
            "assessment":   assessment,
            "greenfield":   greenfield_case,
            "brownfield":   brownfield_case,
            "arbitration":  arbitration,
            "architecture": architecture,
        })

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/architecture/send-review", methods=["POST"])
def architecture_send_review():
    """
    Send the SOW document to a reviewer via email (Gmail SMTP TLS).
    Body: {to_email, to_name, deal_name, sow_text, verdict}
    Env vars: GMAIL_SENDER, GMAIL_APP_PASSWORD
    """
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    body       = request.get_json(silent=True) or {}
    to_email   = (body.get("to_email") or "").strip()
    to_name    = (body.get("to_name") or "Reviewer").strip()
    deal_name  = (body.get("deal_name") or "").strip()
    sow_text   = (body.get("sow_text") or "").strip()
    verdict    = (body.get("verdict") or "").strip()

    if not to_email or not sow_text:
        return jsonify({"error": "to_email and sow_text are required"}), 400

    sender   = os.environ.get("GMAIL_SENDER", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not sender or not password:
        return jsonify({"error": "GMAIL_SENDER and GMAIL_APP_PASSWORD env vars not set"}), 500

    subject = f"Architecture Review Request — {deal_name} [{verdict}]"
    body_text = (
        f"Hi {to_name},\n\n"
        f"Please review the architecture assessment for: {deal_name}\n"
        f"Verdict: {verdict}\n\n"
        f"---\n\n"
        f"{sow_text}\n\n"
        f"---\n"
        f"Sent by Deal Desk Agent — Architecture Review\n"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = to_email
    msg.attach(MIMEText(body_text, "plain"))

    success = False
    error_msg = ""
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, [to_email], msg.as_string())
        success = True
    except Exception as e:
        error_msg = str(e)

    try:
        with get_db() as con:
            con.execute(
                "INSERT INTO email_log (deal_name, recipient_email, sent_at, success) VALUES (?,?,?,?)",
                (deal_name, to_email, datetime.now().isoformat(), success)
            )
    except Exception:
        pass

    if success:
        return jsonify({"sent": True, "to": to_email, "deal_name": deal_name})
    return jsonify({"sent": False, "error": error_msg}), 500


@app.route("/api/architecture/reviewers", methods=["GET"])
def architecture_reviewers():
    """
    Return architect reviewers from ARCHITECT_REVIEWERS env var (comma-separated).
    Format: "Name <email>, Name2 <email2>"  or  "email1, email2"
    """
    raw = os.environ.get("ARCHITECT_REVIEWERS", "")
    if not raw.strip():
        return jsonify({"reviewers": []})
    reviewers = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        import re as _re_rev
        m = _re_rev.match(r'^(.+?)\s*<(.+?)>\s*$', entry)
        if m:
            reviewers.append({"name": m.group(1).strip(), "email": m.group(2).strip()})
        else:
            reviewers.append({"name": entry, "email": entry})
    return jsonify({"reviewers": reviewers})


@app.route("/api/architecture/download-sow", methods=["POST"])
def architecture_download_sow():
    """
    Generate a formatted .docx from the SOW markdown text and stream it as a download.
    Body: {sow_text, deal_name}
    """
    import io
    import re as _re_sow
    try:
        from docx import Document as _DocxDocument
        from docx.shared import Pt as _Pt
    except ImportError:
        return jsonify({"error": "python-docx not installed — run: pip install python-docx"}), 500

    body        = request.get_json(silent=True) or {}
    sow_text    = (body.get("sow_text") or "").strip()
    deal_name   = (body.get("deal_name") or "Statement of Work").strip()
    diagram_b64 = (body.get("diagram_b64") or "").strip()

    if not sow_text:
        return jsonify({"error": "sow_text is required"}), 400

    doc = _DocxDocument()

    # ── Helpers ────────────────────────────────────────────────────────────
    def _flush_table(rows):
        if not rows:
            return
        data_rows = [r for r in rows if not all(c.strip().replace('-', '').replace(':', '') == '' for c in r)]
        if not data_rows:
            return
        n_cols = max(len(r) for r in data_rows)
        tbl = doc.add_table(rows=len(data_rows), cols=n_cols)
        try:
            tbl.style = 'Table Grid'
        except Exception:
            pass
        for ri, row_cells in enumerate(data_rows):
            for ci, cell_text in enumerate(row_cells):
                if ci < n_cols:
                    cell = tbl.rows[ri].cells[ci]
                    cell.text = cell_text.strip()
                    if ri == 0:
                        for para in cell.paragraphs:
                            for run in para.runs:
                                run.bold = True

    # ── Parse markdown line by line ────────────────────────────────────────
    table_rows = []
    for line in sow_text.split('\n'):
        stripped = line.strip()

        # Table row detection (must flush before non-table lines)
        if stripped.startswith('|') and stripped.endswith('|'):
            cells = [c.strip() for c in stripped[1:-1].split('|')]
            table_rows.append(cells)
            continue

        # Any non-table line: flush accumulated table rows first
        _flush_table(table_rows)
        table_rows = []

        if not stripped:
            continue

        # Headings
        if stripped.startswith('### '):
            doc.add_heading(stripped[4:].strip(), level=3)
        elif stripped.startswith('## '):
            heading_text = stripped[3:].strip()
            doc.add_heading(heading_text, level=2)
            # Embed diagram immediately after "Proposed Architecture" heading
            if diagram_b64 and "proposed architecture" in heading_text.lower():
                try:
                    import base64 as _b64
                    from docx.shared import Inches as _Inches
                    _img_bytes = _b64.b64decode(diagram_b64)
                    _img_buf = io.BytesIO(_img_bytes)
                    doc.add_picture(_img_buf, width=_Inches(6.0))
                    doc.add_paragraph()  # spacing after image
                except Exception:
                    pass
        elif stripped.startswith('# ') and not stripped.startswith('## '):
            doc.add_heading(stripped[2:].strip(), level=1)
        # Bullet list
        elif stripped.startswith('- ') or stripped.startswith('* '):
            doc.add_paragraph(stripped[2:].strip(), style='List Bullet')
        # Numbered list
        elif _re_sow.match(r'^\d+\.\s', stripped):
            text = _re_sow.sub(r'^\d+\.\s*', '', stripped)
            doc.add_paragraph(text, style='List Number')
        # Horizontal rule — skip
        elif _re_sow.match(r'^-{3,}$', stripped):
            continue
        # Bold line (section label)
        elif stripped.startswith('**') and stripped.endswith('**'):
            p = doc.add_paragraph()
            run = p.add_run(stripped.strip('*').strip())
            run.bold = True
        # Regular paragraph
        else:
            doc.add_paragraph(stripped)

    # Flush any trailing table
    _flush_table(table_rows)

    # ── Serialise to BytesIO and return ───────────────────────────────────
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    slug = _re_sow.sub(r'[^a-z0-9]+', '_', deal_name.lower()).strip('_')
    date_str = datetime.now().strftime('%Y%m%d')
    filename = f"sow_{slug}_{date_str}.docx"

    from flask import make_response
    resp = make_response(buf.getvalue())
    resp.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


def parse_sow_services(sow_text: str) -> list:
    """
    Extract service names from SOW Section 5 (Azure Services table).
    Table format: | Service | Purpose | Layer |
    Returns unique service names as a list.
    """
    if not sow_text:
        return []

    import re as _re_sow_p

    # Locate Section 5 — Azure Services (handles "5.", "5.0", "5 " numbering)
    sec5 = _re_sow_p.search(
        r'(?:^|\n)#{1,3}\s*5[\d.]*\s+Azure\s+Services?',
        sow_text, _re_sow_p.IGNORECASE,
    )
    if not sec5:
        # Fallback: any heading mentioning "Azure Services"
        sec5 = _re_sow_p.search(
            r'(?:^|\n)#{1,3}[^#\n]*Azure\s+Services?',
            sow_text, _re_sow_p.IGNORECASE,
        )
    if not sec5:
        return []

    # Slice from Section 5 to the next sibling section heading
    fragment = sow_text[sec5.start():]
    next_sec = _re_sow_p.search(r'\n#{1,3}\s+\d+[.\s]', fragment[1:])
    if next_sec:
        fragment = fragment[: next_sec.start() + 1]

    services, seen = [], set()
    for line in fragment.split('\n'):
        stripped = line.strip()
        if not stripped.startswith('|') or not stripped.endswith('|'):
            continue
        cells = [c.strip() for c in stripped[1:-1].split('|')]
        if not cells:
            continue
        svc = cells[0]
        # Skip table header row and separator rows
        if (not svc
                or svc.lower() == 'service'
                or all(ch in '-:| ' for ch in svc)):
            continue
        if svc not in seen:
            services.append(svc)
            seen.add(svc)

    return services


def _build_azure_estimate_tab(ws, gathered: dict, model: dict,
                               pricing: list, _eb_mod) -> None:
    """
    Build the Azure Estimate worksheet using live pricing from azure_pricing.py.
    Matches the style and structure of excel_builder._build_aws_estimate.
    """
    from openpyxl.styles import Alignment, Font
    from datetime import date as _date

    # Reuse style constants from excel_builder
    FILL_HEADER  = _eb_mod.FILL_HEADER
    FILL_SECTION = _eb_mod.FILL_SECTION
    FILL_STRONG  = _eb_mod.FILL_STRONG
    FONT_HEADER  = _eb_mod.FONT_HEADER
    FONT_LABEL   = _eb_mod.FONT_LABEL
    FONT_INPUT   = _eb_mod.FONT_INPUT
    FONT_FORMULA = _eb_mod.FONT_FORMULA
    FONT_XREF    = _eb_mod.FONT_XREF
    FONT_DIM     = _eb_mod.FONT_DIM
    FMT_USD      = _eb_mod.FMT_USD
    FMT_USD2     = _eb_mod.FMT_USD2
    FMT_PCT      = _eb_mod.FMT_PCT

    ws.sheet_properties.tabColor = _eb_mod.TAB_AZURE_ESTIMATE
    ws.freeze_panes = "A4"

    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 38
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["E"].width = 16
    ws.column_dimensions["F"].width = 16
    ws.column_dimensions["G"].width = 10

    # ── Row 1: Header bar ────────────────────────────────────────────────────
    _eb_mod._header_row(ws, 1, [
        "Azure Estimate", "SKU / Description", "Unit Rate ($)", "Unit",
        "Monthly Cost ($)", "Annual Cost ($)", "% of Y1",
    ])
    ws.cell(row=1, column=1).font = FONT_HEADER

    # ── Row 2: Pattern / source note ────────────────────────────────────────
    _eb_mod._section_row(
        ws, 2,
        f"Pattern: {gathered.get('impl_pattern', 'Production Grade')}  ·  "
        "Unit rates from Azure Retail Prices API — eastus (blue = editable).",
        n_cols=7, fill=FILL_SECTION,
    )

    # ── Row 3: Column sub-headers ────────────────────────────────────────────
    sub_labels = ["Service", "SKU / Description", "Unit Rate ($)", "Unit",
                  "Monthly Cost ($)", "Annual Cost ($)", "% of Y1"]
    for i, lbl in enumerate(sub_labels, 1):
        c = ws.cell(row=3, column=i)
        c.value     = lbl
        c.font      = Font(name="Arial", bold=True, size=9, color="FFFFFF")
        c.fill      = FILL_HEADER
        c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[3].height = 16

    # ── Data rows ────────────────────────────────────────────────────────────
    data_start = 4
    if not pricing:
        ws.cell(row=data_start, column=1).value = "No Azure services found in SOW Section 5."
        ws.cell(row=data_start, column=1).font  = FONT_DIM
        data_end  = data_start
        total_row = data_start + 1
    else:
        for idx, p in enumerate(pricing):
            r = data_start + idx

            lc = ws.cell(row=r, column=1)
            lc.value     = p.get("service", "")
            lc.font      = FONT_LABEL
            lc.alignment = Alignment(horizontal="left", vertical="center", indent=1)

            bc = ws.cell(row=r, column=2)
            bc.value     = p.get("sku", "")
            bc.font      = FONT_DIM
            bc.alignment = Alignment(horizontal="left", vertical="center")

            # Unit rate — blue (editable by SA)
            cc = ws.cell(row=r, column=3)
            cc.value          = p.get("unit_rate", 0)
            cc.number_format  = FMT_USD2
            cc.font           = FONT_INPUT
            cc.alignment      = Alignment(horizontal="right", vertical="center")

            dc = ws.cell(row=r, column=4)
            dc.value     = p.get("unit", "—")
            dc.font      = FONT_DIM
            dc.alignment = Alignment(horizontal="center", vertical="center")

            # Monthly cost: hardcoded from API response
            ec = ws.cell(row=r, column=5)
            ec.value         = p.get("monthly_estimate", 0)
            ec.number_format = FMT_USD
            ec.font          = FONT_FORMULA
            ec.alignment     = Alignment(horizontal="right", vertical="center")

            # Annual cost: formula so it updates when SA edits monthly
            fc = ws.cell(row=r, column=6)
            fc.value         = f"=E{r}*12"
            fc.number_format = FMT_USD
            fc.font          = FONT_FORMULA
            fc.alignment     = Alignment(horizontal="right", vertical="center")

            # % of total annual — formula updates live with edits
            gc = ws.cell(row=r, column=7)
            gc.number_format = FMT_PCT
            gc.font          = FONT_DIM
            gc.alignment     = Alignment(horizontal="center", vertical="center")

        data_end  = data_start + len(pricing) - 1
        total_row = data_end + 1

        # Fill % of Y1 formulas after all rows are written (so data_end is known)
        for idx in range(len(pricing)):
            r = data_start + idx
            ws.cell(row=r, column=7).value = (
                f"=IFERROR(F{r}/SUM(F{data_start}:F{data_end}),0)"
            )

        # ── TOTAL row ─────────────────────────────────────────────────────────
        tc = ws.cell(row=total_row, column=1)
        tc.value = "TOTAL"
        tc.font  = Font(name="Arial", bold=True, size=10)
        tc.alignment = Alignment(horizontal="left", vertical="center", indent=1)

        for col, formula, fmt in [
            (5, f"=SUM(E{data_start}:E{data_end})", FMT_USD),
            (6, f"=SUM(F{data_start}:F{data_end})", FMT_USD),
            (7, f"=SUM(G{data_start}:G{data_end})", FMT_PCT),
        ]:
            xc = ws.cell(row=total_row, column=col)
            xc.value         = formula
            xc.number_format = fmt
            xc.font          = Font(name="Arial", bold=True, size=10)
            xc.alignment     = Alignment(horizontal="right")

    # ── 3-Year Projection ─────────────────────────────────────────────────────
    growth_info = (gathered.get("inputs") or {}).get("growth_rate_pct", {})
    growth_rate = float(
        growth_info.get("value", 0.15)
        if isinstance(growth_info, dict) else 0.15
    )

    proj_header_row = total_row + 2
    _eb_mod._section_row(
        ws, proj_header_row,
        "3-YEAR CONSUMPTION PROJECTION", n_cols=7, fill=FILL_STRONG,
    )
    _eb_mod._header_row(
        ws, proj_header_row + 1,
        ["", "Year 1", "Year 2", "Year 3", "3-Year Total", "", ""],
    )

    proj_row = proj_header_row + 2
    ws.cell(row=proj_row, column=1).value = "  Azure Consumption ($)"
    ws.cell(row=proj_row, column=1).font  = FONT_LABEL

    y1_ref = f"F{total_row}"
    for j, formula in enumerate([
        f"={y1_ref}",
        f"={y1_ref}*(1+{growth_rate})",
        f"={y1_ref}*(1+{growth_rate})*(1+{growth_rate})",
    ]):
        c = ws.cell(row=proj_row, column=j + 2)
        c.value         = formula
        c.number_format = FMT_USD
        c.font          = FONT_XREF
        c.alignment     = Alignment(horizontal="right")

    tot_cell = ws.cell(row=proj_row, column=5)
    tot_cell.value         = f"=B{proj_row}+C{proj_row}+D{proj_row}"
    tot_cell.number_format = FMT_USD
    tot_cell.font          = Font(name="Arial", bold=True, size=10)
    tot_cell.alignment     = Alignment(horizontal="right")

    gr_row = proj_row + 1
    ws.cell(row=gr_row, column=1).value = "  Growth rate"
    ws.cell(row=gr_row, column=1).font  = FONT_LABEL
    ws.cell(row=gr_row, column=2).value = "—"
    ws.cell(row=gr_row, column=2).font  = FONT_DIM
    for j in range(2):
        c = ws.cell(row=gr_row, column=j + 3)
        c.value         = growth_rate
        c.number_format = FMT_PCT
        c.font          = FONT_DIM
        c.alignment     = Alignment(horizontal="right")

    # ── Pricing note ─────────────────────────────────────────────────────────
    note_row = gr_row + 2
    note_cell = ws.cell(row=note_row, column=1)
    note_cell.value = (
        f"Pricing sourced from Azure Retail Prices API — eastus region — "
        f"as of {_date.today().strftime('%B %d, %Y')}. "
        "Unit rates are list pricing. Blue cells are editable for negotiated "
        "or reserved instance pricing."
    )
    note_cell.font      = FONT_DIM
    note_cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[note_row].height = 32
    ws.merge_cells(
        start_row=note_row, start_column=1,
        end_row=note_row,   end_column=7,
    )


def generate_sow_financial_model(deal: dict, sow_text: str) -> bytes:
    """
    Generates a complete 6-tab financial model Excel workbook identical to
    excel_builder.py output, but with Tab 5 replaced by Azure services from
    the SOW with live Azure Retail Prices API pricing.

    Returns workbook bytes. Never writes to disk.
    """
    import io as _io
    import os as _os
    import sys as _sys

    _tools_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tools")
    if _tools_dir not in _sys.path:
        _sys.path.insert(0, _tools_dir)

    try:
        from financial_inputs import gather_inputs as _gather_inputs
        from financial_model  import compute       as _compute_model
        import excel_builder as _eb_mod
        from openpyxl import Workbook as _Workbook
    except ImportError as exc:
        raise RuntimeError(
            f"Required module unavailable: {exc}. "
            "Run: pip install openpyxl"
        ) from exc

    from azure_pricing import get_prices_for_sow as _get_prices

    # 1. Gather inputs from deal data
    gathered = _gather_inputs(deal)

    # 2. Compute financial model
    model = _compute_model(gathered)

    # 3. Parse Azure services from SOW
    services = parse_sow_services(sow_text)

    # 4. Fetch Azure pricing
    pricing = _get_prices(services)

    # 5. Build narration (LLM or template fallback)
    api_key = TOGETHER_API_KEY or ""
    if api_key:
        narration = _eb_mod.generate_narration(gathered, model, api_key)
    else:
        narration = {
            "executive_summary": (
                f"3-year ROI: {model['roi_3yr_pct']:.0f}%. "
                f"Payback: {model['payback_months']:.0f} months."
            ),
            "ae_commercial_story": (
                "Present payback period and ROI to anchor pricing conversation."
            ),
            "edp_recommendation": (
                f"Incremental EDP benefit: ${model['incremental_benefit']:,.0f}."
            ),
        }

    # 6. Build workbook — same sheet order as excel_builder.build_workbook
    wb = _Workbook()
    ws_summary    = wb.active
    ws_summary.title = "Summary"
    ws_assumptions  = wb.create_sheet("Assumptions")
    ws_model        = wb.create_sheet("3-Year Model")
    ws_scenarios    = wb.create_sheet("Scenarios")
    ws_azure        = wb.create_sheet("Azure Estimate")   # replaces AWS Estimate
    ws_commercial   = wb.create_sheet("Commercial")

    _eb_mod._build_summary(ws_summary, gathered, model, narration)
    _eb_mod._build_assumptions(ws_assumptions, gathered, model)
    _eb_mod._build_model(ws_model, model)
    model_rows = getattr(ws_model, "_deal_rows", {})
    _eb_mod._build_scenarios(ws_scenarios, model, model_rows)
    _build_azure_estimate_tab(ws_azure, gathered, model, pricing, _eb_mod)
    _eb_mod._build_commercial(ws_commercial, model, narration)

    # 7. Replace "AWS Consumption" label in Assumptions tab
    for row in ws_assumptions.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and "AWS Consumption" in cell.value:
                cell.value = cell.value.replace("AWS Consumption", "Azure Consumption")

    # 8. Replace "AWS" in Summary tab text cells (skip formulas)
    for row in ws_summary.iter_rows():
        for cell in row:
            if (isinstance(cell.value, str)
                    and "AWS" in cell.value
                    and not cell.value.startswith("=")):
                cell.value = cell.value.replace("AWS", "Azure")

    # 9. Serialise to bytes — never write to disk
    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


@app.route("/api/architecture/download-financial-model", methods=["POST"])
def architecture_download_financial_model():
    """
    Generate a SOW-driven Azure financial model Excel workbook and stream
    it as a download.

    Body: { "sow_text": "...", "deal_id": "...", "deal_name": "..." }
    """
    body        = request.get_json(silent=True) or {}
    sow_text    = (body.get("sow_text")  or "").strip()
    deal_id     = (body.get("deal_id")   or "").strip()
    deal_name_q = (body.get("deal_name") or "").strip()

    if not sow_text:
        return jsonify({"error": "sow_text is required"}), 400

    # ── Fetch full deal — pipeline JSON first (has MEDDPICC), then CRM fallback ─
    deal = _fm_find(deal_name=deal_name_q, deal_id=deal_id)
    if not deal:
        crm_deal: dict = {}
        if deal_id:
            try:
                query = f"""
                query {{
                  opportunity(filter: {{ id: {{ eq: "{deal_id}" }} }}) {{
                    id name stage closeDate companyName assignedRep probability
                    amount {{ amountMicros currencyCode }}
                  }}
                }}
                """
                data     = gql(query)
                crm_deal = data.get("opportunity") or {}
                crm_deal["amount"] = (
                    ((crm_deal.get("amount") or {}).get("amountMicros") or 0) / 1_000_000
                )
            except Exception as _crm_err:
                print(f"[arch-fin-model] CRM fetch failed: {_crm_err}")

        deal = {
            "deal_id":       deal_id,
            "deal_name":     crm_deal.get("name")        or deal_name_q,
            "company":       crm_deal.get("companyName") or "",
            "stage":         crm_deal.get("stage")       or "",
            "amount":        float(crm_deal.get("amount") or 0),
            "probability":   float(crm_deal.get("probability") or 50),
            "assigned_rep":  crm_deal.get("assignedRep") or "",
            "overall_score": "NEEDS ATTENTION",
            "elements":      {},
            "flags":         {},
        }

    try:
        xlsx_bytes = generate_sow_financial_model(deal, sow_text)
    except Exception as exc:
        print(f"[arch-fin-model] generation error: {exc}")
        return jsonify({"error": f"Model generation failed: {exc}"}), 500

    slug      = _re.sub(r"[^a-z0-9]+", "_", (deal_name_q or "deal").lower()).strip("_")
    date_str  = datetime.now().strftime("%Y%m%d")
    filename  = f"AzureFinancialModel_{slug}_{date_str}.xlsx"

    from flask import make_response as _mkr
    resp = _mkr(xlsx_bytes)
    resp.headers["Content-Type"] = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


# ── END ARCHITECTURE REVIEW ───────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════
# EVAL SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

_EVAL_DATASET_NAME = "deal-desk-golden-v1"
_LANGSMITH_BASE    = "https://smith.langchain.com"


def _run_pipeline_sync(deal_id: str) -> dict:
    """
    Run the 6-agent pipeline synchronously (no streaming) for a single deal.
    Returns dict with agent text outputs keyed by eval agent name.
    Raises on CRM fetch failure.
    """
    # ── Fetch deal from CRM ────────────────────────────────────────────────
    q = f"""
    query {{
      opportunity(filter: {{ id: {{ eq: "{deal_id}" }} }}) {{
        id name stage amount {{ amountMicros currencyCode }} closeDate createdAt companyName
      }}
    }}
    """
    data  = gql(q)
    deal  = data.get("opportunity") or {}
    if not deal:
        raise ValueError(f"Deal {deal_id} not found in CRM")
    deal["amount"] = ((deal.get("amount") or {}).get("amountMicros") or 0) / 1_000_000
    company_name   = deal.get("companyName") or ""

    # ── Fetch contacts ─────────────────────────────────────────────────────
    pdata = gql("""query { people(first: 50) { edges { node {
        id name { firstName lastName } emails { primaryEmail } jobTitle
        company { name } } } } }""")
    all_people = [e["node"] for e in pdata["people"]["edges"]]
    if company_name:
        contacts = [p for p in all_people if
                    isinstance(p.get("company"), dict) and
                    (p["company"].get("name") == company_name or
                     company_name.lower() in (p.get("company") or {}).get("name", "").lower())]
        contacts = contacts or all_people[:5]
    else:
        contacts = all_people[:5]

    hdata = gql("""query { opportunities(first: 5,
        filter: { stage: { in: [CUSTOMER] } }) {
        edges { node { id name stage amount { amountMicros } } } } }""")
    history = []
    for e in hdata["opportunities"]["edges"]:
        n = e["node"]
        n["amount"] = ((n.get("amount") or {}).get("amountMicros") or 0) / 1_000_000
        history.append(n)

    # ── KB context ─────────────────────────────────────────────────────────
    kb_prefix = ""
    if _KB_AVAILABLE:
        try:
            kctx = build_knowledge_context(deal)
            health_context = _customer_health_summary(deal.get("companyName") or deal.get("company") or "")
            kb_prefix = f"\n{CITATION_INSTRUCTION}\n{kctx}\n{health_context}\n" if (kctx or health_context) else ""
        except Exception:
            health_context = _customer_health_summary(deal.get("companyName") or deal.get("company") or "")
            kb_prefix = f"\n{health_context}\n" if health_context else ""
    else:
        health_context = _customer_health_summary(deal.get("companyName") or deal.get("company") or "")
        kb_prefix = f"\n{health_context}\n" if health_context else ""

    contact_list = "\n".join(
        f"- {c['name']['firstName']} {c['name']['lastName']} | {c.get('jobTitle','N/A')}"
        for c in contacts[:5]
    )
    won = [d for d in history if d.get("stage") == "WON"]

    # ── Agent 2: Risk Analyst ──────────────────────────────────────────────
    risk_prompt = f"""{kb_prefix}You are a senior deal risk analyst at Nexus Solutions, a B2B fintech company.

DEAL: {deal.get('name')}
Stage: {deal.get('stage')} | Amount: ${deal.get('amount', 0):,.0f} | Close: {deal.get('closeDate','Unknown')}
Contacts on file: {len(contacts)}
Historical WON/LOST deals: {len(history)}

KEY CONTACTS:
{contact_list}

Identify all deal risks. Every risk must reference a specific fact from the institutional memory above.

🔴 HIGH RISKS:
[2-3 major risks — cite memory sources]

🟡 MEDIUM RISKS:
[2-3 moderate risks]

⚠️ FLAGS TO WATCH:
[2-3 early warning signals]

RISK VERDICT: [LOW / MEDIUM / HIGH / CRITICAL — one sentence]"""
    risk_text = llm(risk_prompt, 600)

    # ── Agent 3: Opportunity Analyst ──────────────────────────────────────
    opp_prompt = f"""{kb_prefix}You are a senior opportunity analyst at Nexus Solutions.

DEAL: {deal.get('name')}
Stage: {deal.get('stage')} | Amount: ${deal.get('amount', 0):,.0f}
Contacts engaged: {len(contacts)}
Previous WON deals: {len(won)} (value: ${sum(d.get('amount',0) for d in won):,.0f})

Identify strengths and opportunities grounded in the institutional memory above.

✅ KEY STRENGTHS:
[2-3 deal strengths — cite win themes from memory]

🚀 UPSELL / EXPANSION:
[2-3 realistic expansion angles — cite ICP signals from memory]

🤝 RELATIONSHIP SIGNALS:
[assess stakeholder engagement using notes from memory]

💡 WIN STRATEGY:
[one paragraph — clearest path to close, citing specific people and facts]

OPPORTUNITY VERDICT: [LOW / MEDIUM / HIGH confidence — one sentence]"""
    opp_text = llm(opp_prompt, 600)

    # ── Agent 4: Devil's Advocate ──────────────────────────────────────────
    devils_prompt = f"""{kb_prefix}You are a skeptical deal committee member at Nexus Solutions.

THE DEAL: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')}

RISK ANALYST:
{risk_text}

OPPORTUNITY ANALYST:
{opp_text}

Challenge both analyses using specific contradictions from the institutional memory above:

❓ CHALLENGING THE RISK ANALYST:
[2 points where risks may be overstated — cite specific memory facts]

❓ CHALLENGING THE OPPORTUNITY ANALYST:
[2 points where optimism is unwarranted — cite lost deal patterns from memory]

🎯 THE REAL QUESTION:
[single most important unresolved question, derived from memory]

DEVIL'S VERDICT: [YES / CONDITIONAL / NO — one sentence, citing a specific memory fact]"""
    devils_text = llm(devils_prompt, 500)

    # ── Agent 5: Arbitrator ────────────────────────────────────────────────
    arb_prompt = f"""{kb_prefix}You are VP of Sales at Nexus Solutions chairing the deal committee.

Deal: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')} | Close: {deal.get('closeDate','Unknown')}

RISK ANALYST:
{risk_text}

OPPORTUNITY ANALYST:
{opp_text}

DEVIL'S ADVOCATE:
{devils_text}

CONFLICT RESOLUTION:
Risk Analyst verdict: [quote their RISK VERDICT line]
Devil's Advocate verdict: [quote their DEVIL'S VERDICT line]
Where they agree: [1 sentence]
Where they conflict: [1 sentence — the core tension]
How I resolve it: [1 sentence — which view you weight more and why, citing institutional memory]

COMMITTEE DECISION: [PURSUE AGGRESSIVELY / PURSUE WITH CONDITIONS / HOLD / WALK AWAY]

REASONING:
[2-3 sentences referencing the conflict resolution above and MEDDPICC score]

CONDITIONS (if applicable):
[list conditions or "None"]

NEXT ACTIONS:
1. [This week — name a specific person]
2. [Next 30 days]
3. [Escalation trigger]

DEAL SCORE: [X/10] — [one line rationale]"""
    arb_text = llm(arb_prompt, 550)

    # ── Agent 6: Memo Writer (→ sow_writer) ───────────────────────────────
    now = datetime.now().strftime("%B %d, %Y")
    commercial_economics_table = build_commercial_economics_table(deal)
    memo_prompt = f"""{kb_prefix}You are a senior VP writing an executive deal memo. Every section must reference specific facts from the institutional memory above. Name real people. Cite real numbers.
The COMMERCIAL ECONOMICS section below is generated by code from the financial model. Keep that table exactly as provided: do not change metric names, values, formatting, or notes. Reference those values in narrative only when relevant.

DEAL: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')} | Close: {deal.get('closeDate','Unknown')}
Date: {now}

RISK SUMMARY:
{risk_text}

OPPORTUNITY SUMMARY:
{opp_text}

DEVIL'S ADVOCATE:
{devils_text}

COMMITTEE DECISION:
{arb_text}

Write the memo now. Do not paste the prior agents verbatim; synthesize them into a crisp executive artifact.

DEAL DESK MEMO - NEXUS SOLUTIONS
Date: {now}
Deal: {deal.get('name')} | ${deal.get('amount',0):,.0f} | {deal.get('stage')} | Close: {deal.get('closeDate','Unknown')}

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
    memo_text = llm(memo_prompt, 2200)

    enablement_packet = build_pipeline_enablement_packet(
        risk_text=risk_text,
        opp_text=opp_text,
        devils_text=devils_text,
        arb_text=arb_text,
        memo_text=memo_text,
    )

    return {
        "deal":               deal,
        "risk_analyst":       risk_text,
        "devil_advocate":     devils_text,
        "opportunity_analyst":  opp_text,
        "arbitrator":         arb_text,
        "sow_writer":         memo_text,
        "enablement_packet":  enablement_packet,
    }


def _push_eval_to_langsmith(
    ls_client,
    project_name: str,
    deal: dict,
    outputs: dict,
    scoring: dict,
    dataset_name: str,
    example_id: str = "",
) -> str:
    """Create a LangSmith run for one deal eval and attach scoring feedback. Returns run_id."""
    import uuid as _uuid

    run_id = str(_uuid.uuid4())
    deal_name = deal.get("name", "unknown")

    try:
        now = datetime.now()
        create_kwargs = dict(
            name=f"eval — {deal_name}",
            run_type="chain",
            inputs={"deal": deal},
            outputs={"agent_outputs": outputs, "scoring": scoring},
            project_name=project_name,
            run_id=run_id,
            start_time=now,
            end_time=now,
        )
        if example_id:
            create_kwargs["reference_example_id"] = example_id
        ls_client.create_run(**create_kwargs)

        # Attach per-agent and overall scores as feedback
        feedback_map = {
            "risk_analyst_score":         scoring.get("agent_totals", {}).get("risk_analyst", 0),
            "devil_advocate_score":        scoring.get("agent_totals", {}).get("devil_advocate", 0),
            "arbitrator_score":            scoring.get("agent_totals", {}).get("arbitrator", 0),
            "sow_writer_score":            scoring.get("agent_totals", {}).get("sow_writer", 0),
            "pipeline_score":              scoring.get("pipeline_total", 0),
            "final_score":                 scoring.get("final_score", 0),
        }
        enablement_eval = scoring.get("enablement_eval") or {}
        if enablement_eval:
            feedback_map.update({
                "enablement_groundedness_score": enablement_eval.get("groundedness_score", 0),
                "enablement_specificity_score":  enablement_eval.get("specificity_score", 0),
                "enablement_actionability_score": enablement_eval.get("actionability_score", 0),
                "enablement_sku_match_gate": 1 if enablement_eval.get("sku_match_gate") else 0,
                "enablement_grounding_gate": 1 if enablement_eval.get("grounding_gate") else 0,
                "enablement_passed": 1 if enablement_eval.get("passed") else 0,
            })
        for key, score in feedback_map.items():
            ls_client.create_feedback(
                run_id=run_id,
                key=key,
                score=score,
            )
        # Pass/fail
        ls_client.create_feedback(
            run_id=run_id,
            key="passed",
            score=1 if scoring.get("passed") else 0,
        )
    except Exception as exc:
        print(f"[eval] LangSmith push failed for {deal_name}: {exc}")

    return run_id


@app.route("/api/evals/run", methods=["GET"])
def run_evals():
    """
    Trigger a full eval run across the deal-desk-golden-v1 dataset.
    Streams SSE progress events. Final event type 'eval_complete' carries summary.
    """
    def generate():
        def se(event: str, data: dict):
            yield f"event: {event}\ndata: {json.dumps(data)}\n\n"

        eval_run_id  = str(uuid.uuid4())
        started_at   = datetime.now()
        results      = []
        ls_client    = _LSClient()
        project_name = f"deal-desk-eval-{started_at.strftime('%Y%m%d-%H%M%S')}"

        yield from se("eval_start", {
            "run_id":   eval_run_id,
            "dataset":  _EVAL_DATASET_NAME,
            "project":  project_name,
        })

        # ── Create LangSmith experiment project linked to dataset ──────────
        ls_project_id  = None
        experiment_url = ""
        try:
            dataset   = ls_client.read_dataset(dataset_name=_EVAL_DATASET_NAME)
            ls_project = ls_client.create_project(
                project_name=project_name,
                reference_dataset_id=dataset.id,
            )
            ls_project_id  = str(ls_project.id)
            experiment_url = f"{_LANGSMITH_BASE}/projects/p/{ls_project_id}"
            yield from se("eval_progress", {
                "msg": f"LangSmith experiment created: {project_name}",
                "experiment_url": experiment_url,
            })
        except Exception as exc:
            yield from se("eval_progress", {"msg": f"LangSmith setup warning: {exc}"})

        # ── Load examples from dataset ─────────────────────────────────────
        examples = []
        try:
            for ex in ls_client.list_examples(dataset_name=_EVAL_DATASET_NAME):
                examples.append(ex)
            yield from se("eval_progress", {"msg": f"Loaded {len(examples)} examples from dataset"})
        except Exception as exc:
            yield from se("eval_error", {"msg": f"Failed to load dataset: {exc}"})
            return

        if not examples:
            yield from se("eval_error", {"msg": "Dataset is empty. Run: python tools/eval_dataset.py"})
            return

        # ── Evaluate each deal ─────────────────────────────────────────────
        for idx, example in enumerate(examples, 1):
            deal_input = (example.inputs or {}).get("deal", {})
            deal_id    = deal_input.get("id", "")
            deal_name  = deal_input.get("name", "unknown")
            stage      = deal_input.get("stage", "")

            yield from se("eval_progress", {
                "msg":     f"[{idx}/{len(examples)}] Running pipeline: {deal_name}",
                "current": idx,
                "total":   len(examples),
            })

            try:
                pipeline_outputs = _run_pipeline_sync(deal_id)
                live_deal = pipeline_outputs.pop("deal", deal_input)
            except Exception as exc:
                print(f"[eval] Pipeline failed for {deal_name}: {exc}")
                yield from se("eval_progress", {
                    "msg":    f"  ✗ Pipeline failed: {deal_name} — {exc}",
                    "failed": True,
                })
                continue

            yield from se("eval_progress", {
                "msg": f"  ✓ Pipeline done. Running judge…",
            })

            scoring = {}
            try:
                scoring = _judge_run(live_deal, pipeline_outputs)
            except Exception as exc:
                print(f"[eval] Judge failed for {deal_name}: {exc}")
                yield from se("eval_progress", {"msg": f"  ✗ Judge failed: {exc}"})

            try:
                enablement_rec = recommend_for_deal(
                    company=live_deal.get("companyName") or live_deal.get("company", ""),
                    deal=live_deal.get("name") or deal_name,
                    pipeline_packet=pipeline_outputs.get("enablement_packet", {}),
                )
                pipeline_outputs["enablement_agent"] = enablement_rec
                scoring["enablement_eval"] = enablement_rec.get("evaluation", {})
                yield from se("eval_progress", {
                    "msg": (
                        "  ✓ Enablement eval: "
                        f"grounded={scoring['enablement_eval'].get('groundedness_score', 0)} "
                        f"specific={scoring['enablement_eval'].get('specificity_score', 0)} "
                        f"actionable={scoring['enablement_eval'].get('actionability_score', 0)}"
                    ),
                })
            except Exception as exc:
                scoring["enablement_eval"] = {"passed": False, "failure_reasons": [str(exc)]}
                yield from se("eval_progress", {"msg": f"  ✗ Enablement eval failed: {exc}"})

            # Push to LangSmith — pass example_id so run appears inside the experiment
            _push_eval_to_langsmith(
                ls_client, project_name, live_deal,
                pipeline_outputs, scoring, _EVAL_DATASET_NAME,
                example_id=str(example.id) if example.id else "",
            )

            row = {
                "deal_id":                  deal_id,
                "deal_name":                deal_name,
                "stage":                    stage,
                "risk_analyst_score":       scoring.get("agent_totals", {}).get("risk_analyst", 0),
                "devil_advocate_score":     scoring.get("agent_totals", {}).get("devil_advocate", 0),
                "financial_simulation_score": 0,
                "arbitrator_score":         scoring.get("agent_totals", {}).get("arbitrator", 0),
                "sow_writer_score":         scoring.get("agent_totals", {}).get("sow_writer", 0),
                "pipeline_score":           scoring.get("pipeline_total", 0),
                "raw_total":                scoring.get("raw_total", 0),
                "final_score":              scoring.get("final_score", 0),
                "passed":                   scoring.get("passed", False),
                "full_json":                json.dumps(scoring),
            }
            results.append(row)

            yield from se("eval_deal_result", {
                "deal_name":   deal_name,
                "stage":       stage,
                "final_score": row["final_score"],
                "passed":      row["passed"],
            })

        # ── Write to SQLite ────────────────────────────────────────────────
        pass_count  = sum(1 for r in results if r["passed"])
        fail_count  = len(results) - pass_count
        avg_score   = round(sum(r["final_score"] for r in results) / max(len(results), 1), 2)

        with get_db() as con:
            con.execute(
                """INSERT INTO eval_runs VALUES (?,?,?,?,?,?,?,?)""",
                (eval_run_id, started_at.isoformat(), _EVAL_DATASET_NAME,
                 len(results), avg_score, pass_count, fail_count, experiment_url),
            )
            for r in results:
                con.execute(
                    """INSERT INTO eval_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (eval_run_id, r["deal_id"], r["deal_name"], r["stage"],
                     r["risk_analyst_score"], r["devil_advocate_score"],
                     r["financial_simulation_score"], r["arbitrator_score"],
                     r["sow_writer_score"], r["pipeline_score"],
                     r["raw_total"], r["final_score"], r["passed"], r["full_json"]),
                )

        yield from se("eval_complete", {
            "run_id":          eval_run_id,
            "deals_evaluated": len(results),
            "average_score":   avg_score,
            "pass_count":      pass_count,
            "fail_count":      fail_count,
            "experiment_url":  experiment_url,
        })

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/evals/results", methods=["GET"])
def get_eval_results():
    """Return results for the most recent eval run."""
    with get_db() as con:
        run = con.execute(
            "SELECT * FROM eval_runs ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if not run:
            return jsonify({"results": [], "summary": None})
        run_id = run["run_id"]
        rows = con.execute(
            "SELECT * FROM eval_results WHERE run_id = ? ORDER BY final_score DESC",
            (run_id,),
        ).fetchall()

    results = [dict(r) for r in rows]
    # Aggregate criterion fail frequency
    fail_counts: dict[str, int] = {}
    for r in results:
        fj = json.loads(r.get("full_json") or "{}")
        for agent_key, criteria in fj.get("agent_scores", {}).items():
            for crit, val in criteria.items():
                if val == 0:
                    fail_counts[f"{agent_key}.{crit}"] = fail_counts.get(f"{agent_key}.{crit}", 0) + 1
        for crit, val in fj.get("pipeline_scores", {}).items():
            if val == 0:
                fail_counts[f"pipeline.{crit}"] = fail_counts.get(f"pipeline.{crit}", 0) + 1

    top_failures = sorted(fail_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    return jsonify({
        "summary":      dict(run),
        "results":      results,
        "top_failures": top_failures,
    })


@app.route("/api/evals/history", methods=["GET"])
def get_eval_history():
    """Return all eval run summaries."""
    with get_db() as con:
        rows = con.execute(
            "SELECT * FROM eval_runs ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
    return jsonify({"runs": [dict(r) for r in rows]})


# ── END EVAL SYSTEM ───────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("\n" + "█"*50)
    print("  DEAL DESK AGENT — UI Server")
    print("  http://localhost:5001")
    print("█"*50 + "\n")
    app.run(debug=False, port=5001, threaded=True, use_reloader=False)
