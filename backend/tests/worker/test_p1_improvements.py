"""Tests for P1 improvements: step-scoped test data, target overlap, intent consistency guard."""

import pytest

from app.worker.ai_executor import (
    _filter_test_data_for_step,
    _guard_intent_consistency,
    _targets_overlap,
)
from app.worker.intent_schema import ActionIntent, ActionType, StepIntent, TargetDescriptor


# --- _filter_test_data_for_step tests ---


class TestFilterTestDataForStep:
    def test_matching_keys(self):
        result = _filter_test_data_for_step(
            {"email": "a@b.com", "password": "secret", "name": "John"},
            "Enter email and password",
        )
        assert result == {"email": "a@b.com", "password": "secret"}

    def test_no_match_no_data_entry(self):
        result = _filter_test_data_for_step(
            {"email": "a@b.com", "password": "secret"},
            "Click the save button",
        )
        assert result is None

    def test_data_entry_fallback(self):
        """When step implies data entry but no keys match by name, return all."""
        data = {"first_name": "John", "last_name": "Doe", "email": "j@x.com"}
        result = _filter_test_data_for_step(
            data,
            "Fill in the form with user details",
        )
        assert result == data

    def test_underscore_keys(self):
        result = _filter_test_data_for_step(
            {"student_name": "John", "grade_level": "Senior"},
            "Enter student name",
        )
        assert result == {"student_name": "John"}

    def test_none_inputs(self):
        assert _filter_test_data_for_step(None, "any action") is None
        assert _filter_test_data_for_step({}, "any action") == {}

    def test_empty_action(self):
        data = {"email": "test@x.com"}
        assert _filter_test_data_for_step(data, "") == data

    def test_enter_keyword_fallback(self):
        """'enter' is a data entry keyword, should return all keys."""
        data = {"url": "https://example.com", "api_key": "abc"}
        result = _filter_test_data_for_step(
            data,
            "Enter the configuration values",
        )
        assert result == data


# --- _targets_overlap tests ---


class TestTargetsOverlap:
    def test_overlap_by_label(self):
        a = TargetDescriptor(label="Email")
        b = TargetDescriptor(label="email")
        assert _targets_overlap(a, b) is True

    def test_overlap_by_role_name(self):
        a = TargetDescriptor(role="button", name="Submit")
        b = TargetDescriptor(role="button", name="submit")
        assert _targets_overlap(a, b) is True

    def test_overlap_by_test_id(self):
        a = TargetDescriptor(test_id="email-input")
        b = TargetDescriptor(test_id="email-input")
        assert _targets_overlap(a, b) is True

    def test_overlap_by_placeholder(self):
        a = TargetDescriptor(placeholder="you@example.com")
        b = TargetDescriptor(placeholder="you@example.com")
        assert _targets_overlap(a, b) is True

    def test_no_overlap_different_labels(self):
        a = TargetDescriptor(label="Email")
        b = TargetDescriptor(label="Password")
        assert _targets_overlap(a, b) is False

    def test_no_overlap_none_target(self):
        a = TargetDescriptor(label="Email")
        assert _targets_overlap(a, None) is False
        assert _targets_overlap(None, a) is False

    def test_no_overlap_no_matching_fields(self):
        a = TargetDescriptor(role="button", text="Save")
        b = TargetDescriptor(label="Email")
        assert _targets_overlap(a, b) is False


# --- _guard_intent_consistency tests ---


def _make_intent(*actions: ActionIntent) -> StepIntent:
    return StepIntent(step_number=1, actions=list(actions), description="test")


class TestGuardIntentConsistency:
    def test_fixes_conflicting_value(self):
        """Conflicting value on same target replaced with original."""
        original = _make_intent(
            ActionIntent(
                action=ActionType.INPUT,
                target=TargetDescriptor(label="Email"),
                value="correct@email.com",
            ),
            ActionIntent(
                action=ActionType.INPUT,
                target=TargetDescriptor(label="Password"),
                value="secret",
            ),
        )
        regenerated = _make_intent(
            ActionIntent(
                action=ActionType.INPUT,
                target=TargetDescriptor(label="Email"),
                value="WRONG@email.com",  # Conflicts with completed action 0
            ),
            ActionIntent(
                action=ActionType.INPUT,
                target=TargetDescriptor(label="Password"),
                value="newsecret",
            ),
        )

        result = _guard_intent_consistency(regenerated, original, completed_count=1)

        # Email value should be fixed back to the original
        assert result.actions[0].value == "correct@email.com"
        # Password value can differ (action 1 was not completed)
        assert result.actions[1].value == "newsecret"

    def test_no_completed_no_op(self):
        """When completed_count=0, intent returned unchanged."""
        original = _make_intent(
            ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(label="Submit")),
        )
        regenerated = _make_intent(
            ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(label="Save")),
        )

        result = _guard_intent_consistency(regenerated, original, completed_count=0)
        assert result.actions[0].target.label == "Save"

    def test_allows_new_actions(self):
        """New actions not in original are untouched."""
        original = _make_intent(
            ActionIntent(
                action=ActionType.INPUT,
                target=TargetDescriptor(label="Email"),
                value="test@x.com",
            ),
        )
        regenerated = _make_intent(
            ActionIntent(
                action=ActionType.INPUT,
                target=TargetDescriptor(label="Email"),
                value="test@x.com",  # Same as original
            ),
            ActionIntent(
                action=ActionType.CLICK,
                target=TargetDescriptor(role="button", name="Next"),
            ),
        )

        result = _guard_intent_consistency(regenerated, original, completed_count=1)
        assert len(result.actions) == 2
        assert result.actions[1].target.name == "Next"
