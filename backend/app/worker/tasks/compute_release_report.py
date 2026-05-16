"""Celery task: compute the full release intelligence report for a validation."""

from __future__ import annotations

from bson import ObjectId
import structlog

from app.clients.sync_mongo import get_db as _get_sync_db
from app.config import Settings
from app.worker.celery_app import celery_app

settings = Settings()
logger = structlog.get_logger(__name__)


def _build_chat_provider(s: Settings):
    """Build the configured ChatProvider, or return None if not configured."""
    if s.ai_provider == "anthropic" and s.anthropic_api_key:
        from app.ai.providers.anthropic import AnthropicChatProvider
        return AnthropicChatProvider(api_key=s.anthropic_api_key, model=s.anthropic_model)
    if s.ai_provider == "ollama":
        from app.ai.providers.ollama import OllamaChatProvider
        return OllamaChatProvider(base_url=s.ollama_base_url, model=s.ollama_model)
    if s.openai_api_key:
        from app.ai.providers.openai import OpenAIChatProvider
        return OpenAIChatProvider(api_key=s.openai_api_key, model=s.openai_model)
    return None


@celery_app.task(
    name="cg.compute_release_report",
    bind=True,
    soft_time_limit=600,
    time_limit=720,
)
def compute_release_report_task(self, validation_id: str) -> dict:
    logger.info("release_report.start", validation_id=validation_id)

    db = _get_sync_db()
    val_oid = ObjectId(validation_id)

    val_doc = db.release_validations.find_one({"_id": val_oid})
    if not val_doc:
        logger.error("release_report.not_found", validation_id=validation_id)
        return {"status": "error", "message": "Validation not found"}

    try:
        from app.intelligence.final_score_engine import compute_final_score

        provider = _build_chat_provider(settings)

        report = compute_final_score(
            db=db,
            org_id=str(val_doc["org_id"]),
            batch_id=val_doc["batch_id"],
            project_id=str(val_doc["project_id"]),
            chat_provider=provider,
            context={
                "prd_text": val_doc.get("prd_text", ""),
                "notes": val_doc.get("notes", ""),
            },
            min_runs_for_decision=settings.min_runs_for_decision,
        )

        db.release_validations.update_one(
            {"_id": val_oid},
            {
                "$set": {
                    "status": "completed",
                    "report": report,
                    "confidence_score": report.get("confidence_score", 0),
                    "recommendation": report.get("recommendation", "unknown"),
                }
            },
        )

        logger.info(
            "release_report.complete",
            validation_id=validation_id,
            score=report.get("confidence_score"),
            recommendation=report.get("recommendation"),
        )
        return {"status": "ok", "score": report.get("confidence_score")}

    except Exception as e:
        logger.error("release_report.failed", validation_id=validation_id, error=str(e)[:500])
        db.release_validations.update_one(
            {"_id": val_oid},
            {"$set": {"status": "failed", "error": str(e)[:500]}},
        )
        return {"status": "error", "message": str(e)[:500]}
