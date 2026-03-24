"""Singleton synchronous MongoDB client for Celery workers.

Workers run in a forked process with a single-threaded event loop.
Each worker process gets exactly one MongoClient, reused across all
tasks. Connection pool is small (default 5) since workers process
one task at a time.

Lifecycle:
    worker_process_init  → connect()  (Celery signal)
    worker_process_shutdown → close() (Celery signal)

If the singleton is accessed before connect(), it raises RuntimeError.
If the connection drops mid-task, PyMongo's built-in auto-reconnect
handles recovery transparently.
"""

from __future__ import annotations

import threading

from pymongo import MongoClient
from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)

_lock = threading.Lock()
_client: MongoClient | None = None
_database_name: str = ""


def connect(
    dsn: str,
    database: str,
    max_pool_size: int = 5,
    server_selection_timeout_ms: int = 5000,
    connect_timeout_ms: int = 5000,
    socket_timeout_ms: int = 30000,
) -> None:
    """Initialize the singleton MongoClient. Call once per worker process."""
    global _client, _database_name

    with _lock:
        if _client is not None:
            logger.debug("sync_mongo.already_connected")
            return

        logger.info(
            "sync_mongo.connecting",
            host=dsn.split("@")[-1] if "@" in dsn else dsn,
            max_pool=max_pool_size,
        )
        _client = MongoClient(
            dsn,
            maxPoolSize=max_pool_size,
            minPoolSize=1,
            serverSelectionTimeoutMS=server_selection_timeout_ms,
            connectTimeoutMS=connect_timeout_ms,
            socketTimeoutMS=socket_timeout_ms,
            # Reconnect automatically on topology changes
            retryWrites=True,
            retryReads=True,
        )
        _database_name = database

        # Verify connectivity at startup
        _client.admin.command("ping")
        logger.info("sync_mongo.connected")


def close() -> None:
    """Close the singleton MongoClient. Call once on worker shutdown."""
    global _client

    with _lock:
        if _client is not None:
            _client.close()
            _client = None
            logger.info("sync_mongo.disconnected")


def get_db() -> Database:
    """Return the shared Database handle. Raises if not connected."""
    if _client is None:
        raise RuntimeError(
            "Sync MongoClient is not connected. "
            "Worker process init must call sync_mongo.connect() first."
        )
    return _client[_database_name]


def ping() -> bool:
    """Health check. Returns True if the server responds."""
    if _client is None:
        return False
    try:
        _client.admin.command("ping")
        return True
    except Exception:
        return False
