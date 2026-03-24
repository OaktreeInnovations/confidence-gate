"""Release validation scoring engine.

Computes a deterministic release confidence report from a batch of test runs.
Analyzes execution stability, flakiness, timing anomalies, selector confidence,
and behavior signals to produce a 0-100 score, A-F grade, and deploy/caution/block
recommendation.

All functions use sync pymongo (Celery worker context).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from bson import ObjectId
from pymongo.database import Database
import structlog

from app.telemetry.confidence_scorer import compute_run_confidence
from app.telemetry.models import RunTelemetry, StepTelemetry

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Report models
# ---------------------------------------------------------------------------


@dataclass
class ReleaseSignal:
    """A scored signal contributing to the release confidence score."""
    name: str
    category: str       # execution | flakiness | timing | selector | behavior
    severity: str       # good | warning | critical
    score_contribution: float  # 0-100 weighted contribution
    detail: str
    affected_tests: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "severity": self.severity,
            "score_contribution": round(self.score_contribution, 1),
            "detail": self.detail,
            "affected_tests": self.affected_tests[:10],
        }


@dataclass
class RootCause:
    """A ranked root cause of failures in the batch."""
    rank: int
    description: str
    impact_score: float  # 0-1
    affected_tests: list[str] = field(default_factory=list)
    affected_steps: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "description": self.description,
            "impact_score": round(self.impact_score, 3),
            "affected_tests": self.affected_tests[:10],
            "affected_steps": self.affected_steps[:10],
        }


@dataclass
class ReleaseReport:
    """Complete release validation report."""
    confidence_score: int = 0           # 0-100
    confidence_grade: str = "F"
    recommendation: str = "block"       # deploy | caution | block
    recommendation_reasons: list[str] = field(default_factory=list)
    signals: list[ReleaseSignal] = field(default_factory=list)
    root_causes: list[RootCause] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    batch_summary: dict = field(default_factory=dict)
    per_run_results: list[dict] = field(default_factory=list)
    feature_risks: list[dict] = field(default_factory=list)
    flaky_tests: list[dict] = field(default_factory=list)
    timing_anomalies: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "confidence_score": self.confidence_score,
            "confidence_grade": self.confidence_grade,
            "recommendation": self.recommendation,
            "recommendation_reasons": self.recommendation_reasons,
            "signals": [s.to_dict() for s in self.signals],
            "root_causes": [rc.to_dict() for rc in self.root_causes],
            "blockers": self.blockers,
            "batch_summary": self.batch_summary,
            "per_run_results": self.per_run_results,
            "feature_risks": self.feature_risks,
            "flaky_tests": self.flaky_tests,
            "timing_anomalies": self.timing_anomalies,
        }


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def _score_to_grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def _score_to_recommendation(score: int, blockers: list[str]) -> tuple[str, list[str]]:
    """Determine recommendation and reasons from score + blockers."""
    reasons: list[str] = []

    if blockers:
        reasons.extend(blockers)

    if score >= 80 and not blockers:
        reasons.append(f"Confidence score {score}/100 indicates stable execution")
        return "deploy", reasons

    if score >= 50 and len(blockers) <= 1:
        reasons.append(f"Confidence score {score}/100 — review flagged signals before deploying")
        return "caution", reasons

    reasons.append(f"Confidence score {score}/100 is below safe threshold")
    return "block", reasons


def _severity_from_value(value: float, warn_threshold: float, crit_threshold: float) -> str:
    """Classify severity based on thresholds (lower value = worse)."""
    if value >= warn_threshold:
        return "good"
    if value >= crit_threshold:
        return "warning"
    return "critical"


# ---------------------------------------------------------------------------
# Core scoring engine
# ---------------------------------------------------------------------------


def compute_release_report(
    db: Database,
    org_id: str,
    batch_id: str,
    project_id: str,
) -> ReleaseReport:
    """Compute a deterministic release confidence report for a batch.

    Steps:
    1. Query test_runs by batch_id → batch pass rate
    2. Query execution_telemetry per run → per-run confidence
    3. Query execution_profiles per test case → flake/timing/selector data
    4. Query failure_graph for org → clusters, feature risks
    5. Compute weighted signals
    6. Detect blockers, determine recommendation
    7. Rank root causes by impact
    """
    org_oid = ObjectId(org_id)
    report = ReleaseReport()

    # --- 1. Batch test runs ---
    runs = list(db.test_runs.find({
        "batch_id": batch_id,
        "org_id": org_oid,
    }))

    if not runs:
        report.batch_summary = {"total": 0, "passed": 0, "failed": 0, "error": 0}
        report.blockers = ["No test runs found in batch"]
        report.recommendation_reasons = ["No test data available"]
        return report

    total = len(runs)
    passed = sum(1 for r in runs if r.get("status") == "passed")
    failed = sum(1 for r in runs if r.get("status") == "failed")
    errored = sum(1 for r in runs if r.get("status") == "error")
    batch_pass_rate = passed / total

    report.batch_summary = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "error": errored,
        "pass_rate": round(batch_pass_rate, 4),
    }

    # Build per-run results
    run_id_to_doc = {}
    tc_ids_in_batch: set[str] = set()
    for r in runs:
        rid = str(r["_id"])
        tc_id = str(r.get("test_case_id", ""))
        run_id_to_doc[rid] = r
        tc_ids_in_batch.add(tc_id)
        report.per_run_results.append({
            "test_run_id": rid,
            "test_case_id": tc_id,
            "test_case_title": r.get("test_case_title", ""),
            "status": r.get("status", ""),
            "duration_ms": r.get("duration_ms", 0),
        })

    # --- 2. Per-run confidence from telemetry ---
    run_ids = list(run_id_to_doc.keys())
    telemetry_docs = {
        doc["test_run_id"]: doc
        for doc in db.execution_telemetry.find({"test_run_id": {"$in": run_ids}})
    }

    run_confidences: list[float] = []
    for rid in run_ids:
        tel_doc = telemetry_docs.get(rid)
        if tel_doc:
            try:
                run_tel = RunTelemetry(**tel_doc)
                conf = compute_run_confidence(run_tel)
                overall = conf.get("overall", 0.0)
                run_confidences.append(overall)
                # Enrich per_run_results
                for pr in report.per_run_results:
                    if pr["test_run_id"] == rid:
                        pr["confidence"] = overall
                        pr["recommendation"] = conf.get("recommendation", "")
                        break
            except Exception:
                run_confidences.append(0.0)
        else:
            run_confidences.append(0.5 if run_id_to_doc[rid].get("status") == "passed" else 0.0)

    avg_run_confidence = sum(run_confidences) / len(run_confidences) if run_confidences else 0.0

    # --- 3. Execution profiles for test cases in batch ---
    profiles = {
        doc["test_case_id"]: doc
        for doc in db.execution_profiles.find({
            "test_case_id": {"$in": list(tc_ids_in_batch)},
            "org_id": org_id,
        })
    }

    # Flake rates
    flake_rates: list[float] = []
    for tc_id in tc_ids_in_batch:
        profile = profiles.get(tc_id)
        if profile:
            fr = profile.get("flake_rate", 0.0)
            flake_rates.append(fr)
            if fr > 0.2:
                report.flaky_tests.append({
                    "test_case_id": tc_id,
                    "flake_rate": round(fr, 4),
                    "flake_report": profile.get("flake_report", {}),
                })

    max_flake_rate = max(flake_rates) if flake_rates else 0.0
    avg_flake_rate = sum(flake_rates) / len(flake_rates) if flake_rates else 0.0

    # Timing anomalies
    timing_anomaly_count = 0
    total_steps_checked = 0
    for tc_id in tc_ids_in_batch:
        profile = profiles.get(tc_id)
        if not profile:
            continue
        baselines = profile.get("timing_baselines", {})
        for _, step_bl in baselines.get("steps", {}).items():
            total_steps_checked += 1
            if step_bl.get("stddev_ms", 0) > step_bl.get("median_ms", 1) * 0.5:
                timing_anomaly_count += 1
                report.timing_anomalies.append({
                    "test_case_id": tc_id,
                    "step_number": step_bl.get("step_number", 0),
                    "action": step_bl.get("action", ""),
                    "stddev_ms": step_bl.get("stddev_ms", 0),
                    "median_ms": step_bl.get("median_ms", 0),
                })

    timing_anomaly_density = (
        timing_anomaly_count / total_steps_checked
        if total_steps_checked > 0
        else 0.0
    )

    # Selector confidence
    selector_success_rates: list[float] = []
    for tc_id in tc_ids_in_batch:
        profile = profiles.get(tc_id)
        if not profile:
            continue
        for sp in profile.get("step_profiles", []):
            rates = sp.get("selector_success_rates", {})
            if rates:
                selector_success_rates.extend(rates.values())

    avg_selector_confidence = (
        sum(selector_success_rates) / len(selector_success_rates)
        if selector_success_rates
        else 1.0  # No selector data = assume stable
    )

    # Behavior confidence from telemetry
    behavior_failure_count = 0
    total_behavior_steps = 0
    for tel_doc in telemetry_docs.values():
        for step in tel_doc.get("steps", []):
            for attempt in step.get("attempts", []):
                total_behavior_steps += 1
                if attempt.get("behavior_failures", 0) > 0:
                    behavior_failure_count += 1

    behavior_confidence = (
        1.0 - (behavior_failure_count / total_behavior_steps)
        if total_behavior_steps > 0
        else 1.0
    )

    # --- 4. Failure graph data ---
    graph_doc = db.failure_graph.find_one({"org_id": org_id})
    if graph_doc:
        report.feature_risks = graph_doc.get("feature_risks", [])

    # --- 5. Compute weighted signals ---
    signals: list[ReleaseSignal] = []

    # Signal 1: Execution stability (35%)
    stability_raw = batch_pass_rate * 100
    stability_weighted = 0.35 * stability_raw
    failed_tests = [
        r.get("test_case_title", "Unknown")
        for r in runs if r.get("status") in ("failed", "error")
    ]
    signals.append(ReleaseSignal(
        name="Execution Stability",
        category="execution",
        severity=_severity_from_value(batch_pass_rate, 0.9, 0.7),
        score_contribution=stability_weighted,
        detail=f"Batch pass rate: {batch_pass_rate:.0%} ({passed}/{total} tests passed)",
        affected_tests=failed_tests,
    ))

    # Signal 2: Flakiness inverse (20%)
    flake_inverse = max(0, 1.0 - max_flake_rate) * 100
    flake_weighted = 0.20 * flake_inverse
    flaky_test_names = [
        ft.get("test_case_id", "") for ft in report.flaky_tests
    ]
    signals.append(ReleaseSignal(
        name="Flakiness",
        category="flakiness",
        severity=_severity_from_value(1.0 - max_flake_rate, 0.8, 0.5),
        score_contribution=flake_weighted,
        detail=f"Max flake rate: {max_flake_rate:.0%}, avg: {avg_flake_rate:.0%} across {len(flake_rates)} test cases",
        affected_tests=flaky_test_names,
    ))

    # Signal 3: Performance (15%)
    perf_raw = max(0, (1.0 - timing_anomaly_density)) * 100
    perf_weighted = 0.15 * perf_raw
    signals.append(ReleaseSignal(
        name="Performance",
        category="timing",
        severity=_severity_from_value(1.0 - timing_anomaly_density, 0.85, 0.6),
        score_contribution=perf_weighted,
        detail=f"{timing_anomaly_count} timing anomalies across {total_steps_checked} steps ({timing_anomaly_density:.0%})",
        affected_tests=[a["test_case_id"] for a in report.timing_anomalies[:5]],
    ))

    # Signal 4: Selector confidence (15%)
    selector_raw = avg_selector_confidence * 100
    selector_weighted = 0.15 * selector_raw
    signals.append(ReleaseSignal(
        name="Selector Confidence",
        category="selector",
        severity=_severity_from_value(avg_selector_confidence, 0.85, 0.6),
        score_contribution=selector_weighted,
        detail=f"Average selector success rate: {avg_selector_confidence:.0%}",
    ))

    # Signal 5: Behavior confidence (15%)
    behavior_raw = behavior_confidence * 100
    behavior_weighted = 0.15 * behavior_raw
    signals.append(ReleaseSignal(
        name="Behavior Confidence",
        category="behavior",
        severity=_severity_from_value(behavior_confidence, 0.9, 0.7),
        score_contribution=behavior_weighted,
        detail=f"Behavior verification success: {behavior_confidence:.0%} ({total_behavior_steps} steps checked)",
    ))

    report.signals = signals
    raw_score = sum(s.score_contribution for s in signals)

    # --- 6. Blockers ---
    blockers: list[str] = []

    if batch_pass_rate < 0.5:
        blockers.append(f"Batch pass rate critically low: {batch_pass_rate:.0%}")
        raw_score = min(raw_score, 30)

    if batch_pass_rate == 0:
        blockers.append("All tests failed — no confidence in release")
        raw_score = min(raw_score, 5)

    critical_failures = sum(1 for r in runs if r.get("status") == "error")
    if critical_failures > total * 0.3:
        blockers.append(f"{critical_failures}/{total} tests errored (crashes/timeouts)")
        raw_score = min(raw_score, 40)

    if max_flake_rate > 0.5:
        blockers.append(f"High flake rate detected: {max_flake_rate:.0%}")
        raw_score = min(raw_score, 50)

    report.blockers = blockers
    report.confidence_score = max(0, min(100, int(raw_score)))
    report.confidence_grade = _score_to_grade(report.confidence_score)

    recommendation, reasons = _score_to_recommendation(report.confidence_score, blockers)
    report.recommendation = recommendation
    report.recommendation_reasons = reasons

    # --- 7. Root causes ---
    report.root_causes = _compute_root_causes(runs, telemetry_docs, profiles)

    logger.info(
        "release_scorer.computed",
        org_id=org_id,
        batch_id=batch_id,
        score=report.confidence_score,
        grade=report.confidence_grade,
        recommendation=report.recommendation,
        signals=len(report.signals),
        blockers=len(report.blockers),
        root_causes=len(report.root_causes),
    )

    return report


# ---------------------------------------------------------------------------
# Root cause analysis
# ---------------------------------------------------------------------------


def _compute_root_causes(
    runs: list[dict],
    telemetry_docs: dict[str, dict],
    profiles: dict[str, dict],
) -> list[RootCause]:
    """Identify and rank root causes from batch failures.

    Groups failure reasons across runs, scores by impact (breadth x frequency),
    and returns the top 3.
    """
    # Collect failure reasons → affected tests and steps
    reason_data: dict[str, dict] = defaultdict(lambda: {
        "count": 0,
        "tests": set(),
        "steps": [],
    })

    for run in runs:
        if run.get("status") not in ("failed", "error"):
            continue

        rid = str(run["_id"])
        tc_title = run.get("test_case_title", "Unknown")
        tel_doc = telemetry_docs.get(rid)

        if tel_doc:
            for step in tel_doc.get("steps", []):
                if step.get("status") in ("failed", "error"):
                    for attempt in step.get("attempts", []):
                        if not attempt.get("success", False):
                            reason = attempt.get("error_type", "unknown")
                            msg = attempt.get("error_message", "")
                            key = reason
                            reason_data[key]["count"] += 1
                            reason_data[key]["tests"].add(tc_title)
                            reason_data[key]["steps"].append({
                                "test_case_title": tc_title,
                                "step_number": step.get("step_number", 0),
                                "action": step.get("action", "")[:80],
                                "error_message": msg[:200],
                            })
        else:
            # No telemetry — use run-level status
            reason_data["unknown"]["count"] += 1
            reason_data["unknown"]["tests"].add(tc_title)

    if not reason_data:
        return []

    total_tests = len(set(r.get("test_case_title", "") for r in runs))

    # Score and rank
    scored: list[tuple[str, float, dict]] = []
    for reason, data in reason_data.items():
        breadth = len(data["tests"]) / max(total_tests, 1)
        frequency = min(data["count"] / 10.0, 1.0)
        impact = 0.6 * breadth + 0.4 * frequency
        scored.append((reason, impact, data))

    scored.sort(key=lambda x: -x[1])

    root_causes = []
    for rank, (reason, impact, data) in enumerate(scored[:3], 1):
        root_causes.append(RootCause(
            rank=rank,
            description=_humanize_reason(reason, data),
            impact_score=impact,
            affected_tests=sorted(data["tests"]),
            affected_steps=data["steps"][:5],
        ))

    return root_causes


_REASON_DESCRIPTIONS = {
    "selector_not_found": "Element not found on page",
    "selector_not_visible": "Element exists but not visible/interactable",
    "selector_ambiguous": "Multiple matching elements found",
    "navigation_timeout": "Page navigation timed out",
    "page_crash": "Browser page crashed",
    "element_detached": "Element was removed from DOM during interaction",
    "frame_detached": "Page frame was destroyed during interaction",
    "network_error": "Network request failed",
    "js_error": "JavaScript error on page",
    "code_gen_failure": "AI failed to generate valid test code",
    "assertion_failure": "Test assertion did not match expected result",
    "unknown": "Unclassified failure",
}


def _humanize_reason(reason: str, data: dict) -> str:
    """Generate a human-readable root cause description."""
    base = _REASON_DESCRIPTIONS.get(reason, f"Failure type: {reason}")
    test_count = len(data["tests"])
    occurrence_count = data["count"]
    return f"{base} — affected {test_count} test(s), {occurrence_count} occurrence(s)"
