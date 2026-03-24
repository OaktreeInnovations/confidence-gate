"""Test: Universal Interaction Guard Layer (Part 2).

Validates the 7 pre-action checks:
1. Bounding box > 0
1b. Bounding box stable (layout shift)
2. Visible (scroll remediation)
3. Enabled (wait remediation)
4. Not covered by overlay (elementFromPoint)
5. pointer-events not disabled
6. DOM stable (hash comparison)
"""

import time
from unittest.mock import MagicMock, PropertyMock

from app.worker.interaction_guard import (
    GuardResult,
    ensure_interactable,
)


def _make_locator(
    *,
    visible: bool = True,
    enabled: bool = True,
    bbox: dict | None = None,
    overlay_hit: bool = True,
    hash_stable: bool = True,
) -> MagicMock:
    """Create a mock locator with controlled interactability state."""
    locator = MagicMock()
    locator.is_visible.return_value = visible
    locator.is_enabled.return_value = enabled
    locator.bounding_box.return_value = bbox or {"x": 10, "y": 20, "width": 100, "height": 40}

    # Control evaluate() responses based on JS content
    def evaluate_side_effect(js, *args, **kwargs):
        # Overlay check: elementFromPoint
        if "elementFromPoint" in js:
            return overlay_hit
        # DOM stability hash
        if "charCodeAt" in js:
            return 12345 if hash_stable else int(time.monotonic() * 1000)
        return None

    locator.evaluate.side_effect = evaluate_side_effect
    return locator


def test_visible_enabled_passes():
    """Normal visible+enabled element passes all checks."""
    locator = _make_locator()
    result = ensure_interactable(locator)

    assert result.passed
    assert "bounding_box" in result.checks_passed
    assert "bbox_stable" in result.checks_passed
    assert "visible" in result.checks_passed
    assert "enabled" in result.checks_passed
    assert "no_overlay" in result.checks_passed
    assert "dom_stable" in result.checks_passed
    assert len(result.checks_failed) == 0


def test_hidden_element_fails():
    """Zero bounding box fails guard."""
    locator = _make_locator(bbox={"x": 0, "y": 0, "width": 0, "height": 0})
    # scroll_into_view doesn't fix it — bounding_box still zero after scroll
    result = ensure_interactable(locator)

    assert not result.passed
    assert "bounding_box_zero" in result.checks_failed


def test_disabled_element_waits():
    """Disabled element that becomes enabled within timeout passes."""
    locator = _make_locator(enabled=False)

    call_count = {"n": 0}
    original_is_enabled = locator.is_enabled.side_effect

    def delayed_enable():
        call_count["n"] += 1
        # Becomes enabled after 2 calls (~600ms at 300ms intervals)
        return call_count["n"] >= 3

    locator.is_enabled.side_effect = delayed_enable

    result = ensure_interactable(locator, max_wait_ms=3000)

    assert result.passed
    assert "enabled" in result.checks_passed
    assert "waited_for_enabled" in result.remediation_applied


def test_overlay_detected():
    """elementFromPoint returns different element — guard fails."""
    locator = _make_locator(overlay_hit=False)
    result = ensure_interactable(locator)

    assert not result.passed
    assert "overlay_detected" in result.checks_failed


def test_dom_unstable_waits():
    """Element hash changes then stabilizes — passes with remediation."""
    locator = _make_locator()

    # Hash changes first time, stabilizes second time
    hash_calls = {"n": 0}

    def hash_side_effect(js, *args, **kwargs):
        if "elementFromPoint" in js:
            return True
        if "charCodeAt" in js:
            hash_calls["n"] += 1
            # First call: hash A, second call: hash B (unstable)
            # Third call: hash B (stable)
            if hash_calls["n"] == 1:
                return 111
            return 222
        return None

    locator.evaluate.side_effect = hash_side_effect

    result = ensure_interactable(locator)

    assert result.passed
    assert "dom_stable" in result.checks_passed
    assert "waited_for_stability" in result.remediation_applied


def test_scroll_remediation():
    """Off-viewport element gets scrolled into view."""
    locator = _make_locator(visible=False)

    # After scroll, element becomes visible
    scroll_called = {"done": False}

    def scroll_side_effect(*args, **kwargs):
        scroll_called["done"] = True

    locator.scroll_into_view_if_needed.side_effect = scroll_side_effect

    call_count = {"n": 0}

    def visible_side_effect():
        call_count["n"] += 1
        # First call: not visible. After scroll: visible
        return call_count["n"] > 1

    locator.is_visible.side_effect = visible_side_effect
    # bounding_box also needs to succeed: first call returns zero, after scroll returns normal
    bbox_calls = {"n": 0}

    def bbox_side_effect(*args, **kwargs):
        bbox_calls["n"] += 1
        if bbox_calls["n"] == 1:
            return {"x": 0, "y": 0, "width": 0, "height": 0}
        return {"x": 10, "y": 20, "width": 100, "height": 40}

    locator.bounding_box.side_effect = bbox_side_effect

    result = ensure_interactable(locator)

    assert result.passed
    assert "scrolled_into_view" in result.remediation_applied


def test_guard_respects_budget():
    """Guard completes within budget even with remediation attempts."""
    locator = _make_locator()
    t0 = time.monotonic()
    result = ensure_interactable(locator, max_wait_ms=5000)
    elapsed = (time.monotonic() - t0) * 1000

    assert result.passed
    # Should complete well within budget (no remediation needed for healthy element)
    assert elapsed < 2000  # DOM stability check takes ~100ms minimum
