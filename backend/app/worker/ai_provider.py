"""AI Provider abstraction with circuit breaker.

Defines a Protocol for AI operations used by the execution engine,
along with concrete implementations:
- OpenAIProvider: delegates to existing functions using OpenAI client
- DeterministicFallbackProvider: no-AI fallback when circuit is open
- AICircuitBreaker: wraps primary + fallback with automatic failover

Usage:
    provider = AICircuitBreaker(
        primary=OpenAIProvider(client),
        fallback=DeterministicFallbackProvider(),
    )
    intent, p_tok, c_tok = provider.generate_intent(...)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from app.worker.intent_schema import StepIntent
    from app.worker.recovery.diagnosis import FailureDiagnosis
    from app.worker.selector_engine.scoring import CandidateScore

logger = structlog.get_logger(__name__)


class AINotAvailable(Exception):
    """Raised when AI is unavailable (circuit open, no API key, etc.)."""


@runtime_checkable
class AIProvider(Protocol):
    """Protocol for AI operations used by the execution engine."""

    def generate_intent(
        self,
        action: str,
        step_number: int,
        previous_actions: list[str],
        page_context: dict | None = None,
        model: str = "gpt-5-mini",
        test_data: dict[str, str] | None = None,
        selector_hints: list[dict] | None = None,
        intelligence_context: str | None = None,
    ) -> tuple[StepIntent, int, int]: ...

    def regenerate_intent(
        self,
        action: str,
        step_number: int,
        previous_actions: list[str],
        error_context: str,
        page_context: dict | None = None,
        model: str = "gpt-5-mini",
        test_data: dict[str, str] | None = None,
        selector_hints: list[dict] | None = None,
        intelligence_context: str | None = None,
    ) -> tuple[StepIntent, int, int]: ...

    def diagnose_failure(
        self,
        error_message: str,
        page: Page | None = None,
        page_state: object | None = None,
        model: str = "gpt-5-mini",
    ) -> FailureDiagnosis: ...

    def disambiguate(
        self,
        page: Page,
        candidates: list[CandidateScore],
        action_description: str = "",
        last_action_selector: str | None = None,
    ) -> CandidateScore | None: ...

    def verify_vision(
        self,
        screenshot_bytes: bytes,
        action: str,
        expected: str,
        model: str = "gpt-5-mini",
    ) -> dict: ...

    def verify_api_response(
        self,
        method: str,
        url: str,
        request_headers: dict,
        request_body: str,
        status_code: int,
        response_headers: dict,
        response_body: str,
        expected_description: str,
        model: str = "gpt-5-mini",
    ) -> dict: ...

    @property
    def raw_client(self) -> object | None:
        """Return the underlying client object (e.g., OpenAI) or None."""
        ...


class OpenAIProvider:
    """Wraps an OpenAI client and delegates to existing functions."""

    def __init__(self, client) -> None:
        self._client = client

    @property
    def raw_client(self):
        return self._client

    def generate_intent(
        self,
        action: str,
        step_number: int,
        previous_actions: list[str],
        page_context: dict | None = None,
        model: str = "gpt-5-mini",
        test_data: dict[str, str] | None = None,
        selector_hints: list[dict] | None = None,
        intelligence_context: str | None = None,
    ) -> tuple[StepIntent, int, int]:
        from app.worker.intent_generator import generate_step_intent

        return generate_step_intent(
            self._client, action, step_number, previous_actions,
            page_context, model, test_data, selector_hints,
            intelligence_context,
        )

    def regenerate_intent(
        self,
        action: str,
        step_number: int,
        previous_actions: list[str],
        error_context: str,
        page_context: dict | None = None,
        model: str = "gpt-5-mini",
        test_data: dict[str, str] | None = None,
        selector_hints: list[dict] | None = None,
        intelligence_context: str | None = None,
    ) -> tuple[StepIntent, int, int]:
        from app.worker.intent_generator import regenerate_step_intent

        return regenerate_step_intent(
            self._client, action, step_number, previous_actions,
            error_context, page_context, model, test_data,
            selector_hints, intelligence_context,
        )

    def diagnose_failure(
        self,
        error_message: str,
        page: Page | None = None,
        page_state: object | None = None,
        model: str = "gpt-5-mini",
    ) -> FailureDiagnosis:
        from app.worker.recovery.ai_reasoning import ai_diagnose_failure

        return ai_diagnose_failure(
            self._client, error_message, page=page,
            page_state=page_state, model=model,
        )

    def disambiguate(
        self,
        page: Page,
        candidates: list[CandidateScore],
        action_description: str = "",
        last_action_selector: str | None = None,
    ) -> CandidateScore | None:
        from app.worker.selector_engine.disambiguation import disambiguate

        return disambiguate(
            page, candidates, action_description,
            ai_client=self._client,
            last_action_selector=last_action_selector,
        )

    def verify_vision(
        self,
        screenshot_bytes: bytes,
        action: str,
        expected: str,
        model: str = "gpt-5-mini",
    ) -> dict:
        from app.worker.ai_executor import verify_step_with_vision

        return verify_step_with_vision(
            self._client, screenshot_bytes, action, expected, model,
        )

    def verify_api_response(
        self,
        method: str,
        url: str,
        request_headers: dict,
        request_body: str,
        status_code: int,
        response_headers: dict,
        response_body: str,
        expected_description: str,
        model: str = "gpt-5-mini",
    ) -> dict:
        from app.worker.api_executor import verify_response_with_ai

        return verify_response_with_ai(
            self._client, method, url, request_headers, request_body,
            status_code, response_headers, response_body,
            expected_description, model,
        )


class DeterministicFallbackProvider:
    """No-AI fallback. Used when circuit is open or no API key configured."""

    @property
    def raw_client(self):
        return None

    def generate_intent(self, **kwargs) -> tuple[StepIntent, int, int]:
        raise AINotAvailable("AI circuit is open — no intent generation available")

    def regenerate_intent(self, **kwargs) -> tuple[StepIntent, int, int]:
        raise AINotAvailable("AI circuit is open — no intent regeneration available")

    def diagnose_failure(
        self,
        error_message: str,
        page: Page | None = None,
        page_state: object | None = None,
        model: str = "gpt-5-mini",
    ) -> FailureDiagnosis:
        from app.worker.recovery.diagnosis import FailureDiagnosis, FailureType

        return FailureDiagnosis(
            failure_type=FailureType.UNKNOWN,
            root_cause=f"AI unavailable. Error: {error_message[:200]}",
            is_recoverable=True,
            suggested_recovery="retry_with_backoff",
            confidence=0.3,
            diagnosis_source="fallback",
        )

    def disambiguate(
        self,
        page: Page,
        candidates: list[CandidateScore],
        action_description: str = "",
        last_action_selector: str | None = None,
    ) -> CandidateScore | None:
        return None  # No AI disambiguation — scorer result stands

    def verify_vision(
        self,
        screenshot_bytes: bytes,
        action: str,
        expected: str,
        model: str = "gpt-5-mini",
    ) -> dict:
        return {"status": "passed", "actual": "Vision verification unavailable (AI circuit open)"}

    def verify_api_response(
        self,
        method: str,
        url: str,
        request_headers: dict,
        request_body: str,
        status_code: int,
        response_headers: dict,
        response_body: str,
        expected_description: str,
        model: str = "gpt-5-mini",
    ) -> dict:
        return {"status": "passed", "actual": f"Status {status_code} (AI verification unavailable)"}


@dataclass
class AICircuitBreaker:
    """Wraps primary + fallback provider with automatic failover.

    State machine:
    - CLOSED: all calls go to primary
    - OPEN: all calls go to fallback (after 3 consecutive failures)
    - HALF_OPEN: try one call on primary to test recovery (after 60s)
    """

    primary: OpenAIProvider
    fallback: DeterministicFallbackProvider = field(default_factory=DeterministicFallbackProvider)
    failure_threshold: int = 3
    recovery_timeout_s: float = 60.0

    # Internal state
    _consecutive_failures: int = field(default=0, init=False, repr=False)
    _state: str = field(default="closed", init=False, repr=False)
    _opened_at: float = field(default=0.0, init=False, repr=False)

    @property
    def raw_client(self):
        """Return underlying client from the active provider."""
        if self._state == "open":
            return self.fallback.raw_client
        return self.primary.raw_client

    def _should_try_primary(self) -> bool:
        if self._state == "closed":
            return True
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
                self._state = "half_open"
                logger.info("ai_circuit.half_open")
                return True
            return False
        # half_open
        return True

    def _record_success(self) -> None:
        if self._state != "closed":
            logger.info("ai_circuit.closed", prev_state=self._state)
        self._consecutive_failures = 0
        self._state = "closed"

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            if self._state != "open":
                logger.warning(
                    "ai_circuit.opened",
                    consecutive_failures=self._consecutive_failures,
                )
            self._state = "open"
            self._opened_at = time.monotonic()

    def _call_with_breaker(self, method_name: str, *args, **kwargs):
        """Route a call through the circuit breaker."""
        if self._should_try_primary():
            try:
                result = getattr(self.primary, method_name)(*args, **kwargs)
                self._record_success()
                return result
            except AINotAvailable:
                raise
            except Exception as e:
                self._record_failure()
                logger.warning(
                    "ai_circuit.primary_failed",
                    method=method_name,
                    error=str(e)[:200],
                    state=self._state,
                )
                # Fall through to fallback
        return getattr(self.fallback, method_name)(*args, **kwargs)

    def generate_intent(self, *args, **kwargs):
        return self._call_with_breaker("generate_intent", *args, **kwargs)

    def regenerate_intent(self, *args, **kwargs):
        return self._call_with_breaker("regenerate_intent", *args, **kwargs)

    def diagnose_failure(self, *args, **kwargs):
        return self._call_with_breaker("diagnose_failure", *args, **kwargs)

    def disambiguate(self, *args, **kwargs):
        return self._call_with_breaker("disambiguate", *args, **kwargs)

    def verify_vision(self, *args, **kwargs):
        return self._call_with_breaker("verify_vision", *args, **kwargs)

    def verify_api_response(self, *args, **kwargs):
        return self._call_with_breaker("verify_api_response", *args, **kwargs)
