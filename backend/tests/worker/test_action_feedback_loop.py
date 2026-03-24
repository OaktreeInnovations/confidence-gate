"""Tests for the Action Feedback Loop (Part 7).

Validates the unified feedback pipeline:
1. Post-action DOM stability wait before behavior detection
2. Expanded resolution retry (also on verification failure)
3. action_feedback summary in result
"""

from unittest.mock import MagicMock, patch

import pytest

from app.worker.behavior_detection import BehaviorEffect
from app.worker.in_run_memory import InRunSelectorMemory
from app.worker.intent_schema import ActionIntent, ActionType, StepIntent, TargetDescriptor
from app.worker.post_action_verify import ActionVerification


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(action_type: ActionType = ActionType.CLICK, value: str = "") -> ActionIntent:
    return ActionIntent(
        action=action_type,
        target=TargetDescriptor(role="button", name="Submit"),
        value=value or None,
    )


def _no_effect():
    return BehaviorEffect(detected=False, signals=[], detail="No observable effect")


def _has_effect(signals=None):
    signals = signals or ["dom_mutation"]
    return BehaviorEffect(detected=True, signals=signals, detail=", ".join(signals))


def _make_page():
    page = MagicMock()
    page.url = "https://example.com/app"
    page.title.return_value = "Test App"
    return page


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
    loc.evaluate.return_value = {"found": False, "depth": 0, "tag": "", "role": ""}
    return loc


# ---------------------------------------------------------------------------
# Tests: Post-Action DOM Stability Wait
# ---------------------------------------------------------------------------


class TestPostActionStability:
    """Verify DOM stability wait fires before behavior detection."""

    @patch("app.worker.intent_executor.wait_for_dom_stable")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_dom_stable_called_before_detection(
        self, mock_snapshot, mock_detect, mock_stable,
    ):
        """wait_for_dom_stable called after action, before behavior detection."""
        from app.worker.behavior_detection import PageSnapshot
        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _has_effect()

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
                locator=locator, strategy="role",
                selector_path="button >> Submit",
                score=MagicMock(strategy=MagicMock(value="role"), params="name=Submit"),
                to_telemetry=MagicMock(return_value={}),
            )
            mock_guard.return_value = MagicMock(passed=True, failures=[])
            mock_verify.return_value = ActionVerification(verified=True, method="url_change")

            _execute_single_action(page, action, timeout_ms=10000)

        mock_stable.assert_called_once_with(page, timeout_ms=1500, settle_ms=150)

    @patch("app.worker.intent_executor.wait_for_dom_stable")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_dom_stable_exception_non_fatal(
        self, mock_snapshot, mock_detect, mock_stable,
    ):
        """wait_for_dom_stable exception doesn't crash execution."""
        from app.worker.behavior_detection import PageSnapshot
        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _has_effect()
        mock_stable.side_effect = RuntimeError("JS eval failed")

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
                locator=locator, strategy="role",
                selector_path="button >> Submit",
                score=MagicMock(strategy=MagicMock(value="role"), params="name=Submit"),
                to_telemetry=MagicMock(return_value={}),
            )
            mock_guard.return_value = MagicMock(passed=True, failures=[])
            mock_verify.return_value = ActionVerification(verified=True, method="url_change")

            # Should NOT raise
            result = _execute_single_action(page, action, timeout_ms=10000)

        assert isinstance(result, dict)
        # Behavior detection should still have been called
        mock_detect.assert_called_once()

    @patch("app.worker.intent_executor.wait_for_dom_stable")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_dom_stable_skipped_for_untracked_actions(
        self, mock_snapshot, mock_stable,
    ):
        """DOM stability wait skipped for actions without behavior tracking."""
        mock_snapshot.return_value = None  # No snapshot for untracked actions

        from app.worker.intent_executor import _execute_single_action

        page = _make_page()
        # ASSERT_TEXT doesn't capture page_snapshot
        action = ActionIntent(
            action=ActionType.ASSERT_TEXT,
            target=TargetDescriptor(role="heading", name="Title"),
            value="Expected Text",
        )
        locator = _make_locator()

        with patch("app.worker.intent_executor.resolve_target") as mock_resolve, \
             patch("app.worker.intent_executor.ensure_interactable") as mock_guard, \
             patch("app.worker.intent_executor.capture_element_identity", return_value={}), \
             patch("app.worker.intent_executor.revalidate_before_action", return_value=locator), \
             patch("app.worker.intent_executor.capture_pre_state", return_value={}), \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            mock_resolve.return_value = MagicMock(
                locator=locator, strategy="role",
                selector_path="heading >> Title",
                score=MagicMock(strategy=MagicMock(value="role"), params="name=Title"),
                to_telemetry=MagicMock(return_value={}),
            )
            mock_guard.return_value = MagicMock(passed=True, failures=[])

            from playwright.sync_api import expect
            with patch("app.worker.intent_executor.expect") if hasattr(
                __import__("app.worker.intent_executor"), "expect"
            ) else patch("playwright.sync_api.expect"):
                try:
                    _execute_single_action(page, action, timeout_ms=10000)
                except Exception:
                    pass  # expect() may fail on mock, that's fine

        mock_stable.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Expanded Resolution Retry (Verification Failure)
# ---------------------------------------------------------------------------


class TestResolutionRetryOnVerificationFailure:
    """Verify resolution retry also fires on verified=False."""

    @patch("app.worker.intent_executor._execute_single_action")
    def test_retry_on_effect_but_unverified(self, mock_exec):
        """Resolution retry triggers when effect detected but verification fails."""
        # First call: effect detected but verification fails (wrong element)
        first_result = {
            "behavior_effect": _has_effect(["dom_mutation"]),
            "action_verification": ActionVerification(verified=False, method="value_match"),
            "selector_key": "role:name=Submit",
            "description": "Clicked wrong button",
        }
        # Second call: retry succeeds with verification
        second_result = {
            "behavior_effect": _has_effect(["url_change"]),
            "action_verification": ActionVerification(verified=True, method="url_match"),
            "selector_key": "label:Submit",
            "description": "Clicked correct button",
        }
        mock_exec.side_effect = [first_result, second_result]

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = StepIntent(
            step_number=1,
            description="Click submit",
            actions=[_make_action()],
        )

        success, _, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=memory,
        )

        assert success
        assert mock_exec.call_count == 2
        assert metrics["resolution_retries"] == 1

    @patch("app.worker.intent_executor._execute_single_action")
    def test_no_retry_when_both_effect_and_verified(self, mock_exec):
        """No resolution retry when behavior detected AND verified."""
        mock_exec.return_value = {
            "behavior_effect": _has_effect(["url_change"]),
            "action_verification": ActionVerification(verified=True, method="url_match"),
            "selector_key": "role:name=Submit",
            "description": "Clicked Submit",
        }

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = StepIntent(
            step_number=1,
            description="Click submit",
            actions=[_make_action()],
        )

        success, _, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=memory,
        )

        assert mock_exec.call_count == 1
        assert metrics["resolution_retries"] == 0

    @patch("app.worker.intent_executor._execute_single_action")
    def test_retry_accepts_on_verification_pass(self, mock_exec):
        """Resolution retry success determined by verification passing."""
        mock_exec.side_effect = [
            {
                "behavior_effect": _has_effect(["dom_mutation"]),
                "action_verification": ActionVerification(verified=False, method=""),
                "selector_key": "role:name=Submit",
                "description": "Wrong element",
            },
            {
                # No behavior effect but verification passes → accepted
                "behavior_effect": _no_effect(),
                "action_verification": ActionVerification(verified=True, method="value_match"),
                "selector_key": "label:Submit",
                "description": "Correct element",
            },
        ]

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = StepIntent(
            step_number=1,
            description="Click submit",
            actions=[_make_action()],
        )

        success, _, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=memory,
        )

        assert success
        assert metrics["resolution_retries"] == 1
        # The retry result should be used (verification passed)
        assert memory.get_preferred(0) == "label:Submit"


# ---------------------------------------------------------------------------
# Tests: Action Feedback Tracking
# ---------------------------------------------------------------------------


class TestActionFeedback:
    """Verify action_feedback dict captures the full chain."""

    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_feedback_captures_full_chain(
        self, mock_snapshot, mock_detect,
    ):
        """action_feedback includes behavior, repair, verification, attempts."""
        from app.worker.behavior_detection import PageSnapshot
        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _has_effect(["url_change", "dom_mutation"])

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
             patch("app.worker.intent_executor.wait_for_dom_stable"), \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            mock_resolve.return_value = MagicMock(
                locator=locator, strategy="role",
                selector_path="button >> Submit",
                score=MagicMock(strategy=MagicMock(value="role"), params="name=Submit"),
                to_telemetry=MagicMock(return_value={}),
            )
            mock_guard.return_value = MagicMock(passed=True, failures=[])
            mock_verify.return_value = ActionVerification(verified=True, method="url_change")

            result = _execute_single_action(page, action, timeout_ms=10000)

        feedback = result["action_feedback"]
        assert feedback["action"] == "click"
        assert feedback["behavior_detected"] is True
        assert feedback["behavior_signals"] == ["url_change", "dom_mutation"]
        assert feedback["repair_method"] is None
        assert feedback["verified"] is True
        assert feedback["verification_method"] == "url_change"
        assert feedback["interaction_attempts"] == 1
        assert feedback["ancestor_used"] is False

    @patch("app.worker.intent_executor.repair_action")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_feedback_with_repair(
        self, mock_snapshot, mock_detect, mock_repair,
    ):
        """action_feedback captures repair method when repair was used."""
        from app.worker.behavior_detection import PageSnapshot
        from app.worker.healing.action_repair import ActionRepairResult

        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = _no_effect()
        mock_repair.return_value = ActionRepairResult(
            repaired=True, method_used="js_dispatch", attempts=1,
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
             patch("app.worker.intent_executor.wait_for_dom_stable"), \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            mock_resolve.return_value = MagicMock(
                locator=locator, strategy="role",
                selector_path="button >> Submit",
                score=MagicMock(strategy=MagicMock(value="role"), params="name=Submit"),
                to_telemetry=MagicMock(return_value={}),
            )
            mock_guard.return_value = MagicMock(passed=True, failures=[])
            mock_verify.return_value = ActionVerification(verified=True, method="url_change")

            result = _execute_single_action(page, action, timeout_ms=10000)

        feedback = result["action_feedback"]
        assert feedback["repair_method"] == "js_dispatch"
        assert feedback["interaction_attempts"] == 2  # 1 original + 1 repair

    def test_feedback_for_navigate_action(self):
        """action_feedback generated for NAVIGATE (no locator, no ancestor)."""
        from app.worker.intent_executor import _execute_single_action
        from app.worker.behavior_detection import PageSnapshot

        page = _make_page()
        action = ActionIntent(action=ActionType.NAVIGATE, value="https://example.com/next")

        with patch("app.worker.intent_executor.safe_goto"), \
             patch("app.worker.intent_executor.capture_pre_state", return_value={}), \
             patch("app.worker.intent_executor.capture_page_snapshot") as mock_snap, \
             patch("app.worker.intent_executor.detect_behavior_effect") as mock_detect, \
             patch("app.worker.intent_executor.wait_for_dom_stable"), \
             patch("app.worker.intent_executor.verify_navigate") as mock_verify, \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            mock_snap.return_value = PageSnapshot(url="https://example.com")
            mock_detect.return_value = _has_effect(["url_change"])
            mock_verify.return_value = ActionVerification(verified=True, method="url_match")

            result = _execute_single_action(page, action, timeout_ms=10000)

        feedback = result["action_feedback"]
        assert feedback["action"] == "navigate"
        assert feedback["behavior_detected"] is True
        assert feedback["verified"] is True
        assert feedback["ancestor_used"] is False
