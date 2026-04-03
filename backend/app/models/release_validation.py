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
    # 3.5 Approval workflow
    AWAITING_APPROVAL = "awaiting_approval"
    # 9B.4 Explicit approved/rejected terminal states
    APPROVED = "approved"
    REJECTED = "rejected"


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

    # 1.4 Score freeze — snapshotted at decision time, never recomputed
    final_score_at_decision: int | None = None
    decision_at: datetime | None = None
    score_frozen: bool = False

    # 1.2 Override audit trail
    override_by: ObjectId | None = None
    override_reason: str | None = None
    override_at: datetime | None = None
    override_type: str | None = None  # "ship_anyway" | "acknowledge_risk"
    original_decision: str | None = None

    # 2.4 Production outcome integration
    outcome: str | None = None  # "production_passed" | "production_failed" | "rolled_back"
    outcome_recorded_at: datetime | None = None
    outcome_notes: str | None = None

    # 2.5 Validation SLA — set at creation; 0 means no limit
    sla_minutes: int = 0
    sla_breached: bool = False
    sla_breached_at: datetime | None = None

    # 3.1 Change-aware test selection metadata
    changed_areas: list[str] = Field(default_factory=list)
    targeted_selection: bool = False  # True when test cases were filtered by changed_areas

    # Task-level error (set when status = "failed")
    error_message: str | None = None

    # 2.3 Prediction engine validation
    predicted_score_pre_run: int | None = None   # predicted score captured at validation creation
    prediction_accuracy: int | None = None        # actual - predicted (positive = better than expected)

    # 3.3 Webhook delivery tracking
    webhook_delivery_status: str | None = None   # "pending" | "delivered" | "failed"
    webhook_last_attempt_at: datetime | None = None

    # 4.3 Outcome recording nudge
    outcome_reminder_sent_at: datetime | None = None

    # 5.1 AI adjustment audit log
    ai_adjustment_detail: dict | None = None

    # 7C.2 Incident signal attribution (only set when outcome=production_failed)
    incident_signal_attribution: list[dict] | None = None

    # 8.1 Score degradation — set when one or more scoring sub-engines failed
    score_degraded: bool = False
    degraded_engines: list[str] = Field(default_factory=list)

    # 3.5 Approval workflow
    approval_required: bool = False
    approved_by: ObjectId | None = None
    approved_at: datetime | None = None
    rejected_by: ObjectId | None = None
    rejected_at: datetime | None = None
    rejection_reason: str | None = None

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str},
    }
