from datetime import datetime, timezone
from typing import Annotated

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field, model_validator
from pymongo import ReturnDocument
import structlog

from app.api.auth.dependencies import get_current_user
from app.config import Settings
from app.dependencies import get_db, get_settings
from app.models import (
    TestCase,
    TestCasePriority,
    TestCaseStatus,
    TestStep,
    TestType,
    HttpMethod,
    ApiStepConfig,
    User,
)

logger = structlog.get_logger(__name__)


# --- Request / Response schemas ---


class ApiStepConfigRequest(BaseModel):
    method: HttpMethod = HttpMethod.GET
    endpoint: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    expected_status_code: int | None = None
    expected_response: str = ""
    extract_vars: dict[str, str] = Field(default_factory=dict)


class TestStepRequest(BaseModel):
    step_number: int
    action: str = ""
    expected: str = ""
    custom_code: str = ""
    api_config: ApiStepConfigRequest | None = None


class CreateTestCaseRequest(BaseModel):
    project_id: str
    test_type: TestType = TestType.UI
    base_url: str = ""
    title: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    prerequisites: str = ""
    steps: list[TestStepRequest] = Field(default_factory=list)
    priority: TestCasePriority = TestCasePriority.MEDIUM
    status: TestCaseStatus = TestCaseStatus.DRAFT
    tags: list[str] = Field(default_factory=list)
    test_data: dict[str, str] = Field(default_factory=dict)
    is_critical: bool = False
    is_informational: bool = False


class UpdateTestCaseRequest(BaseModel):
    test_type: TestType | None = None
    base_url: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    prerequisites: str | None = None
    steps: list[TestStepRequest] | None = None
    priority: TestCasePriority | None = None
    status: TestCaseStatus | None = None
    tags: list[str] | None = None
    test_data: dict[str, str] | None = None
    is_critical: bool | None = None
    is_informational: bool | None = None


class ApiStepConfigResponse(BaseModel):
    method: str
    endpoint: str
    headers: dict[str, str]
    body: str
    expected_status_code: int | None
    expected_response: str
    extract_vars: dict[str, str]


class TestStepResponse(BaseModel):
    step_number: int
    action: str
    expected: str
    custom_code: str
    api_config: ApiStepConfigResponse | None


class TestCaseResponse(BaseModel):
    id: str
    org_id: str
    project_id: str | None
    test_type: str
    base_url: str
    title: str
    description: str
    prerequisites: str
    steps: list[TestStepResponse]
    priority: TestCasePriority
    status: TestCaseStatus
    tags: list[str]
    test_data: dict[str, str]
    is_critical: bool = False
    is_informational: bool = False
    version: int = 1
    created_by: str
    created_at: str
    updated_at: str
    flake_badge: str | None = None
    quality_grade: str | None = None  # A/B/C/D from test quality score (3.2)

    @classmethod
    def from_test_case(cls, tc: TestCase) -> "TestCaseResponse":
        return cls(
            id=str(tc.id) if tc.id else "",
            org_id=str(tc.org_id),
            project_id=str(tc.project_id) if tc.project_id else None,
            test_type=tc.test_type,
            base_url=tc.base_url,
            title=tc.title,
            description=tc.description,
            prerequisites=tc.prerequisites,
            steps=[
                TestStepResponse(
                    step_number=s.step_number,
                    action=s.action,
                    expected=s.expected,
                    custom_code=s.custom_code,
                    api_config=ApiStepConfigResponse(
                        method=s.api_config.method,
                        endpoint=s.api_config.endpoint,
                        headers=s.api_config.headers,
                        body=s.api_config.body,
                        expected_status_code=s.api_config.expected_status_code,
                        expected_response=s.api_config.expected_response,
                        extract_vars=s.api_config.extract_vars,
                    ) if s.api_config else None,
                )
                for s in tc.steps
            ],
            priority=tc.priority,
            status=tc.status,
            tags=tc.tags,
            test_data=tc.test_data,
            is_critical=tc.is_critical,
            is_informational=tc.is_informational,
            version=tc.version,
            created_by=str(tc.created_by),
            created_at=tc.created_at.isoformat(),
            updated_at=tc.updated_at.isoformat(),
        )


class TestCaseListResponse(BaseModel):
    items: list[TestCaseResponse]
    total: int
    page: int
    page_size: int


class GenerateTestCasesRequest(BaseModel):
    project_id: str
    user_story: str = Field(default="", max_length=10_000)
    prd: str = Field(default="", max_length=100_000)
    srs: str = Field(default="", max_length=100_000)
    existing_test_cases: str = ""
    screenshots: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def at_least_one_input(self) -> "GenerateTestCasesRequest":
        has_input = (
            self.user_story.strip()
            or self.prd.strip()
            or self.srs.strip()
            or self.existing_test_cases.strip()
            or self.screenshots
        )
        if not has_input:
            raise ValueError(
                "At least one input is required (user story, PRD, SRS, existing test cases, or screenshots)"
            )
        return self


class GeneratedStepResponse(BaseModel):
    step_number: int
    action: str
    expected: str
    api_config: ApiStepConfigResponse | None = None


class GeneratedTestCaseResponse(BaseModel):
    title: str
    description: str
    test_type: str
    prerequisites: str
    steps: list[GeneratedStepResponse]
    priority: str
    tags: list[str]


class GenerateTestCasesResponse(BaseModel):
    test_cases: list[GeneratedTestCaseResponse]
    warnings: list[str] = Field(default_factory=list)


class EnhanceStepsRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    prerequisites: str = ""
    steps: list[TestStepRequest] = Field(..., min_length=1)
    test_data_keys: list[str] = Field(default_factory=list)


class EnhancedStepResponse(BaseModel):
    step_number: int
    action: str
    expected: str


class EnhanceStepsResponse(BaseModel):
    steps: list[EnhancedStepResponse]
    warnings: list[str] = Field(default_factory=list)


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


def _compute_flake_badge(flake_score: float) -> str:
    """Derive a flake badge level from the overall flake score."""
    if flake_score > 0.35:
        return "high_flake_risk"
    if flake_score > 0.15:
        return "sometimes_flaky"
    return "stable"


async def _get_flake_badge(db: AsyncIOMotorDatabase, test_case_id: str, org_id: str) -> str | None:
    """Look up execution profile and return flake badge for a single test case."""
    profile = await db.execution_profiles.find_one(
        {"test_case_id": test_case_id, "org_id": org_id},
        {"flake_report.overall_flake_score": 1},
    )
    if not profile:
        return None
    score = profile.get("flake_report", {}).get("overall_flake_score", 0)
    return _compute_flake_badge(score)


# --- Router ---

router = APIRouter(prefix="/api/test-cases", tags=["test-cases"])


@router.post("", response_model=TestCaseResponse, status_code=status.HTTP_201_CREATED)
async def create_test_case(
    body: CreateTestCaseRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> TestCaseResponse:
    org_id = require_org(current_user)
    project_oid = parse_object_id(body.project_id)

    project_doc = await db.projects.find_one({"_id": project_oid, "org_id": org_id})
    if not project_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    now = datetime.now(timezone.utc)

    tc = TestCase(
        org_id=org_id,
        project_id=project_oid,
        test_type=body.test_type,
        base_url=body.base_url,
        title=body.title,
        description=body.description,
        prerequisites=body.prerequisites,
        steps=[TestStep(**s.model_dump()) for s in body.steps],
        priority=body.priority,
        status=body.status,
        tags=body.tags,
        test_data=body.test_data,
        is_critical=body.is_critical,
        is_informational=body.is_informational,
        created_by=current_user.id,
        created_at=now,
        updated_at=now,
    )

    result = await db.test_cases.insert_one(
        tc.model_dump(by_alias=True, exclude={"id"})
    )
    tc.id = result.inserted_id

    logger.info("test_case.created", test_case_id=str(tc.id), org_id=str(org_id))
    return TestCaseResponse.from_test_case(tc)


@router.post("/generate", response_model=GenerateTestCasesResponse)
async def generate_test_cases(
    body: GenerateTestCasesRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GenerateTestCasesResponse:
    """Generate test case drafts from a user story using AI."""
    import asyncio
    from functools import partial

    from openai import APIError, APITimeoutError, RateLimitError

    from app.api.test_cases.ai_generator import generate_test_cases_from_story
    from app.models import Project

    org_id = require_org(current_user)
    project_oid = parse_object_id(body.project_id)

    project_doc = await db.projects.find_one({"_id": project_oid, "org_id": org_id})
    if not project_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    if not settings.openai_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OpenAI API key not configured",
        )

    project = Project(**project_doc)

    # Collect test_data keys from existing test cases in this project
    test_data_keys: list[str] = []
    sample_tc = await db.test_cases.find_one(
        {"project_id": project_oid, "org_id": org_id, "test_data": {"$ne": {}}},
        {"test_data": 1},
    )
    if sample_tc and isinstance(sample_tc.get("test_data"), dict):
        test_data_keys = list(sample_tc["test_data"].keys())

    # Enrich prd with extracted text from project documents
    enriched_prd = body.prd or ""
    try:
        doc_cursor = db.project_documents.find(
            {"project_id": project_oid, "org_id": org_id, "extracted_text": {"$ne": ""}},
            {"name": 1, "extracted_text": 1},
        ).limit(10)
        async for d in doc_cursor:
            text = (d.get("extracted_text") or "").strip()
            if text:
                enriched_prd += f"\n\n[Document: {d['name']}]\n{text[:8000]}"
    except Exception:
        pass

    loop = asyncio.get_running_loop()
    try:
        drafts, gen_warnings = await loop.run_in_executor(
            None,
            partial(
                generate_test_cases_from_story,
                api_key=settings.openai_api_key,
                user_story=body.user_story,
                project_name=project.name,
                base_url=project.base_url,
                global_setup=project.global_setup,
                project_description=project.description,
                test_data_keys=test_data_keys if test_data_keys else None,
                model=settings.openai_model,
                prd=enriched_prd,
                srs=body.srs,
                existing_test_cases=body.existing_test_cases,
                screenshots=body.screenshots if body.screenshots else None,
            ),
        )
    except RateLimitError:
        raise HTTPException(status_code=429, detail="AI rate limit exceeded — try again shortly")
    except APITimeoutError:
        raise HTTPException(status_code=504, detail="AI request timed out")
    except APIError as e:
        logger.error("generate.openai_error", error=str(e)[:300])
        raise HTTPException(status_code=503, detail="AI service temporarily unavailable")

    logger.info(
        "test_cases.generated",
        project_id=str(project_oid),
        org_id=str(org_id),
        count=len(drafts),
    )

    return GenerateTestCasesResponse(
        test_cases=[GeneratedTestCaseResponse(**tc) for tc in drafts],
        warnings=gen_warnings,
    )


@router.post("/enhance-steps", response_model=EnhanceStepsResponse)
async def enhance_steps(
    body: EnhanceStepsRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EnhanceStepsResponse:
    """Enhance test case steps using AI to make them automation-ready."""
    import asyncio
    from functools import partial

    from openai import APIError, APITimeoutError, RateLimitError

    from app.api.test_cases.ai_enhancer import enhance_test_case_steps

    require_org(current_user)

    if not settings.openai_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OpenAI API key not configured",
        )

    steps_dicts = [s.model_dump() for s in body.steps]

    loop = asyncio.get_running_loop()
    try:
        enhanced, warnings = await loop.run_in_executor(
            None,
            partial(
                enhance_test_case_steps,
                api_key=settings.openai_api_key,
                title=body.title,
                description=body.description,
                prerequisites=body.prerequisites,
                steps=steps_dicts,
                test_data_keys=body.test_data_keys if body.test_data_keys else None,
                model=settings.openai_model,
            ),
        )
    except RateLimitError:
        raise HTTPException(status_code=429, detail="AI rate limit exceeded — try again shortly")
    except APITimeoutError:
        raise HTTPException(status_code=504, detail="AI request timed out")
    except APIError as e:
        logger.error("enhance.openai_error", error=str(e)[:300])
        raise HTTPException(status_code=503, detail="AI service temporarily unavailable")

    logger.info("test_cases.enhanced", step_count=len(enhanced))

    return EnhanceStepsResponse(
        steps=[EnhancedStepResponse(**s) for s in enhanced],
        warnings=warnings,
    )


@router.get("", response_model=TestCaseListResponse)
async def list_test_cases(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    project_id: str | None = Query(default=None),
    status_filter: TestCaseStatus | None = Query(default=None, alias="status"),
    priority: TestCasePriority | None = Query(default=None),
    tag: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> TestCaseListResponse:
    org_id = require_org(current_user)

    query: dict = {"org_id": org_id}

    if project_id is not None:
        query["project_id"] = parse_object_id(project_id)

    if status_filter is not None:
        query["status"] = status_filter
    else:
        query["status"] = {"$ne": TestCaseStatus.ARCHIVED}

    if priority is not None:
        query["priority"] = priority

    if tag is not None:
        query["tags"] = tag

    if search:
        query["title"] = {"$regex": search, "$options": "i"}

    total = await db.test_cases.count_documents(query)

    skip = (page - 1) * page_size
    cursor = (
        db.test_cases.find(query)
        .sort("created_at", -1)
        .skip(skip)
        .limit(page_size)
    )
    docs = await cursor.to_list(length=page_size)

    items = [TestCaseResponse.from_test_case(TestCase(**doc)) for doc in docs]

    # Enrich with flake badges
    tc_ids = [str(doc["_id"]) for doc in docs]
    if tc_ids:
        profiles = await db.execution_profiles.find(
            {"test_case_id": {"$in": tc_ids}, "org_id": str(org_id)},
            {"test_case_id": 1, "flake_report.overall_flake_score": 1, "quality_grade": 1},
        ).to_list(length=len(tc_ids))
        profile_map = {p["test_case_id"]: p for p in profiles}
        for item in items:
            profile = profile_map.get(item.id)
            if profile:
                score = profile.get("flake_report", {}).get("overall_flake_score", 0)
                item.flake_badge = _compute_flake_badge(score)
                item.quality_grade = profile.get("quality_grade")

    return TestCaseListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{test_case_id}", response_model=TestCaseResponse)
async def get_test_case(
    test_case_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> TestCaseResponse:
    org_id = require_org(current_user)
    oid = parse_object_id(test_case_id)

    doc = await db.test_cases.find_one({"_id": oid, "org_id": org_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test case not found",
        )

    resp = TestCaseResponse.from_test_case(TestCase(**doc))
    resp.flake_badge = await _get_flake_badge(db, test_case_id, str(org_id))
    profile = await db.execution_profiles.find_one(
        {"test_case_id": test_case_id, "org_id": str(org_id)},
        {"quality_grade": 1},
    )
    if profile:
        resp.quality_grade = profile.get("quality_grade")
    return resp


@router.get("/{test_case_id}/execution-health")
async def get_execution_health(
    test_case_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """Return the execution profile for a test case."""
    org_id = require_org(current_user)
    oid = parse_object_id(test_case_id)

    tc_doc = await db.test_cases.find_one({"_id": oid, "org_id": org_id})
    if not tc_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test case not found",
        )

    profile_doc = await db.execution_profiles.find_one(
        {"test_case_id": test_case_id, "org_id": str(org_id)},
    )
    if not profile_doc:
        return {}

    profile_doc["_id"] = str(profile_doc["_id"])
    return profile_doc


@router.get("/{test_case_id}/execution-profile")
async def get_execution_profile(
    test_case_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """Return the full execution profile for a test case."""
    org_id = require_org(current_user)
    oid = parse_object_id(test_case_id)

    tc_doc = await db.test_cases.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not tc_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Test case not found")

    profile_doc = await db.execution_profiles.find_one(
        {"test_case_id": test_case_id, "org_id": str(org_id)},
    )
    if not profile_doc:
        return {}

    profile_doc["_id"] = str(profile_doc["_id"])
    return profile_doc


@router.get("/{test_case_id}/flake-report")
async def get_flake_report(
    test_case_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    """Return the flake analysis for a test case."""
    org_id = require_org(current_user)
    oid = parse_object_id(test_case_id)

    tc_doc = await db.test_cases.find_one({"_id": oid, "org_id": org_id}, {"_id": 1})
    if not tc_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Test case not found")

    profile_doc = await db.execution_profiles.find_one(
        {"test_case_id": test_case_id, "org_id": str(org_id)},
        {"flake_report": 1},
    )
    if not profile_doc or "flake_report" not in profile_doc:
        return {}

    return profile_doc["flake_report"]


@router.put("/{test_case_id}", response_model=TestCaseResponse)
async def update_test_case(
    test_case_id: str,
    body: UpdateTestCaseRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> TestCaseResponse:
    org_id = require_org(current_user)
    oid = parse_object_id(test_case_id)

    update_data: dict = {}
    if body.test_type is not None:
        update_data["test_type"] = body.test_type
    if body.base_url is not None:
        update_data["base_url"] = body.base_url
    if body.title is not None:
        update_data["title"] = body.title
    if body.description is not None:
        update_data["description"] = body.description
    if body.prerequisites is not None:
        update_data["prerequisites"] = body.prerequisites
    if body.steps is not None:
        update_data["steps"] = [s.model_dump() for s in body.steps]
    if body.priority is not None:
        update_data["priority"] = body.priority
    if body.status is not None:
        update_data["status"] = body.status
    if body.tags is not None:
        update_data["tags"] = body.tags
    if body.test_data is not None:
        update_data["test_data"] = body.test_data
    if body.is_critical is not None:
        update_data["is_critical"] = body.is_critical
    if body.is_informational is not None:
        update_data["is_informational"] = body.is_informational

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update",
        )

    update_data["updated_at"] = datetime.now(timezone.utc)

    mongo_update: dict = {"$set": update_data}
    # 2.3 Increment version whenever steps change
    if body.steps is not None:
        mongo_update["$inc"] = {"version": 1}

    result = await db.test_cases.find_one_and_update(
        {"_id": oid, "org_id": org_id},
        mongo_update,
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test case not found",
        )

    logger.info("test_case.updated", test_case_id=test_case_id, org_id=str(org_id))
    return TestCaseResponse.from_test_case(TestCase(**result))


@router.delete("/{test_case_id}", response_model=TestCaseResponse)
async def delete_test_case(
    test_case_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> TestCaseResponse:
    """Soft delete: sets status to archived."""
    org_id = require_org(current_user)
    oid = parse_object_id(test_case_id)

    now = datetime.now(timezone.utc)
    result = await db.test_cases.find_one_and_update(
        {"_id": oid, "org_id": org_id},
        {"$set": {"status": TestCaseStatus.ARCHIVED, "updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test case not found",
        )

    logger.info("test_case.archived", test_case_id=test_case_id, org_id=str(org_id))
    return TestCaseResponse.from_test_case(TestCase(**result))


@router.delete("/{test_case_id}/hard", status_code=status.HTTP_204_NO_CONTENT)
async def hard_delete_test_case(
    test_case_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> None:
    """Permanently delete a test case and all associated test runs."""
    org_id = require_org(current_user)
    oid = parse_object_id(test_case_id)

    doc = await db.test_cases.find_one({"_id": oid, "org_id": org_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test case not found",
        )

    await db.test_runs.delete_many({"test_case_id": oid, "org_id": org_id})
    await db.test_cases.delete_one({"_id": oid, "org_id": org_id})

    logger.info("test_case.hard_deleted", test_case_id=test_case_id, org_id=str(org_id))
