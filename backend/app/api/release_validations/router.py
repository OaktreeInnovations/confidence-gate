from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
import structlog

from app.api.auth.dependencies import get_current_user
from app.dependencies import get_db
from app.models import TestCase, TestRun, TestRunStatus, User
from app.models.release_validation import (
    ReleaseValidation,
    ReleaseValidationContext,
    ReleaseValidationStatus,
)
from app.worker.tasks.execute_test_run import execute_test_run

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/release-validations", tags=["release-validations"])


# --- Request / Response schemas ---


class CreateReleaseValidationRequest(BaseModel):
    project_id: str
    title: str = ""
    prd_text: str = ""
    notes: str = ""
    test_case_ids: list[str] | None = None


class ReleaseValidationResponse(BaseModel):
    id: str
    org_id: str
    project_id: str
    project_name: str
    title: str
    status: str
    batch_id: str
    total_runs: int
    completed_runs: int
    passed_runs: int
    failed_runs: int
    error_runs: int
    context: dict
    confidence_score: int | None
    confidence_grade: str | None
    recommendation: str | None
    report: dict
    created_at: str
    updated_at: str
    completed_at: str | None

    @classmethod
    def from_doc(cls, doc: dict) -> "ReleaseValidationResponse":
        return cls(
            id=str(doc["_id"]),
            org_id=str(doc["org_id"]),
            project_id=str(doc["project_id"]),
            project_name=doc.get("project_name", ""),
            title=doc.get("title", ""),
            status=doc.get("status", "pending"),
            batch_id=doc.get("batch_id", ""),
            total_runs=doc.get("total_runs", 0),
            completed_runs=doc.get("completed_runs", 0),
            passed_runs=doc.get("passed_runs", 0),
            failed_runs=doc.get("failed_runs", 0),
            error_runs=doc.get("error_runs", 0),
            context=doc.get("context", {}),
            confidence_score=doc.get("confidence_score"),
            confidence_grade=doc.get("confidence_grade"),
            recommendation=doc.get("recommendation"),
            report=doc.get("report", {}),
            created_at=doc["created_at"].isoformat(),
            updated_at=doc["updated_at"].isoformat(),
            completed_at=doc["completed_at"].isoformat() if doc.get("completed_at") else None,
        )


class ReleaseValidationListResponse(BaseModel):
    items: list[ReleaseValidationResponse]
    total: int
    page: int
    page_size: int


# --- Helpers ---


def _parse_oid(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except (InvalidId, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid ID format: {id_str}",
        )


def _require_org(user: User) -> ObjectId:
    if not user.org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User must belong to an organization",
        )
    return user.org_id


async def _compute_batch_stats(db: AsyncIOMotorDatabase, batch_id: str, org_id: ObjectId) -> dict:
    """Compute run status counts for a batch by querying test_runs."""
    pipeline = [
        {"$match": {"batch_id": batch_id, "org_id": org_id}},
        {"$group": {
            "_id": None,
            "total": {"$sum": 1},
            "completed": {"$sum": {"$cond": [
                {"$in": ["$status", ["passed", "failed", "error"]]}, 1, 0,
            ]}},
            "passed": {"$sum": {"$cond": [{"$eq": ["$status", "passed"]}, 1, 0]}},
            "failed": {"$sum": {"$cond": [{"$eq": ["$status", "failed"]}, 1, 0]}},
            "error": {"$sum": {"$cond": [{"$eq": ["$status", "error"]}, 1, 0]}},
        }},
    ]
    result = await db.test_runs.aggregate(pipeline).to_list(length=1)
    if not result:
        return {"total": 0, "completed": 0, "passed": 0, "failed": 0, "error": 0}
    return result[0]


# --- Endpoints ---


@router.post("", response_model=ReleaseValidationResponse, status_code=status.HTTP_201_CREATED)
async def create_release_validation(
    body: CreateReleaseValidationRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ReleaseValidationResponse:
    """Create a release validation and trigger batch test execution."""
    org_id = _require_org(current_user)
    project_oid = _parse_oid(body.project_id)

    # Validate project
    project_doc = await db.projects.find_one({"_id": project_oid, "org_id": org_id})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project_name = project_doc.get("name", "")

    # Fetch test cases
    tc_query: dict = {
        "org_id": org_id,
        "project_id": project_oid,
        "status": {"$ne": "archived"},
    }
    if body.test_case_ids:
        tc_oids = [_parse_oid(tid) for tid in body.test_case_ids]
        tc_query["_id"] = {"$in": tc_oids}

    tc_docs = await db.test_cases.find(tc_query).to_list(length=500)
    if not tc_docs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active test cases found")

    now = datetime.now(timezone.utc)
    batch_id = str(uuid4())
    batch_name = f"Release \u00b7 {project_name} \u00b7 {now.strftime('%b %d, %H:%M')}"

    # Create the release validation document
    val = ReleaseValidation(
        org_id=org_id,
        project_id=project_oid,
        project_name=project_name,
        title=body.title,
        triggered_by=current_user.id,
        status=ReleaseValidationStatus.RUNNING,
        batch_id=batch_id,
        total_runs=len(tc_docs),
        context=ReleaseValidationContext(prd_text=body.prd_text, notes=body.notes),
        created_at=now,
        updated_at=now,
    )
    result = await db.release_validations.insert_one(
        val.model_dump(by_alias=True, exclude={"id"})
    )
    val_id = result.inserted_id

    # Create test runs (same pattern as batch_create_test_runs)
    for tc_doc in tc_docs:
        tc = TestCase(**tc_doc)
        tr = TestRun(
            org_id=org_id,
            test_case_id=tc.id,
            test_case_title=tc.title,
            test_type=tc.test_type,
            project_name=project_name,
            batch_name=batch_name,
            batch_id=batch_id,
            status=TestRunStatus.QUEUED,
            triggered_by=current_user.id,
            total_steps=len(tc.steps),
            created_at=now,
            updated_at=now,
        )
        run_result = await db.test_runs.insert_one(
            tr.model_dump(by_alias=True, exclude={"id"})
        )
        tr.id = run_result.inserted_id
        task = execute_test_run.delay(str(tr.id))
        await db.test_runs.update_one(
            {"_id": tr.id},
            {"$set": {"celery_task_id": task.id}},
        )

    logger.info(
        "release_validation.created",
        validation_id=str(val_id),
        project_id=body.project_id,
        batch_id=batch_id,
        total_runs=len(tc_docs),
        org_id=str(org_id),
    )

    doc = await db.release_validations.find_one({"_id": val_id})
    return ReleaseValidationResponse.from_doc(doc)


@router.get("", response_model=ReleaseValidationListResponse)
async def list_release_validations(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    project_id: str | None = Query(default=None),
) -> ReleaseValidationListResponse:
    """List release validations for the org."""
    org_id = _require_org(current_user)
    query: dict = {"org_id": org_id}
    if project_id:
        query["project_id"] = _parse_oid(project_id)

    total = await db.release_validations.count_documents(query)
    skip = (page - 1) * page_size
    docs = await db.release_validations.find(query).sort("created_at", -1).skip(skip).limit(page_size).to_list(length=page_size)

    return ReleaseValidationListResponse(
        items=[ReleaseValidationResponse.from_doc(d) for d in docs],
        total=total,
        page=page,
        page_size=page_size,
    )


class TrendPoint(BaseModel):
    id: str
    score: int | None
    grade: str | None
    recommendation: str | None
    created_at: str


class TrendsResponse(BaseModel):
    project_id: str
    points: list[TrendPoint]


@router.get("/trends", response_model=TrendsResponse)
async def get_validation_trends(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    project_id: str = Query(...),
    limit: int = Query(default=10, ge=1, le=50),
) -> TrendsResponse:
    """Get recent validation scores for a project (for sparkline/chart)."""
    org_id = _require_org(current_user)
    project_oid = _parse_oid(project_id)

    docs = await db.release_validations.find(
        {
            "org_id": org_id,
            "project_id": project_oid,
            "status": "completed",
        },
        {"_id": 1, "confidence_score": 1, "confidence_grade": 1, "recommendation": 1, "created_at": 1},
    ).sort("created_at", -1).limit(limit).to_list(length=limit)

    points = [
        TrendPoint(
            id=str(d["_id"]),
            score=d.get("confidence_score"),
            grade=d.get("confidence_grade"),
            recommendation=d.get("recommendation"),
            created_at=d["created_at"].isoformat(),
        )
        for d in reversed(docs)  # chronological order for chart
    ]

    return TrendsResponse(project_id=project_id, points=points)


class CancelAllResponse(BaseModel):
    cancelled: int


@router.post("/cancel-all", response_model=CancelAllResponse)
async def cancel_all_release_validations(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> CancelAllResponse:
    """Cancel all running or computing release validations for the org."""
    org_id = _require_org(current_user)
    now = datetime.now(timezone.utc)

    active_docs = await db.release_validations.find(
        {
            "org_id": org_id,
            "status": {"$in": [ReleaseValidationStatus.RUNNING, ReleaseValidationStatus.COMPUTING]},
        },
        {"_id": 1, "batch_id": 1},
    ).to_list(length=100)

    if not active_docs:
        return CancelAllResponse(cancelled=0)

    from app.worker.celery_app import celery_app as _celery

    for doc in active_docs:
        active_runs = await db.test_runs.find(
            {"batch_id": doc["batch_id"], "org_id": org_id, "status": {"$in": ["queued", "running"]}},
            {"_id": 1, "celery_task_id": 1},
        ).to_list(length=500)

        if active_runs:
            for run in active_runs:
                if run.get("celery_task_id"):
                    _celery.control.revoke(run["celery_task_id"], terminate=True, signal="SIGTERM")
            await db.test_runs.update_many(
                {"_id": {"$in": [r["_id"] for r in active_runs]}},
                {"$set": {"status": "error", "updated_at": now}},
            )

    val_ids = [d["_id"] for d in active_docs]
    await db.release_validations.update_many(
        {"_id": {"$in": val_ids}},
        {"$set": {"status": ReleaseValidationStatus.CANCELLED, "updated_at": now}},
    )

    logger.info("release_validation.cancel_all", org_id=str(org_id), count=len(active_docs))
    return CancelAllResponse(cancelled=len(active_docs))


@router.post("/{validation_id}/cancel", response_model=ReleaseValidationResponse)
async def cancel_release_validation(
    validation_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ReleaseValidationResponse:
    """Cancel a single running or computing release validation."""
    org_id = _require_org(current_user)
    val_oid = _parse_oid(validation_id)
    now = datetime.now(timezone.utc)

    doc = await db.release_validations.find_one({"_id": val_oid, "org_id": org_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release validation not found")

    if doc.get("status") not in (ReleaseValidationStatus.RUNNING, ReleaseValidationStatus.COMPUTING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot cancel a validation with status '{doc.get('status')}'",
        )

    from app.worker.celery_app import celery_app as _celery

    active_runs = await db.test_runs.find(
        {"batch_id": doc["batch_id"], "org_id": org_id, "status": {"$in": ["queued", "running"]}},
        {"_id": 1, "celery_task_id": 1},
    ).to_list(length=500)

    if active_runs:
        for run in active_runs:
            if run.get("celery_task_id"):
                _celery.control.revoke(run["celery_task_id"], terminate=True, signal="SIGTERM")
        await db.test_runs.update_many(
            {"_id": {"$in": [r["_id"] for r in active_runs]}},
            {"$set": {"status": "error", "updated_at": now}},
        )

    await db.release_validations.update_one(
        {"_id": val_oid},
        {"$set": {"status": ReleaseValidationStatus.CANCELLED, "updated_at": now}},
    )

    logger.info("release_validation.cancelled", validation_id=validation_id, org_id=str(org_id))
    doc = await db.release_validations.find_one({"_id": val_oid})
    return ReleaseValidationResponse.from_doc(doc)


@router.get("/{validation_id}", response_model=ReleaseValidationResponse)
async def get_release_validation(
    validation_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ReleaseValidationResponse:
    """Get a release validation. Dynamically updates progress and triggers computation when all runs complete."""
    org_id = _require_org(current_user)
    val_oid = _parse_oid(validation_id)

    doc = await db.release_validations.find_one({"_id": val_oid, "org_id": org_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release validation not found")

    # If still running, compute fresh stats from test_runs
    if doc.get("status") == ReleaseValidationStatus.RUNNING:
        stats = await _compute_batch_stats(db, doc["batch_id"], org_id)
        now = datetime.now(timezone.utc)

        update: dict = {
            "completed_runs": stats["completed"],
            "passed_runs": stats["passed"],
            "failed_runs": stats["failed"],
            "error_runs": stats["error"],
            "updated_at": now,
        }

        # Check if all runs are done
        if stats["completed"] >= doc.get("total_runs", 0) and doc.get("total_runs", 0) > 0:
            # Transition to computing and trigger intelligence task
            update["status"] = ReleaseValidationStatus.COMPUTING
            try:
                from app.worker.tasks.compute_release_report import compute_release_report_task
                compute_release_report_task.delay(str(val_oid))
                logger.info(
                    "release_validation.compute_triggered",
                    validation_id=validation_id,
                    batch_id=doc["batch_id"],
                )
            except Exception as e:
                logger.warning("release_validation.compute_dispatch_failed", error=str(e)[:200])
                update["status"] = ReleaseValidationStatus.FAILED

        await db.release_validations.update_one({"_id": val_oid}, {"$set": update})
        doc.update(update)

    return ReleaseValidationResponse.from_doc(doc)
