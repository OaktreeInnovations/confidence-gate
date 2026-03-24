"""Date picker interaction strategy.

Handles calendar opening, month navigation, day selection, and
calendar dismissal across multiple UI frameworks (Shadcn/Radix,
MUI, Ant Design, react-datepicker, flatpickr, etc.).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import TYPE_CHECKING, Union

import structlog

from app.worker.component_helpers import resolve_single
from app.worker.execution_budget import budget_wait

from .base import _click_with_overlay_fallback, _js_pointer_click
from .locale import PLACEHOLDER_PATTERNS

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Calendar detection selectors — ordered generic-first, framework-specific last
# ---------------------------------------------------------------------------

_CALENDAR_CONTAINER_SELECTORS = [
    "[class*='calendar']",
    "[class*='datepicker']",
    "[class*='date-picker']",
    "[data-state='open'][class*='popover']",
    # Framework-specific
    ".react-datepicker",
    ".flatpickr-calendar",
    ".MuiPickersPopper-root",
    ".ant-picker-dropdown",
    "[data-radix-popper-content-wrapper]",
]

_CALENDAR_OPEN_SELECTORS = [
    "[class*='calendar'][class*='open']",
    "[data-state='open'][class*='popover']",
    ".react-datepicker__month-container",
    ".flatpickr-calendar.open",
    ".MuiPickersPopper-root",
    ".ant-picker-dropdown:not(.ant-picker-dropdown-hidden)",
    "[data-radix-popper-content-wrapper]",
]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def pick_date(
    page: Page,
    trigger: Union[Locator, str],
    day: Union[int, str],
    *,
    month_label: str = "",
    next_month_label: str = "Next month",
    max_month_clicks: int = 12,
) -> None:
    """Interact with a date picker component.

    Args:
        page: Playwright Page (or Locator — auto-extracts .page for modal compat).
        trigger: Locator or string label (e.g. "Start Date") for auto-discovery.
        day: Day number (1-31), OR relative string:
             "today", "today+5", "today-3", etc.
             When relative, auto-computes the day and month navigation.
        month_label: If set, navigate until this month is visible.
        next_month_label: Accessible name of the "next month" button.
        max_month_clicks: Safety cap on month navigation clicks.
    """
    # Support being called from interact_in_modal where first arg is a Locator
    if not hasattr(page, "goto"):
        page = page.page

    # Resolve relative dates ("today", "today+5", etc.)
    if isinstance(day, str):
        resolved = _resolve_relative_date(day)
        if resolved is not None:
            day = resolved.day
            # Auto-set month_label if the target is in a different month
            if not month_label and resolved.month != date.today().month:
                month_label = resolved.strftime("%B %Y")
            logger.info(
                "pick_date.resolved",
                input=str(day),
                date=str(resolved),
                month_label=month_label,
            )
        else:
            day = int(day)

    # Dismiss any pre-existing open calendar before starting
    _dismiss_calendar(page)

    if isinstance(trigger, str):
        trigger = _find_date_trigger(page, trigger)

    try:
        trigger.wait_for(state="visible", timeout=5000)
    except Exception:
        logger.warning("pick_date.trigger_not_visible", trigger=str(trigger)[:200])
        trigger = _find_any_date_input(page)

    # Capture trigger text before interaction to verify date was set later
    trigger_text_before = ""
    try:
        trigger_text_before = trigger.inner_text(timeout=1000).strip()
    except Exception:
        pass

    # IDEMPOTENCY CHECK: if the trigger already shows a date (not a placeholder),
    # the date was likely already set by a previous step. Skip re-selection to
    # avoid toggling the date off (Shadcn calendars toggle on re-click).
    if trigger_text_before and trigger_text_before.lower() not in PLACEHOLDER_PATTERNS:
        # Trigger shows a date-like value — check if it looks like a date (contains digits)
        if any(c.isdigit() for c in trigger_text_before):
            logger.info(
                "pick_date.already_set",
                current_value=trigger_text_before,
                requested_day=str(day),
            )
            return

    _click_with_overlay_fallback(trigger)
    budget_wait(page, 700, "calendar_open")

    # Find calendar container — retry trigger click if calendar doesn't open
    calendar = _find_calendar_container(page)
    if calendar is None:
        # Calendar didn't open — try JS click on trigger as fallback
        logger.info("pick_date.calendar_not_found_retrying_trigger")
        _js_pointer_click(trigger)
        budget_wait(page, 1000, "calendar_retry_open")
        calendar = _find_calendar_container(page)

    # Navigate months if needed
    if month_label and calendar:
        _navigate_to_month(page, calendar, month_label, next_month_label, max_month_clicks)

    # Select the day
    day_str = str(day)
    container = calendar if calendar else page

    _select_day_cell(container, day_str, page)

    budget_wait(page, 500, "day_selected")

    # Dismiss calendar popup if still open — prevents overlay blocking next interaction
    _dismiss_calendar(page, trigger)

    # Verify the date was actually set (trigger text should have changed)
    try:
        trigger_text_after = trigger.inner_text(timeout=1000).strip()
        if trigger_text_after and trigger_text_after != trigger_text_before:
            logger.info(
                "pick_date.verified",
                before=trigger_text_before,
                after=trigger_text_after,
            )
        elif trigger_text_before and trigger_text_after == trigger_text_before:
            logger.warning(
                "pick_date.date_may_not_be_set",
                trigger_text=trigger_text_after,
            )
    except Exception:
        pass


def _select_day_cell(container, day_str: str, page) -> None:
    """Find and click a day cell in the calendar container.

    Tries multiple strategies to handle different calendar implementations
    (Shadcn/react-day-picker, MUI, custom, etc.).
    """
    # Strategy 1: Button with exact text content (most reliable for Shadcn)
    try:
        btn = resolve_single(container.locator(f"button:text-is('{day_str}')"), context="select_day_cell:button_text_is")
        btn.wait_for(state="visible", timeout=3000)
        _click_with_overlay_fallback(btn)
        logger.info("pick_date.day_selected", strategy="button_text_is", day=day_str)
        return
    except Exception:
        pass

    # Strategy 2: Gridcell role with name (standard ARIA grid pattern)
    try:
        cell = resolve_single(container.get_by_role("gridcell", name=day_str), context="select_day_cell:gridcell_name")
        cell.wait_for(state="visible", timeout=3000)
        _click_with_overlay_fallback(cell)
        logger.info("pick_date.day_selected", strategy="gridcell_name", day=day_str)
        return
    except Exception:
        pass

    # Strategy 3: Button inside a gridcell/td (react-day-picker structure)
    try:
        btn = resolve_single(container.locator(f"td button:text-is('{day_str}')"), context="select_day_cell:td_button_text")
        btn.wait_for(state="visible", timeout=3000)
        _click_with_overlay_fallback(btn)
        logger.info("pick_date.day_selected", strategy="td_button_text", day=day_str)
        return
    except Exception:
        pass

    # Strategy 4: Button role with regex name match (aria-label contains day number)
    try:
        day_re = re.compile(rf"\b{re.escape(day_str)}\b")
        btn = resolve_single(container.get_by_role("button", name=day_re), context="select_day_cell:button_name_regex")
        btn.wait_for(state="visible", timeout=3000)
        _click_with_overlay_fallback(btn)
        logger.info("pick_date.day_selected", strategy="button_name_regex", day=day_str)
        return
    except Exception:
        pass

    # Strategy 5: Any element with exact text (get_by_text)
    try:
        target = resolve_single(container.get_by_text(day_str, exact=True), context="select_day_cell:text_exact")
        target.wait_for(state="visible", timeout=3000)
        _click_with_overlay_fallback(target)
        logger.info("pick_date.day_selected", strategy="text_exact", day=day_str)
        return
    except Exception:
        pass

    # Strategy 6: CSS selector for aria-label or td text
    try:
        target = resolve_single(container.locator(
            f"[aria-label*='{day_str}'], td:has-text('{day_str}')"
        ), context="select_day_cell:aria_label_css")
        target.wait_for(state="visible", timeout=3000)
        _click_with_overlay_fallback(target)
        logger.info("pick_date.day_selected", strategy="aria_label_css", day=day_str)
        return
    except Exception:
        pass

    # Strategy 7: Search page-wide if container search failed
    if container != page:
        try:
            btn = resolve_single(page.locator(f"button:text-is('{day_str}')"), context="select_day_cell:page_wide_button")
            btn.wait_for(state="visible", timeout=3000)
            _click_with_overlay_fallback(btn)
            logger.info("pick_date.day_selected", strategy="page_wide_button", day=day_str)
            return
        except Exception:
            pass

    raise TimeoutError(f"Could not find day {day_str} in calendar")


def _resolve_relative_date(day_str: str) -> date | None:
    """Resolve relative date strings to a date object.

    Supports: "today", "today+N", "today-N" where N is days offset.
    Returns None if the string is not a recognized pattern.
    """
    day_str = day_str.strip().lower()
    if day_str == "today":
        return date.today()
    match = re.match(r"today\s*([+-])\s*(\d+)", day_str)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        offset = int(match.group(2))
        return date.today() + timedelta(days=sign * offset)
    return None


def _find_date_trigger(page: Page, label: str) -> Locator:
    """Auto-discover a date picker trigger by label text."""
    label_re = re.compile(re.escape(label), re.IGNORECASE)

    strategies: list[tuple[str, object]] = [
        ("label", lambda: resolve_single(page.get_by_label(label_re), context="find_date_trigger:label")),
        ("button_name", lambda: resolve_single(page.get_by_role("button", name=label_re), context="find_date_trigger:button_name")),
        ("placeholder", lambda: resolve_single(page.get_by_placeholder(label_re), context="find_date_trigger:placeholder")),
        ("textbox_name", lambda: resolve_single(page.get_by_role("textbox", name=label_re), context="find_date_trigger:textbox_name")),
        ("input_near_label", lambda: _find_input_near_label(page, label)),
    ]

    for name, finder in strategies:
        try:
            loc = finder()
            if loc is None:
                continue
            loc.wait_for(state="visible", timeout=2000)
            logger.info("pick_date.trigger_found", strategy=name, label=label)
            return loc
        except Exception:
            continue

    return _find_any_date_input(page)


def _find_input_near_label(page: Page, label: str) -> Locator | None:
    """Find an input/button in the same container as a label text."""
    try:
        label_el = resolve_single(page.get_by_text(label, exact=False), context="find_input_near_label:label_text")
        label_el.wait_for(state="visible", timeout=2000)
        for depth in ("xpath=..", "xpath=../..", "xpath=../../.."):
            try:
                parent = label_el.locator(depth)
                for sel in ("input", "button[data-slot]", "button"):
                    try:
                        target = resolve_single(parent.locator(sel), context="find_input_near_label:input_element")
                        if target.is_visible():
                            return target
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return None


def _find_any_date_input(page: Page) -> Locator:
    """Find any visible date-related input or button on the page."""
    for selector in (
        "input[type='date']",
        "input[placeholder*='date' i]",
        "input[placeholder*='select' i]",
        "button[data-slot='calendar-trigger']",
        "[data-testid*='date' i]",
    ):
        try:
            el = resolve_single(page.locator(selector), context="find_any_date_input:selector")
            if el.is_visible():
                return el
        except Exception:
            continue

    # Try any button/input near "date" text
    try:
        return _find_input_near_label(page, "date")
    except Exception:
        pass

    raise TimeoutError("No date input found on page")


def _find_calendar_container(page: Page) -> Locator | None:
    """Locate the calendar popup container.

    Returns the OUTERMOST calendar wrapper (not just the day grid) so that
    month navigation buttons are included in the container.

    IMPORTANT: Never returns role="dialog" — inside a modal, that matches
    the modal itself, not the calendar.
    """
    # Priority 1: role="grid" with month navigation buttons in a parent
    grid = None
    try:
        grid = resolve_single(page.get_by_role("grid"), context="find_calendar_container:grid")
        grid.wait_for(state="visible", timeout=3000)
        for depth in ("xpath=..", "xpath=../..", "xpath=../../.."):
            try:
                parent = grid.locator(depth)
                nav_btn = parent.locator(
                    "button[aria-label*='month' i], button[aria-label*='next' i], "
                    "button[aria-label*='prev' i]"
                )
                if nav_btn.count() > 0:
                    return parent
            except Exception:
                continue
    except Exception:
        pass

    # Priority 2: CSS class and framework-specific selectors
    for selector in _CALENDAR_CONTAINER_SELECTORS:
        try:
            loc = resolve_single(page.locator(selector), context="find_calendar_container:css_selector")
            loc.wait_for(state="visible", timeout=2000)
            return loc
        except Exception:
            continue

    # Priority 3: Bare grid fallback
    if grid is not None:
        try:
            if grid.is_visible():
                return grid
        except Exception:
            pass

    return None


def _dismiss_calendar(page: Page, trigger: Locator | None = None) -> None:
    """Dismiss the calendar popup if it's still open after day selection."""
    if not _is_calendar_open(page):
        return

    logger.info("pick_date.dismissing_calendar")

    # Strategy 1: Click the trigger button again (toggle close)
    if trigger is not None:
        try:
            trigger.click(timeout=2000)
            budget_wait(page, 400, "dismiss_trigger_toggle")
            if not _is_calendar_open(page):
                logger.info("pick_date.calendar_dismissed", strategy="trigger_toggle")
                return
        except Exception:
            pass

    # Strategy 2: Press Escape (standard popover close)
    page.keyboard.press("Escape")
    budget_wait(page, 400, "dismiss_escape")
    if not _is_calendar_open(page):
        logger.info("pick_date.calendar_dismissed", strategy="escape")
        return

    # Strategy 3: Press Tab to move focus away (blur-based close)
    page.keyboard.press("Tab")
    budget_wait(page, 400, "dismiss_tab_blur")
    if not _is_calendar_open(page):
        logger.info("pick_date.calendar_dismissed", strategy="tab_blur")
        return

    # Strategy 4: Click a non-interactive element inside the modal
    for text_target in ("h2", "h3", "[class*='title']", "[class*='header']", "label"):
        try:
            dialog = resolve_single(page.get_by_role("dialog"), context="dismiss_calendar:dialog")
            if not dialog.is_visible():
                break
            heading = resolve_single(dialog.locator(text_target), context="dismiss_calendar:heading")
            if heading.is_visible():
                heading.click(timeout=1000)
                budget_wait(page, 400, "dismiss_click_heading")
                if not _is_calendar_open(page):
                    logger.info("pick_date.calendar_dismissed", strategy="click_heading")
                    return
        except Exception:
            continue

    # Strategy 5: JS-based popover dismissal
    try:
        page.evaluate("""() => {
            const openContainers = document.querySelectorAll(
                '[data-state="open"], [data-radix-popper-content-wrapper], '
                + '[class*="popover"]:not([style*="display: none"]), '
                + '[class*="popup"]:not([style*="display: none"])'
            );
            for (const el of openContainers) {
                el.dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'Escape', code: 'Escape', keyCode: 27,
                    bubbles: true, cancelable: true,
                }));
            }
        }""")
        budget_wait(page, 400, "dismiss_js_escape")
        if not _is_calendar_open(page):
            logger.info("pick_date.calendar_dismissed", strategy="js_escape")
            return
    except Exception:
        pass

    # Strategy 6: Force-hide via JS as absolute last resort
    try:
        page.evaluate("""() => {
            const selectors = [
                '[data-state="open"][class*="popover"]',
                '[data-state="open"][class*="calendar"]',
                '[data-radix-popper-content-wrapper]',
                '[class*="datepicker"]:not([style*="display: none"])',
            ];
            for (const sel of selectors) {
                const els = document.querySelectorAll(sel);
                for (const el of els) { el.style.display = 'none'; }
            }
        }""")
        budget_wait(page, 200, "dismiss_js_force_hide")
        logger.info("pick_date.calendar_dismissed", strategy="js_force_hide")
    except Exception:
        logger.warning("pick_date.calendar_dismiss_failed")


def _is_calendar_open(page: Page) -> bool:
    """Check if any calendar popup/popover is currently visible on the page."""
    for selector in _CALENDAR_OPEN_SELECTORS:
        try:
            el = resolve_single(page.locator(selector), context="is_calendar_open:open_selector", timeout_ms=500)
            if el.is_visible():
                return True
        except Exception:
            continue
    for wrapper in (
        "[data-state='open']",
        "[class*='popover']",
        "[class*='popup']",
    ):
        try:
            grid = resolve_single(page.locator(f"{wrapper} [role='grid']"), context="is_calendar_open:grid_in_wrapper", timeout_ms=500)
            if grid.is_visible():
                return True
        except Exception:
            continue
    return False


def _navigate_to_month(
    page: Page,
    calendar: Locator,
    target_month: str,
    next_label: str,
    max_clicks: int,
) -> None:
    """Click the next-month button until the target month heading is visible."""
    target_lower = target_month.lower()

    try:
        text = calendar.inner_text(timeout=2000)
        if target_lower in text.lower():
            return
    except Exception:
        pass

    next_btn = _find_next_month_button(page, calendar, next_label)
    if next_btn is None:
        logger.warning("pick_date.no_next_month_button")
        return

    for click_num in range(max_clicks):
        try:
            next_btn.click(timeout=2000)
            budget_wait(page, 400, "navigate_month")
        except Exception:
            logger.warning("pick_date.next_month_click_failed", click=click_num)
            return

        try:
            text = calendar.inner_text(timeout=1000)
            if target_lower in text.lower():
                logger.info("pick_date.month_navigated", target=target_month, clicks=click_num + 1)
                return
        except Exception:
            pass

    logger.warning("pick_date.month_nav_exhausted", target=target_month, max_clicks=max_clicks)


def _find_next_month_button(
    page: Page, calendar: Locator, next_label: str
) -> Locator | None:
    """Find the next-month navigation button in the calendar."""
    # Strategy 1: By accessible name (within calendar container)
    try:
        btn = resolve_single(calendar.get_by_role("button", name=next_label), context="find_next_month_button:accessible_name")
        if btn.is_visible():
            return btn
    except Exception:
        pass

    # Strategy 2: By aria-label (within calendar container)
    try:
        btn = resolve_single(calendar.locator(
            "button[aria-label*='next' i], button[aria-label*='Next']"
        ), context="find_next_month_button:aria_label")
        if btn.is_visible():
            return btn
    except Exception:
        pass

    # Strategy 3: By Shadcn chevron-right pattern (within calendar)
    try:
        btn = resolve_single(calendar.locator(
            "button:has(svg[class*='chevron-right']), "
            "button:has(svg[class*='ChevronRight']), "
            "button[class*='nav_button_next']"
        ), context="find_next_month_button:chevron_pattern")
        if btn.is_visible():
            return btn
    except Exception:
        pass

    # Strategy 4: Search the entire page
    try:
        btn = resolve_single(page.get_by_role("button", name=next_label), context="find_next_month_button:page_wide_name")
        if btn.is_visible():
            return btn
    except Exception:
        pass

    try:
        btn = resolve_single(page.locator(
            "button[aria-label*='next month' i], button[aria-label*='Go to next']"
        ), context="find_next_month_button:page_wide_aria")
        if btn.is_visible():
            return btn
    except Exception:
        pass

    return None
