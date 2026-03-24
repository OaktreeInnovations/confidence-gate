"""AI Dependency Reduction — gate all AI calls through a single control point.

Ensures AI is ONLY invoked when deterministic approaches cannot solve the problem:
1. Intent generation — needed only when no cached intent exists
2. Disambiguation — needed only when scorer produces ambiguous results
3. Failure diagnosis — needed only when deterministic diagnosis is inconclusive
4. Vision verification — needed only in DEEP execution mode

All other operations (selector resolution, execution, recovery, validation)
are fully deterministic.

This module provides:
- AIGate: central gatekeeper that tracks and limits AI calls per step
- ai_call_budget: configurable limits on AI calls per step/run
- Metrics: tracks AI call count, token usage, and whether AI was needed
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)

# Explicit list of allowed AI purposes — AI is advisory only,
# never bypasses scoring or guard checks.
_ALLOWED_AI_PURPOSES = frozenset({
    "intent_gen",
    "intent_regen",
    "vision_verify",
    "structural_arbitration",
    "disambiguation",
    "diagnose",
    "vision",
    "disambiguate",
})


@dataclass
class AICallBudget:
    """Configurable limits on AI calls."""

    max_calls_per_step: int = 4
    max_calls_per_run: int = 50
    max_tokens_per_step: int = 10000
    allow_disambiguation: bool = True
    allow_diagnosis: bool = True
    allow_vision: bool = False  # Only enabled in DEEP mode


@dataclass
class AICallRecord:
    """Record of a single AI call."""

    purpose: str  # "intent_gen", "intent_regen", "disambiguate", "diagnose", "vision"
    model: str = ""
    tokens_used: int = 0
    was_necessary: bool = True  # False if deterministic could have handled it


@dataclass
class AIGate:
    """Central gatekeeper for AI calls — tracks usage and enforces limits.

    Usage:
        gate = AIGate(budget=AICallBudget(...))

        if gate.can_call("disambiguate"):
            result = ai_disambiguate(...)
            gate.record_call("disambiguate", model, tokens)
    """

    budget: AICallBudget = field(default_factory=AICallBudget)
    calls: list[AICallRecord] = field(default_factory=list)
    _step_calls: int = 0
    _step_tokens: int = 0
    _run_calls: int = 0

    def can_call(self, purpose: str) -> bool:
        """Check if an AI call is allowed within budget constraints.

        Enforces an explicit allow-list of AI purposes. Unknown purposes
        are rejected to prevent uncontrolled AI usage creep.
        """
        # Policy enforcement: reject unknown purposes
        if purpose not in _ALLOWED_AI_PURPOSES:
            logger.warning("ai_gate.unknown_purpose_rejected", purpose=purpose)
            return False

        if self._step_calls >= self.budget.max_calls_per_step:
            logger.debug("ai_gate.step_budget_exceeded", purpose=purpose)
            return False
        if self._run_calls >= self.budget.max_calls_per_run:
            logger.debug("ai_gate.run_budget_exceeded", purpose=purpose)
            return False
        if self._step_tokens >= self.budget.max_tokens_per_step:
            logger.debug("ai_gate.token_budget_exceeded", purpose=purpose)
            return False

        # Purpose-specific gates
        if purpose == "disambiguate" and not self.budget.allow_disambiguation:
            return False
        if purpose == "diagnose" and not self.budget.allow_diagnosis:
            return False
        if purpose in ("vision", "vision_verify") and not self.budget.allow_vision:
            return False

        return True

    def record_call(
        self,
        purpose: str,
        model: str = "",
        tokens: int = 0,
        was_necessary: bool = True,
    ) -> None:
        """Record an AI call for tracking."""
        self.calls.append(AICallRecord(
            purpose=purpose,
            model=model,
            tokens_used=tokens,
            was_necessary=was_necessary,
        ))
        self._step_calls += 1
        self._step_tokens += tokens
        self._run_calls += 1

    def reset_step(self) -> None:
        """Reset per-step counters (call between steps)."""
        self._step_calls = 0
        self._step_tokens = 0

    @property
    def step_ai_calls(self) -> int:
        return self._step_calls

    @property
    def run_ai_calls(self) -> int:
        return self._run_calls

    @property
    def ai_ratio(self) -> float:
        """Ratio of AI calls that were necessary vs total."""
        if not self.calls:
            return 0.0
        necessary = sum(1 for c in self.calls if c.was_necessary)
        return necessary / len(self.calls)

    def get_metrics(self) -> dict:
        """Get AI usage metrics for telemetry."""
        total_tokens = sum(c.tokens_used for c in self.calls)
        by_purpose: dict[str, int] = {}
        for c in self.calls:
            by_purpose[c.purpose] = by_purpose.get(c.purpose, 0) + 1

        return {
            "total_calls": len(self.calls),
            "total_tokens": total_tokens,
            "step_calls": self._step_calls,
            "run_calls": self._run_calls,
            "ai_ratio": round(self.ai_ratio, 3),
            "by_purpose": by_purpose,
        }
