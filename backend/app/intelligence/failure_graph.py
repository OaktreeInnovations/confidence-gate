"""Cross-Test Failure Intelligence Graph.

Detects systemic instability across multiple test cases by clustering
failures from recent telemetry data. Groups by error classification,
URL path, selector fingerprint, and environment pattern.

Produces:
- FailureClusters: groups of correlated failures across test cases
- FeatureRiskProfiles: per-tag/feature risk scoring
- ReleaseConfidenceScore: 0-100 aggregate confidence

All functions use sync pymongo (Celery worker context).
Pure read-only computation — no execution modification.

MongoDB collection: failure_graph
Document schema:
    {
        _id: ObjectId,
        org_id: str,
        computed_at: datetime,
        window_hours: int,
        clusters: [FailureCluster],
        feature_risks: [FeatureRiskProfile],
        release_confidence: ReleaseConfidenceReport,
    }
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog
from pymongo.database import Database

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class FailureInstance:
    """Single failure observation used for clustering."""
    test_run_id: str
    test_case_id: str
    step_number: int
    action: str
    error_type: str          # RetryReason value
    error_message: str
    selectors_tried: list[str] = field(default_factory=list)
    url_path: str = ""       # extracted from action/error
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FailureCluster:
    """Group of correlated failures across test cases."""
    cluster_id: str
    common_failure_type: str
    fingerprint: str          # hashed grouping key
    affected_test_cases: list[str] = field(default_factory=list)
    affected_test_runs: list[str] = field(default_factory=list)
    frequency: int = 0
    sample_error_messages: list[str] = field(default_factory=list)
    common_selectors: list[str] = field(default_factory=list)
    url_pattern: str = ""
    environment_pattern: str = ""
    severity_score: float = 0.0   # 0.0–1.0

    def to_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "common_failure_type": self.common_failure_type,
            "fingerprint": self.fingerprint,
            "affected_test_cases": self.affected_test_cases,
            "affected_test_runs": self.affected_test_runs,
            "frequency": self.frequency,
            "sample_error_messages": self.sample_error_messages[:3],
            "common_selectors": self.common_selectors[:5],
            "url_pattern": self.url_pattern,
            "environment_pattern": self.environment_pattern,
            "severity_score": round(self.severity_score, 4),
        }


@dataclass
class FeatureRiskProfile:
    """Risk assessment for a feature/tag grouping."""
    feature_name: str
    risk_score: float = 0.0       # 0.0–1.0
    trend_direction: str = "stable"  # "improving", "degrading", "stable"
    confidence_level: float = 0.0  # 0.0–1.0 (based on sample size)

    # Component metrics
    pass_rate: float = 0.0
    flake_rate: float = 0.0
    failure_density: float = 0.0    # failures per test case
    timing_anomaly_density: float = 0.0
    test_case_count: int = 0
    total_runs: int = 0

    def to_dict(self) -> dict:
        return {
            "feature_name": self.feature_name,
            "risk_score": round(self.risk_score, 4),
            "trend_direction": self.trend_direction,
            "confidence_level": round(self.confidence_level, 4),
            "pass_rate": round(self.pass_rate, 4),
            "flake_rate": round(self.flake_rate, 4),
            "failure_density": round(self.failure_density, 4),
            "timing_anomaly_density": round(self.timing_anomaly_density, 4),
            "test_case_count": self.test_case_count,
            "total_runs": self.total_runs,
        }


@dataclass
class ReleaseConfidenceReport:
    """Aggregate release confidence score."""
    score: int = 0               # 0–100
    grade: str = "F"             # A/B/C/D/F
    components: dict = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "grade": self.grade,
            "components": self.components,
            "blockers": self.blockers[:10],
        }


# ---------------------------------------------------------------------------
# Part 1 — Failure Clustering Engine
# ---------------------------------------------------------------------------

_URL_PATTERN = re.compile(r'https?://[^\s"\']+')
_PATH_PATTERN = re.compile(r'https?://[^/]+(/[^\s"\']*)')


def _extract_url_path(text: str) -> str:
    """Extract URL path from action text or error message."""
    match = _PATH_PATTERN.search(text)
    return match.group(1) if match else ""


def _selector_fingerprint(selectors: list[str]) -> str:
    """Create a stable fingerprint from selector patterns.

    Normalizes selectors by stripping specific text values to group
    structurally similar selectors together.
    """
    if not selectors:
        return ""
    # Keep selector type but normalize the value
    normalized = []
    for s in selectors[:5]:
        # Extract just the method name: get_by_role, locator, etc.
        method = s.split("(")[0] if "(" in s else s
        normalized.append(method)
    key = "|".join(sorted(set(normalized)))
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _error_fingerprint(error_type: str, error_msg: str) -> str:
    """Fingerprint an error by type + normalized message structure."""
    # Strip variable parts: line numbers, specific element text, timeouts
    normalized = re.sub(r'\d+', 'N', error_msg[:200].lower())
    normalized = re.sub(r'["\'][^"\']*["\']', '""', normalized)
    key = f"{error_type}:{normalized}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _compute_cluster_severity(
    cluster: FailureCluster,
    total_test_cases: int,
    total_runs: int,
) -> float:
    """Score cluster severity 0.0–1.0.

    Factors:
    - Breadth: fraction of test cases affected (40%)
    - Frequency: failure count relative to total runs (30%)
    - Error type weight: some errors are more severe (30%)
    """
    ERROR_WEIGHTS = {
        "page_crash": 1.0,
        "frame_detached": 0.9,
        "network_error": 0.8,
        "navigation_timeout": 0.6,
        "selector_not_found": 0.4,
        "selector_not_visible": 0.4,
        "selector_ambiguous": 0.3,
        "js_error": 0.7,
        "element_detached": 0.5,
        "code_gen_failure": 0.5,
        "assertion_failure": 0.3,
        "unknown": 0.3,
    }

    breadth = len(cluster.affected_test_cases) / max(total_test_cases, 1)
    breadth = min(breadth, 1.0)

    frequency = cluster.frequency / max(total_runs, 1)
    frequency = min(frequency, 1.0)

    error_weight = ERROR_WEIGHTS.get(cluster.common_failure_type, 0.3)

    return round(0.4 * breadth + 0.3 * frequency + 0.3 * error_weight, 4)


def collect_failure_instances(
    db: Database,
    org_id: str,
    window_hours: int = 72,
) -> list[FailureInstance]:
    """Collect recent failure instances from telemetry.

    Scans execution_telemetry for failed/errored steps within the window.
    Joins with test_runs to get test_case_id.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    # Get recent telemetry docs
    telemetry_docs = list(db.execution_telemetry.find(
        {"org_id": org_id, "created_at": {"$gte": cutoff}},
        {"test_run_id": 1, "steps": 1},
    ))

    if not telemetry_docs:
        return []

    # Map test_run_id → test_case_id
    run_ids = [doc["test_run_id"] for doc in telemetry_docs]
    from bson import ObjectId
    run_docs = {
        str(r["_id"]): r
        for r in db.test_runs.find(
            {"_id": {"$in": [ObjectId(rid) for rid in run_ids]}},
            {"test_case_id": 1, "created_at": 1},
        )
    }

    instances: list[FailureInstance] = []
    for tel_doc in telemetry_docs:
        run_id = tel_doc["test_run_id"]
        run_doc = run_docs.get(run_id)
        if not run_doc:
            continue
        test_case_id = str(run_doc["test_case_id"])
        run_ts = run_doc.get("created_at", datetime.now(timezone.utc))

        for step in tel_doc.get("steps", []):
            if step.get("status") not in ("error", "failed"):
                continue

            attempts = step.get("attempts", [])
            failed_attempts = [a for a in attempts if not a.get("success", False)]
            if not failed_attempts:
                continue

            # Use the last failed attempt for classification
            last_fail = failed_attempts[-1]
            error_type = last_fail.get("error_type", "unknown")
            error_msg = last_fail.get("error_message", "")
            action = step.get("action", "")

            instances.append(FailureInstance(
                test_run_id=run_id,
                test_case_id=test_case_id,
                step_number=step.get("step_number", 0),
                action=action,
                error_type=error_type,
                error_message=error_msg,
                selectors_tried=step.get("selectors_tried", []),
                url_path=_extract_url_path(action) or _extract_url_path(error_msg),
                timestamp=run_ts,
            ))

    logger.info(
        "failure_graph.instances_collected",
        org_id=org_id,
        count=len(instances),
        window_hours=window_hours,
    )
    return instances


def cluster_failures(
    instances: list[FailureInstance],
    total_test_cases: int,
    total_runs: int,
) -> list[FailureCluster]:
    """Group failure instances into clusters.

    Primary grouping: error_type
    Secondary grouping within type: error message fingerprint + selector fingerprint

    Returns clusters sorted by severity (descending).
    """
    if not instances:
        return []

    # Group by (error_type, error_fingerprint)
    buckets: dict[str, list[FailureInstance]] = defaultdict(list)
    for inst in instances:
        fp = _error_fingerprint(inst.error_type, inst.error_message)
        key = f"{inst.error_type}:{fp}"
        buckets[key].append(inst)

    clusters: list[FailureCluster] = []
    for key, bucket in buckets.items():
        error_type = key.split(":")[0]
        fp = key.split(":")[1]

        affected_tcs = sorted(set(i.test_case_id for i in bucket))
        affected_runs = sorted(set(i.test_run_id for i in bucket))

        # Aggregate selectors across cluster
        selector_counts: Counter[str] = Counter()
        for inst in bucket:
            for sel in inst.selectors_tried:
                selector_counts[sel] += 1
        common_selectors = [s for s, _ in selector_counts.most_common(5)]

        # URL pattern (most common path)
        url_counts: Counter[str] = Counter()
        for inst in bucket:
            if inst.url_path:
                url_counts[inst.url_path] += 1
        url_pattern = url_counts.most_common(1)[0][0] if url_counts else ""

        # Deduplicated error messages
        seen_msgs: set[str] = set()
        sample_msgs: list[str] = []
        for inst in bucket:
            short = inst.error_message[:200]
            if short and short not in seen_msgs:
                seen_msgs.add(short)
                sample_msgs.append(short)
                if len(sample_msgs) >= 3:
                    break

        cluster_id = f"fc-{error_type}-{fp}"
        cluster = FailureCluster(
            cluster_id=cluster_id,
            common_failure_type=error_type,
            fingerprint=fp,
            affected_test_cases=affected_tcs,
            affected_test_runs=affected_runs,
            frequency=len(bucket),
            sample_error_messages=sample_msgs,
            common_selectors=common_selectors,
            url_pattern=url_pattern,
        )
        cluster.severity_score = _compute_cluster_severity(
            cluster, total_test_cases, total_runs,
        )
        clusters.append(cluster)

    clusters.sort(key=lambda c: -c.severity_score)

    logger.info(
        "failure_graph.clusters_computed",
        cluster_count=len(clusters),
        top_severity=clusters[0].severity_score if clusters else 0,
    )
    return clusters


# ---------------------------------------------------------------------------
# Part 2 — Feature Risk Scoring
# ---------------------------------------------------------------------------


def compute_feature_risks(
    db: Database,
    org_id: str,
    clusters: list[FailureCluster],
) -> list[FeatureRiskProfile]:
    """Compute risk profiles per tag/feature.

    Aggregates execution_profiles data grouped by test case tags.
    Factors in cluster data for failure density.
    """
    from bson import ObjectId

    # Load all test cases for this org with their tags
    tc_docs = list(db.test_cases.find(
        {"org_id": ObjectId(org_id), "status": {"$ne": "archived"}},
        {"_id": 1, "tags": 1, "title": 1},
    ))

    if not tc_docs:
        return []

    # Build tag → test_case_ids mapping
    tag_to_tcs: dict[str, list[str]] = defaultdict(list)
    tc_id_to_tags: dict[str, list[str]] = {}
    for tc in tc_docs:
        tc_id = str(tc["_id"])
        tags = tc.get("tags", [])
        tc_id_to_tags[tc_id] = tags
        for tag in tags:
            tag_to_tcs[tag].append(tc_id)

    if not tag_to_tcs:
        return []

    # Load execution profiles for this org
    profile_by_tc: dict[str, dict] = {}
    for profile in db.execution_profiles.find({"org_id": org_id}):
        profile_by_tc[profile["test_case_id"]] = profile

    # Build cluster failure count per test case
    tc_cluster_failures: Counter[str] = Counter()
    for cluster in clusters:
        for tc_id in cluster.affected_test_cases:
            tc_cluster_failures[tc_id] += cluster.frequency

    risks: list[FeatureRiskProfile] = []

    for tag, tc_ids in sorted(tag_to_tcs.items()):
        tc_count = len(tc_ids)

        # Aggregate metrics from execution profiles
        pass_rates: list[float] = []
        flake_rates: list[float] = []
        total_runs = 0
        trends: list[float] = []
        timing_anomaly_count = 0

        for tc_id in tc_ids:
            profile = profile_by_tc.get(tc_id)
            if not profile:
                continue
            pass_rates.append(profile.get("pass_rate", 0.0))
            flake_rates.append(profile.get("flake_rate", 0.0))
            total_runs += profile.get("total_runs", 0)
            trends.append(profile.get("reliability_trend", 0.0))

            # Count steps with timing anomalies
            baselines = profile.get("timing_baselines", {})
            for _, step_bl in baselines.get("steps", {}).items():
                if step_bl.get("stddev_ms", 0) > step_bl.get("median_ms", 1) * 0.5:
                    timing_anomaly_count += 1

        if not pass_rates:
            # No profile data — low confidence
            risks.append(FeatureRiskProfile(
                feature_name=tag,
                risk_score=0.5,
                confidence_level=0.0,
                test_case_count=tc_count,
            ))
            continue

        avg_pass_rate = sum(pass_rates) / len(pass_rates)
        avg_flake_rate = sum(flake_rates) / len(flake_rates)

        # Failure density: cluster failures per test case
        total_cluster_failures = sum(tc_cluster_failures.get(tc_id, 0) for tc_id in tc_ids)
        failure_density = total_cluster_failures / tc_count if tc_count else 0.0

        # Timing anomaly density
        timing_anomaly_density = timing_anomaly_count / tc_count if tc_count else 0.0

        # Trend direction
        avg_trend = sum(trends) / len(trends) if trends else 0.0
        if avg_trend > 0.05:
            trend_dir = "improving"
        elif avg_trend < -0.05:
            trend_dir = "degrading"
        else:
            trend_dir = "stable"

        # Confidence level based on sample size
        confidence = min(total_runs / (tc_count * 5), 1.0)

        # Risk score: weighted combination
        # Higher = worse
        risk = (
            0.35 * (1.0 - avg_pass_rate) +    # pass rate inverted
            0.25 * avg_flake_rate +             # flakiness
            0.25 * min(failure_density / 5.0, 1.0) +  # failure density normalized
            0.15 * min(timing_anomaly_density, 1.0)    # timing instability
        )
        risk = min(risk, 1.0)

        risks.append(FeatureRiskProfile(
            feature_name=tag,
            risk_score=risk,
            trend_direction=trend_dir,
            confidence_level=confidence,
            pass_rate=avg_pass_rate,
            flake_rate=avg_flake_rate,
            failure_density=failure_density,
            timing_anomaly_density=timing_anomaly_density,
            test_case_count=tc_count,
            total_runs=total_runs,
        ))

    risks.sort(key=lambda r: -r.risk_score)

    logger.info(
        "failure_graph.feature_risks_computed",
        org_id=org_id,
        feature_count=len(risks),
        top_risk=risks[0].feature_name if risks else "",
        top_score=risks[0].risk_score if risks else 0,
    )
    return risks


# ---------------------------------------------------------------------------
# Part 3 — Release Confidence Score
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


def compute_release_confidence(
    db: Database,
    org_id: str,
    clusters: list[FailureCluster],
    feature_risks: list[FeatureRiskProfile],
) -> ReleaseConfidenceReport:
    """Compute aggregate release confidence score (0–100).

    Components:
    1. Test stability (30%): org-wide pass rate
    2. Flake distribution (20%): org-wide flake rate
    3. Cluster severity (30%): weighted severity of active clusters
    4. Feature risk (20%): average feature risk score

    Blockers: conditions that cap the score.
    """
    # Load all profiles for this org
    profiles = list(db.execution_profiles.find({"org_id": org_id}))

    # 1. Test stability — org-wide pass rate
    if profiles:
        weighted_pass = sum(
            p.get("pass_rate", 0) * p.get("total_runs", 0)
            for p in profiles
        )
        total_run_weight = sum(p.get("total_runs", 0) for p in profiles)
        org_pass_rate = weighted_pass / total_run_weight if total_run_weight else 0.0
    else:
        org_pass_rate = 0.0

    stability_score = org_pass_rate * 100  # 0–100

    # 2. Flake distribution
    if profiles:
        weighted_flake = sum(
            p.get("flake_rate", 0) * p.get("total_runs", 0)
            for p in profiles
        )
        total_run_weight = sum(p.get("total_runs", 0) for p in profiles)
        org_flake_rate = weighted_flake / total_run_weight if total_run_weight else 0.0
    else:
        org_flake_rate = 0.0

    flake_score = max(0, (1.0 - org_flake_rate * 2)) * 100  # penalize flakes heavily

    # 3. Cluster severity
    if clusters:
        # Weighted sum of cluster severities, capped at 1.0
        max_severity = max(c.severity_score for c in clusters)
        avg_severity = sum(c.severity_score for c in clusters) / len(clusters)
        combined_severity = 0.6 * max_severity + 0.4 * avg_severity
        cluster_score = max(0, (1.0 - combined_severity)) * 100
    else:
        cluster_score = 100.0

    # 4. Feature risk aggregation
    if feature_risks:
        # Weight by confidence level
        weighted_risk = sum(
            fr.risk_score * fr.confidence_level
            for fr in feature_risks
        )
        total_confidence = sum(fr.confidence_level for fr in feature_risks)
        avg_risk = weighted_risk / total_confidence if total_confidence else 0.0
        feature_score = max(0, (1.0 - avg_risk)) * 100
    else:
        feature_score = 50.0  # no data = uncertain

    # Weighted combination
    raw_score = (
        0.30 * stability_score +
        0.20 * flake_score +
        0.30 * cluster_score +
        0.20 * feature_score
    )

    # Blockers — conditions that cap the score
    blockers: list[str] = []

    if clusters:
        critical_clusters = [c for c in clusters if c.severity_score >= 0.7]
        if critical_clusters:
            blocker_msg = (
                f"{len(critical_clusters)} critical failure cluster(s): "
                f"{', '.join(c.common_failure_type for c in critical_clusters[:3])}"
            )
            blockers.append(blocker_msg)
            raw_score = min(raw_score, 50)  # cap at 50 with critical clusters

    if org_pass_rate < 0.5:
        blockers.append(f"Org pass rate critically low: {org_pass_rate:.0%}")
        raw_score = min(raw_score, 30)

    high_risk_features = [fr for fr in feature_risks if fr.risk_score >= 0.7 and fr.confidence_level >= 0.5]
    if high_risk_features:
        blocker_msg = (
            f"{len(high_risk_features)} high-risk feature(s): "
            f"{', '.join(fr.feature_name for fr in high_risk_features[:3])}"
        )
        blockers.append(blocker_msg)
        raw_score = min(raw_score, 60)

    score = max(0, min(100, int(raw_score)))

    report = ReleaseConfidenceReport(
        score=score,
        grade=_score_to_grade(score),
        components={
            "stability": round(stability_score, 1),
            "flake": round(flake_score, 1),
            "clusters": round(cluster_score, 1),
            "feature_risk": round(feature_score, 1),
            "org_pass_rate": round(org_pass_rate, 4),
            "org_flake_rate": round(org_flake_rate, 4),
            "cluster_count": len(clusters),
            "feature_risk_count": len(feature_risks),
        },
        blockers=blockers,
    )

    logger.info(
        "failure_graph.release_confidence",
        org_id=org_id,
        score=score,
        grade=report.grade,
        blockers=len(blockers),
    )
    return report


# ---------------------------------------------------------------------------
# Orchestrator — computes full graph and stores it
# ---------------------------------------------------------------------------


def compute_failure_graph(
    db: Database,
    org_id: str,
    window_hours: int = 72,
) -> dict:
    """Compute the full failure intelligence graph for an org.

    Returns the graph document (also stored in MongoDB).
    """
    from bson import ObjectId

    # Count total test cases and runs for severity normalization
    total_test_cases = db.test_cases.count_documents({
        "org_id": ObjectId(org_id),
        "status": {"$ne": "archived"},
    })
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    total_runs = db.test_runs.count_documents({
        "org_id": ObjectId(org_id),
        "status": {"$in": ["passed", "failed", "error"]},
        "created_at": {"$gte": cutoff},
    })

    # Part 1: Cluster failures
    instances = collect_failure_instances(db, org_id, window_hours)
    clusters = cluster_failures(instances, total_test_cases, total_runs)

    # Part 2: Feature risk
    feature_risks = compute_feature_risks(db, org_id, clusters)

    # Part 3: Release confidence
    release_confidence = compute_release_confidence(
        db, org_id, clusters, feature_risks,
    )

    now = datetime.now(timezone.utc)
    graph_doc = {
        "org_id": org_id,
        "computed_at": now,
        "window_hours": window_hours,
        "total_test_cases": total_test_cases,
        "total_runs_in_window": total_runs,
        "total_failure_instances": len(instances),
        "clusters": [c.to_dict() for c in clusters],
        "feature_risks": [fr.to_dict() for fr in feature_risks],
        "release_confidence": release_confidence.to_dict(),
    }

    # Upsert — one graph per org
    db.failure_graph.update_one(
        {"org_id": org_id},
        {"$set": graph_doc},
        upsert=True,
    )

    logger.info(
        "failure_graph.stored",
        org_id=org_id,
        clusters=len(clusters),
        features=len(feature_risks),
        confidence=release_confidence.score,
    )
    return graph_doc
