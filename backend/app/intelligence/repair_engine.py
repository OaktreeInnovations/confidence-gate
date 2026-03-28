"""AI-powered test repair suggestion engine.

Given a failed step result and its evidence, produces ranked repair suggestions
that can be applied directly to the test case step (custom_code, expected, action).
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)

RepairType = Literal[
    "css_selector_override",
    "update_expected",
    "add_wait",
    "update_action",
]

_SYSTEM_PROMPT = """\
You are an automated test repair specialist. A test step has failed or produced wrong results.
Analyze the context and return a JSON array of repair suggestions.

Each suggestion must have:
  - "type": one of "css_selector_override", "update_expected", "add_wait", "update_action"
  - "description": short human-readable explanation (1-2 sentences, plain language)
  - "confidence": float 0.0-1.0
  - "changes": object with the fields to change in the test step:
      - For "css_selector_override": {"custom_code": <StepIntent JSON string>}
      - For "update_expected": {"expected": <new expected outcome string>}
      - For "add_wait": {"custom_code": <StepIntent JSON with navigate + wait_for_text>}
      - For "update_action": {"action": <new action string>}

StepIntent JSON format:
{
  "step_number": <int>,
  "description": "<description>",
  "actions": [
    {"action": "<action_type>", "target": {"css": "<selector>"}, "value": "<value>"}
  ]
}

Valid action types: navigate, input, click, select, wait_for_text, wait_for_navigation,
assert_text, assert_visible, assert_value, extract_value

Rules:
- Return 1-3 suggestions, ranked by confidence descending.
- Only suggest changes that directly address the observed failure.
- For password/masked fields: suggest "update_expected" to accept masked characters as success.
- For wrong element targeted: suggest "css_selector_override" with a precise CSS selector.
- For SPA timing issues (page shows wrong content): suggest "add_wait" with wait_for_text.
- For vision-only failures (element acted correctly but AI cannot verify): suggest "update_expected".
- Use ${key} syntax for test data references (e.g. ${password}, ${email}).
- Return ONLY valid JSON array, no markdown, no explanation outside the array.
"""


@dataclass
class RepairSuggestion:
    type: RepairType
    description: str
    confidence: float
    changes: dict


def suggest_repairs(
    step_number: int,
    action: str,
    expected: str,
    actual: str,
    error_message: str,
    generated_code: str,
    screenshot_bytes: bytes | None,
    api_key: str,
    model: str = "gpt-4o",
) -> list[RepairSuggestion]:
    """Analyze a failed step and return ranked repair suggestions.

    Args:
        step_number: Step number in the test case.
        action: The step action text.
        expected: The expected outcome text.
        actual: The vision-observed actual outcome.
        error_message: Raw error message from execution.
        generated_code: The StepIntent JSON that was used.
        screenshot_bytes: Screenshot PNG bytes (optional, enables vision).
        api_key: OpenAI API key.
        model: OpenAI model to use (gpt-4o for vision support).

    Returns:
        List of RepairSuggestion ordered by confidence descending.
    """
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("repair_engine.openai_not_installed")
        return []

    prompt_text = _build_prompt(step_number, action, expected, actual, error_message, generated_code)

    messages: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]

    if screenshot_bytes:
        b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"},
                },
            ],
        })
    else:
        messages.append({"role": "user", "content": prompt_text})

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=1200,
            temperature=0.2,
        )
        raw = response.choices[0].message.content or "[]"
        logger.info(
            "repair_engine.response",
            step=step_number,
            tokens=response.usage.total_tokens if response.usage else 0,
        )
        return _parse_suggestions(raw, step_number)
    except Exception as exc:
        logger.warning("repair_engine.ai_failed", error=str(exc), step=step_number)
        return []


def _build_prompt(
    step_number: int,
    action: str,
    expected: str,
    actual: str,
    error_message: str,
    generated_code: str,
) -> str:
    parts = [
        f"STEP {step_number} FAILURE CONTEXT",
        f"Action: {action}",
        f"Expected: {expected or '(none)'}",
        f"Actual (vision observed): {actual or '(none)'}",
    ]
    if error_message:
        parts.append(f"Error: {error_message}")
    if generated_code:
        parts.append(f"Intent used:\n{generated_code}")
    parts.append("\nProvide repair suggestions as a JSON array.")
    return "\n".join(parts)


def _parse_suggestions(raw: str, step_number: int) -> list[RepairSuggestion]:
    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)

    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            logger.warning("repair_engine.invalid_response_format", step=step_number)
            return []
    except json.JSONDecodeError as exc:
        logger.warning("repair_engine.parse_failed", error=str(exc), step=step_number)
        return []

    suggestions = []
    valid_types = {"css_selector_override", "update_expected", "add_wait", "update_action"}
    for item in items:
        t = item.get("type", "")
        if t not in valid_types:
            continue
        try:
            suggestions.append(RepairSuggestion(
                type=t,
                description=str(item.get("description", "")),
                confidence=float(item.get("confidence", 0.5)),
                changes=item.get("changes", {}),
            ))
        except (ValueError, TypeError):
            continue

    return sorted(suggestions, key=lambda s: s.confidence, reverse=True)
