from datetime import datetime, timezone
from enum import StrEnum

from bson import ObjectId
from pydantic import BaseModel, Field


class TestRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class StepResultStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class StepResult(BaseModel):
    step_number: int
    status: StepResultStatus = StepResultStatus.SKIPPED
    action: str = ""
    expected: str = ""
    actual: str = ""
    error_message: str = ""
    evidence_url: str = ""
    generated_code: str = ""
    duration_ms: int = 0
    verification_mode: str = ""  # "ai", "dom-only", or "skipped"
    retry_count: int = 0
    ai_heal_attempts: int = 0


class TestRun(BaseModel):
    id: ObjectId | None = Field(default=None, alias="_id")
    org_id: ObjectId
    test_case_id: ObjectId
    test_case_title: str
    test_type: str = "ui"
    status: TestRunStatus = TestRunStatus.QUEUED
    triggered_by: ObjectId
    project_name: str = ""
    batch_name: str = ""
    batch_id: str = ""
    celery_task_id: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int = 0
    total_steps: int = 0
    passed_steps: int = 0
    failed_steps: int = 0
    error_message: str = ""
    run_until_step: int = 0  # 0 = run all steps; N = run steps 1..N only
    intent_overrides: dict[str, str] = Field(default_factory=dict)  # step_number (str) → intent JSON
    results: list[StepResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str},
    }
