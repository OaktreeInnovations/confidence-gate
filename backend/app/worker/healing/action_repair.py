"""Action Repair Engine — alternate interaction patterns per action type.

When the element is found but the action fails (click doesn't cause state change,
select doesn't work), this module tries alternate interaction patterns:

- click → focus + Enter, JS dispatch click, dblclick
- fill → press_sequentially, clear + type, JS value assignment
- select → click trigger + click option by text, type into combobox

Each alternate is verified via existing post_action_verify functions.
First verified alternate wins.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from app.worker.intent_schema import ActionType
from app.worker.stability_wrappers import _budget_sleep
from app.worker.post_action_verify import (
    ActionVerification,
    capture_pre_state,
    verify_click,
    verify_fill,
    verify_select,
)

if TYPE_CHECKING:
    from app.worker.intent_schema import ActionIntent
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


@dataclass
class ActionRepairResult:
    """Result of an action repair attempt."""

    repaired: bool
    method_used: str = ""
    attempts: int = 0
    verification: ActionVerification | None = None
    elapsed_ms: int = 0


def repair_action(
    page: Page,
    locator: Locator,
    action: ActionIntent,
    pre_state: dict,
    max_attempts: int = 3,
) -> ActionRepairResult:
    """Attempt to repair a failed action using alternate interaction patterns.

    Args:
        page: Playwright Page instance.
        locator: Resolved locator for the target element.
        action: The action that failed.
        pre_state: Pre-action state captured before the original attempt.
        max_attempts: Maximum alternate methods to try.

    Returns:
        ActionRepairResult with verification from the first successful alternate.
    """
    t0 = time.monotonic()

    if action.action == ActionType.CLICK:
        result = _repair_click(page, locator, pre_state, max_attempts)
    elif action.action == ActionType.INPUT:
        result = _repair_fill(page, locator, action.value or "", max_attempts)
    elif action.action == ActionType.SELECT:
        result = _repair_select(page, locator, action.value or "", max_attempts)
    else:
        result = ActionRepairResult(repaired=False)

    result.elapsed_ms = int((time.monotonic() - t0) * 1000)
    if result.repaired:
        logger.info(
            "action_repair.success",
            action=action.action.value,
            method=result.method_used,
            attempts=result.attempts,
            elapsed_ms=result.elapsed_ms,
        )
    else:
        logger.info(
            "action_repair.failed",
            action=action.action.value,
            attempts=result.attempts,
            elapsed_ms=result.elapsed_ms,
        )
    return result


def _repair_click(
    page: Page,
    locator: Locator,
    pre_state: dict,
    max_attempts: int,
) -> ActionRepairResult:
    """Try alternate click methods."""
    alternates = [
        ("focus_enter", _click_via_focus_enter),
        ("js_dispatch", _click_via_js_dispatch),
        ("dblclick", _click_via_dblclick),
    ]

    attempts = 0
    for method_name, method_fn in alternates[:max_attempts]:
        attempts += 1
        try:
            fresh_pre = capture_pre_state(page, locator, "click")
            method_fn(page, locator)
            verification = verify_click(page, locator, fresh_pre)
            if verification.verified:
                return ActionRepairResult(
                    repaired=True,
                    method_used=method_name,
                    attempts=attempts,
                    verification=verification,
                )
        except Exception as e:
            logger.debug(
                "action_repair.click_alternate_failed",
                method=method_name,
                error=str(e)[:200],
            )

    return ActionRepairResult(repaired=False, attempts=attempts)


def _repair_fill(
    page: Page,
    locator: Locator,
    value: str,
    max_attempts: int,
) -> ActionRepairResult:
    """Try alternate fill methods."""
    alternates = [
        ("press_sequentially", _fill_via_sequential),
        ("clear_and_type", _fill_via_clear_type),
        ("js_value", _fill_via_js_value),
    ]

    attempts = 0
    for method_name, method_fn in alternates[:max_attempts]:
        attempts += 1
        try:
            method_fn(page, locator, value)
            verification = verify_fill(locator, value)
            if verification.verified:
                return ActionRepairResult(
                    repaired=True,
                    method_used=method_name,
                    attempts=attempts,
                    verification=verification,
                )
        except Exception as e:
            logger.debug(
                "action_repair.fill_alternate_failed",
                method=method_name,
                error=str(e)[:200],
            )

    return ActionRepairResult(repaired=False, attempts=attempts)


def _repair_select(
    page: Page,
    locator: Locator,
    value: str,
    max_attempts: int,
) -> ActionRepairResult:
    """Try alternate select methods."""
    alternates = [
        ("click_option", _select_via_click_option),
        ("type_filter", _select_via_type_filter),
    ]

    attempts = 0
    for method_name, method_fn in alternates[:min(max_attempts, len(alternates))]:
        attempts += 1
        try:
            method_fn(page, locator, value)
            verification = verify_select(page, locator, value)
            if verification.verified:
                return ActionRepairResult(
                    repaired=True,
                    method_used=method_name,
                    attempts=attempts,
                    verification=verification,
                )
        except Exception as e:
            logger.debug(
                "action_repair.select_alternate_failed",
                method=method_name,
                error=str(e)[:200],
            )

    return ActionRepairResult(repaired=False, attempts=attempts)


# --- Click alternates ---


def _click_via_focus_enter(page: Page, locator: Locator) -> None:
    """Focus the element and press Enter."""
    locator.focus()
    page.keyboard.press("Enter")


def _click_via_js_dispatch(page: Page, locator: Locator) -> None:
    """Dispatch a click event via JavaScript."""
    locator.evaluate("""(el) => {
        el.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
        el.dispatchEvent(new PointerEvent('pointerup', {bubbles: true}));
        el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
    }""")


def _click_via_dblclick(page: Page, locator: Locator) -> None:
    """Double-click the element."""
    locator.dblclick()


# --- Fill alternates ---


def _fill_via_sequential(page: Page, locator: Locator, value: str) -> None:
    """Clear and type character by character."""
    locator.click()
    locator.evaluate("(el) => { el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); }")
    locator.press_sequentially(value, delay=20)


def _fill_via_clear_type(page: Page, locator: Locator, value: str) -> None:
    """Triple-click to select all, then type replacement."""
    locator.click(click_count=3)
    page.keyboard.type(value)


def _fill_via_js_value(page: Page, locator: Locator, value: str) -> None:
    """Set value directly via JS and dispatch input event."""
    locator.evaluate(
        "(el, val) => {"
        "  el.value = val;"
        "  el.dispatchEvent(new Event('input', {bubbles: true}));"
        "  el.dispatchEvent(new Event('change', {bubbles: true}));"
        "}",
        value,
    )


# --- Select alternates ---


def _select_via_click_option(page: Page, locator: Locator, value: str) -> None:
    """Click the trigger to open dropdown, then click the option by text."""
    locator.click()
    # Wait briefly for dropdown to appear
    _budget_sleep(0.3, "dropdown_open_settle")
    # Find and click the option
    option = page.get_by_role("option", name=value)
    if option.count() == 0:
        # Try listbox items
        option = page.locator(f'[role="listbox"] >> text="{value}"')
    option.first.click()


def _select_via_type_filter(page: Page, locator: Locator, value: str) -> None:
    """Type into combobox to filter, then select from filtered results."""
    locator.click()
    page.keyboard.type(value)
    _budget_sleep(0.3, "combobox_filter_settle")
    # Press Enter to select the first filtered result
    page.keyboard.press("Enter")
