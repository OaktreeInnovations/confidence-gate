"""Tests for BDRE Execution Telemetry (Part 10).

Validates that BDRE signals flow through the telemetry pipeline:
- behavior_failures, interaction_attempts, resolution_retries
- behavior_signals, mutation_total, mutation_significant
- Fields stored in ExecutionAttempt via record_stability_metrics
- Fields aggregated in execute_intent's stability_metrics dict
"""

from unittest.mock import MagicMock, patch

import pytest

from app.telemetry.collector import _StepTelemetryBuilder
from app.telemetry.models import ExecutionAttempt
from app.worker.behavior_detection import BehaviorEffect
from app.worker.mutation_watcher import MutationSummary


# ---------------------------------------------------------------------------
# Telemetry Model Tests
# ---------------------------------------------------------------------------


class TestExecutionAttemptBDREFields:
    """Verify BDRE fields exist on ExecutionAttempt model."""

    def test_default_bdre_fields(self):
        """BDRE fields have correct defaults."""
        attempt = ExecutionAttempt(attempt_number=1)
        assert attempt.behavior_failures == 0
        assert attempt.interaction_attempts == 0
        assert attempt.resolution_retries == 0
        assert attempt.behavior_signals == []
        assert attempt.mutation_total == 0
        assert attempt.mutation_significant is False

    def test_bdre_fields_set_explicitly(self):
        """BDRE fields can be set explicitly."""
        attempt = ExecutionAttempt(
            attempt_number=1,
            behavior_failures=2,
            interaction_attempts=5,
            resolution_retries=1,
            behavior_signals=["url_change", "dom_mutation"],
            mutation_total=15,
            mutation_significant=True,
        )
        assert attempt.behavior_failures == 2
        assert attempt.interaction_attempts == 5
        assert attempt.resolution_retries == 1
        assert attempt.behavior_signals == ["url_change", "dom_mutation"]
        assert attempt.mutation_total == 15
        assert attempt.mutation_significant is True

    def test_bdre_fields_serialize(self):
        """BDRE fields included in model serialization."""
        attempt = ExecutionAttempt(
            attempt_number=1,
            behavior_failures=1,
            behavior_signals=["focus_change"],
            mutation_total=8,
        )
        d = attempt.model_dump()
        assert d["behavior_failures"] == 1
        assert d["behavior_signals"] == ["focus_change"]
        assert d["mutation_total"] == 8


# ---------------------------------------------------------------------------
# Telemetry Collector Tests
# ---------------------------------------------------------------------------


class TestCollectorBDREMetrics:
    """Verify _StepTelemetryBuilder stages and records BDRE metrics."""

    def test_stability_metrics_stages_bdre_fields(self):
        """record_stability_metrics accepts and stages BDRE fields."""
        builder = _StepTelemetryBuilder(step_number=1, action="click")

        builder.record_stability_metrics(
            resolution_tier=2,
            guard_failures=["overlay_detected"],
            post_action_verified=True,
            post_action_method="url_change",
            behavior_failures=1,
            interaction_attempts=3,
            resolution_retries=1,
            behavior_signals=["url_change", "dom_mutation"],
            mutation_total=12,
            mutation_significant=True,
        )

        builder.record_attempt(
            attempt_number=1,
            success=True,
            code='{"actions": []}',
            execution_ms=100,
        )

        step = builder.build("passed")
        attempt = step.attempts[0]

        # Existing fields
        assert attempt.resolution_tier == 2
        assert "overlay_detected" in attempt.guard_failures
        assert attempt.post_action_verified is True

        # BDRE fields
        assert attempt.behavior_failures == 1
        assert attempt.interaction_attempts == 3
        assert attempt.resolution_retries == 1
        assert attempt.behavior_signals == ["url_change", "dom_mutation"]
        assert attempt.mutation_total == 12
        assert attempt.mutation_significant is True

    def test_bdre_defaults_when_not_staged(self):
        """BDRE fields default to zero/empty when not staged."""
        builder = _StepTelemetryBuilder(step_number=1, action="click")

        # Only stage basic stability metrics (no BDRE fields)
        builder.record_stability_metrics(
            resolution_tier=1,
        )

        builder.record_attempt(
            attempt_number=1,
            success=True,
            code='{"actions": []}',
            execution_ms=50,
        )

        step = builder.build("passed")
        attempt = step.attempts[0]

        assert attempt.behavior_failures == 0
        assert attempt.interaction_attempts == 0
        assert attempt.resolution_retries == 0
        assert attempt.behavior_signals == []
        assert attempt.mutation_total == 0
        assert attempt.mutation_significant is False

    def test_bdre_fields_per_attempt(self):
        """Each attempt gets its own BDRE metrics from staging."""
        builder = _StepTelemetryBuilder(step_number=1, action="click")

        # First attempt: high behavior failures
        builder.record_stability_metrics(
            behavior_failures=2,
            interaction_attempts=5,
            behavior_signals=["dom_mutation"],
        )
        builder.record_attempt(1, False, '{}', 100, error_message="timeout")

        # Second attempt: resolved with fewer issues
        builder.record_stability_metrics(
            behavior_failures=0,
            interaction_attempts=1,
            behavior_signals=["url_change"],
            mutation_total=3,
            mutation_significant=True,
        )
        builder.record_attempt(2, True, '{}', 80)

        step = builder.build("passed")
        assert len(step.attempts) == 2

        assert step.attempts[0].behavior_failures == 2
        assert step.attempts[0].interaction_attempts == 5
        assert step.attempts[0].behavior_signals == ["dom_mutation"]
        assert step.attempts[0].mutation_total == 0

        assert step.attempts[1].behavior_failures == 0
        assert step.attempts[1].interaction_attempts == 1
        assert step.attempts[1].behavior_signals == ["url_change"]
        assert step.attempts[1].mutation_total == 3
        assert step.attempts[1].mutation_significant is True


# ---------------------------------------------------------------------------
# execute_intent Stability Metrics Tests
# ---------------------------------------------------------------------------


class TestExecuteIntentBDREMetrics:
    """Verify execute_intent aggregates BDRE signals into stability_metrics."""

    @patch("app.worker.intent_executor._execute_single_action")
    def test_behavior_signals_aggregated(self, mock_exec):
        """Behavior signals from all actions are collected (deduplicated)."""
        from app.worker.intent_executor import execute_intent
        from app.worker.intent_schema import ActionIntent, ActionType, StepIntent, TargetDescriptor

        mock_exec.side_effect = [
            {
                "behavior_effect": BehaviorEffect(
                    detected=True, signals=["url_change", "dom_mutation"],
                ),
                "description": "Clicked Submit",
            },
            {
                "behavior_effect": BehaviorEffect(
                    detected=True, signals=["dom_mutation", "focus_change"],
                ),
                "description": "Filled input",
            },
        ]

        page = MagicMock()
        page.url = "https://example.com"
        intent = StepIntent(
            step_number=1,
            description="Two actions",
            actions=[
                ActionIntent(
                    action=ActionType.CLICK,
                    target=TargetDescriptor(role="button", name="Submit"),
                ),
                ActionIntent(
                    action=ActionType.INPUT,
                    target=TargetDescriptor(label="Email"),
                    value="test@example.com",
                ),
            ],
        )

        _, _, _, metrics, _ = execute_intent(page, intent, wait_budget_ms=30000)

        # Deduplicated: url_change, dom_mutation, focus_change
        assert "url_change" in metrics["behavior_signals"]
        assert "dom_mutation" in metrics["behavior_signals"]
        assert "focus_change" in metrics["behavior_signals"]
        assert len(metrics["behavior_signals"]) == 3

    @patch("app.worker.intent_executor._execute_single_action")
    def test_mutation_data_aggregated(self, mock_exec):
        """Mutation counts and significance aggregated across actions."""
        from app.worker.intent_executor import execute_intent
        from app.worker.intent_schema import ActionIntent, ActionType, StepIntent, TargetDescriptor

        mock_exec.side_effect = [
            {
                "behavior_effect": BehaviorEffect(detected=True, signals=["dom_mutation"]),
                "mutation_summary": MutationSummary(
                    total_mutations=5, has_significant_mutations=False,
                ),
                "description": "Click 1",
            },
            {
                "behavior_effect": BehaviorEffect(detected=True, signals=["url_change"]),
                "mutation_summary": MutationSummary(
                    total_mutations=10, has_significant_mutations=True,
                ),
                "description": "Click 2",
            },
        ]

        page = MagicMock()
        page.url = "https://example.com"
        intent = StepIntent(
            step_number=1,
            description="Two clicks",
            actions=[
                ActionIntent(
                    action=ActionType.CLICK,
                    target=TargetDescriptor(role="button", name="A"),
                ),
                ActionIntent(
                    action=ActionType.CLICK,
                    target=TargetDescriptor(role="button", name="B"),
                ),
            ],
        )

        _, _, _, metrics, _ = execute_intent(page, intent, wait_budget_ms=30000)

        assert metrics["mutation_total"] == 15  # 5 + 10
        assert metrics["mutation_significant"] is True  # any=True

    @patch("app.worker.intent_executor._execute_single_action")
    def test_all_bdre_metrics_present(self, mock_exec):
        """All BDRE fields present in stability_metrics dict."""
        from app.worker.intent_executor import execute_intent
        from app.worker.intent_schema import ActionIntent, ActionType, StepIntent, TargetDescriptor

        mock_exec.return_value = {
            "behavior_effect": BehaviorEffect(detected=True, signals=["dom_mutation"]),
            "description": "Done",
        }

        page = MagicMock()
        page.url = "https://example.com"
        intent = StepIntent(
            step_number=1,
            description="Simple",
            actions=[ActionIntent(
                action=ActionType.CLICK,
                target=TargetDescriptor(role="button", name="OK"),
            )],
        )

        _, _, _, metrics, _ = execute_intent(page, intent, wait_budget_ms=30000)

        # All BDRE fields must be present
        assert "behavior_failures" in metrics
        assert "interaction_attempts" in metrics
        assert "resolution_retries" in metrics
        assert "behavior_signals" in metrics
        assert "mutation_total" in metrics
        assert "mutation_significant" in metrics
