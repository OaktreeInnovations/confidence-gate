from app.worker.celery_app import celery_app


@celery_app.task(name="cg.ping", bind=True)
def ping(self) -> dict:
    return {
        "status": "pong",
        "worker": self.request.hostname,
        "task_id": self.request.id,
    }
