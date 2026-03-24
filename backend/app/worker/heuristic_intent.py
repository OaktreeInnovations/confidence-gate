"""Heuristic intent generator for simple test steps.

Generates StepIntent from natural language step descriptions by matching
against regex patterns. No AI required.

Handles:
- Navigate: "Navigate to URL", "Go to URL"
- Click: "Click the X button/link/tab"
- Input: "Enter/Type/Fill 'value' in/into X"
- Assert visible: "Verify X is visible"
- Wait for text: "Verify page displays 'Welcome'"

Returns None for complex actions (select_date, upload_file, select)
or step descriptions that don't match any pattern.
"""

from __future__ import annotations

import re

import structlog

from app.worker.intent_schema import (
    ActionIntent,
    ActionType,
    StepIntent,
    TargetDescriptor,
)

logger = structlog.get_logger(__name__)

# --- Action verb patterns ---

_NAVIGATE_PATTERN = re.compile(
    r"^(?:navigate|go|open|visit|browse)\s+(?:to\s+)?(.+)",
    re.IGNORECASE,
)

_CLICK_PATTERN = re.compile(
    r"^(?:click|tap|press|hit)\s+(?:on\s+)?(?:the\s+)?(.+)",
    re.IGNORECASE,
)

_INPUT_PATTERN = re.compile(
    r'^(?:enter|type|fill|input|write)\s+["\'](.+?)["\']\s+(?:in|into)\s+(?:the\s+)?(.+)',
    re.IGNORECASE,
)

_INPUT_PATTERN_ALT = re.compile(
    r"^(?:fill|set)\s+(?:the\s+)?(.+?)\s+(?:field|input|textbox|textarea)\s+(?:with|to)\s+[\"']?(.+?)[\"']?\s*$",
    re.IGNORECASE,
)

_ASSERT_VISIBLE_PATTERN = re.compile(
    r"^(?:verify|assert|check|confirm|ensure)\s+(?:that\s+)?(?:the\s+)?(.+?)\s+(?:is\s+)?(?:visible|displayed|shown|present)",
    re.IGNORECASE,
)

_ASSERT_TEXT_PATTERN = re.compile(
    r'^(?:verify|assert|check|confirm|ensure)\s+(?:that\s+)?(?:the\s+)?(?:page\s+)?(?:displays?|shows?|contains?)\s+["\'](.+?)["\']\s*$',
    re.IGNORECASE,
)

_WAIT_TEXT_PATTERN = re.compile(
    r'^(?:wait\s+(?:for|until)\s+)?["\'](.+?)["\']\s+(?:appears?|is\s+visible|is\s+shown)',
    re.IGNORECASE,
)

# --- Role keywords for target resolution ---

_ROLE_KEYWORDS: dict[str, str] = {
    "button": "button",
    "link": "link",
    "tab": "tab",
    "checkbox": "checkbox",
    "radio button": "radio",
    "radio": "radio",
    "switch": "switch",
    "menu item": "menuitem",
    "menuitem": "menuitem",
    "heading": "heading",
    "textbox": "textbox",
    "combobox": "combobox",
}


def generate_heuristic_intent(
    step_description: str,
    step_number: int,
    page_context: dict | None = None,
) -> StepIntent | None:
    """Attempt to generate a StepIntent from a natural language step description.

    Returns None if the step is too complex for heuristic parsing.
    """
    desc = step_description.strip()
    if not desc:
        return None

    action_intent = (
        _try_navigate(desc)
        or _try_input(desc)
        or _try_click(desc)
        or _try_assert_text(desc)
        or _try_assert_visible(desc)
        or _try_wait_text(desc)
    )

    if action_intent is None:
        logger.info(
            "heuristic_intent.no_match",
            step=step_number,
            description=desc[:100],
        )
        return None

    intent = StepIntent(
        step_number=step_number,
        actions=[action_intent],
        description=f"[heuristic] {desc}",
    )

    logger.info(
        "heuristic_intent.generated",
        step=step_number,
        action=action_intent.action.value,
        description=desc[:100],
    )

    return intent


def _try_navigate(desc: str) -> ActionIntent | None:
    m = _NAVIGATE_PATTERN.match(desc)
    if not m:
        return None
    url = m.group(1).strip().strip("\"'")
    if not url.startswith(("http://", "https://", "/")):
        return None
    return ActionIntent(action=ActionType.NAVIGATE, value=url)


def _try_click(desc: str) -> ActionIntent | None:
    m = _CLICK_PATTERN.match(desc)
    if not m:
        return None
    target_desc = m.group(1).strip().strip("\"'")
    target = _resolve_target_from_description(target_desc)
    return ActionIntent(action=ActionType.CLICK, target=target)


def _try_input(desc: str) -> ActionIntent | None:
    m = _INPUT_PATTERN.match(desc)
    if m:
        value = m.group(1).strip()
        field_desc = m.group(2).strip().strip("\"'")
        target = _resolve_target_from_description(field_desc, prefer_input=True)
        return ActionIntent(action=ActionType.INPUT, target=target, value=value)

    m = _INPUT_PATTERN_ALT.match(desc)
    if m:
        field_desc = m.group(1).strip()
        value = m.group(2).strip()
        target = _resolve_target_from_description(field_desc, prefer_input=True)
        return ActionIntent(action=ActionType.INPUT, target=target, value=value)

    return None


def _try_assert_text(desc: str) -> ActionIntent | None:
    m = _ASSERT_TEXT_PATTERN.match(desc)
    if not m:
        return None
    text = m.group(1).strip()
    return ActionIntent(action=ActionType.WAIT_FOR_TEXT, value=text)


def _try_assert_visible(desc: str) -> ActionIntent | None:
    m = _ASSERT_VISIBLE_PATTERN.match(desc)
    if not m:
        return None
    element_desc = m.group(1).strip()
    target = _resolve_target_from_description(element_desc)
    return ActionIntent(action=ActionType.ASSERT_VISIBLE, target=target)


def _try_wait_text(desc: str) -> ActionIntent | None:
    m = _WAIT_TEXT_PATTERN.match(desc)
    if not m:
        return None
    text = m.group(1).strip()
    return ActionIntent(action=ActionType.WAIT_FOR_TEXT, value=text)


def _resolve_target_from_description(
    desc: str,
    prefer_input: bool = False,
) -> TargetDescriptor:
    """Build a TargetDescriptor from a natural language element description.

    Extracts role and name from phrases like "Submit button", "Email field".
    """
    desc_lower = desc.lower().strip()

    # Check for role keywords (longer keywords first to avoid partial matches)
    detected_role = None
    element_name = desc

    for keyword in sorted(_ROLE_KEYWORDS, key=len, reverse=True):
        if keyword in desc_lower:
            detected_role = _ROLE_KEYWORDS[keyword]
            idx = desc_lower.rfind(keyword)
            element_name = desc[:idx].strip().strip("\"'")
            if not element_name:
                element_name = desc[idx + len(keyword) :].strip().strip("\"'")
            break

    # Clean up common articles
    for article in ("the ", "a ", "an "):
        if element_name.lower().startswith(article):
            element_name = element_name[len(article) :]

    element_name = element_name.strip().strip("\"'")

    # For "field" / "input" descriptions, use label-based targeting
    if prefer_input and not detected_role:
        # Strip trailing "field", "input" etc.
        for suffix in (" field", " input", " textbox", " textarea", " box"):
            if element_name.lower().endswith(suffix):
                element_name = element_name[: -len(suffix)].strip()
                break
        return TargetDescriptor(label=element_name)

    if detected_role:
        return TargetDescriptor(role=detected_role, name=element_name if element_name else None)

    # Fallback: use text for matching
    return TargetDescriptor(text=element_name)
