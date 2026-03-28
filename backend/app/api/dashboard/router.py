from datetime import datetime, timezone, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.auth.dependencies import get_current_user
from app.dependencies import get_db
from app.models import User

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _require_org(user: User):
    from bson import ObjectId
    if not user.org_id:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No org")
    return ObjectId(str(user.org_id))


@router.get("")
async def get_dashboard(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    org_id = _require_org(current_user)

    # Run all queries concurrently
    import asyncio

    (
        recent_runs_docs,
        projects_docs,
        profiles_docs,
        stuck_runs_docs,
        latest_releases_docs,
    ) = await asyncio.gather(
        # 1. Recent 10 runs
        db.test_runs.find(
            {"org_id": org_id},
            {"_id": 1, "test_case_title": 1, "test_case_id": 1, "project_name": 1,
             "status": 1, "duration_ms": 1, "passed_steps": 1, "failed_steps": 1,
             "total_steps": 1, "created_at": 1},
        ).sort("created_at", -1).limit(5).to_list(5),

        # 2. All projects
        db.projects.find(
            {"org_id": org_id},
            {"_id": 1, "name": 1, "created_at": 1},
        ).to_list(100),

        # 3. Execution profiles (for flake + pass rate)
        db.execution_profiles.find(
            {"org_id": org_id},
            {"_id": 1, "test_case_id": 1, "pass_rate": 1, "total_runs": 1,
             "flake_report": 1, "reliability_trend": 1},
        ).to_list(500),

        # 4. Stuck runs (running > 10 min)
        db.test_runs.find(
            {
                "org_id": org_id,
                "status": "running",
                "updated_at": {"$lt": datetime.now(timezone.utc) - timedelta(minutes=10)},
            },
            {"_id": 1, "test_case_title": 1, "updated_at": 1},
        ).to_list(20),

        # 5. Latest release per project (most recent completed)
        db.release_validations.find(
            {"org_id": org_id, "status": "completed"},
            {"_id": 1, "project_name": 1, "confidence_score": 1,
             "confidence_grade": 1, "recommendation": 1, "created_at": 1},
        ).sort("created_at", -1).limit(50).to_list(50),
    )

    # --- Recent runs ---
    recent_runs = [
        {
            "id": str(r["_id"]),
            "test_case_title": r.get("test_case_title") or "Untitled",
            "test_case_id": str(r.get("test_case_id", "")),
            "project_name": r.get("project_name") or "",
            "status": r.get("status", ""),
            "duration_ms": r.get("duration_ms", 0),
            "passed_steps": r.get("passed_steps", 0),
            "failed_steps": r.get("failed_steps", 0),
            "total_steps": r.get("total_steps", 0),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        }
        for r in recent_runs_docs
    ]

    # --- Flaky test leaderboard (top 5) ---
    flaky = []
    for p in profiles_docs:
        flake_score = p.get("flake_report", {}).get("overall_flake_score", 0) or 0
        if flake_score > 0:
            flaky.append({
                "test_case_id": str(p.get("test_case_id", "")),
                "flake_score": round(flake_score, 3),
                "pass_rate": round(p.get("pass_rate", 0), 3),
                "total_runs": p.get("total_runs", 0),
                "trend": _trend_label(p.get("reliability_trend", 0)),
            })
    flaky.sort(key=lambda x: x["flake_score"], reverse=True)
    flaky = flaky[:5]

    # Enrich flaky list with test case titles
    if flaky:
        from bson import ObjectId
        tc_ids = [ObjectId(f["test_case_id"]) for f in flaky if f["test_case_id"]]
        tc_docs = await db.test_cases.find(
            {"_id": {"$in": tc_ids}}, {"_id": 1, "title": 1}
        ).to_list(10)
        tc_title_map = {str(t["_id"]): t.get("title", "Untitled") for t in tc_docs}
        for f in flaky:
            f["test_case_title"] = tc_title_map.get(f["test_case_id"], "Untitled")

    # --- Project health ---
    # Latest release per project (take first match per project name)
    latest_release_by_project: dict[str, dict] = {}
    for rv in latest_releases_docs:
        pname = rv.get("project_name", "")
        if pname and pname not in latest_release_by_project:
            latest_release_by_project[pname] = rv

    # Count test cases per project
    tc_count_pipeline = [
        {"$match": {"org_id": org_id}},
        {"$group": {"_id": "$project_id", "count": {"$sum": 1}}},
    ]
    tc_count_docs = await db.test_cases.aggregate(tc_count_pipeline).to_list(100)
    tc_count_by_project = {str(d["_id"]): d["count"] for d in tc_count_docs}

    # Last run per project
    last_run_pipeline = [
        {"$match": {"org_id": org_id}},
        {"$sort": {"created_at": -1}},
        {"$group": {"_id": "$project_name", "last_status": {"$first": "$status"},
                    "last_run_at": {"$first": "$created_at"}}},
    ]
    last_run_docs = await db.test_runs.aggregate(last_run_pipeline).to_list(100)
    last_run_by_project = {d["_id"]: d for d in last_run_docs}

    project_health = []
    for p in projects_docs:
        pid = str(p["_id"])
        pname = p.get("name", "")
        release = latest_release_by_project.get(pname)
        last_run = last_run_by_project.get(pname)
        project_health.append({
            "project_id": pid,
            "project_name": pname,
            "test_case_count": tc_count_by_project.get(pid, 0),
            "last_run_status": last_run["last_status"] if last_run else None,
            "last_run_at": last_run["last_run_at"].isoformat() if last_run and last_run.get("last_run_at") else None,
            "latest_release_score": release.get("confidence_score") if release else None,
            "latest_release_grade": release.get("confidence_grade") if release else None,
            "latest_release_recommendation": release.get("recommendation") if release else None,
        })

    # --- Stuck runs ---
    now = datetime.now(timezone.utc)
    stuck_runs = [
        {
            "id": str(r["_id"]),
            "test_case_title": r.get("test_case_title") or "Untitled",
            "minutes_running": int(
                (now - r["updated_at"].replace(tzinfo=timezone.utc)).total_seconds() / 60
            ) if r.get("updated_at") else 0,
        }
        for r in stuck_runs_docs
    ]

    return {
        "recent_runs": recent_runs,
        "flaky_tests": flaky,
        "project_health": project_health,
        "stuck_runs": stuck_runs,
    }


def _trend_label(trend: float) -> str:
    if trend > 0.1:
        return "up"
    if trend < -0.1:
        return "down"
    return "stable"
