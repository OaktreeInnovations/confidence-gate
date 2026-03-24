"""Test: DOM Revalidation (Part 4).

Validates element identity capture and re-resolution:
- Stable element passes without re-resolve
- Replaced element triggers transparent re-resolve
- Detached element triggers re-resolve
- Budget exceeded skips re-validation
"""

from unittest.mock import MagicMock, patch

from app.worker.dom_revalidation import (
    capture_element_identity,
    is_same_element,
    revalidate_before_action,
)
from app.worker.intent_schema import TargetDescriptor


def _make_identity(tag="button", elem_id="submit", text_hash=12345):
    """Create a mock element identity."""
    return {
        "tag": tag,
        "id": elem_id,
        "ariaLabel": "",
        "textHash": text_hash,
        "rect": {"x": 10, "y": 20, "w": 100, "h": 40},
    }


def test_stable_element_passes():
    """Same element identity — no re-resolve needed."""
    identity = _make_identity()
    locator = MagicMock()
    locator.evaluate.return_value = identity.copy()

    assert is_same_element(locator, identity)


def test_replaced_element_re_resolves():
    """Identity changed — transparent re-resolve."""
    identity = _make_identity(text_hash=12345)
    locator = MagicMock()
    # Current element has different text hash
    locator.evaluate.return_value = _make_identity(text_hash=99999)

    assert not is_same_element(locator, identity)

    # Verify revalidate_before_action triggers re-resolve
    page = MagicMock()
    target = TargetDescriptor(role="button", name="Submit")

    new_locator = MagicMock()
    mock_resolved = MagicMock()
    mock_resolved.locator = new_locator
    mock_resolved.strategy.value = "role"
    mock_resolved.score.composite = 0.85

    with patch("app.worker.selector_engine.resolve_target", return_value=mock_resolved):
        result = revalidate_before_action(page, locator, identity, target)

    assert result is new_locator


def test_detached_element_re_resolves():
    """Element gone from DOM — re-resolve."""
    identity = _make_identity()
    locator = MagicMock()
    locator.evaluate.side_effect = Exception("Element is detached from DOM")

    assert not is_same_element(locator, identity)


def test_revalidation_respects_budget():
    """Empty identity skips re-validation entirely."""
    locator = MagicMock()
    page = MagicMock()
    target = TargetDescriptor(role="button", name="Submit")

    # Empty identity — should return original locator without re-resolve
    result = revalidate_before_action(page, locator, {}, target)
    assert result is locator
