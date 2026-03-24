"""Enhance test case steps using AI to make them automation-ready.

Takes rough/vague test steps and rewrites them with:
- Atomic actions (one action per step, compound steps split)
- Explicit field labels and element references
- Concrete, observable expected results
- Test data references instead of hardcoded values
"""

import json

from openai import OpenAI
import structlog

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """\
You are a senior QA automation specialist. Your task is to improve test case steps \
so they are clear, atomic, and automation-ready.

You receive:
- Test case title and description (context for what the test verifies)
- Prerequisites (setup conditions)
- Current steps (action + expected result, may be vague or missing expected results)
- Available test data keys (if any)

Your job is to REWRITE the steps to be better for automated test execution.

------------------------------------------------
RULES
------------------------------------------------

1. PRESERVE THE ORIGINAL TEST FLOW AND INTENT.
   Do NOT add new test scenarios, remove steps, or change what the test verifies.
   You are improving clarity, not redesigning the test.

2. SPLIT COMPOUND STEPS into separate atomic steps.
   BAD:  "Click the 1st audience you see and click continue"
   GOOD: Step N: "Click the first audience card in the list"
         Step N+1: "Click the 'Continue' button"

3. USE EXPLICIT ELEMENT REFERENCES in actions.
   BAD:  "Fill in the form"
   GOOD: "Enter 'John' into the 'First Name' field"
   BAD:  "Click the button"
   GOOD: "Click the 'Submit' button"

4. EVERY STEP MUST HAVE A CONCRETE EXPECTED RESULT.
   BAD:  "" (empty) or "Page loads" or "It works"
   GOOD: "The 'Campaign Details' page is displayed with the campaign title visible"
   GOOD: "A success toast notification appears with the message 'Campaign created'"
   GOOD: "The 'Email' field displays 'user@example.com'"
   Expected results must describe specific, observable UI state changes.

5. REFERENCE TEST DATA GENERICALLY.
   If test data keys are provided, reference them as "the <key> from test data".
   BAD:  "Enter 'admin@test.com' into the Email field"
   GOOD: "Enter the email from test data into the 'Email' field"
   Only apply this for keys that are provided. For other values, use realistic examples.

6. NAVIGATION STEPS should specify what page/section appears.
   BAD:  "Go to campaigns"
   GOOD: "Navigate to the /campaigns page"
   Expected: "The campaigns list page is displayed"

7. WAIT/ASSERTION STEPS should be explicit.
   BAD:  "Wait for the page"
   GOOD: "Wait for the page to fully load"
   Expected: "The page content is visible and interactive"

8. For steps that involve SELECTING FROM A LIST (radio buttons, cards, checkboxes):
   Specify what to select by visible text or position.
   BAD:  "Click an audience"
   GOOD: "Click the first audience card in the audience selection list"

9. Keep step count reasonable. Do not over-split trivial sequences, but always \
   split when a step contains multiple distinct user interactions.

------------------------------------------------
OUTPUT FORMAT (STRICT)
------------------------------------------------

Respond with ONLY a JSON object:

{
  "steps": [
    {"step_number": 1, "action": "...", "expected": "..."},
    {"step_number": 2, "action": "...", "expected": "..."}
  ]
}

No extra explanation. No commentary. No markdown. Only valid JSON.
"""


def enhance_test_case_steps(
    api_key: str,
    title: str,
    description: str,
    prerequisites: str,
    steps: list[dict],
    test_data_keys: list[str] | None = None,
    model: str = "gpt-5-mini",
) -> tuple[list[dict], list[str]]:
    """Call OpenAI to enhance test case steps for automation readiness.

    Returns (enhanced_steps, warnings).

    Raises OpenAI errors (RateLimitError, APITimeoutError, APIError) to caller.
    """
    warnings: list[str] = []
    client = OpenAI(api_key=api_key, timeout=60.0)

    # Build user prompt
    parts = [f"Test case: {title}"]
    if description:
        parts.append(f"Description: {description}")
    if prerequisites:
        parts.append(f"Prerequisites: {prerequisites}")

    if test_data_keys:
        parts.append(f"Available test data keys: {', '.join(test_data_keys)}")

    parts.append("\nCurrent steps:")
    for step in steps:
        action = step.get("action", "").strip()
        expected = step.get("expected", "").strip()
        num = step.get("step_number", "?")
        line = f"  Step {num}: Action: {action}"
        if expected:
            line += f" | Expected: {expected}"
        else:
            line += " | Expected: (none)"
        parts.append(line)

    prompt_text = "\n".join(parts)

    logger.info(
        "ai_enhancer.request",
        title=title[:80],
        step_count=len(steps),
    )

    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=4096,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_text},
        ],
    )

    if response.usage:
        logger.info(
            "ai_enhancer.token_usage",
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            model=model,
        )

    raw = response.choices[0].message.content
    if not raw:
        logger.warning("ai_enhancer.empty_response")
        return [], ["AI returned empty response"]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("ai_enhancer.invalid_json", raw=raw[:500])
        return [], ["AI returned invalid JSON"]

    raw_steps = data.get("steps", [])
    if not isinstance(raw_steps, list):
        logger.error("ai_enhancer.bad_structure", keys=list(data.keys()))
        return [], ["AI returned unexpected structure"]

    # Normalize
    enhanced: list[dict] = []
    for i, step in enumerate(raw_steps, start=1):
        if not isinstance(step, dict):
            continue
        enhanced.append({
            "step_number": i,
            "action": str(step.get("action", "")).strip(),
            "expected": str(step.get("expected", "")).strip(),
        })

    if not enhanced:
        return [], ["AI returned no valid steps"]

    # Validation warnings
    empty_expected = sum(1 for s in enhanced if not s["expected"])
    if empty_expected:
        warnings.append(f"{empty_expected} step(s) still have no expected result")

    original_count = len(steps)
    new_count = len(enhanced)
    if new_count > original_count * 3:
        warnings.append(f"Step count expanded significantly ({original_count} → {new_count})")

    logger.info(
        "ai_enhancer.success",
        original_steps=original_count,
        enhanced_steps=new_count,
    )

    return enhanced, warnings
