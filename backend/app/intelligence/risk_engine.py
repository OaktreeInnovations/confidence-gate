"""Weights test failures by step importance to compute a risk-adjusted score delta."""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


def _position_weight(step_number: int) -> float:
    if step_number <= 3:
        return 1.5
    if step_number <= 7:
        return 1.2
    return 1.0


def compute_risk_adjustment(
    runs: list[dict],
    telemetry_docs: dict[str, dict],
    profiles: dict[str, dict],
) -> dict:
    if not runs:
        return {"risk_adjustment": 0.0, "high_risk_steps": []}

    try:
        total_test_cases = len({
            str(r.get("test_case_id", "")) for r in runs if r.get("test_case_id")
        })
        if total_test_cases == 0:
            total_test_cases = 1

        # Build step_number -> count of test cases that include this step
        step_tc_counts: dict[int, int] = {}
        for run in runs:
            tc_id = str(run.get("test_case_id", ""))
            rid = str(run.get("_id", run.get("test_run_id", "")))
            tel_doc = telemetry_docs.get(rid)
            if not tel_doc:
                continue
            seen_steps: set[int] = set()
            for step in tel_doc.get("steps", []):
                sn = step.get("step_number", 0)
                if sn and sn not in seen_steps:
                    seen_steps.add(sn)
                    step_tc_counts[sn] = step_tc_counts.get(sn, 0) + 1

        # Compute risk weight per step
        step_risks: list[dict] = []
        all_step_numbers: set[int] = set(step_tc_counts.keys())

        for sn in all_step_numbers:
            # Get failure rate from profiles
            failure_rate = 0.3  # fallback
            for profile in profiles.values():
                for sp in profile.get("step_profiles", []):
                    if sp.get("step_number") == sn:
                        pass_rate = sp.get("pass_rate", 0.7)
                        failure_rate = 1.0 - pass_rate
                        break

            pos_w = _position_weight(sn)

            # frequency_weight: normalized 0.5-1.5
            raw_freq = step_tc_counts.get(sn, 1) / total_test_cases
            freq_w = 0.5 + raw_freq  # maps [0,1] -> [0.5, 1.5]

            risk_weight = pos_w * failure_rate * freq_w
            step_risks.append({
                "step_number": sn,
                "risk_weight": round(risk_weight, 4),
                "position_weight": pos_w,
                "failure_rate": round(failure_rate, 4),
                "frequency_weight": round(freq_w, 4),
            })

        step_risks.sort(key=lambda x: -x["risk_weight"])
        top_3 = step_risks[:3]

        if not top_3:
            return {"risk_adjustment": 0.0, "high_risk_steps": []}

        top_3_step_numbers = {s["step_number"] for s in top_3}

        # Check pass/fail status of top-3 steps in current batch
        top_3_statuses: dict[int, list[bool]] = {sn: [] for sn in top_3_step_numbers}

        for run in runs:
            rid = str(run.get("_id", run.get("test_run_id", "")))
            tel_doc = telemetry_docs.get(rid)
            if not tel_doc:
                continue
            for step in tel_doc.get("steps", []):
                sn = step.get("step_number", 0)
                if sn in top_3_step_numbers:
                    passed = step.get("status", "") == "passed"
                    top_3_statuses[sn].append(passed)

        top_3_results: list[bool | None] = []
        for sn in top_3_step_numbers:
            statuses = top_3_statuses[sn]
            if not statuses:
                top_3_results.append(None)
            else:
                top_3_results.append(all(statuses))

        valid_results = [r for r in top_3_results if r is not None]

        if not valid_results:
            risk_adjustment = 0.0
        elif all(r is False for r in valid_results):
            risk_adjustment = -3.0
        elif all(r is True for r in valid_results):
            risk_adjustment = 2.0
        else:
            risk_adjustment = 0.0

        return {
            "risk_adjustment": risk_adjustment,
            "high_risk_steps": top_3,
        }

    except Exception as e:
        logger.warning("risk_engine.error", error=str(e)[:200])
        return {"risk_adjustment": 0.0, "high_risk_steps": []}
