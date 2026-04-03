import asyncio
import json
from datetime import datetime, timezone
from typing import Annotated, AsyncGenerator
from uuid import uuid4

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
import structlog

from app.api.auth.dependencies import get_current_user
from app.dependencies import get_db
from app.intelligence.rate_limiter import check_validation_rate_limit
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
    # 3.1 Change-aware test selection
    changed_areas: list[str] = Field(default_factory=list)


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
    score_version: str | None = None
    trend: str | None = None
    risk_delta: int | None = None
    historical_confidence: int | None = None
    created_at: str
    updated_at: str
    completed_at: str | None
    # 1.4 Score freeze
    final_score_at_decision: int | None = None
    decision_at: str | None = None
    score_frozen: bool = False
    # 1.2 Override audit trail
    override_by: str | None = None
    override_reason: str | None = None
    override_at: str | None = None
    override_type: str | None = None
    original_decision: str | None = None
    # 2.4 Production outcome
    outcome: str | None = None
    outcome_recorded_at: str | None = None
    outcome_notes: str | None = None
    # 2.5 Validation SLA
    sla_minutes: int = 0
    sla_breached: bool = False
    sla_breached_at: str | None = None
    # 3.1 Change-aware selection
    changed_areas: list[str] = Field(default_factory=list)
    targeted_selection: bool = False
    # 3.5 Approval workflow
    approval_required: bool = False
    approved_by: str | None = None
    approved_at: str | None = None
    rejected_by: str | None = None
    rejected_at: str | None = None
    rejection_reason: str | None = None
    # Task error (populated when status = "failed")
    error_message: str | None = None
    # 2.3 Prediction accuracy
    predicted_score_pre_run: int | None = None
    prediction_accuracy: int | None = None
    # 3.3 Webhook delivery
    webhook_delivery_status: str | None = None
    webhook_last_attempt_at: str | None = None
    # 5.1 AI adjustment audit
    ai_adjustment_detail: dict | None = None
    # 7C.2 Incident signal attribution
    incident_signal_attribution: list[dict] | None = None
    # 8.1 Score degradation
    score_degraded: bool = False
    degraded_engines: list[str] = Field(default_factory=list)

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
            score_version=doc.get("score_version"),
            trend=doc.get("trend"),
            risk_delta=doc.get("risk_delta"),
            historical_confidence=doc.get("historical_confidence"),
            created_at=doc["created_at"].isoformat(),
            updated_at=doc["updated_at"].isoformat(),
            completed_at=doc["completed_at"].isoformat() if doc.get("completed_at") else None,
            final_score_at_decision=doc.get("final_score_at_decision"),
            decision_at=doc["decision_at"].isoformat() if doc.get("decision_at") else None,
            score_frozen=doc.get("score_frozen", False),
            override_by=str(doc["override_by"]) if doc.get("override_by") else None,
            override_reason=doc.get("override_reason"),
            override_at=doc["override_at"].isoformat() if doc.get("override_at") else None,
            override_type=doc.get("override_type"),
            original_decision=doc.get("original_decision"),
            outcome=doc.get("outcome"),
            outcome_recorded_at=doc["outcome_recorded_at"].isoformat() if doc.get("outcome_recorded_at") else None,
            outcome_notes=doc.get("outcome_notes"),
            sla_minutes=doc.get("sla_minutes", 0),
            sla_breached=doc.get("sla_breached", False),
            sla_breached_at=doc["sla_breached_at"].isoformat() if doc.get("sla_breached_at") else None,
            changed_areas=doc.get("changed_areas", []),
            targeted_selection=doc.get("targeted_selection", False),
            approval_required=doc.get("approval_required", False),
            approved_by=str(doc["approved_by"]) if doc.get("approved_by") else None,
            approved_at=doc["approved_at"].isoformat() if doc.get("approved_at") else None,
            rejected_by=str(doc["rejected_by"]) if doc.get("rejected_by") else None,
            rejected_at=doc["rejected_at"].isoformat() if doc.get("rejected_at") else None,
            rejection_reason=doc.get("rejection_reason"),
            error_message=doc.get("error_message"),
            predicted_score_pre_run=doc.get("predicted_score_pre_run"),
            prediction_accuracy=doc.get("prediction_accuracy"),
            webhook_delivery_status=doc.get("webhook_delivery_status"),
            webhook_last_attempt_at=doc["webhook_last_attempt_at"].isoformat() if doc.get("webhook_last_attempt_at") else None,
            ai_adjustment_detail=doc.get("ai_adjustment_detail"),
            incident_signal_attribution=doc.get("incident_signal_attribution"),
            score_degraded=doc.get("score_degraded", False),
            degraded_engines=doc.get("degraded_engines", []),
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
    _rate_limit: Annotated[None, Depends(check_validation_rate_limit)] = None,
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

    # 3.1 Change-aware test selection — filter by changed_areas tags
    targeted_selection = False
    if body.changed_areas:
        changed_lower = [a.lower() for a in body.changed_areas]
        filtered = [
            tc for tc in tc_docs
            if any(
                tag.lower() in changed_lower or
                any(area in tag.lower() for area in changed_lower)
                for tag in tc.get("tags", [])
            )
        ]
        # Always include critical tests regardless of filter
        critical_ids = {str(tc["_id"]) for tc in tc_docs if tc.get("is_critical")}
        critical_docs = [tc for tc in tc_docs if str(tc["_id"]) in critical_ids]
        filtered_ids = {str(tc["_id"]) for tc in filtered}
        for ct in critical_docs:
            if str(ct["_id"]) not in filtered_ids:
                filtered.append(ct)
        if filtered:
            tc_docs = filtered
            targeted_selection = True
        # If no matches, fall back to running all (log a warning)
        else:
            logger.warning(
                "release_validation.change_filter_no_match",
                changed_areas=body.changed_areas,
                project_id=body.project_id,
            )

    now = datetime.now(timezone.utc)
    batch_id = str(uuid4())
    batch_name = f"Release \u00b7 {project_name} \u00b7 {now.strftime('%b %d, %H:%M')}"

    from app.config import Settings as _Settings
    _settings = _Settings()

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
        sla_minutes=_settings.validation_sla_minutes,
        changed_areas=body.changed_areas,
        targeted_selection=targeted_selection,
    )
    result = await db.release_validations.insert_one(
        val.model_dump(by_alias=True, exclude={"id"})
    )
    val_id = result.inserted_id

    # 2.3 Capture pre-run predicted score for later accuracy comparison (non-fatal)
    try:
        import asyncio
        from app.intelligence.prediction_engine import compute_predicted_score
        from app.clients import sync_mongo as _sync_mongo
        loop = asyncio.get_event_loop()
        pred = await loop.run_in_executor(
            None,
            compute_predicted_score,
            _sync_mongo.db,
            str(org_id),
            body.project_id,
        )
        predicted_score = pred.get("predicted_score")
        if predicted_score is not None:
            await db.release_validations.update_one(
                {"_id": val_id},
                {"$set": {"predicted_score_pre_run": int(predicted_score)}},
            )
    except Exception:
        pass  # non-fatal — prediction may not exist for new projects

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

    doc = await db.release_validations.find_one({"_id": val_id, "org_id": org_id})
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


class OverrideRequest(BaseModel):
    reason: str = Field(..., min_length=10)
    override_type: str = Field(..., pattern="^(ship_anyway|acknowledge_risk)$")


@router.post("/{validation_id}/override", response_model=ReleaseValidationResponse)
async def override_release_validation(
    validation_id: str,
    body: OverrideRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ReleaseValidationResponse:
    """Override a blocked or insufficient-data release validation with an audit trail."""
    org_id = _require_org(current_user)
    val_oid = _parse_oid(validation_id)
    now = datetime.now(timezone.utc)

    doc = await db.release_validations.find_one({"_id": val_oid, "org_id": org_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release validation not found")

    if doc.get("status") != ReleaseValidationStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Can only override completed validations",
        )

    current_rec = doc.get("recommendation")
    if current_rec not in ("block", "insufficient_data"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Override is only allowed for 'block' or 'insufficient_data' recommendations, not '{current_rec}'",
        )

    if doc.get("override_at"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This validation has already been overridden",
        )

    await db.release_validations.update_one(
        {"_id": val_oid},
        {"$set": {
            "original_decision": current_rec,
            "override_by": current_user.id,
            "override_reason": body.reason,
            "override_at": now,
            "override_type": body.override_type,
            "recommendation": "override_shipped",
            "updated_at": now,
        }},
    )

    logger.info(
        "release_validation.overridden",
        validation_id=validation_id,
        override_type=body.override_type,
        original_decision=current_rec,
        org_id=str(org_id),
    )

    doc = await db.release_validations.find_one({"_id": val_oid})
    return ReleaseValidationResponse.from_doc(doc)


class ApproveRequest(BaseModel):
    pass  # approval has no required body


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=5)


@router.post("/{validation_id}/approve", response_model=ReleaseValidationResponse)
async def approve_release_validation(
    validation_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ReleaseValidationResponse:
    """Approve a validation awaiting approval. Only org admins/owners may approve."""
    org_id = _require_org(current_user)
    if current_user.role not in ("owner", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can approve validations")

    val_oid = _parse_oid(validation_id)
    now = datetime.now(timezone.utc)

    doc = await db.release_validations.find_one({"_id": val_oid, "org_id": org_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release validation not found")

    if doc.get("status") != ReleaseValidationStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Validation is not awaiting approval (status: {doc.get('status')})",
        )

    await db.release_validations.update_one(
        {"_id": val_oid},
        {"$set": {
            "status": ReleaseValidationStatus.APPROVED,
            "approved_by": current_user.id,
            "approved_at": now,
            "updated_at": now,
        }},
    )

    # 9B.4 Audit log — approval action
    try:
        await db.score_mutations.insert_one({
            "validation_id": val_oid,
            "old_score": doc.get("confidence_score"),
            "new_score": doc.get("confidence_score"),
            "actor": str(current_user.id),
            "reason": "approved",
            "timestamp": now,
        })
    except Exception:
        pass

    # 9B.4 Fire deferred webhook now that approval is granted
    try:
        project_doc = await db.projects.find_one(
            {"_id": doc["project_id"]}, {"webhook_url": 1}
        )
        webhook_url = (project_doc or {}).get("webhook_url", "").strip()
        if webhook_url:
            from app.worker.tasks.send_webhook import send_webhook_task
            send_webhook_task.delay(
                validation_id=validation_id,
                url=webhook_url,
                payload={
                    "event": "validation.approved",
                    "project_id": str(doc["project_id"]),
                    "validation_id": validation_id,
                    "score": doc.get("confidence_score"),
                    "grade": doc.get("confidence_grade"),
                    "decision": doc.get("recommendation"),
                    "approved_by": str(current_user.id),
                    "approved_at": now.isoformat(),
                },
            )
    except Exception as e:
        logger.warning("release_validation.approve_webhook_error", error=str(e)[:200])

    logger.info("release_validation.approved", validation_id=validation_id, by=str(current_user.id))
    doc = await db.release_validations.find_one({"_id": val_oid})
    return ReleaseValidationResponse.from_doc(doc)


@router.post("/{validation_id}/reject", response_model=ReleaseValidationResponse)
async def reject_release_validation(
    validation_id: str,
    body: RejectRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ReleaseValidationResponse:
    """Reject a validation awaiting approval. Only org admins/owners may reject."""
    org_id = _require_org(current_user)
    if current_user.role not in ("owner", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can reject validations")

    val_oid = _parse_oid(validation_id)
    now = datetime.now(timezone.utc)

    doc = await db.release_validations.find_one({"_id": val_oid, "org_id": org_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release validation not found")

    if doc.get("status") != ReleaseValidationStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Validation is not awaiting approval (status: {doc.get('status')})",
        )

    await db.release_validations.update_one(
        {"_id": val_oid},
        {"$set": {
            "status": ReleaseValidationStatus.REJECTED,
            "rejected_by": current_user.id,
            "rejected_at": now,
            "rejection_reason": body.reason,
            "updated_at": now,
        }},
    )

    # 9B.4 Audit log — rejection action
    try:
        await db.score_mutations.insert_one({
            "validation_id": val_oid,
            "old_score": doc.get("confidence_score"),
            "new_score": doc.get("confidence_score"),
            "actor": str(current_user.id),
            "reason": f"rejected: {body.reason[:100]}",
            "timestamp": now,
        })
    except Exception:
        pass

    logger.info("release_validation.rejected", validation_id=validation_id, by=str(current_user.id))
    doc = await db.release_validations.find_one({"_id": val_oid})
    return ReleaseValidationResponse.from_doc(doc)


class OutcomeRequest(BaseModel):
    outcome: str = Field(..., pattern="^(production_passed|production_failed|rolled_back)$")
    notes: str = ""


@router.post("/{validation_id}/outcome", response_model=ReleaseValidationResponse)
async def record_release_outcome(
    validation_id: str,
    body: OutcomeRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ReleaseValidationResponse:
    """Record the actual production outcome of a shipped release."""
    org_id = _require_org(current_user)
    val_oid = _parse_oid(validation_id)
    now = datetime.now(timezone.utc)

    doc = await db.release_validations.find_one({"_id": val_oid, "org_id": org_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release validation not found")

    if doc.get("status") != ReleaseValidationStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Can only record outcome for completed validations",
        )

    await db.release_validations.update_one(
        {"_id": val_oid},
        {"$set": {
            "outcome": body.outcome,
            "outcome_recorded_at": now,
            "outcome_notes": body.notes,
            "updated_at": now,
        }},
    )

    logger.info(
        "release_validation.outcome_recorded",
        validation_id=validation_id,
        outcome=body.outcome,
        org_id=str(org_id),
    )

    # 7C.2 Trigger incident attribution in background for production failures
    if body.outcome == "production_failed":
        import asyncio as _asyncio
        from app.clients import sync_mongo as _sync_mongo

        def _run_attribution() -> None:
            from app.intelligence.incident_attributor import store_attribution
            store_attribution(_sync_mongo.db, validation_id, str(org_id))

        loop = _asyncio.get_event_loop()
        loop.run_in_executor(None, _run_attribution)

    doc = await db.release_validations.find_one({"_id": val_oid})
    return ReleaseValidationResponse.from_doc(doc)


@router.get("/{validation_id}/stream")
async def stream_validation_progress(
    validation_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> StreamingResponse:
    """SSE stream that emits validation state updates until terminal status is reached.

    Clients receive newline-delimited `data: {...}` events every ~2 seconds while
    the validation is active. The stream closes automatically on completed/failed/cancelled.
    """
    org_id = _require_org(current_user)
    val_oid = _parse_oid(validation_id)

    doc = await db.release_validations.find_one({"_id": val_oid, "org_id": org_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release validation not found")

    async def _generate() -> AsyncGenerator[str, None]:
        _terminal = {"completed", "failed", "cancelled"}
        max_ticks = 300  # cap at 10 minutes (300 × 2s)
        tick = 0

        while tick < max_ticks:
            val = await db.release_validations.find_one(
                {"_id": val_oid, "org_id": org_id},
                {"status": 1, "completed_runs": 1, "total_runs": 1,
                 "confidence_score": 1, "recommendation": 1},
            )
            if not val:
                break

            val_status = val.get("status", "")
            event = {
                "status": val_status,
                "completed_runs": val.get("completed_runs", 0),
                "total_runs": val.get("total_runs", 0),
                "confidence_score": val.get("confidence_score"),
                "recommendation": val.get("recommendation"),
            }
            yield f"data: {json.dumps(event)}\n\n"

            if val_status in _terminal:
                break

            await asyncio.sleep(2)
            tick += 1

        yield 'data: {"done": true}\n\n'

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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


# 9A.8 Score mutation audit log endpoint
@router.get("/{validation_id}/score-history")
async def get_score_history(
    validation_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """Return the immutable audit log of every score write for this validation."""
    org_id = _require_org(current_user)
    val_oid = _parse_oid(validation_id)

    doc = await db.release_validations.find_one(
        {"_id": val_oid, "org_id": org_id}, {"_id": 1, "score_frozen": 1}
    )
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release validation not found")

    mutations = await db.score_mutations.find(
        {"validation_id": val_oid},
        {"_id": 0, "validation_id": 0},
    ).sort("timestamp", 1).to_list(50)

    return {
        "validation_id": validation_id,
        "score_frozen": doc.get("score_frozen", False),
        "history": [
            {
                **m,
                "timestamp": m["timestamp"].isoformat() if m.get("timestamp") else None,
            }
            for m in mutations
        ],
    }
