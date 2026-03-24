"""Test: Identical elements — resolve_single picks the visible one.

Scenario: Two identical buttons exist on the page. Only one is visible.
resolve_single should score by visibility and pick the visible one.
"""

from unittest.mock import MagicMock

from app.worker.component_helpers import resolve_single


def _make_multi_locator(count: int, visible_index: int = 0):
    """Create a mock locator that returns `count` elements."""
    loc = MagicMock()
    loc.count.return_value = count

    elements = []
    for i in range(count):
        el = MagicMock()
        el.is_visible.return_value = (i == visible_index)
        el.is_enabled.return_value = True
        elements.append(el)

    loc.nth = lambda idx: elements[idx]
    loc.first = elements[0]

    # Make wait_for work on first element (for the zero-count retry path)
    elements[0].wait_for = MagicMock()

    return loc, elements


def test_single_match_returns_first():
    """When there's exactly one match, resolve_single returns it."""
    loc = MagicMock()
    loc.count.return_value = 1
    loc.first = MagicMock()

    result = resolve_single(loc, context="test_single")
    assert result is loc.first


def test_multiple_picks_visible():
    """When two elements exist, resolve_single picks the visible one."""
    loc, elements = _make_multi_locator(count=2, visible_index=1)

    result = resolve_single(loc, context="test_multi")
    # Should pick index 1 (the visible one)
    assert result is elements[1]


def test_multiple_all_visible_picks_first_enabled():
    """When all elements are visible, picks the first with highest score."""
    loc, elements = _make_multi_locator(count=3, visible_index=0)
    # Make all visible
    for el in elements:
        el.is_visible.return_value = True
    # Disable the first one
    elements[0].is_enabled.return_value = False

    result = resolve_single(loc, context="test_enabled")
    # Should pick index 1 (visible + enabled)
    assert result is elements[1]


def test_zero_count_waits_and_retries():
    """When count is 0, resolve_single waits then retries."""
    loc = MagicMock()
    # First call returns 0, second returns 1
    loc.count.side_effect = [0, 1]
    loc.first = MagicMock()
    loc.first.wait_for = MagicMock()

    result = resolve_single(loc, context="test_zero")
    assert result is loc.first
    loc.first.wait_for.assert_called_once()


def test_zero_count_after_wait_raises():
    """When count is still 0 after wait, resolve_single raises."""
    loc = MagicMock()
    loc.count.side_effect = [0, 0]
    loc.first = MagicMock()
    loc.first.wait_for = MagicMock()

    try:
        resolve_single(loc, context="test_still_zero")
        assert False, "Should have raised"
    except RuntimeError as e:
        assert "No element found" in str(e)
