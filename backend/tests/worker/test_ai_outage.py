"""Test: AI circuit breaker — outage handling.

Scenario: The AI provider fails 3 consecutive times, causing the
circuit breaker to open. Subsequent calls should go to the fallback
provider. After 60s, the circuit should enter half-open state.
"""

import time
from unittest.mock import patch

import pytest

from app.worker.ai_provider import (
    AICircuitBreaker,
    AINotAvailable,
    DeterministicFallbackProvider,
)


class FailingProvider:
    """Provider that always raises on generate_intent."""

    @property
    def raw_client(self):
        return None

    def generate_intent(self, **kwargs):
        raise RuntimeError("API error: 500 internal server error")

    def regenerate_intent(self, **kwargs):
        raise RuntimeError("API error")

    def diagnose_failure(self, **kwargs):
        from app.worker.recovery.diagnosis import FailureDiagnosis, FailureType
        return FailureDiagnosis(
            failure_type=FailureType.UNKNOWN,
            root_cause="AI diagnosis",
            is_recoverable=True,
            confidence=0.9,
        )

    def disambiguate(self, **kwargs):
        return None

    def verify_vision(self, **kwargs):
        return {"status": "passed", "actual": "ok"}

    def verify_api_response(self, **kwargs):
        return {"status": "passed", "actual": "ok"}


class CountingProvider(FailingProvider):
    """Provider that fails N times then succeeds."""

    def __init__(self, fail_count: int = 3):
        self._fail_count = fail_count
        self._call_count = 0

    def generate_intent(self, **kwargs):
        self._call_count += 1
        if self._call_count <= self._fail_count:
            raise RuntimeError(f"API error (call {self._call_count})")
        from app.worker.intent_schema import ActionIntent, ActionType, StepIntent, TargetDescriptor
        return StepIntent(
            step_number=1,
            description="Recovered",
            actions=[ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(role="button", name="OK"))],
        ), 10, 5


def test_circuit_starts_closed():
    """Circuit breaker should start in closed state."""
    cb = AICircuitBreaker(
        primary=FailingProvider(),
        failure_threshold=3,
    )
    assert cb._state == "closed"
    assert cb._consecutive_failures == 0


def test_circuit_opens_after_threshold():
    """Circuit should open after 3 consecutive failures."""
    cb = AICircuitBreaker(
        primary=FailingProvider(),
        failure_threshold=3,
    )

    for _ in range(3):
        # generate_intent fails on primary, falls through to fallback
        # Fallback raises AINotAvailable
        with pytest.raises(AINotAvailable):
            cb.generate_intent()

    assert cb._state == "open"
    assert cb._consecutive_failures >= 3


def test_open_circuit_uses_fallback():
    """When circuit is open, calls should go to fallback provider."""
    cb = AICircuitBreaker(
        primary=FailingProvider(),
        failure_threshold=3,
    )

    # Open the circuit
    for _ in range(3):
        with pytest.raises(AINotAvailable):
            cb.generate_intent()

    # Now diagnose_failure should use fallback (which returns a low-confidence diagnosis)
    diag = cb.diagnose_failure(error_message="test error")
    assert diag.confidence == 0.3
    assert diag.diagnosis_source == "fallback"


def test_circuit_half_open_after_timeout():
    """After recovery_timeout_s, circuit should enter half-open state."""
    cb = AICircuitBreaker(
        primary=CountingProvider(fail_count=3),
        failure_threshold=3,
        recovery_timeout_s=0.1,  # Short timeout for testing
    )

    # Open the circuit
    for _ in range(3):
        with pytest.raises(AINotAvailable):
            cb.generate_intent()

    assert cb._state == "open"

    # Wait for recovery timeout
    time.sleep(0.15)

    # Next call should try primary (half-open)
    # CountingProvider has failed 3 times already, call 4 will succeed
    intent, p, c = cb.generate_intent()
    assert intent.description == "Recovered"
    assert cb._state == "closed"


def test_circuit_closes_on_success():
    """A successful call should close the circuit."""
    cb = AICircuitBreaker(
        primary=CountingProvider(fail_count=0),
        failure_threshold=3,
    )

    intent, p, c = cb.generate_intent()
    assert cb._state == "closed"
    assert cb._consecutive_failures == 0


def test_fallback_diagnose_returns_safe_default():
    """DeterministicFallbackProvider should return a safe diagnosis."""
    fallback = DeterministicFallbackProvider()
    diag = fallback.diagnose_failure(error_message="timeout waiting for element")

    assert diag.is_recoverable is True
    assert diag.confidence == 0.3
    assert "timeout" in diag.root_cause.lower()


def test_fallback_verify_vision_passes():
    """Fallback vision verification should always pass."""
    fallback = DeterministicFallbackProvider()
    result = fallback.verify_vision(
        screenshot_bytes=b"fake",
        action="Click Submit",
        expected="Form submitted",
    )
    assert result["status"] == "passed"
