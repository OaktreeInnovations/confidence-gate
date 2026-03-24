"""Deterministic Playwright interaction wrappers.

These functions wrap common Playwright actions (click, fill, select, goto) with:
- Scroll-into-view before visibility wait (handles below-fold elements)
- Deadline-aware timeouts (respects step execution budget)
- Retry logic for transient failures AND timeouts
- Structured error reporting for telemetry

Available inside the sandbox as `safe_click`, `safe_fill`, `safe_select`, `safe_goto`.
AI-generated code MUST use these instead of raw locator methods.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import structlog

from app.worker.component_helpers import resolve_single
from app.worker.execution_budget import budget_wait

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)

# --- Execution deadline (thread-local) ---
# Set by sandboxed_exec before running AI code. All safe_* wrappers
# check this to avoid burning past the step budget.
_deadline = threading.local()


def set_execution_deadline(deadline_monotonic: float | None) -> None:
    """Set/clear the execution deadline for the current thread."""
    _deadline.time = deadline_monotonic


def _remaining_ms() -> int:
    """Milliseconds remaining until the deadline, or a large default."""
    dl = getattr(_deadline, "time", None)
    if dl is None:
        return 30_000  # No deadline set — default cap
    remaining = int((dl - time.monotonic()) * 1000)
    if remaining <= 0:
        raise TimeoutError("Step execution budget exceeded")
    return remaining


def _budget_sleep(seconds: float, context: str = "") -> None:
    """Budget-aware time.sleep replacement.

    Caps sleep at remaining step deadline to prevent budget overrun.
    Falls back to normal sleep if no deadline is set.
    """
    dl = getattr(_deadline, "time", None)
    if dl is not None:
        remaining_s = dl - time.monotonic()
        if remaining_s <= 0:
            raise TimeoutError("Step execution budget exceeded")
        seconds = min(seconds, remaining_s)
    time.sleep(seconds)


def _effective_timeout(requested_ms: int) -> int:
    """Return the lesser of requested_ms and remaining budget."""
    return min(requested_ms, _remaining_ms())


# Errors worth retrying — includes timeouts now
_RETRYABLE_PATTERNS = (
    # Transient DOM issues
    "element is detached",
    "element is not attached",
    "intercepted",
    "element is outside",
    "not in the viewport",
    "is hidden",
    "not visible",
    # Timeouts (element may appear slightly after budget)
    "timeout",
    "exceeded",
    # Overlay / click interception
    "other element would receive",
    "pointer event",
)

_MAX_RETRIES = 2
_VISIBILITY_TIMEOUT_MS = 5000
_POST_CLICK_SETTLE_MS = 300
_POST_FILL_SETTLE_MS = 200


def _is_retryable(error: str) -> bool:
    msg = error.lower()
    return any(p in msg for p in _RETRYABLE_PATTERNS)


def _is_overlay_error(error: str) -> bool:
    """Check if error is caused by an overlay intercepting pointer events."""
    msg = error.lower()
    return "intercepts pointer events" in msg or "other element would receive" in msg


def _scaled_timeout(base_ms: int, attempt: int) -> int:
    """Progressive timeout: attempt 1 = base, 2 = 1.5x."""
    return int(base_ms * (1.0 + 0.5 * (attempt - 1)))


def _scroll_and_wait_visible(locator: Locator, timeout_ms: int) -> None:
    """Scroll element into view, then wait for visible state."""
    timeout_ms = _effective_timeout(timeout_ms)
    try:
        locator.scroll_into_view_if_needed(timeout=min(timeout_ms, 3000))
    except Exception:
        pass  # Element may not exist yet — wait_for will handle it
    locator.wait_for(state="visible", timeout=timeout_ms)


def _click_option(locator: Locator) -> None:
    """Click a locator with force-click fallback for overlay interception."""
    try:
        locator.click()
    except Exception as e:
        if _is_overlay_error(str(e)):
            logger.info("_click_option.overlay_fallback", error=str(e)[:200])
            try:
                locator.click(force=True, timeout=2000)
                return
            except Exception:
                locator.evaluate("""el => {
                    const opts = {bubbles: true, cancelable: true, pointerId: 1};
                    el.dispatchEvent(new PointerEvent('pointerdown', opts));
                    el.dispatchEvent(new PointerEvent('pointerup', opts));
                    el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                }""")
                return
        raise


def safe_click(
    locator: Locator,
    *,
    timeout_ms: int = _VISIBILITY_TIMEOUT_MS,
    post_settle_ms: int = _POST_CLICK_SETTLE_MS,
) -> None:
    """Click a locator with scroll, visibility wait, and retry with progressive timeout.

    Usage in AI-generated code:
        safe_click(page.get_by_role("button", name="Submit").first)
    """
    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            _scroll_and_wait_visible(locator, _scaled_timeout(timeout_ms, attempt))
            locator.click()
            if post_settle_ms > 0:
                budget_wait(locator.page, post_settle_ms, "safe_click.post_settle")
            return
        except Exception as e:
            last_error = e
            err_str = str(e)

            # Budget exceeded — stop immediately
            if "Step execution budget exceeded" in err_str:
                raise

            # Overlay interception — use force=True click (real browser click
            # bypassing coverage check), then JS dispatch as last resort
            if _is_overlay_error(err_str):
                logger.info(
                    "safe_click.overlay_detected",
                    attempt=attempt,
                    error=err_str[:200],
                )
                try:
                    locator.click(force=True, timeout=2000)
                    if post_settle_ms > 0:
                        budget_wait(locator.page, post_settle_ms, "safe_click.overlay_settle")
                    return
                except Exception:
                    pass
                # JS dispatch as last resort
                try:
                    locator.evaluate("""el => {
                        const opts = {bubbles: true, cancelable: true, pointerId: 1};
                        el.dispatchEvent(new PointerEvent('pointerdown', opts));
                        el.dispatchEvent(new PointerEvent('pointerup', opts));
                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    }""")
                    if post_settle_ms > 0:
                        budget_wait(locator.page, post_settle_ms, "safe_click.overlay_settle")
                    return
                except Exception:
                    pass

            if attempt < _MAX_RETRIES and _is_retryable(err_str):
                _budget_sleep(0.5 * attempt, "safe_click.retry_backoff")
                continue
            raise
    raise last_error  # type: ignore[misc]


def safe_fill(
    locator: Locator,
    value: str,
    *,
    clear_first: bool = True,
    timeout_ms: int = _VISIBILITY_TIMEOUT_MS,
    post_settle_ms: int = _POST_FILL_SETTLE_MS,
) -> None:
    """Fill an input with scroll, visibility wait, optional clear, and retry.

    Usage in AI-generated code:
        safe_fill(page.get_by_label("Email").first, "user@example.com")
    """
    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            _scroll_and_wait_visible(locator, _scaled_timeout(timeout_ms, attempt))
            if clear_first:
                locator.fill("")
            locator.fill(value)
            if post_settle_ms > 0:
                budget_wait(locator.page, post_settle_ms, "safe_fill.post_settle")
            return
        except Exception as e:
            last_error = e
            if "Step execution budget exceeded" in str(e):
                raise
            if attempt < _MAX_RETRIES and _is_retryable(str(e)):
                _budget_sleep(0.5 * attempt, "safe_fill.retry_backoff")
                continue
            raise
    raise last_error  # type: ignore[misc]


def safe_select(
    locator: Locator,
    value: str,
    *,
    timeout_ms: int = _VISIBILITY_TIMEOUT_MS,
    post_settle_ms: int = 500,
) -> None:
    """Select an option from a <select> element or custom dropdown.

    Tries native select_option first. If that fails (custom dropdown),
    falls back to click-then-pick-option pattern.

    Usage in AI-generated code:
        safe_select(page.get_by_label("Country").first, "Philippines")
    """
    _scroll_and_wait_visible(locator, timeout_ms)

    # Try native <select> first
    try:
        locator.select_option(value, timeout=_effective_timeout(3000))
        if post_settle_ms > 0:
            budget_wait(locator.page, post_settle_ms, "safe_select.native")
        return
    except Exception:
        pass

    # Try by label
    try:
        locator.select_option(label=value, timeout=_effective_timeout(3000))
        if post_settle_ms > 0:
            budget_wait(locator.page, post_settle_ms, "safe_select.by_label")
        return
    except Exception:
        pass

    # Custom dropdown fallback: click to open, then find option
    page = locator.page
    locator.click()
    budget_wait(page, 500, "safe_select.open_dropdown")

    # Try role="option" first
    try:
        option = resolve_single(page.get_by_role("option", name=value), context=f"select_option:{value[:20]}", timeout_ms=3000)
        option.wait_for(state="visible", timeout=_effective_timeout(3000))
        _click_option(option)
        budget_wait(page, post_settle_ms, "safe_select.option")
        return
    except Exception:
        pass

    # Try role="menuitem"
    try:
        item = resolve_single(page.get_by_role("menuitem", name=value), context=f"select_menuitem:{value[:20]}", timeout_ms=3000)
        item.wait_for(state="visible", timeout=_effective_timeout(3000))
        _click_option(item)
        budget_wait(page, post_settle_ms, "safe_select.menuitem")
        return
    except Exception:
        pass

    # Try text match within listbox/menu
    try:
        listbox = resolve_single(page.get_by_role("listbox"), context="select_listbox", timeout_ms=3000)
        listbox.wait_for(state="visible", timeout=_effective_timeout(3000))
        target = resolve_single(listbox.get_by_text(value), context=f"select_listbox_text:{value[:20]}")
        _click_option(target)
        budget_wait(page, post_settle_ms, "safe_select.listbox")
        return
    except Exception:
        pass

    # Keyboard fallback: type to filter + Enter (Radix/Shadcn typeahead)
    try:
        page.keyboard.type(value[:3], delay=100)
        budget_wait(page, 500, "safe_select.typeahead")
        page.keyboard.press("Enter")
        budget_wait(page, post_settle_ms, "safe_select.typeahead_settle")
        return
    except Exception:
        pass

    # Last resort: force click on any visible text match
    target = resolve_single(page.get_by_text(value), context=f"select_text_fallback:{value[:20]}")
    _click_option(target)
    budget_wait(page, post_settle_ms, "safe_select.text_fallback")


def safe_goto(
    page: Page,
    url: str,
    *,
    wait_until: str = "load",
    post_settle_ms: int = 2000,
    timeout_ms: int = 30000,
) -> None:
    """Navigate to a URL with load-state wait and post-navigation settle.

    Usage in AI-generated code:
        safe_goto(page, "https://example.com/login")
    """
    page.goto(url, wait_until=wait_until, timeout=_effective_timeout(timeout_ms))
    try:
        page.wait_for_load_state("domcontentloaded", timeout=_effective_timeout(5000))
    except Exception:
        pass
    if post_settle_ms > 0:
        budget_wait(page, post_settle_ms, "safe_goto.post_settle")
