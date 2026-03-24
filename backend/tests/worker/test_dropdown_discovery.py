"""Tests for improved dropdown trigger discovery.

Validates:
- Extended parent traversal (5 levels) for deeply nested UIs
- Radix trigger discovery via sibling detection
- Page-scoped portal fallback via aria-label
"""

from unittest.mock import MagicMock, patch, PropertyMock

from app.worker.component_strategies.dropdown import (
    _find_combobox_near_label,
    _find_radix_trigger,
)


def _make_page():
    return MagicMock()


def _make_visible_locator():
    loc = MagicMock()
    loc.is_visible.return_value = True
    loc.count.return_value = 1
    loc.first = loc
    return loc


def _make_invisible_locator():
    loc = MagicMock()
    loc.is_visible.return_value = False
    loc.count.return_value = 0
    loc.first = loc
    return loc


def test_combobox_near_label_deep_nesting():
    """Finds combobox at 5th parent level (previously failed at 3)."""
    page = _make_page()

    label_el = MagicMock()
    label_el.wait_for = MagicMock()

    # First 4 levels return no combobox, 5th level finds one
    combo_found = _make_visible_locator()

    def mock_locator(xpath_str):
        parent = MagicMock()
        if xpath_str == "xpath=../../../../../..":
            # This shouldn't happen — we only go to 5 levels
            raise RuntimeError("Too deep")
        if xpath_str == "xpath=../../../../..":
            # 5th level — return a parent that has a combobox
            parent.get_by_role.return_value = combo_found
            return parent
        # Levels 1-4 — raise so we continue
        parent.get_by_role.side_effect = RuntimeError("not found")
        parent.locator.side_effect = RuntimeError("not found")
        return parent

    label_el.locator = mock_locator

    with patch("app.worker.component_strategies.dropdown.resolve_single",
               side_effect=lambda loc, context="": loc if loc == label_el else combo_found):
        # Mock page.get_by_text to return our label element
        page.get_by_text.return_value = label_el
        result = _find_combobox_near_label(page, "Objective")

    assert result is combo_found


def test_radix_trigger_sibling_discovery():
    """Finds button sibling after label element."""
    page = _make_page()

    label_el = MagicMock()
    label_el.wait_for = MagicMock()

    sibling_btn = _make_visible_locator()
    label_el.locator.return_value = sibling_btn

    with patch("app.worker.component_strategies.dropdown.resolve_single",
               return_value=label_el):
        page.get_by_text.return_value = label_el
        result = _find_radix_trigger(page, "Category")

    assert result is sibling_btn


def test_fallback_to_page_wide_search():
    """Portal trigger found via page-scoped aria-label search."""
    page = _make_page()

    # Make label_el locator fail (no siblings)
    label_el = MagicMock()
    label_el.wait_for = MagicMock()
    no_match = _make_invisible_locator()
    no_match.count.return_value = 0
    label_el.locator.return_value = no_match

    # Page-scoped search finds a button with aria-label
    trigger = _make_visible_locator()
    page_matches = MagicMock()
    page_matches.count.return_value = 1
    page_matches.nth.return_value = trigger

    def mock_page_locator(sel):
        if 'aria-label' in sel:
            return page_matches
        m = MagicMock()
        m.count.return_value = 0
        return m

    page.locator = mock_page_locator

    with patch("app.worker.component_strategies.dropdown.resolve_single",
               return_value=label_el):
        page.get_by_text.return_value = label_el
        result = _find_radix_trigger(page, "Status")

    assert result is trigger
