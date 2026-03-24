from datetime import datetime, timezone
from enum import StrEnum

from bson import ObjectId
from pydantic import BaseModel, Field


class ReleaseValidationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPUTING = "computing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReleaseValidationContext(BaseModel):
    prd_text: str = ""
    notes: str = ""


class ReleaseValidation(BaseModel):
    id: ObjectId | None = Field(default=None, alias="_id")
    org_id: ObjectId
    project_id: ObjectId
    project_name: str = ""
    title: str = ""
    triggered_by: ObjectId
    status: ReleaseValidationStatus = ReleaseValidationStatus.PENDING
    batch_id: str = ""
    total_runs: int = 0
    completed_runs: int = 0
    passed_runs: int = 0
    failed_runs: int = 0
    error_runs: int = 0
    context: ReleaseValidationContext = Field(default_factory=ReleaseValidationContext)
    confidence_score: int | None = None
    confidence_grade: str | None = None
    recommendation: str | None = None
    report: dict = Field(default_factory=dict)
    score_version: str | None = None
    trend: str | None = None
    risk_delta: int | None = None
    historical_confidence: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str},
    }
