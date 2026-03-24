"""Timing baseline and anomaly detection.

Computes per-step timing baselines (median, P95, stddev) from historical
telemetry data. Flags anomalies when current execution significantly
exceeds the baseline — useful for detecting performance regressions
and timing-dependent flakiness.

Anomaly threshold: step_time > median + 3 * stddev (or 3x median if stddev is 0).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class StepTimingBaseline:
    """Timing baseline for a single step."""
    step_number: int
    action: str = ""
    median_ms: int = 0
    p95_ms: int = 0
    mean_ms: int = 0
    stddev_ms: int = 0
    min_ms: int = 0
    max_ms: int = 0
    sample_count: int = 0

    # Anomaly threshold (ms) — times above this are flagged
    anomaly_threshold_ms: int = 0

    def is_anomalous(self, exec_ms: int) -> bool:
        """Check if an execution time is anomalous."""
        if self.sample_count < 3:
            return False  # Not enough data
        return exec_ms > self.anomaly_threshold_ms

    def to_dict(self) -> dict:
        return {
            "step_number": self.step_number,
            "action": self.action,
            "median_ms": self.median_ms,
            "p95_ms": self.p95_ms,
            "mean_ms": self.mean_ms,
            "stddev_ms": self.stddev_ms,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "sample_count": self.sample_count,
            "anomaly_threshold_ms": self.anomaly_threshold_ms,
        }


@dataclass
class TimingBaselines:
    """Timing baselines for all steps of a test case."""
    test_case_id: str
    org_id: str
    total_runs: int = 0
    run_median_ms: int = 0
    run_p95_ms: int = 0
    run_anomaly_threshold_ms: int = 0
    step_baselines: dict[int, StepTimingBaseline] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "test_case_id": self.test_case_id,
            "org_id": self.org_id,
            "total_runs": self.total_runs,
            "run_median_ms": self.run_median_ms,
            "run_p95_ms": self.run_p95_ms,
            "run_anomaly_threshold_ms": self.run_anomaly_threshold_ms,
            "steps": {
                str(sn): b.to_dict()
                for sn, b in self.step_baselines.items()
            },
        }

    def check_anomalies(self, step_timings: dict[int, int]) -> list[dict]:
        """Check step timings against baselines, return list of anomalies.

        Args:
            step_timings: dict of step_number -> execution_ms

        Returns:
            List of dicts with anomaly details
        """
        anomalies = []
        for sn, exec_ms in step_timings.items():
            baseline = self.step_baselines.get(sn)
            if baseline and baseline.is_anomalous(exec_ms):
                anomalies.append({
                    "step_number": sn,
                    "action": baseline.action,
                    "exec_ms": exec_ms,
                    "median_ms": baseline.median_ms,
                    "threshold_ms": baseline.anomaly_threshold_ms,
                    "ratio": round(exec_ms / baseline.median_ms, 2) if baseline.median_ms else 0,
                })
        return anomalies


def _percentile(sorted_values: list[int], pct: float) -> int:
    """Compute percentile from a pre-sorted list."""
    if not sorted_values:
        return 0
    n = len(sorted_values)
    idx = int(pct / 100.0 * (n - 1))
    return int(sorted_values[min(idx, n - 1)])


def _compute_anomaly_threshold(median: int, stddev: int, multiplier: float = 3.0) -> int:
    """Compute anomaly threshold: median + multiplier * stddev.

    If stddev is 0 (all same values), fall back to multiplier * median.
    """
    if stddev > 0:
        return int(median + multiplier * stddev)
    return int(median * multiplier) if median > 0 else 0


def compute_timing_baselines(
    step_data: dict[int, list[dict]],
    step_actions: dict[int, str],
    test_case_id: str,
    org_id: str,
    total_runs: int,
    run_durations: list[int] | None = None,
) -> TimingBaselines:
    """Compute timing baselines for all steps from historical data.

    Args:
        step_data: Dict of step_number -> list of observations (from aggregator)
        step_actions: Dict of step_number -> action text
        test_case_id: The test case ID
        org_id: The org ID
        total_runs: Total number of runs analyzed
        run_durations: Optional list of run-level durations for overall baseline
    """
    baselines = TimingBaselines(
        test_case_id=test_case_id,
        org_id=org_id,
        total_runs=total_runs,
    )

    # Run-level baseline
    if run_durations:
        sorted_durations = sorted(run_durations)
        baselines.run_median_ms = int(statistics.median(sorted_durations))
        baselines.run_p95_ms = _percentile(sorted_durations, 95)
        stddev = int(statistics.stdev(sorted_durations)) if len(sorted_durations) >= 2 else 0
        baselines.run_anomaly_threshold_ms = _compute_anomaly_threshold(
            baselines.run_median_ms, stddev
        )

    # Per-step baselines
    for sn in sorted(step_data.keys()):
        observations = step_data[sn]
        exec_times = sorted(
            o["exec_ms"] for o in observations if o.get("exec_ms", 0) > 0
        )

        if not exec_times:
            continue

        median = int(statistics.median(exec_times))
        mean = int(statistics.mean(exec_times))
        stddev = int(statistics.stdev(exec_times)) if len(exec_times) >= 2 else 0

        baselines.step_baselines[sn] = StepTimingBaseline(
            step_number=sn,
            action=step_actions.get(sn, ""),
            median_ms=median,
            p95_ms=_percentile(exec_times, 95),
            mean_ms=mean,
            stddev_ms=stddev,
            min_ms=exec_times[0],
            max_ms=exec_times[-1],
            sample_count=len(exec_times),
            anomaly_threshold_ms=_compute_anomaly_threshold(median, stddev),
        )

    logger.info(
        "timing_analyzer.baselines_computed",
        test_case_id=test_case_id,
        step_count=len(baselines.step_baselines),
        run_median_ms=baselines.run_median_ms,
    )

    return baselines
