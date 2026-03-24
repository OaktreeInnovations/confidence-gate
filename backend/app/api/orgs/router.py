import re
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
import structlog

from app.api.auth.dependencies import get_current_user
from app.dependencies import get_db
from app.models import Organization, User, UserRole

logger = structlog.get_logger(__name__)


class CreateOrgRequest(BaseModel):
    name: str
    slug: str


class OrgResponse(BaseModel):
    id: str
    name: str
    slug: str
    created_by: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_org(cls, org: Organization) -> "OrgResponse":
        return cls(
            id=str(org.id) if org.id else "",
            name=org.name,
            slug=org.slug,
            created_by=str(org.created_by) if org.created_by else None,
            created_at=org.created_at.isoformat(),
            updated_at=org.updated_at.isoformat(),
        )


router = APIRouter(prefix="/api/orgs", tags=["organizations"])


@router.post("", response_model=OrgResponse, status_code=status.HTTP_201_CREATED)
async def create_org(
    body: CreateOrgRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> OrgResponse:
    if not re.match(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$", body.slug):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Slug must be 3-40 lowercase alphanumeric characters or hyphens",
        )

    existing = await db.organizations.find_one({"slug": body.slug})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Organization slug '{body.slug}' already exists",
        )

    now = datetime.now(timezone.utc)
    org = Organization(
        name=body.name,
        slug=body.slug,
        created_by=current_user.id,
        created_at=now,
        updated_at=now,
    )

    result = await db.organizations.insert_one(
        org.model_dump(by_alias=True, exclude={"id"})
    )
    org.id = result.inserted_id

    await db.users.update_one(
        {"_id": current_user.id},
        {"$set": {"role": UserRole.OWNER, "org_id": org.id, "updated_at": now}},
    )

    logger.info(
        "org.created",
        org_id=str(org.id),
        slug=body.slug,
        owner_id=str(current_user.id),
    )
    return OrgResponse.from_org(org)


@router.get("/me", response_model=OrgResponse)
async def get_my_org(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> OrgResponse:
    if not current_user.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User does not belong to an organization",
        )

    org_doc = await db.organizations.find_one({"_id": current_user.org_id})
    if not org_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    return OrgResponse.from_org(Organization(**org_doc))
