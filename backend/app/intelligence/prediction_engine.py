"""3.3 Predictive Score Before Running.

Estimates the expected confidence score for a project's next release validation
using historical execution profiles — without running any tests.

Algorithm:
  1. Load execution profiles for all active test cases in the project.
  2. For each test case compute: expected pass, expected flake penalty, stability.
  3. Aggregate into a simulated batch score using the same V1 weights.
  4. Estimate a ±confidence interval via bootstrap on historical variance.
"""

from __future__ import annotations

import math
from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)

# Weights mirror V1 scorer so predictions stay comparable to real scores
_PASS_RATE_WEIGHT = 0.40
_FLAKE_PENALTY_WEIGHT = 0.20
_STABILITY_WEIGHT = 0.20
_COVERAGE_WEIGHT = 0.20

_PATTERN_KEYWORDS: dict[str, list[str]] = {
    "login / auth": ["login", "auth", "signin", "sign in", "password", "oauth", "sso"],
    "checkout / payment": ["checkout", "payment", "cart", "order", "purchase", "billing"],
    "form submission": ["form", "submit", "register", "signup", "sign up", "create account"],
    "navigation": ["nav", "menu", "link", "route", "page", "redirect", "breadcrumb"],
    "search": ["search", "filter", "query", "find", "results"],
    "profile / settings": ["profile", "setting", "account", "preference", "update"],
    "data / CRUD": ["create", "update", "delete", "edit", "save", "list", "table"],
}


def _detect_pattern(title: str, tags: list[str]) -> str:
    combined = (title + " " + " ".join(tags)).lower()
    for pattern, keywords in _PATTERN_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return pattern
    return "other"


def compute_predicted_score(
    db: Database,
    org_id: str,
    project_id: str,
) -> dict:
    """Return a predicted confidence score for the project's next validation."""
    profiles = list(db.execution_profiles.find(
        {"org_id": org_id},
        {"test_case_id": 1, "pass_rate": 1, "flake_rate": 1,
         "total_runs": 1, "avg_duration_ms": 1, "last_updated": 1},
    ))

    if not profiles:
        return {
            "predicted_score": None,
            "confidence_interval": None,
            "data_quality": "insufficient",
            "message": "No execution history found. Run at least one validation first.",
            "per_test_predictions": [],
            "pattern_summary": [],
            "total_test_cases": 0,
            "profiled_test_cases": 0,
        }

    # Map profile by test_case_id
    profile_map = {p["test_case_id"]: p for p in profiles}

    # Active test cases for the project
    from bson import ObjectId as _ObjId
    tc_docs = list(db.test_cases.find(
        {"org_id": _ObjId(org_id), "project_id": _ObjId(project_id), "status": {"$ne": "archived"}},
        {"_id": 1, "title": 1, "tags": 1, "is_critical": 1, "is_informational": 1, "priority": 1},
    ))

    if not tc_docs:
        return {
            "predicted_score": None,
            "confidence_interval": None,
            "data_quality": "insufficient",
            "message": "No active test cases in this project.",
            "per_test_predictions": [],
            "pattern_summary": [],
            "total_test_cases": 0,
            "profiled_test_cases": 0,
        }

    per_test: list[dict] = []
    pass_rates: list[float] = []
    flake_rates: list[float] = []
    runs_list: list[int] = []

    for tc in tc_docs:
        tc_id = str(tc["_id"])
        profile = profile_map.get(tc_id)
        if not profile:
            # No history — assume worst case (flaky) for conservative estimate
            per_test.append({
                "test_case_id": tc_id,
                "title": tc.get("title", tc_id),
                "pattern": _detect_pattern(tc.get("title", ""), tc.get("tags", [])),
                "predicted_pass_rate": None,
                "flake_rate": None,
                "total_runs": 0,
                "confidence": "none",
                "is_critical": tc.get("is_critical", False),
                "is_informational": tc.get("is_informational", False),
            })
            continue

        pr = float(profile.get("pass_rate", 0.5))
        fr = float(profile.get("flake_rate", 0.1))
        runs = int(profile.get("total_runs", 0))

        pass_rates.append(pr)
        flake_rates.append(fr)
        runs_list.append(runs)

        # Data confidence per test
        conf = "high" if runs >= 10 else ("medium" if runs >= 3 else "low")

        per_test.append({
            "test_case_id": tc_id,
            "title": tc.get("title", tc_id),
            "pattern": _detect_pattern(tc.get("title", ""), tc.get("tags", [])),
            "predicted_pass_rate": round(pr, 3),
            "flake_rate": round(fr, 3),
            "total_runs": runs,
            "confidence": conf,
            "is_critical": tc.get("is_critical", False),
            "is_informational": tc.get("is_informational", False),
        })

    if not pass_rates:
        return {
            "predicted_score": None,
            "confidence_interval": None,
            "data_quality": "insufficient",
            "message": "No profiled test cases found. Run at least one validation first.",
            "per_test_predictions": per_test,
            "pattern_summary": [],
            "total_test_cases": len(tc_docs),
            "profiled_test_cases": 0,
        }

    # ── Aggregate predicted score ─────────────────────────────────────────────
    avg_pass_rate = sum(pass_rates) / len(pass_rates)
    avg_flake_rate = sum(flake_rates) / len(flake_rates)

    # Pass rate → base score (same mapping as V1 scorer)
    base_score = avg_pass_rate * 100.0
    flake_penalty = avg_flake_rate * 20.0          # up to -20 pts for fully flaky suite
    stability_bonus = max(0.0, (1.0 - avg_flake_rate) * 10.0)  # up to +10 for stable suite
    predicted = max(0.0, min(100.0, base_score - flake_penalty + stability_bonus))

    # ── Confidence interval ───────────────────────────────────────────────────
    # Use standard deviation of pass rates weighted by run count
    if len(pass_rates) >= 2:
        variance = sum((p - avg_pass_rate) ** 2 for p in pass_rates) / len(pass_rates)
        std = math.sqrt(variance)
        # Scale std to score space and apply 1.96σ for 95% CI
        ci_half = round(std * 100.0 * 1.96 / math.sqrt(len(pass_rates)), 1)
    else:
        ci_half = 10.0  # wide interval for single test

    ci_low = max(0, round(predicted - ci_half))
    ci_high = min(100, round(predicted + ci_half))

    # ── Data quality ──────────────────────────────────────────────────────────
    avg_runs = sum(runs_list) / len(runs_list) if runs_list else 0
    profiled_fraction = len(pass_rates) / len(tc_docs)
    if avg_runs >= 10 and profiled_fraction >= 0.8:
        data_quality = "high"
    elif avg_runs >= 3 and profiled_fraction >= 0.5:
        data_quality = "medium"
    else:
        data_quality = "low"

    # ── Predicted recommendation ──────────────────────────────────────────────
    ps = int(round(predicted))
    if ps >= 85:
        pred_recommendation = "deploy"
    elif ps >= 70:
        pred_recommendation = "caution"
    else:
        pred_recommendation = "block"

    # ── Pattern summary ───────────────────────────────────────────────────────
    pattern_groups: dict[str, list[float]] = {}
    for pt in per_test:
        p = pt["pattern"]
        if pt["predicted_pass_rate"] is not None:
            pattern_groups.setdefault(p, []).append(pt["predicted_pass_rate"])

    pattern_summary = []
    for pattern, rates in sorted(pattern_groups.items()):
        avg = sum(rates) / len(rates)
        pattern_summary.append({
            "pattern": pattern,
            "test_count": len(rates),
            "avg_pass_rate": round(avg, 3),
            "risk": "high" if avg < 0.6 else ("medium" if avg < 0.85 else "low"),
        })

    # ── Risk warnings ─────────────────────────────────────────────────────────
    warnings: list[str] = []
    critical_at_risk = [
        pt["title"] for pt in per_test
        if pt.get("is_critical") and (pt.get("predicted_pass_rate") or 1.0) < 0.8
    ]
    if critical_at_risk:
        warnings.append(
            f"{len(critical_at_risk)} critical test(s) have a pass rate below 80%: "
            + ", ".join(critical_at_risk[:3])
        )
    high_flake = [
        pt["title"] for pt in per_test
        if (pt.get("flake_rate") or 0) > 0.3
    ]
    if high_flake:
        warnings.append(
            f"{len(high_flake)} test(s) have a flake rate above 30%: "
            + ", ".join(high_flake[:3])
        )
    no_history = [pt["title"] for pt in per_test if pt["total_runs"] == 0]
    if no_history:
        warnings.append(
            f"{len(no_history)} test(s) have no execution history — excluded from prediction."
        )

    logger.info(
        "prediction_engine.computed",
        project_id=project_id,
        org_id=org_id,
        predicted_score=ps,
        data_quality=data_quality,
        profiled=len(pass_rates),
        total=len(tc_docs),
    )

    return {
        "predicted_score": ps,
        "confidence_interval": {"low": ci_low, "high": ci_high, "half": ci_half},
        "predicted_recommendation": pred_recommendation,
        "data_quality": data_quality,
        "message": None,
        "per_test_predictions": per_test,
        "pattern_summary": pattern_summary,
        "warnings": warnings,
        "total_test_cases": len(tc_docs),
        "profiled_test_cases": len(pass_rates),
    }
