"""Tests for resolution retry loop (Part 4).

Validates that when an action produces no behavior effect and behavior
repair also fails, execute_intent blacklists the selector and re-resolves
once before falling back to the step-level retry.
"""

from unittest.mock import MagicMock, patch, call

import pytest

from app.worker.behavior_detection import BehaviorEffect
from app.worker.in_run_memory import InRunSelectorMemory
from app.worker.intent_schema import ActionIntent, ActionType, StepIntent, TargetDescriptor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(action_type: ActionType = ActionType.CLICK, value: str = "") -> ActionIntent:
    return ActionIntent(
        action=action_type,
        target=TargetDescriptor(role="button", name="Submit"),
        value=value or None,
    )


def _make_intent(actions=None) -> StepIntent:
    return StepIntent(
        step_number=1,
        description="Test step",
        actions=actions or [_make_action()],
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


# ---------------------------------------------------------------------------
# Tests: Resolution Retry Trigger Conditions
# ---------------------------------------------------------------------------


class TestResolutionRetryTrigger:
    """Verify resolution retry triggers only under correct conditions."""

    @patch("app.worker.intent_executor._execute_single_action")
    def test_retry_triggers_on_no_effect_no_repair(self, mock_exec):
        """Resolution retry triggers when no effect and repair didn't help."""
        # First call: no effect, no repair success
        first_result = {
            "behavior_effect": _no_effect(),
            "selector_key": "role:name=Submit",
            "description": "Clicked Submit",
        }
        # Second call: effect detected (re-resolution worked)
        second_result = {
            "behavior_effect": _has_effect(),
            "selector_key": "label:Submit",
            "description": "Clicked Submit",
        }
        mock_exec.side_effect = [first_result, second_result]

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = _make_intent()

        success, _, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=memory,
        )

        assert success
        assert mock_exec.call_count == 2
        assert metrics["resolution_retries"] == 1

    @patch("app.worker.intent_executor._execute_single_action")
    def test_no_retry_when_effect_detected(self, mock_exec):
        """No resolution retry when behavior effect IS detected."""
        mock_exec.return_value = {
            "behavior_effect": _has_effect(),
            "selector_key": "role:name=Submit",
            "description": "Clicked Submit",
        }

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = _make_intent()

        success, _, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=memory,
        )

        assert success
        assert mock_exec.call_count == 1
        assert metrics["resolution_retries"] == 0

    @patch("app.worker.intent_executor._execute_single_action")
    def test_no_retry_when_repair_succeeded(self, mock_exec):
        """No resolution retry when behavior repair succeeded."""
        mock_exec.return_value = {
            "behavior_effect": _no_effect(),
            "behavior_retry_method": "focus_enter",  # repair worked
            "selector_key": "role:name=Submit",
            "description": "Clicked Submit",
            "interaction_attempts": 2,
        }

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = _make_intent()

        success, _, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=memory,
        )

        assert success
        assert mock_exec.call_count == 1
        assert metrics["resolution_retries"] == 0

    @patch("app.worker.intent_executor._execute_single_action")
    def test_no_retry_without_in_run_memory(self, mock_exec):
        """No resolution retry when in_run_memory is None."""
        mock_exec.return_value = {
            "behavior_effect": _no_effect(),
            "selector_key": "role:name=Submit",
            "description": "Clicked Submit",
        }

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        intent = _make_intent()

        success, _, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=None,
        )

        assert success
        assert mock_exec.call_count == 1
        assert metrics["resolution_retries"] == 0

    @patch("app.worker.intent_executor._execute_single_action")
    def test_no_retry_without_selector_key(self, mock_exec):
        """No resolution retry when result has no selector_key."""
        mock_exec.return_value = {
            "behavior_effect": _no_effect(),
            # No selector_key — e.g. targetless action
            "description": "Waited",
        }

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = _make_intent([ActionIntent(
            action=ActionType.NAVIGATE,
            value="https://example.com",
        )])

        success, _, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=memory,
        )

        assert mock_exec.call_count == 1
        assert metrics["resolution_retries"] == 0

    @patch("app.worker.intent_executor._execute_single_action")
    def test_no_retry_for_non_retriable_action(self, mock_exec):
        """No resolution retry for CHECK (not in _RESOLUTION_RETRY_ACTIONS)."""
        mock_exec.return_value = {
            "behavior_effect": _no_effect(),
            "selector_key": "role:name=Agree",
            "description": "Checked Agree",
        }

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = _make_intent([ActionIntent(
            action=ActionType.CHECK,
            target=TargetDescriptor(role="checkbox", name="Agree"),
        )])

        success, _, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=memory,
        )

        assert mock_exec.call_count == 1
        assert metrics["resolution_retries"] == 0


class TestResolutionRetryBlacklist:
    """Verify blacklisting behavior during resolution retry."""

    @patch("app.worker.intent_executor._execute_single_action")
    def test_selector_blacklisted_before_retry(self, mock_exec):
        """Failed selector is blacklisted before re-resolution."""
        mock_exec.side_effect = [
            {
                "behavior_effect": _no_effect(),
                "selector_key": "role:name=Submit",
                "description": "Clicked Submit",
            },
            {
                "behavior_effect": _has_effect(),
                "selector_key": "label:Submit",
                "description": "Clicked Submit",
            },
        ]

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = _make_intent()

        execute_intent(page, intent, wait_budget_ms=30000, in_run_memory=memory)

        # The old selector should be blacklisted
        assert memory.is_blacklisted(0, "role:name=Submit")

    @patch("app.worker.intent_executor._execute_single_action")
    def test_new_selector_promoted_on_retry_success(self, mock_exec):
        """Successful retry's selector is promoted in in_run_memory."""
        mock_exec.side_effect = [
            {
                "behavior_effect": _no_effect(),
                "selector_key": "role:name=Submit",
                "description": "Clicked Submit",
            },
            {
                "behavior_effect": _has_effect(),
                "selector_key": "label:Submit",
                "description": "Clicked Submit",
            },
        ]

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = _make_intent()

        execute_intent(page, intent, wait_budget_ms=30000, in_run_memory=memory)

        # New selector should be promoted
        assert memory.get_preferred(0) == "label:Submit"


class TestResolutionRetryOutcome:
    """Verify retry outcomes and metric tracking."""

    @patch("app.worker.intent_executor._execute_single_action")
    def test_retry_result_used_when_effect_detected(self, mock_exec):
        """When retry produces an effect, its result replaces the original."""
        mock_exec.side_effect = [
            {
                "behavior_effect": _no_effect(),
                "selector_key": "role:name=Submit",
                "selector_path": "button >> Submit",
                "description": "Clicked Submit (wrong)",
            },
            {
                "behavior_effect": _has_effect(["url_change"]),
                "selector_key": "label:Submit",
                "selector_path": "input >> Submit",
                "description": "Clicked Submit (correct)",
            },
        ]

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = _make_intent()

        success, summary, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=memory,
        )

        assert success
        assert metrics["resolution_retries"] == 1
        # Behavior failure should NOT be counted since retry succeeded
        assert metrics["behavior_failures"] == 0

    @patch("app.worker.intent_executor._execute_single_action")
    def test_retry_result_used_when_repair_succeeded(self, mock_exec):
        """When retry's behavior repair succeeded, its result replaces original."""
        mock_exec.side_effect = [
            {
                "behavior_effect": _no_effect(),
                "selector_key": "role:name=Submit",
                "description": "Clicked Submit (wrong)",
            },
            {
                "behavior_effect": _no_effect(),  # still no effect from primary action
                "behavior_retry_method": "js_dispatch",  # but repair worked
                "selector_key": "label:Submit",
                "description": "Clicked Submit (retry)",
                "interaction_attempts": 2,
            },
        ]

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = _make_intent()

        success, _, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=memory,
        )

        assert success
        assert metrics["resolution_retries"] == 1

    @patch("app.worker.intent_executor._execute_single_action")
    def test_original_kept_when_retry_also_fails(self, mock_exec):
        """When retry also has no effect, original result is kept."""
        mock_exec.side_effect = [
            {
                "behavior_effect": _no_effect(),
                "selector_key": "role:name=Submit",
                "description": "Original click",
            },
            {
                "behavior_effect": _no_effect(),
                "selector_key": "label:Submit",
                "description": "Retry click",
            },
        ]

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = _make_intent()

        success, _, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=memory,
        )

        assert success
        assert metrics["resolution_retries"] == 1
        # Behavior failure counted since neither attempt succeeded
        assert metrics["behavior_failures"] == 1

    @patch("app.worker.intent_executor._execute_single_action")
    def test_retry_exception_handled_gracefully(self, mock_exec):
        """RuntimeError during re-resolution doesn't crash execute_intent."""
        mock_exec.side_effect = [
            {
                "behavior_effect": _no_effect(),
                "selector_key": "role:name=Submit",
                "description": "Clicked Submit",
            },
            RuntimeError("All candidates blacklisted"),
        ]

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        memory = InRunSelectorMemory()
        intent = _make_intent()

        # Should NOT raise
        success, _, _, metrics, _ = execute_intent(
            page, intent, wait_budget_ms=30000, in_run_memory=memory,
        )

        assert success
        assert metrics["resolution_retries"] == 1

    @patch("app.worker.intent_executor._execute_single_action")
    def test_metrics_include_resolution_retries(self, mock_exec):
        """resolution_retries metric is always present in stability_metrics."""
        mock_exec.return_value = {
            "behavior_effect": _has_effect(),
            "description": "Clicked Submit",
        }

        from app.worker.intent_executor import execute_intent

        page = _make_page()
        intent = _make_intent()

        _, _, _, metrics, _ = execute_intent(page, intent, wait_budget_ms=30000)

        assert "resolution_retries" in metrics
        assert metrics["resolution_retries"] == 0
