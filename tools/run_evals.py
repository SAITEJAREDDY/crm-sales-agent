"""
run_evals.py
Standalone eval runner — no Flask required.

Usage:
    python tools/run_evals.py               # run all deals in dataset
    python tools/run_evals.py --dry-run     # pipeline only, skip judge

What it does:
  1. Loads examples from LangSmith dataset 'deal-desk-golden-v1'
  2. Runs the full 6-agent pipeline for each deal (calls Twenty CRM + Together AI)
  3. Scores each agent with the configured Qwen judge
  4. Pushes results to LangSmith as a new experiment (status=success, reference_example_id set)
  5. Writes results to metrics.db SQLite (eval_runs + eval_results tables)
     so the UI Evals tab auto-populates on next page load
"""
from __future__ import annotations

try:
    from tools.llm_client import TOGETHER_MODEL, create_completion
except ModuleNotFoundError:
    from llm_client import TOGETHER_MODEL, create_completion

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_DIR  = Path(__file__).resolve().parent.parent
TOOLS_DIR = BASE_DIR / "tools"
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(TOOLS_DIR))

import requests
from langsmith import Client as LSClient, traceable
from langsmith.run_helpers import get_current_run_tree

try:
    from enablement_agent import recommend_for_deal
except Exception as _enablement_import_err:
    print(f"[enablement] unavailable: {_enablement_import_err}")
    def recommend_for_deal(*args, **kwargs):
        raise RuntimeError("Enablement Agent unavailable")

# ── per-run metrics accumulator ───────────────────────────────────────────────
# keyed by agent name; each entry: {"latency_ms": float, "tokens_in": int,
#                                    "tokens_out": int, "error": bool}
_AGENT_STATS: list[dict] = []

def _record(agent: str, latency_ms: float, tokens_in: int, tokens_out: int, error: bool) -> None:
    _AGENT_STATS.append({
        "agent": agent,
        "latency_ms": latency_ms,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "error": error,
    })

# ── CRM ───────────────────────────────────────────────────────────────────────
TWENTY_URL   = "http://localhost:3000/graphql"
TWENTY_TOKEN = os.environ.get("TWENTY_API_TOKEN", "")
TWENTY_HEADERS = {
    "Authorization": f"Bearer {TWENTY_TOKEN}",
    "Content-Type": "application/json",
}

TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")
DATASET_NAME     = "deal-desk-golden-v1"
DB_PATH          = BASE_DIR / "metrics.db"
LANGSMITH_BASE   = "https://smith.langchain.com"

# ── helpers ───────────────────────────────────────────────────────────────────

def _gql(query: str) -> dict:
    r = requests.post(TWENTY_URL, headers=TWENTY_HEADERS, json={"query": query}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "errors" in data and data.get("data", {}).get("opportunity") is None:
        raise ValueError(f"GQL error: {data['errors']}")
    return data.get("data", {})


@traceable(run_type="llm", name="pipeline-llm")
def _llm(prompt: str, max_tokens: int = 700, agent: str = "") -> str:
    from together import Together
    t0 = time.time()
    error = False
    tokens_in = tokens_out = 0
    try:
        client = Together(api_key=TOGETHER_API_KEY)
        resp = create_completion(client,
            model=TOGETHER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=max_tokens,
        )
        content = (resp.choices[0].message.content or "").strip()

        if resp.usage:
            tokens_in  = resp.usage.prompt_tokens
            tokens_out = resp.usage.completion_tokens
            # attach to the active LangSmith span
            rt = get_current_run_tree()
            if rt:
                rt.outputs = {
                    "output": content,
                    "usage": {
                        "prompt_tokens":     tokens_in,
                        "completion_tokens": tokens_out,
                        "total_tokens":      resp.usage.total_tokens,
                    },
                }

        return content
    except Exception:
        error = True
        raise
    finally:
        _record(agent or "unknown", (time.time() - t0) * 1000, tokens_in, tokens_out, error)


# ── pipeline ──────────────────────────────────────────────────────────────────

@traceable(run_type="chain", name="deal-pipeline")
def run_pipeline(deal_id: str) -> dict:
    q = f"""query {{
      opportunity(filter: {{ id: {{ eq: "{deal_id}" }} }}) {{
        id name stage amount {{ amountMicros currencyCode }} closeDate companyName
      }}
    }}"""
    data = _gql(q)
    deal = data.get("opportunity") or {}
    if not deal:
        raise ValueError(f"Deal {deal_id} not found")
    deal["amount"] = ((deal.get("amount") or {}).get("amountMicros") or 0) / 1_000_000

    company = deal.get("companyName", "")

    pdata = _gql("""query { people(first: 50) { edges { node {
        name { firstName lastName } jobTitle company { name } } } } }""")
    all_people = [e["node"] for e in pdata["people"]["edges"]]
    contacts = [p for p in all_people if isinstance(p.get("company"), dict) and
                company.lower() in (p["company"].get("name") or "").lower()]
    contacts = contacts or all_people[:5]
    contact_list = "\n".join(
        f"- {c['name']['firstName']} {c['name']['lastName']} | {c.get('jobTitle','N/A')}"
        for c in contacts[:5]
    )

    amt  = deal.get("amount", 0)
    stage = deal.get("stage", "")
    name  = deal.get("name", "")
    close = deal.get("closeDate", "Unknown")

    # Agent 2 — Risk
    risk_text = _llm(f"""You are a senior deal risk analyst at Nexus Solutions.
DEAL: {name} | Stage: {stage} | Amount: ${amt:,.0f} | Close: {close}
KEY CONTACTS:\n{contact_list}

🔴 HIGH RISKS: [2-3 major risks]
🟡 MEDIUM RISKS: [2-3 moderate risks]
⚠️ FLAGS TO WATCH: [2-3 early warning signals]
RISK VERDICT: [LOW / MEDIUM / HIGH / CRITICAL]""", 500, agent="risk_analyst")

    # Agent 3 — Opportunity
    opp_text = _llm(f"""You are a senior opportunity analyst at Nexus Solutions.
DEAL: {name} | Stage: {stage} | Amount: ${amt:,.0f}
✅ KEY STRENGTHS: [2-3 strengths]
🚀 UPSELL / EXPANSION: [2-3 angles]
💡 WIN STRATEGY: [path to close]
OPPORTUNITY VERDICT: [LOW / MEDIUM / HIGH confidence]""", 500, agent="opportunity_analyst")

    # Agent 4 — Devil's Advocate
    devils_text = _llm(f"""You are a skeptical deal committee member at Nexus Solutions.
DEAL: {name} | ${amt:,.0f} | {stage}
RISK ANALYST: {risk_text[:400]}
OPPORTUNITY: {opp_text[:400]}
❓ CHALLENGING THE RISK ANALYST: [2 counter-points]
❓ CHALLENGING THE OPPORTUNITY ANALYST: [2 counter-points]
🎯 THE REAL QUESTION: [single unresolved issue]
DEVIL'S VERDICT: [YES / CONDITIONAL / NO]""", 450, agent="devil_advocate")

    # Agent 5 — Arbitrator
    arb_text = _llm(f"""You are VP of Sales at Nexus Solutions chairing the deal committee.
Deal: {name} | ${amt:,.0f} | {stage} | Close: {close}

RISK ANALYST: {risk_text[:300]}
OPPORTUNITY ANALYST: {opp_text[:300]}
DEVIL'S ADVOCATE: {devils_text[:300]}

CONFLICT RESOLUTION:
Risk Analyst verdict: [quote their RISK VERDICT line]
Devil's Advocate verdict: [quote their DEVIL'S VERDICT line]
Where they agree: [1 sentence]
Where they conflict: [1 sentence — the core tension]
How I resolve it: [1 sentence — which view you weight more and why]

COMMITTEE DECISION: [PURSUE AGGRESSIVELY / PURSUE WITH CONDITIONS / HOLD / WALK AWAY]
REASONING: [2-3 sentences referencing the conflict resolution above]
CONDITIONS: [list or "None"]
NEXT ACTIONS:
1. [This week]
2. [Next 30 days]
3. [Escalation trigger]
DEAL SCORE: [X/10] — [one line rationale]""", 550, agent="arbitrator")

    # Agent 6 — Memo
    memo_text = _llm(f"""You are a senior VP writing an executive deal memo.
DEAL: {name} | ${amt:,.0f} | {stage} | Close: {close}
Date: {datetime.now().strftime('%B %d, %Y')}
RISK: {risk_text[:250]}
OPPORTUNITY: {opp_text[:250]}
COMMITTEE DECISION: {arb_text[:350]}

DEAL DESK MEMO - NEXUS SOLUTIONS
EXECUTIVE SUMMARY
[2-3 sentences]

RISK ASSESSMENT
- Risk 1:
- Risk 2:
- Risk 3:

OPPORTUNITY ASSESSMENT
- Strength 1:
- Strength 2:
- Strength 3:

SYNTHESIS
[One conclusion that emerges only when combining risk + opportunity + committee views — something no single agent stated alone. What does the full picture reveal?]

COMMITTEE RECOMMENDATION
[State the decision and resolve any tension between risk and opportunity assessments]

NEXT ACTIONS
1.
2.
3.

DEAL SCORE: X/10""", 1000, agent="sow_writer")

    return {
        "deal":                 deal,
        "risk_analyst":         risk_text,
        "devil_advocate":       devils_text,
        "opportunity_analyst":  opp_text,
        "arbitrator":           arb_text,
        "sow_writer":           memo_text,
    }


# ── LangSmith push ────────────────────────────────────────────────────────────

def push_run_to_langsmith(
    ls: LSClient,
    project_name: str,
    deal: dict,
    outputs: dict,
    scoring: dict,
    example_id: str,
) -> None:
    run_id = str(uuid.uuid4())
    now    = datetime.now()
    try:
        ls.create_run(
            name=f"eval — {deal.get('name', '?')}",
            run_type="chain",
            inputs={"deal": deal},
            outputs={
                "scoring": {k: v for k, v in scoring.items() if k != "agent_scores"},
                "agent_totals": scoring.get("agent_totals", {}),
            },
            project_name=project_name,
            run_id=run_id,
            reference_example_id=example_id,
            start_time=now,
            end_time=now,
        )
        feedback = {
            "risk_analyst_score":          scoring.get("agent_totals", {}).get("risk_analyst", 0),
            "devil_advocate_score":        scoring.get("agent_totals", {}).get("devil_advocate", 0),
            "arbitrator_score":            scoring.get("agent_totals", {}).get("arbitrator", 0),
            "sow_writer_score":            scoring.get("agent_totals", {}).get("sow_writer", 0),
            "pipeline_score":              scoring.get("pipeline_total", 0),
            "final_score":                 scoring.get("final_score", 0),
            "passed":                      1 if scoring.get("passed") else 0,
        }
        enablement_eval = scoring.get("enablement_eval") or {}
        if enablement_eval:
            feedback.update({
                "enablement_groundedness_score": enablement_eval.get("groundedness_score", 0),
                "enablement_specificity_score":  enablement_eval.get("specificity_score", 0),
                "enablement_actionability_score": enablement_eval.get("actionability_score", 0),
                "enablement_sku_match_gate": 1 if enablement_eval.get("sku_match_gate") else 0,
                "enablement_grounding_gate": 1 if enablement_eval.get("grounding_gate") else 0,
                "enablement_passed": 1 if enablement_eval.get("passed") else 0,
            })
        for key, score in feedback.items():
            ls.create_feedback(run_id=run_id, key=key, score=score)
    except Exception as exc:
        print(f"  [langsmith] push failed for {deal.get('name')}: {exc}")


# ── SQLite write ──────────────────────────────────────────────────────────────

def write_to_sqlite(eval_run_id: str, results: list[dict], avg_score: float,
                    pass_count: int, fail_count: int, experiment_url: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO eval_runs VALUES (?,?,?,?,?,?,?,?)",
            (eval_run_id, datetime.now().isoformat(), DATASET_NAME,
             len(results), avg_score, pass_count, fail_count, experiment_url),
        )
        for r in results:
            con.execute(
                "INSERT INTO eval_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eval_run_id, r["deal_id"], r["deal_name"], r["stage"],
                 r["risk_analyst_score"], r["devil_advocate_score"],
                 r["financial_simulation_score"], r["arbitrator_score"],
                 r["sow_writer_score"], r["pipeline_score"],
                 r["raw_total"], r["final_score"], r["passed"], r["full_json"]),
            )
        con.commit()


# ── main ──────────────────────────────────────────────────────────────────────

def _compute_tool_stats() -> dict[str, dict]:
    """Aggregate _AGENT_STATS into per-tool summary."""
    by_agent: dict[str, list[dict]] = {}
    for r in _AGENT_STATS:
        by_agent.setdefault(r["agent"], []).append(r)
    summary = {}
    for agent, records in by_agent.items():
        latencies  = [r["latency_ms"] for r in records if not r["error"]]
        errors     = [r for r in records if r["error"]]
        tokens_out = [r["tokens_out"] for r in records if not r["error"]]
        summary[agent] = {
            "run_count":        len(records),
            "error_count":      len(errors),
            "error_rate":       round(len(errors) / len(records), 3) if records else 0,
            "median_latency_ms": round(statistics.median(latencies), 1) if latencies else 0,
            "p95_latency_ms":   round(sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) >= 2 else (latencies[0] if latencies else 0), 1),
            "total_tokens_out": sum(tokens_out),
            "median_tokens_out": round(statistics.median(tokens_out), 1) if tokens_out else 0,
        }
    return summary


def main(dry_run: bool = False) -> None:
    from eval_judge import judge_run

    global _AGENT_STATS
    _AGENT_STATS = []  # reset for this run

    ls = LSClient()
    timestamp    = datetime.now().strftime("%Y%m%d-%H%M%S")
    project_name = f"deal-desk-eval-{timestamp}"
    eval_run_id  = str(uuid.uuid4())

    # Create LangSmith experiment
    experiment_url = ""
    try:
        dataset    = ls.read_dataset(dataset_name=DATASET_NAME)
        ls_project = ls.create_project(project_name=project_name, reference_dataset_id=dataset.id)
        experiment_url = f"{LANGSMITH_BASE}/projects/p/{ls_project.id}"
        print(f"[langsmith] experiment: {project_name}")
        print(f"            {experiment_url}\n")
    except Exception as exc:
        print(f"[langsmith] project creation failed: {exc}")

    # Load examples
    examples = list(ls.list_examples(dataset_name=DATASET_NAME))
    print(f"[dataset] {len(examples)} examples loaded\n")

    results: list[dict] = []

    for idx, example in enumerate(examples, 1):
        deal_input = (example.inputs or {}).get("deal", {})
        deal_id    = deal_input.get("id", "")
        deal_name  = deal_input.get("name", "?")
        stage      = deal_input.get("stage", "")

        print(f"[{idx}/{len(examples)}] {deal_name} ({stage})")

        # Run pipeline
        try:
            pipeline_out = run_pipeline(deal_id)
            live_deal    = pipeline_out.pop("deal", deal_input)
            print(f"  ✓ pipeline done")
        except Exception as exc:
            print(f"  ✗ pipeline failed: {exc}")
            continue

        # Judge
        scoring: dict = {}
        if not dry_run:
            try:
                scoring = judge_run(live_deal, pipeline_out)
                print(f"  ✓ judge: {scoring.get('final_score', 0):.1f}/100  {'PASS' if scoring.get('passed') else 'FAIL'}")
            except Exception as exc:
                print(f"  ✗ judge failed: {exc}")

        try:
            enablement_rec = recommend_for_deal(
                company=live_deal.get("companyName") or live_deal.get("company", ""),
                deal=live_deal.get("name") or deal_name,
            )
            pipeline_out["enablement_agent"] = enablement_rec
            scoring["enablement_eval"] = enablement_rec.get("evaluation", {})
            ev = scoring["enablement_eval"]
            print(
                "  ✓ enablement: "
                f"grounded={ev.get('groundedness_score', 0)} "
                f"specific={ev.get('specificity_score', 0)} "
                f"actionable={ev.get('actionability_score', 0)} "
                f"{'PASS' if ev.get('passed') else 'FAIL'}"
            )
        except Exception as exc:
            scoring["enablement_eval"] = {"passed": False, "failure_reasons": [str(exc)]}
            print(f"  ✗ enablement failed: {exc}")

        # Push to LangSmith
        push_run_to_langsmith(ls, project_name, live_deal, pipeline_out, scoring, str(example.id))

        row = {
            "deal_id":                    deal_id,
            "deal_name":                  deal_name,
            "stage":                      stage,
            "risk_analyst_score":         scoring.get("agent_totals", {}).get("risk_analyst", 0),
            "devil_advocate_score":       scoring.get("agent_totals", {}).get("devil_advocate", 0),
            "financial_simulation_score": 0,
            "arbitrator_score":           scoring.get("agent_totals", {}).get("arbitrator", 0),
            "sow_writer_score":           scoring.get("agent_totals", {}).get("sow_writer", 0),
            "pipeline_score":             scoring.get("pipeline_total", 0),
            "raw_total":                  scoring.get("raw_total", 0),
            "final_score":                scoring.get("final_score", 0),
            "passed":                     scoring.get("passed", False),
            "full_json":                  json.dumps(scoring),
        }
        results.append(row)

    if not results:
        print("\n[done] No results — all deals failed pipeline.")
        return

    pass_count = sum(1 for r in results if r["passed"])
    fail_count = len(results) - pass_count
    avg_score  = round(sum(r["final_score"] for r in results) / len(results), 2)

    # Write to SQLite
    try:
        write_to_sqlite(eval_run_id, results, avg_score, pass_count, fail_count, experiment_url)
        print(f"\n[sqlite] written to {DB_PATH.name}")
    except Exception as exc:
        print(f"\n[sqlite] write failed: {exc}")

    # ── Tool-level stats ──────────────────────────────────────────────────────
    tool_stats = _compute_tool_stats()

    # Push aggregate metrics to LangSmith as feedback on the experiment project
    _AGENT_ORDER = ["risk_analyst", "devil_advocate", "arbitrator", "sow_writer"]
    if experiment_url:
        try:
            for agent, s in tool_stats.items():
                for key, val in s.items():
                    ls.create_feedback(
                        run_id=eval_run_id,
                        key=f"{agent}__{key}",
                        score=float(val),
                        source_info={"source": "run_evals_aggregate"},
                    )
        except Exception as exc:
            print(f"[langsmith] aggregate feedback push failed: {exc}")

    # Print tool stats table
    print(f"\n{'─'*85}")
    print(f"  {'Tool':<26} {'Runs':>5}  {'Errors':>6}  {'Err%':>5}  {'Med Lat(ms)':>11}  {'Med ToksOut':>11}  {'TotalToksOut':>12}")
    print(f"{'─'*85}")
    for agent in _AGENT_ORDER:
        if agent not in tool_stats:
            continue
        s = tool_stats[agent]
        print(
            f"  {agent:<26} {s['run_count']:>5}  {s['error_count']:>6}  "
            f"{s['error_rate']*100:>4.1f}%  {s['median_latency_ms']:>11.0f}  "
            f"{s['median_tokens_out']:>11.0f}  {s['total_tokens_out']:>12}"
        )
    total_tokens_out = sum(s["total_tokens_out"] for s in tool_stats.values())
    print(f"{'─'*85}")
    print(f"  {'TOTAL OUTPUT TOKENS':<26} {total_tokens_out:>56}")

    print(f"\n{'='*55}")
    print(f"  Run: {project_name}")
    print(f"  Deals: {len(results)}  |  Avg: {avg_score:.1f}  |  Pass: {pass_count}  Fail: {fail_count}")
    print(f"  LangSmith: {experiment_url}")
    print(f"  UI: http://localhost:5001  →  Evals tab")
    print(f"{'='*55}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip judge, pipeline only")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
