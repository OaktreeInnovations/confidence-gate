from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorDatabase
import structlog

from app.auth.base import AuthProvider
from app.dependencies import get_db
from app.models import User, UserRole

logger = structlog.get_logger(__name__)


async def get_firebase_token(request: Request) -> dict:
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    id_token = auth_header.removeprefix("Bearer ").strip()
    if not id_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    auth: AuthProvider = request.app.state.auth
    try:
        decoded_token = await auth.verify_id_token(id_token)
    except Exception as exc:
        logger.warning("auth.token_verification_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return decoded_token


async def get_current_user(
    decoded_token: Annotated[dict, Depends(get_firebase_token)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> User:
    firebase_uid: str = decoded_token["uid"]
    email: str = decoded_token.get("email", "")
    display_name: str | None = decoded_token.get("name")

    user_doc = await db.users.find_one({"firebase_uid": firebase_uid})

    if user_doc is not None:
        return User(**user_doc)

    logger.info("auth.auto_provisioning_user", firebase_uid=firebase_uid, email=email)

    new_user = User(
        firebase_uid=firebase_uid,
        email=email,
        display_name=display_name,
        role=UserRole.MEMBER,
    )

    result = await db.users.insert_one(
        new_user.model_dump(by_alias=True, exclude={"id"})
    )
    new_user.id = result.inserted_id

    logger.info("auth.user_provisioned", user_id=str(result.inserted_id), email=email)
    return new_user
