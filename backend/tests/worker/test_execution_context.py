"""Test: Execution Context Memory (Part 4 of Intelligence Upgrade).

Tests contextual awareness tracking across actions: section detection,
modal awareness, dropdown state, and proximity hints.
"""

from unittest.mock import MagicMock

from app.worker.execution_context import ExecutionContext, update_context


def _make_page_and_locator(
    *,
    section="main",
    modal="",
    expanded=None,
    parent_text="",
    url="https://app.example.com/dashboard",
):
    """Create MagicMock Page and Locator with controlled evaluate results."""
    page = MagicMock()
    page.url = url

    # page.evaluate for modal detection
    page.evaluate.return_value = modal

    locator = MagicMock()

    # locator.evaluate: first call is section, second call is parent text
    def _eval_side_effect(js_code):
        if "getBoundingClientRect" in js_code:
            return section
        if "parentElement" in js_code:
            return parent_text
        return ""

    locator.evaluate.side_effect = _eval_side_effect

    # aria-expanded
    locator.get_attribute.return_value = expanded

    return page, locator


def test_initial_context_empty():
    """Fresh context has empty values."""
    ctx = ExecutionContext()
    assert ctx.last_element_selector == ""
    assert ctx.last_page_section == ""
    assert ctx.active_modal == ""
    assert not ctx.open_dropdown
    assert ctx.get_proximity_hint() is None


def test_update_sets_selector_path():
    """Selector path is captured from action result."""
    ctx = ExecutionContext()
    page, locator = _make_page_and_locator()

    update_context(ctx, page, locator, "click", selector_path="div#main > button")

    assert ctx.last_element_selector == "div#main > button"
    assert ctx.get_proximity_hint() == "div#main > button"


def test_update_detects_page_section():
    """Section detection from element position."""
    ctx = ExecutionContext()
    page, locator = _make_page_and_locator(section="sidebar")

    update_context(ctx, page, locator, "click")

    assert ctx.last_page_section == "sidebar"
    assert "sidebar" in ctx.sections_visited


def test_update_detects_modal():
    """Modal detection via page.evaluate."""
    ctx = ExecutionContext()
    page, locator = _make_page_and_locator(modal="Create New Item")

    update_context(ctx, page, locator, "click")

    assert ctx.active_modal == "Create New Item"


def test_update_detects_open_dropdown():
    """Dropdown open state from aria-expanded."""
    ctx = ExecutionContext()
    page, locator = _make_page_and_locator(expanded="true")

    update_context(ctx, page, locator, "click", selector_path="button#dropdown")

    assert ctx.open_dropdown is True
    assert ctx.open_dropdown_trigger == "button#dropdown"


def test_update_clears_dropdown_on_select():
    """Select action clears dropdown state."""
    ctx = ExecutionContext()
    ctx.open_dropdown = True
    ctx.open_dropdown_trigger = "button#dropdown"

    page, locator = _make_page_and_locator(expanded=None)
    update_context(ctx, page, locator, "select")

    assert ctx.open_dropdown is False


def test_update_captures_text_anchor():
    """Parent text anchor captured for future disambiguation."""
    ctx = ExecutionContext()
    page, locator = _make_page_and_locator(parent_text="Personal Information")

    update_context(ctx, page, locator, "input")

    assert ctx.last_visible_text_anchor == "Personal Information"


def test_update_tracks_navigation_url():
    """Navigation URL updated from page.url."""
    ctx = ExecutionContext()
    page, locator = _make_page_and_locator(url="https://app.example.com/settings")

    update_context(ctx, page, locator, "navigate")

    assert ctx.last_navigation_url == "https://app.example.com/settings"


def test_context_hints_structure():
    """get_context_hints returns the expected dict shape."""
    ctx = ExecutionContext()
    ctx.last_page_section = "main"
    ctx.active_modal = "Edit Profile"
    ctx.open_dropdown = True
    ctx.last_visible_text_anchor = "Personal Info"

    hints = ctx.get_context_hints()

    assert hints["section"] == "main"
    assert hints["modal"] == "Edit Profile"
    assert hints["dropdown_open"] is True
    assert hints["text_anchor"] == "Personal Info"


def test_sections_visited_accumulates():
    """Sections visited list accumulates without duplicates."""
    ctx = ExecutionContext()
    page1, loc1 = _make_page_and_locator(section="header")
    page2, loc2 = _make_page_and_locator(section="main")
    page3, loc3 = _make_page_and_locator(section="header")

    update_context(ctx, page1, loc1, "click")
    update_context(ctx, page2, loc2, "click")
    update_context(ctx, page3, loc3, "click")

    assert ctx.sections_visited == ["header", "main"]


def test_update_no_locator_safe():
    """update_context with locator=None doesn't crash."""
    ctx = ExecutionContext()
    page = MagicMock()
    page.url = "https://example.com"

    update_context(ctx, page, None, "navigate")

    assert ctx.last_navigation_url == "https://example.com"
