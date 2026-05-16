from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import structlog

from app.config import Settings
from app.logging import setup_logging
from app.clients import MongoClient, RedisClient, S3Client
from app.middleware import AccessLogMiddleware
from app.api.router import api_router

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings: Settings = app.state.settings

    mongo = MongoClient(
        dsn=settings.mongo_dsn,
        database=settings.mongo_db,
        max_pool_size=settings.mongo_max_pool_size,
        min_pool_size=settings.mongo_min_pool_size,
        server_selection_timeout_ms=settings.mongo_server_selection_timeout_ms,
        connect_timeout_ms=settings.mongo_connect_timeout_ms,
        socket_timeout_ms=settings.mongo_socket_timeout_ms,
    )
    redis = RedisClient(url=settings.redis_url)
    s3 = S3Client(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_root_user,
        secret_key=settings.minio_root_password,
        use_ssl=settings.minio_use_ssl,
    )

    if settings.auth_provider == "local":
        from app.auth.providers.local import LocalAuthProvider
        auth = LocalAuthProvider(secret=settings.local_auth_secret)
    else:
        from app.auth.providers.firebase import FirebaseAuthProvider
        auth = FirebaseAuthProvider(project_id=settings.firebase_project_id)
        auth.connect()

    await mongo.connect()
    await mongo.ensure_indexes()
    await redis.connect()
    logger.info("s3.session_ready", endpoint=settings.minio_endpoint)

    app.state.mongo = mongo
    app.state.redis = redis
    app.state.s3 = s3
    app.state.auth = auth

    logger.info("app.startup_complete", auth_provider=settings.auth_provider)
    yield

    if hasattr(auth, "close"):
        auth.close()
    await mongo.close()
    await redis.close()
    logger.info("app.shutdown_complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    setup_logging(log_level=settings.backend_log_level, log_format=settings.backend_log_format)

    app = FastAPI(
        title="Confidence Gate API",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AccessLogMiddleware)

    app.include_router(api_router)

    return app


app = create_app()
