"""Tests for Healing Pipeline Orchestrator.

Validates:
- Selector repair heals element not found
- Action repair heals click failure
- AI repair after deterministic fails
- Budget exceeded skips healing
- Deterministic priority over AI
- Telemetry fields populated
- Layer 4 step reconstruction integration
"""

from unittest.mock import MagicMock, patch

from app.worker.healing.action_repair import ActionRepairResult
from app.worker.healing.ai_repair import AIRepairResult
from app.worker.healing.pipeline import (
    HealingResult,
    execute_healing,
    _MIN_HEALING_BUDGET_MS,
)
from app.worker.healing.selector_repair import RepairResult
from app.worker.healing.step_reconstruction import StepReconstructionResult
from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
from app.worker.post_action_verify import ActionVerification
from app.worker.recovery.diagnosis import FailureDiagnosis, FailureType


def _make_target():
    return TargetDescriptor(role="button", name="Submit")


def _make_action(action_type=ActionType.CLICK, value=None):
    return ActionIntent(action=action_type, target=_make_target(), value=value)


def _make_diagnosis(failure_type=FailureType.ELEMENT_NOT_FOUND):
    return FailureDiagnosis(
        failure_type=failure_type,
        root_cause="Element not found",
        is_recoverable=True,
    )


def test_selector_repair_heals_not_found():
    """Element not found → selector repair finds it → healed."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis(FailureType.ELEMENT_NOT_FOUND)
    error = RuntimeError("Element not found")

    mock_locator = MagicMock()
    mock_repair = RepairResult(
        repaired=True,
        locator=mock_locator,
        strategy_used="text_similarity",
        confidence=0.85,
        candidates_evaluated=5,
    )

    with patch("app.worker.healing.pipeline.repair_selector",
               return_value=mock_repair):
        result = execute_healing(page, action, error, diagnosis)

    assert result.healed
    assert result.layer_used == 1
    assert result.strategy_used == "text_similarity"
    assert result.locator is mock_locator


def test_action_repair_heals_click_failure():
    """Element found but click has no effect → action repair succeeds."""
    page = MagicMock()
    locator = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis(FailureType.UNKNOWN)
    error = RuntimeError("Click had no effect")

    mock_action_repair = ActionRepairResult(
        repaired=True,
        method_used="focus_enter",
        attempts=1,
        verification=ActionVerification(
            verified=True, method="url_change", detail="URL changed"),
    )

    with patch("app.worker.healing.pipeline.repair_action",
               return_value=mock_action_repair):
        result = execute_healing(
            page, action, error, diagnosis,
            locator=locator, pre_state={"url": "https://example.com"},
        )

    assert result.healed
    assert result.layer_used == 2
    assert result.strategy_used == "focus_enter"


def test_ai_repair_after_deterministic_fails():
    """Layers 1-2 fail → AI layer succeeds."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis(FailureType.ELEMENT_NOT_FOUND)
    error = RuntimeError("Element not found")
    ai_client = MagicMock()

    mock_locator = MagicMock()
    mock_selector_fail = RepairResult(repaired=False, candidates_evaluated=10)
    mock_ai_success = AIRepairResult(
        repaired=True,
        locator=mock_locator,
        selector="#ai-found",
        confidence=0.8,
        pipeline_score=0.7,
    )

    with patch("app.worker.healing.pipeline.repair_selector",
               return_value=mock_selector_fail), \
         patch("app.worker.healing.pipeline.repair_action",
               return_value=ActionRepairResult(repaired=False, attempts=0)), \
         patch("app.worker.healing.pipeline.ai_structural_repair",
               return_value=mock_ai_success):
        result = execute_healing(
            page, action, error, diagnosis,
            ai_client=ai_client,
        )

    assert result.healed
    assert result.layer_used == 3
    assert result.strategy_used == "ai_structural"


def test_budget_exceeded_skips_healing():
    """< 2s remaining → no healing attempted."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis()
    error = RuntimeError("Element not found")

    result = execute_healing(
        page, action, error, diagnosis,
        budget_remaining_ms=1000,  # Below _MIN_HEALING_BUDGET_MS
    )

    assert not result.healed
    assert result.layer_used == 0
    assert result.elapsed_ms == 0


def test_deterministic_priority_over_ai():
    """Selector repair succeeds → AI never called."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis(FailureType.ELEMENT_NOT_FOUND)
    error = RuntimeError("Element not found")
    ai_client = MagicMock()

    mock_locator = MagicMock()
    mock_selector_success = RepairResult(
        repaired=True,
        locator=mock_locator,
        strategy_used="text_similarity",
        confidence=0.9,
        candidates_evaluated=3,
    )

    with patch("app.worker.healing.pipeline.repair_selector",
               return_value=mock_selector_success) as mock_repair, \
         patch("app.worker.healing.pipeline.ai_structural_repair") as mock_ai:
        result = execute_healing(
            page, action, error, diagnosis,
            ai_client=ai_client,
        )

    assert result.healed
    assert result.layer_used == 1
    # AI was never called
    mock_ai.assert_not_called()


def test_healing_result_telemetry_fields():
    """All telemetry-relevant fields are populated."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis(FailureType.ELEMENT_NOT_FOUND)
    error = RuntimeError("Element not found")

    mock_locator = MagicMock()
    mock_selector = RepairResult(
        repaired=True,
        locator=mock_locator,
        strategy_used="aria_label_similarity",
        confidence=0.88,
        candidates_evaluated=7,
        elapsed_ms=150,
    )

    with patch("app.worker.healing.pipeline.repair_selector",
               return_value=mock_selector):
        result = execute_healing(page, action, error, diagnosis)

    assert result.healed
    assert result.layer_used == 1
    assert result.strategy_used == "aria_label_similarity"
    assert result.attempts_total >= 7
    assert result.elapsed_ms >= 0
    assert result.selector_repair_result is not None
    assert result.selector_repair_result.confidence == 0.88


def test_all_layers_fail():
    """All healing layers fail — returns healed=False."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis(FailureType.ELEMENT_NOT_FOUND)
    error = RuntimeError("Element not found")
    ai_client = MagicMock()

    with patch("app.worker.healing.pipeline.repair_selector",
               return_value=RepairResult(repaired=False, candidates_evaluated=5)), \
         patch("app.worker.healing.pipeline.repair_action",
               return_value=ActionRepairResult(repaired=False, attempts=0)), \
         patch("app.worker.healing.pipeline.ai_structural_repair",
               return_value=AIRepairResult(repaired=False)), \
         patch("app.worker.healing.pipeline.reconstruct_step",
               return_value=StepReconstructionResult(reconstructed=False)):
        result = execute_healing(
            page, action, error, diagnosis,
            ai_client=ai_client,
        )

    assert not result.healed
    assert result.layer_used == 0
    assert result.selector_repair_result is not None
    assert result.ai_repair_result is not None


# --- Layer 4: Step Reconstruction ---


def test_layer4_after_all_layers_fail():
    """Layers 1-3 fail, Layer 4 reconstructs successfully."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis(FailureType.ELEMENT_NOT_FOUND)
    error = RuntimeError("Element not found")
    ai_client = MagicMock()

    recon_actions = [
        ActionIntent(action=ActionType.CLICK,
                     target=TargetDescriptor(role="link", name="Continue")),
        ActionIntent(action=ActionType.CLICK,
                     target=TargetDescriptor(role="button", name="Confirm")),
    ]
    mock_recon = StepReconstructionResult(
        reconstructed=True,
        original_action=action,
        reconstructed_actions=recon_actions,
        confidence=0.85,
        validation_scores=[0.8, 0.75],
    )

    with patch("app.worker.healing.pipeline.repair_selector",
               return_value=RepairResult(repaired=False, candidates_evaluated=5)), \
         patch("app.worker.healing.pipeline.repair_action",
               return_value=ActionRepairResult(repaired=False, attempts=0)), \
         patch("app.worker.healing.pipeline.ai_structural_repair",
               return_value=AIRepairResult(repaired=False)), \
         patch("app.worker.healing.pipeline.reconstruct_step",
               return_value=mock_recon):
        result = execute_healing(
            page, action, error, diagnosis,
            ai_client=ai_client,
        )

    assert result.healed
    assert result.layer_used == 4
    assert result.strategy_used == "step_reconstruction"
    assert result.reconstructed_actions is not None
    assert len(result.reconstructed_actions) == 2


def test_layer4_not_called_when_layer3_succeeds():
    """Layer 3 AI repair succeeds → Layer 4 never invoked."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis(FailureType.ELEMENT_NOT_FOUND)
    error = RuntimeError("Element not found")
    ai_client = MagicMock()

    mock_locator = MagicMock()
    with patch("app.worker.healing.pipeline.repair_selector",
               return_value=RepairResult(repaired=False, candidates_evaluated=3)), \
         patch("app.worker.healing.pipeline.repair_action",
               return_value=ActionRepairResult(repaired=False, attempts=0)), \
         patch("app.worker.healing.pipeline.ai_structural_repair",
               return_value=AIRepairResult(
                   repaired=True, locator=mock_locator,
                   selector="#found", confidence=0.8, pipeline_score=0.7,
               )), \
         patch("app.worker.healing.pipeline.reconstruct_step") as mock_recon:
        result = execute_healing(
            page, action, error, diagnosis,
            ai_client=ai_client,
        )

    assert result.healed
    assert result.layer_used == 3
    mock_recon.assert_not_called()


def test_layer4_provides_reconstructed_actions():
    """Layer 4 populates reconstructed_actions on HealingResult."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis(FailureType.ELEMENT_NOT_FOUND)
    error = RuntimeError("Element not found")
    ai_client = MagicMock()

    expected_actions = [
        ActionIntent(action=ActionType.CLICK,
                     target=TargetDescriptor(role="button", name="Next")),
    ]
    mock_recon = StepReconstructionResult(
        reconstructed=True,
        original_action=action,
        reconstructed_actions=expected_actions,
        confidence=0.9,
        validation_scores=[0.85],
    )

    with patch("app.worker.healing.pipeline.repair_selector",
               return_value=RepairResult(repaired=False, candidates_evaluated=0)), \
         patch("app.worker.healing.pipeline.ai_structural_repair",
               return_value=AIRepairResult(repaired=False)), \
         patch("app.worker.healing.pipeline.reconstruct_step",
               return_value=mock_recon):
        result = execute_healing(
            page, action, error, diagnosis,
            ai_client=ai_client,
        )

    assert result.reconstructed_actions is expected_actions
    assert result.step_reconstruction_result is mock_recon
    assert result.step_reconstruction_result.confidence == 0.9
