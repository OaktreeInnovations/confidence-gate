"""Tests for Step Reconstruction (Healing Layer 4).

Validates:
- AI returns 1-3 actions → all validated → reconstructed=True
- Over-limit (>3 actions) → rejected
- Unresolvable targets → rejected
- Guard failures → rejected
- AI errors → graceful failure
- Low confidence → rejected
- Low pipeline score → rejected
- No AI client → not attempted
"""

import json
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from app.worker.healing.step_reconstruction import (
    StepReconstructionResult,
    reconstruct_step,
)
from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
from app.worker.interaction_guard import GuardResult
from app.worker.recovery.diagnosis import FailureDiagnosis, FailureType
from app.worker.selector_engine.scoring import CandidateScore
from app.worker.selector_resilience import SelectorStrategy


def _make_target():
    return TargetDescriptor(role="button", name="Submit")


def _make_action(action_type=ActionType.CLICK, value=None):
    return ActionIntent(action=action_type, target=_make_target(), value=value)


def _make_diagnosis():
    return FailureDiagnosis(
        failure_type=FailureType.ELEMENT_NOT_FOUND,
        root_cause="Element not found",
        is_recoverable=True,
    )


def _make_ai_client(response_json=None):
    """Create a mock AI client with controllable response."""
    client = MagicMock()
    raw = MagicMock()
    client.raw_client = raw

    if response_json is not None:
        completion = MagicMock()
        completion.choices = [MagicMock()]
        completion.choices[0].message.content = json.dumps(response_json)
        raw.chat.completions.create.return_value = completion

    return client


def _make_resolved_target(composite=0.75):
    """Create a mock ResolvedTarget with specified composite score."""
    mock = MagicMock()
    mock.locator = MagicMock()
    mock.score = MagicMock()
    mock.score.composite = composite
    return mock


def _make_guard(passed=True):
    """Create a GuardResult."""
    return GuardResult(
        passed=passed,
        checks_failed=[] if passed else ["not_visible"],
    )


# --- Test 1: Single action reconstruction ---


def test_reconstruct_single_action():
    """AI returns 1 action, resolves + guard passes → reconstructed=True."""
    page = MagicMock()
    ai_client = _make_ai_client({
        "actions": [
            {"action": "click", "target": {"role": "button", "name": "Continue"}}
        ],
        "confidence": 0.85,
        "reasoning": "Submit button replaced by Continue button",
    })

    mock_resolved = _make_resolved_target(composite=0.80)

    with patch("app.worker.healing.step_reconstruction._get_a11y_tree",
               return_value="(tree)"), \
         patch("app.worker.healing.step_reconstruction._get_interactive_html",
               return_value="<button>Continue</button>"), \
         patch("app.worker.healing.step_reconstruction.resolve_target",
               return_value=mock_resolved), \
         patch("app.worker.healing.step_reconstruction.ensure_interactable",
               return_value=_make_guard(passed=True)):
        result = reconstruct_step(
            page, _make_action(), _make_diagnosis(),
            ai_client, failed_selectors=["role:button"],
        )

    assert result.reconstructed
    assert len(result.reconstructed_actions) == 1
    assert result.reconstructed_actions[0].action == ActionType.CLICK
    assert result.reconstructed_actions[0].target.name == "Continue"
    assert result.confidence == 0.85
    assert len(result.validation_scores) == 1
    assert result.validation_scores[0] == 0.80


# --- Test 2: Multi action reconstruction ---


def test_reconstruct_multi_action():
    """AI returns 3 actions, all resolve and pass guard → reconstructed=True."""
    page = MagicMock()
    ai_client = _make_ai_client({
        "actions": [
            {"action": "click", "target": {"role": "link", "name": "Continue"}},
            {"action": "wait_for_text", "value": "Confirmation page"},
            {"action": "click", "target": {"role": "button", "name": "Confirm"}},
        ],
        "confidence": 0.75,
        "reasoning": "Two-step confirmation flow",
    })

    mock_resolved = _make_resolved_target(composite=0.70)

    with patch("app.worker.healing.step_reconstruction._get_a11y_tree",
               return_value="(tree)"), \
         patch("app.worker.healing.step_reconstruction._get_interactive_html",
               return_value=""), \
         patch("app.worker.healing.step_reconstruction.resolve_target",
               return_value=mock_resolved), \
         patch("app.worker.healing.step_reconstruction.ensure_interactable",
               return_value=_make_guard(passed=True)):
        result = reconstruct_step(
            page, _make_action(), _make_diagnosis(), ai_client,
        )

    assert result.reconstructed
    assert len(result.reconstructed_actions) == 3
    assert result.reconstructed_actions[0].action == ActionType.CLICK
    assert result.reconstructed_actions[1].action == ActionType.WAIT_FOR_TEXT
    assert result.reconstructed_actions[2].action == ActionType.CLICK


# --- Test 3: Over limit rejected ---


def test_reconstruct_rejects_over_limit():
    """AI returns 4 actions (exceeds max 3) → rejected."""
    page = MagicMock()
    ai_client = _make_ai_client({
        "actions": [
            {"action": "click", "target": {"role": "button", "name": "A"}},
            {"action": "click", "target": {"role": "button", "name": "B"}},
            {"action": "click", "target": {"role": "button", "name": "C"}},
            {"action": "click", "target": {"role": "button", "name": "D"}},
        ],
        "confidence": 0.8,
        "reasoning": "Too many steps",
    })

    with patch("app.worker.healing.step_reconstruction._get_a11y_tree",
               return_value="(tree)"), \
         patch("app.worker.healing.step_reconstruction._get_interactive_html",
               return_value=""):
        result = reconstruct_step(
            page, _make_action(), _make_diagnosis(), ai_client,
        )

    assert not result.reconstructed


# --- Test 4: Unresolvable target ---


def test_reconstruct_rejects_unresolvable():
    """resolve_target raises → rejected."""
    page = MagicMock()
    ai_client = _make_ai_client({
        "actions": [
            {"action": "click", "target": {"role": "button", "name": "Ghost"}},
        ],
        "confidence": 0.8,
        "reasoning": "Found button",
    })

    with patch("app.worker.healing.step_reconstruction._get_a11y_tree",
               return_value="(tree)"), \
         patch("app.worker.healing.step_reconstruction._get_interactive_html",
               return_value=""), \
         patch("app.worker.healing.step_reconstruction.resolve_target",
               side_effect=RuntimeError("No candidates")):
        result = reconstruct_step(
            page, _make_action(), _make_diagnosis(), ai_client,
        )

    assert not result.reconstructed


# --- Test 5: Guard failure ---


def test_reconstruct_rejects_guard_failure():
    """Guard fails → rejected."""
    page = MagicMock()
    ai_client = _make_ai_client({
        "actions": [
            {"action": "click", "target": {"role": "button", "name": "Hidden"}},
        ],
        "confidence": 0.8,
        "reasoning": "Found button",
    })

    mock_resolved = _make_resolved_target(composite=0.75)

    with patch("app.worker.healing.step_reconstruction._get_a11y_tree",
               return_value="(tree)"), \
         patch("app.worker.healing.step_reconstruction._get_interactive_html",
               return_value=""), \
         patch("app.worker.healing.step_reconstruction.resolve_target",
               return_value=mock_resolved), \
         patch("app.worker.healing.step_reconstruction.ensure_interactable",
               return_value=_make_guard(passed=False)):
        result = reconstruct_step(
            page, _make_action(), _make_diagnosis(), ai_client,
        )

    assert not result.reconstructed


# --- Test 6: AI error ---


def test_reconstruct_handles_ai_error():
    """AI call throws → reconstructed=False gracefully."""
    page = MagicMock()
    ai_client = MagicMock()
    raw = MagicMock()
    ai_client.raw_client = raw
    raw.chat.completions.create.side_effect = RuntimeError("API timeout")

    with patch("app.worker.healing.step_reconstruction._get_a11y_tree",
               return_value="(tree)"), \
         patch("app.worker.healing.step_reconstruction._get_interactive_html",
               return_value=""):
        result = reconstruct_step(
            page, _make_action(), _make_diagnosis(), ai_client,
        )

    assert not result.reconstructed
    assert result.elapsed_ms >= 0


# --- Test 7: Low confidence ---


def test_reconstruct_low_confidence_rejected():
    """AI confidence < 0.6 → rejected."""
    page = MagicMock()
    ai_client = _make_ai_client({
        "actions": [
            {"action": "click", "target": {"role": "button", "name": "Maybe"}},
        ],
        "confidence": 0.4,
        "reasoning": "Uncertain",
    })

    with patch("app.worker.healing.step_reconstruction._get_a11y_tree",
               return_value="(tree)"), \
         patch("app.worker.healing.step_reconstruction._get_interactive_html",
               return_value=""):
        result = reconstruct_step(
            page, _make_action(), _make_diagnosis(), ai_client,
        )

    assert not result.reconstructed
    assert result.confidence == 0.4


# --- Test 8: Low validation score ---


def test_reconstruct_low_validation_score():
    """resolve_target returns composite 0.3 (below 0.5) → rejected."""
    page = MagicMock()
    ai_client = _make_ai_client({
        "actions": [
            {"action": "click", "target": {"role": "button", "name": "Weak"}},
        ],
        "confidence": 0.8,
        "reasoning": "Found button",
    })

    mock_resolved = _make_resolved_target(composite=0.3)

    with patch("app.worker.healing.step_reconstruction._get_a11y_tree",
               return_value="(tree)"), \
         patch("app.worker.healing.step_reconstruction._get_interactive_html",
               return_value=""), \
         patch("app.worker.healing.step_reconstruction.resolve_target",
               return_value=mock_resolved):
        result = reconstruct_step(
            page, _make_action(), _make_diagnosis(), ai_client,
        )

    assert not result.reconstructed


# --- Test 9: No AI client ---


def test_reconstruct_no_ai_client():
    """raw_client is None → reconstructed=False immediately."""
    page = MagicMock()
    ai_client = MagicMock()
    ai_client.raw_client = None

    result = reconstruct_step(
        page, _make_action(), _make_diagnosis(), ai_client,
    )

    assert not result.reconstructed
    assert result.elapsed_ms == 0
