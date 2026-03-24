"""Hard rules tests for the healing system.

Validates:
- AI repair goes through scoring pipeline
- AI repair goes through interaction guard
- Deterministic layers run before AI
- Healing respects budget
"""

from unittest.mock import MagicMock, patch, call

from app.worker.healing.ai_repair import AIRepairResult, ai_structural_repair
from app.worker.healing.pipeline import execute_healing
from app.worker.healing.selector_repair import RepairResult
from app.worker.healing.action_repair import ActionRepairResult
from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
from app.worker.interaction_guard import GuardResult
from app.worker.recovery.diagnosis import FailureDiagnosis, FailureType
from app.worker.selector_engine.scoring import CandidateScore
from app.worker.selector_resilience import SelectorStrategy


def _make_target():
    return TargetDescriptor(role="button", name="Submit")


def _make_action():
    return ActionIntent(action=ActionType.CLICK, target=_make_target())


def _make_diagnosis(ft=FailureType.ELEMENT_NOT_FOUND):
    return FailureDiagnosis(
        failure_type=ft,
        root_cause="Element not found",
        is_recoverable=True,
    )


def test_ai_repair_goes_through_scoring():
    """AI repair MUST call score_candidates — hard rule enforcement."""
    import json

    page = MagicMock()
    page.accessibility.snapshot.return_value = {"role": "WebArea"}
    page.evaluate.return_value = ""

    ai_client = MagicMock()
    raw = MagicMock()
    ai_client.raw_client = raw

    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = json.dumps({
        "selector": "#btn", "confidence": 0.9,
        "reasoning": "test", "action_override": "",
    })
    raw.chat.completions.create.return_value = completion

    mock_locator = MagicMock()
    page.locator.return_value = mock_locator

    mock_candidate = CandidateScore(
        locator=mock_locator, strategy=SelectorStrategy.CSS,
        params={"selector": "#btn"}, composite=0.8,
    )

    with patch("app.worker.healing.ai_repair.score_candidates",
               return_value=[mock_candidate]) as mock_score, \
         patch("app.worker.healing.ai_repair.ensure_interactable",
               return_value=GuardResult(passed=True)):
        result = ai_structural_repair(
            page, _make_target(), _make_action(), _make_diagnosis(),
            ai_client,
        )

    # score_candidates MUST have been called
    mock_score.assert_called_once()
    assert result.repaired


def test_ai_repair_goes_through_guard():
    """AI repair MUST call ensure_interactable — hard rule enforcement."""
    import json

    page = MagicMock()
    page.accessibility.snapshot.return_value = {"role": "WebArea"}
    page.evaluate.return_value = ""

    ai_client = MagicMock()
    raw = MagicMock()
    ai_client.raw_client = raw

    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = json.dumps({
        "selector": "#btn", "confidence": 0.9,
        "reasoning": "test", "action_override": "",
    })
    raw.chat.completions.create.return_value = completion

    mock_locator = MagicMock()
    page.locator.return_value = mock_locator

    mock_candidate = CandidateScore(
        locator=mock_locator, strategy=SelectorStrategy.CSS,
        params={"selector": "#btn"}, composite=0.8,
    )

    with patch("app.worker.healing.ai_repair.score_candidates",
               return_value=[mock_candidate]), \
         patch("app.worker.healing.ai_repair.ensure_interactable",
               return_value=GuardResult(passed=True)) as mock_guard:
        result = ai_structural_repair(
            page, _make_target(), _make_action(), _make_diagnosis(),
            ai_client,
        )

    # ensure_interactable MUST have been called
    mock_guard.assert_called_once()
    assert result.repaired


def test_deterministic_before_ai():
    """Healing pipeline tries selector repair (deterministic) before AI."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis(FailureType.ELEMENT_NOT_FOUND)
    error = RuntimeError("Element not found")
    ai_client = MagicMock()

    call_order = []

    def mock_selector_repair(*args, **kwargs):
        call_order.append("selector_repair")
        return RepairResult(repaired=False, candidates_evaluated=5)

    def mock_action_repair(*args, **kwargs):
        call_order.append("action_repair")
        return ActionRepairResult(repaired=False, attempts=0)

    def mock_ai_repair(*args, **kwargs):
        call_order.append("ai_repair")
        return AIRepairResult(repaired=False)

    with patch("app.worker.healing.pipeline.repair_selector",
               side_effect=mock_selector_repair), \
         patch("app.worker.healing.pipeline.repair_action",
               side_effect=mock_action_repair), \
         patch("app.worker.healing.pipeline.ai_structural_repair",
               side_effect=mock_ai_repair):
        execute_healing(
            page, action, error, diagnosis,
            ai_client=ai_client,
        )

    # Deterministic layers MUST run before AI
    assert call_order.index("selector_repair") < call_order.index("ai_repair")


def test_healing_respects_budget():
    """Healing with < 2s budget doesn't attempt any repair."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis()
    error = RuntimeError("Element not found")

    with patch("app.worker.healing.pipeline.repair_selector") as mock_selector, \
         patch("app.worker.healing.pipeline.ai_structural_repair") as mock_ai:
        result = execute_healing(
            page, action, error, diagnosis,
            budget_remaining_ms=500,
        )

    assert not result.healed
    # Neither repair function should have been called
    mock_selector.assert_not_called()
    mock_ai.assert_not_called()
