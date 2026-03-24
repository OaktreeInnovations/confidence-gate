from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth.dependencies import get_current_user
from app.models import User, UserRole


class UserResponse(BaseModel):
    id: str
    firebase_uid: str
    email: str
    display_name: str | None
    role: UserRole
    org_id: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_user(cls, user: User) -> "UserResponse":
        return cls(
            id=str(user.id) if user.id else "",
            firebase_uid=user.firebase_uid,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            org_id=str(user.org_id) if user.org_id else None,
            created_at=user.created_at.isoformat(),
            updated_at=user.updated_at.isoformat(),
        )


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/me", response_model=UserResponse)
async def get_me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserResponse:
    return UserResponse.from_user(current_user)
