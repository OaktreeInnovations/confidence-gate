"""Z-score based anomaly detection across retries, timing, and flake signals."""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


def _z_score(current: float, mean: float, stddev: float) -> float:
    return (current - mean) / max(stddev, 0.1)


def detect_anomalies(
    runs: list[dict],
    telemetry_docs: dict[str, dict],
    profiles: dict[str, dict],
) -> list[dict]:
    anomalies: list[dict] = []

    if not runs:
        return anomalies

    try:
        # --- 1. Retry spike: current avg_attempts vs profile baseline ---
        for tc_id, profile in profiles.items():
            step_profiles = profile.get("step_profiles", [])
            if not step_profiles:
                continue

            # Collect current avg_attempts from telemetry for this test case
            current_attempts: list[float] = []
            for run in runs:
                if str(run.get("test_case_id", "")) != tc_id:
                    continue
                rid = str(run.get("_id", run.get("test_run_id", "")))
                tel_doc = telemetry_docs.get(rid)
                if not tel_doc:
                    continue
                for step in tel_doc.get("steps", []):
                    attempts = step.get("attempts", [])
                    if attempts:
                        current_attempts.append(len(attempts))

            if not current_attempts:
                continue

            current_avg = sum(current_attempts) / len(current_attempts)

            baseline_avgs = [sp.get("avg_attempts", 1.0) for sp in step_profiles]
            if not baseline_avgs:
                continue

            baseline_mean = sum(baseline_avgs) / len(baseline_avgs)
            if len(baseline_avgs) >= 2:
                variance = sum((x - baseline_mean) ** 2 for x in baseline_avgs) / len(baseline_avgs)
                baseline_stddev = variance ** 0.5
            else:
                baseline_stddev = 0.1

            z = _z_score(current_avg, baseline_mean, baseline_stddev)

            if z > 2.0:
                severity = "high"
            elif z > 1.5:
                severity = "medium"
            else:
                continue

            anomalies.append({
                "type": "retry_spike",
                "metric": "avg_attempts",
                "severity": severity,
                "deviation": round(z, 3),
                "detail": (
                    f"Test {tc_id}: avg attempts {current_avg:.2f} vs baseline {baseline_mean:.2f} "
                    f"(z={z:.2f})"
                ),
            })

        # --- 2. Timing anomaly: current duration vs profile median ---
        tc_durations: dict[str, list[int]] = {}
        for run in runs:
            tc_id = str(run.get("test_case_id", ""))
            dur = run.get("duration_ms", 0)
            if tc_id and dur:
                tc_durations.setdefault(tc_id, []).append(dur)

        for tc_id, durations in tc_durations.items():
            profile = profiles.get(tc_id)
            if not profile:
                continue

            median_dur = profile.get("median_duration_ms", 0)
            if median_dur == 0:
                continue

            timing_baselines = profile.get("timing_baselines", {})
            stddev = timing_baselines.get("stddev_ms", 0) if timing_baselines else 0

            current_avg_dur = sum(durations) / len(durations)
            z = _z_score(current_avg_dur, median_dur, max(stddev, 1))

            if z > 2.5:
                severity = "high"
            elif z > 1.8:
                severity = "medium"
            else:
                continue

            anomalies.append({
                "type": "timing_anomaly",
                "metric": "duration_ms",
                "severity": severity,
                "deviation": round(z, 3),
                "detail": (
                    f"Test {tc_id}: avg duration {current_avg_dur:.0f}ms vs median {median_dur}ms "
                    f"(z={z:.2f})"
                ),
            })

        # --- 3. Flake spike: current failure rate vs profile flake_rate ---
        tc_outcomes: dict[str, list[bool]] = {}
        for run in runs:
            tc_id = str(run.get("test_case_id", ""))
            if tc_id:
                passed = run.get("status", "") == "passed"
                tc_outcomes.setdefault(tc_id, []).append(passed)

        for tc_id, outcomes in tc_outcomes.items():
            profile = profiles.get(tc_id)
            if not profile:
                continue

            total_runs_profile = profile.get("total_runs", 0)
            if total_runs_profile < 3:
                continue

            flake_rate = profile.get("flake_rate", 0.0)
            current_failure_rate = sum(1 for p in outcomes if not p) / len(outcomes)

            if current_failure_rate > flake_rate + 0.3:
                anomalies.append({
                    "type": "flake_spike",
                    "metric": "failure_rate",
                    "severity": "high",
                    "deviation": round(current_failure_rate - flake_rate, 4),
                    "detail": (
                        f"Test {tc_id}: current failure rate {current_failure_rate:.0%} exceeds "
                        f"profile flake rate {flake_rate:.0%} by more than 30%"
                    ),
                })

    except Exception as e:
        logger.warning("anomaly_engine.error", error=str(e)[:200])

    return anomalies
