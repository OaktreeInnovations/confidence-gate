import asyncio
from functools import partial

import firebase_admin
from firebase_admin import auth as firebase_auth
import structlog

logger = structlog.get_logger(__name__)


class FirebaseClient:
    def __init__(self, project_id: str):
        self._project_id = project_id
        self._app: firebase_admin.App | None = None

    def connect(self) -> None:
        self._app = firebase_admin.initialize_app(
            None,
            options={"projectId": self._project_id},
        )
        logger.info("firebase.initialized", project_id=self._project_id)

    def close(self) -> None:
        if self._app:
            firebase_admin.delete_app(self._app)
            logger.info("firebase.closed")

    async def verify_id_token(self, id_token: str) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            partial(
                firebase_auth.verify_id_token,
                id_token,
                app=self._app,
                check_revoked=False,
            ),
        )

    async def ping(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                partial(firebase_auth.list_users, max_results=1, app=self._app),
            )
            return True
        except Exception:
            logger.warning("firebase.ping_failed", exc_info=True)
            return False
