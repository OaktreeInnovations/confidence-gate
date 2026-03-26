"""Tests for healing penalty in confidence scorer.

Validates:
- Steps that used healing get a lower confidence score
- Healing usage is reported as a risk factor
"""

from app.telemetry.confidence_scorer import compute_step_confidence, _identify_step_risks
from app.telemetry.models import (
    ExecutionAttempt,
    PhaseTimings,
    StepTelemetry,
)


def _make_step(healing_attempted=False, healing_layer=0, healing_success=True) -> StepTelemetry:
    """Create a minimal passing StepTelemetry."""
    attempts = [
        ExecutionAttempt(
            attempt_number=1,
            success=True,
            execution_ms=500,
            healing_attempted=healing_attempted,
            healing_layer_used=healing_layer,
            healing_strategy="text_similarity" if healing_attempted else "",
            healing_success=healing_success if healing_attempted else False,
        ),
    ]
    return StepTelemetry(
        step_number=1,
        action="click Submit",
        status="passed",
        timings=PhaseTimings(total_ms=600, verification_ms=100),
        attempts=attempts,
        total_attempts=1,
    )


def test_healing_reduces_confidence():
    """Step with healing_attempted=True scores lower than identical step without."""
    step_normal = _make_step(healing_attempted=False)
    step_healed = _make_step(healing_attempted=True, healing_layer=1)

    score_normal = compute_step_confidence(step_normal)
    score_healed = compute_step_confidence(step_healed)

    assert score_healed < score_normal
    # Healed step loses 0.10 from healing penalty
    assert score_healed <= score_normal - 0.05


def test_healing_risk_factor_reported():
    """Healing usage appears in risk factors."""
    step = _make_step(healing_attempted=True, healing_layer=2)
    score = compute_step_confidence(step)

    risks = _identify_step_risks(step, score)

    healing_risks = [r for r in risks if "Healing" in r]
    assert len(healing_risks) == 1
    assert "layers: [2]" in healing_risks[0]
