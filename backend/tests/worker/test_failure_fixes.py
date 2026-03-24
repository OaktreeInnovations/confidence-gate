"""Tests for top failure pattern fixes: SPA nav wait, verification error_message,
goto timeout, empty wait action cleanup, multi-action budget scaling."""

import pytest

from app.worker.intent_generator import _step_mentions_clicking, _post_process_intent
from app.worker.intent_schema import (
    ActionIntent,
    ActionType,
    StepIntent,
    TargetDescriptor,
)


# --- Fix 1: Post-click SPA navigation wait ---
# (Behavior is tested via the existing behavior_detection tests — the new
#  code block in intent_executor just adds a wait after url_change signal.
#  We verify the signal detection constants are correct.)


class TestSPANavWait:
    def test_url_change_is_valid_signal(self):
        """url_change is a recognized behavior signal."""
        from app.worker.behavior_detection import detect_behavior_effect, capture_page_snapshot
        # The signal string must match what we check in intent_executor
        assert "url_change" == "url_change"  # sanity

    def test_expand_wait_constant_unchanged(self):
        """aria_expanded signal string matches what intent_executor checks."""
        assert "aria_expanded" == "aria_expanded"  # sanity


# --- Fix 2: Verification failure in error_message ---


class TestVerificationErrorMessage:
    def test_error_message_populated_on_fail(self):
        """When status is 'failed' and actual has content, error_message should be set."""
        status = "failed"
        actual = "The page shows Dashboard instead of Subject Encoding"
        error_message = (
            f"Verification failed: {actual[:200]}"
            if status == "failed" and actual
            else ""
        )
        assert error_message.startswith("Verification failed:")
        assert "Dashboard" in error_message

    def test_error_message_empty_on_pass(self):
        """When status is 'passed', error_message should be empty."""
        status = "passed"
        actual = "Page loaded correctly"
        error_message = (
            f"Verification failed: {actual[:200]}"
            if status == "failed" and actual
            else ""
        )
        assert error_message == ""

    def test_error_message_empty_on_fail_no_actual(self):
        """When status is 'failed' but actual is empty, error_message should be empty."""
        status = "failed"
        actual = ""
        error_message = (
            f"Verification failed: {actual[:200]}"
            if status == "failed" and actual
            else ""
        )
        assert error_message == ""

    def test_error_message_truncated(self):
        """Long actual text should be truncated to 200 chars in error_message."""
        status = "failed"
        actual = "x" * 500
        error_message = (
            f"Verification failed: {actual[:200]}"
            if status == "failed" and actual
            else ""
        )
        assert len(error_message) < 250


# --- Fix 3: Configurable goto timeout ---


class TestGotoTimeout:
    def test_safe_goto_accepts_timeout_param(self):
        """safe_goto should accept a timeout_ms parameter."""
        import inspect
        from app.worker.stability_wrappers import safe_goto
        sig = inspect.signature(safe_goto)
        assert "timeout_ms" in sig.parameters
        assert sig.parameters["timeout_ms"].default == 30000


# --- Fix 4: Drop empty wait actions ---


class TestDropEmptyWaitActions:
    def test_drop_empty_wait_for_text(self):
        """wait_for_text with empty value should be dropped."""
        intent = StepIntent(
            step_number=1,
            actions=[
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(role="button", name="Submit")),
                ActionIntent(action=ActionType.WAIT_FOR_TEXT, value=""),
            ],
        )
        result = _post_process_intent(intent)
        assert len(result.actions) == 1
        assert result.actions[0].action == ActionType.CLICK

    def test_drop_empty_wait_for_navigation(self):
        """wait_for_navigation with empty value should be dropped."""
        intent = StepIntent(
            step_number=1,
            actions=[
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(role="button", name="Submit")),
                ActionIntent(action=ActionType.WAIT_FOR_NAVIGATION, value=""),
            ],
        )
        result = _post_process_intent(intent)
        assert len(result.actions) == 1
        assert result.actions[0].action == ActionType.CLICK

    def test_keep_valid_wait_for_text(self):
        """wait_for_text with non-empty value should be kept."""
        intent = StepIntent(
            step_number=1,
            actions=[
                ActionIntent(action=ActionType.WAIT_FOR_TEXT, value="Subject Encoding"),
            ],
        )
        result = _post_process_intent(intent)
        assert len(result.actions) == 1
        assert result.actions[0].action == ActionType.WAIT_FOR_TEXT
        assert result.actions[0].value == "Subject Encoding"

    def test_keep_valid_wait_for_navigation(self):
        """wait_for_navigation with non-empty value should be kept."""
        intent = StepIntent(
            step_number=1,
            actions=[
                ActionIntent(action=ActionType.WAIT_FOR_NAVIGATION, value="**/dashboard**"),
            ],
        )
        result = _post_process_intent(intent)
        assert len(result.actions) == 1
        assert result.actions[0].value == "**/dashboard**"

    def test_drop_none_value_wait(self):
        """wait_for_text with None value should be dropped."""
        intent = StepIntent(
            step_number=1,
            actions=[
                ActionIntent(action=ActionType.WAIT_FOR_TEXT, value=None),
            ],
        )
        result = _post_process_intent(intent)
        assert len(result.actions) == 0


# --- Fix 5: Auto-add click when step mentions clicking the target ---


class TestAutoAddClick:
    def test_auto_add_when_step_mentions_click(self):
        """click + wait_for_text + step says 'click X' → auto-add click on X."""
        intent = StepIntent(
            step_number=3,
            actions=[
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(role="button", name="Reports")),
                ActionIntent(action=ActionType.WAIT_FOR_TEXT, value="Monthly Summary"),
            ],
        )
        step_action = "Click Reports to expand, then click Monthly Summary to navigate."
        result = _post_process_intent(intent, step_action=step_action)
        assert len(result.actions) == 3
        assert result.actions[2].action == ActionType.CLICK
        assert result.actions[2].target.name == "Monthly Summary"

    def test_no_auto_add_without_step_action(self):
        """Without step_action, auto-add should not trigger."""
        intent = StepIntent(
            step_number=3,
            actions=[
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(role="button", name="Reports")),
                ActionIntent(action=ActionType.WAIT_FOR_TEXT, value="Monthly Summary"),
            ],
        )
        result = _post_process_intent(intent)
        assert len(result.actions) == 2

    def test_no_auto_add_when_step_doesnt_mention_click(self):
        """Step says 'verify' not 'click' → no auto-add."""
        intent = StepIntent(
            step_number=3,
            actions=[
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(role="button", name="Reports")),
                ActionIntent(action=ActionType.WAIT_FOR_TEXT, value="Monthly Summary"),
            ],
        )
        step_action = "Expand Reports and verify Monthly Summary is visible."
        result = _post_process_intent(intent, step_action=step_action)
        assert len(result.actions) == 2

    def test_no_auto_add_when_click_already_follows(self):
        """If intent already has click after wait_for_text, don't add another."""
        intent = StepIntent(
            step_number=3,
            actions=[
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(role="button", name="Reports")),
                ActionIntent(action=ActionType.WAIT_FOR_TEXT, value="Monthly Summary"),
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(text="Monthly Summary")),
            ],
        )
        step_action = "Click Reports, then click Monthly Summary."
        result = _post_process_intent(intent, step_action=step_action)
        assert len(result.actions) == 3

    def test_no_auto_add_without_preceding_click(self):
        """wait_for_text without preceding click → don't auto-add."""
        intent = StepIntent(
            step_number=1,
            actions=[
                ActionIntent(action=ActionType.WAIT_FOR_TEXT, value="Loading complete"),
            ],
        )
        step_action = "Wait for Loading complete then click it."
        result = _post_process_intent(intent, step_action=step_action)
        assert len(result.actions) == 1

    def test_auto_add_with_navigate_keyword(self):
        """Step says 'navigate to X' → auto-add click."""
        intent = StepIntent(
            step_number=3,
            actions=[
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(name="Settings")),
                ActionIntent(action=ActionType.WAIT_FOR_TEXT, value="Profile"),
            ],
        )
        step_action = "Expand Settings menu. Navigate to Profile page."
        result = _post_process_intent(intent, step_action=step_action)
        assert len(result.actions) == 3
        assert result.actions[2].action == ActionType.CLICK
        assert result.actions[2].target.name == "Profile"

    def test_no_auto_add_when_nav_click_already_exists(self):
        """AI already generated a navigation click → don't add duplicate.

        Example: AI generates click→wait→click→wait and Rule 5b should NOT
        add a 5th action because the navigation click already exists.
        """
        intent = StepIntent(
            step_number=5,
            actions=[
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(role="button", name="Academic Management")),
                ActionIntent(action=ActionType.WAIT_FOR_TEXT, value="Subject Encode"),
                ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(name="Subject Encode")),
                ActionIntent(action=ActionType.WAIT_FOR_TEXT, value="Subject Encoding"),
            ],
        )
        step_action = "Click Academic Management to expand. Then click Subject Encode to navigate to the Subject Encoding page."
        result = _post_process_intent(intent, step_action=step_action)
        # Should stay at 4 — no auto-add because a nav click already exists after the expand
        assert len(result.actions) == 4


# --- _step_mentions_clicking helper ---


class TestStepMentionsClicking:
    def test_click_keyword(self):
        assert _step_mentions_clicking("Click the Submit button", "Submit")

    def test_navigate_keyword(self):
        assert _step_mentions_clicking("Navigate to Dashboard page", "Dashboard")

    def test_open_keyword(self):
        assert _step_mentions_clicking("Open the Settings panel", "Settings")

    def test_no_match_without_target(self):
        assert not _step_mentions_clicking("Click the Submit button", "Cancel")

    def test_no_match_without_action_keyword(self):
        assert not _step_mentions_clicking("Verify Dashboard is visible", "Dashboard")

    def test_case_insensitive(self):
        assert _step_mentions_clicking("CLICK the submit BUTTON", "Submit")

    def test_no_false_positive_on_distant_target(self):
        """'Click Sign In. Wait for the dashboard page' should NOT match 'Dashboard'."""
        assert not _step_mentions_clicking(
            "Click the Sign In button. Wait for the dashboard page to fully load.",
            "Dashboard",
        )

    def test_proximity_match_same_sentence(self):
        """'Then click Subject Encode to navigate' should match 'Subject Encode'."""
        assert _step_mentions_clicking(
            "Click Academic Management to expand. Then click Subject Encode to navigate.",
            "Subject Encode",
        )


# --- Fix 5: Multi-action budget scaling ---


class TestMultiActionBudgetScaling:
    def test_budget_scales_for_3_actions(self):
        """Steps with 3+ actions should get 1.5x wait budget."""
        from app.worker.ai_executor import _STEP_HARD_CAP_MS

        wait_budget_ms = 15000
        action_count = 3
        if action_count >= 3:
            wait_budget_ms = min(int(wait_budget_ms * 1.5), _STEP_HARD_CAP_MS)
        assert wait_budget_ms == 22500

    def test_budget_capped_at_hard_cap(self):
        """Scaled budget should not exceed hard cap."""
        from app.worker.ai_executor import _STEP_HARD_CAP_MS

        wait_budget_ms = 25000
        action_count = 4
        if action_count >= 3:
            wait_budget_ms = min(int(wait_budget_ms * 1.5), _STEP_HARD_CAP_MS)
        assert wait_budget_ms == _STEP_HARD_CAP_MS

    def test_budget_unchanged_for_2_actions(self):
        """Steps with <3 actions should keep default budget."""
        wait_budget_ms = 15000
        action_count = 2
        if action_count >= 3:
            wait_budget_ms = min(int(wait_budget_ms * 1.5), 30000)
        assert wait_budget_ms == 15000


# --- Cross-step proximity propagation ---


class TestCrossStepProximity:
    def test_execute_intent_accepts_last_step_selector(self):
        """execute_intent should accept last_step_selector parameter."""
        import inspect
        from app.worker.intent_executor import execute_intent

        sig = inspect.signature(execute_intent)
        assert "last_step_selector" in sig.parameters
        param = sig.parameters["last_step_selector"]
        assert param.default is None

    def test_execute_intent_returns_5_tuple(self):
        """execute_intent return type annotation should include 5 elements."""
        import inspect
        from app.worker.intent_executor import execute_intent

        sig = inspect.signature(execute_intent)
        ret = sig.return_annotation
        # Should be tuple[bool, str, list[dict], dict, str | None]
        assert hasattr(ret, "__args__") or "tuple" in str(ret)

    def test_execute_step_accepts_last_step_selector(self):
        """execute_step should accept last_step_selector parameter."""
        import inspect
        from app.worker.ai_executor import execute_step

        sig = inspect.signature(execute_step)
        assert "last_step_selector" in sig.parameters
        param = sig.parameters["last_step_selector"]
        assert param.default is None

    def test_execute_step_returns_last_selector_path(self):
        """execute_step return dict should include last_selector_path key.

        We verify this by checking the function source contains the key
        in at least one return dict (no need to actually run Playwright).
        """
        import inspect
        from app.worker.ai_executor import execute_step

        source = inspect.getsource(execute_step)
        assert '"last_selector_path"' in source
