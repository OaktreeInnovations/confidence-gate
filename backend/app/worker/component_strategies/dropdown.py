"""Dropdown / combobox interaction strategy.

Handles custom dropdown opening, option discovery, selection, and
verification across multiple UI frameworks.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Union

import structlog

from app.worker.component_helpers import resolve_single
from app.worker.execution_budget import budget_wait

from .base import (
    _click_with_overlay_fallback,
    _js_pointer_click,
    _select_by_keyboard,
)

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


def select_dropdown(
    page: Page,
    trigger: Union[Locator, str],
    option_text: str,
    *,
    search_text: str = "",
) -> None:
    """Interact with a custom dropdown or combobox.

    Accepts either a Playwright Locator or a string label for the trigger.
    When a string is passed, auto-discovers the dropdown trigger by trying
    multiple selector strategies (combobox role, label association, nearby
    text, etc.).

    Args:
        page: Playwright Page (or Locator — auto-extracts .page for modal compat).
        trigger: Locator for the dropdown trigger, OR a string label
                 (e.g. "Objective") for auto-discovery.
        option_text: The visible text of the option to select.
        search_text: If set, type this into the combobox to filter options.
    """
    # Support being called from interact_in_modal where first arg is a Locator
    if not hasattr(page, "goto"):
        page = page.page

    # Auto-discover trigger when a string label is provided
    if isinstance(trigger, str):
        trigger = _find_dropdown_trigger(page, trigger)

    # Verify trigger is visible, fallback to auto-discovery if not
    try:
        trigger.wait_for(state="visible", timeout=5000)
    except Exception:
        logger.warning(
            "select_dropdown.trigger_not_visible",
            trigger=str(trigger)[:200],
        )
        trigger = _find_any_visible_dropdown(page)

    # Skip clicking if the dropdown is already open (avoids toggling it closed)
    already_open = False
    try:
        expanded = trigger.get_attribute("aria-expanded")
        data_state = trigger.get_attribute("data-state")
        if expanded == "true" or data_state == "open":
            already_open = True
            logger.info("select_dropdown.already_open")
    except Exception:
        pass

    if not already_open:
        _click_with_overlay_fallback(trigger)
        budget_wait(page, 500, "dropdown_open")

    # Type to search if this is a searchable combobox
    if search_text:
        try:
            trigger.fill(search_text)
            budget_wait(page, 500, "dropdown_search_fill")
        except Exception:
            trigger.press_sequentially(search_text, delay=50)
            budget_wait(page, 500, "dropdown_search_type")

    # Find and click the option
    _select_option_from_list(page, option_text)
    budget_wait(page, 500, "dropdown_option_click")

    # Verify selection: if a listbox/option is still visible, selection failed.
    if _is_dropdown_still_open(page):
        logger.warning(
            "select_dropdown.selection_not_confirmed",
            option=option_text,
        )
        # Try keyboard: type to filter + Enter
        try:
            _select_by_keyboard(page, option_text)
            budget_wait(page, 500, "dropdown_keyboard_fallback")
        except Exception:
            pass

        # If still open, try direct pointer events on the option
        if _is_dropdown_still_open(page):
            try:
                opt = resolve_single(page.get_by_role("option", name=option_text), context="select_dropdown:option_js_fallback")
                _js_pointer_click(opt)
                budget_wait(page, 500, "dropdown_js_click_fallback")
            except Exception:
                pass

        # If STILL open, Escape to close and accept partial state
        if _is_dropdown_still_open(page):
            logger.warning("select_dropdown.forcing_close")
            page.keyboard.press("Escape")
            budget_wait(page, 500, "dropdown_force_close")


def _is_dropdown_still_open(page: Page) -> bool:
    """Check if a dropdown/listbox/menu is still visibly open."""
    for role in ("listbox", "menu"):
        try:
            el = resolve_single(page.get_by_role(role), context="is_dropdown_still_open:role_check")
            if el.is_visible():
                return True
        except Exception:
            pass
    # Check for Radix select content portal
    try:
        content = resolve_single(page.locator("[data-radix-select-content], [data-radix-popper-content-wrapper]"), context="is_dropdown_still_open:radix_content")
        if content.is_visible():
            return True
    except Exception:
        pass
    return False


def _find_dropdown_trigger(page: Page, label: str) -> Locator:
    """Auto-discover a dropdown trigger by label text."""
    label_re = re.compile(re.escape(label), re.IGNORECASE)
    select_re = re.compile(rf"select\s*{re.escape(label)}", re.IGNORECASE)

    strategies: list[tuple[str, object]] = [
        ("combobox_name", lambda: resolve_single(page.get_by_role("combobox", name=label_re), context="find_dropdown_trigger:combobox_name")),
        ("label_assoc", lambda: resolve_single(page.get_by_label(label_re), context="find_dropdown_trigger:label_assoc")),
        ("button_name", lambda: resolve_single(page.get_by_role("button", name=label_re), context="find_dropdown_trigger:button_name")),
        ("select_placeholder", lambda: resolve_single(page.get_by_role("combobox", name=select_re), context="find_dropdown_trigger:select_placeholder")),
        ("radix_trigger", lambda: _find_radix_trigger(page, label)),
        ("combobox_near_label", lambda: _find_combobox_near_label(page, label)),
    ]

    for name, finder in strategies:
        try:
            loc = finder()
            if loc is None:
                continue
            loc.wait_for(state="visible", timeout=2000)
            logger.info("select_dropdown.trigger_found", strategy=name, label=label)
            return loc
        except Exception:
            continue

    # Ultimate fallback: any visible combobox on the page
    logger.warning("select_dropdown.auto_discovery_fallback", label=label)
    return _find_any_visible_dropdown(page)


def _find_radix_trigger(page: Page, label: str) -> Locator | None:
    """Find Radix/shadcn select trigger near a label.

    Handles portal-based UIs where label and trigger are siblings or
    nearby elements rather than sharing a close DOM ancestor.
    """
    try:
        label_el = resolve_single(
            page.get_by_text(label, exact=False),
            context="find_radix_trigger:label",
        )
        label_el.wait_for(state="visible", timeout=2000)

        # Check following sibling button
        sibling = label_el.locator("xpath=following-sibling::button[1]")
        if sibling.count() and sibling.first.is_visible():
            return sibling.first

        # Check following element with combobox role (across DOM levels)
        combo = label_el.locator("xpath=following::*[@role='combobox'][1]")
        if combo.count() and combo.first.is_visible():
            return combo.first
    except Exception:
        pass

    # Page-scoped fallback: find trigger by aria-label matching the label
    label_lower = label.lower()
    for sel in (
        f'button[aria-label*="{label}" i]',
        f'[role="combobox"][aria-label*="{label}" i]',
        "button[data-placeholder]",
        "button[aria-haspopup='dialog']",
    ):
        try:
            matches = page.locator(sel)
            count = matches.count()
            for i in range(min(count, 5)):
                m = matches.nth(i)
                if m.is_visible():
                    if "data-placeholder" in sel or "aria-haspopup='dialog'" in sel:
                        text = m.text_content() or m.get_attribute("data-placeholder") or ""
                        if label_lower not in text.lower():
                            continue
                    return m
        except Exception:
            continue

    return None


def _find_combobox_near_label(page: Page, label: str) -> Locator | None:
    """Find a combobox in the same DOM container as a label text.

    Traverses up to 5 parent levels to handle deeply nested UI frameworks.
    """
    try:
        label_el = resolve_single(page.get_by_text(label, exact=False), context="find_combobox_near_label:label_text")
        label_el.wait_for(state="visible", timeout=2000)

        for depth in range(1, 6):
            xpath = "xpath=" + "/".join([".."] * depth)
            try:
                parent = label_el.locator(xpath)
                combo = resolve_single(parent.get_by_role("combobox"), context="find_combobox_near_label:combobox")
                if combo.is_visible():
                    return combo
                btn = resolve_single(parent.locator("button[aria-haspopup]"), context="find_combobox_near_label:haspopup_button")
                if btn.is_visible():
                    return btn
            except Exception:
                continue
    except Exception:
        pass
    return None


def _find_any_visible_dropdown(page: Page) -> Locator:
    """Find any visible combobox or dropdown trigger on the page."""
    try:
        combos = page.get_by_role("combobox")
        count = combos.count()
        for i in range(count):
            c = combos.nth(i)
            if c.is_visible():
                return c
    except Exception:
        pass

    try:
        btns = page.locator(
            "button[aria-haspopup='listbox'], button[aria-haspopup='true'], button[aria-haspopup='dialog']"
        )
        count = btns.count()
        for i in range(count):
            b = btns.nth(i)
            if b.is_visible():
                return b
    except Exception:
        pass

    raise TimeoutError("No dropdown trigger found on page")


def _select_option_from_list(page: Page, option_text: str) -> None:
    """Find and click an option in an open dropdown.

    Prioritizes text matching within popup containers (works with any
    UI library), then falls back to ARIA roles, then keyboard.
    """
    # Strategy 1: Find text within popup/dropdown containers
    popup_selectors = (
        "[data-radix-popper-content-wrapper]",
        "[data-radix-select-content]",
        "[data-state='open'][role]",
        "[class*='popover']",
        "[class*='dropdown-menu']",
        "[class*='select-content']",
        "[class*='listbox']",
        "[class*='menu-content']",
    )
    for sel in popup_selectors:
        try:
            container = resolve_single(page.locator(sel), context="select_option:popup_container")
            if not container.is_visible():
                continue
            target = resolve_single(container.get_by_text(option_text, exact=True), context="select_option:popup_option_text")
            if target.is_visible():
                logger.info(
                    "select_option.found",
                    strategy="popup_container",
                    selector=sel,
                    option=option_text,
                )
                _click_with_overlay_fallback(target)
                return
        except Exception:
            continue

    # Strategy 2: Direct text match anywhere (wait up to 3s)
    try:
        target = resolve_single(page.get_by_text(option_text, exact=True), context="select_option:direct_text_match")
        target.wait_for(state="visible", timeout=3000)
        logger.info("select_option.found", strategy="text_match", option=option_text)
        _click_with_overlay_fallback(target)
        return
    except Exception as e:
        logger.info("select_option.failed", strategy="text_match", error=str(e)[:150])

    # Strategy 3: ARIA roles (role="option", role="menuitem")
    for role in ("option", "menuitem"):
        try:
            el = resolve_single(page.get_by_role(role, name=option_text), context="select_option:aria_role")
            el.wait_for(state="visible", timeout=3000)
            logger.info("select_option.found", strategy=f"role_{role}", option=option_text)
            _click_with_overlay_fallback(el)
            return
        except Exception as e:
            logger.info("select_option.failed", strategy=f"role_{role}", error=str(e)[:150])

    # Strategy 4: Within listbox/menu containers
    for role in ("listbox", "menu"):
        try:
            container = resolve_single(page.get_by_role(role), context="select_option:list_container")
            container.wait_for(state="visible", timeout=3000)
            target = resolve_single(container.get_by_text(option_text), context="select_option:list_option_text")
            logger.info("select_option.found", strategy=f"{role}_text", option=option_text)
            _click_with_overlay_fallback(target)
            return
        except Exception as e:
            logger.info("select_option.failed", strategy=f"{role}_text", error=str(e)[:150])

    # Strategy 5: Keyboard typeahead (last resort)
    logger.info("select_option.trying", strategy="keyboard", option=option_text)
    _select_by_keyboard(page, option_text)
