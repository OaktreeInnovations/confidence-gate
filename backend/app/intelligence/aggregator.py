"""Execution profile aggregation from existing telemetry data.

Queries test_runs + execution_telemetry collections to build an
ExecutionProfile for a given test case. Uses sync pymongo for
Celery worker compatibility.

All functions are non-fatal — they log warnings on failure but
never raise, so aggregation infrastructure cannot break execution.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

from bson import ObjectId
from pymongo.database import Database
import structlog

from app.intelligence.flake_detector import compute_flake_report
from app.intelligence.timing_analyzer import compute_timing_baselines
from app.intelligence.models import (
    ExecutionProfile,
    FailureRecord,
    StepProfile,
)

logger = structlog.get_logger(__name__)


def _percentile(sorted_values: list[int | float], pct: float) -> int:
    """Compute percentile from a pre-sorted list. Returns 0 for empty lists."""
    if not sorted_values:
        return 0
    n = len(sorted_values)
    idx = int(pct / 100.0 * (n - 1))
    return int(sorted_values[min(idx, n - 1)])


def compute_execution_profile(
    db: Database,
    test_case_id: str,
    org_id: str,
    window_size: int = 20,
) -> ExecutionProfile:
    """Compute an ExecutionProfile for a test case from recent runs.

    Joins test_runs with execution_telemetry to build per-step profiles
    with timing distributions, failure classifications, and selector stats.

    Args:
        db: sync pymongo Database instance
        test_case_id: ObjectId string of the test case
        org_id: organization ID for multi-tenant isolation
        window_size: number of recent runs to analyze

    Returns:
        ExecutionProfile with aggregated metrics
    """
    # Fetch recent completed runs for this test case
    tc_oid = ObjectId(test_case_id)
    runs = list(
        db.test_runs.find(
            {
                "test_case_id": tc_oid,
                "org_id": ObjectId(org_id),
                "status": {"$in": ["passed", "failed", "error"]},
            },
            sort=[("created_at", -1)],
            limit=window_size,
        )
    )

    if not runs:
        logger.info("aggregator.no_runs", test_case_id=test_case_id)
        return ExecutionProfile(
            test_case_id=test_case_id,
            org_id=org_id,
            window_size=window_size,
        )

    run_ids = [str(r["_id"]) for r in runs]
    total_runs = len(runs)

    # Fetch telemetry for these runs
    telemetry_docs = {
        doc["test_run_id"]: doc
        for doc in db.execution_telemetry.find(
            {"test_run_id": {"$in": run_ids}},
        )
    }

    # --- Run-level aggregation ---
    passed_count = sum(1 for r in runs if r.get("status") == "passed")
    error_count = sum(1 for r in runs if r.get("status") == "error")
    flake_count = sum(
        1
        for rid in run_ids
        if rid in telemetry_docs
        and telemetry_docs[rid].get("flake_indicators")
    )

    durations = sorted(r.get("duration_ms", 0) for r in runs if r.get("duration_ms"))

    pass_rate = passed_count / total_runs if total_runs else 0.0
    flake_rate = flake_count / total_runs if total_runs else 0.0
    error_rate = error_count / total_runs if total_runs else 0.0

    # --- Per-step aggregation ---
    # Collect step data across all runs
    # Key: step_number -> list of step observations
    step_data: dict[int, list[dict]] = defaultdict(list)
    step_actions: dict[int, str] = {}

    for run in runs:
        rid = str(run["_id"])
        results = run.get("results", [])
        tel_doc = telemetry_docs.get(rid)
        tel_steps = {}
        if tel_doc:
            for ts in tel_doc.get("steps", []):
                tel_steps[ts.get("step_number", 0)] = ts

        for result in results:
            sn = result.get("step_number", 0)
            if sn == 0:
                continue

            step_actions.setdefault(sn, result.get("action", ""))

            observation = {
                "status": result.get("status", ""),
                "duration_ms": result.get("duration_ms", 0),
            }

            # Enrich with telemetry if available
            ts = tel_steps.get(sn)
            if ts:
                timings = ts.get("timings", {})
                observation["exec_ms"] = timings.get("total_ms", 0)
                observation["total_attempts"] = ts.get("total_attempts", 1)
                observation["selectors_tried"] = ts.get("selectors_tried", [])
                observation["selector_that_worked"] = ts.get("selector_that_worked", "")

                # Extract failure reasons from attempts
                attempts = ts.get("attempts", [])
                observation["failure_reasons"] = [
                    a.get("error_type", "unknown")
                    for a in attempts
                    if not a.get("success", False) and a.get("error_type")
                ]
                observation["failure_messages"] = [
                    a.get("error_message", "")
                    for a in attempts
                    if not a.get("success", False) and a.get("error_message")
                ]
                # Behavior failure signals for BEHAVIOR_FLAKE classification
                observation["behavior_failures"] = sum(
                    a.get("behavior_failures", 0) for a in attempts
                )
                observation["behavior_failed"] = any(
                    a.get("behavior_failed", False) for a in attempts
                )
                observation["verification_level"] = ts.get("verification_level", "")
            else:
                observation["exec_ms"] = observation["duration_ms"]
                observation["total_attempts"] = 1
                observation["selectors_tried"] = []
                observation["selector_that_worked"] = ""
                observation["failure_reasons"] = []
                observation["failure_messages"] = []
                observation["behavior_failures"] = 0
                observation["behavior_failed"] = False
                observation["verification_level"] = ""

            step_data[sn].append(observation)

    # Build step profiles
    step_profiles = []
    weakest_step = None
    worst_pass_rate = 1.1  # sentinel

    for sn in sorted(step_data.keys()):
        observations = step_data[sn]
        n = len(observations)

        # Timing
        exec_times = sorted(o["exec_ms"] for o in observations if o["exec_ms"] > 0)
        median_ms = int(statistics.median(exec_times)) if exec_times else 0
        p95_ms = _percentile(exec_times, 95)
        min_ms = exec_times[0] if exec_times else 0
        max_ms = exec_times[-1] if exec_times else 0

        # Reliability
        step_passed = sum(1 for o in observations if o["status"] == "passed")
        step_pass_rate = step_passed / n if n else 0.0
        needed_retry = sum(1 for o in observations if o["total_attempts"] > 1)
        step_retry_rate = needed_retry / n if n else 0.0
        avg_attempts = (
            statistics.mean(o["total_attempts"] for o in observations)
            if observations else 1.0
        )

        # Failure analysis
        all_failures: Counter[str] = Counter()
        failure_examples: dict[str, str] = {}
        for o in observations:
            for reason in o["failure_reasons"]:
                all_failures[reason] += 1
            for reason, msg in zip(o["failure_reasons"], o["failure_messages"]):
                if reason not in failure_examples and msg:
                    failure_examples[reason] = msg[:200]

        common_failures = [
            FailureRecord(
                reason=reason,
                count=count,
                example_message=failure_examples.get(reason, ""),
            )
            for reason, count in all_failures.most_common(5)
        ]

        # Selector analysis
        selector_successes: Counter[str] = Counter()
        selector_appearances: Counter[str] = Counter()
        successful_selectors_set: set[str] = set()

        for o in observations:
            for sel in o["selectors_tried"]:
                selector_appearances[sel] += 1
            worked = o["selector_that_worked"]
            if worked:
                selector_successes[worked] += 1
                successful_selectors_set.add(worked)

        selector_success_rates = {}
        for sel in selector_appearances:
            total = selector_appearances[sel]
            successes = selector_successes.get(sel, 0)
            selector_success_rates[sel] = round(successes / total, 3) if total else 0.0

        sp = StepProfile(
            step_number=sn,
            action=step_actions.get(sn, ""),
            median_exec_ms=median_ms,
            p95_exec_ms=p95_ms,
            min_exec_ms=min_ms,
            max_exec_ms=max_ms,
            pass_rate=round(step_pass_rate, 4),
            retry_rate=round(step_retry_rate, 4),
            avg_attempts=round(avg_attempts, 2),
            most_common_failures=common_failures,
            successful_selectors=sorted(successful_selectors_set),
            selector_success_rates=selector_success_rates,
            total_observations=n,
        )
        step_profiles.append(sp)

        # Ambiguous step detection: flag steps that fail frequently with
        # code_gen_failure, unknown, or assertion_failure — likely bad step text
        if sp.pass_rate < 0.5 and sp.total_observations >= 5 and sp.most_common_failures:
            top = sp.most_common_failures[0]
            if top.reason in ("code_gen_failure", "unknown", "assertion_failure"):
                sp.ambiguous_step_flag = True
                sp.ambiguous_step_reason = (
                    f"Step fails {1 - sp.pass_rate:.0%} of the time "
                    f"(top failure: {top.reason}, {top.count}/{sp.total_observations} runs). "
                    f"Consider rewriting the step description."
                )

        # Track weakest step
        if step_pass_rate < worst_pass_rate:
            worst_pass_rate = step_pass_rate
            weakest_step = sn

    # --- Reliability trend ---
    # Compare pass rate of first half vs second half of the window
    reliability_trend = 0.0
    if total_runs >= 4:
        mid = total_runs // 2
        # runs are sorted newest-first, so recent = runs[:mid], older = runs[mid:]
        recent_passed = sum(1 for r in runs[:mid] if r.get("status") == "passed")
        older_passed = sum(1 for r in runs[mid:] if r.get("status") == "passed")
        recent_rate = recent_passed / mid
        older_rate = older_passed / (total_runs - mid)
        reliability_trend = round(recent_rate - older_rate, 4)

    # --- Flake detection ---
    flake_report_data = {}
    try:
        flake_report = compute_flake_report(
            step_data=step_data,
            step_actions=step_actions,
            test_case_id=test_case_id,
            org_id=org_id,
            total_runs=total_runs,
        )
        flake_report_data = flake_report.to_dict()
    except Exception as e:
        logger.warning("aggregator.flake_detection_failed", error=str(e)[:200])

    # --- Timing baselines ---
    timing_baselines_data = {}
    try:
        timing_baselines = compute_timing_baselines(
            step_data=step_data,
            step_actions=step_actions,
            test_case_id=test_case_id,
            org_id=org_id,
            total_runs=total_runs,
            run_durations=durations,
        )
        timing_baselines_data = timing_baselines.to_dict()
    except Exception as e:
        logger.warning("aggregator.timing_baselines_failed", error=str(e)[:200])

    profile = ExecutionProfile(
        test_case_id=test_case_id,
        org_id=org_id,
        window_size=window_size,
        total_runs=total_runs,
        pass_rate=round(pass_rate, 4),
        flake_rate=round(flake_rate, 4),
        error_rate=round(error_rate, 4),
        median_duration_ms=int(statistics.median(durations)) if durations else 0,
        p95_duration_ms=_percentile(sorted(durations), 95) if durations else 0,
        min_duration_ms=min(durations) if durations else 0,
        max_duration_ms=max(durations) if durations else 0,
        step_profiles=step_profiles,
        weakest_step=weakest_step,
        reliability_trend=reliability_trend,
        flake_report=flake_report_data,
        timing_baselines=timing_baselines_data,
        run_ids=run_ids,
        last_updated=datetime.now(timezone.utc),
    )

    logger.info(
        "aggregator.profile_computed",
        test_case_id=test_case_id,
        total_runs=total_runs,
        pass_rate=profile.pass_rate,
        flake_rate=profile.flake_rate,
        weakest_step=weakest_step,
        trend=reliability_trend,
    )

    return profile


def store_execution_profile(db: Database, profile: ExecutionProfile) -> None:
    """Upsert an ExecutionProfile into the execution_profiles collection."""
    doc = profile.model_dump(by_alias=True, exclude={"id"})
    db.execution_profiles.update_one(
        {"test_case_id": profile.test_case_id, "org_id": profile.org_id},
        {"$set": doc},
        upsert=True,
    )
    logger.info(
        "aggregator.profile_stored",
        test_case_id=profile.test_case_id,
    )
