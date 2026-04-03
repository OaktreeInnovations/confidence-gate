from datetime import datetime, timezone
from enum import StrEnum

from bson import ObjectId
from pydantic import BaseModel, Field


class TestType(StrEnum):
    UI = "ui"
    API = "api"


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class TestCasePriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TestCaseStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class ApiStepConfig(BaseModel):
    method: HttpMethod = HttpMethod.GET
    endpoint: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    expected_status_code: int | None = None
    expected_response: str = ""
    extract_vars: dict[str, str] = Field(default_factory=dict)


class TestStep(BaseModel):
    step_number: int
    action: str = ""
    expected: str = ""
    custom_code: str = ""
    api_config: ApiStepConfig | None = None


class TestCase(BaseModel):
    id: ObjectId | None = Field(default=None, alias="_id")
    org_id: ObjectId
    project_id: ObjectId | None = None
    test_type: TestType = TestType.UI
    base_url: str = ""
    title: str
    description: str = ""
    prerequisites: str = ""
    steps: list[TestStep] = Field(default_factory=list)
    priority: TestCasePriority = TestCasePriority.MEDIUM
    status: TestCaseStatus = TestCaseStatus.DRAFT
    tags: list[str] = Field(default_factory=list)
    test_data: dict[str, str] = Field(default_factory=dict)
    # 1.3 Critical/Informational designation
    is_critical: bool = False       # failure always blocks release regardless of score
    is_informational: bool = False  # failure is a warning only; excluded from score
    # 2.3 Versioning — incremented whenever steps change
    version: int = 1
    created_by: ObjectId
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str},
    }
