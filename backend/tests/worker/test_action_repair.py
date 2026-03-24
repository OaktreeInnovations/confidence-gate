"""Tests for Action Repair Engine (Healing Layer 2).

Validates alternate interaction patterns:
- click → focus+Enter, JS dispatch, dblclick
- fill → press_sequentially, clear+type, JS value
- select → click option, type filter
"""

from unittest.mock import MagicMock, patch, call

from app.worker.healing.action_repair import (
    ActionRepairResult,
    repair_action,
)
from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
from app.worker.post_action_verify import ActionVerification


def _make_action(action_type=ActionType.CLICK, value=None):
    target = TargetDescriptor(role="button", name="Submit")
    return ActionIntent(action=action_type, target=target, value=value)


def _make_locator():
    locator = MagicMock()
    locator.input_value.return_value = ""
    return locator


def _make_page():
    page = MagicMock()
    page.url = "https://example.com"
    return page


# --- Click repair ---


def test_click_repaired_via_focus_enter():
    """Click fails, focus+Enter works — verified via URL change."""
    page = _make_page()
    locator = _make_locator()
    action = _make_action(ActionType.CLICK)
    pre_state = {"url": "https://example.com/login"}

    # capture_pre_state returns current URL, verify_click detects change
    page.url = "https://example.com/dashboard"

    with patch("app.worker.healing.action_repair.capture_pre_state",
               return_value={"url": "https://example.com/login"}), \
         patch("app.worker.healing.action_repair.verify_click",
               return_value=ActionVerification(
                   verified=True, method="url_change", detail="URL changed")):
        result = repair_action(page, locator, action, pre_state)

    assert result.repaired
    assert result.method_used == "focus_enter"
    assert result.attempts == 1
    assert result.verification.verified


def test_click_repaired_via_js_dispatch():
    """Click and focus+Enter fail, JS dispatch works."""
    page = _make_page()
    locator = _make_locator()
    action = _make_action(ActionType.CLICK)
    pre_state = {"url": "https://example.com"}

    verify_calls = {"n": 0}

    def mock_verify(*args, **kwargs):
        verify_calls["n"] += 1
        # First call (focus_enter): not verified. Second call (js_dispatch): verified.
        if verify_calls["n"] >= 2:
            return ActionVerification(
                verified=True, method="dom_mutation", detail="DOM changed")
        return ActionVerification(
            verified=False, method="none", detail="No change")

    with patch("app.worker.healing.action_repair.capture_pre_state",
               return_value={"url": "https://example.com"}), \
         patch("app.worker.healing.action_repair.verify_click",
               side_effect=mock_verify):
        result = repair_action(page, locator, action, pre_state)

    assert result.repaired
    assert result.method_used == "js_dispatch"
    assert result.attempts == 2


def test_click_all_alternates_fail():
    """All click alternates fail — returns not repaired."""
    page = _make_page()
    locator = _make_locator()
    action = _make_action(ActionType.CLICK)
    pre_state = {"url": "https://example.com"}

    with patch("app.worker.healing.action_repair.capture_pre_state",
               return_value={"url": "https://example.com"}), \
         patch("app.worker.healing.action_repair.verify_click",
               return_value=ActionVerification(
                   verified=False, method="none", detail="No change")):
        result = repair_action(page, locator, action, pre_state)

    assert not result.repaired
    assert result.attempts == 3  # All 3 alternates tried


# --- Fill repair ---


def test_fill_repaired_via_sequential():
    """fill() fails, press_sequentially works."""
    page = _make_page()
    locator = _make_locator()
    action = _make_action(ActionType.INPUT, value="hello@test.com")
    pre_state = {}

    with patch("app.worker.healing.action_repair.verify_fill",
               return_value=ActionVerification(
                   verified=True, method="value_match", detail="Value matches")):
        result = repair_action(page, locator, action, pre_state)

    assert result.repaired
    assert result.method_used == "press_sequentially"
    assert result.attempts == 1


def test_fill_all_alternates_fail():
    """All fill alternates fail — returns not repaired."""
    page = _make_page()
    locator = _make_locator()
    action = _make_action(ActionType.INPUT, value="test")
    pre_state = {}

    with patch("app.worker.healing.action_repair.verify_fill",
               return_value=ActionVerification(
                   verified=False, method="none", detail="Mismatch")):
        result = repair_action(page, locator, action, pre_state)

    assert not result.repaired
    assert result.attempts == 3


# --- Select repair ---


def test_select_repaired_via_click_option():
    """select() fails, click-trigger+click-option works."""
    page = _make_page()
    locator = _make_locator()
    action = _make_action(ActionType.SELECT, value="Canada")
    pre_state = {}

    mock_option = MagicMock()
    mock_option.count.return_value = 1
    mock_option.first = mock_option
    page.get_by_role.return_value = mock_option

    with patch("app.worker.healing.action_repair.verify_select",
               return_value=ActionVerification(
                   verified=True, method="option_selected", detail="Canada selected")):
        result = repair_action(page, locator, action, pre_state)

    assert result.repaired
    assert result.method_used == "click_option"
    assert result.attempts == 1


# --- Edge cases ---


def test_no_repair_for_unsupported_action():
    """Unsupported action types return not repaired immediately."""
    page = _make_page()
    locator = _make_locator()
    action = _make_action(ActionType.NAVIGATE, value="https://example.com")
    pre_state = {}

    result = repair_action(page, locator, action, pre_state)

    assert not result.repaired
    assert result.attempts == 0


def test_max_attempts_respected():
    """Only max_attempts alternates are tried."""
    page = _make_page()
    locator = _make_locator()
    action = _make_action(ActionType.CLICK)
    pre_state = {"url": "https://example.com"}

    with patch("app.worker.healing.action_repair.capture_pre_state",
               return_value={"url": "https://example.com"}), \
         patch("app.worker.healing.action_repair.verify_click",
               return_value=ActionVerification(
                   verified=False, method="none", detail="No change")):
        result = repair_action(page, locator, action, pre_state, max_attempts=1)

    # Only 1 attempt (focus_enter), not all 3
    assert result.attempts == 1
    assert not result.repaired


def test_repair_exception_in_alternate_continues():
    """Exception in one alternate doesn't stop trying others."""
    page = _make_page()
    locator = _make_locator()
    action = _make_action(ActionType.CLICK)
    pre_state = {"url": "https://example.com"}

    call_count = {"n": 0}

    def mock_capture(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception("Focus failed")
        return {"url": "https://example.com"}

    with patch("app.worker.healing.action_repair.capture_pre_state",
               side_effect=mock_capture), \
         patch("app.worker.healing.action_repair.verify_click",
               return_value=ActionVerification(
                   verified=True, method="url_change", detail="Changed")):
        result = repair_action(page, locator, action, pre_state)

    # First alternate threw, second succeeded
    assert result.repaired
    assert result.method_used == "js_dispatch"
    assert result.attempts == 2
