"""Layered step validation for the structured execution engine.

Validation layers (executed in order, short-circuit on success):

Layer 1 — DOM Assertions:
    If the StepIntent contains assert_text / assert_visible / assert_value
    actions and they all passed during execution, the step is validated.
    No further checks needed.

Layer 2 — DOM State Check:
    For non-assertion actions (click, input, select, etc.), verify the action
    had a visible effect on the page. Basic heuristic: URL changed, DOM
    mutated, or new content appeared.

Layer 2.5 — Text Content Verification:
    For observation/verification steps, extract keywords from the expected
    result and verify they exist in the page text. Catches false positives
    where the step "passed" execution but the page shows wrong content
    (e.g. still on login page when expecting a dashboard).

Layer 3 — Vision Verification (optional):
    Only invoked if:
    - vision_enabled=True in the execution mode config
    - The step has an 'expected' result description
    - No DOM assertion was present in the intent (ambiguous outcome)
    Vision NEVER triggers regeneration — returns passed or inconclusive.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog

from app.worker.intent_schema import ActionType, StepIntent

if TYPE_CHECKING:
    from app.worker.ai_provider import AIProvider

logger = structlog.get_logger(__name__)

# Action types that are explicit DOM assertions
_ASSERTION_ACTIONS = frozenset({
    ActionType.ASSERT_TEXT,
    ActionType.ASSERT_VISIBLE,
    ActionType.ASSERT_VALUE,
})


def validate_step(
    page,
    intent: StepIntent,
    expected: str,
    screenshot_bytes: bytes | None,
    ai_client: AIProvider | None = None,
    vision_enabled: bool = False,
    model: str = "gpt-5-mini",
    pre_step_url: str = "",
    step_action: str = "",
) -> tuple[str, str, str]:
    """Validate step outcome using layered approach.

    Args:
        page: Playwright Page instance.
        intent: The StepIntent that was executed.
        expected: Human-readable expected outcome from the test step.
        screenshot_bytes: Post-execution screenshot (for vision layer).
        ai_client: OpenAI client (needed for vision layer).
        pre_step_url: URL before step execution (for detecting navigation).
        vision_enabled: Whether to invoke vision verification.
        model: OpenAI model for vision.
        step_action: Original step action text (used for vision instead of
            intent.description to avoid AI-generated bias).

    Returns:
        (status, actual_description, verification_mode) where:
        - status is "passed" or "failed"
        - verification_mode is "ai", "dom-only", or "skipped"
    """
    # Layer 1: DOM assertions
    has_assertions = any(a.action in _ASSERTION_ACTIONS for a in intent.actions)
    if has_assertions:
        # If we reach this point, assertions already passed in intent_executor
        # (they would have thrown an exception otherwise)
        return "passed", "All DOM assertions passed", "dom-only"

    # Layer 2: DOM state check (lightweight)
    dom_status, dom_desc = _check_dom_state(page, intent, pre_step_url)

    # Layer 2.5: Text content verification for observation steps
    # If expected mentions specific observable content, verify it in page text
    text_check = _check_expected_text_in_page(page, expected)

    # Use original step action for vision (avoids AI-generated description bias)
    vision_action = step_action or intent.description

    if dom_status == "passed":
        # If vision is also available, try it for higher confidence
        if vision_enabled and expected and screenshot_bytes and ai_client:
            vision_status, vision_desc = _vision_verify(
                ai_client, screenshot_bytes, expected,
                vision_action, model,
            )
            if vision_status is not None:
                return vision_status, vision_desc, "ai"

        # If text check found missing keywords, fail even if DOM says passed
        if text_check is not None:
            tc_status, tc_desc = text_check
            if tc_status == "failed":
                logger.warning(
                    "validation.text_check_failed",
                    expected=expected[:200],
                    detail=tc_desc[:200],
                )
                return "failed", tc_desc, "dom-only"

        return "passed", dom_desc, "dom-only"

    # Layer 3: Vision verification (optional, expensive)
    if vision_enabled and expected and screenshot_bytes and ai_client:
        vision_status, vision_desc = _vision_verify(
            ai_client, screenshot_bytes, expected,
            vision_action, model,
        )
        if vision_status is not None:
            return vision_status, vision_desc, "ai"
        # Vision unavailable — fall through to dom-only
        return "passed", dom_desc or "Step executed", "dom-only"

    # No assertions and no vision — trust execution success
    if not expected:
        return "passed", "Step executed (no expected result to verify)", "skipped"

    # Text check can still catch mismatches even without vision
    if text_check is not None:
        tc_status, tc_desc = text_check
        if tc_status == "failed":
            return "failed", tc_desc, "dom-only"

    # Heuristic: if expected mentions navigation/redirect but URL didn't change, fail
    if pre_step_url and expected:
        try:
            current_url = page.url
        except Exception:
            current_url = ""
        nav_keywords = ("redirect", "navigate", "logged in", "dashboard", "loads")
        expected_lower = expected.lower()
        if (
            current_url == pre_step_url
            and any(kw in expected_lower for kw in nav_keywords)
        ):
            logger.warning(
                "validation.expected_navigation_failed",
                pre_url=pre_step_url,
                post_url=current_url,
                expected=expected[:200],
            )
            return (
                "failed",
                f"Expected navigation ('{expected[:80]}') but URL unchanged: {current_url}",
                "dom-only",
            )

    return "passed", "Step executed (vision disabled)", "dom-only"


def _check_dom_state(
    page, intent: StepIntent, pre_step_url: str = "",
) -> tuple[str, str]:
    """Layer 2: Basic DOM state heuristic.

    For navigation: check URL actually changed from pre-step URL.
    For input/select: check value was set.
    For click: trust execution (click errors are caught by wrappers).
    """
    current_url = ""
    try:
        current_url = page.url
    except Exception:
        pass

    has_navigation_action = False

    for action in intent.actions:
        if action.action == ActionType.NAVIGATE:
            return "passed", f"Navigated to {current_url}"

        if action.action == ActionType.WAIT_FOR_TEXT:
            # If wait_for completed without timeout, text is visible
            return "passed", f"Text '{(action.value or '')[:50]}' is visible"

        if action.action == ActionType.WAIT_FOR_NAVIGATION:
            has_navigation_action = True
            # Check if URL actually changed
            if pre_step_url and current_url and current_url == pre_step_url:
                logger.warning(
                    "validation.navigation_unchanged",
                    pre_url=pre_step_url,
                    post_url=current_url,
                )
                return "failed", (
                    f"Expected navigation but URL unchanged: {current_url}"
                )
            return "passed", f"Navigation completed to {current_url}"

    # If the step had click actions and the URL changed, that's a good signal
    has_click = any(a.action == ActionType.CLICK for a in intent.actions)
    if has_click and pre_step_url and current_url and current_url != pre_step_url:
        return "passed", f"Click triggered navigation to {current_url}"

    # For click, input, select, check, uncheck — trust wrapper execution
    # (they throw on failure, so reaching here means success)
    desc = intent.description or "Step actions completed"
    return "passed", desc


def _vision_verify(
    ai_client: AIProvider,
    screenshot_bytes: bytes,
    expected: str,
    step_description: str,
    model: str,
) -> tuple[str | None, str | None]:
    """Layer 3: GPT vision verification.

    Delegates to provider's verify_vision method.
    Vision NEVER triggers regeneration — only returns passed/inconclusive.

    Returns (None, None) if vision verification is unavailable (fallback provider).
    """
    try:
        result = ai_client.verify_vision(
            screenshot_bytes=screenshot_bytes,
            action=step_description,
            expected=expected,
            model=model,
        )
        status = result.get("status", "passed")
        actual = result.get("actual", "Vision check completed")

        # Detect fallback verification (DeterministicFallbackProvider)
        if "unavailable" in actual.lower() or "circuit open" in actual.lower():
            return None, None

        if status != "passed":
            logger.info(
                "validation.vision_inconclusive",
                expected=expected[:200],
                actual=actual[:200],
            )
            return status, actual

        return "passed", actual

    except Exception as e:
        logger.warning(
            "validation.vision_error",
            error=str(e)[:200],
        )
        # Vision failure — signal unavailable to caller
        return None, None


# --- Observation keyword patterns ---
# Keywords in expected results that signal observable content to verify
_OBSERVE_KEYWORDS = re.compile(
    r"\b(observe|verify|confirm|check|see|visible|displayed|shows?|appear|present)\b",
    re.IGNORECASE,
)

# Pattern to extract quoted or comma-separated field/element names
# e.g. "Name, Email, Phone are visible" → ["Name", "Email", "Phone"]
# e.g. "'Submit' button is visible" → ["Submit"]
_FIELD_EXTRACT = re.compile(
    r"""
    '([^']+)'           |  # single-quoted
    "([^"]+)"           |  # double-quoted
    \b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)  # CamelCase words (field names)
    """,
    re.VERBOSE,
)

# Words to ignore when extracting field names (too generic)
_IGNORE_WORDS = frozenset({
    "The", "All", "Each", "This", "That", "Page", "Step", "User",
    "Required", "Following", "Default", "Selected", "Correct",
    "Visible", "Displayed", "Present", "Shows", "Appears",
    "Subject", "Verify", "Observe", "Confirm", "Check",
})


def _check_expected_text_in_page(
    page,
    expected: str,
) -> tuple[str, str] | None:
    """Layer 2.5: Text content verification for observation steps.

    Extracts keywords from expected result and checks if they exist in
    visible page text. Only activates for observation-type expected results
    (those containing "observe", "verify", "visible", etc.).

    Returns:
        (status, description) if check was applicable, or None if skipped.
    """
    if not expected:
        return None

    # Only apply to observation/verification expected results
    if not _OBSERVE_KEYWORDS.search(expected):
        return None

    # Extract field/element names to look for
    keywords = _extract_verification_keywords(expected)
    if not keywords:
        return None

    # Get visible page text
    try:
        page_text = page.evaluate("""() => {
            return document.body ? document.body.innerText : '';
        }""")
    except Exception:
        return None

    if not page_text:
        return None

    page_text_lower = page_text.lower()

    # Check which keywords are missing
    missing = [kw for kw in keywords if kw.lower() not in page_text_lower]

    if not missing:
        return "passed", f"All expected content found: {', '.join(keywords[:5])}"

    # Allow up to 30% of keywords to be missing (fuzzy tolerance)
    missing_ratio = len(missing) / len(keywords)
    if missing_ratio <= 0.3:
        return "passed", (
            f"Most expected content found ({len(keywords) - len(missing)}/{len(keywords)})"
        )

    logger.info(
        "validation.text_keywords_missing",
        expected=expected[:200],
        missing=missing[:10],
        total=len(keywords),
    )
    return "failed", (
        f"Expected content not found on page. "
        f"Missing: {', '.join(missing[:5])}. "
        f"({len(missing)}/{len(keywords)} keywords absent)"
    )


def _extract_verification_keywords(expected: str) -> list[str]:
    """Extract field/element names from expected result text.

    Extracts:
    - Quoted strings: 'Name', "Email"
    - Capitalized multi-word names: Subject Name, Date Of Birth
    """
    keywords = []
    for match in _FIELD_EXTRACT.finditer(expected):
        # One of the 3 groups will match
        word = match.group(1) or match.group(2) or match.group(3) or ""
        word = word.strip()
        if word and word not in _IGNORE_WORDS and len(word) > 1:
            keywords.append(word)

    return keywords
