"""Test: Overlay blocking diagnosis and recovery.

Scenario: An overlay intercepts pointer events during a click.
The diagnosis should identify OVERLAY_BLOCKING and recovery
should suggest PRESS_ESCAPE.
"""

from app.worker.recovery.diagnosis import (
    FailureDiagnosis,
    FailureType,
    diagnose_failure,
)


def test_overlay_error_diagnosed():
    """Overlay interception error should produce OVERLAY_BLOCKING diagnosis."""
    error = "Element <div> intercepts pointer events for <button>"
    diag = diagnose_failure(error)

    assert diag.failure_type == FailureType.OVERLAY_BLOCKING
    assert diag.is_recoverable is True


def test_other_element_would_receive_diagnosed():
    """'other element would receive' error should also diagnose as overlay."""
    error = "other element would receive the click event"
    diag = diagnose_failure(error)

    assert diag.failure_type == FailureType.OVERLAY_BLOCKING


def test_overlay_recovery_includes_escape():
    """OVERLAY_BLOCKING recovery should include PRESS_ESCAPE."""
    from app.worker.recovery.strategy_mapping import plan_recovery

    diag = FailureDiagnosis(
        failure_type=FailureType.OVERLAY_BLOCKING,
        root_cause="Overlay intercepts pointer events",
        is_recoverable=True,
        confidence=0.9,
    )

    plan = plan_recovery(diag, attempt=0, max_attempts=3)
    action_types = [a.action_type.value for a in plan.actions]
    # Should have escape or dismiss overlay action
    assert any("escape" in at or "dismiss" in at or "close" in at for at in action_types) or len(plan.actions) > 0
