"""Measures how much to trust the release confidence score based on data quality."""

from __future__ import annotations

import statistics

import structlog

logger = structlog.get_logger(__name__)


def compute_score_confidence(
    runs: list[dict],
    telemetry_docs: dict[str, dict],
    profiles: dict[str, dict],
) -> dict:
    total_runs = len(runs)
    if total_runs == 0:
        return {"score_confidence": 0.0, "data_quality": "LOW"}

    try:
        # 1. Telemetry completeness
        run_ids = [str(r.get("_id", r.get("test_run_id", ""))) for r in runs]
        runs_with_telemetry = sum(1 for rid in run_ids if rid in telemetry_docs)
        telemetry_completeness = runs_with_telemetry / total_runs

        # 2. Step coverage
        total_steps = sum(
            len(doc.get("steps", [])) for doc in telemetry_docs.values()
        )
        step_coverage = min(1.0, total_steps / 20.0)

        # 3. Signal consistency — variance of per-step pass rates
        step_pass_rates: list[float] = []
        for doc in telemetry_docs.values():
            for step in doc.get("steps", []):
                attempts = step.get("attempts", [])
                if not attempts:
                    continue
                passed = sum(1 for a in attempts if a.get("success", False))
                step_pass_rates.append(passed / len(attempts))

        if len(step_pass_rates) >= 3:
            variance = statistics.variance(step_pass_rates)
            signal_consistency = max(0.0, 1.0 - variance)
        else:
            signal_consistency = 0.5

        # 4. Profile coverage
        unique_tc_ids: set[str] = set()
        for run in runs:
            tc_id = str(run.get("test_case_id", ""))
            if tc_id:
                unique_tc_ids.add(tc_id)

        total_unique = len(unique_tc_ids)
        if total_unique == 0:
            profile_coverage = 0.0
        else:
            profiles_with_runs = sum(
                1 for tc_id in unique_tc_ids
                if profiles.get(tc_id, {}).get("total_runs", 0) >= 1
            )
            profile_coverage = profiles_with_runs / total_unique

        score_confidence = round(
            (telemetry_completeness + step_coverage + signal_consistency + profile_coverage) / 4.0,
            4,
        )

        if score_confidence >= 0.75:
            data_quality = "HIGH"
        elif score_confidence >= 0.45:
            data_quality = "MEDIUM"
        else:
            data_quality = "LOW"

        return {"score_confidence": score_confidence, "data_quality": data_quality}

    except Exception as e:
        logger.warning("score_confidence_engine.error", error=str(e)[:200])
        return {"score_confidence": 0.0, "data_quality": "LOW"}
