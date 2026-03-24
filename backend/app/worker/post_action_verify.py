"""Post-action state verification — confirms each action had its intended effect.

Called after each action in _execute_single_action(). Verification failure
triggers a warning (non-fatal) — the retry loop in ai_executor handles recovery.

Per-action-type verification:
- click: URL changed OR aria-expanded toggled OR new listbox/menu appeared OR DOM mutated
- fill/input: element.value matches expected
- navigate: URL matches expected
- select: option text appears in trigger or aria-selected found
- check: locator.is_checked() == True
- uncheck: locator.is_checked() == False
- select_date: input value updated OR aria-selected date present
- upload_file: input.files length > 0 OR file name in DOM
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


@dataclass
class ActionVerification:
    """Result of post-action state verification."""

    verified: bool
    method: str  # "url_change", "aria_expanded", "dom_mutation", "value_match", "none"
    detail: str = ""
    elapsed_ms: int = 0


def capture_pre_state(page: Page, locator: Locator | None, action_type: str) -> dict:
    """Snapshot state before action for comparison.

    Args:
        page: Playwright Page.
        locator: Target element locator (may be None for navigate).
        action_type: ActionType value string.

    Returns:
        Dict of pre-state values for post-action comparison.
    """
    state: dict = {"url": page.url}

    if locator is not None:
        try:
            state["aria_expanded"] = locator.get_attribute("aria-expanded", timeout=500)
        except Exception:
            state["aria_expanded"] = None

        if action_type in ("click",):
            try:
                state["listbox_count"] = page.get_by_role("listbox").count()
            except Exception:
                state["listbox_count"] = 0

            try:
                state["menu_count"] = page.get_by_role("menu").count()
            except Exception:
                state["menu_count"] = 0

        if action_type in ("input",):
            try:
                state["current_value"] = locator.input_value(timeout=500)
            except Exception:
                state["current_value"] = ""

    return state


def verify_click(
    page: Page, locator: Locator | None, pre_state: dict,
) -> ActionVerification:
    """Verify click had an effect: URL, aria-expanded, listbox/menu, or DOM change."""
    t0 = time.monotonic()

    # Check 1: URL changed
    if page.url != pre_state.get("url", ""):
        return ActionVerification(
            verified=True,
            method="url_change",
            detail=f"URL changed to {page.url[:100]}",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    if locator is not None:
        # Check 2: aria-expanded toggled
        try:
            new_expanded = locator.get_attribute("aria-expanded", timeout=500)
            if new_expanded != pre_state.get("aria_expanded"):
                return ActionVerification(
                    verified=True,
                    method="aria_expanded",
                    detail=f"aria-expanded: {pre_state.get('aria_expanded')} -> {new_expanded}",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
        except Exception:
            pass

    # Check 3: New listbox or menu appeared
    try:
        new_listbox_count = page.get_by_role("listbox").count()
        if new_listbox_count > pre_state.get("listbox_count", 0):
            return ActionVerification(
                verified=True,
                method="dom_mutation",
                detail=f"New listbox appeared ({pre_state.get('listbox_count', 0)} -> {new_listbox_count})",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
    except Exception:
        pass

    try:
        new_menu_count = page.get_by_role("menu").count()
        if new_menu_count > pre_state.get("menu_count", 0):
            return ActionVerification(
                verified=True,
                method="dom_mutation",
                detail=f"New menu appeared ({pre_state.get('menu_count', 0)} -> {new_menu_count})",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
    except Exception:
        pass

    # No observable change detected
    return ActionVerification(
        verified=False,
        method="none",
        detail="No observable state change after click",
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


def verify_fill(
    locator: Locator, expected_value: str,
) -> ActionVerification:
    """Verify fill: element value matches expected."""
    t0 = time.monotonic()

    try:
        actual = locator.input_value(timeout=1000)
        if actual == expected_value:
            return ActionVerification(
                verified=True,
                method="value_match",
                detail=f"Value matches: '{expected_value[:50]}'",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
        return ActionVerification(
            verified=False,
            method="value_match",
            detail=f"Value mismatch: expected '{expected_value[:50]}', got '{actual[:50]}'",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception:
        return ActionVerification(
            verified=False,
            method="none",
            detail="Could not read element value",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )


def verify_navigate(
    page: Page, expected_url: str,
) -> ActionVerification:
    """Verify navigation: URL matches expected pattern."""
    t0 = time.monotonic()

    current = page.url
    # Check if expected URL is a substring or exact match
    if expected_url in current or current.startswith(expected_url):
        return ActionVerification(
            verified=True,
            method="url_change",
            detail=f"Navigated to {current[:100]}",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    return ActionVerification(
        verified=False,
        method="url_change",
        detail=f"Expected URL '{expected_url[:50]}', got '{current[:50]}'",
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


def verify_select(
    page: Page, locator: Locator | None, expected_option: str,
) -> ActionVerification:
    """Verify select: chosen option text visible or aria-selected set.

    Checks:
    1. The trigger element's text contains the expected option
    2. An element with aria-selected="true" containing the option text exists
    """
    t0 = time.monotonic()

    if not expected_option:
        return ActionVerification(
            verified=True,
            method="none",
            detail="No expected option to verify",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    # Check 1: Trigger text contains expected option
    if locator is not None:
        try:
            text = locator.inner_text(timeout=1000)
            if expected_option.lower() in text.lower():
                return ActionVerification(
                    verified=True,
                    method="option_selected",
                    detail=f"Trigger text contains '{expected_option[:50]}'",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
        except Exception:
            pass

    # Check 2: aria-selected="true" on an element with matching text
    try:
        selected = page.locator('[aria-selected="true"]')
        count = selected.count()
        for i in range(min(count, 10)):
            try:
                text = selected.nth(i).inner_text(timeout=500)
                if expected_option.lower() in text.lower():
                    return ActionVerification(
                        verified=True,
                        method="option_selected",
                        detail=f"aria-selected element contains '{expected_option[:50]}'",
                        elapsed_ms=int((time.monotonic() - t0) * 1000),
                    )
            except Exception:
                continue
    except Exception:
        pass

    return ActionVerification(
        verified=False,
        method="option_selected",
        detail=f"Option '{expected_option[:50]}' not confirmed in trigger or aria-selected",
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


def verify_check(locator: Locator) -> ActionVerification:
    """Verify check: element is checked after action."""
    t0 = time.monotonic()
    try:
        if locator.is_checked(timeout=1000):
            return ActionVerification(
                verified=True,
                method="checked_state",
                detail="Checkbox is checked",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
        return ActionVerification(
            verified=False,
            method="checked_state",
            detail="Checkbox is not checked after check action",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception:
        return ActionVerification(
            verified=False,
            method="none",
            detail="Could not read checked state",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )


def verify_uncheck(locator: Locator) -> ActionVerification:
    """Verify uncheck: element is unchecked after action."""
    t0 = time.monotonic()
    try:
        if not locator.is_checked(timeout=1000):
            return ActionVerification(
                verified=True,
                method="checked_state",
                detail="Checkbox is unchecked",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
        return ActionVerification(
            verified=False,
            method="checked_state",
            detail="Checkbox is still checked after uncheck action",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception:
        return ActionVerification(
            verified=False,
            method="none",
            detail="Could not read checked state",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )


def verify_select_date(
    page: Page, locator: Locator | None, expected_day: str,
) -> ActionVerification:
    """Verify date selection: input value updated or aria-selected date present."""
    t0 = time.monotonic()

    # Check 1: input value contains the expected day
    if locator is not None:
        try:
            value = locator.input_value(timeout=1000)
            if expected_day in value:
                return ActionVerification(
                    verified=True,
                    method="value_match",
                    detail=f"Date input contains '{expected_day}'",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
        except Exception:
            pass

        # Also check inner_text for non-input triggers
        try:
            text = locator.inner_text(timeout=500)
            if expected_day in text:
                return ActionVerification(
                    verified=True,
                    method="value_match",
                    detail=f"Date trigger contains '{expected_day}'",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
        except Exception:
            pass

    # Check 2: aria-selected="true" on a date cell containing the day
    try:
        selected = page.locator('[aria-selected="true"]')
        count = selected.count()
        for i in range(min(count, 10)):
            try:
                text = selected.nth(i).inner_text(timeout=500)
                if expected_day in text:
                    return ActionVerification(
                        verified=True,
                        method="option_selected",
                        detail=f"aria-selected date contains '{expected_day}'",
                        elapsed_ms=int((time.monotonic() - t0) * 1000),
                    )
            except Exception:
                continue
    except Exception:
        pass

    return ActionVerification(
        verified=False,
        method="value_match",
        detail=f"Date '{expected_day}' not confirmed in input or aria-selected",
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


def verify_upload(
    page: Page, locator: Locator | None, file_path: str,
) -> ActionVerification:
    """Verify file upload: input has files or file name visible in DOM."""
    t0 = time.monotonic()
    import os

    file_name = os.path.basename(file_path) if file_path else ""

    # Check 1: file input has files
    if locator is not None:
        try:
            has_files = locator.evaluate(
                "(el) => el.files && el.files.length > 0"
            )
            if has_files:
                return ActionVerification(
                    verified=True,
                    method="value_match",
                    detail="File input has files attached",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
        except Exception:
            pass

    # Check 2: file name visible on page
    if file_name:
        try:
            name_visible = page.get_by_text(file_name).count() > 0
            if name_visible:
                return ActionVerification(
                    verified=True,
                    method="value_match",
                    detail=f"File name '{file_name}' visible on page",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
        except Exception:
            pass

    return ActionVerification(
        verified=False,
        method="value_match",
        detail=f"File '{file_name}' not confirmed in input or DOM",
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
