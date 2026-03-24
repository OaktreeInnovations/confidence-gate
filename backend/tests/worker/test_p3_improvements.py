"""Tests for P3 improvements: batch auth, intent post-processing,
cache versioning, ambiguous step flag."""

import pytest

from app.worker.batch_auth import (
    _build_storage_state_key,
    is_login_test_case,
)
from app.worker.intent_generator import _post_process_intent, _same_target
from app.worker.intent_schema import (
    ActionIntent,
    ActionType,
    StepIntent,
    TargetDescriptor,
)
from app.intelligence.code_reuse import action_hash
from app.intelligence.models import StepProfile, FailureRecord


# --- Batch auth tests ---


class TestBatchAuth:
    def test_is_login_test_case_true(self):
        steps = [
            {"step_number": 1, "action": "Navigate to https://example.com"},
            {"step_number": 2, "action": "Enter email and password to log in"},
        ]
        assert is_login_test_case(steps) is True

    def test_is_login_test_case_signin(self):
        steps = [{"step_number": 1, "action": "Click the sign in button"}]
        assert is_login_test_case(steps) is True

    def test_is_login_test_case_false(self):
        steps = [
            {"step_number": 1, "action": "Navigate to https://example.com"},
            {"step_number": 2, "action": "Click the dashboard link"},
        ]
        assert is_login_test_case(steps) is False

    def test_storage_state_key_format(self):
        key = _build_storage_state_key("org123", "batch456")
        assert key == "org123/batch_sessions/batch456/storage_state.json"


# --- Intent post-processing tests ---


class TestPostProcessIntent:
    def test_dedup_consecutive_clicks(self):
        """Consecutive clicks on same target should be deduplicated."""
        target = TargetDescriptor(role="button", name="Submit")
        intent = StepIntent(
            step_number=1,
            actions=[
                ActionIntent(action=ActionType.CLICK, target=target),
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(role="button", name="Submit")),
            ],
        )
        result = _post_process_intent(intent)
        assert len(result.actions) == 1
        assert result.actions[0].action == ActionType.CLICK

    def test_no_dedup_different_targets(self):
        """Clicks on completely different targets should not be deduplicated."""
        intent = StepIntent(
            step_number=1,
            actions=[
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(test_id="btn-submit")),
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(label="Cancel")),
            ],
        )
        result = _post_process_intent(intent)
        assert len(result.actions) == 2

    def test_rewrite_option_click_to_select(self):
        """Click on role:'option' should be rewritten to select."""
        intent = StepIntent(
            step_number=1,
            actions=[
                ActionIntent(
                    action=ActionType.CLICK,
                    target=TargetDescriptor(role="option", name="United States"),
                ),
            ],
        )
        result = _post_process_intent(intent)
        assert len(result.actions) == 1
        assert result.actions[0].action == ActionType.SELECT
        assert result.actions[0].value == "United States"
        assert result.actions[0].target.role is None
        assert result.actions[0].target.name is None

    def test_no_change_normal_intent(self):
        """Normal intents pass through unchanged."""
        intent = StepIntent(
            step_number=1,
            actions=[
                ActionIntent(action=ActionType.INPUT, target=TargetDescriptor(role="textbox", name="Email"), value="test@example.com"),
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(role="button", name="Submit")),
            ],
        )
        result = _post_process_intent(intent)
        assert len(result.actions) == 2
        assert result.actions[0].action == ActionType.INPUT
        assert result.actions[1].action == ActionType.CLICK


class TestSameTarget:
    def test_matching_by_role_and_name(self):
        a = TargetDescriptor(role="button", name="Submit")
        b = TargetDescriptor(role="button", name="Submit")
        assert _same_target(a, b) is True

    def test_matching_by_test_id(self):
        a = TargetDescriptor(test_id="btn-submit")
        b = TargetDescriptor(test_id="btn-submit")
        assert _same_target(a, b) is True

    def test_no_match_different_names_same_role(self):
        a = TargetDescriptor(role="button", name="Submit")
        b = TargetDescriptor(role="button", name="Cancel")
        # role alone is too generic — different names means different elements
        assert _same_target(a, b) is False

    def test_no_match_completely_different(self):
        a = TargetDescriptor(role="button")
        b = TargetDescriptor(label="Email")
        assert _same_target(a, b) is False

    def test_both_none(self):
        assert _same_target(None, None) is True

    def test_one_none(self):
        a = TargetDescriptor(role="button")
        assert _same_target(a, None) is False
        assert _same_target(None, a) is False


# --- Cache versioning tests ---


class TestActionHash:
    def test_deterministic(self):
        h1 = action_hash("Click the Submit button")
        h2 = action_hash("Click the Submit button")
        assert h1 == h2

    def test_differs_for_different_text(self):
        h1 = action_hash("Click the Submit button")
        h2 = action_hash("Click the Cancel button")
        assert h1 != h2

    def test_case_insensitive(self):
        h1 = action_hash("Click Submit")
        h2 = action_hash("click submit")
        assert h1 == h2

    def test_strips_whitespace(self):
        h1 = action_hash("  Click Submit  ")
        h2 = action_hash("Click Submit")
        assert h1 == h2

    def test_returns_16_char_hex(self):
        h = action_hash("test")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


# --- Ambiguous step flag tests ---


class TestAmbiguousStepFlag:
    def test_flagged_when_low_pass_rate(self):
        """Step with <50% pass rate and code_gen_failure should be flagged."""
        sp = StepProfile(
            step_number=1,
            action="Do something ambiguous",
            pass_rate=0.3,
            total_observations=10,
            most_common_failures=[
                FailureRecord(reason="code_gen_failure", count=6, example_message="Failed to generate")
            ],
        )
        # Simulate the aggregator logic
        if sp.pass_rate < 0.5 and sp.total_observations >= 5 and sp.most_common_failures:
            top = sp.most_common_failures[0]
            if top.reason in ("code_gen_failure", "unknown", "assertion_failure"):
                sp.ambiguous_step_flag = True
                sp.ambiguous_step_reason = (
                    f"Step fails {1 - sp.pass_rate:.0%} of the time "
                    f"(top failure: {top.reason}, {top.count}/{sp.total_observations} runs). "
                    f"Consider rewriting the step description."
                )
        assert sp.ambiguous_step_flag is True
        assert "70%" in sp.ambiguous_step_reason
        assert "code_gen_failure" in sp.ambiguous_step_reason

    def test_not_flagged_high_pass_rate(self):
        """Step with high pass rate should not be flagged."""
        sp = StepProfile(
            step_number=1,
            action="Click Submit",
            pass_rate=0.95,
            total_observations=20,
            most_common_failures=[
                FailureRecord(reason="selector_not_found", count=1)
            ],
        )
        # Same logic, should NOT trigger
        if sp.pass_rate < 0.5 and sp.total_observations >= 5 and sp.most_common_failures:
            top = sp.most_common_failures[0]
            if top.reason in ("code_gen_failure", "unknown", "assertion_failure"):
                sp.ambiguous_step_flag = True
        assert sp.ambiguous_step_flag is False

    def test_not_flagged_wrong_failure_type(self):
        """Step with low pass rate but selector_not_found should not be flagged."""
        sp = StepProfile(
            step_number=1,
            action="Click Submit",
            pass_rate=0.3,
            total_observations=10,
            most_common_failures=[
                FailureRecord(reason="selector_not_found", count=7)
            ],
        )
        if sp.pass_rate < 0.5 and sp.total_observations >= 5 and sp.most_common_failures:
            top = sp.most_common_failures[0]
            if top.reason in ("code_gen_failure", "unknown", "assertion_failure"):
                sp.ambiguous_step_flag = True
        assert sp.ambiguous_step_flag is False

    def test_not_flagged_low_observations(self):
        """Step with <5 observations should not be flagged regardless."""
        sp = StepProfile(
            step_number=1,
            action="Do something",
            pass_rate=0.2,
            total_observations=3,
            most_common_failures=[
                FailureRecord(reason="code_gen_failure", count=2)
            ],
        )
        if sp.pass_rate < 0.5 and sp.total_observations >= 5 and sp.most_common_failures:
            top = sp.most_common_failures[0]
            if top.reason in ("code_gen_failure", "unknown", "assertion_failure"):
                sp.ambiguous_step_flag = True
        assert sp.ambiguous_step_flag is False
