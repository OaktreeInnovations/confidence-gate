"""Cross-run flake detection engine.

Analyzes historical execution telemetry to identify flaky test steps
and classify the type of flakiness. A step is flaky if it produces
inconsistent results across runs despite identical inputs.

Flake classification:
- RETRY_FLAKE: Step passes after retry (passes but unreliably)
- INTERMITTENT: Step passes in some runs, fails in others
- SELECTOR_FLAKE: Failures caused by selector instability
- TIMING_FLAKE: Failures correlated with execution time
- NETWORK_FLAKE: Failures caused by network instability
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

logger = structlog.get_logger(__name__)


class FlakeType(StrEnum):
    RETRY_FLAKE = "retry_flake"         # Passes after retry
    INTERMITTENT = "intermittent"        # Sometimes passes, sometimes fails
    SELECTOR_FLAKE = "selector_flake"    # Selector-related failures
    TIMING_FLAKE = "timing_flake"        # Timing-dependent failures
    NETWORK_FLAKE = "network_flake"      # Network-related failures
    BEHAVIOR_FLAKE = "behavior_flake"    # Action executed but no DOM effect in >30% of runs


# Map RetryReason values to FlakeType
_REASON_TO_FLAKE_TYPE = {
    "selector_not_found": FlakeType.SELECTOR_FLAKE,
    "selector_not_visible": FlakeType.SELECTOR_FLAKE,
    "selector_ambiguous": FlakeType.SELECTOR_FLAKE,
    "navigation_timeout": FlakeType.TIMING_FLAKE,
    "network_error": FlakeType.NETWORK_FLAKE,
    "element_detached": FlakeType.SELECTOR_FLAKE,
}


@dataclass
class StepFlakeAnalysis:
    """Flake analysis for a single step across runs."""
    step_number: int
    action: str = ""

    # Core metrics
    flake_score: float = 0.0     # 0.0 (stable) to 1.0 (always flaky)
    is_flaky: bool = False       # flake_score > threshold

    # Flake classification
    flake_types: list[str] = field(default_factory=list)
    primary_flake_type: str = ""

    # Evidence
    retry_flake_count: int = 0      # Runs where step passed after retry
    intermittent_failures: int = 0  # Runs where step failed
    total_observations: int = 0
    behavior_flake_count: int = 0   # Runs where behavior_failed was True

    # Failure pattern breakdown
    failure_reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "step_number": self.step_number,
            "action": self.action,
            "flake_score": round(self.flake_score, 4),
            "is_flaky": self.is_flaky,
            "flake_types": self.flake_types,
            "primary_flake_type": self.primary_flake_type,
            "retry_flake_count": self.retry_flake_count,
            "intermittent_failures": self.intermittent_failures,
            "total_observations": self.total_observations,
            "behavior_flake_count": self.behavior_flake_count,
            "failure_reasons": self.failure_reasons,
        }


@dataclass
class FlakeReport:
    """Flake analysis for an entire test case."""
    test_case_id: str
    org_id: str
    total_runs: int = 0
    overall_flake_score: float = 0.0
    flaky_steps: list[StepFlakeAnalysis] = field(default_factory=list)
    stable_steps: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "test_case_id": self.test_case_id,
            "org_id": self.org_id,
            "total_runs": self.total_runs,
            "overall_flake_score": round(self.overall_flake_score, 4),
            "flaky_step_count": len(self.flaky_steps),
            "stable_step_count": len(self.stable_steps),
            "flaky_steps": [s.to_dict() for s in self.flaky_steps],
            "stable_steps": self.stable_steps,
        }


def analyze_step_flakiness(
    step_observations: list[dict],
    step_number: int,
    action: str = "",
    flake_threshold: float = 0.15,
) -> StepFlakeAnalysis:
    """Analyze flakiness for a single step from its cross-run observations.

    Each observation is a dict from the aggregator's step_data:
        - status: "passed" | "failed" | "error"
        - total_attempts: int
        - failure_reasons: list[str]

    Args:
        step_observations: List of per-run observations for this step
        step_number: The step number
        action: Step action description
        flake_threshold: Score above which a step is considered flaky

    Returns:
        StepFlakeAnalysis with flake classification
    """
    n = len(step_observations)
    if n == 0:
        return StepFlakeAnalysis(step_number=step_number, action=action)

    analysis = StepFlakeAnalysis(
        step_number=step_number,
        action=action,
        total_observations=n,
    )

    # Count retry flakes (passed after retry)
    retry_flakes = sum(
        1 for o in step_observations
        if o.get("status") == "passed" and o.get("total_attempts", 1) > 1
    )
    analysis.retry_flake_count = retry_flakes

    # Count intermittent failures
    failures = sum(
        1 for o in step_observations
        if o.get("status") in ("failed", "error")
    )
    passes = sum(
        1 for o in step_observations
        if o.get("status") == "passed"
    )
    analysis.intermittent_failures = failures

    # Count behavior flakes — runs where action had no observable DOM effect
    behavior_flakes = sum(
        1 for o in step_observations
        if o.get("behavior_failed", False) or o.get("behavior_failures", 0) > 0
    )
    analysis.behavior_flake_count = behavior_flakes

    # Aggregate failure reasons
    reason_counts: Counter[str] = Counter()
    for o in step_observations:
        for reason in o.get("failure_reasons", []):
            reason_counts[reason] += 1
    analysis.failure_reasons = dict(reason_counts.most_common(10))

    # --- Compute flake score ---
    # Score components (weighted):
    # 1. Retry rate: steps that need retries are potentially flaky
    retry_rate = retry_flakes / n if n else 0.0

    # 2. Intermittent rate: mix of passes and failures = flaky
    # If ALL pass or ALL fail, it's not flaky (it's stable or broken)
    intermittent_rate = 0.0
    if passes > 0 and failures > 0:
        # Shannon entropy-inspired: max flakiness when 50/50
        minority_fraction = min(passes, failures) / n
        intermittent_rate = minority_fraction * 2  # Normalize to 0-1 range

    # 3. Multi-attempt rate: average extra attempts beyond 1
    avg_extra_attempts = (
        sum(max(0, o.get("total_attempts", 1) - 1) for o in step_observations) / n
    )
    multi_attempt_score = min(avg_extra_attempts / 2, 1.0)  # Cap at 1.0

    # 4. Behavior failure rate: fraction of runs where action had no DOM effect
    behavior_failure_rate = behavior_flakes / n if n else 0.0

    # Weighted combination (behavior_failure_rate is an independent signal)
    analysis.flake_score = (
        0.30 * retry_rate +
        0.40 * intermittent_rate +
        0.15 * multi_attempt_score +
        0.15 * behavior_failure_rate
    )
    analysis.is_flaky = analysis.flake_score > flake_threshold

    # --- Classify flake type ---
    flake_types_detected: list[str] = []

    if retry_flakes > 0:
        flake_types_detected.append(FlakeType.RETRY_FLAKE)

    if passes > 0 and failures > 0:
        flake_types_detected.append(FlakeType.INTERMITTENT)

    # Behavior flake: action had no DOM effect in >30% of runs
    if behavior_failure_rate > 0.30:
        flake_types_detected.append(FlakeType.BEHAVIOR_FLAKE)

    # Classify by failure reason patterns
    for reason, count in reason_counts.items():
        flake_type = _REASON_TO_FLAKE_TYPE.get(reason)
        if flake_type and flake_type not in flake_types_detected:
            flake_types_detected.append(flake_type)

    analysis.flake_types = flake_types_detected
    analysis.primary_flake_type = flake_types_detected[0] if flake_types_detected else ""

    return analysis


def compute_flake_report(
    step_data: dict[int, list[dict]],
    step_actions: dict[int, str],
    test_case_id: str,
    org_id: str,
    total_runs: int,
    flake_threshold: float = 0.15,
) -> FlakeReport:
    """Compute a full flake report for a test case.

    Args:
        step_data: Dict of step_number -> list of observations (from aggregator)
        step_actions: Dict of step_number -> action text
        test_case_id: The test case ID
        org_id: The org ID
        total_runs: Total number of runs analyzed
        flake_threshold: Score above which a step is considered flaky

    Returns:
        FlakeReport with per-step flake analysis
    """
    report = FlakeReport(
        test_case_id=test_case_id,
        org_id=org_id,
        total_runs=total_runs,
    )

    step_analyses = []
    for sn in sorted(step_data.keys()):
        analysis = analyze_step_flakiness(
            step_observations=step_data[sn],
            step_number=sn,
            action=step_actions.get(sn, ""),
            flake_threshold=flake_threshold,
        )
        step_analyses.append(analysis)

    report.flaky_steps = [a for a in step_analyses if a.is_flaky]
    report.stable_steps = [a.step_number for a in step_analyses if not a.is_flaky]

    # Overall flake score: weighted average favoring worst steps
    if step_analyses:
        scores = [a.flake_score for a in step_analyses]
        # Use max-weighted average: overall is pulled toward the worst step
        max_score = max(scores)
        avg_score = sum(scores) / len(scores)
        report.overall_flake_score = 0.6 * max_score + 0.4 * avg_score

    logger.info(
        "flake_detector.report",
        test_case_id=test_case_id,
        overall_score=round(report.overall_flake_score, 4),
        flaky_count=len(report.flaky_steps),
        stable_count=len(report.stable_steps),
    )

    return report
