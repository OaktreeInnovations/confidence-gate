"""Tests for healing telemetry pipeline.

Validates:
- Healing metrics are written to ExecutionAttempt via record_healing_metrics()
- Default values when record_healing_metrics() is not called
- Healing metrics reset between attempts (no bleed)
"""

from app.telemetry.collector import _StepTelemetryBuilder


def _make_builder() -> _StepTelemetryBuilder:
    return _StepTelemetryBuilder(step_number=1, action="click Submit")


def test_healing_telemetry_written_on_success():
    """Healing metrics appear in ExecutionAttempt after record_healing_metrics()."""
    builder = _make_builder()

    builder.record_healing_metrics(
        healing_attempted=True,
        healing_layer_used=1,
        healing_strategy="text_similarity",
        healing_elapsed_ms=250,
        selector_repaired=True,
        action_repaired=False,
    )
    builder.record_attempt(1, False, "code", 100, "Element not found")

    step = builder.build("passed")
    assert len(step.attempts) == 1

    attempt = step.attempts[0]
    assert attempt.healing_attempted is True
    assert attempt.healing_layer_used == 1
    assert attempt.healing_strategy == "text_similarity"
    assert attempt.healing_elapsed_ms == 250
    assert attempt.selector_repaired is True
    assert attempt.action_repaired is False


def test_healing_telemetry_defaults_without_call():
    """Healing fields have defaults when record_healing_metrics() is not called."""
    builder = _make_builder()

    builder.record_attempt(1, True, "code", 100)

    step = builder.build("passed")
    attempt = step.attempts[0]

    assert attempt.healing_attempted is False
    assert attempt.healing_layer_used == 0
    assert attempt.healing_strategy == ""
    assert attempt.healing_elapsed_ms == 0
    assert attempt.selector_repaired is False
    assert attempt.action_repaired is False


def test_healing_telemetry_resets_between_attempts():
    """Healing metrics from first attempt don't bleed into second attempt."""
    builder = _make_builder()

    # First attempt: healing was used
    builder.record_healing_metrics(
        healing_attempted=True,
        healing_layer_used=2,
        healing_strategy="focus_enter",
        healing_elapsed_ms=180,
        selector_repaired=False,
        action_repaired=True,
    )
    builder.record_attempt(1, False, "code", 100, "Click had no effect")

    # Second attempt: no healing — should have defaults
    builder.record_attempt(2, True, "code", 50)

    step = builder.build("passed")
    assert len(step.attempts) == 2

    # First attempt should have healing data
    assert step.attempts[0].healing_attempted is True
    assert step.attempts[0].healing_layer_used == 2
    assert step.attempts[0].healing_strategy == "focus_enter"
    assert step.attempts[0].action_repaired is True

    # Second attempt should have defaults (reset)
    assert step.attempts[1].healing_attempted is False
    assert step.attempts[1].healing_layer_used == 0
    assert step.attempts[1].healing_strategy == ""
    assert step.attempts[1].healing_elapsed_ms == 0
    assert step.attempts[1].selector_repaired is False
    assert step.attempts[1].action_repaired is False
