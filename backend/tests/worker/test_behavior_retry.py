"""Tests for behavior-driven retry (Part 3).

Validates that when behavior effect detection returns no effect,
the executor proactively calls repair_action() with alternate
interaction strategies before falling through to verification.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.worker.behavior_detection import BehaviorEffect, PageSnapshot
from app.worker.healing.action_repair import ActionRepairResult
from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
from app.worker.post_action_verify import ActionVerification


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(action_type: ActionType, value: str = "") -> ActionIntent:
    return ActionIntent(
        action=action_type,
        target=TargetDescriptor(role="button", name="Submit"),
        value=value or None,
    )


def _make_locator():
    loc = MagicMock()
    loc.count.return_value = 1
    loc.first = loc
    loc.is_visible.return_value = True
    loc.is_enabled.return_value = True
    loc.click = MagicMock()
    loc.fill = MagicMock()
    loc.input_value.return_value = ""
    loc.get_attribute.return_value = None
    # discover_clickable_ancestor JS evaluate
    loc.evaluate.return_value = {"found": False, "depth": 0, "tag": "", "role": ""}
    return loc


def _make_page():
    page = MagicMock()
    page.url = "https://example.com/app"
    page.title.return_value = "Test App"
    page.wait_for_timeout = MagicMock()
    page.keyboard = MagicMock()
    page.evaluate.return_value = {
        "url": "https://example.com/app",
        "domHash": 12345,
        "activeTag": "body",
        "activeId": "",
        "listboxCount": 0,
        "menuCount": 0,
        "dialogCount": 0,
    }
    return page


def _no_effect():
    return BehaviorEffect(detected=False, signals=[], detail="No observable effect")


def _has_effect(signals=None):
    signals = signals or ["dom_mutation"]
    return BehaviorEffect(detected=True, signals=signals, detail=", ".join(signals))


def _repair_success(method="focus_enter", attempts=1):
    return ActionRepairResult(
        repaired=True,
        method_used=method,
        attempts=attempts,
        verification=ActionVerification(verified=True, method="url_change"),
        elapsed_ms=50,
    )


def _repair_failure(attempts=2):
    return ActionRepairResult(
        repaired=False,
        method_used="",
        attempts=attempts,
        elapsed_ms=100,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBehaviorRetryTrigger:
    """Verify that behavior retry triggers only when appropriate."""

    @patch("app.worker.intent_executor.repair_action")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_retry_triggers_on_no_effect_click(
        self, mock_snapshot, mock_detect, mock_repair,
    ):
        """Behavior retry triggers for CLICK when no effect detected."""
        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _no_effect()
        mock_repair.return_value = _repair_success()

        from app.worker.intent_executor import _execute_single_action

        page = _make_page()
        action = _make_action(ActionType.CLICK)
        locator = _make_locator()

        with patch("app.worker.intent_executor.resolve_target") as mock_resolve, \
             patch("app.worker.intent_executor.ensure_interactable") as mock_guard, \
             patch("app.worker.intent_executor.capture_element_identity", return_value={}), \
             patch("app.worker.intent_executor.revalidate_before_action", return_value=locator), \
             patch("app.worker.intent_executor.discover_clickable_ancestor", return_value=locator), \
             patch("app.worker.intent_executor.safe_click"), \
             patch("app.worker.intent_executor.verify_click") as mock_verify, \
             patch("app.worker.intent_executor.capture_pre_state", return_value={}), \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            mock_resolve.return_value = MagicMock(
                locator=locator,
                strategy="role",
                selector_path="button >> Submit",
                score=MagicMock(strategy=MagicMock(value="role"), params="name=Submit"),
                to_telemetry=MagicMock(return_value={}),
            )
            mock_guard.return_value = MagicMock(passed=True, failures=[])
            mock_verify.return_value = ActionVerification(verified=True, method="url_change")

            result = _execute_single_action(page, action, timeout_ms=10000)

        mock_repair.assert_called_once()
        assert result.get("behavior_retry_method") == "focus_enter"

    @patch("app.worker.intent_executor.repair_action")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_retry_triggers_on_no_effect_input(
        self, mock_snapshot, mock_detect, mock_repair,
    ):
        """Behavior retry triggers for INPUT when no effect detected."""
        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _no_effect()
        mock_repair.return_value = _repair_success(method="press_sequentially")

        from app.worker.intent_executor import _execute_single_action

        page = _make_page()
        action = _make_action(ActionType.INPUT, value="hello")
        locator = _make_locator()

        with patch("app.worker.intent_executor.resolve_target") as mock_resolve, \
             patch("app.worker.intent_executor.ensure_interactable") as mock_guard, \
             patch("app.worker.intent_executor.capture_element_identity", return_value={}), \
             patch("app.worker.intent_executor.revalidate_before_action", return_value=locator), \
             patch("app.worker.intent_executor.discover_clickable_ancestor", return_value=locator), \
             patch("app.worker.intent_executor.safe_fill"), \
             patch("app.worker.intent_executor.verify_fill") as mock_verify, \
             patch("app.worker.intent_executor.capture_pre_state", return_value={}), \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            mock_resolve.return_value = MagicMock(
                locator=locator,
                strategy="role",
                selector_path="input >> Email",
                score=MagicMock(strategy=MagicMock(value="role"), params="name=Email"),
                to_telemetry=MagicMock(return_value={}),
            )
            mock_guard.return_value = MagicMock(passed=True, failures=[])
            mock_verify.return_value = ActionVerification(verified=True, method="value_match")

            result = _execute_single_action(page, action, timeout_ms=10000)

        mock_repair.assert_called_once()
        assert result.get("behavior_retry_method") == "press_sequentially"

    @patch("app.worker.intent_executor.repair_action")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_no_retry_when_effect_detected(
        self, mock_snapshot, mock_detect, mock_repair,
    ):
        """No behavior retry when effect IS detected."""
        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _has_effect(["dom_mutation"])

        from app.worker.intent_executor import _execute_single_action

        page = _make_page()
        action = _make_action(ActionType.CLICK)
        locator = _make_locator()

        with patch("app.worker.intent_executor.resolve_target") as mock_resolve, \
             patch("app.worker.intent_executor.ensure_interactable") as mock_guard, \
             patch("app.worker.intent_executor.capture_element_identity", return_value={}), \
             patch("app.worker.intent_executor.revalidate_before_action", return_value=locator), \
             patch("app.worker.intent_executor.discover_clickable_ancestor", return_value=locator), \
             patch("app.worker.intent_executor.safe_click"), \
             patch("app.worker.intent_executor.verify_click") as mock_verify, \
             patch("app.worker.intent_executor.capture_pre_state", return_value={}), \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            mock_resolve.return_value = MagicMock(
                locator=locator,
                strategy="role",
                selector_path="button >> Submit",
                score=MagicMock(strategy=MagicMock(value="role"), params="name=Submit"),
                to_telemetry=MagicMock(return_value={}),
            )
            mock_guard.return_value = MagicMock(passed=True, failures=[])
            mock_verify.return_value = ActionVerification(verified=True, method="url_change")

            _execute_single_action(page, action, timeout_ms=10000)

        mock_repair.assert_not_called()

    @patch("app.worker.intent_executor.repair_action")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_no_retry_for_navigate_action(
        self, mock_snapshot, mock_detect, mock_repair,
    ):
        """No behavior retry for NAVIGATE — not in _BEHAVIOR_RETRY_ACTIONS."""
        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _no_effect()

        from app.worker.intent_executor import _execute_single_action

        page = _make_page()
        action = ActionIntent(
            action=ActionType.NAVIGATE,
            value="https://example.com/other",
        )

        with patch("app.worker.intent_executor.safe_goto"), \
             patch("app.worker.intent_executor.verify_navigate") as mock_verify, \
             patch("app.worker.intent_executor.capture_pre_state", return_value={}), \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            mock_verify.return_value = ActionVerification(verified=True, method="url_match")
            _execute_single_action(page, action, timeout_ms=10000)

        mock_repair.assert_not_called()


class TestBehaviorRetryOutcome:
    """Verify results and tracking from behavior retry."""

    @patch("app.worker.intent_executor.repair_action")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_interaction_attempts_tracked_on_success(
        self, mock_snapshot, mock_detect, mock_repair,
    ):
        """interaction_attempts = 1 (original) + repair attempts."""
        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _no_effect()
        mock_repair.return_value = _repair_success(method="js_dispatch", attempts=2)

        from app.worker.intent_executor import _execute_single_action

        page = _make_page()
        action = _make_action(ActionType.CLICK)
        locator = _make_locator()

        with patch("app.worker.intent_executor.resolve_target") as mock_resolve, \
             patch("app.worker.intent_executor.ensure_interactable") as mock_guard, \
             patch("app.worker.intent_executor.capture_element_identity", return_value={}), \
             patch("app.worker.intent_executor.revalidate_before_action", return_value=locator), \
             patch("app.worker.intent_executor.discover_clickable_ancestor", return_value=locator), \
             patch("app.worker.intent_executor.safe_click"), \
             patch("app.worker.intent_executor.verify_click") as mock_verify, \
             patch("app.worker.intent_executor.capture_pre_state", return_value={}), \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            mock_resolve.return_value = MagicMock(
                locator=locator,
                strategy="role",
                selector_path="button >> Submit",
                score=MagicMock(strategy=MagicMock(value="role"), params="name=Submit"),
                to_telemetry=MagicMock(return_value={}),
            )
            mock_guard.return_value = MagicMock(passed=True, failures=[])
            mock_verify.return_value = ActionVerification(verified=True, method="url_change")

            result = _execute_single_action(page, action, timeout_ms=10000)

        # 1 original + 2 repair attempts
        assert result.get("interaction_attempts") == 3

    @patch("app.worker.intent_executor.repair_action")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_interaction_attempts_tracked_on_failure(
        self, mock_snapshot, mock_detect, mock_repair,
    ):
        """interaction_attempts tracked even when repair fails."""
        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _no_effect()
        mock_repair.return_value = _repair_failure(attempts=2)

        from app.worker.intent_executor import _execute_single_action

        page = _make_page()
        action = _make_action(ActionType.CLICK)
        locator = _make_locator()

        with patch("app.worker.intent_executor.resolve_target") as mock_resolve, \
             patch("app.worker.intent_executor.ensure_interactable") as mock_guard, \
             patch("app.worker.intent_executor.capture_element_identity", return_value={}), \
             patch("app.worker.intent_executor.revalidate_before_action", return_value=locator), \
             patch("app.worker.intent_executor.discover_clickable_ancestor", return_value=locator), \
             patch("app.worker.intent_executor.safe_click"), \
             patch("app.worker.intent_executor.verify_click") as mock_verify, \
             patch("app.worker.intent_executor.capture_pre_state", return_value={}), \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            mock_resolve.return_value = MagicMock(
                locator=locator,
                strategy="role",
                selector_path="button >> Submit",
                score=MagicMock(strategy=MagicMock(value="role"), params="name=Submit"),
                to_telemetry=MagicMock(return_value={}),
            )
            mock_guard.return_value = MagicMock(passed=True, failures=[])
            mock_verify.return_value = ActionVerification(verified=False, method="")

            result = _execute_single_action(page, action, timeout_ms=10000)

        # 1 original + 2 repair attempts
        assert result.get("interaction_attempts") == 3
        # No retry method when repair failed
        assert "behavior_retry_method" not in result

    @patch("app.worker.intent_executor.repair_action")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_behavior_retry_method_tracked_on_success(
        self, mock_snapshot, mock_detect, mock_repair,
    ):
        """Successful repair tracks the method used in result."""
        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _no_effect()
        mock_repair.return_value = ActionRepairResult(
            repaired=True,
            method_used="focus_enter",
            attempts=1,
            verification=ActionVerification(verified=True, method="url_change"),
            elapsed_ms=50,
        )

        from app.worker.intent_executor import _execute_single_action

        page = _make_page()
        action = _make_action(ActionType.CLICK)
        locator = _make_locator()

        with patch("app.worker.intent_executor.resolve_target") as mock_resolve, \
             patch("app.worker.intent_executor.ensure_interactable") as mock_guard, \
             patch("app.worker.intent_executor.capture_element_identity", return_value={}), \
             patch("app.worker.intent_executor.revalidate_before_action", return_value=locator), \
             patch("app.worker.intent_executor.discover_clickable_ancestor", return_value=locator), \
             patch("app.worker.intent_executor.safe_click"), \
             patch("app.worker.intent_executor.verify_click") as mock_verify, \
             patch("app.worker.intent_executor.capture_pre_state", return_value={}), \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            mock_resolve.return_value = MagicMock(
                locator=locator,
                strategy="role",
                selector_path="button >> Submit",
                score=MagicMock(strategy=MagicMock(value="role"), params="name=Submit"),
                to_telemetry=MagicMock(return_value={}),
            )
            mock_guard.return_value = MagicMock(passed=True, failures=[])
            mock_verify.return_value = ActionVerification(verified=True, method="url_change")

            result = _execute_single_action(page, action, timeout_ms=10000)

        assert result.get("behavior_retry_method") == "focus_enter"
        assert result.get("interaction_attempts") == 2  # 1 original + 1 repair

    @patch("app.worker.intent_executor.repair_action")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_repair_exception_handled_gracefully(
        self, mock_snapshot, mock_detect, mock_repair,
    ):
        """Exception in repair_action doesn't crash the executor."""
        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _no_effect()
        mock_repair.side_effect = RuntimeError("repair exploded")

        from app.worker.intent_executor import _execute_single_action

        page = _make_page()
        action = _make_action(ActionType.CLICK)
        locator = _make_locator()

        with patch("app.worker.intent_executor.resolve_target") as mock_resolve, \
             patch("app.worker.intent_executor.ensure_interactable") as mock_guard, \
             patch("app.worker.intent_executor.capture_element_identity", return_value={}), \
             patch("app.worker.intent_executor.revalidate_before_action", return_value=locator), \
             patch("app.worker.intent_executor.discover_clickable_ancestor", return_value=locator), \
             patch("app.worker.intent_executor.safe_click"), \
             patch("app.worker.intent_executor.verify_click") as mock_verify, \
             patch("app.worker.intent_executor.capture_pre_state", return_value={}), \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            mock_resolve.return_value = MagicMock(
                locator=locator,
                strategy="role",
                selector_path="button >> Submit",
                score=MagicMock(strategy=MagicMock(value="role"), params="name=Submit"),
                to_telemetry=MagicMock(return_value={}),
            )
            mock_guard.return_value = MagicMock(passed=True, failures=[])
            mock_verify.return_value = ActionVerification(verified=False, method="")

            # Should NOT raise
            result = _execute_single_action(page, action, timeout_ms=10000)

        # Result should still exist (graceful degradation)
        assert isinstance(result, dict)

    @patch("app.worker.intent_executor.repair_action")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_no_retry_when_locator_is_none(
        self, mock_snapshot, mock_detect, mock_repair,
    ):
        """No behavior retry when locator is None (targetless NAVIGATE action)."""
        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _no_effect()

        from app.worker.intent_executor import _execute_single_action

        page = _make_page()
        # NAVIGATE is targetless — locator will be None
        action = ActionIntent(action=ActionType.NAVIGATE, value="https://example.com/other")

        with patch("app.worker.intent_executor.safe_goto"), \
             patch("app.worker.intent_executor.verify_navigate") as mock_verify, \
             patch("app.worker.intent_executor.capture_pre_state", return_value={}), \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):
            mock_verify.return_value = ActionVerification(verified=True, method="url_match")
            _execute_single_action(page, action, timeout_ms=10000)

        mock_repair.assert_not_called()


class TestBehaviorRetryMetrics:
    """Verify metrics aggregation in execute_intent."""

    @patch("app.worker.intent_executor._execute_single_action")
    def test_behavior_failures_counted_in_step(self, mock_exec):
        """behavior_failure_count increments per action with no effect."""
        mock_exec.return_value = {
            "behavior_effect": _no_effect(),
            "interaction_attempts": 3,
            "description": "Clicked Submit",
        }

        from app.worker.intent_executor import execute_intent
        from app.worker.intent_schema import StepIntent

        page = _make_page()
        intent = StepIntent(
            step_number=1,
            description="Click submit",
            actions=[_make_action(ActionType.CLICK)],
        )

        success, summary, selectors, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000,
        )

        assert metrics["behavior_failures"] == 1
        assert metrics["interaction_attempts"] == 3

    @patch("app.worker.intent_executor._execute_single_action")
    def test_no_behavior_failure_when_effect_detected(self, mock_exec):
        """behavior_failure_count stays 0 when effect is detected."""
        mock_exec.return_value = {
            "behavior_effect": _has_effect(["dom_mutation"]),
            "interaction_attempts": 1,
            "description": "Clicked Submit",
        }

        from app.worker.intent_executor import execute_intent
        from app.worker.intent_schema import StepIntent

        page = _make_page()
        intent = StepIntent(
            step_number=1,
            description="Click submit",
            actions=[_make_action(ActionType.CLICK)],
        )

        success, summary, selectors, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000,
        )

        assert metrics["behavior_failures"] == 0
        assert metrics["interaction_attempts"] == 1
