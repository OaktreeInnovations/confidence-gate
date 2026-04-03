"""Learns per-org decision thresholds from historical production outcomes.

The default release scorer uses fixed thresholds: BLOCK <60, CAUTION 60-84,
DEPLOY ≥85. These were chosen heuristically. Once an org accumulates ≥30
labeled outcomes (synced by sync_outcomes_to_calibration), this module computes
data-driven thresholds by finding the score boundary where incident probability
crosses 50%.

Learned thresholds are stored in `org_learned_thresholds`:
  { org_id, block_threshold, caution_threshold, data_points, computed_at }

`release_scorer._score_to_recommendation` falls back to defaults (60/85) when
no learned thresholds exist. Thresholds are updated nightly.

Algorithm (no external ML libraries required):
  1. Group outcomes into 10-point score buckets
  2. Compute incident rate per bucket
  3. block_threshold  = highest score where incident_rate > 0.5  (with min=40, max=75)
  4. caution_threshold = highest score where incident_rate > 0.2  (with min=block+10, max=90)
  5. Require ≥5 data points per bucket before trusting it; otherwise use defaults
"""

from __future__ import annotations

from datetime import datetime, timezone

from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)

_MIN_DATA_POINTS = 30       # minimum total outcomes before using learned thresholds
_MIN_BUCKET_SIZE = 3        # minimum outcomes per bucket to trust that bucket
_DEFAULT_BLOCK = 60
_DEFAULT_CAUTION = 85


def _bucket(score: int) -> int:
    """Round down to nearest 10."""
    return (max(0, min(100, score)) // 10) * 10


def _compute_raw_thresholds(outcomes: list[dict]) -> tuple[int | None, int | None, dict]:
    """Derive block/caution thresholds from outcome data. Returns (block, caution, rates).

    Returns (None, None, {}) when there are not enough trusted buckets to draw conclusions.
    """
    bucket_totals: dict[int, int] = {}
    bucket_incidents: dict[int, int] = {}

    for o in outcomes:
        score = int(o.get("confidence_score") or 0)
        b = _bucket(score)
        bucket_totals[b] = bucket_totals.get(b, 0) + 1
        if o.get("incident_occurred", False):
            bucket_incidents[b] = bucket_incidents.get(b, 0) + 1

    incident_rates: dict[int, float] = {}
    for b, total in bucket_totals.items():
        if total >= _MIN_BUCKET_SIZE:
            incident_rates[b] = bucket_incidents.get(b, 0) / total

    if not incident_rates:
        return None, None, {}

    raw_block: int = _DEFAULT_BLOCK
    for b in sorted(incident_rates, reverse=True):
        if incident_rates[b] > 0.5:
            raw_block = min(75, max(40, b + 10))
            break

    raw_caution: int = _DEFAULT_CAUTION
    for b in sorted(incident_rates, reverse=True):
        if incident_rates[b] > 0.2:
            raw_caution = min(90, max(raw_block + 10, b + 10))
            break

    return raw_block, raw_caution, incident_rates


def compute_org_thresholds(db: Database, org_id: str) -> dict:
    """Compute learned decision thresholds for one org from outcome history.

    Cold-start acceleration: even with <30 outcomes, partially interpolate toward
    the observed data so the system begins adapting immediately instead of waiting
    for a full 30-sample cliff.
    """
    outcomes = list(db.production_outcomes.find({"org_id": org_id}))
    outcome_count = len(outcomes)

    if outcome_count == 0:
        return {
            "org_id": org_id,
            "block_threshold": _DEFAULT_BLOCK,
            "caution_threshold": _DEFAULT_CAUTION,
            "data_points": 0,
            "source": "default_no_data",
            "computed_at": datetime.now(timezone.utc),
        }

    raw_block, raw_caution, incident_rates = _compute_raw_thresholds(outcomes)

    if raw_block is None:
        # No trusted buckets yet — stay on pure defaults
        return {
            "org_id": org_id,
            "block_threshold": _DEFAULT_BLOCK,
            "caution_threshold": _DEFAULT_CAUTION,
            "data_points": outcome_count,
            "source": "default_sparse_buckets",
            "computed_at": datetime.now(timezone.utc),
        }

    # Blend weight: 0 at 0 outcomes → 1 at _MIN_DATA_POINTS outcomes.
    # This means every outcome nudges the threshold slightly toward the observed data.
    blend = min(1.0, outcome_count / _MIN_DATA_POINTS)
    block_threshold = int(round(_DEFAULT_BLOCK * (1 - blend) + raw_block * blend))
    caution_threshold = int(round(_DEFAULT_CAUTION * (1 - blend) + raw_caution * blend))
    # Enforce ordering invariant
    caution_threshold = max(caution_threshold, block_threshold + 10)

    source = "learned" if outcome_count >= _MIN_DATA_POINTS else "partial"

    result = {
        "org_id": org_id,
        "block_threshold": block_threshold,
        "caution_threshold": caution_threshold,
        "data_points": outcome_count,
        "blend_weight": round(blend, 3),
        "source": source,
        "bucket_incident_rates": {str(k): round(v, 3) for k, v in sorted(incident_rates.items())},
        "computed_at": datetime.now(timezone.utc),
    }

    db.org_learned_thresholds.update_one(
        {"org_id": org_id},
        {"$set": result},
        upsert=True,
    )

    logger.info(
        "weight_learner.computed",
        org_id=org_id,
        block_threshold=block_threshold,
        caution_threshold=caution_threshold,
        data_points=outcome_count,
        blend_weight=round(blend, 3),
        source=source,
    )

    return result


def get_org_thresholds(db: Database, org_id: str) -> tuple[int, int]:
    """Return (block_threshold, caution_threshold) for org, using defaults as fallback.

    Accepts both 'learned' (≥30 outcomes) and 'partial' (<30 outcomes, blended) sources.
    Results are cached in Redis for 60 seconds to avoid redundant MongoDB reads.
    """
    from app.intelligence import cache as _cache
    cache_key = f"cg:thresholds:{org_id}"
    cached = _cache.get(cache_key)
    if cached and isinstance(cached, dict):
        return cached["block_threshold"], cached["caution_threshold"]

    doc = db.org_learned_thresholds.find_one({"org_id": org_id})
    if doc and doc.get("source") in ("learned", "partial"):
        _cache.set(cache_key, {
            "block_threshold": doc["block_threshold"],
            "caution_threshold": doc["caution_threshold"],
        }, ttl=60)
        return doc["block_threshold"], doc["caution_threshold"]
    return _DEFAULT_BLOCK, _DEFAULT_CAUTION
