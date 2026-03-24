from contextlib import asynccontextmanager
from typing import AsyncGenerator

from aiobotocore.session import AioSession
from aiobotocore.client import AioBaseClient
import structlog

logger = structlog.get_logger(__name__)


class S3Client:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, use_ssl: bool = False):
        self._endpoint = f"{'https' if use_ssl else 'http'}://{endpoint}"
        self._access_key = access_key
        self._secret_key = secret_key
        self._session = AioSession()

    @asynccontextmanager
    async def get_client(self) -> AsyncGenerator[AioBaseClient, None]:
        async with self._session.create_client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name="us-east-1",
        ) as client:
            yield client

    async def ping(self) -> bool:
        try:
            async with self.get_client() as client:
                await client.list_buckets()
            return True
        except Exception:
            logger.warning("s3.ping_failed", exc_info=True)
            return False
