"""AI-powered release validation summary generation.

Optional, non-fatal. Falls back to a template-based summary if:
- No OpenAI API key configured
- API call fails
- Any exception occurs

Returns a dict with executive_summary, key_findings, risk_areas,
next_steps, and ai_generated (bool) flag.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


def generate_release_summary(
    report: dict,
    context: dict | None,
    openai_api_key: str,
) -> dict:
    """Generate an AI-enhanced summary of a release validation report.

    Returns:
        {
            "executive_summary": str,
            "key_findings": list[str],
            "risk_areas": list[str],
            "next_steps": list[str],
            "ai_generated": bool,
        }
    """
    if not openai_api_key:
        return _template_summary(report, context)

    try:
        return _ai_summary(report, context, openai_api_key)
    except Exception as e:
        logger.warning(
            "release_summarizer.ai_failed",
            error=str(e)[:200],
        )
        return _template_summary(report, context)


def _ai_summary(report: dict, context: dict | None, api_key: str) -> dict:
    """Call OpenAI to generate a summary."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    # Build a concise representation of the report for the prompt
    score = report.get("confidence_score", 0)
    grade = report.get("confidence_grade", "?")
    recommendation = report.get("recommendation", "unknown")
    batch = report.get("batch_summary", {})
    signals = report.get("signals", [])
    root_causes = report.get("root_causes", [])
    blockers = report.get("blockers", [])

    signal_text = "\n".join(
        f"- {s['name']} ({s['severity']}): {s['detail']}"
        for s in signals
    )

    root_cause_text = "\n".join(
        f"- #{rc['rank']}: {rc['description']}"
        for rc in root_causes
    )

    blocker_text = "\n".join(f"- {b}" for b in blockers) if blockers else "None"

    context_text = ""
    if context:
        if context.get("prd_text"):
            context_text += f"\nPRD/Requirements:\n{context['prd_text'][:500]}"
        if context.get("notes"):
            context_text += f"\nNotes:\n{context['notes'][:300]}"

    prompt = f"""You are a QA engineering lead analyzing a release validation report.

Release Validation Results:
- Confidence Score: {score}/100 (Grade: {grade})
- Recommendation: {recommendation}
- Tests: {batch.get('passed', 0)} passed, {batch.get('failed', 0)} failed, {batch.get('error', 0)} errors out of {batch.get('total', 0)} total

Signals:
{signal_text}

Root Causes:
{root_cause_text}

Blockers:
{blocker_text}
{context_text}

Provide a concise analysis in this exact JSON format:
{{
  "executive_summary": "2-3 sentence summary of release readiness",
  "key_findings": ["finding 1", "finding 2", "finding 3"],
  "risk_areas": ["risk 1", "risk 2"],
  "next_steps": ["action 1", "action 2"]
}}

Be specific and actionable. Reference actual test names and failure types."""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.3,
        max_tokens=500,
    )

    import json
    result = json.loads(response.choices[0].message.content)
    result["ai_generated"] = True

    logger.info(
        "release_summarizer.ai_success",
        tokens=response.usage.total_tokens if response.usage else 0,
    )

    return result


def _template_summary(report: dict, context: dict | None) -> dict:
    """Generate a template-based summary (no AI)."""
    score = report.get("confidence_score", 0)
    grade = report.get("confidence_grade", "?")
    recommendation = report.get("recommendation", "unknown")
    batch = report.get("batch_summary", {})
    root_causes = report.get("root_causes", [])
    blockers = report.get("blockers", [])
    signals = report.get("signals", [])

    total = batch.get("total", 0)
    passed = batch.get("passed", 0)
    failed = batch.get("failed", 0)

    # Executive summary
    if recommendation == "deploy":
        exec_summary = (
            f"Release validation scored {score}/100 (Grade {grade}). "
            f"{passed}/{total} tests passed. The release appears safe to deploy."
        )
    elif recommendation == "caution":
        exec_summary = (
            f"Release validation scored {score}/100 (Grade {grade}). "
            f"{passed}/{total} tests passed with {failed} failures. "
            "Review flagged signals before deploying."
        )
    else:
        exec_summary = (
            f"Release validation scored {score}/100 (Grade {grade}). "
            f"{passed}/{total} tests passed with {failed} failures. "
            "The release is not recommended for deployment."
        )

    # Key findings from signals
    key_findings = []
    for s in signals:
        if s.get("severity") in ("warning", "critical"):
            key_findings.append(f"{s['name']}: {s['detail']}")
    if not key_findings:
        key_findings = [f"Batch pass rate: {passed}/{total}"]

    # Risk areas from root causes
    risk_areas = [rc["description"] for rc in root_causes[:3]]
    if not risk_areas:
        risk_areas = ["No specific risk areas identified"]

    # Next steps
    next_steps = []
    if blockers:
        next_steps.append("Address blockers before deploying")
    if failed > 0:
        next_steps.append("Investigate and fix failing test cases")
    critical_signals = [s for s in signals if s.get("severity") == "critical"]
    if critical_signals:
        next_steps.append("Review critical signals in signal analysis")
    if not next_steps:
        next_steps = ["Proceed with deployment"]

    return {
        "executive_summary": exec_summary,
        "key_findings": key_findings[:5],
        "risk_areas": risk_areas[:3],
        "next_steps": next_steps[:3],
        "ai_generated": False,
    }
