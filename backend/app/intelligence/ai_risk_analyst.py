"""AI-powered risk analysis layer that produces a bounded ±5 score adjustment."""

from __future__ import annotations

import json

import structlog

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are a release reliability analyst. Given a test execution report, "
    "return a JSON object with keys: adjustment (int -5 to 5), confidence (float 0-1), "
    "insights (list of strings), risk_explanations (list of strings), "
    "hidden_patterns (list of strings). "
    "adjustment represents how much to shift the confidence score. "
    "Be concise. No markdown."
)


def _build_prompt(report_dict: dict, context: dict) -> str:
    batch = report_dict.get("batch_summary", {})
    blockers = report_dict.get("blockers", [])
    root_causes = [rc.get("description", "") for rc in report_dict.get("root_causes", [])]
    anomalies = context.get("anomalies", [])
    instability_index = context.get("instability_index", 0)
    coverage_score = context.get("coverage_score", 0)
    score_delta = context.get("score_delta")
    trend = context.get("trend", "stable")

    lines = [
        f"Confidence score: {report_dict.get('confidence_score', 0)}/100",
        f"Recommendation: {report_dict.get('recommendation', 'unknown')}",
        f"Pass rate: {batch.get('pass_rate', 0):.0%} ({batch.get('passed', 0)}/{batch.get('total', 0)})",
        f"Instability index: {instability_index}",
        f"Coverage score: {coverage_score}",
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

        client = OpenAI(api_key=api_key)
        prompt = _build_prompt(report_dict, context)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.1,
            max_tokens=300,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )

        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)

        adjustment = int(data.get("adjustment", 0))
        adjustment = max(-5, min(5, adjustment))

        return {
            "ai_adjustment": adjustment,
            "ai_confidence": float(data.get("confidence", 0.0)),
            "ai_insights": list(data.get("insights", [])),
            "risk_explanations": list(data.get("risk_explanations", [])),
        }

    except Exception as e:
        logger.warning("ai_risk_analyst.error", error=str(e)[:200])
        return _default
