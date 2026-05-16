"""9B.1 Automated outcome inference from deployment/incident signals.

Inference rules:
- deployment_completed + no incident_opened within 48 h  → production_passed
- incident_opened within 48 h of the most recent deployment → production_failed

The task writes outcome onto the most recent completed release_validation for the
project that has no outcome yet. It marks the deployment_events documents as
processed so they are not re-evaluated on the next run.

This task is triggered on every deployment event ingestion AND runs on a 4-hour
Beat schedule to catch any events that missed the immediate dispatch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from app.clients.sync_mongo import get_db as _get_sync_db
from app.worker.celery_app import celery_app

logger = structlog.get_logger(__name__)

_INCIDENT_WINDOW_H = 48   # hours after deployment to watch for incidents
_PASS_SETTLE_H = 48       # hours to wait before recording production_passed


@celery_app.task(name="cg.infer_outcomes_for_project", ignore_result=True)
def infer_outcomes_for_project(project_id: str, org_id: str) -> None:
    """Infer outcome for a single project from its unprocessed deployment events."""
    from bson import ObjectId
    db = _get_sync_db()

    try:
        proj_oid = ObjectId(project_id)
        org_oid = ObjectId(org_id)
    except Exception:
        logger.warning("infer_outcomes.invalid_ids", project_id=project_id, org_id=org_id)
        return

    _run_inference(db, proj_oid, org_oid)


@celery_app.task(name="cg.infer_outcomes_all", ignore_result=True)
def infer_outcomes_all() -> None:
    """4-hour Beat task: run inference across all projects with unprocessed events."""
    db = _get_sync_db()
    now = datetime.now(timezone.utc)

    # Find projects with unprocessed deployment events
    pipeline = [
        {"$match": {"processed": False, "recorded_at": {"$lte": now}}},
        {"$group": {"_id": {"project_id": "$project_id", "org_id": "$org_id"}}},
    ]
    groups = list(db.deployment_events.aggregate(pipeline))
    if not groups:
        return

    logger.info("infer_outcomes_all.start", project_count=len(groups))
    for g in groups:
        proj_oid = g["_id"]["project_id"]
        org_oid = g["_id"]["org_id"]
        try:
            _run_inference(db, proj_oid, org_oid)
        except Exception as e:
            logger.warning(
                "infer_outcomes_all.project_error",
                project_id=str(proj_oid),
                error=str(e)[:200],
            )


def _run_inference(db, proj_oid, org_oid) -> None:
    """Core inference logic for one project."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=_INCIDENT_WINDOW_H)

    # Fetch unprocessed events within the inference window
    events = list(db.deployment_events.find(
        {
            "project_id": proj_oid,
            "org_id": org_oid,
            "processed": False,
            "event_at": {"$gte": window_start},
        },
        sort=[("event_at", 1)],
    ))

    if not events:
        return

    deployments = [e for e in events if e["event_type"] == "deployment_completed"]
    incidents = [e for e in events if e["event_type"] == "incident_opened"]

    if not deployments:
        return

    # Find the most recent completed validation with no outcome
    validation = db.release_validations.find_one(
        {
            "project_id": proj_oid,
            "org_id": org_oid,
            "status": "completed",
            "outcome": None,
        },
        sort=[("completed_at", -1)],
    )
    if not validation:
        return

    latest_deploy = max(deployments, key=lambda e: e["event_at"])
    deploy_ts = latest_deploy["event_at"].replace(tzinfo=timezone.utc) if latest_deploy["event_at"].tzinfo is None else latest_deploy["event_at"]

    # Check for incidents within the window after latest deployment
    incidents_after_deploy = [
        i for i in incidents
        if (i["event_at"].replace(tzinfo=timezone.utc) if i["event_at"].tzinfo is None else i["event_at"]) >= deploy_ts
        and (i["event_at"].replace(tzinfo=timezone.utc) if i["event_at"].tzinfo is None else i["event_at"]) <= deploy_ts + timedelta(hours=_INCIDENT_WINDOW_H)
    ]

    inferred_outcome: str | None = None

    if incidents_after_deploy:
        inferred_outcome = "production_failed"
    elif (now - deploy_ts).total_seconds() >= _PASS_SETTLE_H * 3600:
        # 48 hours passed since deployment with no incidents → production_passed
        inferred_outcome = "production_passed"

    if inferred_outcome:
        db.release_validations.update_one(
            {"_id": validation["_id"]},
            {"$set": {
                "outcome": inferred_outcome,
                "outcome_recorded_at": now,
                "outcome_notes": f"Auto-inferred from deployment signal (deployment_id={latest_deploy.get('deployment_id', '')})",
                "updated_at": now,
            }},
        )
        logger.info(
            "infer_outcomes.recorded",
            validation_id=str(validation["_id"]),
            outcome=inferred_outcome,
            project_id=str(proj_oid),
        )

    # Mark all events as processed
    event_ids = [e["_id"] for e in events]
    db.deployment_events.update_many(
        {"_id": {"$in": event_ids}},
        {"$set": {"processed": True}},
    )
