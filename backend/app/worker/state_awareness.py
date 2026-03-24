"""State-Aware Execution — skip already-satisfied goals.

Before executing an action, checks whether the intended effect is already
present in the page state. This avoids:
- Re-navigating to an already-loaded URL
- Re-filling an input that already has the correct value
- Re-clicking a checkbox that's already checked
- Re-selecting a dropdown that already shows the right option

Detection is deterministic (DOM inspection) with no AI involved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from app.worker.intent_schema import ActionIntent, ActionType

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


def is_action_already_satisfied(
    page: Page,
    action: ActionIntent,
    locator: Locator | None = None,
) -> bool:
    """Check if an action's intended effect is already present.

    Returns True if the action can be skipped. False if it needs execution.
    Only checks actions where state can be unambiguously verified.
    """
    try:
        if action.action == ActionType.NAVIGATE:
            return _is_already_on_url(page, action.value or "")

        if action.action == ActionType.INPUT and locator is not None:
            return _input_has_value(locator, action.value or "")

        if action.action == ActionType.CHECK and locator is not None:
            return _is_checked(locator)

        if action.action == ActionType.UNCHECK and locator is not None:
            return not _is_checked(locator)

        if action.action == ActionType.ASSERT_VISIBLE and locator is not None:
            return _is_visible(locator)

        if action.action == ActionType.ASSERT_TEXT and locator is not None:
            return _has_text(locator, action.value or "")

        if action.action == ActionType.ASSERT_VALUE and locator is not None:
            return _input_has_value(locator, action.value or "")

        if action.action == ActionType.WAIT_FOR_TEXT:
            return _text_exists(page, action.value or "")

        if action.action == ActionType.WAIT_FOR_NAVIGATION:
            # Never auto-skip: wait_for_navigation must always execute to detect
            # URL changes. A wildcard "*" would match any URL and incorrectly skip.
            return False

    except Exception:
        # Any detection failure → not satisfied → execute normally
        return False

    # Actions like click, select, select_date, upload_file — always execute
    return False


def _is_already_on_url(page: Page, target_url: str) -> bool:
    """Check if page is already at the target URL."""
    if not target_url:
        return False
    current = page.url
    # Exact match
    if current == target_url:
        return True
    # Path match (ignoring query params and trailing slash)
    from urllib.parse import urlparse
    current_path = urlparse(current).path.rstrip("/")
    target_path = urlparse(target_url).path.rstrip("/")
    if current_path and target_path and current_path == target_path:
        return True
    # Pattern match (for glob/regex patterns)
    if "*" in target_url:
        import fnmatch
        return fnmatch.fnmatch(current, target_url)
    return False


def _input_has_value(locator: Locator, expected: str) -> bool:
    """Check if input already has the expected value."""
    if not expected:
        return False
    try:
        current = locator.input_value(timeout=1000)
        return current == expected
    except Exception:
        return False


def _is_checked(locator: Locator) -> bool:
    """Check if checkbox is checked."""
    try:
        return locator.is_checked(timeout=1000)
    except Exception:
        return False


def _is_visible(locator: Locator) -> bool:
    """Check if element is visible."""
    try:
        return locator.is_visible(timeout=1000)
    except Exception:
        return False


def _has_text(locator: Locator, expected: str) -> bool:
    """Check if element contains the expected text."""
    if not expected:
        return False
    try:
        text = locator.text_content(timeout=1000)
        return expected in (text or "")
    except Exception:
        return False


def _text_exists(page: Page, text: str) -> bool:
    """Check if text is visible on the page."""
    if not text:
        return False
    try:
        loc = page.get_by_text(text)
        count = loc.count()
        if count == 0:
            return False
        # Check each match for visibility (avoid blind .first)
        for i in range(min(count, 5)):
            try:
                if loc.nth(i).is_visible(timeout=500):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False
