"""Tests for healing integration parameters.

Validates:
- Healing receives re-resolved locator for action repair
- Healing receives captured pre_state for verification
- Healing still works when target resolution fails
"""

from unittest.mock import MagicMock, patch

from app.worker.healing.pipeline import HealingResult
from app.worker.healing.selector_repair import RepairResult
from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
from app.worker.recovery.diagnosis import FailureDiagnosis, FailureType


def _make_target():
    return TargetDescriptor(role="button", name="Submit")


def _make_action():
    return ActionIntent(action=ActionType.CLICK, target=_make_target())


def _make_diagnosis():
    return FailureDiagnosis(
        failure_type=FailureType.ELEMENT_NOT_FOUND,
        root_cause="Element not found",
        is_recoverable=True,
    )


def _make_resolved_target():
    """Create a mock ResolvedTarget."""
    mock = MagicMock()
    mock.locator = MagicMock()
    return mock


def test_action_repair_receives_locator():
    """When target resolves, execute_healing receives the locator."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis()
    resolved = _make_resolved_target()

    mock_healing_result = HealingResult(healed=False)

    with patch("app.worker.healing.pipeline.repair_selector",
               return_value=RepairResult(repaired=False, candidates_evaluated=0)), \
         patch("app.worker.healing.pipeline.repair_action") as mock_action_repair, \
         patch("app.worker.healing.pipeline.ai_structural_repair"):

        mock_action_repair.return_value = MagicMock(repaired=False, attempts=0)

        # Import and call execute_healing with a locator
        from app.worker.healing.pipeline import execute_healing
        result = execute_healing(
            page, action, RuntimeError("fail"), diagnosis,
            locator=resolved.locator,
            pre_state={"url": "https://example.com"},
        )

    # action_repair should have been called with the locator
    mock_action_repair.assert_called_once()
    call_args = mock_action_repair.call_args
    assert call_args[0][1] is resolved.locator  # second positional arg is locator


def test_action_repair_receives_pre_state():
    """When pre_state is provided, action repair receives it."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis()
    locator = MagicMock()
    pre_state = {"url": "https://example.com", "aria_expanded": None}

    from app.worker.healing.pipeline import execute_healing

    with patch("app.worker.healing.pipeline.repair_selector",
               return_value=RepairResult(repaired=False, candidates_evaluated=0)), \
         patch("app.worker.healing.pipeline.repair_action") as mock_action_repair, \
         patch("app.worker.healing.pipeline.ai_structural_repair"):

        mock_action_repair.return_value = MagicMock(repaired=False, attempts=0)

        execute_healing(
            page, action, RuntimeError("fail"), diagnosis,
            locator=locator,
            pre_state=pre_state,
        )

    mock_action_repair.assert_called_once()
    call_kwargs = mock_action_repair.call_args
    # pre_state is passed as keyword arg
    assert call_kwargs[1]["pre_state"] == pre_state


def test_healing_works_without_locator():
    """When resolve_target fails, healing still runs with locator=None."""
    page = MagicMock()
    action = _make_action()
    diagnosis = _make_diagnosis()

    from app.worker.healing.pipeline import execute_healing

    with patch("app.worker.healing.pipeline.repair_selector",
               return_value=RepairResult(repaired=False, candidates_evaluated=3)) as mock_sel, \
         patch("app.worker.healing.pipeline.ai_structural_repair"):

        # No locator, no pre_state — Layer 1 selector repair should still run
        result = execute_healing(
            page, action, RuntimeError("fail"), diagnosis,
            locator=None,
            pre_state=None,
        )

    # Selector repair was called (doesn't need locator)
    mock_sel.assert_called_once()
    # Result reflects that healing was attempted
    assert result.selector_repair_result is not None
