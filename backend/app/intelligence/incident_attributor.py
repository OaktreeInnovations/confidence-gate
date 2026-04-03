"""7C.2 Incident signal attribution.

When outcome=production_failed is recorded, computes which signals at decision
time were most anomalous versus the org's recent passing releases (z-score
per signal).  Result is stored as incident_signal_attribution on the validation.
"""

from __future__ import annotations

import math

import structlog
from pymongo.database import Database

logger = structlog.get_logger(__name__)

_SIGNALS = [
    # (name, report_path, direction)
    # direction: "higher_is_better" — low value at failure → negative z-score is bad
    ("pass_rate",        "batch_summary.pass_rate",  "higher_is_better"),
    ("instability_index","instability_index",         "lower_is_better"),
    ("coverage_score",   "coverage_score",            "higher_is_better"),
    ("ai_adjustment",    "ai_adjustment",             "higher_is_better"),
]

_LOOKBACK = 50  # last N passing validations for baseline


def _get_report_value(report: dict, path: str) -> float | None:
    """Drill into a dot-separated path in the report dict."""
    parts = path.split(".")
    node: dict | float | None = report
    for part in parts:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return float(node) if node is not None else None


def _mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 1.0
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 1.0
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    std = math.sqrt(variance) if variance > 0 else 1.0
    return mean, std


def compute_attribution(
    db: Database,
    validation_id_str: str,
    org_id_str: str,
) -> list[dict]:
    """Return incident_signal_attribution list for the given validation."""
    from bson import ObjectId

    val_oid = ObjectId(validation_id_str)
    org_oid = ObjectId(org_id_str)

    # Load the failed validation
    failed_doc = db.release_validations.find_one(
        {"_id": val_oid, "org_id": org_oid},
        {"report": 1, "project_id": 1},
    )
    if not failed_doc:
        logger.warning("incident_attributor.not_found", validation_id=validation_id_str)
        return []

    failed_report = failed_doc.get("report", {})
    project_id = failed_doc.get("project_id")

    # Baseline: last N passing validations for the same project
    baseline_cursor = db.release_validations.find(
        {
            "org_id": org_oid,
            "project_id": project_id,
            "outcome": "production_passed",
            "status": "completed",
        },
        {"report": 1},
    ).sort("completed_at", -1).limit(_LOOKBACK)

    baseline_docs = list(baseline_cursor)

    if not baseline_docs:
        # No baseline — still produce attribution with org-wide baseline
        baseline_cursor = db.release_validations.find(
            {
                "org_id": org_oid,
                "outcome": "production_passed",
                "status": "completed",
            },
            {"report": 1},
        ).sort("completed_at", -1).limit(_LOOKBACK)
        baseline_docs = list(baseline_cursor)

    if not baseline_docs:
        return []

    attribution: list[dict] = []

    for signal_name, path, direction in _SIGNALS:
        # Collect baseline values
        baseline_values = []
        for doc in baseline_docs:
            v = _get_report_value(doc.get("report", {}), path)
            if v is not None:
                baseline_values.append(v)

        if not baseline_values:
            continue

        failed_value = _get_report_value(failed_report, path)
        if failed_value is None:
            continue

        mean, std = _mean_std(baseline_values)
        z = (failed_value - mean) / std

        # Determine directionality of the anomaly
        if direction == "higher_is_better":
            anomaly_direction = "low" if z < 0 else "high"
        else:
            anomaly_direction = "high" if z > 0 else "low"

        attribution.append({
            "signal": signal_name,
            "value": round(failed_value, 4),
            "org_mean": round(mean, 4),
            "z_score": round(z, 3),
            "direction": anomaly_direction,
        })

    # Sort by absolute z-score descending (most anomalous first)
    attribution.sort(key=lambda x: abs(x["z_score"]), reverse=True)
    return attribution


def store_attribution(
    db: Database,
    validation_id_str: str,
    org_id_str: str,
) -> None:
    """Compute and persist incident_signal_attribution on the validation document."""
    from bson import ObjectId

    try:
        attribution = compute_attribution(db, validation_id_str, org_id_str)
        db.release_validations.update_one(
            {"_id": ObjectId(validation_id_str)},
            {"$set": {"incident_signal_attribution": attribution}},
        )
        logger.info(
            "incident_attributor.stored",
            validation_id=validation_id_str,
            signals=len(attribution),
        )
    except Exception as e:
        logger.warning("incident_attributor.error", error=str(e)[:300])
