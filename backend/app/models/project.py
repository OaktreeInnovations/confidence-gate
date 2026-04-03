from datetime import datetime, timezone

from bson import ObjectId
from pydantic import BaseModel, Field


class Project(BaseModel):
    id: ObjectId | None = Field(default=None, alias="_id")
    org_id: ObjectId
    name: str
    description: str = ""
    base_url: str = ""
    global_setup: str = ""
    # 1.5 Webhook notification
    webhook_url: str = ""
    # 3.1 Change-aware test selection — optional git integration
    git_repo_url: str = ""
    git_token: str = ""  # stored as-is; rotate via project settings
    # 3.5 Release approval workflow
    release_approvers: list[ObjectId] = Field(default_factory=list)
    # 8.3 Configurable gate thresholds (None = use system defaults)
    inconclusive_gate_pct: float | None = None   # default 0.15 (15%)
    behavior_override_pct: float | None = None   # default 0.20 (20%)
    created_by: ObjectId
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str},
    }
