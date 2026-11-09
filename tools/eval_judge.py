"""
eval_judge.py
Together AI judge for Deal Desk Agent eval runs.

Each agent is scored on exactly 4 criteria with strict Literal[0, 2.5, 5] values
enforced by Pydantic and Together AI JSON schema structured output.

Score mapping:
  0   = Fail
  2.5 = Partial pass
  5   = Full pass

Thresholds:
  Agent pass:    agent total >= 10  (out of 20)
  Pipeline pass: pipeline total >= 10 (out of 20)
  Overall pass:  final_score >= 70  (raw_total, max 100)
  Raw total max: 100  (4 agents × 20 + 20 pipeline)

Agent → pipeline mapping:
  risk_analyst         ← Risk Analyst output
  devil_advocate       ← Devil's Advocate output
  arbitrator           ← Deal Arbitrator output
  sow_writer           ← Memo Writer output (final deal memo)
"""
from __future__ import annotations

try:
    from tools.llm_client import TOGETHER_MODEL, create_completion
except ModuleNotFoundError:
    from llm_client import TOGETHER_MODEL, create_completion

import json
import os
import sys
from typing import Any, Literal

from dotenv import load_dotenv
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from pydantic import BaseModel

load_dotenv()

JUDGE_MODEL = TOGETHER_MODEL

# Strict score type — only these three values are legal
Score = Literal[0, 2.5, 5]


# ── Per-agent Pydantic schemas ────────────────────────────────────────────────

class RiskAnalystScore(BaseModel):
    groundedness:        Score
    risk_completeness:   Score
    context_utilization: Score
    hallucination_check: Score


class DevilAdvocateScore(BaseModel):
    role_adherence:    Score
    specificity:       Score
    groundedness:      Score
    pushback_strength: Score


class ArbitratorScore(BaseModel):
    synthesis_quality:      Score
    recommendation_clarity: Score
    groundedness:           Score
    completeness:           Score


class SOWWriterScore(BaseModel):
    deal_alignment:         Score
    structure_completeness: Score
    groundedness:           Score
    consistency:            Score


class PipelineLevelScore(BaseModel):
    pipeline_coherence:      Score
    contradiction_detection: Score
    deal_data_thread:        Score
    final_memo_quality:      Score


# ── Schema registry ───────────────────────────────────────────────────────────

_SCHEMAS: dict[str, type[BaseModel]] = {
    "risk_analyst":         RiskAnalystScore,
    "devil_advocate":       DevilAdvocateScore,
    "arbitrator":           ArbitratorScore,
    "sow_writer":           SOWWriterScore,
    "pipeline":             PipelineLevelScore,
}

_RUBRIC_HINTS: dict[str, str] = {
    "risk_analyst": (
        "groundedness: every risk must cite a specific, verifiable deal fact. "
        "risk_completeness: are all obvious risk categories (financial, stakeholder, timing, competition) covered? "
        "context_utilization: does the agent use CRM data (amount, stage, contacts, close date)? "
        "hallucination_check: does the agent invent contacts, events, or numbers not present in the deal?"
    ),
    "devil_advocate": (
        "role_adherence: does the agent genuinely challenge prior risk and opportunity analyses, not agree with them? "
        "specificity: are counter-arguments tied to specific named facts or people from the deal? "
        "groundedness: are challenges anchored in actual deal data, not generic objections? "
        "pushback_strength: does the agent surface at least 2 substantive, non-trivial counter-arguments?"
    ),
    "arbitrator": (
        "synthesis_quality: does the arbitrator clearly synthesize inputs from prior agents rather than repeat one? "
        "recommendation_clarity: is the committee decision explicit (PURSUE / HOLD / WALK AWAY) with clear rationale? "
        "groundedness: is the rationale tied to named people, specific risks, and deal facts? "
        "completeness: are next actions, conditions (if any), and a deal score explicitly stated?"
    ),
    "sow_writer": (
        "deal_alignment: does the memo reference the actual deal name, stage, and amount correctly? "
        "structure_completeness: does the memo include all required sections (Executive Summary, Risk, Opportunity, Recommendation, Actions, Score)? "
        "groundedness: are risk and opportunity bullets backed by citations from prior agent outputs? "
        "consistency: do the risk assessment, opportunity assessment, and recommendation sections tell a coherent story?"
    ),
    "pipeline": (
        "pipeline_coherence: do the 4 evaluated outputs form a logical, escalating narrative arc from risk to decision to memo? "
        "contradiction_detection: when the risk analyst and devil's advocate disagree, does the arbitrator explicitly acknowledge the tension and explain how it was resolved? score 5 if resolved, 2.5 if partially addressed, 0 if ignored. "
        "deal_data_thread: is the deal amount, stage, company, and close date cited consistently across all outputs? "
        "final_memo_quality: does the final memo include a SYNTHESIS section with a conclusion not stated by any individual agent, going beyond summarizing the inputs?"
    ),
}


def _build_prompt(agent_key: str, deal: dict, output: str) -> str:
    deal_ctx = json.dumps({
        "name":          deal.get("name", ""),
        "stage":         deal.get("stage", ""),
        "amount":        deal.get("amount", 0),
        "company":       deal.get("companyName", ""),
        "probability":   deal.get("probability", 0),
        "closeDate":     deal.get("closeDate", ""),
        "overall_score": deal.get("overall_score", ""),
    }, indent=2)

    schema_cls = _SCHEMAS[agent_key]
    fields_list = "\n".join(
        f'  "{f}": <0 | 2.5 | 5>'
        for f in schema_cls.model_fields
    )

    return f"""You are a strict evaluator assessing a Deal Desk AI pipeline output.
Score each criterion as 0 (fail), 2.5 (partial pass), or 5 (full pass).
Be conservative — only give 5 for genuinely excellent outputs.

DEAL DATA (ground truth):
{deal_ctx}

AGENT EVALUATED: {agent_key.upper().replace("_", " ")}

AGENT OUTPUT (first 2000 chars):
{output[:2000]}

SCORING RUBRIC:
{_RUBRIC_HINTS.get(agent_key, '')}

Return ONLY a JSON object with these exact keys and numeric values (0, 2.5, or 5):
{{
{fields_list}
}}"""


@traceable(run_type="llm", name="judge-llm")
def _call_judge(agent_key: str, prompt: str) -> dict[str, float]:
    """
    Call Together AI judge for one agent. Returns scored field dict.
    On any failure: logs the error, returns all-zero scores for that agent.
    """
    from together import Together

    api_key = os.environ.get("TOGETHER_API_KEY", "")
    if not api_key:
        print(f"[judge] TOGETHER_API_KEY not set — zeroing {agent_key}", file=sys.stderr)
        return {f: 0 for f in _SCHEMAS[agent_key].model_fields}

    schema_cls = _SCHEMAS[agent_key]

    try:
        client = Together(api_key=api_key)
        resp = create_completion(client,
            model=JUDGE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=256,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "{}").strip()
        parsed = json.loads(raw)

        # Attach token usage to current LangSmith span
        if resp.usage:
            rt = get_current_run_tree()
            if rt:
                rt.outputs = {
                    "scores": parsed,
                    "usage": {
                        "prompt_tokens":     resp.usage.prompt_tokens,
                        "completion_tokens": resp.usage.completion_tokens,
                        "total_tokens":      resp.usage.total_tokens,
                    },
                }

        # Coerce values: model may return 0/2/5 integers — remap 2→2.5
        coerced = {}
        for field in schema_cls.model_fields:
            v = parsed.get(field, 0)
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = 0.0
            # snap to nearest allowed value
            if v <= 1.25:
                v = 0.0
            elif v <= 3.75:
                v = 2.5
            else:
                v = 5.0
            coerced[field] = v

        validated = schema_cls.model_validate(coerced)
        return validated.model_dump()

    except Exception as exc:
        print(f"[judge] {agent_key} failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return {f: 0 for f in schema_cls.model_fields}


@traceable(run_type="chain", name="judge-run")
def judge_run(deal: dict, outputs: dict[str, str]) -> dict[str, Any]:
    """
    Evaluate the active deal desk agents + pipeline level for one deal run.

    Parameters
    ----------
    deal    : deal dict (from LangSmith example inputs["deal"])
    outputs : dict with keys:
                risk_analyst, devil_advocate, arbitrator, sow_writer

    Returns
    -------
    Full scoring dict containing:
      agent_scores    — per-agent criterion scores
      agent_totals    — per-agent sum (max 20)
      agent_pass      — per-agent bool (pass if >= 10)
      pipeline_scores — pipeline-level criterion scores
      pipeline_total  — pipeline sum (max 20)
      pipeline_pass   — bool
      raw_total       — sum of all scores (max 100)
      final_score     — raw_total (0-100)
      passed          — bool (pass if final_score >= 70)
    """
    agent_keys = [
        "risk_analyst",
        "devil_advocate",
        "arbitrator",
        "sow_writer",
    ]

    agent_scores: dict[str, dict[str, float]] = {}
    agent_totals: dict[str, float]            = {}
    agent_pass:   dict[str, bool]             = {}

    for key in agent_keys:
        prompt = _build_prompt(key, deal, outputs.get(key, ""))
        scored = _call_judge(key, prompt)
        total  = sum(scored.values())
        agent_scores[key] = scored
        agent_totals[key] = total
        agent_pass[key]   = (total >= 10.0)

    # Pipeline-level eval uses the active evaluated outputs concatenated
    combined = "\n\n---\n\n".join(
        f"[{k.upper().replace('_', ' ')}]\n{outputs.get(k, '')[:600]}"
        for k in agent_keys
    )
    pipeline_prompt = _build_prompt("pipeline", deal, combined)
    pipeline_scored = _call_judge("pipeline", pipeline_prompt)
    pipeline_total  = sum(pipeline_scored.values())
    pipeline_pass   = (pipeline_total >= 10.0)

    raw_total   = sum(agent_totals.values()) + pipeline_total
    final_score = round(raw_total, 2)
    passed      = (final_score >= 70.0)

    return {
        "agent_scores":    agent_scores,
        "agent_totals":    agent_totals,
        "agent_pass":      agent_pass,
        "pipeline_scores": pipeline_scored,
        "pipeline_total":  pipeline_total,
        "pipeline_pass":   pipeline_pass,
        "raw_total":       raw_total,
        "final_score":     final_score,
        "passed":          passed,
    }
