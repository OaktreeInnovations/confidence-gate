"""Execution Confidence Scoring.

Computes a 0.0–1.0 confidence score for each step and the overall test run
based on telemetry signals. This enables:
- Identifying flaky tests before they become a problem
- Prioritizing which tests need attention
- Giving users a quick reliability indicator

Scoring philosophy:
- Start at 1.0 (perfect confidence) and subtract penalties
- Never go below 0.0
- Heavier penalties for signals that strongly indicate flakiness
"""

from __future__ import annotations

from app.telemetry.models import RunTelemetry, StepTelemetry


# --- Penalty weights ---
_RETRY_PENALTY = 0.15          # Per additional attempt beyond the first
_FLAKE_PASS_PENALTY = 0.20     # Passed but only after retries
_SELECTOR_AMBIGUITY = 0.10     # Multiple selectors tried
_FAILED_AI_CALL = 0.10         # AI call failed (had to retry/fallback)
_CUSTOM_CODE_PENALTY = 0.05    # Custom code is less adaptive
_HEALING_USED_PENALTY = 0.10   # Healing needed (indicates selector instability)
_HEALING_FAILED_PENALTY = 0.15 # Healing was attempted but did not succeed
_SLOW_EXECUTION = 0.05         # Execution took > 10s (single step)
_VERY_SLOW_EXECUTION = 0.10    # Execution took > 30s
_NO_VERIFICATION = 0.05        # No vision verification performed
_BEHAVIOR_OVERRIDE_PENALTY = 0.25  # Vision overridden by heuristic keyword match
_BEHAVIOR_FAILURE_PENALTY = 0.20   # Action executed but no observable DOM effect
_INCONCLUSIVE_PENALTY = 0.35   # Step outcome was inconclusive (counts as 0 confidence)

# --- Verification level multipliers (applied to final score) ---
# "ai" = full vision verification — no adjustment
# "dom-only" = weaker; reduce confidence slightly
# "behavior_override" = heuristic pass; confidence already penalised above
# "skipped" = no verification at all; strong penalty
_VERIFICATION_MULTIPLIER: dict[str, float] = {
    "ai": 1.0,
    "dom-only": 0.90,
    "behavior_override": 0.85,
    "inconclusive": 0.0,   # inconclusive = not passed; score must be 0
    "skipped": 0.80,
    "": 0.95,               # unknown/not recorded
}

# --- Bonus weights ---
_FIRST_ATTEMPT_BONUS = 0.0     # No bonus needed — it's the default
_FAST_EXECUTION_BONUS = 0.02   # Under 3s total
_VERIFICATION_PASSED = 0.0     # Already the default assumption

# --- Thresholds ---
_RELIABLE_THRESHOLD = 0.85
_FLAKY_THRESHOLD = 0.60


def compute_step_confidence(step: StepTelemetry) -> float:
    """Compute confidence score for a single step.

    Returns a float between 0.0 and 1.0.
    """
    score = 1.0

    # Inconclusive and failed steps get a floor score of 0.0
    if step.status in ("failed", "error", "inconclusive"):
        return 0.0

    # Non-passed statuses (skipped) treated as 0
    if step.status != "passed":
        return 0.0

    # Behavior override: vision was overridden by a heuristic keyword match.
    # This is inherently less reliable — penalise before other adjustments.
    if step.verification_level == "behavior_override":
        score -= _BEHAVIOR_OVERRIDE_PENALTY

    # Behavior failures: action had no observable DOM effect on at least one attempt
    behavior_failed_count = sum(
        1 for a in step.attempts if getattr(a, "behavior_failed", False)
    )
    if behavior_failed_count > 0:
        score -= _BEHAVIOR_FAILURE_PENALTY

    # Retry penalties
    extra_attempts = max(0, step.total_attempts - 1)
    score -= extra_attempts * _RETRY_PENALTY

    # Flake indicator: passed but needed retries
    if step.total_attempts > 1:
        score -= _FLAKE_PASS_PENALTY

    # Selector ambiguity
    if len(step.selectors_tried) > 2:
        score -= _SELECTOR_AMBIGUITY

    # Healing penalties
    healed_attempts = [a for a in step.attempts if a.healing_attempted]
    if healed_attempts:
        score -= _HEALING_USED_PENALTY
        # Extra penalty if healing was attempted but never succeeded
        if not any(getattr(a, "healing_success", False) for a in healed_attempts):
            score -= _HEALING_FAILED_PENALTY

    # Failed AI calls
    failed_ai = sum(1 for c in step.ai_calls if not c.success)
    score -= failed_ai * _FAILED_AI_CALL

    # Custom code penalty
    if step.used_custom_code:
        score -= _CUSTOM_CODE_PENALTY

    # Execution time penalties
    total_ms = step.timings.total_ms
    if total_ms > 30000:
        score -= _VERY_SLOW_EXECUTION
    elif total_ms > 10000:
        score -= _SLOW_EXECUTION

    # Fast execution bonus
    if total_ms < 3000 and step.total_attempts == 1:
        score += _FAST_EXECUTION_BONUS

    # No verification penalty
    if step.timings.verification_ms == 0:
        score -= _NO_VERIFICATION

    # Apply verification level multiplier
    multiplier = _VERIFICATION_MULTIPLIER.get(step.verification_level, 0.95)
    score *= multiplier

    return max(0.0, min(1.0, round(score, 3)))


def compute_run_confidence(run: RunTelemetry) -> dict:
    """Compute confidence metrics for an entire test run.

    Returns:
        {
            "overall": float,           # 0.0–1.0 weighted average
            "per_step": [               # Per-step breakdown
                {"step": 1, "confidence": 0.95, "risk_factors": [...]},
                ...
            ],
            "risk_factors": [...],       # Run-level risk descriptions
            "recommendation": str,       # "reliable" | "flaky" | "unstable"
        }
    """
    if not run.steps:
        return {
            "overall": 0.0,
            "per_step": [],
            "risk_factors": ["No steps executed"],
            "recommendation": "unstable",
        }

    per_step = []
    step_scores = []
    run_risks: list[str] = []

    for step in run.steps:
        score = compute_step_confidence(step)
        step_scores.append(score)

        step_risks = _identify_step_risks(step, score)
        per_step.append({
            "step": step.step_number,
            "action": step.action[:80],
            "confidence": score,
            "risk_factors": step_risks,
        })

    # Overall score: weighted average (failed steps count more)
    if step_scores:
        # Weight lower scores more heavily — a chain is as strong as its weakest link
        weights = [1.0 / max(s, 0.01) for s in step_scores]
        total_weight = sum(weights)
        weighted_sum = sum(s * w for s, w in zip(step_scores, weights))
        overall = weighted_sum / total_weight if total_weight > 0 else 0.0
        # Normalize to keep in 0-1 range
        overall = max(0.0, min(1.0, overall))
    else:
        overall = 0.0

    # Run-level risk factors
    if run.total_retries > 0:
        run_risks.append(f"{run.total_retries} total retries across all steps")

    if run.flake_indicators:
        run_risks.append(f"{len(run.flake_indicators)} flake indicators detected")

    failed_steps = sum(1 for s in run.steps if s.status in ("failed", "error"))
    inconclusive_steps = sum(1 for s in run.steps if s.status == "inconclusive")
    if failed_steps > 0:
        run_risks.append(f"{failed_steps} step(s) failed")
    if inconclusive_steps > 0:
        run_risks.append(f"{inconclusive_steps} step(s) inconclusive — outcome unverifiable")

    # Behavior override rate: what fraction of passed steps used heuristic override
    passed_steps = [s for s in run.steps if s.status == "passed"]
    override_steps = sum(1 for s in passed_steps if s.verification_level == "behavior_override")
    if passed_steps:
        override_rate = override_steps / len(passed_steps)
        if override_rate > 0.2:
            run_risks.append(
                f"High behavior_override rate ({override_rate:.0%}) — "
                f"{override_steps}/{len(passed_steps)} steps verified by heuristic only"
            )

    slow_steps = sum(1 for s in run.steps if s.timings.total_ms > 15000)
    if slow_steps > 0:
        run_risks.append(f"{slow_steps} step(s) took > 15s")

    total_ai_failures = sum(
        1 for s in run.steps for c in s.ai_calls if not c.success
    )
    if total_ai_failures > 0:
        run_risks.append(f"{total_ai_failures} AI call(s) failed")

    # Recommendation — inconclusive steps always make the run unstable
    all_non_pass = failed_steps + inconclusive_steps
    if overall >= _RELIABLE_THRESHOLD and all_non_pass == 0:
        recommendation = "reliable"
    elif overall >= _FLAKY_THRESHOLD and inconclusive_steps == 0:
        recommendation = "flaky"
    else:
        recommendation = "unstable"

    return {
        "overall": round(overall, 3),
        "per_step": per_step,
        "risk_factors": run_risks,
        "recommendation": recommendation,
    }


def _identify_step_risks(step: StepTelemetry, score: float) -> list[str]:
    """Identify human-readable risk factors for a single step."""
    risks = []

    if step.status == "inconclusive":
        risks.append("Step inconclusive — action executed but outcome unverifiable")
    elif step.status != "passed":
        risks.append(f"Step {step.status}")

    if step.verification_level == "behavior_override":
        risks.append("Verification by heuristic keyword match (behavior_override)")

    # Behavior failures in any attempt
    behavior_failed_count = sum(
        1 for a in step.attempts if getattr(a, "behavior_failed", False)
    )
    if behavior_failed_count > 0:
        risks.append(f"Action had no observable DOM effect in {behavior_failed_count} attempt(s)")

    if step.total_attempts > 1:
        risks.append(f"Required {step.total_attempts} attempts")

    if len(step.selectors_tried) > 2:
        risks.append(f"Tried {len(step.selectors_tried)} selector strategies")

    failed_ai = sum(1 for c in step.ai_calls if not c.success)
    if failed_ai > 0:
        risks.append(f"{failed_ai} AI call(s) failed")

    if step.timings.total_ms > 30000:
        risks.append(f"Very slow execution ({step.timings.total_ms}ms)")
    elif step.timings.total_ms > 10000:
        risks.append(f"Slow execution ({step.timings.total_ms}ms)")

    if step.timings.verification_ms == 0 and step.status == "passed":
        risks.append("No vision verification")

    # Healing usage risk
    healing_attempts = [a for a in step.attempts if a.healing_attempted]
    if healing_attempts:
        layers = set(
            a.healing_layer_used for a in healing_attempts
            if a.healing_layer_used > 0
        )
        if layers:
            risks.append(f"Healing required (layers: {sorted(layers)})")
        if not any(getattr(a, "healing_success", False) for a in healing_attempts):
            risks.append("Healing attempted but did not succeed")

    return risks
