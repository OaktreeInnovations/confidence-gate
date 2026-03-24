"""In-run adaptive memory for selector blacklisting and recovery tracking.

Provides two data structures mutated during a single test run:

1. InRunSelectorMemory: Tracks which selectors fail/succeed per action index.
   After 2 failures for the same (action_index, selector) pair, the selector
   is blacklisted for the rest of the run. Successful selectors are promoted
   so retry attempts try them first.

2. RecoveryEffectivenessTracker: Tracks which recovery action types work for
   which failure types. After observing a low success rate (< 30% over last 5),
   the tracker recommends skipping that recovery action to save budget.
   Can be persisted to DB for cross-run learning.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class InRunSelectorMemory:
    """Per-run selector blacklist and promotions. Mutated mid-run."""

    _blacklist: set[tuple[int, str]] = field(default_factory=set)
    _promotions: dict[int, str] = field(default_factory=dict)
    _failure_counts: dict[tuple[int, str], int] = field(
        default_factory=lambda: defaultdict(int)
    )

    def record_failure(self, action_index: int, selector: str) -> None:
        """Record a selector failure. Blacklists after 2 failures."""
        key = (action_index, selector)
        self._failure_counts[key] += 1
        if self._failure_counts[key] >= 2:
            self._blacklist.add(key)
            logger.info(
                "in_run_memory.blacklisted",
                action_index=action_index,
                selector=selector[:80],
            )

    def record_success(self, action_index: int, selector: str) -> None:
        """Promote a selector that worked."""
        self._promotions[action_index] = selector

    def is_blacklisted(self, action_index: int, selector: str) -> bool:
        """Check if a selector is blacklisted for this action."""
        return (action_index, selector) in self._blacklist

    def get_preferred(self, action_index: int) -> str | None:
        """Get the promoted selector for this action, if any."""
        return self._promotions.get(action_index)

    @property
    def blacklist_count(self) -> int:
        return len(self._blacklist)

    @property
    def promotion_count(self) -> int:
        return len(self._promotions)


@dataclass
class RecoveryEffectivenessTracker:
    """Tracks which recovery actions work for which failure types.

    Keeps a sliding window of outcomes per (failure_type, action_type) pair.
    After enough observations, can recommend skipping ineffective actions.
    """

    _history: dict[tuple[str, str], list[bool]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def record(self, failure_type: str, action_type: str, success: bool) -> None:
        """Record a recovery action outcome."""
        self._history[(failure_type, action_type)].append(success)

    def should_skip(
        self,
        failure_type: str,
        action_type: str,
        threshold: float = 0.30,
        min_observations: int = 3,
    ) -> bool:
        """Check if a recovery action should be skipped due to low effectiveness.

        Returns True if the action's recent success rate is below threshold
        and we have enough observations to be confident.
        """
        history = self._history.get((failure_type, action_type), [])
        if len(history) < min_observations:
            return False
        recent = history[-5:]  # Look at last 5 attempts
        success_rate = sum(recent) / len(recent)
        if success_rate < threshold:
            logger.info(
                "recovery_tracker.skipping",
                failure_type=failure_type,
                action_type=action_type,
                success_rate=round(success_rate, 2),
                observations=len(history),
            )
            return True
        return False

    def get_summary(self) -> dict[str, dict[str, float]]:
        """Get a summary of effectiveness rates for telemetry."""
        summary: dict[str, dict[str, float]] = {}
        for (ft, at), hist in self._history.items():
            if ft not in summary:
                summary[ft] = {}
            summary[ft][at] = sum(hist) / len(hist) if hist else 0.0
        return summary
