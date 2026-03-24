"""Tests for P2 improvements: a11y pruning, feedback-driven regen, adaptive timeouts."""

import pytest

from app.worker.ai_executor import (
    _ACTION_ROLE_PRIORITY,
    _STRUCTURAL_ROLES,
    _format_a11y_node,
    _infer_action_category,
)
from app.intelligence.strategy import StepStrategy, _apply_wait_strategy


# --- _infer_action_category tests ---


class TestInferActionCategory:
    def test_click(self):
        assert _infer_action_category("click Submit") == "click"

    def test_input(self):
        assert _infer_action_category("enter email and password") == "input"

    def test_select(self):
        assert _infer_action_category("select from the dropdown") == "select"

    def test_check(self):
        assert _infer_action_category("check the terms checkbox") == "check"

    def test_navigate(self):
        assert _infer_action_category("navigate to https://example.com") == "navigate"

    def test_none_for_unrecognized(self):
        assert _infer_action_category("verify the page displays results") is None

    def test_case_insensitive(self):
        assert _infer_action_category("CLICK the button") == "click"

    def test_fill_maps_to_input(self):
        assert _infer_action_category("fill in the form") == "input"

    def test_submit_maps_to_click(self):
        assert _infer_action_category("submit the form") == "click"


# --- _format_a11y_node pruning tests ---


class TestFormatA11yNodePruning:
    def _make_node(self, role, name="", children=None):
        node = {"role": role, "name": name}
        if children:
            node["children"] = children
        return node

    def test_no_pruning_includes_all(self):
        """Without priority_roles, all nodes are included."""
        node = self._make_node("heading", "Welcome")
        result = _format_a11y_node(node, depth=0, priority_roles=None)
        assert '[heading] "Welcome"' in result

    def test_pruning_excludes_non_priority_leaf(self):
        """Non-priority leaf nodes are excluded when pruning is active."""
        node = self._make_node("heading", "Welcome")
        priority = {"button", "link"}
        result = _format_a11y_node(node, depth=0, priority_roles=priority)
        assert result == ""

    def test_pruning_keeps_priority_leaf(self):
        """Priority leaf nodes are kept."""
        node = self._make_node("button", "Submit")
        priority = {"button", "link"}
        result = _format_a11y_node(node, depth=0, priority_roles=priority)
        assert '[button] "Submit"' in result

    def test_pruning_keeps_structural_roles(self):
        """Structural container roles are kept even when not in priority set."""
        button = self._make_node("button", "Save")
        form = self._make_node("form", "", children=[button])
        priority = {"button"}
        result = _format_a11y_node(form, depth=0, priority_roles=priority)
        assert "[form]" in result
        assert '[button] "Save"' in result

    def test_pruning_removes_non_priority_leaf_preserves_container(self):
        """Container with mixed children: priority kept, non-priority leaf pruned."""
        heading = self._make_node("heading", "Title")
        button = self._make_node("button", "OK")
        group = self._make_node("group", "", children=[heading, button])
        priority = {"button"}
        result = _format_a11y_node(group, depth=0, priority_roles=priority)
        assert "heading" not in result
        assert '[button] "OK"' in result

    def test_structural_roles_constant(self):
        """Ensure key structural roles are in the set."""
        expected = {"form", "dialog", "navigation", "tree", "list", "menu"}
        assert expected.issubset(_STRUCTURAL_ROLES)

    def test_action_role_priority_mapping(self):
        """Verify key role mappings exist."""
        assert "textbox" in _ACTION_ROLE_PRIORITY["input"]
        assert "button" in _ACTION_ROLE_PRIORITY["click"]
        assert "combobox" in _ACTION_ROLE_PRIORITY["select"]
        assert "checkbox" in _ACTION_ROLE_PRIORITY["check"]
        assert len(_ACTION_ROLE_PRIORITY["navigate"]) == 0


# --- Feedback-driven regeneration tests ---


class TestRegenerateReceivesHints:
    def test_build_user_prompt_includes_hints(self):
        """_build_user_prompt includes selector_hints when provided."""
        from app.worker.intent_generator import _build_user_prompt

        hints = [{"selector": "role:button:Submit", "success_rate": 0.95}]
        prompt = _build_user_prompt(
            action="click Submit",
            step_number=1,
            previous_actions=[],
            selector_hints=hints,
        )
        assert "Submit" in prompt
        assert "95%" in prompt

    def test_build_user_prompt_includes_intelligence(self):
        """_build_user_prompt includes intelligence_context when provided."""
        from app.worker.intent_generator import _build_user_prompt

        prompt = _build_user_prompt(
            action="click Submit",
            step_number=1,
            previous_actions=[],
            intelligence_context="Use label-based targeting for this step.",
        )
        assert "Use label-based targeting" in prompt


# --- Adaptive step timeouts tests ---


class TestAdaptiveStepTimeouts:
    def test_timing_baseline_stored(self):
        """P95 is stored in timing_baseline_ms when available."""
        strategy = StepStrategy(step_number=1)
        timing = {"sample_count": 5, "median_ms": 2000, "p95_ms": 8000}
        _apply_wait_strategy(strategy, step_profile=None, timing_baseline=timing, flake_analysis=None)
        assert strategy.timing_baseline_ms == 8000

    def test_timing_baseline_not_stored_low_sample(self):
        """P95 not stored when sample count is below threshold."""
        strategy = StepStrategy(step_number=1)
        timing = {"sample_count": 1, "median_ms": 2000, "p95_ms": 8000}
        _apply_wait_strategy(strategy, step_profile=None, timing_baseline=timing, flake_analysis=None)
        assert strategy.timing_baseline_ms is None

    def test_has_overrides_with_timing_baseline(self):
        """has_overrides() returns True when only timing_baseline_ms is set."""
        strategy = StepStrategy(step_number=1, timing_baseline_ms=5000)
        assert strategy.has_overrides() is True

    def test_has_overrides_default(self):
        """has_overrides() returns False with all defaults."""
        strategy = StepStrategy(step_number=1)
        assert strategy.has_overrides() is False

    def test_to_dict_includes_timing_baseline(self):
        """to_dict() includes timing_baseline_ms."""
        strategy = StepStrategy(step_number=1, timing_baseline_ms=5000)
        d = strategy.to_dict()
        assert d["timing_baseline_ms"] == 5000

    def test_adaptive_budget_formula(self):
        """Adaptive budget: max(mode_budget, p95*1.5) capped at hard cap."""
        from app.worker.ai_executor import _STEP_HARD_CAP_MS

        # P95 = 8000 → adaptive = 12000 > mode default 15000 → keeps 15000
        mode_budget = 15000
        p95 = 8000
        adaptive = int(p95 * 1.5)
        result = min(max(mode_budget, adaptive), _STEP_HARD_CAP_MS)
        assert result == 15000

        # P95 = 15000 → adaptive = 22500 > mode default 15000 → uses 22500
        p95 = 15000
        adaptive = int(p95 * 1.5)
        result = min(max(mode_budget, adaptive), _STEP_HARD_CAP_MS)
        assert result == 22500

        # P95 = 25000 → adaptive = 37500 > cap 30000 → capped at 30000
        p95 = 25000
        adaptive = int(p95 * 1.5)
        result = min(max(mode_budget, adaptive), _STEP_HARD_CAP_MS)
        assert result == _STEP_HARD_CAP_MS
