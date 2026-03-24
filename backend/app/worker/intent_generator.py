"""AI-powered structured intent generator.

Replaces generate_playwright_code() — AI now produces a JSON intent
describing WHAT actions to perform, never HOW (no Python code, no
raw selectors, no wait logic).

The intent is validated against the StepIntent Pydantic model before
being returned, ensuring type safety at the boundary.
"""

from __future__ import annotations

import base64
import json
import time
from typing import TYPE_CHECKING

import structlog

from app.worker.intent_schema import ActionIntent, ActionType, StepIntent

if TYPE_CHECKING:
    from openai import OpenAI

logger = structlog.get_logger(__name__)


def _extract_usage(response) -> tuple[int, int]:
    """Extract prompt/completion token counts from an OpenAI response."""
    try:
        usage = response.usage
        if usage:
            return usage.prompt_tokens or 0, usage.completion_tokens or 0
    except Exception:
        pass
    return 0, 0


def _downscale_screenshot(screenshot_bytes: bytes, max_width: int = 800) -> bytes:
    """Downscale a screenshot to reduce API payload size."""
    import io
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(screenshot_bytes))
        if img.width > max_width:
            ratio = max_width / img.width
            new_size = (max_width, int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        max_height = 1200
        if img.height > max_height:
            img = img.crop((0, 0, img.width, max_height))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        return buf.getvalue()
    except Exception:
        return screenshot_bytes


INTENT_SYSTEM_PROMPT = """\
You are a test automation intent generator. Given a test step description \
and the current page context (screenshot + accessibility tree + interactive HTML), \
output a JSON object describing WHAT actions to perform, NOT HOW to perform them.

You MUST output ONLY valid JSON matching this exact schema:
{
  "step_number": <int>,
  "actions": [
    {
      "action": "<ActionType>",
      "target": {
        "test_id": "<data-testid value or null>",
        "role": "<ARIA role or null>",
        "name": "<accessible name or null>",
        "label": "<associated label text or null>",
        "placeholder": "<placeholder text or null>",
        "text": "<visible text content or null>",
        "css": "<CSS selector as last resort or null>"
      },
      "value": "<input text, URL, option text, file path, or null>",
      "day": "<day number or 'today' — select_date only>",
      "month_label": "<target month e.g. 'March 2026' — select_date only>"
    }
  ],
  "description": "Human-readable summary of what this step does"
}

Allowed action types:
- navigate: Go to a URL. No target needed, value = URL.
- input: Type text into a field. target = the input, value = text to type.
- click: Click an element. target = the element, no value needed.
- select: Select an option from a dropdown/combobox. target = the dropdown TRIGGER (use "label" matching the field label text, NOT "role": "option"). value = the option text to select.
- select_date: Pick a date from a date picker. target = the date field TRIGGER (use "label" matching the field label text, e.g. {"label": "Start Date"}, NOT "role": "button"). day = day string, month_label = target month.
- upload_file: Upload a file. target = file input/trigger, value = file path.
- check: Check a checkbox. target = the checkbox.
- uncheck: Uncheck a checkbox. target = the checkbox.
- wait_for_text: Wait for text to appear. No target needed, value = expected text (REQUIRED, must not be empty).
- wait_for_navigation: Wait for URL change. No target needed, value = URL glob pattern (REQUIRED, e.g. "**/dashboard**").
- assert_text: Assert element contains text. target = the element, value = expected text.
- assert_visible: Assert element is visible. target = the element.
- assert_value: Assert input has value. target = the input, value = expected value.

RULES:
1. NEVER output CSS selectors unless absolutely no semantic alternative exists.
2. Prefer role+name and label over text. Prefer test_id over everything.
3. Use the accessibility tree to identify exact ARIA roles and accessible names.
   CRITICAL: Generate ONLY the actions described in the step. Do NOT add
   navigation clicks, section-reveal clicks, or "ensure we are on the right page"
   actions unless the step EXPLICITLY asks for them. Previous steps have already
   set up the page state. If a target IS visible in the accessibility tree or
   interactive HTML, click it directly — do NOT prepend parent navigation clicks.
4. For date pickers: use select_date with "day" and optionally "month_label".
   The target must be the date field (identified by "label", e.g. {"label": "Start Date"}).
   NEVER use "role": "button" with "name": "Select date" — that targets a generic trigger button.
   Use the field's visible label text so the engine can discover the correct input.
5. For dropdowns/comboboxes: use select (NOT a click+click sequence).
   The target must be the dropdown trigger (identified by "label", e.g. {"label": "Objective"}).
   NEVER use "role": "option" or "role": "listbox" as the target — those are inside the popup.
   The value field is the option text to select (e.g. "Awareness").
6. For file uploads: use upload_file (NOT click on input).
7. One action per UI interaction. Do NOT combine multiple interactions into one action.
8. Only include target fields that have a value. Omit null fields.
9. Output ONLY the JSON object. No markdown fences, no explanation, no commentary.
10. For card-based radio groups (clickable cards with radio buttons inside):
    Use "role": "radio" with the accessible "name" from the accessibility tree.
    If the radio has no accessible name, use "text" matching the visible card label.
11. For calendar day cells: NEVER use "role": "gridcell" with "name": "Today".
    Calendar cells use day NUMBERS as accessible names (e.g., "13"), not words.
    To select today's date, use select_date with day: "today" and target the
    date field by its label. The engine resolves "today" to the correct number.
12. If the target element is NOT in the accessibility tree AND the step
    description explicitly mentions expanding or revealing a section (e.g.
    "expand the Reports menu then click Monthly Summary"), generate actions
    to reach it:
    a) Click the parent item to expand/reveal the hidden section.
    b) Add wait_for_text with the target item name to wait for it to appear.
    c) Add a CLICK on the target item to actually navigate to it.
    CRITICAL: Do NOT stop after wait_for_text — the click on the item is
    required to trigger navigation.
    HOWEVER: If the step ONLY says "Click X" and does NOT mention expanding
    or revealing anything, generate ONLY the click on X.  Do NOT add
    parent navigation clicks on your own — the page is already set up
    by previous steps.
13. For wait_for_navigation: the value should be the full DESTINATION URL or a
    glob using "**" for multi-segment matching (e.g. "**/dashboard**").
    Prefer using the exact destination URL when available from test data.
14. When test data is provided with template keys, use the template reference
    as the value. Example: if test data has "email" and "password", use
    "value": "${email}" and "value": "${password}" — NOT the literal text.
    The executor substitutes actual values at runtime. This avoids JSON
    escaping issues with special characters in passwords, URLs, etc.

EXAMPLES (use these as templates for structuring your output):

Example 1 — Login with template variables:
Step action: "Enter email and password, then click Log In"
Test data keys: email, password
{
  "step_number": 2,
  "actions": [
    {"action": "input", "target": {"role": "textbox", "name": "Email"}, "value": "${email}"},
    {"action": "input", "target": {"role": "textbox", "name": "Password"}, "value": "${password}"},
    {"action": "click", "target": {"role": "button", "name": "Log In"}}
  ],
  "description": "Fill login credentials and submit"
}

Example 2 — Expand a collapsible section then click a revealed item:
Step action: "Click the Reports menu to expand it, then click Monthly Summary"
Note: "Monthly Summary" is NOT in the accessibility tree (hidden in collapsed menu) — generate the click anyway.
{
  "step_number": 3,
  "actions": [
    {"action": "click", "target": {"name": "Reports"}},
    {"action": "wait_for_text", "value": "Monthly Summary"},
    {"action": "click", "target": {"name": "Monthly Summary"}}
  ],
  "description": "Expand Reports menu, wait for items, click Monthly Summary to navigate"
}

Example 3 — Form fill with labeled fields:
Step action: "Fill in the student name as John Doe and select grade level Senior"
{
  "step_number": 4,
  "actions": [
    {"action": "input", "target": {"label": "Student Name"}, "value": "John Doe"},
    {"action": "select", "target": {"label": "Grade Level"}, "value": "Senior"}
  ],
  "description": "Fill student name and select grade level"
}

Example 4 — Dropdown selection (single select action, NOT click+click):
Step action: "Select 'Awareness' from the Objective dropdown"
{
  "step_number": 5,
  "actions": [
    {"action": "select", "target": {"label": "Objective"}, "value": "Awareness"}
  ],
  "description": "Select Awareness from Objective dropdown"
}
"""


def generate_step_intent(
    client: OpenAI,
    action: str,
    step_number: int,
    previous_actions: list[str],
    page_context: dict | None = None,
    model: str = "gpt-5-mini",
    test_data: dict[str, str] | None = None,
    selector_hints: list[dict] | None = None,
    intelligence_context: str | None = None,
) -> tuple[StepIntent, int, int]:
    """Generate a structured StepIntent via GPT.

    Args:
        client: OpenAI client instance.
        action: Natural-language step description.
        step_number: 1-based step index.
        previous_actions: Actions already executed in this run.
        page_context: Dict with screenshot, a11y_tree, interactive_html.
        model: OpenAI model identifier.
        test_data: Key-value pairs for form data.
        selector_hints: Previously successful selectors for this step.
        intelligence_context: Adaptive strategy hints.

    Returns:
        (StepIntent, prompt_tokens, completion_tokens)

    Raises:
        ValueError: If AI output cannot be parsed as valid StepIntent.
    """
    prompt_text = _build_user_prompt(
        action, step_number, previous_actions,
        test_data, selector_hints, intelligence_context,
    )

    user_content = _build_user_content(prompt_text, page_context)

    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=2048,
        messages=[
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    prompt_tokens, completion_tokens = _extract_usage(response)

    raw = response.choices[0].message.content.strip()
    raw = _strip_json_fences(raw)

    logger.info(
        "intent_gen.response",
        step=step_number,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        raw_length=len(raw),
    )

    intent = _parse_intent(raw, step_number)
    intent = _post_process_intent(intent, step_action=action)
    return intent, prompt_tokens, completion_tokens


def regenerate_step_intent(
    client: OpenAI,
    action: str,
    step_number: int,
    previous_actions: list[str],
    error_context: str,
    page_context: dict | None = None,
    model: str = "gpt-5-mini",
    test_data: dict[str, str] | None = None,
    selector_hints: list[dict] | None = None,
    intelligence_context: str | None = None,
) -> tuple[StepIntent, int, int]:
    """Regenerate intent after execution failure.

    Includes the error context so AI can adjust targets/approach.
    Now also receives selector_hints and intelligence_context for
    parity with initial generation.

    Returns:
        (StepIntent, prompt_tokens, completion_tokens)
    """
    prompt_text = _build_user_prompt(
        action, step_number, previous_actions,
        test_data, selector_hints, intelligence_context,
    )
    prompt_text += (
        f"\n\nPREVIOUS ATTEMPT FAILED with error:\n{error_context[:500]}"
        "\n\nGenerate a corrected intent. Use different selectors or "
        "a different approach to accomplish the same action."
    )

    # If the error includes completed actions, instruct AI not to redo them
    if "COMPLETED_ACTIONS:" in error_context:
        prompt_text += (
            "\n\nIMPORTANT: Some actions already succeeded and their effects "
            "are visible on the page. Do NOT repeat or contradict those actions. "
            "Generate only the remaining actions needed to complete the step."
        )

    user_content = _build_user_content(prompt_text, page_context)

    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=2048,
        messages=[
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    prompt_tokens, completion_tokens = _extract_usage(response)

    raw = response.choices[0].message.content.strip()
    raw = _strip_json_fences(raw)

    logger.info(
        "intent_gen.regen_response",
        step=step_number,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )

    intent = _parse_intent(raw, step_number)
    intent = _post_process_intent(intent, step_action=action)
    return intent, prompt_tokens, completion_tokens


# --- Private helpers ---


def _build_user_prompt(
    action: str,
    step_number: int,
    previous_actions: list[str],
    test_data: dict[str, str] | None = None,
    selector_hints: list[dict] | None = None,
    intelligence_context: str | None = None,
) -> str:
    """Build the user prompt text (without image/HTML context)."""
    parts = [f"Generate intent for step {step_number}:\n\nAction: {action}"]

    if previous_actions:
        prev = "\n".join(f"- Step {i + 1}: {a}" for i, a in enumerate(previous_actions))
        parts.append(f"\nPrevious steps already executed:\n{prev}")

    if test_data:
        # Show test_data as template references to avoid JSON escaping issues
        # with special characters (quotes, unicode, etc.)
        data_lines = "\n".join(f"  {k}: ${{{k}}}" for k in test_data)
        parts.append(
            f"\nTest data keys (use ${{{'{key}'}}} as the value in JSON):\n{data_lines}"
            f"\nIMPORTANT: For input actions using test data, set value to the "
            f"template reference like \"${{email}}\" or \"${{password}}\". "
            f"The executor will substitute the actual value at runtime."
        )

    if selector_hints:
        hint_lines = "\n".join(
            f"  - {h['selector']} (success rate: {h['success_rate']:.0%})"
            for h in selector_hints
        )
        parts.append(f"\nHistorically successful selectors (use as target hints):\n{hint_lines}")

    if intelligence_context:
        parts.append(f"\n{intelligence_context}")

    return "\n".join(parts)


def _build_user_content(
    prompt_text: str,
    page_context: dict | None,
) -> list[dict]:
    """Build the user content array including optional screenshot and page info."""
    if not page_context:
        return [{"type": "text", "text": prompt_text}]

    user_content: list[dict] = []

    # Screenshot
    if page_context.get("screenshot"):
        compressed = _downscale_screenshot(page_context["screenshot"])
        screenshot_b64 = base64.b64encode(compressed).decode("utf-8")
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
        })

    # Structural context
    a11y = page_context.get("a11y_tree", "")
    html = page_context.get("interactive_html", "")

    structural = ""
    if a11y:
        structural += f"\n\nAccessibility tree (all interactive elements):\n{a11y}"
    if html:
        structural += f"\n\nInteractive HTML elements:\n{html}"

    user_content.append({
        "type": "text",
        "text": (
            f"Above is a screenshot of the current page.{structural}"
            f"\n\nUse the accessibility tree and HTML to pick exact targets."
            f"\n\n{prompt_text}"
        ),
    })

    return user_content


def _strip_json_fences(raw: str) -> str:
    """Remove markdown code fences if the model wraps JSON in them."""
    if raw.startswith("```"):
        lines = raw.split("\n")
        # Remove first line (```json or ```)
        lines = lines[1:]
        # Remove last line if it's ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)
    return raw.strip()


def _parse_intent(raw: str, step_number: int) -> StepIntent:
    """Parse and validate AI-generated JSON into a StepIntent.

    Applies safety checks:
    - Must be valid JSON
    - Must have 'actions' list
    - Each action must have a valid ActionType
    - Step number is forced to the expected value
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"AI returned invalid JSON: {e}. Raw: {raw[:300]}") from e

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")

    # Force correct step number
    data["step_number"] = step_number

    # Ensure actions key exists
    actions = data.get("actions")
    if not actions or not isinstance(actions, list):
        raise ValueError(f"Missing or invalid 'actions' list in intent: {raw[:300]}")

    # Validate action types
    valid_types = {t.value for t in ActionType}
    for i, act in enumerate(actions):
        if not isinstance(act, dict):
            raise ValueError(f"Action {i} is not a dict: {act}")
        action_type = act.get("action", "")
        if action_type not in valid_types:
            raise ValueError(
                f"Invalid action type '{action_type}' in action {i}. "
                f"Valid types: {sorted(valid_types)}"
            )

    # Normalize select_date actions: AI sometimes nests day/month_label
    # inside "value" as a dict instead of using top-level fields
    for act in actions:
        if act.get("action") == "select_date" and isinstance(act.get("value"), dict):
            date_val = act.pop("value")
            if "day" in date_val and not act.get("day"):
                act["day"] = str(date_val["day"])
            if "month_label" in date_val and not act.get("month_label"):
                act["month_label"] = str(date_val["month_label"])

    # Validate action-specific required fields
    for i, act in enumerate(actions):
        action_type = act.get("action", "")
        if action_type == "select_date":
            if not act.get("day") and not act.get("value"):
                raise ValueError(
                    f"select_date action {i} missing required 'day' field. "
                    f"Got: {act}"
                )
        if action_type == "select" and not act.get("value"):
            raise ValueError(
                f"select action {i} missing required 'value' field. "
                f"Got: {act}"
            )
        if action_type == "input" and act.get("value") is None:
            raise ValueError(
                f"input action {i} missing required 'value' field. "
                f"Got: {act}"
            )

    # Validate via Pydantic (catches type mismatches, extra fields)
    try:
        intent = StepIntent.model_validate(data)
    except Exception as e:
        raise ValueError(f"Intent validation failed: {e}. Raw: {raw[:300]}") from e

    if not intent.actions:
        raise ValueError("Intent has empty actions list")

    return intent



def _step_mentions_clicking(step_action: str, target_text: str) -> bool:
    """Check if step description mentions clicking/navigating to the target.

    Uses sentence-level matching: the step text is split on sentence boundaries
    ("." or ";") and each clause is checked for BOTH an action keyword AND the
    target text.  This prevents false positives like
    "Click Sign In. Wait for the dashboard page" from matching "Dashboard" —
    the clause with "click" doesn't contain "dashboard".
    """
    target_lower = target_text.lower()
    _KEYWORDS = ("click", "navigate", "go to", "open")
    # Split on sentence/clause boundaries; also treat "then" as a boundary
    # so "Click X then click Y" works per-clause.
    import re
    clauses = re.split(r'[.;]\s*|\bthen\b', step_action.lower())
    for clause in clauses:
        if target_lower in clause and any(kw in clause for kw in _KEYWORDS):
            return True
    return False


def _post_process_intent(intent: StepIntent, step_action: str = "") -> StepIntent:
    """Deterministic post-processing — fixes common AI generation bugs.

    1. Deduplicates consecutive clicks on the same target
    2. Rewrites click on role:"option" to select with proper target/value
    3. Warns on wait_for_navigation with non-glob, non-URL pattern
    """
    cleaned: list[ActionIntent] = []
    for action in intent.actions:
        # 1. Deduplicate consecutive clicks on same target
        if (
            action.action == ActionType.CLICK
            and cleaned
            and cleaned[-1].action == ActionType.CLICK
            and _same_target(cleaned[-1].target, action.target)
        ):
            logger.info(
                "intent_postprocess.dedup_click",
                step=intent.step_number,
            )
            continue

        # 2. Rewrite click on role:"option" → select
        if (
            action.action == ActionType.CLICK
            and action.target
            and action.target.role == "option"
        ):
            option_name = action.target.name or action.target.text or ""
            if option_name:
                action.action = ActionType.SELECT
                action.value = option_name
                action.target.role = None
                action.target.name = None
                logger.info(
                    "intent_postprocess.rewrite_option_to_select",
                    step=intent.step_number,
                    option=option_name[:50],
                )

        # 3. Warn on wait_for_navigation with suspicious pattern
        if action.action == ActionType.WAIT_FOR_NAVIGATION:
            url = action.value or ""
            if url and not url.startswith("http") and "**" not in url and "*" not in url:
                logger.warning(
                    "intent_postprocess.suspicious_nav_pattern",
                    step=intent.step_number,
                    pattern=url[:100],
                )

        # 4. Drop wait_for_text / wait_for_navigation with empty value
        if action.action in (ActionType.WAIT_FOR_TEXT, ActionType.WAIT_FOR_NAVIGATION):
            if not action.value:
                logger.warning(
                    "intent_postprocess.drop_empty_wait",
                    step=intent.step_number,
                    action_type=str(action.action),
                )
                continue

        cleaned.append(action)

    # 5. Auto-add missing click after expand → wait_for_text pattern.
    # When AI expands a collapsible (click) then waits for content
    # (wait_for_text) but never clicks the revealed item, navigation
    # won't happen.  Only add the click when:
    #   a) The step description explicitly mentions clicking the target
    #   b) The intent doesn't already contain a click on the same target
    if len(cleaned) >= 2 and step_action:
        last = cleaned[-1]
        has_preceding_click = any(
            a.action == ActionType.CLICK for a in cleaned[:-1]
        )
        # Check if intent already has a click after the first one (the
        # expand click).  If the AI already generated a navigation click,
        # don't add another — the full expand→wait→click flow is present.
        already_has_nav_click = any(
            a.action == ActionType.CLICK for a in cleaned[1:]
        )
        if (
            has_preceding_click
            and last.action == ActionType.WAIT_FOR_TEXT
            and last.value
            and not already_has_nav_click
            and _step_mentions_clicking(step_action, last.value)
        ):
            from app.worker.intent_schema import TargetDescriptor
            missing_click = ActionIntent(
                action=ActionType.CLICK,
                target=TargetDescriptor(name=last.value),
            )
            cleaned.append(missing_click)
            logger.info(
                "intent_postprocess.auto_add_click",
                step=intent.step_number,
                target_text=last.value[:50],
            )

    intent.actions = cleaned
    return intent


def _same_target(a, b) -> bool:
    """Check if two targets refer to the same element.

    Matches on any shared non-None *identity* field.  Role is excluded
    because it's too generic — hundreds of elements share "button" or "link".
    """
    if a is None or b is None:
        return a is None and b is None
    for f in ("test_id", "name", "label", "placeholder", "text", "css"):
        va = getattr(a, f, None)
        vb = getattr(b, f, None)
        if va is not None and vb is not None and va == vb:
            return True
    return False
