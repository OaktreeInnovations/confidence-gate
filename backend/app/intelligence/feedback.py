"""Execution Feedback Loop — injects historical intelligence into AI code generation.

Queries the execution profile for a test case and builds context hints
that improve first-attempt success rates:
- Flake warnings for known-flaky steps
- Timing expectations (expected duration, anomaly thresholds)
- Known failure patterns to avoid
- Known-good selectors (integrated via selector_memory)

This completes the intelligence cycle:
  execution → telemetry → aggregation → memory → feedback → better execution
"""

from __future__ import annotations

from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)


def build_step_intelligence(
    db: Database,
    test_case_id: str,
    org_id: str,
    step_number: int,
) -> str:
    """Build an intelligence context string for a specific step.

    Queries the execution profile for flake data, timing baselines,
    and failure patterns. Returns a human-readable string that gets
    injected into the AI code generation prompt.

    Returns empty string if no intelligence is available.
    """
    profile_doc = db.execution_profiles.find_one({
        "test_case_id": test_case_id,
        "org_id": org_id,
    })

    if not profile_doc:
        return ""

    hints: list[str] = []

    # --- Flake warnings ---
    flake_report = profile_doc.get("flake_report", {})
    flaky_steps = flake_report.get("flaky_steps", [])
    for fs in flaky_steps:
        if fs.get("step_number") == step_number:
            score = fs.get("flake_score", 0)
            types = fs.get("flake_types", [])
            reasons = fs.get("failure_reasons", {})

            hints.append(
                f"WARNING: This step is historically FLAKY (flake score: {score:.0%})."
            )

            if "selector_flake" in types:
                hints.append(
                    "  - Selector instability detected. Use multiple fallback selectors."
                )
            if "timing_flake" in types:
                hints.append(
                    "  - Timing-dependent failures detected. Add explicit waits after actions."
                )
            if "network_flake" in types:
                hints.append(
                    "  - Network instability detected. Add retry logic for network operations."
                )

            # Top failure reasons
            if reasons:
                top_reasons = sorted(reasons.items(), key=lambda x: -x[1])[:3]
                reason_str = ", ".join(f"{r} ({c}x)" for r, c in top_reasons)
                hints.append(f"  - Common failures: {reason_str}")

            break

    # --- Timing expectations ---
    timing_baselines = profile_doc.get("timing_baselines", {})
    step_baselines = timing_baselines.get("steps", {})
    step_baseline = step_baselines.get(str(step_number), {})

    if step_baseline:
        median = step_baseline.get("median_ms", 0)
        p95 = step_baseline.get("p95_ms", 0)
        if median > 0:
            hints.append(
                f"Timing: This step typically takes {median}ms (P95: {p95}ms)."
            )
            if median > 5000:
                hints.append(
                    "  - This is a slow step. Ensure adequate wait times."
                )

    # --- Step profile data ---
    for sp in profile_doc.get("step_profiles", []):
        if sp.get("step_number") == step_number:
            pass_rate = sp.get("pass_rate", 1.0)
            retry_rate = sp.get("retry_rate", 0.0)

            if pass_rate < 0.8:
                hints.append(
                    f"Reliability: This step has a {pass_rate:.0%} pass rate. "
                    "Take extra care with selector accuracy and wait timing."
                )
            if retry_rate > 0.3:
                hints.append(
                    f"Retry rate: {retry_rate:.0%} of runs needed retries for this step."
                )

            # Known failure patterns
            failures = sp.get("most_common_failures", [])
            if failures:
                for f in failures[:2]:
                    reason = f.get("reason", "")
                    example = f.get("example_message", "")
                    if reason and example:
                        hints.append(
                            f"  - Avoid: {reason} — e.g. \"{example[:100]}\""
                        )
            break

    if not hints:
        return ""

    return "\n".join(["[Execution Intelligence]"] + hints)


def build_run_intelligence(
    db: Database,
    test_case_id: str,
    org_id: str,
) -> dict[int, str]:
    """Build intelligence context for all steps of a test case.

    Returns dict of step_number -> intelligence string.
    Only includes steps that have non-empty intelligence.
    """
    profile_doc = db.execution_profiles.find_one({
        "test_case_id": test_case_id,
        "org_id": org_id,
    })

    if not profile_doc:
        return {}

    # Get all step numbers from profile
    step_numbers = set()
    for sp in profile_doc.get("step_profiles", []):
        step_numbers.add(sp.get("step_number", 0))
    for fs in profile_doc.get("flake_report", {}).get("flaky_steps", []):
        step_numbers.add(fs.get("step_number", 0))

    result = {}
    for sn in step_numbers:
        if sn == 0:
            continue
        intel = build_step_intelligence(db, test_case_id, org_id, sn)
        if intel:
            result[sn] = intel

    return result
