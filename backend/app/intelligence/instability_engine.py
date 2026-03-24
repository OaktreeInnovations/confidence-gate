"""Unified instability index aggregating retry, healing, and behavior failure signals."""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


def compute_instability(
    runs: list[dict],
    telemetry_docs: dict[str, dict],
) -> dict:
    default = {
        "instability_index": 0,
        "instability_penalty": 0.0,
        "components": {
            "retry_rate": 0.0,
            "healing_rate": 0.0,
            "behavior_failure_rate": 0.0,
            "resolution_retry_rate": 0.0,
        },
    }

    if not runs and not telemetry_docs:
        return default

    try:
        total_attempts = 0
        total_retries = 0
        total_behavior_failures = 0
        total_steps = 0
        healing_steps = 0          # steps where avg attempts > 2
        resolution_retry_steps = 0  # steps with 3+ attempts

        for doc in telemetry_docs.values():
            for step in doc.get("steps", []):
                attempts = step.get("attempts", [])
                if not attempts:
                    continue

                total_steps += 1
                step_attempts = len(attempts)
                total_attempts += step_attempts

                # retries: attempts where attempt_number > 1
                retries_in_step = sum(
                    1 for a in attempts if a.get("attempt_number", 1) > 1
                )
                total_retries += retries_in_step

                # behavior failures
                total_behavior_failures += sum(
                    a.get("behavior_failures", 0) for a in attempts
                )

                # healing: avg attempts > 2
                avg_attempts = step_attempts / 1  # step_attempts already is total
                # Use total_attempts from the step field if available, else count attempts list
                recorded_avg = step.get("total_attempts", step_attempts)
                if recorded_avg > 2:
                    healing_steps += 1

                # resolution retry: 3+ attempts
                if step_attempts >= 3:
                    resolution_retry_steps += 1

        retry_rate = total_retries / max(total_attempts, 1)
        healing_rate = healing_steps / max(total_steps, 1)
        behavior_failure_rate = total_behavior_failures / max(total_attempts, 1)
        resolution_retry_rate = resolution_retry_steps / max(total_steps, 1)

        instability_index = int(
            ((retry_rate + healing_rate + behavior_failure_rate + resolution_retry_rate) / 4.0) * 100
        )
        instability_penalty = round(instability_index * 0.15, 4)

        return {
            "instability_index": instability_index,
            "instability_penalty": instability_penalty,
            "components": {
                "retry_rate": round(retry_rate, 4),
                "healing_rate": round(healing_rate, 4),
                "behavior_failure_rate": round(behavior_failure_rate, 4),
                "resolution_retry_rate": round(resolution_retry_rate, 4),
            },
        }

    except Exception as e:
        logger.warning("instability_engine.error", error=str(e)[:200])
        return default
