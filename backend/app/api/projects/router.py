from datetime import datetime, timezone
from typing import Annotated

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
import structlog

from app.api.auth.dependencies import get_current_user
from app.dependencies import get_db
from app.models import Project, User

logger = structlog.get_logger(__name__)


# --- Request / Response schemas ---


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    base_url: str = ""
    global_setup: str = ""


class UpdateProjectRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    base_url: str | None = None
    global_setup: str | None = None


class ProjectResponse(BaseModel):
    id: str
    org_id: str
    name: str
    description: str
    base_url: str
    global_setup: str
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

    project = Project(
        org_id=org_id,
        name=body.name,
        description=body.description,
        base_url=body.base_url,
        global_setup=body.global_setup,
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
    """CI gate: check if project meets minimum confidence threshold."""
    org_id = require_org(current_user)
    oid = parse_object_id(project_id)

    project_doc = await db.projects.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not project_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    # Get test case IDs
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

    # Find latest computed_at
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
