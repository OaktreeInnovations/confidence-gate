"""Tests for intent pre-validation and critical step detection."""

import pytest

from app.worker.ai_executor import _pre_validate_intent
from app.worker.intent_schema import ActionIntent, ActionType, StepIntent, TargetDescriptor


# --- _pre_validate_intent tests ---


def _make_intent(*actions: ActionIntent) -> StepIntent:
    return StepIntent(step_number=1, actions=list(actions), description="test")


def _ctx(a11y: str = "", html: str = "") -> dict:
    return {"a11y_tree": a11y, "interactive_html": html, "screenshot": None}


class TestPreValidateIntent:
    def test_navigate_action_skipped(self):
        """Navigate actions don't need a target on the page."""
        intent = _make_intent(
            ActionIntent(action=ActionType.NAVIGATE, value="https://example.com"),
        )
        result = _pre_validate_intent(intent, _ctx(a11y="[heading] Welcome"))
        assert result is None

    def test_target_found_in_a11y(self):
        """Target name found in accessibility tree passes validation."""
        intent = _make_intent(
            ActionIntent(
                action=ActionType.CLICK,
                target=TargetDescriptor(role="button", name="Submit"),
            ),
        )
        result = _pre_validate_intent(intent, _ctx(a11y='[button] "Submit"'))
        assert result is None

    def test_target_found_in_html(self):
        """Target label found in interactive HTML passes validation."""
        intent = _make_intent(
            ActionIntent(
                action=ActionType.INPUT,
                target=TargetDescriptor(label="Email"),
                value="test@example.com",
            ),
        )
        result = _pre_validate_intent(
            intent, _ctx(html='<input aria-label="Email" placeholder="you@example.com">')
        )
        assert result is None

    def test_target_missing(self):
        """Target not found in page context returns feedback string."""
        intent = _make_intent(
            ActionIntent(
                action=ActionType.CLICK,
                target=TargetDescriptor(role="button", name="NonexistentButton"),
            ),
        )
        result = _pre_validate_intent(intent, _ctx(a11y='[button] "Save"'))
        assert result is not None
        assert "NonexistentButton" in result
        assert "Pre-validation failed" in result

    def test_multiple_actions_partial_miss(self):
        """Only missing targets are reported."""
        intent = _make_intent(
            ActionIntent(
                action=ActionType.INPUT,
                target=TargetDescriptor(label="Email"),
                value="test@example.com",
            ),
            ActionIntent(
                action=ActionType.INPUT,
                target=TargetDescriptor(label="Password"),
                value="secret",
            ),
        )
        result = _pre_validate_intent(
            intent, _ctx(a11y='[textbox] "Email"')
        )
        assert result is not None
        assert "Password" in result
        assert "Email" not in result  # Email is found, not reported

    def test_no_page_context(self):
        """No page context skips validation."""
        intent = _make_intent(
            ActionIntent(
                action=ActionType.CLICK,
                target=TargetDescriptor(name="Anything"),
            ),
        )
        assert _pre_validate_intent(intent, None) is None

    def test_empty_target_skipped(self):
        """Actions with empty targets are skipped."""
        intent = _make_intent(
            ActionIntent(
                action=ActionType.WAIT_FOR_TEXT,
                value="Loading...",
            ),
        )
        result = _pre_validate_intent(intent, _ctx(a11y="[heading] Page"))
        assert result is None

    def test_case_insensitive_match(self):
        """Matching is case-insensitive."""
        intent = _make_intent(
            ActionIntent(
                action=ActionType.CLICK,
                target=TargetDescriptor(name="SUBMIT"),
            ),
        )
        result = _pre_validate_intent(intent, _ctx(a11y='[button] "submit"'))
        assert result is None


# --- _is_critical_step tests ---


from app.worker.tasks.execute_test_run import _is_critical_step


class TestIsCriticalStep:
    def test_login_step_0(self):
        assert _is_critical_step("enter email and password then click log in", 1) is True

    def test_login_step_4_not_critical(self):
        assert _is_critical_step("login to the system", 4) is False

    def test_navigate_step_0(self):
        assert _is_critical_step("navigate to https://example.com", 0) is True

    def test_navigate_step_1(self):
        assert _is_critical_step("go to the login page", 1) is True

    def test_navigate_step_2_not_critical(self):
        assert _is_critical_step("navigate to settings page", 2) is False

    def test_regular_step(self):
        assert _is_critical_step("fill in the form", 1) is False

    def test_signin_keyword(self):
        assert _is_critical_step("sign in with your credentials", 2) is True
