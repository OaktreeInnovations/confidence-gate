"""Tests for clickable element discovery.

Validates that discover_clickable_ancestor():
- Returns original locator when element is already clickable
- Walks up DOM to find interactive ancestors (button, role, tabindex, cursor)
- Respects max depth limit
- Handles edge cases (evaluate failure, empty count, body reached)
"""

from unittest.mock import MagicMock

from app.worker.interaction_discovery import discover_clickable_ancestor


def _make_locator(evaluate_result=None, ancestor_count=1, ancestor_visible=True):
    """Build a mock locator with configurable discovery JS result."""
    locator = MagicMock()

    if evaluate_result is None:
        # Default: element is already clickable
        evaluate_result = {"found": False, "depth": 0, "tag": "", "role": ""}

    locator.evaluate.return_value = evaluate_result

    # Ancestor locator returned by locator.locator("xpath=...")
    ancestor = MagicMock()
    ancestor.count.return_value = ancestor_count
    ancestor.first = ancestor
    ancestor.is_visible.return_value = ancestor_visible
    locator.locator.return_value = ancestor

    return locator, ancestor


def test_button_returns_self():
    """A <button> element is already clickable — no ancestor walk needed."""
    locator, _ = _make_locator(
        evaluate_result={"found": False, "depth": 0, "tag": "", "role": ""},
    )
    result = discover_clickable_ancestor(locator)
    assert result is locator


def test_role_button_returns_self():
    """Element with role=button is already clickable."""
    locator, _ = _make_locator(
        evaluate_result={"found": False, "depth": 0, "tag": "", "role": ""},
    )
    result = discover_clickable_ancestor(locator)
    assert result is locator


def test_span_in_clickable_div_finds_ancestor():
    """<span> inside <div onclick> — discovery finds the parent div."""
    locator, ancestor = _make_locator(
        evaluate_result={"found": True, "depth": 1, "tag": "div", "role": ""},
    )
    result = discover_clickable_ancestor(locator)
    assert result is ancestor
    # Verify XPath was ".."
    locator.locator.assert_called_once_with("xpath=..")


def test_deep_ancestor_button_found():
    """Element 3 levels deep inside a button — walks up 3 levels."""
    locator, ancestor = _make_locator(
        evaluate_result={"found": True, "depth": 3, "tag": "button", "role": ""},
    )
    result = discover_clickable_ancestor(locator)
    assert result is ancestor
    locator.locator.assert_called_once_with("xpath=../../..")


def test_role_combobox_ancestor_found():
    """Span inside a role=combobox trigger — finds the combobox."""
    locator, ancestor = _make_locator(
        evaluate_result={"found": True, "depth": 2, "tag": "div", "role": "combobox"},
    )
    result = discover_clickable_ancestor(locator)
    assert result is ancestor
    locator.locator.assert_called_once_with("xpath=../..")


def test_tabindex_ancestor_found():
    """Text inside an element with tabindex=0."""
    locator, ancestor = _make_locator(
        evaluate_result={"found": True, "depth": 1, "tag": "div", "role": ""},
    )
    result = discover_clickable_ancestor(locator)
    assert result is ancestor


def test_no_ancestor_returns_original():
    """No clickable ancestor found within max depth — returns original."""
    locator, _ = _make_locator(
        evaluate_result={"found": False, "depth": 0, "tag": "", "role": ""},
    )
    result = discover_clickable_ancestor(locator)
    assert result is locator


def test_evaluate_exception_returns_original():
    """JS evaluation failure — gracefully returns original locator."""
    locator = MagicMock()
    locator.evaluate.side_effect = RuntimeError("Element detached")
    result = discover_clickable_ancestor(locator)
    assert result is locator


def test_ancestor_locator_zero_count_returns_original():
    """Ancestor XPath resolves to 0 elements — returns original."""
    locator, ancestor = _make_locator(
        evaluate_result={"found": True, "depth": 1, "tag": "div", "role": ""},
        ancestor_count=0,
    )
    result = discover_clickable_ancestor(locator)
    assert result is locator


def test_ancestor_locator_multiple_picks_visible_first():
    """Ancestor XPath resolves to multiple — picks first if visible."""
    locator, ancestor = _make_locator(
        evaluate_result={"found": True, "depth": 1, "tag": "div", "role": ""},
        ancestor_count=3,
        ancestor_visible=True,
    )
    result = discover_clickable_ancestor(locator)
    assert result is ancestor


def test_ancestor_locator_count_exception_returns_original():
    """count() raises — returns original locator."""
    locator = MagicMock()
    locator.evaluate.return_value = {"found": True, "depth": 1, "tag": "div", "role": ""}
    bad_ancestor = MagicMock()
    bad_ancestor.count.side_effect = RuntimeError("Frame detached")
    locator.locator.return_value = bad_ancestor
    result = discover_clickable_ancestor(locator)
    assert result is locator


def test_depth_5_xpath_correct():
    """Depth 5 generates correct XPath: ../../../../.."""
    locator, ancestor = _make_locator(
        evaluate_result={"found": True, "depth": 5, "tag": "a", "role": "link"},
    )
    result = discover_clickable_ancestor(locator)
    assert result is ancestor
    locator.locator.assert_called_once_with("xpath=../../../../..")
