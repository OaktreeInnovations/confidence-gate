"""Adaptive Execution Strategy Engine.

Takes historical intelligence data (ExecutionProfile, StepProfile, FlakeScore,
TimingBaseline) and outputs an ExecutionStrategy that modifies runtime
execution behavior per step.

Strategy decisions:
- selector_priority_override: reorder selector hints based on historical success
- retry_budget_override: increase/decrease max retries based on flake patterns
- wait_budget_multiplier: scale wait times based on timing baselines
- aggressive_stability_mode: enable extra pre/post-exec stability waits for flaky steps
- preemptive_retry_enabled: preemptively retry steps with high flake scores

The engine is pure computation — no I/O. It reads from already-fetched
profile data and outputs strategy decisions. The caller (execution pipeline)
is responsible for fetching the profile and applying the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)

# --- Thresholds ---

# Flake score above which a step is considered "high risk"
_HIGH_FLAKE_THRESHOLD = 0.3

# Flake score above which aggressive stability kicks in
_AGGRESSIVE_STABILITY_THRESHOLD = 0.5

# Pass rate below which we increase retry budget
_LOW_PASS_RATE_THRESHOLD = 0.7

# Timing ratio (p95/median) above which we increase wait budget
_TIMING_VARIANCE_THRESHOLD = 2.5

# Minimum observations before we trust the data
_MIN_OBSERVATIONS = 3


@dataclass
class StepStrategy:
    """Execution strategy overrides for a single step."""
    step_number: int

    # Selector priority: reranked list of selector hints
    # If non-empty, these replace the default selector_hints order
    selector_priority_override: list[dict] = field(default_factory=list)

    # Retry budget: override max_attempts for this step
    # None = use default (3). Values 1-5.
    retry_budget_override: int | None = None

    # Wait budget multiplier: scale all wait_for_timeout() calls
    # 1.0 = no change, 1.5 = 50% longer waits, 2.0 = double waits
    wait_budget_multiplier: float = 1.0

    # Aggressive stability mode: add extra pre/post stability waits
    aggressive_stability_mode: bool = False

    # Preemptive retry: if True, retry even on first-attempt success
    # (used for steps with very high intermittent flake rates)
    preemptive_retry_enabled: bool = False

    # Timing baseline: P95 execution time in ms from historical data
    # Used to compute adaptive wait budget instead of fixed mode config
    timing_baseline_ms: int | None = None

    # Reasoning: why these decisions were made (for logging/debugging)
    reasons: list[str] = field(default_factory=list)

    def has_overrides(self) -> bool:
        """Check if this strategy has any non-default overrides."""
        return (
            bool(self.selector_priority_override)
            or self.retry_budget_override is not None
            or self.wait_budget_multiplier != 1.0
            or self.aggressive_stability_mode
            or self.preemptive_retry_enabled
            or self.timing_baseline_ms is not None
        )

    def to_dict(self) -> dict:
        return {
            "step_number": self.step_number,
            "selector_priority_override": self.selector_priority_override,
            "retry_budget_override": self.retry_budget_override,
            "wait_budget_multiplier": self.wait_budget_multiplier,
            "aggressive_stability_mode": self.aggressive_stability_mode,
            "preemptive_retry_enabled": self.preemptive_retry_enabled,
            "timing_baseline_ms": self.timing_baseline_ms,
            "reasons": self.reasons,
        }


@dataclass
class ExecutionStrategy:
    """Full execution strategy for a test run."""
    test_case_id: str
    org_id: str

    # Per-step strategies (keyed by step_number)
    step_strategies: dict[int, StepStrategy] = field(default_factory=dict)

    # Run-level flags
    has_any_overrides: bool = False

    # Summary stats for logging
    steps_with_overrides: int = 0
    total_steps: int = 0

    def get_step(self, step_number: int) -> StepStrategy | None:
        """Get strategy for a specific step, or None for defaults."""
        return self.step_strategies.get(step_number)

    def to_dict(self) -> dict:
        return {
            "test_case_id": self.test_case_id,
            "org_id": self.org_id,
            "has_any_overrides": self.has_any_overrides,
            "steps_with_overrides": self.steps_with_overrides,
            "total_steps": self.total_steps,
            "step_strategies": {
                str(sn): s.to_dict()
                for sn, s in self.step_strategies.items()
                if s.has_overrides()
            },
        }


# --- Strategy Decision Engine ---


def compute_step_strategy(
    step_number: int,
    step_profile: dict | None,
    flake_analysis: dict | None,
    timing_baseline: dict | None,
    selector_hints: list[dict] | None,
) -> StepStrategy:
    """Compute execution strategy for a single step.

    All inputs are dicts (from MongoDB documents), not Pydantic models,
    to avoid coupling with model changes.

    Args:
        step_number: The step number
        step_profile: StepProfile data from execution_profiles.step_profiles
        flake_analysis: StepFlakeAnalysis data from flake_report.flaky_steps
        timing_baseline: StepTimingBaseline data from timing_baselines.steps
        selector_hints: Known-good selectors from selector_memory

    Returns:
        StepStrategy with computed overrides
    """
    strategy = StepStrategy(step_number=step_number)

    # --- Selector priority override ---
    if selector_hints and len(selector_hints) > 1:
        _apply_selector_strategy(strategy, selector_hints, flake_analysis)

    # --- Retry budget override ---
    _apply_retry_strategy(strategy, step_profile, flake_analysis)

    # --- Wait budget multiplier ---
    _apply_wait_strategy(strategy, step_profile, timing_baseline, flake_analysis)

    # --- Aggressive stability mode ---
    _apply_stability_strategy(strategy, flake_analysis, step_profile)

    # --- Preemptive retry ---
    _apply_preemptive_retry_strategy(strategy, flake_analysis, step_profile)

    return strategy


def _apply_selector_strategy(
    strategy: StepStrategy,
    selector_hints: list[dict],
    flake_analysis: dict | None,
) -> None:
    """Rerank selectors based on flake patterns."""
    if not selector_hints:
        return

    is_selector_flaky = False
    if flake_analysis:
        flake_types = flake_analysis.get("flake_types", [])
        is_selector_flaky = "selector_flake" in flake_types

    if is_selector_flaky:
        # For selector-flaky steps, strongly prefer highest success rate
        # and deprioritize selectors with < 80% success
        reranked = sorted(
            selector_hints,
            key=lambda h: (-h.get("success_rate", 0), -h.get("uses", 0)),
        )
        # Only keep selectors above 70% success rate for flaky steps
        reranked = [h for h in reranked if h.get("success_rate", 0) >= 0.7] or reranked[:1]
        strategy.selector_priority_override = reranked
        strategy.reasons.append(
            f"selector_rerank: selector_flake detected, "
            f"filtered to {len(reranked)} high-success selectors"
        )


def _apply_retry_strategy(
    strategy: StepStrategy,
    step_profile: dict | None,
    flake_analysis: dict | None,
) -> None:
    """Determine retry budget based on reliability data."""
    if not step_profile and not flake_analysis:
        return

    observations = 0
    if step_profile:
        observations = step_profile.get("total_observations", 0)
    if observations < _MIN_OBSERVATIONS:
        return

    pass_rate = step_profile.get("pass_rate", 1.0) if step_profile else 1.0
    flake_score = flake_analysis.get("flake_score", 0.0) if flake_analysis else 0.0
    retry_rate = step_profile.get("retry_rate", 0.0) if step_profile else 0.0

    if flake_score >= _HIGH_FLAKE_THRESHOLD or pass_rate < _LOW_PASS_RATE_THRESHOLD:
        # High-risk step: increase retry budget
        strategy.retry_budget_override = 5
        strategy.reasons.append(
            f"retry_increase: flake={flake_score:.2f} pass_rate={pass_rate:.2f} "
            f"→ max_attempts=5"
        )
    elif pass_rate > 0.95 and retry_rate < 0.05 and flake_score < 0.05:
        # Very stable step: reduce retry budget to save time
        strategy.retry_budget_override = 2
        strategy.reasons.append(
            f"retry_reduce: pass_rate={pass_rate:.2f} retry_rate={retry_rate:.2f} "
            f"→ max_attempts=2"
        )


def _apply_wait_strategy(
    strategy: StepStrategy,
    step_profile: dict | None,
    timing_baseline: dict | None,
    flake_analysis: dict | None,
) -> None:
    """Scale wait budgets based on timing data."""
    if not timing_baseline:
        return

    sample_count = timing_baseline.get("sample_count", 0)
    if sample_count < _MIN_OBSERVATIONS:
        return

    median_ms = timing_baseline.get("median_ms", 0)
    p95_ms = timing_baseline.get("p95_ms", 0)

    # Always store P95 as timing baseline (used for adaptive wait budget)
    if p95_ms > 0:
        strategy.timing_baseline_ms = int(p95_ms)

    if median_ms <= 0:
        return

    timing_ratio = p95_ms / median_ms if median_ms > 0 else 1.0

    # Check for timing flake
    is_timing_flaky = False
    if flake_analysis:
        flake_types = flake_analysis.get("flake_types", [])
        is_timing_flaky = "timing_flake" in flake_types

    if is_timing_flaky:
        # Timing-flaky step: scale waits to P95
        multiplier = min(timing_ratio, 3.0)
        strategy.wait_budget_multiplier = round(max(multiplier, 1.5), 2)
        strategy.reasons.append(
            f"wait_increase: timing_flake p95/median={timing_ratio:.1f} "
            f"→ multiplier={strategy.wait_budget_multiplier}"
        )
    elif timing_ratio > _TIMING_VARIANCE_THRESHOLD:
        # High variance: moderate wait increase
        multiplier = min(timing_ratio * 0.6, 2.0)
        strategy.wait_budget_multiplier = round(max(multiplier, 1.2), 2)
        strategy.reasons.append(
            f"wait_increase: high_variance p95/median={timing_ratio:.1f} "
            f"→ multiplier={strategy.wait_budget_multiplier}"
        )


def _apply_stability_strategy(
    strategy: StepStrategy,
    flake_analysis: dict | None,
    step_profile: dict | None,
) -> None:
    """Enable aggressive stability mode for highly flaky steps."""
    if not flake_analysis:
        return

    flake_score = flake_analysis.get("flake_score", 0.0)
    observations = flake_analysis.get("total_observations", 0)

    if observations < _MIN_OBSERVATIONS:
        return

    if flake_score >= _AGGRESSIVE_STABILITY_THRESHOLD:
        strategy.aggressive_stability_mode = True
        strategy.reasons.append(
            f"aggressive_stability: flake_score={flake_score:.2f} >= {_AGGRESSIVE_STABILITY_THRESHOLD}"
        )


def _apply_preemptive_retry_strategy(
    strategy: StepStrategy,
    flake_analysis: dict | None,
    step_profile: dict | None,
) -> None:
    """Enable preemptive retry for intermittently flaky steps."""
    if not flake_analysis or not step_profile:
        return

    flake_types = flake_analysis.get("flake_types", [])
    flake_score = flake_analysis.get("flake_score", 0.0)
    observations = flake_analysis.get("total_observations", 0)

    if observations < _MIN_OBSERVATIONS:
        return

    # Only enable for intermittent flakes with high scores
    if "intermittent" in flake_types and flake_score >= _HIGH_FLAKE_THRESHOLD:
        avg_attempts = step_profile.get("avg_attempts", 1.0)
        if avg_attempts > 1.5:
            strategy.preemptive_retry_enabled = True
            strategy.reasons.append(
                f"preemptive_retry: intermittent flake_score={flake_score:.2f} "
                f"avg_attempts={avg_attempts:.1f}"
            )


def compute_run_strategy(
    profile_doc: dict | None,
    step_selector_hints: dict[int, list[dict]] | None = None,
) -> ExecutionStrategy | None:
    """Compute execution strategy for an entire test run.

    Args:
        profile_doc: Full execution_profiles document from MongoDB
        step_selector_hints: Per-step selector hints (from selector_memory)

    Returns:
        ExecutionStrategy with per-step overrides, or None if no profile exists
    """
    if not profile_doc:
        return None

    test_case_id = profile_doc.get("test_case_id", "")
    org_id = profile_doc.get("org_id", "")

    # Index step profiles by step_number
    step_profiles_by_num: dict[int, dict] = {}
    for sp in profile_doc.get("step_profiles", []):
        sn = sp.get("step_number", 0)
        if sn > 0:
            step_profiles_by_num[sn] = sp

    # Index flaky step analyses by step_number
    flake_report = profile_doc.get("flake_report", {})
    flaky_by_num: dict[int, dict] = {}
    for fs in flake_report.get("flaky_steps", []):
        sn = fs.get("step_number", 0)
        if sn > 0:
            flaky_by_num[sn] = fs

    # Index timing baselines by step_number
    timing_baselines = profile_doc.get("timing_baselines", {})
    timing_steps = timing_baselines.get("steps", {})

    # Collect all known step numbers
    all_steps = set(step_profiles_by_num.keys())
    all_steps |= set(flaky_by_num.keys())
    all_steps |= {int(sn) for sn in timing_steps.keys()}
    if step_selector_hints:
        all_steps |= set(step_selector_hints.keys())

    strategy = ExecutionStrategy(
        test_case_id=test_case_id,
        org_id=org_id,
        total_steps=len(all_steps),
    )

    for sn in sorted(all_steps):
        step_strategy = compute_step_strategy(
            step_number=sn,
            step_profile=step_profiles_by_num.get(sn),
            flake_analysis=flaky_by_num.get(sn),
            timing_baseline=timing_steps.get(str(sn)),
            selector_hints=(step_selector_hints or {}).get(sn),
        )

        if step_strategy.has_overrides():
            strategy.step_strategies[sn] = step_strategy

    strategy.steps_with_overrides = len(strategy.step_strategies)
    strategy.has_any_overrides = strategy.steps_with_overrides > 0

    if strategy.has_any_overrides:
        logger.info(
            "strategy.computed",
            test_case_id=test_case_id,
            org_id=org_id,
            steps_with_overrides=strategy.steps_with_overrides,
            total_steps=strategy.total_steps,
            overrides={
                sn: s.reasons for sn, s in strategy.step_strategies.items()
            },
        )

    return strategy
