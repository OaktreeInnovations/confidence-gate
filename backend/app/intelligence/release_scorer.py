"""Release validation scoring engine — V1/V2 deterministic architecture.

V1: Five weighted signals (execution, flakiness, performance, selector, behavior)
    plus a healing-dependency penalty. Decision thresholds: SAFE≥85, CAUTION≥60,
    BLOCK<60.

V2: Activates when execution_profiles contain sufficient historical data
    (total_runs ≥ 3 for at least two test cases). Adds historical_stability (15%)
    and trend_adjustment (10%) signals; V1 signal weights scale down proportionally.
    Outputs trend direction and risk_delta vs historical norm.

All functions use sync pymongo (Celery worker context).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from bson import ObjectId
from pymongo.database import Database
import structlog

from app.telemetry.confidence_scorer import compute_run_confidence
from app.telemetry.models import RunTelemetry

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Report models
# ---------------------------------------------------------------------------


@dataclass
class ReleaseSignal:
    """A scored signal contributing to the release confidence score."""
    name: str
    category: str       # execution | flakiness | timing | selector | behavior | historical | trend
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
    confidence_score: int = 0           # 0-100 final score
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

    # Scoring metadata
    score_version: str = "v1"           # v1 | v2 | v3
    healing_penalty: float = 0.0        # points deducted for healing dependency
    historical_confidence: int | None = None  # V2+: historical avg pass rate 0-100
    trend: str = "stable"               # V2+: up | down | stable
    risk_delta: int | None = None       # V2+: current_score - historical_score

    def to_dict(self) -> dict:
        d = {
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
            "score_version": self.score_version,
            "healing_penalty": round(self.healing_penalty, 1),
            "trend": self.trend,
        }
        if self.historical_confidence is not None:
            d["historical_confidence"] = self.historical_confidence
        if self.risk_delta is not None:
            d["risk_delta"] = self.risk_delta
        return d


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


def _score_to_recommendation(
    score: int,
    blockers: list[str],
    deploy_threshold: int = 85,
    caution_threshold: int = 60,
) -> tuple[str, list[str]]:
    """Determine recommendation and reasons.

    Default thresholds: DEPLOY≥85, CAUTION≥60, BLOCK<60.
    Callers can pass learned per-org thresholds from weight_learner.
    """
    reasons: list[str] = []

    if blockers:
        reasons.extend(blockers)

    if score >= deploy_threshold and not blockers:
        reasons.append(f"Confidence score {score}/100 — execution is stable and ready to deploy")
        return "deploy", reasons

    if score >= caution_threshold and len(blockers) <= 1:
        reasons.append(f"Confidence score {score}/100 — review flagged signals before deploying")
        return "caution", reasons

    reasons.append(f"Confidence score {score}/100 is below safe threshold ({deploy_threshold})")
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

    V1 (always): Five weighted signals + healing penalty.
    V2 (when historical data available): Adds historical_stability and
    trend_adjustment signals; redistributes weights.
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
    run_id_to_doc: dict[str, dict] = {}
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
                for pr in report.per_run_results:
                    if pr["test_run_id"] == rid:
                        pr["confidence"] = overall
                        pr["recommendation"] = conf.get("recommendation", "")
                        break
            except Exception:
                run_confidences.append(0.0)
        else:
            run_confidences.append(0.5 if run_id_to_doc[rid].get("status") == "passed" else 0.0)

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
    tc_flake_map: dict[str, float] = {}  # 8.2: per-test-case flake rate for weighted scoring
    for tc_id in tc_ids_in_batch:
        profile = profiles.get(tc_id)
        if profile:
            fr = profile.get("flake_rate", 0.0)
            flake_rates.append(fr)
            tc_flake_map[tc_id] = fr
            if fr > 0.2:
                report.flaky_tests.append({
                    "test_case_id": tc_id,
                    "flake_rate": round(fr, 4),
                    "flake_report": profile.get("flake_report", {}),
                })

    max_flake_rate = max(flake_rates) if flake_rates else 0.0
    avg_flake_rate = sum(flake_rates) if flake_rates else 0.0
    if flake_rates:
        avg_flake_rate = sum(flake_rates) / len(flake_rates)

    # 8.2 Flaky-weighted pass rate: weight each run's pass contribution by
    # max(0, 1 - flake_rate) so high-flake tests don't drag the score unfairly.
    # Raw batch_pass_rate is preserved in batch_summary for display.
    _w_passed = 0.0
    _w_total = 0.0
    for r in runs:
        tc_id = str(r.get("test_case_id", ""))
        fr = tc_flake_map.get(tc_id, 0.0)
        weight = max(0.0, 1.0 - fr)
        _w_total += weight
        if r.get("status") == "passed":
            _w_passed += weight
    weighted_pass_rate = (_w_passed / _w_total) if _w_total > 0 else batch_pass_rate

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
        timing_anomaly_count / total_steps_checked if total_steps_checked > 0 else 0.0
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
        else 1.0
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

    # --- 5. Healing dependency penalty ---
    # High avg_attempts on step profiles means tests rely on retries/AI regeneration.
    # Penalty = 0-10 points proportional to fraction of healing-dependent steps.
    healing_dependent_steps = 0
    total_steps_with_data = 0
    for tc_id in tc_ids_in_batch:
        profile = profiles.get(tc_id)
        if not profile:
            continue
        for sp in profile.get("step_profiles", []):
            total_observations = sp.get("total_observations", 0)
            if total_observations < 2:
                continue
            total_steps_with_data += 1
            avg_attempts = sp.get("avg_attempts", 1.0)
            if avg_attempts > 1.5:
                healing_dependent_steps += 1

    healing_ratio = (
        healing_dependent_steps / total_steps_with_data
        if total_steps_with_data > 0
        else 0.0
    )
    healing_penalty = min(10.0, healing_ratio * 20.0)
    report.healing_penalty = healing_penalty

    # --- 6. Check V2 historical data availability ---
    # 9A.4 V2 activates when ≥50% of test cases have ≥3 historical runs in their profile.
    # A single test case with history shouldn't drive historical signals for the whole suite.
    profiles_with_history = [
        p for p in profiles.values()
        if p.get("total_runs", 0) >= 3
    ]
    use_v2 = (
        len(profiles_with_history) >= 1
        and len(profiles_with_history) / max(len(profiles), 1) >= 0.5
    )

    # --- 7. Compute weighted signals ---
    signals: list[ReleaseSignal] = []
    failed_tests = [
        r.get("test_case_title", "Unknown")
        for r in runs if r.get("status") in ("failed", "error")
    ]
    flaky_test_names = [ft.get("test_case_id", "") for ft in report.flaky_tests]

    # Load dynamic weights (7C.1); fall back to hardcoded defaults on any error.
    try:
        from app.intelligence.global_weight_optimizer import load_global_weights
        gw = load_global_weights(db)
    except Exception:
        gw = {"exec": 0.30, "flake": 0.20, "perf": 0.15, "selector": 0.15, "behavior": 0.20}

    if use_v2:
        # V2 weights: exec 25%, flake 15%, perf 10%, selector 10%, behavior 15%,
        #             historical 15%, trend 10%
        # Dynamic weights from 7C.1 are blended with V2 scaling (which adds historical/trend).
        # Scale exec/flake/perf/selector/behavior to sum to 0.75, leaving 0.25 for hist+trend.
        v2_base_total = gw["exec"] + gw["flake"] + gw["perf"] + gw["selector"] + gw["behavior"]
        v2_scale = 0.75 / v2_base_total if v2_base_total > 0 else 1.0
        w_exec = gw["exec"] * v2_scale
        w_flake = gw["flake"] * v2_scale
        w_perf = gw["perf"] * v2_scale
        w_sel = gw["selector"] * v2_scale
        w_beh = gw["behavior"] * v2_scale

        signals.append(ReleaseSignal(
            name="Execution Stability",
            category="execution",
            severity=_severity_from_value(weighted_pass_rate, 0.9, 0.7),
            score_contribution=w_exec * weighted_pass_rate * 100,
            detail=f"Batch pass rate: {batch_pass_rate:.0%} ({passed}/{total} tests passed)",
            affected_tests=failed_tests,
        ))

        signals.append(ReleaseSignal(
            name="Flakiness",
            category="flakiness",
            severity=_severity_from_value(1.0 - max_flake_rate, 0.8, 0.5),
            score_contribution=w_flake * max(0, 1.0 - max_flake_rate) * 100,
            detail=f"Max flake rate: {max_flake_rate:.0%}, avg: {avg_flake_rate:.0%} across {len(flake_rates)} test cases",
            affected_tests=flaky_test_names,
        ))

        signals.append(ReleaseSignal(
            name="Performance",
            category="timing",
            severity=_severity_from_value(1.0 - timing_anomaly_density, 0.85, 0.6),
            score_contribution=w_perf * max(0, 1.0 - timing_anomaly_density) * 100,
            detail=f"{timing_anomaly_count} timing anomalies across {total_steps_checked} steps ({timing_anomaly_density:.0%})",
            affected_tests=[a["test_case_id"] for a in report.timing_anomalies[:5]],
        ))

        signals.append(ReleaseSignal(
            name="Selector Confidence",
            category="selector",
            severity=_severity_from_value(avg_selector_confidence, 0.85, 0.6),
            score_contribution=w_sel * avg_selector_confidence * 100,
            detail=f"Average selector success rate: {avg_selector_confidence:.0%}",
        ))

        signals.append(ReleaseSignal(
            name="Behavior Confidence",
            category="behavior",
            severity=_severity_from_value(behavior_confidence, 0.9, 0.7),
            score_contribution=w_beh * behavior_confidence * 100,
            detail=f"Behavior verification success: {behavior_confidence:.0%} ({total_behavior_steps} steps checked)",
        ))

        # Historical stability signal (15%): avg pass_rate across profiles with history
        historical_pass_rates = [p.get("pass_rate", 0.0) for p in profiles_with_history]
        avg_historical_pass_rate = sum(historical_pass_rates) / len(historical_pass_rates)
        report.historical_confidence = int(avg_historical_pass_rate * 100)

        signals.append(ReleaseSignal(
            name="Historical Stability",
            category="historical",
            severity=_severity_from_value(avg_historical_pass_rate, 0.85, 0.6),
            score_contribution=0.15 * avg_historical_pass_rate * 100,
            detail=f"Historical pass rate: {avg_historical_pass_rate:.0%} across {len(profiles_with_history)} test cases ({sum(p.get('total_runs', 0) for p in profiles_with_history)} total runs)",
        ))

        # Trend adjustment signal (10%): uses reliability_trend from profiles
        # reliability_trend: positive = improving, negative = degrading (-1 to +1)
        trend_values = [
            p.get("reliability_trend", 0.0)
            for p in profiles_with_history
        ]
        avg_trend = sum(trend_values) / len(trend_values) if trend_values else 0.0

        # Normalize trend to 0-100 (50 = neutral/stable)
        trend_raw = 50.0 + (avg_trend * 50.0)
        trend_raw = max(0.0, min(100.0, trend_raw))
        trend_contribution = 0.10 * trend_raw

        if avg_trend > 0.1:
            report.trend = "up"
            trend_detail = f"Reliability improving (trend: +{avg_trend:.2f})"
            trend_severity = "good"
        elif avg_trend < -0.1:
            report.trend = "down"
            trend_detail = f"Reliability declining (trend: {avg_trend:.2f})"
            trend_severity = "warning" if avg_trend > -0.3 else "critical"
        else:
            report.trend = "stable"
            trend_detail = f"Reliability stable (trend: {avg_trend:.2f})"
            trend_severity = "good"

        signals.append(ReleaseSignal(
            name="Reliability Trend",
            category="trend",
            severity=trend_severity,
            score_contribution=trend_contribution,
            detail=trend_detail,
        ))

        report.score_version = "v2"

    else:
        # V1 weights: exec/flake/perf/selector/behavior from global learner (7C.1).
        signals.append(ReleaseSignal(
            name="Execution Stability",
            category="execution",
            severity=_severity_from_value(weighted_pass_rate, 0.9, 0.7),
            score_contribution=gw["exec"] * weighted_pass_rate * 100,
            detail=f"Batch pass rate: {batch_pass_rate:.0%} ({passed}/{total} tests passed)",
            affected_tests=failed_tests,
        ))

        signals.append(ReleaseSignal(
            name="Flakiness",
            category="flakiness",
            severity=_severity_from_value(1.0 - max_flake_rate, 0.8, 0.5),
            score_contribution=gw["flake"] * max(0, 1.0 - max_flake_rate) * 100,
            detail=f"Max flake rate: {max_flake_rate:.0%}, avg: {avg_flake_rate:.0%} across {len(flake_rates)} test cases",
            affected_tests=flaky_test_names,
        ))

        signals.append(ReleaseSignal(
            name="Performance",
            category="timing",
            severity=_severity_from_value(1.0 - timing_anomaly_density, 0.85, 0.6),
            score_contribution=gw["perf"] * max(0, 1.0 - timing_anomaly_density) * 100,
            detail=f"{timing_anomaly_count} timing anomalies across {total_steps_checked} steps ({timing_anomaly_density:.0%})",
            affected_tests=[a["test_case_id"] for a in report.timing_anomalies[:5]],
        ))

        signals.append(ReleaseSignal(
            name="Selector Confidence",
            category="selector",
            severity=_severity_from_value(avg_selector_confidence, 0.85, 0.6),
            score_contribution=gw["selector"] * avg_selector_confidence * 100,
            detail=f"Average selector success rate: {avg_selector_confidence:.0%}",
        ))

        signals.append(ReleaseSignal(
            name="Behavior Confidence",
            category="behavior",
            severity=_severity_from_value(behavior_confidence, 0.9, 0.7),
            score_contribution=gw["behavior"] * behavior_confidence * 100,
            detail=f"Behavior verification success: {behavior_confidence:.0%} ({total_behavior_steps} steps checked)",
        ))

        report.score_version = "v1"

    report.signals = signals
    raw_score = sum(s.score_contribution for s in signals)

    # Apply healing penalty
    raw_score -= healing_penalty

    # --- 8. Blockers (hard caps) ---
    blockers: list[str] = []

    if batch_pass_rate == 0:
        blockers.append("All tests failed — no confidence in release")
        raw_score = min(raw_score, 5)
    elif batch_pass_rate < 0.5:
        blockers.append(f"Batch pass rate critically low: {batch_pass_rate:.0%}")
        raw_score = min(raw_score, 30)

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

    # V2: compute risk_delta vs historical norm
    if use_v2 and report.historical_confidence is not None:
        report.risk_delta = report.confidence_score - report.historical_confidence

    # Load per-org learned thresholds (2.2); fall back to defaults if not available
    deploy_threshold = 85
    caution_threshold = 60
    try:
        from app.intelligence.weight_learner import get_org_thresholds
        caution_threshold, deploy_threshold = get_org_thresholds(db, org_id)
    except Exception as e:
        logger.warning("release_scorer.thresholds_load_failed", error=str(e)[:200])

    recommendation, reasons = _score_to_recommendation(
        report.confidence_score, blockers,
        deploy_threshold=deploy_threshold,
        caution_threshold=caution_threshold,
    )
    report.recommendation = recommendation
    report.recommendation_reasons = reasons

    # --- 9. Root causes ---
    report.root_causes = _compute_root_causes(runs, telemetry_docs, profiles)

    logger.info(
        "release_scorer.computed",
        org_id=org_id,
        batch_id=batch_id,
        score=report.confidence_score,
        grade=report.confidence_grade,
        recommendation=report.recommendation,
        score_version=report.score_version,
        healing_penalty=round(healing_penalty, 1),
        trend=report.trend,
        risk_delta=report.risk_delta,
        signals=len(report.signals),
        blockers=len(report.blockers),
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
    """Identify and rank root causes from batch failures."""
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
                            reason_data[reason]["count"] += 1
                            reason_data[reason]["tests"].add(tc_title)
                            reason_data[reason]["steps"].append({
                                "test_case_title": tc_title,
                                "step_number": step.get("step_number", 0),
                                "action": step.get("action", "")[:80],
                                "error_message": msg[:200],
                            })
        else:
            reason_data["unknown"]["count"] += 1
            reason_data["unknown"]["tests"].add(tc_title)

    if not reason_data:
        return []

    total_tests = len(set(r.get("test_case_title", "") for r in runs))

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
    base = _REASON_DESCRIPTIONS.get(reason, f"Failure type: {reason}")
    test_count = len(data["tests"])
    occurrence_count = data["count"]
    return f"{base} — affected {test_count} test(s), {occurrence_count} occurrence(s)"
