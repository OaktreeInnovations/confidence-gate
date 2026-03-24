"""Strict Execution Budget Enforcement.

Provides a StepExecutionBudget that tracks time spent across all phases
of step execution (context capture, AI generation, execution, recovery,
validation) and enforces hard limits.

Key behaviors:
- Total step budget (default 60s) is the hard ceiling
- Each phase gets a proportional allocation
- Remaining budget dynamically computed — no operation gets more than what's left
- Budget exceeded → TimeoutError with detailed breakdown
- Integrates with Playwright page.set_default_timeout() to cap internal waits

Usage:
    budget = StepExecutionBudget(total_s=60)
    budget.start()

    with budget.phase("context_capture", max_pct=0.15):
        capture_context(page)

    remaining_ms = budget.remaining_ms()
    page.set_default_timeout(min(15000, remaining_ms))
    ...
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class PhaseRecord:
    """Timing record for a single execution phase."""

    name: str
    started_at: float = 0.0
    ended_at: float = 0.0
    duration_ms: int = 0
    allocated_ms: int = 0
    exceeded: bool = False


@dataclass
class StepExecutionBudget:
    """Enforces strict time budgets across step execution phases.

    All time tracking uses monotonic clock. The budget is immutable
    once started — no extending allowed.
    """

    total_s: float = 60.0
    started_at: float = 0.0
    phases: list[PhaseRecord] = field(default_factory=list)
    _active_phase: PhaseRecord | None = field(default=None, repr=False)

    def start(self) -> None:
        """Start the budget clock."""
        self.started_at = time.monotonic()
        self.phases = []

    @property
    def total_ms(self) -> int:
        return int(self.total_s * 1000)

    def elapsed_ms(self) -> int:
        """Total elapsed since budget start."""
        if self.started_at == 0:
            return 0
        return int((time.monotonic() - self.started_at) * 1000)

    def remaining_ms(self) -> int:
        """Remaining budget in milliseconds. Never negative."""
        return max(0, self.total_ms - self.elapsed_ms())

    def remaining_s(self) -> float:
        """Remaining budget in seconds."""
        return self.remaining_ms() / 1000.0

    def is_exceeded(self) -> bool:
        """Check if total budget is exhausted."""
        return self.remaining_ms() <= 0

    def check_budget(self, context: str = "") -> None:
        """Raise TimeoutError if budget is exceeded.

        Call this before any expensive operation.
        """
        if self.is_exceeded():
            breakdown = self.get_breakdown()
            raise TimeoutError(
                f"Step budget exceeded ({self.total_s}s). "
                f"Context: {context}. "
                f"Breakdown: {breakdown}"
            )

    def allocate_timeout(self, max_ms: int, min_ms: int = 2000) -> int:
        """Get a safe timeout value that fits within remaining budget.

        Returns the minimum of max_ms and remaining budget, floored at min_ms.
        """
        remaining = self.remaining_ms()
        if remaining <= 0:
            return min_ms
        return max(min(max_ms, remaining), min_ms)

    @contextmanager
    def phase(self, name: str, max_pct: float = 1.0):
        """Context manager to track a named phase.

        Args:
            name: Phase name for logging/telemetry.
            max_pct: Maximum percentage of total budget this phase can use (0.0–1.0).
        """
        self.check_budget(f"before {name}")

        record = PhaseRecord(
            name=name,
            started_at=time.monotonic(),
            allocated_ms=int(self.total_ms * max_pct),
        )
        self._active_phase = record

        try:
            yield record
        finally:
            record.ended_at = time.monotonic()
            record.duration_ms = int((record.ended_at - record.started_at) * 1000)
            record.exceeded = record.duration_ms > record.allocated_ms
            self._active_phase = None
            self.phases.append(record)

            if record.exceeded:
                logger.warning(
                    "budget.phase_exceeded",
                    phase=name,
                    duration_ms=record.duration_ms,
                    allocated_ms=record.allocated_ms,
                )

    def cap_playwright_timeout(self, page, max_ms: int = 15000) -> int:
        """Set page.set_default_timeout to min(max_ms, remaining_budget).

        Returns the timeout that was set.
        """
        timeout = self.allocate_timeout(max_ms)
        try:
            page.set_default_timeout(timeout)
        except Exception:
            pass
        return timeout

    def get_breakdown(self) -> dict:
        """Get timing breakdown for telemetry."""
        return {
            "total_budget_ms": self.total_ms,
            "elapsed_ms": self.elapsed_ms(),
            "remaining_ms": self.remaining_ms(),
            "phases": [
                {
                    "name": p.name,
                    "duration_ms": p.duration_ms,
                    "allocated_ms": p.allocated_ms,
                    "exceeded": p.exceeded,
                }
                for p in self.phases
            ],
        }

    def phase_summary(self) -> str:
        """Human-readable phase summary for logging."""
        parts = []
        for p in self.phases:
            flag = " [!]" if p.exceeded else ""
            parts.append(f"{p.name}={p.duration_ms}ms{flag}")
        return ", ".join(parts) + f" | total={self.elapsed_ms()}/{self.total_ms}ms"


# --- Budget-safe wait helpers ---


def budget_wait(page, ms: int, context: str = "") -> None:
    """Budget-aware replacement for page.wait_for_timeout().

    Caps the requested wait at whatever time remains in the step deadline,
    preventing component interactions from burning budget.
    """
    from app.worker.stability_wrappers import _remaining_ms

    remaining = _remaining_ms()
    actual = min(ms, remaining)

    if actual <= 0:
        logger.debug("budget_wait.skipped", context=context, requested_ms=ms)
        return

    if actual < ms:
        logger.debug(
            "budget_wait.capped",
            context=context,
            requested_ms=ms,
            actual_ms=actual,
        )

    page.wait_for_timeout(actual)


class ComponentBudgetExceeded(TimeoutError):
    """Raised when a component interaction exceeds its budget allocation."""

    def __init__(self, elapsed_ms: int, allowed_ms: int):
        self.elapsed_ms = elapsed_ms
        self.allowed_ms = allowed_ms
        super().__init__(
            f"Component budget exceeded: {elapsed_ms}ms > {allowed_ms}ms allowed"
        )


@contextmanager
def component_budget(max_pct: float = 0.30, enforce: bool = True):
    """Context manager that caps a single component interaction.

    Args:
        max_pct: Maximum percentage of remaining step budget.
        enforce: If True, raise ComponentBudgetExceeded when exceeded.
                 If False, log warning only (legacy behavior).

    Usage:
        with component_budget(0.30):
            strategies.select_dropdown(page, label, option)
    """
    from app.worker.stability_wrappers import _remaining_ms

    budget_at_entry = _remaining_ms()
    allowed_ms = int(budget_at_entry * max_pct)
    t0 = time.monotonic()

    try:
        yield allowed_ms
    finally:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if elapsed_ms > allowed_ms:
            logger.warning(
                "component_budget.exceeded",
                elapsed_ms=elapsed_ms,
                allowed_ms=allowed_ms,
                budget_at_entry_ms=budget_at_entry,
            )
            if enforce:
                raise ComponentBudgetExceeded(elapsed_ms, allowed_ms)
