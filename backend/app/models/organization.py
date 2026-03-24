from datetime import datetime, timezone

from bson import ObjectId
from pydantic import BaseModel, Field


class Organization(BaseModel):
    id: ObjectId | None = Field(default=None, alias="_id")
    name: str
    slug: str
    created_by: ObjectId | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str},
    }
