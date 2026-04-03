from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
import structlog

from app.api.auth.dependencies import get_current_user
from app.dependencies import get_db, get_redis_client, get_s3, get_settings
from app.execution_limits import ExecutionLimiter, QueueDepthExceeded
from app.models import (
    TestCase,
    TestRun,
    TestRunStatus,
    StepResult,
    StepResultStatus,
    User,
)
from app.worker.tasks.execute_test_run import execute_test_run

logger = structlog.get_logger(__name__)


# --- Request / Response schemas ---


class CreateTestRunRequest(BaseModel):
    test_case_id: str
    run_until_step: int = 0  # 0 = all steps; N = run steps 1..N only
    intent_overrides: dict[str, str] = {}  # step_number (str) → intent JSON override


class BatchCreateTestRunRequest(BaseModel):
    project_id: str
    test_case_ids: list[str] | None = None


class BatchCreateTestRunResponse(BaseModel):
    test_runs: list["TestRunResponse"]
    total: int


class StepResultResponse(BaseModel):
    step_number: int
    status: StepResultStatus
    action: str
    expected: str
    actual: str
    error_message: str
    evidence_url: str
    generated_code: str
    duration_ms: int
    retry_count: int = 0
    ai_heal_attempts: int = 0


class TestRunResponse(BaseModel):
    id: str
    org_id: str
    test_case_id: str
    test_case_title: str
    test_type: str
    project_name: str
    batch_name: str
    batch_id: str
    status: TestRunStatus
    triggered_by: str
    celery_task_id: str
    started_at: str | None
    completed_at: str | None
    duration_ms: int
    total_steps: int
    passed_steps: int
    failed_steps: int
    error_message: str
    run_until_step: int
    current_step: int | None = None
    intent_overrides: dict[str, str] = {}
    results: list[StepResultResponse]
    created_at: str
    updated_at: str

    @classmethod
    def from_test_run(cls, tr: TestRun) -> "TestRunResponse":
        return cls(
            id=str(tr.id) if tr.id else "",
            org_id=str(tr.org_id),
            test_case_id=str(tr.test_case_id),
            test_case_title=tr.test_case_title,
            test_type=tr.test_type,
            project_name=tr.project_name,
            batch_name=tr.batch_name,
            batch_id=tr.batch_id,
            status=tr.status,
            triggered_by=str(tr.triggered_by),
            celery_task_id=tr.celery_task_id,
            started_at=tr.started_at.isoformat() if tr.started_at else None,
            completed_at=tr.completed_at.isoformat() if tr.completed_at else None,
            duration_ms=tr.duration_ms,
            total_steps=tr.total_steps,
            passed_steps=tr.passed_steps,
            failed_steps=tr.failed_steps,
            error_message=tr.error_message,
            run_until_step=tr.run_until_step,
            current_step=tr.current_step,
            intent_overrides=tr.intent_overrides or {},
            results=[
                StepResultResponse(
                    step_number=r.step_number,
                    status=r.status,
                    action=r.action,
                    expected=r.expected,
                    actual=r.actual,
                    error_message=r.error_message,
                    evidence_url=r.evidence_url,
                    generated_code=r.generated_code,
                    duration_ms=r.duration_ms,
                )
                for r in tr.results
            ],
            created_at=tr.created_at.isoformat(),
            updated_at=tr.updated_at.isoformat(),
        )


class TestRunListResponse(BaseModel):
    items: list[TestRunResponse]
    total: int
    page: int
    page_size: int


class BatchSummary(BaseModel):
    batch_id: str
    project_name: str
    batch_name: str
    runs: list[TestRunResponse]
    total_runs: int
    passed: int
    failed: int
    queued: int
    running: int
    error: int
    status: str
    created_at: str


class BatchListResponse(BaseModel):
    items: list[BatchSummary]
    total: int
    page: int
    page_size: int


# --- Helpers ---


def parse_object_id(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except (InvalidId, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid ID format: {id_str}",
        )


def require_org(user: User) -> ObjectId:
    if not user.org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User must belong to an organization",
        )
    return user.org_id


# --- Router ---

router = APIRouter(prefix="/api/test-runs", tags=["test-runs"])


@router.post("", response_model=TestRunResponse, status_code=status.HTTP_201_CREATED)
async def create_test_run(
    body: CreateTestRunRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> TestRunResponse:
    org_id = require_org(current_user)
    tc_oid = parse_object_id(body.test_case_id)

    # Rate limiting guard (feature-flagged)
    settings = get_settings(request)
    if settings.execution_rate_limit_enabled:
        redis = get_redis_client(request)
        limiter = ExecutionLimiter(
            max_concurrent_per_org=settings.execution_max_concurrent_per_org,
            max_queue_depth_per_org=settings.execution_max_queue_depth_per_org,
            global_max_concurrent=settings.execution_global_max_concurrent,
            lock_ttl_seconds=settings.execution_lock_ttl_seconds,
        )
        try:
            await limiter.enqueue(redis, str(org_id))
        except QueueDepthExceeded:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many pending test runs. Please wait for some to complete.",
            )

    # Verify test case exists and belongs to org
    tc_doc = await db.test_cases.find_one({"_id": tc_oid, "org_id": org_id})
    if not tc_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test case not found",
        )

    tc = TestCase(**tc_doc)
    now = datetime.now(timezone.utc)

    # Resolve project name
    project_name = ""
    if tc.project_id:
        project_doc = await db.projects.find_one({"_id": tc.project_id})
        if project_doc:
            project_name = project_doc.get("name", "")

    run_until = body.run_until_step
    effective_steps = len(tc.steps) if run_until == 0 else min(run_until, len(tc.steps))

    batch_name = tc.title

    tr = TestRun(
        org_id=org_id,
        test_case_id=tc.id,
        test_case_title=tc.title,
        test_type=tc.test_type,
        project_name=project_name,
        batch_name=batch_name,
        batch_id=str(uuid4()),
        status=TestRunStatus.QUEUED,
        triggered_by=current_user.id,
        total_steps=effective_steps,
        run_until_step=run_until,
        intent_overrides=body.intent_overrides,
        created_at=now,
        updated_at=now,
    )

    # Cancel any running test runs for the same test case
    active_runs = await db.test_runs.find(
        {
            "test_case_id": tc.id,
            "org_id": org_id,
            "status": {"$in": ["queued", "running"]},
        },
        {"_id": 1, "celery_task_id": 1},
    ).to_list(length=10)
    if active_runs:
        from app.worker.celery_app import celery_app as _celery

        for ar in active_runs:
            if ar.get("celery_task_id"):
                _celery.control.revoke(ar["celery_task_id"], terminate=True, signal="SIGTERM")
        await db.test_runs.update_many(
            {"_id": {"$in": [ar["_id"] for ar in active_runs]}},
            {"$set": {"status": "error", "updated_at": now}},
        )

    result = await db.test_runs.insert_one(
        tr.model_dump(by_alias=True, exclude={"id"})
    )
    tr.id = result.inserted_id

    # Dispatch Celery task
    task = execute_test_run.delay(str(tr.id))

    # Store celery task ID back
    await db.test_runs.update_one(
        {"_id": tr.id},
        {"$set": {"celery_task_id": task.id}},
    )
    tr.celery_task_id = task.id

    logger.info(
        "test_run.created",
        test_run_id=str(tr.id),
        test_case_id=str(tc.id),
        org_id=str(org_id),
    )
    return TestRunResponse.from_test_run(tr)


@router.post("/batch", response_model=BatchCreateTestRunResponse, status_code=status.HTTP_201_CREATED)
async def batch_create_test_runs(
    body: BatchCreateTestRunRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> BatchCreateTestRunResponse:
    """Create test runs for selected (or all active) test cases in a project."""
    org_id = require_org(current_user)
    project_oid = parse_object_id(body.project_id)

    if body.test_case_ids:
        # Run only selected test cases
        tc_oids = [parse_object_id(tid) for tid in body.test_case_ids]
        cursor = db.test_cases.find({
            "_id": {"$in": tc_oids},
            "org_id": org_id,
            "project_id": project_oid,
            "status": {"$ne": "archived"},
        })
    else:
        # Run all active test cases in the project
        cursor = db.test_cases.find({
            "org_id": org_id,
            "project_id": project_oid,
            "status": {"$ne": "archived"},
        })

    tc_docs = await cursor.to_list(length=500)

    if not tc_docs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active test cases found",
        )

    # Resolve project name
    project_doc = await db.projects.find_one({"_id": project_oid})
    project_name = project_doc.get("name", "") if project_doc else ""

    now = datetime.now(timezone.utc)
    batch_id = str(uuid4())
    batch_name = f"{project_name} \u00b7 {now.strftime('%b %d, %H:%M')}" if project_name else now.strftime("%b %d, %H:%M")
    created_runs: list[TestRunResponse] = []

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

        result = await db.test_runs.insert_one(
            tr.model_dump(by_alias=True, exclude={"id"})
        )
        tr.id = result.inserted_id

        task = execute_test_run.delay(str(tr.id))
        await db.test_runs.update_one(
            {"_id": tr.id},
            {"$set": {"celery_task_id": task.id}},
        )
        tr.celery_task_id = task.id
        created_runs.append(TestRunResponse.from_test_run(tr))

    logger.info(
        "test_runs.batch_created",
        project_id=body.project_id,
        batch_id=batch_id,
        count=len(created_runs),
        org_id=str(org_id),
    )
    return BatchCreateTestRunResponse(test_runs=created_runs, total=len(created_runs))


@router.get("", response_model=TestRunListResponse)
async def list_test_runs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    test_case_id: str | None = Query(default=None),
    status_filter: TestRunStatus | None = Query(default=None, alias="status"),
) -> TestRunListResponse:
    org_id = require_org(current_user)

    query: dict = {"org_id": org_id}

    if test_case_id is not None:
        query["test_case_id"] = parse_object_id(test_case_id)

    if status_filter is not None:
        query["status"] = status_filter

    total = await db.test_runs.count_documents(query)

    skip = (page - 1) * page_size
    cursor = (
        db.test_runs.find(query)
        .sort("created_at", -1)
        .skip(skip)
        .limit(page_size)
    )
    docs = await cursor.to_list(length=page_size)

    items = [TestRunResponse.from_test_run(TestRun(**doc)) for doc in docs]

    return TestRunListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/batches", response_model=BatchListResponse)
async def list_batches(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
) -> BatchListResponse:
    """List test runs grouped by batch_id."""
    org_id = require_org(current_user)

    match_stage: dict = {"org_id": org_id}
    if search:
        import re
        pattern = re.escape(search)
        match_stage["$or"] = [
            {"project_name": {"$regex": pattern, "$options": "i"}},
            {"batch_name": {"$regex": pattern, "$options": "i"}},
            {"test_case_title": {"$regex": pattern, "$options": "i"}},
        ]

    # Aggregate to group by batch_id
    pipeline: list[dict] = [
        {"$match": match_stage},
        {"$sort": {"created_at": -1}},
        {
            "$group": {
                "_id": {
                    "$cond": [
                        {"$or": [
                            {"$eq": ["$batch_id", ""]},
                            {"$eq": [{"$ifNull": ["$batch_id", ""]}, ""]},
                        ]},
                        {"$toString": "$_id"},  # solo runs get their own group
                        "$batch_id",
                    ]
                },
                "runs": {"$push": "$$ROOT"},
                "project_name": {"$first": "$project_name"},
                "batch_name": {"$first": "$batch_name"},
                "created_at": {"$min": "$created_at"},
                "total_runs": {"$sum": 1},
                "passed": {
                    "$sum": {"$cond": [{"$eq": ["$status", "passed"]}, 1, 0]}
                },
                "failed": {
                    "$sum": {"$cond": [{"$eq": ["$status", "failed"]}, 1, 0]}
                },
                "queued": {
                    "$sum": {"$cond": [{"$eq": ["$status", "queued"]}, 1, 0]}
                },
                "running": {
                    "$sum": {"$cond": [{"$eq": ["$status", "running"]}, 1, 0]}
                },
                "error": {
                    "$sum": {"$cond": [{"$eq": ["$status", "error"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"created_at": -1}},
    ]

    # Get total count first
    count_pipeline = pipeline + [{"$count": "total"}]
    count_result = await db.test_runs.aggregate(count_pipeline).to_list(length=1)
    total = count_result[0]["total"] if count_result else 0

    # Paginate
    skip = (page - 1) * page_size
    pipeline += [{"$skip": skip}, {"$limit": page_size}]

    batches_raw = await db.test_runs.aggregate(pipeline).to_list(length=page_size)

    items: list[BatchSummary] = []
    for batch in batches_raw:
        # Compute overall status
        if batch["running"] > 0 or batch["queued"] > 0:
            overall = "running"
        elif batch["error"] > 0:
            overall = "error"
        elif batch["failed"] > 0:
            overall = "failed"
        else:
            overall = "passed"

        runs = [
            TestRunResponse.from_test_run(TestRun(**doc))
            for doc in batch["runs"]
        ]

        # Apply status filter at batch level
        if status_filter and overall != status_filter:
            total -= 1
            continue

        items.append(BatchSummary(
            batch_id=batch["_id"],
            project_name=batch.get("project_name", "") or "",
            batch_name=batch.get("batch_name", "") or "",
            runs=runs,
            total_runs=batch["total_runs"],
            passed=batch["passed"],
            failed=batch["failed"],
            queued=batch["queued"],
            running=batch["running"],
            error=batch["error"],
            status=overall,
            created_at=batch["created_at"].isoformat(),
        ))

    return BatchListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{test_run_id}", response_model=TestRunResponse)
async def get_test_run(
    test_run_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> TestRunResponse:
    org_id = require_org(current_user)
    oid = parse_object_id(test_run_id)

    doc = await db.test_runs.find_one({"_id": oid, "org_id": org_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test run not found",
        )

    return TestRunResponse.from_test_run(TestRun(**doc))


class RerunStepRequest(BaseModel):
    step_number: int
    instruction: str = ""
    override_intent: str = ""  # User-edited intent JSON, bypasses AI generation


@router.post("/{test_run_id}/rerun-step", response_model=TestRunResponse)
async def rerun_step(
    test_run_id: str,
    body: RerunStepRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> TestRunResponse:
    """Re-execute a single step in-place on an existing test run."""
    from app.worker.tasks.execute_test_run import rerun_step as rerun_step_task

    org_id = require_org(current_user)
    oid = parse_object_id(test_run_id)

    doc = await db.test_runs.find_one({"_id": oid, "org_id": org_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test run not found",
        )

    tr = TestRun(**doc)

    # If instruction provided, append to the step's action in the test case
    if body.instruction:
        tc_doc = await db.test_cases.find_one({"_id": tr.test_case_id, "org_id": org_id})
        if tc_doc:
            updated_steps = []
            for s in tc_doc.get("steps", []):
                if s["step_number"] == body.step_number:
                    s = {**s, "action": f"{s['action']}. {body.instruction}", "custom_code": ""}
                updated_steps.append(s)
            await db.test_cases.update_one(
                {"_id": tr.test_case_id},
                {"$set": {"steps": updated_steps, "updated_at": datetime.now(timezone.utc)}},
            )

    # Revoke any existing celery task before dispatching a new one
    if tr.celery_task_id:
        try:
            from app.worker.celery_app import celery_app as _celery
            _celery.control.revoke(tr.celery_task_id, terminate=True, signal="SIGTERM")
        except Exception:
            pass

    # Mark as running and dispatch
    new_task = rerun_step_task.delay(
        test_run_id, body.step_number, body.override_intent or None,
    )
    await db.test_runs.update_one(
        {"_id": oid},
        {"$set": {"status": "running", "celery_task_id": new_task.id, "updated_at": datetime.now(timezone.utc)}},
    )
    tr.status = TestRunStatus.RUNNING

    logger.info(
        "test_run.rerun_step",
        test_run_id=test_run_id,
        step_number=body.step_number,
        has_override=bool(body.override_intent),
        org_id=str(org_id),
    )

    # Reload to return latest state
    doc = await db.test_runs.find_one({"_id": oid})
    return TestRunResponse.from_test_run(TestRun(**doc))


class RepairSuggestionResponse(BaseModel):
    type: str
    description: str
    confidence: float
    changes: dict


@router.post("/{test_run_id}/steps/{step_number}/repair-suggestions")
async def get_repair_suggestions(
    test_run_id: str,
    step_number: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    request: Request,
) -> list[RepairSuggestionResponse]:
    """Generate AI repair suggestions for a failed step."""
    from app.intelligence.repair_engine import suggest_repairs

    org_id = require_org(current_user)
    oid = parse_object_id(test_run_id)
    settings = get_settings(request)

    doc = await db.test_runs.find_one({"_id": oid, "org_id": org_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Test run not found")

    tr = TestRun(**doc)
    step_result = next((r for r in tr.results if r.step_number == step_number), None)
    if not step_result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found")

    # Fetch screenshot from MinIO (best-effort)
    screenshot: bytes | None = None
    if step_result.evidence_url:
        s3_client = get_s3(request)
        key = f"{step_result.evidence_url.rsplit('/', 1)[0]}/step_{step_number}.png" \
            if not step_result.evidence_url.endswith(".png") \
            else step_result.evidence_url
        try:
            async with s3_client.get_client() as client:
                s3_resp = await client.get_object(
                    Bucket=settings.minio_default_bucket, Key=key
                )
                screenshot = await s3_resp["Body"].read()
        except Exception:
            pass

    if not settings.openai_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI repair requires an OpenAI API key",
        )

    suggestions = await _run_repair_in_thread(
        step_number=step_number,
        action=step_result.action,
        expected=step_result.expected,
        actual=step_result.actual,
        error_message=step_result.error_message,
        generated_code=step_result.generated_code,
        screenshot_bytes=screenshot,
        api_key=settings.openai_api_key,
    )

    return [
        RepairSuggestionResponse(
            type=s.type,
            description=s.description,
            confidence=s.confidence,
            changes=s.changes,
        )
        for s in suggestions
    ]


async def _run_repair_in_thread(**kwargs) -> list:
    """Run the synchronous repair engine in a thread pool to avoid blocking the event loop."""
    import asyncio
    from functools import partial
    from app.intelligence.repair_engine import suggest_repairs

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(suggest_repairs, **kwargs))


class ValidateIntentRequest(BaseModel):
    intent_json: str


@router.post("/validate-intent")
async def validate_intent(
    body: ValidateIntentRequest,
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Validate an intent JSON string without executing it."""
    from app.worker.intent_schema import StepIntent

    try:
        StepIntent.from_json_str(body.intent_json)
        return {"valid": True}
    except Exception as e:
        return {"valid": False, "error": str(e)[:500]}


@router.delete("/batch/{batch_id}")
async def delete_batch(
    batch_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    org_id = require_org(current_user)
    result = await db.test_runs.delete_many({"batch_id": batch_id, "org_id": org_id})
    if result.deleted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Batch not found",
        )
    logger.info(
        "test_runs.batch_deleted",
        batch_id=batch_id,
        count=result.deleted_count,
        org_id=str(org_id),
    )
    return {"deleted": result.deleted_count}


@router.post("/{test_run_id}/stop")
async def stop_test_run(
    test_run_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    org_id = require_org(current_user)
    oid = parse_object_id(test_run_id)

    doc = await db.test_runs.find_one({"_id": oid, "org_id": org_id}, {"status": 1, "celery_task_id": 1})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Test run not found")

    if doc.get("status") not in ("queued", "running"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Run is not active")

    celery_task_id = doc.get("celery_task_id")
    if celery_task_id:
        try:
            from app.worker.celery_app import celery_app as _celery
            _celery.control.revoke(celery_task_id, terminate=True, signal="SIGTERM")
        except Exception:
            pass

    await db.test_runs.update_one(
        {"_id": oid},
        {"$set": {
            "status": "error",
            "error_message": "Stopped by user",
            "completed_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    logger.info("test_run.stopped", test_run_id=test_run_id, org_id=str(org_id))
    return {"stopped": True}


@router.delete("/{test_run_id}")
async def delete_test_run(
    test_run_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    org_id = require_org(current_user)
    oid = parse_object_id(test_run_id)

    result = await db.test_runs.delete_one({"_id": oid, "org_id": org_id})
    if result.deleted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test run not found",
        )
    logger.info("test_run.deleted", test_run_id=test_run_id, org_id=str(org_id))
    return {"deleted": 1}


@router.get("/{test_run_id}/evidence/{step_number}")
async def get_step_evidence(
    test_run_id: str,
    step_number: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    request: Request,
    type: str = Query(default="screenshot", description="Evidence type: screenshot, console, dom, network"),
) -> Response:
    """Stream a step's evidence from MinIO.

    Supports multiple evidence types via the `type` query parameter:
    - screenshot (default): PNG screenshot
    - console: Browser console logs (JSON)
    - dom: DOM snapshot (HTML)
    - network: Network request log (JSON)
    """
    org_id = require_org(current_user)
    oid = parse_object_id(test_run_id)

    doc = await db.test_runs.find_one({"_id": oid, "org_id": org_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test run not found",
        )

    tr = TestRun(**doc)
    step_result = next(
        (r for r in tr.results if r.step_number == step_number), None
    )
    if not step_result or not step_result.evidence_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No evidence for this step",
        )

    # Derive the evidence key based on type
    base_key = step_result.evidence_url  # e.g., "org/run/step_1.png"
    prefix = base_key.rsplit("/", 1)[0]  # "org/run"

    type_map = {
        "screenshot": (f"{prefix}/step_{step_number}.png", "image/png"),
        "console": (f"{prefix}/step_{step_number}_console.json", "application/json"),
        "dom": (f"{prefix}/step_{step_number}_dom.html", "text/html"),
        "network": (f"{prefix}/step_{step_number}_network.json", "application/json"),
    }

    if type not in type_map:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid evidence type: {type}. Must be one of: {', '.join(type_map.keys())}",
        )

    key, content_type = type_map[type]

    s3_client = get_s3(request)
    settings = get_settings(request)

    try:
        async with s3_client.get_client() as client:
            s3_response = await client.get_object(
                Bucket=settings.minio_default_bucket,
                Key=key,
            )
            body = await s3_response["Body"].read()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evidence type '{type}' not available for this step",
        )

    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/{test_run_id}/evidence/{step_number}/manifest")
async def get_step_evidence_manifest(
    test_run_id: str,
    step_number: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    request: Request,
) -> dict:
    """Return a manifest of available evidence types for a step.

    Checks which evidence files exist in MinIO for the given step.
    Returns: {"available": ["screenshot", "console", "dom", "network"]}
    """
    org_id = require_org(current_user)
    oid = parse_object_id(test_run_id)

    doc = await db.test_runs.find_one({"_id": oid, "org_id": org_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test run not found",
        )

    tr = TestRun(**doc)
    step_result = next(
        (r for r in tr.results if r.step_number == step_number), None
    )
    if not step_result or not step_result.evidence_url:
        return {"available": []}

    prefix = step_result.evidence_url.rsplit("/", 1)[0]
    evidence_keys = {
        "screenshot": f"{prefix}/step_{step_number}.png",
        "console": f"{prefix}/step_{step_number}_console.json",
        "dom": f"{prefix}/step_{step_number}_dom.html",
        "network": f"{prefix}/step_{step_number}_network.json",
    }

    s3_client = get_s3(request)
    settings = get_settings(request)
    available = []

    async with s3_client.get_client() as client:
        for evidence_type, key in evidence_keys.items():
            try:
                await client.head_object(
                    Bucket=settings.minio_default_bucket,
                    Key=key,
                )
                available.append(evidence_type)
            except Exception:
                pass

    return {"available": available}


# --- Telemetry endpoint ---


@router.get("/{test_run_id}/telemetry")
async def get_test_run_telemetry(
    test_run_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """Return execution telemetry for a test run.

    Includes per-step timing breakdowns, retry history,
    selector outcomes, AI call metrics, and environment snapshot.
    """
    org_id = require_org(current_user)
    oid = parse_object_id(test_run_id)

    # Verify user owns this test run
    run_doc = await db.test_runs.find_one({"_id": oid, "org_id": org_id})
    if not run_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test run not found",
        )

    tel_doc = await db.execution_telemetry.find_one({"test_run_id": test_run_id})
    if not tel_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No telemetry available for this test run",
        )

    # Convert ObjectId for JSON serialization
    tel_doc["_id"] = str(tel_doc["_id"])
    return tel_doc


# --- Execution Profile endpoint ---


@router.get("/{test_run_id}/profile")
async def get_execution_profile(
    test_run_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """Return the execution profile for the test case associated with this run.

    The profile aggregates pass rates, flake rates, timing distributions,
    and per-step reliability metrics across recent runs of the same test case.
    """
    org_id = require_org(current_user)
    oid = parse_object_id(test_run_id)

    run_doc = await db.test_runs.find_one({"_id": oid, "org_id": org_id})
    if not run_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test run not found",
        )

    test_case_id = str(run_doc["test_case_id"])
    profile_doc = await db.execution_profiles.find_one(
        {"test_case_id": test_case_id, "org_id": str(org_id)},
    )
    if not profile_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No execution profile available yet. Run the test case at least once.",
        )

    profile_doc["_id"] = str(profile_doc["_id"])
    return profile_doc


# ---------------------------------------------------------------------------
# Full step results (3.1 — externalized from test_runs documents)
# ---------------------------------------------------------------------------


@router.get("/{test_run_id}/steps")
async def get_test_run_steps(
    test_run_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """Return full step results including generated_code, sourced from test_run_steps.

    Falls back to test_runs.results for legacy runs that pre-date externalization.
    """
    org_id = require_org(current_user)
    oid = parse_object_id(test_run_id)

    # Verify org access
    run_doc = await db.test_runs.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not run_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Test run not found")

    # Primary: test_run_steps (full data)
    steps_doc = await db.test_run_steps.find_one({"test_run_id": test_run_id, "org_id": str(org_id)})
    if steps_doc:
        steps_doc.pop("_id", None)
        return {"source": "test_run_steps", "steps": steps_doc.get("steps", [])}

    # Fallback: test_runs.results (lean, no generated_code)
    full_run = await db.test_runs.find_one({"_id": oid, "org_id": org_id}, {"results": 1})
    return {"source": "test_runs_legacy", "steps": full_run.get("results", []) if full_run else []}


# ---------------------------------------------------------------------------
# PDF report
# ---------------------------------------------------------------------------


@router.get("/{test_run_id}/report")
async def get_test_run_report(
    test_run_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    request: Request,
) -> Response:
    """Download a comprehensive PDF report for a test run.

    On first request the report is generated by a Celery worker and cached
    in MinIO.  Subsequent requests stream the cached PDF directly.
    """
    import asyncio

    org_id = require_org(current_user)
    oid = parse_object_id(test_run_id)

    doc = await db.test_runs.find_one({"_id": oid, "org_id": org_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test run not found",
        )

    if doc.get("status") in ("queued", "running"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Test run is not yet complete. Wait for it to finish before downloading the report.",
        )

    s3_client = get_s3(request)
    app_settings = get_settings(request)
    bucket = app_settings.minio_default_bucket
    pdf_key = f"{org_id}/{test_run_id}/report.pdf"

    # Check MinIO cache first
    try:
        async with s3_client.get_client() as client:
            s3_resp = await client.get_object(Bucket=bucket, Key=pdf_key)
            pdf_bytes = await s3_resp["Body"].read()

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="cg-report-{test_run_id[:8]}.pdf"',
                "Cache-Control": "private, max-age=3600",
            },
        )
    except Exception:
        pass  # Not cached — generate below

    # Dispatch Celery task and wait for result
    from app.worker.tasks.generate_report import generate_report

    task = generate_report.delay(test_run_id, str(org_id))

    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, task.get, 60),
            timeout=65,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Report generation timed out. Please try again.",
        )
    except Exception as exc:
        logger.error("report.wait_failed", error=str(exc)[:200])
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Report generation failed. Please try again.",
        )

    if not isinstance(result, dict) or result.get("status") != "ok":
        detail = result.get("message", "Unknown error") if isinstance(result, dict) else "Unknown error"
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Report generation failed: {detail}",
        )

    # Fetch the generated PDF from MinIO
    try:
        async with s3_client.get_client() as client:
            s3_resp = await client.get_object(Bucket=bucket, Key=pdf_key)
            pdf_bytes = await s3_resp["Body"].read()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Report was generated but could not be retrieved.",
        )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="cg-report-{test_run_id[:8]}.pdf"',
            "Cache-Control": "private, max-age=3600",
        },
    )
