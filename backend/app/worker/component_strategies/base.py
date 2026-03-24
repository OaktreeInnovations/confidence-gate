"""Shared helpers for component interaction strategies.

Overlay-aware click utilities and keyboard helpers used across
date picker, dropdown, autocomplete, and other component modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from app.worker.execution_budget import budget_wait

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


def _is_overlay_error(error: str) -> bool:
    """Check if error is caused by an overlay intercepting pointer events."""
    msg = error.lower()
    return "intercepts pointer events" in msg or "other element would receive" in msg


def _click_with_overlay_fallback(locator: Locator, timeout_ms: int = 2000) -> None:
    """Click a locator, falling back to force-click on overlay interception.

    When an overlay blocks standard clicks, uses Playwright's force=True
    which performs a real browser click bypassing the coverage check.
    Falls back to JS pointer dispatch as last resort.
    Uses a short timeout (2s) to avoid wasting budget on retry loops.
    """
    try:
        locator.click(timeout=timeout_ms)
    except Exception as e:
        if _is_overlay_error(str(e)):
            logger.info("component.overlay_force_click", error=str(e)[:200])
            try:
                locator.click(force=True, timeout=timeout_ms)
                return
            except Exception:
                logger.info("component.overlay_js_fallback")
                _js_pointer_click(locator)
                return
        raise


def _js_pointer_click(locator: Locator) -> None:
    """Dispatch pointerdown + pointerup + click via JS, bypassing overlays.

    Radix UI Select/Menu items listen on pointerup for selection.
    Regular components listen on click. We fire all three to cover both.
    """
    locator.evaluate("""el => {
        const opts = {bubbles: true, cancelable: true, pointerId: 1};
        el.dispatchEvent(new PointerEvent('pointerdown', opts));
        el.dispatchEvent(new PointerEvent('pointerup', opts));
        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    }""")


def _select_by_keyboard(page: Page, option_text: str) -> None:
    """Select a dropdown option using keyboard typeahead + Enter.

    Works with Radix UI, Shadcn, and other components that support
    typeahead navigation in open dropdown/select popups.
    """
    page.keyboard.type(option_text[:3], delay=100)
    budget_wait(page, 500, "keyboard_typeahead")
    page.keyboard.press("Enter")
    budget_wait(page, 300, "keyboard_enter")
