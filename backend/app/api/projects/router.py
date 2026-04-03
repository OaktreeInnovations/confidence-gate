from datetime import datetime, timezone
from typing import Annotated
import io
import uuid

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
import structlog

from app.api.auth.dependencies import get_current_user
from app.dependencies import get_db, get_s3, get_settings
from app.models import Project, User

logger = structlog.get_logger(__name__)


# --- Request / Response schemas ---


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    base_url: str = ""
    global_setup: str = ""
    webhook_url: str = ""
    # 3.1 Change-aware git integration
    git_repo_url: str = ""
    git_token: str = ""
    # 3.5 Approval workflow
    release_approvers: list[str] = Field(default_factory=list)


class UpdateProjectRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    base_url: str | None = None
    global_setup: str | None = None
    webhook_url: str | None = None
    git_repo_url: str | None = None
    git_token: str | None = None
    release_approvers: list[str] | None = None
    # 8.3 Configurable gate thresholds
    inconclusive_gate_pct: float | None = None
    behavior_override_pct: float | None = None


class ProjectResponse(BaseModel):
    id: str
    org_id: str
    name: str
    description: str
    base_url: str
    global_setup: str
    webhook_url: str = ""
    git_repo_url: str = ""
    release_approvers: list[str] = Field(default_factory=list)
    # 8.3 Configurable gate thresholds
    inconclusive_gate_pct: float | None = None
    behavior_override_pct: float | None = None
    created_by: str
    created_at: str
    updated_at: str
    test_case_count: int = 0

    @classmethod
    def from_project(
        cls, project: Project, test_case_count: int = 0
    ) -> "ProjectResponse":
        return cls(
            id=str(project.id) if project.id else "",
            org_id=str(project.org_id),
            name=project.name,
            description=project.description,
            base_url=project.base_url,
            global_setup=project.global_setup,
            webhook_url=project.webhook_url,
            git_repo_url=project.git_repo_url,
            release_approvers=[str(a) for a in project.release_approvers],
            inconclusive_gate_pct=project.inconclusive_gate_pct,
            behavior_override_pct=project.behavior_override_pct,
            created_by=str(project.created_by),
            created_at=project.created_at.isoformat(),
            updated_at=project.updated_at.isoformat(),
            test_case_count=test_case_count,
        )


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse]
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

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: CreateProjectRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ProjectResponse:
    org_id = require_org(current_user)
    now = datetime.now(timezone.utc)

    from bson import ObjectId as _ObjId
    approver_oids = []
    for a in body.release_approvers:
        try:
            approver_oids.append(_ObjId(a))
        except Exception:
            pass

    project = Project(
        org_id=org_id,
        name=body.name,
        description=body.description,
        base_url=body.base_url,
        global_setup=body.global_setup,
        webhook_url=body.webhook_url,
        git_repo_url=body.git_repo_url,
        git_token=body.git_token,
        release_approvers=approver_oids,
        created_by=current_user.id,
        created_at=now,
        updated_at=now,
    )

    result = await db.projects.insert_one(
        project.model_dump(by_alias=True, exclude={"id"})
    )
    project.id = result.inserted_id

    logger.info("project.created", project_id=str(project.id), org_id=str(org_id))
    return ProjectResponse.from_project(project)


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None),
) -> ProjectListResponse:
    org_id = require_org(current_user)

    # Auto-migrate orphan test cases (no project_id) into a "Default" project
    orphan_count = await db.test_cases.count_documents(
        {"org_id": org_id, "project_id": None}
    )
    if orphan_count > 0:
        now = datetime.now(timezone.utc)
        default_project = Project(
            org_id=org_id,
            name="Default",
            description="Auto-created project for pre-existing test cases",
            created_by=current_user.id,
            created_at=now,
            updated_at=now,
        )
        result = await db.projects.insert_one(
            default_project.model_dump(by_alias=True, exclude={"id"})
        )
        default_project.id = result.inserted_id
        await db.test_cases.update_many(
            {"org_id": org_id, "project_id": None},
            {"$set": {"project_id": default_project.id, "updated_at": now}},
        )
        logger.info(
            "project.default_created",
            project_id=str(default_project.id),
            org_id=str(org_id),
            migrated_count=orphan_count,
        )

    query: dict = {"org_id": org_id}
    if search:
        query["name"] = {"$regex": search, "$options": "i"}

    total = await db.projects.count_documents(query)

    skip = (page - 1) * page_size
    cursor = (
        db.projects.find(query)
        .sort("created_at", -1)
        .skip(skip)
        .limit(page_size)
    )
    docs = await cursor.to_list(length=page_size)

    # Batch count test cases per project in a single aggregation
    project_ids = [doc["_id"] for doc in docs]
    count_pipeline = [
        {"$match": {"org_id": org_id, "project_id": {"$in": project_ids}, "status": {"$ne": "archived"}}},
        {"$group": {"_id": "$project_id", "count": {"$sum": 1}}},
    ]
    count_cursor = db.test_cases.aggregate(count_pipeline)
    counts_by_project: dict = {}
    async for entry in count_cursor:
        counts_by_project[entry["_id"]] = entry["count"]

    items = []
    for doc in docs:
        project = Project(**doc)
        count = counts_by_project.get(project.id, 0)
        items.append(ProjectResponse.from_project(project, test_case_count=count))

    return ProjectListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ProjectResponse:
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    doc = await db.projects.find_one({"_id": oid, "org_id": org_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project = Project(**doc)
    count = await db.test_cases.count_documents({
        "org_id": org_id,
        "project_id": project.id,
        "status": {"$ne": "archived"},
    })

    return ProjectResponse.from_project(project, test_case_count=count)


@router.put("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    body: UpdateProjectRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ProjectResponse:
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    update_data: dict = {}
    if body.name is not None:
        update_data["name"] = body.name
    if body.description is not None:
        update_data["description"] = body.description
    if body.base_url is not None:
        update_data["base_url"] = body.base_url
    if body.global_setup is not None:
        update_data["global_setup"] = body.global_setup
    if body.webhook_url is not None:
        update_data["webhook_url"] = body.webhook_url
    if body.git_repo_url is not None:
        update_data["git_repo_url"] = body.git_repo_url
    if body.git_token is not None:
        update_data["git_token"] = body.git_token
    if body.release_approvers is not None:
        from bson import ObjectId as _ObjId
        update_data["release_approvers"] = [_ObjId(a) for a in body.release_approvers if a]
    # 8.3 Configurable gate thresholds
    if body.inconclusive_gate_pct is not None:
        update_data["inconclusive_gate_pct"] = body.inconclusive_gate_pct
    if body.behavior_override_pct is not None:
        update_data["behavior_override_pct"] = body.behavior_override_pct

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update",
        )

    update_data["updated_at"] = datetime.now(timezone.utc)

    result = await db.projects.find_one_and_update(
        {"_id": oid, "org_id": org_id},
        {"$set": update_data},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    logger.info("project.updated", project_id=project_id, org_id=str(org_id))
    return ProjectResponse.from_project(Project(**result))


class CIGateRequest(BaseModel):
    min_confidence: float = Field(..., ge=0, le=100)


class CIGateResponse(BaseModel):
    passed: bool
    confidence_score: float
    release_grade: str
    computed_at: str | None = None


@router.get("/{project_id}/release-confidence")
async def get_project_release_confidence(
    project_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """Get weighted release confidence across all test cases in a project."""
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    project_doc = await db.projects.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    # Get all test case IDs in this project
    tc_cursor = db.test_cases.find(
        {"project_id": oid, "org_id": org_id, "status": {"$ne": "archived"}},
        {"_id": 1},
    )
    tc_ids = [str(doc["_id"]) async for doc in tc_cursor]

    if not tc_ids:
        return {"score": 0, "grade": "F", "avg_pass_rate": 0, "avg_flake_rate": 0, "test_case_count": 0}

    # Fetch execution profiles for these test cases
    profiles = await db.execution_profiles.find(
        {"test_case_id": {"$in": tc_ids}, "org_id": str(org_id)},
    ).to_list(length=len(tc_ids))

    if not profiles:
        return {"score": 0, "grade": "F", "avg_pass_rate": 0, "avg_flake_rate": 0, "test_case_count": len(tc_ids)}

    total_pass_rate = 0.0
    total_flake_score = 0.0
    count = len(profiles)

    for p in profiles:
        total_pass_rate += p.get("pass_rate", 0)
        flake = p.get("flake_report", {})
        total_flake_score += flake.get("overall_flake_score", 0)

    avg_pass = total_pass_rate / count if count else 0
    avg_flake = total_flake_score / count if count else 0

    # Weighted confidence: pass_rate contribution - flake penalty
    score = max(0, min(100, avg_pass * 100 - avg_flake * 50))
    grade = (
        "A" if score >= 90 else
        "B" if score >= 75 else
        "C" if score >= 60 else
        "D" if score >= 40 else "F"
    )

    return {
        "score": round(score, 1),
        "grade": grade,
        "avg_pass_rate": round(avg_pass, 3),
        "avg_flake_rate": round(avg_flake, 3),
        "test_case_count": len(tc_ids),
        "profiles_count": count,
    }


@router.get("/{project_id}/predicted-score")
async def get_predicted_score(
    project_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """3.3 Predict the next release validation score without running tests."""
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    project_doc = await db.projects.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    import asyncio
    from app.intelligence.prediction_engine import compute_predicted_score
    loop = asyncio.get_event_loop()
    from app.clients import sync_mongo as _sync_mongo
    result = await loop.run_in_executor(
        None,
        compute_predicted_score,
        _sync_mongo.db,
        str(org_id),
        project_id,
    )
    return result


@router.get("/{project_id}/prediction-accuracy")
async def get_prediction_accuracy(
    project_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    n: int = 20,
) -> dict:
    """4.4 Return prediction accuracy stats for the last N completed validations."""
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    project_doc = await db.projects.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    # Fetch last N completed validations that have both scores
    docs = await db.release_validations.find(
        {
            "project_id": oid,
            "org_id": org_id,
            "status": "completed",
            "predicted_score_pre_run": {"$ne": None},
            "prediction_accuracy": {"$ne": None},
        },
        {"_id": 1, "predicted_score_pre_run": 1, "prediction_accuracy": 1,
         "confidence_score": 1, "created_at": 1},
    ).sort("created_at", -1).limit(n).to_list(n)

    if not docs:
        return {"sample_size": 0, "mean_absolute_error": None, "reliable": None, "deltas": []}

    deltas = [
        {
            "id": str(d["_id"]),
            "predicted": d["predicted_score_pre_run"],
            "actual": d.get("confidence_score"),
            "delta": d["prediction_accuracy"],
            "created_at": d["created_at"].isoformat() if d.get("created_at") else None,
        }
        for d in docs
    ]
    abs_errors = [abs(d["delta"]) for d in deltas if d["delta"] is not None]
    mae = round(sum(abs_errors) / len(abs_errors), 1) if abs_errors else None
    reliable = (mae is not None and mae <= 15)

    return {
        "sample_size": len(deltas),
        "mean_absolute_error": mae,
        "reliable": reliable,
        "deltas": deltas,
    }


@router.get("/{project_id}/benchmark")
async def get_project_benchmark(
    project_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """3.4 Return within-org benchmark comparison for test cases in this project."""
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    project_doc = await db.projects.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    import asyncio
    from app.intelligence.benchmark_engine import get_benchmark_for_project
    from app.clients import sync_mongo as _sync_mongo
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        get_benchmark_for_project,
        _sync_mongo.db,
        str(org_id),
        project_id,
    )
    # Strip internal _id if present
    result.pop("_id", None)
    return result


@router.get("/{project_id}/confidence-benchmark")
async def get_confidence_benchmark(
    project_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """7C.3 Return this org's cross-org confidence score percentile ranking."""
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    project_doc = await db.projects.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    stats = await db.benchmark_stats.find_one(
        {"org_id": str(org_id)},
        {"confidence_percentile": 1, "percentile_computed_at": 1},
    )

    if not stats or stats.get("confidence_percentile") is None:
        return {"confidence_percentile": None, "available": False}

    return {
        "confidence_percentile": stats["confidence_percentile"],
        "available": True,
        "computed_at": stats["percentile_computed_at"].isoformat() if stats.get("percentile_computed_at") else None,
    }


@router.get("/{project_id}/failure-graph")
async def get_project_failure_graph(
    project_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """Get the org failure graph filtered to this project's test cases."""
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    project_doc = await db.projects.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    # Get test case IDs in this project
    tc_cursor = db.test_cases.find(
        {"project_id": oid, "org_id": org_id, "status": {"$ne": "archived"}},
        {"_id": 1},
    )
    tc_ids = {str(doc["_id"]) async for doc in tc_cursor}

    # Get org failure graph
    graph_doc = await db.failure_graph.find_one({"org_id": str(org_id)})
    if not graph_doc:
        return {"status": "no_data", "clusters": [], "feature_risks": []}

    # Filter clusters to only include this project's test cases
    filtered_clusters = []
    for cluster in graph_doc.get("clusters", []):
        cluster_tc_ids = set(cluster.get("test_case_ids", []))
        overlap = cluster_tc_ids & tc_ids
        if overlap:
            filtered = {**cluster, "test_case_ids": list(overlap)}
            filtered_clusters.append(filtered)

    return {
        "status": "ok",
        "computed_at": graph_doc.get("computed_at"),
        "clusters": filtered_clusters,
    }


@router.post("/{project_id}/gate", response_model=CIGateResponse)
async def ci_gate(
    project_id: str,
    body: CIGateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> CIGateResponse:
    """CI gate: check if project meets minimum confidence threshold.

    Uses the most recent completed release validation score (same 11-layer pipeline
    as the UI) so CI and the dashboard always agree. Falls back to a simple profile
    aggregation when no completed validations exist yet.
    """
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    project_doc = await db.projects.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    # Primary: use the most recently completed release validation (full pipeline score)
    last_validation = await db.release_validations.find_one(
        {
            "project_id": oid,
            "org_id": org_id,
            "status": "completed",
            "final_score_at_decision": {"$ne": None},
        },
        {"final_score_at_decision": 1, "confidence_grade": 1, "completed_at": 1},
        sort=[("completed_at", -1)],
    )

    if last_validation:
        score = float(last_validation["final_score_at_decision"])
        grade = last_validation.get("confidence_grade") or (
            "A" if score >= 90 else
            "B" if score >= 75 else
            "C" if score >= 60 else
            "D" if score >= 40 else "F"
        )
        completed_at = last_validation.get("completed_at")
        computed_at = completed_at.isoformat() if hasattr(completed_at, "isoformat") else str(completed_at) if completed_at else None
        return CIGateResponse(
            passed=score >= body.min_confidence,
            confidence_score=round(score, 1),
            release_grade=grade,
            computed_at=computed_at,
        )

    # Fallback: no completed validations yet — aggregate from execution profiles
    tc_cursor = db.test_cases.find(
        {"project_id": oid, "org_id": org_id, "status": {"$ne": "archived"}},
        {"_id": 1},
    )
    tc_ids = [str(doc["_id"]) async for doc in tc_cursor]

    if not tc_ids:
        return CIGateResponse(passed=False, confidence_score=0, release_grade="F")

    profiles = await db.execution_profiles.find(
        {"test_case_id": {"$in": tc_ids}, "org_id": str(org_id)},
        {"pass_rate": 1, "flake_report.overall_flake_score": 1, "updated_at": 1},
    ).to_list(length=len(tc_ids))

    if not profiles:
        return CIGateResponse(passed=False, confidence_score=0, release_grade="F")

    count = len(profiles)
    avg_pass = sum(p.get("pass_rate", 0) for p in profiles) / count
    avg_flake = sum(p.get("flake_report", {}).get("overall_flake_score", 0) for p in profiles) / count

    score = max(0, min(100, avg_pass * 100 - avg_flake * 50))
    grade = (
        "A" if score >= 90 else
        "B" if score >= 75 else
        "C" if score >= 60 else
        "D" if score >= 40 else "F"
    )

    computed_at = None
    for p in profiles:
        ts = p.get("updated_at")
        if ts:
            computed_at = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)

    return CIGateResponse(
        passed=score >= body.min_confidence,
        confidence_score=round(score, 1),
        release_grade=grade,
        computed_at=computed_at,
    )


# ── Project Documents ────────────────────────────────────────────────────────

_TEXT_EXTENSIONS = {"txt", "md", "markdown", "rst", "csv", "json", "yaml", "yml"}
_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}
_PDF_EXTENSIONS = {"pdf"}
_MAX_DOC_SIZE = 20 * 1024 * 1024  # 20 MB


def _doc_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _PDF_EXTENSIONS:
        return "pdf"
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _TEXT_EXTENSIONS:
        return "text"
    return "other"


def _extract_text(filename: str, content: bytes) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _TEXT_EXTENSIONS:
        return content.decode("utf-8", errors="ignore")[:50_000]
    if ext in _PDF_EXTENSIONS:
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            pages_text = []
            for page in reader.pages:
                t = page.extract_text()
                if t:
                    pages_text.append(t)
            return "\n".join(pages_text)[:50_000]
        except Exception:
            return ""
    return ""


@router.post("/{project_id}/documents")
async def upload_project_document(
    project_id: str,
    request: Request,
    file: UploadFile = File(...),
    current_user: Annotated[User, Depends(get_current_user)] = ...,  # type: ignore[assignment]
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)] = ...,  # type: ignore[assignment]
) -> dict:
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    project_doc = await db.projects.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    content = await file.read()
    if len(content) > _MAX_DOC_SIZE:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large (max 20 MB)")

    doc_id = str(uuid.uuid4())
    filename = file.filename or "document"
    dtype = _doc_type(filename)
    extracted_text = _extract_text(filename, content)

    s3_key = f"project-docs/{org_id}/{project_id}/{doc_id}/{filename}"

    settings = get_settings(request)
    s3_client = get_s3(request)
    async with s3_client.get_client() as client:
        await client.put_object(
            Bucket=settings.minio_default_bucket,
            Key=s3_key,
            Body=content,
            ContentType=file.content_type or "application/octet-stream",
        )

    now = datetime.now(timezone.utc)
    doc_record = {
        "_id": doc_id,
        "project_id": oid,
        "org_id": org_id,
        "name": filename,
        "type": dtype,
        "size": len(content),
        "s3_key": s3_key,
        "extracted_text": extracted_text,
        "uploaded_by": current_user.id,
        "uploaded_at": now,
    }
    await db.project_documents.insert_one(doc_record)

    logger.info("project.document_uploaded", project_id=project_id, doc_id=doc_id, dtype=dtype, size=len(content))
    return {
        "id": doc_id,
        "name": filename,
        "type": dtype,
        "size": len(content),
        "uploaded_at": now.isoformat(),
    }


@router.get("/{project_id}/documents")
async def list_project_documents(
    project_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    project_doc = await db.projects.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    docs = await db.project_documents.find(
        {"project_id": oid, "org_id": org_id},
        {"extracted_text": 0},
    ).sort("uploaded_at", -1).to_list(100)

    return {
        "documents": [
            {
                "id": str(d["_id"]),
                "name": d["name"],
                "type": d["type"],
                "size": d["size"],
                "uploaded_at": d["uploaded_at"].isoformat() if d.get("uploaded_at") else None,
            }
            for d in docs
        ]
    }


@router.delete("/{project_id}/documents/{doc_id}")
async def delete_project_document(
    project_id: str,
    doc_id: str,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    doc = await db.project_documents.find_one({"_id": doc_id, "project_id": oid, "org_id": org_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    try:
        settings = get_settings(request)
        s3_client = get_s3(request)
        async with s3_client.get_client() as client:
            await client.delete_object(Bucket=settings.minio_default_bucket, Key=doc["s3_key"])
    except Exception:
        pass

    await db.project_documents.delete_one({"_id": doc_id})
    logger.info("project.document_deleted", project_id=project_id, doc_id=doc_id)
    return {"deleted": True}


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> None:
    """Hard delete a project. Fails if it still has non-archived test cases."""
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    doc = await db.projects.find_one({"_id": oid, "org_id": org_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    tc_count = await db.test_cases.count_documents({
        "org_id": org_id,
        "project_id": oid,
        "status": {"$ne": "archived"},
    })
    if tc_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete project with {tc_count} active test case(s). Delete them first.",
        )

    await db.projects.delete_one({"_id": oid, "org_id": org_id})
    logger.info("project.deleted", project_id=project_id, org_id=str(org_id))


# ── 9B.2 Test prioritization ──────────────────────────────────────────────────

@router.get("/{project_id}/prioritized-tests")
async def get_prioritized_tests(
    project_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    n: int = Query(default=20, ge=1, le=200),
) -> dict:
    """Return test cases ranked by risk score (highest risk first).

    risk = recency_weight * (1 - pass_rate) * (1 + is_critical)
    """
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    project_doc = await db.projects.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    import asyncio
    from app.intelligence.test_prioritizer import get_prioritized_tests as _prioritize
    from app.clients import sync_mongo as _sync_mongo
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(
        None,
        _prioritize,
        _sync_mongo.db,
        str(org_id),
        project_id,
        n,
    )

    return {"project_id": project_id, "n": n, "tests": results}


# ── 9B.1 Deployment event ingestion ──────────────────────────────────────────

class DeploymentEventRequest(BaseModel):
    event_type: str = Field(
        ...,
        pattern="^(deployment_completed|incident_opened|incident_resolved)$",
    )
    deployment_id: str = ""
    service: str = ""
    deployed_at: str | None = None   # ISO-8601; defaults to now


@router.post("/{project_id}/deployment-event", status_code=status.HTTP_202_ACCEPTED)
async def ingest_deployment_event(
    project_id: str,
    body: DeploymentEventRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """Record a deployment or incident event and schedule outcome inference.

    Outcome inference rules (run asynchronously):
    - deployment_completed + no incident within 48 h  → production_passed
    - incident_opened within 48 h of latest deployment → production_failed
    """
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    project_doc = await db.projects.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    event_ts = now
    if body.deployed_at:
        try:
            event_ts = datetime.fromisoformat(body.deployed_at.replace("Z", "+00:00"))
        except ValueError:
            pass

    event_doc = {
        "org_id": org_id,
        "project_id": oid,
        "event_type": body.event_type,
        "deployment_id": body.deployment_id,
        "service": body.service,
        "event_at": event_ts,
        "recorded_at": now,
        "processed": False,
    }
    result = await db.deployment_events.insert_one(event_doc)

    # Queue background inference
    try:
        from app.worker.tasks.infer_outcomes import infer_outcomes_for_project
        infer_outcomes_for_project.delay(project_id=project_id, org_id=str(org_id))
    except Exception as e:
        logger.warning("deployment_event.dispatch_failed", error=str(e)[:200])

    logger.info(
        "deployment_event.ingested",
        project_id=project_id,
        event_type=body.event_type,
        event_id=str(result.inserted_id),
    )
    return {"accepted": True, "event_id": str(result.inserted_id)}
