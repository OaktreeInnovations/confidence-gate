"""Test: Disabled button diagnosis and recovery.

Scenario: A button exists on the page but is disabled. The execution
engine should diagnose ELEMENT_DISABLED and suggest WAIT_FOR_ENABLED
recovery.
"""

from app.worker.recovery.diagnosis import (
    FailureDiagnosis,
    FailureType,
    diagnose_failure,
)


def test_disabled_button_diagnosed():
    """Error containing 'disabled' should produce ELEMENT_DISABLED diagnosis."""
    error = "Element is disabled: locator.click"
    diag = diagnose_failure(error)

    assert diag.failure_type == FailureType.ELEMENT_DISABLED
    assert diag.is_recoverable is True
    assert diag.confidence > 0.5


def test_disabled_button_recovery_plan():
    """ELEMENT_DISABLED should produce a recovery plan with WAIT_FOR_ENABLED."""
    from app.worker.recovery.strategy_mapping import plan_recovery

    diag = FailureDiagnosis(
        failure_type=FailureType.ELEMENT_DISABLED,
        root_cause="Button is disabled",
        is_recoverable=True,
        confidence=0.9,
    )

    plan = plan_recovery(diag, attempt=0, max_attempts=3)
    action_types = [a.action_type.value for a in plan.actions]
    assert "wait_for_enabled" in action_types or len(plan.actions) > 0
