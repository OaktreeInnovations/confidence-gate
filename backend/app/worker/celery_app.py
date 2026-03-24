from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown
import structlog

from app.config import Settings

settings = Settings()
logger = structlog.get_logger(__name__)

celery_app = Celery(
    "qualora",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_default_queue="qualora.default",
    worker_prefetch_multiplier=1,
    worker_concurrency=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_soft_time_limit=1800,
    task_time_limit=1920,
    include=[
        "app.worker.tasks.ping",
        "app.worker.tasks.execute_test_run",
        "app.worker.tasks.aggregate_profiles",
        "app.worker.tasks.compute_failure_graph",
        "app.worker.tasks.prune_evidence",
        "app.worker.tasks.generate_report",
        "app.worker.tasks.compute_release_report",
    ],
)

celery_app.conf.beat_schedule = {
    "prune-expired-evidence": {
        "task": "qualora.prune_expired_evidence",
        "schedule": 3600.0,  # every hour
    },
}


@worker_process_init.connect
def _on_worker_process_init(**kwargs) -> None:
    """Initialize shared clients when a worker process starts."""
    from app.clients import sync_mongo

    logger.info("worker.process_init")
    sync_mongo.connect(
        dsn=settings.mongo_dsn,
        database=settings.mongo_db,
        max_pool_size=settings.mongo_worker_pool_size,
        server_selection_timeout_ms=settings.mongo_server_selection_timeout_ms,
        connect_timeout_ms=settings.mongo_connect_timeout_ms,
        socket_timeout_ms=settings.mongo_socket_timeout_ms,
    )


@worker_process_shutdown.connect
def _on_worker_process_shutdown(**kwargs) -> None:
    """Clean up shared clients when a worker process shuts down."""
    from app.clients import sync_mongo

    logger.info("worker.process_shutdown")
    sync_mongo.close()
