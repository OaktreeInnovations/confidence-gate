"""Test: Verification provenance — validate_step returns verification_mode.

Validates that validate_step correctly reports how each step was verified:
- "dom-only": Only DOM-based validation used
- "ai": AI vision verification used
- "skipped": No verification performed
"""

from unittest.mock import MagicMock

from app.worker.intent_schema import ActionIntent, ActionType, StepIntent
from app.worker.validation import validate_step


def _make_intent(*actions: ActionIntent) -> StepIntent:
    """Create a StepIntent with the given actions."""
    return StepIntent(
        step_number=1,
        actions=list(actions),
        description="Test step",
    )


def test_dom_assertions_return_dom_only():
    """When step has DOM assertions, verification_mode should be 'dom-only'."""
    intent = _make_intent(
        ActionIntent(action=ActionType.ASSERT_TEXT, value="Hello"),
    )
    page = MagicMock()
    status, actual, mode = validate_step(page, intent, "Hello visible", None)
    assert status == "passed"
    assert mode == "dom-only"


def test_dom_state_check_returns_dom_only():
    """When DOM state check passes without vision, mode should be 'dom-only'."""
    intent = _make_intent(
        ActionIntent(action=ActionType.NAVIGATE, value="https://example.com"),
    )
    page = MagicMock()
    page.url = "https://example.com"
    status, actual, mode = validate_step(page, intent, "", None)
    assert status == "passed"
    assert mode == "dom-only"


def test_no_expected_click_returns_dom_only():
    """Click with no expected result uses DOM state check → 'dom-only'."""
    intent = _make_intent(
        ActionIntent(action=ActionType.CLICK),
    )
    page = MagicMock()
    status, actual, mode = validate_step(page, intent, "", None)
    assert status == "passed"
    assert mode == "dom-only"


def test_vision_returns_ai_mode():
    """When AI vision verification succeeds, mode should be 'ai'."""
    intent = _make_intent(
        ActionIntent(action=ActionType.CLICK),
    )
    page = MagicMock()

    ai_client = MagicMock()
    ai_client.verify_vision.return_value = {
        "status": "passed",
        "actual": "Button was clicked and form submitted",
    }

    status, actual, mode = validate_step(
        page, intent, "Form should be submitted",
        screenshot_bytes=b"fake_screenshot",
        ai_client=ai_client,
        vision_enabled=True,
    )
    assert status == "passed"
    assert mode == "ai"


def test_vision_unavailable_returns_dom_only():
    """When AI vision is unavailable (fallback), mode should be 'dom-only'."""
    intent = _make_intent(
        ActionIntent(action=ActionType.NAVIGATE, value="https://example.com"),
    )
    page = MagicMock()
    page.url = "https://example.com"

    ai_client = MagicMock()
    ai_client.verify_vision.return_value = {
        "status": "passed",
        "actual": "Vision verification unavailable (AI circuit open)",
    }

    status, actual, mode = validate_step(
        page, intent, "Page should load",
        screenshot_bytes=b"fake_screenshot",
        ai_client=ai_client,
        vision_enabled=True,
    )
    assert status == "passed"
    assert mode == "dom-only"


def test_vision_error_falls_back_to_dom():
    """When vision verification throws, should fall back to dom-only."""
    intent = _make_intent(
        ActionIntent(action=ActionType.NAVIGATE, value="https://example.com"),
    )
    page = MagicMock()
    page.url = "https://example.com"

    ai_client = MagicMock()
    ai_client.verify_vision.side_effect = Exception("API error")

    status, actual, mode = validate_step(
        page, intent, "Page should load",
        screenshot_bytes=b"fake_screenshot",
        ai_client=ai_client,
        vision_enabled=True,
    )
    assert status == "passed"
    assert mode == "dom-only"
