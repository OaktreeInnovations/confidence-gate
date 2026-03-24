"""Tests for DOM Mutation Watcher (Part 8).

Validates:
- MutationWatcher correctly parses JS mutation data
- Significant mutation classification
- Integration with intent_executor (watcher override of behavior detection)
"""

from unittest.mock import MagicMock, patch

import pytest

from app.worker.mutation_watcher import (
    MutationSummary,
    _is_significant,
    collect_mutations,
    start_mutation_watcher,
)


# ---------------------------------------------------------------------------
# Unit Tests: start_mutation_watcher
# ---------------------------------------------------------------------------


class TestStartWatcher:
    """Verify watcher installation via JS."""

    def test_start_returns_true_on_success(self):
        page = MagicMock()
        page.evaluate.return_value = None
        assert start_mutation_watcher(page) is True
        page.evaluate.assert_called_once()

    def test_start_returns_false_on_exception(self):
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("Page crashed")
        assert start_mutation_watcher(page) is False


# ---------------------------------------------------------------------------
# Unit Tests: collect_mutations
# ---------------------------------------------------------------------------


class TestCollectMutations:
    """Verify mutation collection and parsing."""

    def test_collect_parses_js_result(self):
        page = MagicMock()
        page.evaluate.return_value = {
            "insertions": 5,
            "removals": 2,
            "attrChanges": 10,
            "textChanges": 3,
            "total": 20,
            "durationMs": 150,
            "tags": ["div", "span", "li"],
            "attrs": ["aria-expanded", "class"],
        }
        summary = collect_mutations(page)
        assert summary.node_insertions == 5
        assert summary.node_removals == 2
        assert summary.attribute_changes == 10
        assert summary.text_changes == 3
        assert summary.total_mutations == 20
        assert summary.mutation_duration_ms == 150
        assert summary.mutated_tags == ["div", "span", "li"]
        assert summary.changed_attributes == ["aria-expanded", "class"]

    def test_collect_returns_empty_on_none_result(self):
        page = MagicMock()
        page.evaluate.return_value = None
        summary = collect_mutations(page)
        assert summary.total_mutations == 0
        assert summary.has_significant_mutations is False

    def test_collect_returns_empty_on_exception(self):
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("Page navigated")
        summary = collect_mutations(page)
        assert summary.total_mutations == 0

    def test_collect_returns_empty_on_non_dict(self):
        page = MagicMock()
        page.evaluate.return_value = True
        summary = collect_mutations(page)
        assert summary.total_mutations == 0

    def test_to_dict_serialization(self):
        summary = MutationSummary(
            node_insertions=3,
            node_removals=1,
            attribute_changes=5,
            total_mutations=9,
            mutated_tags=["div", "button"],
            has_significant_mutations=True,
        )
        d = summary.to_dict()
        assert d["node_insertions"] == 3
        assert d["node_removals"] == 1
        assert d["total_mutations"] == 9
        assert d["has_significant_mutations"] is True
        assert d["mutated_tags"] == ["div", "button"]


# ---------------------------------------------------------------------------
# Unit Tests: _is_significant
# ---------------------------------------------------------------------------


class TestSignificanceClassification:
    """Verify mutation significance logic."""

    def test_many_insertions_significant(self):
        s = MutationSummary(node_insertions=3, total_mutations=3)
        assert _is_significant(s) is True

    def test_many_removals_significant(self):
        s = MutationSummary(node_removals=2, total_mutations=2)
        assert _is_significant(s) is True

    def test_significant_tag_mutation(self):
        s = MutationSummary(
            node_insertions=1, total_mutations=1,
            mutated_tags=["dialog"],
        )
        assert _is_significant(s) is True

    def test_non_cosmetic_attribute_significant(self):
        s = MutationSummary(
            attribute_changes=1, total_mutations=1,
            changed_attributes=["aria-expanded"],
        )
        assert _is_significant(s) is True

    def test_cosmetic_only_not_significant(self):
        s = MutationSummary(
            attribute_changes=1, total_mutations=1,
            changed_attributes=["class"],
        )
        assert _is_significant(s) is False

    def test_high_volume_significant(self):
        s = MutationSummary(
            attribute_changes=9, total_mutations=9,
            changed_attributes=["style"],
            mutated_tags=["div"],
        )
        assert _is_significant(s) is True

    def test_zero_mutations_not_significant(self):
        s = MutationSummary()
        assert _is_significant(s) is False

    def test_input_tag_significant(self):
        s = MutationSummary(
            node_insertions=1, total_mutations=1,
            mutated_tags=["input"],
        )
        assert _is_significant(s) is True

    def test_single_style_change_not_significant(self):
        s = MutationSummary(
            attribute_changes=1, total_mutations=1,
            changed_attributes=["style"],
            mutated_tags=["div"],
        )
        assert _is_significant(s) is False


# ---------------------------------------------------------------------------
# Integration Tests: Mutation Watcher Override in Intent Executor
# ---------------------------------------------------------------------------


class TestMutationWatcherIntegration:
    """Verify mutation watcher supplements behavior detection."""

    @patch("app.worker.intent_executor.collect_mutations")
    @patch("app.worker.intent_executor.start_mutation_watcher")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_watcher_overrides_false_negative_behavior(
        self, mock_snapshot, mock_detect, mock_start, mock_collect,
    ):
        """When behavior detection misses but watcher saw significant
        mutations, behavior_effect is overridden to detected=True."""
        from app.worker.behavior_detection import BehaviorEffect, PageSnapshot

        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = BehaviorEffect(
            detected=False, signals=[], detail="No observable effect",
        )
        mock_start.return_value = True
        mock_collect.return_value = MutationSummary(
            node_insertions=5,
            total_mutations=8,
            mutated_tags=["li", "ul"],
            has_significant_mutations=True,
        )

        from app.worker.intent_executor import _execute_single_action
        from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
        from app.worker.post_action_verify import ActionVerification

        page = MagicMock()
        page.url = "https://example.com"
        action = ActionIntent(
            action=ActionType.CLICK,
            target=TargetDescriptor(role="button", name="Submit"),
        )
        locator = MagicMock()
        locator.count.return_value = 1
        locator.first = locator
        locator.is_visible.return_value = True
        locator.evaluate.return_value = {"found": False, "depth": 0, "tag": "", "role": ""}

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

        # Behavior effect should be overridden by watcher
        assert result["behavior_effect"].detected is True
        assert "mutation_watcher" in result["behavior_effect"].signals

    @patch("app.worker.intent_executor.collect_mutations")
    @patch("app.worker.intent_executor.start_mutation_watcher")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_no_override_when_behavior_already_detected(
        self, mock_snapshot, mock_detect, mock_start, mock_collect,
    ):
        """No override when behavior detection already found an effect."""
        from app.worker.behavior_detection import BehaviorEffect, PageSnapshot

        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = BehaviorEffect(
            detected=True, signals=["url_change"], detail="url_change",
        )
        mock_start.return_value = True
        mock_collect.return_value = MutationSummary(
            node_insertions=5, total_mutations=8,
            has_significant_mutations=True,
        )

        from app.worker.intent_executor import _execute_single_action
        from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
        from app.worker.post_action_verify import ActionVerification

        page = MagicMock()
        page.url = "https://example.com"
        action = ActionIntent(
            action=ActionType.CLICK,
            target=TargetDescriptor(role="button", name="Submit"),
        )
        locator = MagicMock()
        locator.count.return_value = 1
        locator.first = locator
        locator.is_visible.return_value = True
        locator.evaluate.return_value = {"found": False, "depth": 0, "tag": "", "role": ""}

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

        # Original behavior effect should remain (url_change)
        assert result["behavior_effect"].signals == ["url_change"]

    @patch("app.worker.intent_executor.collect_mutations")
    @patch("app.worker.intent_executor.start_mutation_watcher")
    @patch("app.worker.intent_executor.detect_behavior_effect")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_no_override_when_mutations_not_significant(
        self, mock_snapshot, mock_detect, mock_start, mock_collect,
    ):
        """No override when watcher saw only cosmetic mutations."""
        from app.worker.behavior_detection import BehaviorEffect, PageSnapshot

        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_detect.return_value = BehaviorEffect(
            detected=False, signals=[], detail="No observable effect",
        )
        mock_start.return_value = True
        mock_collect.return_value = MutationSummary(
            attribute_changes=1,
            total_mutations=1,
            changed_attributes=["class"],
            has_significant_mutations=False,
        )

        from app.worker.intent_executor import _execute_single_action
        from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
        from app.worker.post_action_verify import ActionVerification

        page = MagicMock()
        page.url = "https://example.com"
        action = ActionIntent(
            action=ActionType.CLICK,
            target=TargetDescriptor(role="button", name="Submit"),
        )
        locator = MagicMock()
        locator.count.return_value = 1
        locator.first = locator
        locator.is_visible.return_value = True
        locator.evaluate.return_value = {"found": False, "depth": 0, "tag": "", "role": ""}

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
            mock_verify.return_value = ActionVerification(verified=False, method="")

            result = _execute_single_action(page, action, timeout_ms=10000)

        # Behavior effect should remain undetected
        assert result["behavior_effect"].detected is False

    @patch("app.worker.intent_executor.collect_mutations")
    @patch("app.worker.intent_executor.start_mutation_watcher")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_feedback_includes_mutation_data(
        self, mock_snapshot, mock_start, mock_collect,
    ):
        """action_feedback includes mutation count and significance."""
        from app.worker.behavior_detection import PageSnapshot

        mock_snapshot.return_value = PageSnapshot(url="https://example.com")
        mock_start.return_value = True
        mock_collect.return_value = MutationSummary(
            node_insertions=3, total_mutations=7,
            has_significant_mutations=True,
        )

        from app.worker.intent_executor import _execute_single_action
        from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
        from app.worker.post_action_verify import ActionVerification

        page = MagicMock()
        page.url = "https://example.com"
        action = ActionIntent(
            action=ActionType.CLICK,
            target=TargetDescriptor(role="button", name="Submit"),
        )
        locator = MagicMock()
        locator.count.return_value = 1
        locator.first = locator
        locator.is_visible.return_value = True
        locator.evaluate.return_value = {"found": False, "depth": 0, "tag": "", "role": ""}

        with patch("app.worker.intent_executor.resolve_target") as mock_resolve, \
             patch("app.worker.intent_executor.ensure_interactable") as mock_guard, \
             patch("app.worker.intent_executor.capture_element_identity", return_value={}), \
             patch("app.worker.intent_executor.revalidate_before_action", return_value=locator), \
             patch("app.worker.intent_executor.discover_clickable_ancestor", return_value=locator), \
             patch("app.worker.intent_executor.safe_click"), \
             patch("app.worker.intent_executor.verify_click") as mock_verify, \
             patch("app.worker.intent_executor.capture_pre_state", return_value={}), \
             patch("app.worker.intent_executor.wait_for_dom_stable"), \
             patch("app.worker.intent_executor.detect_behavior_effect") as mock_detect, \
             patch("app.worker.intent_executor.is_action_already_satisfied", return_value=False):

            from app.worker.behavior_detection import BehaviorEffect
            mock_detect.return_value = BehaviorEffect(
                detected=True, signals=["dom_mutation"], detail="dom_mutation",
            )
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
        assert feedback["mutations_total"] == 7
        assert feedback["mutations_significant"] is True

    @patch("app.worker.intent_executor.start_mutation_watcher")
    @patch("app.worker.intent_executor.capture_page_snapshot")
    def test_watcher_not_started_for_untracked_actions(
        self, mock_snapshot, mock_start,
    ):
        """Mutation watcher skipped for actions without behavior tracking."""
        mock_snapshot.return_value = None  # Not a tracked action

        from app.worker.intent_executor import _execute_single_action
        from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor

        page = MagicMock()
        page.url = "https://example.com"
        action = ActionIntent(
            action=ActionType.ASSERT_VISIBLE,
            target=TargetDescriptor(role="heading", name="Title"),
        )
        locator = MagicMock()
        locator.count.return_value = 1
        locator.first = locator
        locator.is_visible.return_value = True

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
            with patch("playwright.sync_api.expect"):
                try:
                    _execute_single_action(page, action, timeout_ms=10000)
                except Exception:
                    pass

        mock_start.assert_not_called()
