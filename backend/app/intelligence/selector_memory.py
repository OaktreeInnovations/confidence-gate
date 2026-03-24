"""Selector Execution Memory — learns which selectors work for each step.

Tracks per-step selector history across runs, building a ranked memory
of which Playwright selectors succeed or fail. This memory is queried
before code generation to give the AI known-good selector hints.

Storage: `selector_memory` collection in MongoDB.
One document per (test_case_id, step_number, org_id).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from bson import ObjectId
from pymongo.database import Database
import structlog

from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class SelectorRecord(BaseModel):
    """Historical record for a single selector."""
    selector: str                  # e.g. 'get_by_role("button", name="Submit")' or strategy name
    strategy: str = ""             # e.g. "role", "test_id", "label"
    params: dict = Field(default_factory=dict)  # e.g. {"role": "button", "name": "Submit"}
    success_count: int = 0
    failure_count: int = 0
    last_used: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_succeeded: datetime | None = None

    @property
    def total_uses(self) -> int:
        return self.success_count + self.failure_count

    @property
    def success_rate(self) -> float:
        if self.total_uses == 0:
            return 0.0
        return self.success_count / self.total_uses


class StepSelectorMemory(BaseModel):
    """Aggregated selector memory for one step of a test case."""
    test_case_id: str
    step_number: int
    org_id: str
    action: str = ""

    selectors: list[SelectorRecord] = Field(default_factory=list)

    # Best selector: highest success rate with >= 2 observations
    best_selector: str = ""
    best_selector_success_rate: float = 0.0

    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
    }


def update_selector_memory(
    db: Database,
    test_case_id: str,
    org_id: str,
    step_number: int,
    action: str,
    selectors_tried: list[str],
    selector_that_worked: str,
    selectors_structured: list[dict] | None = None,
) -> None:
    """Update selector memory for a step based on a single run's telemetry.

    Increments success/failure counts for each selector tried.
    The selector_that_worked gets a success; all others get a failure.

    When selectors_structured is provided (list of dicts with strategy, params,
    etc. from resolve_target telemetry), the structured data is stored alongside
    the string selector for feeding back into the resolver.
    """
    if not selectors_tried:
        return

    now = datetime.now(timezone.utc)
    query = {
        "test_case_id": test_case_id,
        "step_number": step_number,
        "org_id": org_id,
    }

    existing = db.selector_memory.find_one(query)

    if existing:
        selector_map: dict[str, dict] = {
            s["selector"]: s for s in existing.get("selectors", [])
        }
    else:
        selector_map = {}

    # Build a lookup from strategy name to structured data
    structured_by_strategy: dict[str, dict] = {}
    if selectors_structured:
        for sd in selectors_structured:
            strategy = sd.get("strategy", "")
            if strategy:
                structured_by_strategy[strategy] = sd

    for sel in selectors_tried:
        if sel not in selector_map:
            selector_map[sel] = {
                "selector": sel,
                "strategy": "",
                "params": {},
                "success_count": 0,
                "failure_count": 0,
                "last_used": now,
                "last_succeeded": None,
            }

        record = selector_map[sel]
        record["last_used"] = now

        # Enrich with structured data if available
        if sel in structured_by_strategy:
            sd = structured_by_strategy[sel]
            record["strategy"] = sd.get("strategy", record.get("strategy", ""))
            if sd.get("params"):
                record["params"] = sd["params"]

        if sel == selector_that_worked:
            record["success_count"] = record.get("success_count", 0) + 1
            record["last_succeeded"] = now
        else:
            record["failure_count"] = record.get("failure_count", 0) + 1

    # Compute best selector (>= 2 observations, highest success rate)
    best_selector = ""
    best_rate = 0.0
    for sel, rec in selector_map.items():
        total = rec.get("success_count", 0) + rec.get("failure_count", 0)
        if total >= 2:
            rate = rec["success_count"] / total
            if rate > best_rate or (rate == best_rate and total > (
                selector_map.get(best_selector, {}).get("success_count", 0) +
                selector_map.get(best_selector, {}).get("failure_count", 0)
            )):
                best_rate = rate
                best_selector = sel

    update_doc = {
        "$set": {
            "selectors": list(selector_map.values()),
            "action": action,
            "best_selector": best_selector,
            "best_selector_success_rate": round(best_rate, 4),
            "last_updated": now,
        },
        "$setOnInsert": {
            "test_case_id": test_case_id,
            "step_number": step_number,
            "org_id": org_id,
        },
    }

    db.selector_memory.update_one(query, update_doc, upsert=True)


_DECAY_HALF_LIFE_DAYS = 30.0
_STALE_THRESHOLD_DAYS = 90.0


def get_known_good_selectors(
    db: Database,
    test_case_id: str,
    org_id: str,
    step_number: int,
    min_success_rate: float = 0.5,
    min_observations: int = 2,
    limit: int = 5,
) -> list[dict]:
    """Query selector memory for known-good selectors for a step.

    Returns selectors ranked by recency-weighted success rate (descending).
    Applies exponential decay with a 30-day half-life so that recently
    successful selectors rank higher than stale ones at the same raw rate.
    Selectors not used in 90+ days are auto-pruned.

    Returns list of dicts:
        [{"selector": str, "strategy": str, "params": dict,
          "success_rate": float, "weighted_score": float, "uses": int}]
    """
    doc = db.selector_memory.find_one({
        "test_case_id": test_case_id,
        "step_number": step_number,
        "org_id": org_id,
    })

    if not doc:
        return []

    now = datetime.now(timezone.utc)
    ranked = []
    for rec in doc.get("selectors", []):
        total = rec.get("success_count", 0) + rec.get("failure_count", 0)
        if total < min_observations:
            continue
        rate = rec["success_count"] / total
        if rate < min_success_rate:
            continue

        # Auto-prune stale selectors (not used in 90+ days)
        last_used = rec.get("last_used")
        if last_used:
            if isinstance(last_used, str):
                last_used = datetime.fromisoformat(last_used)
            if last_used.tzinfo is None:
                last_used = last_used.replace(tzinfo=timezone.utc)
            days_since = (now - last_used).total_seconds() / 86400
            if days_since > _STALE_THRESHOLD_DAYS:
                continue
        else:
            days_since = _STALE_THRESHOLD_DAYS  # No last_used → treat as stale

        # Recency-weighted score: exponential decay with 30-day half-life
        last_succeeded = rec.get("last_succeeded")
        if last_succeeded:
            if isinstance(last_succeeded, str):
                last_succeeded = datetime.fromisoformat(last_succeeded)
            if last_succeeded.tzinfo is None:
                last_succeeded = last_succeeded.replace(tzinfo=timezone.utc)
            days_since_success = (now - last_succeeded).total_seconds() / 86400
        else:
            days_since_success = days_since  # Fall back to last_used

        recency_weight = math.exp(-days_since_success / _DECAY_HALF_LIFE_DAYS)
        weighted_score = rate * recency_weight

        entry: dict = {
            "selector": rec["selector"],
            "success_rate": round(rate, 3),
            "weighted_score": round(weighted_score, 4),
            "uses": total,
            "success_count": rec.get("success_count", 0),
        }
        # Include structured data for resolver consumption
        if rec.get("strategy"):
            entry["strategy"] = rec["strategy"]
        if rec.get("params"):
            entry["params"] = rec["params"]
        ranked.append(entry)

    ranked.sort(key=lambda x: (-x["weighted_score"], -x["uses"]))
    return ranked[:limit]


def get_step_selector_summary(
    db: Database,
    test_case_id: str,
    org_id: str,
) -> dict[int, dict]:
    """Get selector memory summary for all steps of a test case.

    Returns dict keyed by step_number with best_selector and success_rate.
    Used for API responses and debugging.
    """
    cursor = db.selector_memory.find({
        "test_case_id": test_case_id,
        "org_id": org_id,
    })

    summary = {}
    for doc in cursor:
        sn = doc.get("step_number", 0)
        summary[sn] = {
            "best_selector": doc.get("best_selector", ""),
            "best_selector_success_rate": doc.get("best_selector_success_rate", 0.0),
            "total_selectors_tracked": len(doc.get("selectors", [])),
        }

    return summary
