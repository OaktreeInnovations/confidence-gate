"""7C.1 Cross-org signal weight optimization.

Nightly task aggregates (pass_rate, flake_rate, instability_index, coverage_score)
across all orgs and fits a logistic regression to predict outcome=production_failed.

Learned coefficients are normalized to a weight distribution and stored in the
`global_signal_weights` collection, which release_scorer.py loads as dynamic signal
weights (with hardcoded-defaults fallback).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import structlog
from pymongo.database import Database

logger = structlog.get_logger(__name__)

_MIN_SAMPLES = 20
_DEFAULT_WEIGHTS = {
    "exec": 0.30,
    "flake": 0.20,
    "perf": 0.15,
    "selector": 0.15,
    "behavior": 0.20,
}


# ---------------------------------------------------------------------------
# Pure-Python logistic regression (no scipy/sklearn dependency)
# ---------------------------------------------------------------------------


def _sigmoid(z: float) -> float:
    z = max(-500.0, min(500.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def _fit_logistic(
    X: list[list[float]],
    y: list[float],
    lr: float = 0.05,
    epochs: int = 300,
    pos_weight: float = 1.0,
) -> list[float]:
    """Batch gradient-descent logistic regression with class weighting.

    pos_weight scales the gradient contribution of positive (failure) samples,
    compensating for class imbalance when failures are rare.
    Returns weight vector (no bias).
    """
    if not X or len(X) != len(y):
        return []
    n = len(X)
    k = len(X[0])
    w = [0.0] * k
    for _ in range(epochs):
        grad = [0.0] * k
        for xi, yi in zip(X, y):
            z = sum(w[j] * xi[j] for j in range(k))
            err = _sigmoid(z) - yi
            # 9A.7 Scale gradient for positive samples to counter class imbalance
            sample_weight = pos_weight if yi == 1.0 else 1.0
            for j in range(k):
                grad[j] += sample_weight * err * xi[j]
        for j in range(k):
            w[j] -= lr * grad[j] / n
    return w


def _normalize_weights(raw: list[float]) -> list[float]:
    """Normalize abs(raw) to sum to 1.0."""
    abs_vals = [abs(v) for v in raw]
    total = sum(abs_vals) or 1.0
    return [v / total for v in abs_vals]


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------


def _extract_samples(db: Database) -> tuple[list[list[float]], list[float]]:
    """Return (X, y) from all completed validations with known outcomes.

    Features (4): pass_rate, (1-max_flake_rate), (1-instability/100), coverage/100
    Target: 1.0 if production_failed, 0.0 if production_passed
    """
    docs = list(db.release_validations.find(
        {
            "status": "completed",
            "outcome": {"$in": ["production_passed", "production_failed"]},
            "confidence_score": {"$ne": None},
        },
        {
            "outcome": 1,
            "report.batch_summary.pass_rate": 1,
            "report.instability_index": 1,
            "report.coverage_score": 1,
            "report.flaky_tests": 1,
        },
    ))

    X: list[list[float]] = []
    y: list[float] = []

    for doc in docs:
        outcome = doc.get("outcome")
        if outcome not in ("production_passed", "production_failed"):
            continue

        report = doc.get("report", {})
        batch = report.get("batch_summary") or {}
        pass_rate = float(batch.get("pass_rate", 0.0))
        instability = float(report.get("instability_index") or 0.0)
        coverage = float(report.get("coverage_score") or 0.0)
        flaky_tests = report.get("flaky_tests") or []
        if flaky_tests:
            max_flake = max((float(ft.get("flake_rate", 0.0)) for ft in flaky_tests), default=0.0)
        else:
            max_flake = 0.0

        features = [
            pass_rate,
            max(0.0, 1.0 - max_flake),
            max(0.0, 1.0 - instability / 100.0),
            min(1.0, coverage / 100.0),
        ]
        label = 1.0 if outcome == "production_failed" else 0.0
        X.append(features)
        y.append(label)

    return X, y


# ---------------------------------------------------------------------------
# Main optimizer
# ---------------------------------------------------------------------------


def run_global_optimization(db: Database) -> dict | None:
    """Fit logistic regression and upsert learned weights.

    Returns the stored document, or None if insufficient data.
    """
    X, y = _extract_samples(db)

    if len(X) < _MIN_SAMPLES:
        logger.info(
            "global_weight_optimizer.insufficient_data",
            sample_count=len(X),
            min_required=_MIN_SAMPLES,
        )
        return None

    positive = sum(y)
    negative = len(y) - positive

    if positive == 0 or negative == 0:
        logger.info("global_weight_optimizer.no_variance", positive=positive, negative=negative)
        return None

    # 9A.7 Class weighting: upweight positive (failure) samples proportionally
    # so the model doesn't learn "predict pass always" on typical org data
    pos_weight = negative / positive if positive > 0 else 1.0

    raw_w = _fit_logistic(X, y, pos_weight=pos_weight)
    norm_w = _normalize_weights(raw_w)

    # Map normalized importance to signal weight categories.
    # Feature order: exec, flake, instability, coverage
    # V1 residual (perf + selector + behavior) gets what exec+flake don't use.
    exec_w = norm_w[0]
    flake_w = norm_w[1]
    instability_w = norm_w[2]
    coverage_w = norm_w[3]

    # Redistribute: exec and flake are direct V1 signals.
    # instability and coverage inform the remaining V1 signals (perf, selector, behavior)
    # proportionally.
    direct_total = exec_w + flake_w
    residual_total = instability_w + coverage_w
    # Scale direct to sum 0.5, residual to sum 0.5 — preserves balance
    scale = 1.0
    if direct_total + residual_total > 0:
        scale = 1.0 / (direct_total + residual_total)
    exec_w *= scale
    flake_w *= scale
    instability_w *= scale
    coverage_w *= scale

    # Distribute residual across perf(0.15), selector(0.15), behavior(0.20)
    residual = instability_w + coverage_w
    orig_residual = 0.15 + 0.15 + 0.20  # 0.50
    perf_w = residual * (0.15 / orig_residual)
    selector_w = residual * (0.15 / orig_residual)
    behavior_w = residual * (0.20 / orig_residual)

    doc = {
        "exec": round(exec_w, 4),
        "flake": round(flake_w, 4),
        "perf": round(perf_w, 4),
        "selector": round(selector_w, 4),
        "behavior": round(behavior_w, 4),
        "raw_coefficients": [round(v, 6) for v in raw_w],
        "feature_order": ["pass_rate", "inv_flake_rate", "inv_instability", "coverage_norm"],
        "sample_count": len(X),
        "positive_outcomes": int(positive),
        "negative_outcomes": int(negative),
        "pos_weight_used": round(pos_weight, 4),
        "computed_at": datetime.now(timezone.utc),
    }

    db.global_signal_weights.update_one(
        {"_type": "global"},
        {"$set": {**doc, "_type": "global"}},
        upsert=True,
    )

    logger.info(
        "global_weight_optimizer.done",
        sample_count=len(X),
        exec_w=doc["exec"],
        flake_w=doc["flake"],
        perf_w=doc["perf"],
        selector_w=doc["selector"],
        behavior_w=doc["behavior"],
    )

    return doc


def load_global_weights(db: Database) -> dict:
    """Load learned global signal weights.  Falls back to hardcoded defaults."""
    try:
        doc = db.global_signal_weights.find_one({"_type": "global"})
        if not doc:
            return _DEFAULT_WEIGHTS.copy()

        computed_at = doc.get("computed_at")
        if computed_at:
            age_days = (datetime.now(timezone.utc) - computed_at).total_seconds() / 86400
            if age_days > 7:
                logger.info("global_weight_optimizer.stale_weights", age_days=round(age_days, 1))
                return _DEFAULT_WEIGHTS.copy()

        return {
            "exec": float(doc.get("exec", _DEFAULT_WEIGHTS["exec"])),
            "flake": float(doc.get("flake", _DEFAULT_WEIGHTS["flake"])),
            "perf": float(doc.get("perf", _DEFAULT_WEIGHTS["perf"])),
            "selector": float(doc.get("selector", _DEFAULT_WEIGHTS["selector"])),
            "behavior": float(doc.get("behavior", _DEFAULT_WEIGHTS["behavior"])),
        }
    except Exception as e:
        logger.warning("global_weight_optimizer.load_error", error=str(e)[:200])
        return _DEFAULT_WEIGHTS.copy()
