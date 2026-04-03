"""3.4 Cross-Org Benchmarking (anonymised, opt-in).

Aggregates per-test flake rates and pass rates by pattern category.
Nightly Beat job writes results into `benchmark_stats`.

Within an org:  compare each test case against the org's pattern average.
Cross-org:      compare org pattern averages against the global distribution.
                Cross-org data is never attributed — only aggregated buckets.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)

_PATTERN_KEYWORDS: dict[str, list[str]] = {
    "login / auth": ["login", "auth", "signin", "sign in", "password", "oauth", "sso"],
    "checkout / payment": ["checkout", "payment", "cart", "order", "purchase", "billing"],
    "form submission": ["form", "submit", "register", "signup", "sign up", "create account"],
    "navigation": ["nav", "menu", "link", "route", "page", "redirect"],
    "search": ["search", "filter", "query", "find", "results"],
    "profile / settings": ["profile", "setting", "account", "preference"],
    "data / CRUD": ["create", "update", "delete", "edit", "save", "list"],
}


def _detect_pattern(title: str, tags: list[str]) -> str:
    combined = (title + " " + " ".join(tags)).lower()
    for pattern, keywords in _PATTERN_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return pattern
    return "other"


def compute_org_benchmark(db: Database, org_id: str) -> dict:
    """Compute within-org benchmarks for all test cases."""
    from bson import ObjectId as _ObjId

    # Load profiles
    profiles = {
        p["test_case_id"]: p
        for p in db.execution_profiles.find(
            {"org_id": org_id},
            {"test_case_id": 1, "pass_rate": 1, "flake_rate": 1, "total_runs": 1},
        )
    }

    if not profiles:
        return {"test_cases": [], "pattern_averages": {}, "org_id": org_id}

    # Load test cases for metadata
    tc_ids = list(profiles.keys())
    tc_map = {}
    try:
        tc_docs = db.test_cases.find(
            {"org_id": _ObjId(org_id)},
            {"_id": 1, "title": 1, "tags": 1, "project_id": 1},
        )
        for tc in tc_docs:
            tc_map[str(tc["_id"])] = tc
    except Exception:
        pass

    # Group by pattern
    pattern_groups: dict[str, list[dict]] = {}
    test_cases: list[dict] = []

    for tc_id, profile in profiles.items():
        tc = tc_map.get(tc_id, {})
        title = tc.get("title", tc_id)
        tags = tc.get("tags", [])
        pattern = _detect_pattern(title, tags)
        pr = float(profile.get("pass_rate", 0.5))
        fr = float(profile.get("flake_rate", 0.1))
        runs = int(profile.get("total_runs", 0))

        entry = {
            "test_case_id": tc_id,
            "title": title,
            "pattern": pattern,
            "pass_rate": round(pr, 3),
            "flake_rate": round(fr, 3),
            "total_runs": runs,
        }
        test_cases.append(entry)
        pattern_groups.setdefault(pattern, []).append(entry)

    # Compute pattern averages (within this org)
    pattern_averages: dict[str, dict] = {}
    for pattern, entries in pattern_groups.items():
        avg_pr = sum(e["pass_rate"] for e in entries) / len(entries)
        avg_fr = sum(e["flake_rate"] for e in entries) / len(entries)
        pattern_averages[pattern] = {
            "avg_pass_rate": round(avg_pr, 3),
            "avg_flake_rate": round(avg_fr, 3),
            "test_count": len(entries),
        }

    # Annotate each test case with comparison to its pattern average
    for tc_entry in test_cases:
        avg = pattern_averages.get(tc_entry["pattern"], {})
        avg_fr = avg.get("avg_flake_rate", 0.1)
        fr = tc_entry["flake_rate"]
        if avg_fr > 0:
            tc_entry["flake_vs_avg"] = round((fr - avg_fr) / avg_fr, 2)
        else:
            tc_entry["flake_vs_avg"] = 0.0
        tc_entry["flake_percentile"] = _percentile_rank(
            fr, [e["flake_rate"] for e in pattern_groups.get(tc_entry["pattern"], [])]
        )

    return {
        "org_id": org_id,
        "test_cases": test_cases,
        "pattern_averages": pattern_averages,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _percentile_rank(value: float, values: list[float]) -> int:
    """Return 0-100 percentile rank of value in values (higher = worse for flake)."""
    if not values:
        return 50
    below = sum(1 for v in values if v < value)
    return int((below / len(values)) * 100)


def compute_confidence_percentiles(db: Database) -> dict[str, int]:
    """7C.3 Compute cross-org confidence score percentiles.

    For each org: compute average confidence_score over recent completed validations,
    then rank each org against all others.  Returns {org_id: percentile (0-100)}.
    """
    # Aggregate avg confidence_score per org from last 90 days
    from datetime import timedelta, timezone
    from datetime import datetime

    cutoff = datetime.now(timezone.utc) - timedelta(days=90)

    # Using pymongo aggregation
    pipeline = [
        {
            "$match": {
                "status": "completed",
                "confidence_score": {"$ne": None},
                "created_at": {"$gte": cutoff},
            }
        },
        {
            "$group": {
                "_id": {"$toString": "$org_id"},
                "avg_score": {"$avg": "$confidence_score"},
                "validation_count": {"$sum": 1},
            }
        },
        {"$match": {"validation_count": {"$gte": 3}}},  # min 3 validations
    ]

    rows = list(db.release_validations.aggregate(pipeline))
    if not rows:
        return {}

    # Sort by avg_score to compute percentile ranks
    rows.sort(key=lambda r: r["avg_score"])
    n = len(rows)
    percentiles: dict[str, int] = {}
    for i, row in enumerate(rows):
        pct = int((i / n) * 100) if n > 1 else 50
        percentiles[row["_id"]] = pct

    return percentiles


def get_benchmark_for_project(db: Database, org_id: str, project_id: str) -> dict:
    """Return benchmark data filtered to test cases belonging to project_id."""
    from bson import ObjectId as _ObjId

    cached = db.benchmark_stats.find_one(
        {"org_id": org_id},
        sort=[("computed_at", -1)],
    )
    if not cached:
        # Compute on-demand if no cache yet
        result = compute_org_benchmark(db, org_id)
    else:
        result = cached

    # Filter to project
    try:
        project_tc_ids = {
            str(tc["_id"])
            for tc in db.test_cases.find(
                {"org_id": _ObjId(org_id), "project_id": _ObjId(project_id)},
                {"_id": 1},
            )
        }
        result = dict(result)
        result["test_cases"] = [
            tc for tc in result.get("test_cases", [])
            if tc["test_case_id"] in project_tc_ids
        ]
    except Exception:
        pass

    return result
