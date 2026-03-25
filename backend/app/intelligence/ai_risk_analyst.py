"""AI-powered risk analysis layer.

Adjustment range:
  - No PRD:       bounded ±5  (pattern detection only)
  - PRD provided: bounded ±20 (PRD coverage gap analysis)
"""

from __future__ import annotations

import json

import structlog

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT_NO_PRD = (
    "You are a release reliability analyst. Given a test execution report, "
    "return a JSON object with keys: adjustment (int -5 to 5), confidence (float 0-1), "
    "insights (list of strings), risk_explanations (list of strings), "
    "hidden_patterns (list of strings). "
    "adjustment represents how much to shift the confidence score based on hidden patterns. "
    "Be concise. No markdown."
)

_SYSTEM_PROMPT_WITH_PRD = (
    "You are a release reliability analyst. You are given a test execution report AND "
    "a Product Requirements Document (PRD). Your job is to identify gaps between what "
    "the PRD requires and what the tests actually cover, then penalize or reward accordingly.\n\n"
    "Return a JSON object with keys:\n"
    "  adjustment (int -20 to 5): negative = requirements not met, positive = exceeded expectations\n"
    "  confidence (float 0-1): your confidence in this assessment\n"
    "  insights (list of strings): observations about test coverage vs PRD\n"
    "  risk_explanations (list of strings): specific PRD requirements that are untested or failing\n"
    "  hidden_patterns (list of strings): patterns in test execution quality\n\n"
    "Scoring guidance:\n"
    "  -15 to -20: critical PRD requirements (P0 features, security, billing) completely absent from tests\n"
    "  -10 to -14: major features in PRD have no test coverage\n"
    "  -5 to -9:   several PRD requirements untested or partially covered\n"
    "  -1 to -4:   minor PRD gaps\n"
    "  0:          tests reasonably cover the PRD\n"
    "  +1 to +5:   tests exceed PRD requirements\n\n"
    "Be precise. Cite specific PRD sections when explaining gaps. No markdown."
)


def _build_prompt(report_dict: dict, context: dict) -> str:
    """Build the AI prompt.

    report_dict: enriched report with all computed signals (instability, coverage, etc.)
    context: release validation context with prd_text and notes
    """
    batch = report_dict.get("batch_summary", {})
    blockers = report_dict.get("blockers", [])
    root_causes = [rc.get("description", "") for rc in report_dict.get("root_causes", [])]

    # These live in report_dict (enriched), NOT in context
    anomalies = report_dict.get("anomalies", [])
    instability_index = report_dict.get("instability_index", 0)
    coverage_score = report_dict.get("coverage_score", 0)
    delta = report_dict.get("delta", {})
    score_delta = delta.get("score_delta") if isinstance(delta, dict) else None
    trend = report_dict.get("trend", "stable")

    # PRD and notes come from context
    prd_text = (context.get("prd_text") or "").strip()
    notes = (context.get("notes") or "").strip()

    lines = [
        f"Confidence score: {report_dict.get('confidence_score', 0)}/100",
        f"Recommendation: {report_dict.get('recommendation', 'unknown')}",
        f"Pass rate: {batch.get('pass_rate', 0):.0%} ({batch.get('passed', 0)}/{batch.get('total', 0)})",
        f"Instability index: {instability_index}/100",
        f"Coverage score: {coverage_score}/100",
        f"Trend: {trend}",
    ]

    if score_delta is not None:
        lines.append(f"Score delta vs previous release: {score_delta:+d}")

    if blockers:
        lines.append(f"Blockers: {'; '.join(blockers)}")

    if root_causes:
        lines.append(f"Root causes: {'; '.join(root_causes)}")

    if anomalies:
        anomaly_summaries = [
            f"{a.get('type')} ({a.get('severity')}): {a.get('detail', '')[:100]}"
            for a in anomalies[:5]
        ]
        lines.append(f"Anomalies: {'; '.join(anomaly_summaries)}")

    if notes:
        lines.append(f"\nRelease notes: {notes[:500]}")

    if prd_text:
        lines.append(f"\n--- PRODUCT REQUIREMENTS DOCUMENT ---\n{prd_text[:3000]}\n--- END PRD ---")

    return "\n".join(lines)


def analyze_with_ai(report_dict: dict, context: dict, api_key: str) -> dict:
    _default = {
        "ai_adjustment": 0,
        "ai_confidence": 0.0,
        "ai_insights": [],
        "risk_explanations": [],
    }

    if not api_key:
        return _default

    try:
        from openai import OpenAI

        prd_text = (context.get("prd_text") or "").strip() if context else ""
        has_prd = bool(prd_text)

        system_prompt = _SYSTEM_PROMPT_WITH_PRD if has_prd else _SYSTEM_PROMPT_NO_PRD
        adj_min = -20 if has_prd else -5
        adj_max = 5

        client = OpenAI(api_key=api_key)
        prompt = _build_prompt(report_dict, context or {})

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.1,
            max_tokens=600,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        )

        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)

        adjustment = int(data.get("adjustment", 0))
        adjustment = max(adj_min, min(adj_max, adjustment))

        logger.info(
            "ai_risk_analyst.result",
            has_prd=has_prd,
            raw_adjustment=data.get("adjustment", 0),
            clamped_adjustment=adjustment,
            ai_confidence=data.get("confidence", 0.0),
        )

        return {
            "ai_adjustment": adjustment,
            "ai_confidence": float(data.get("confidence", 0.0)),
            "ai_insights": list(data.get("insights", [])),
            "risk_explanations": list(data.get("risk_explanations", [])),
        }

    except Exception as e:
        logger.warning("ai_risk_analyst.error", error=str(e)[:200])
        return _default
