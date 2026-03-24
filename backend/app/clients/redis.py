import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger(__name__)


class RedisClient:
    def __init__(self, url: str):
        self._url = url
        self._client: aioredis.Redis | None = None

    async def connect(self) -> None:
        logger.info("redis.connecting", url=self._url)
        self._client = aioredis.from_url(
            self._url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
        await self._client.ping()
        logger.info("redis.connected")

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            logger.info("redis.disconnected")

    @property
    def client(self) -> aioredis.Redis:
        if self._client is None:
            raise RuntimeError("RedisClient is not connected. Call connect() first.")
        return self._client

    async def ping(self) -> bool:
        try:
            return await self._client.ping()
        except Exception:
            return False
