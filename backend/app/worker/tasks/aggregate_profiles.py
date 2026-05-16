"""Celery task for computing execution profiles and selector memory.

Triggered after a test run completes. Aggregates recent runs for the
same test case into an ExecutionProfile and updates selector memory.
"""

from bson import ObjectId
import structlog

from app.clients.sync_mongo import get_db as _get_sync_db
from app.intelligence.aggregator import compute_execution_profile, store_execution_profile
from app.intelligence.code_reuse import update_code_cache
from app.intelligence.selector_memory import update_selector_memory
from app.worker.celery_app import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(
    name="cg.aggregate_execution_profile",
    bind=True,
    soft_time_limit=60,
    time_limit=90,
    ignore_result=True,
)
def aggregate_execution_profile(self, test_run_id: str) -> dict:
    """Compute and store the execution profile for a test run's test case.

    Looks up the test_run to find the test_case_id, then aggregates
    the last N runs for that test case into an ExecutionProfile.
    """
    db = _get_sync_db()

    try:
        run_doc = db.test_runs.find_one({"_id": ObjectId(test_run_id)})
        if not run_doc:
            logger.warning("aggregate.run_not_found", test_run_id=test_run_id)
            return {"status": "error", "message": "Test run not found"}

        test_case_id = str(run_doc["test_case_id"])
        org_id = str(run_doc["org_id"])

        profile = compute_execution_profile(
            db=db,
            test_case_id=test_case_id,
            org_id=org_id,
            window_size=20,
        )

        store_execution_profile(db, profile)

        # Update selector memory and code cache from this run's telemetry
        _update_selector_memory_from_run(db, test_run_id, test_case_id, org_id)
        _update_code_cache_from_run(db, test_run_id, test_case_id, org_id)

        # Trigger failure graph computation (non-fatal, feature-flagged)
        try:
            from app.worker.tasks.compute_failure_graph import compute_failure_graph_task
            compute_failure_graph_task.delay(org_id)
        except Exception as e:
            logger.warning("aggregate.failure_graph_trigger_failed", error=str(e)[:200])

        return {
            "status": "ok",
            "test_case_id": test_case_id,
            "total_runs": profile.total_runs,
            "pass_rate": profile.pass_rate,
        }

    except Exception as e:
        logger.error(
            "aggregate.failed",
            test_run_id=test_run_id,
            error=str(e)[:300],
        )
        return {"status": "error", "message": str(e)[:300]}


def _update_code_cache_from_run(
    db, test_run_id: str, test_case_id: str, org_id: str
) -> None:
    """Update step code cache from this run's results and telemetry."""
    run_doc = db.test_runs.find_one({"_id": ObjectId(test_run_id)})
    if not run_doc:
        return

    tel_doc = db.execution_telemetry.find_one({"test_run_id": test_run_id})
    tel_steps = {}
    if tel_doc:
        for ts in tel_doc.get("steps", []):
            tel_steps[ts.get("step_number", 0)] = ts

    for result in run_doc.get("results", []):
        sn = result.get("step_number", 0)
        if sn == 0:
            continue

        code = result.get("generated_code", "")
        if not code:
            continue

        # Detect code type: intent JSON vs Python code
        code_type = _detect_code_type(code)

        status = result.get("status", "")
        step_tel = tel_steps.get(sn, {})
        total_attempts = step_tel.get("total_attempts", 1)
        first_try_success = status == "passed" and total_attempts == 1
        used_selector_fallback = step_tel.get("used_selector_fallback", False)
        needed_retry = total_attempts > 1
        exec_ms = result.get("duration_ms", 0)

        try:
            update_code_cache(
                db=db,
                test_case_id=test_case_id,
                org_id=org_id,
                step_number=sn,
                action=result.get("action", ""),
                code=code,
                status=status,
                first_try_success=first_try_success,
                execution_ms=exec_ms,
                used_selector_fallback=used_selector_fallback,
                needed_retry=needed_retry,
                code_type=code_type,
            )
        except Exception as e:
            logger.warning(
                "aggregate.code_cache_failed",
                step=sn,
                error=str(e)[:200],
            )


def _detect_code_type(code: str) -> str:
    """Detect whether generated_code is intent JSON or Python code."""
    import json
    try:
        data = json.loads(code)
        if isinstance(data, dict) and "actions" in data:
            return "intent"
    except (json.JSONDecodeError, ValueError):
        pass
    return "python"


def _update_selector_memory_from_run(
    db, test_run_id: str, test_case_id: str, org_id: str
) -> None:
    """Extract selector data from telemetry and update selector memory."""
    tel_doc = db.execution_telemetry.find_one({"test_run_id": test_run_id})
    if not tel_doc:
        return

    for step_tel in tel_doc.get("steps", []):
        selectors_tried = step_tel.get("selectors_tried", [])
        if not selectors_tried:
            continue

        try:
            update_selector_memory(
                db=db,
                test_case_id=test_case_id,
                org_id=org_id,
                step_number=step_tel.get("step_number", 0),
                action=step_tel.get("action", ""),
                selectors_tried=selectors_tried,
                selector_that_worked=step_tel.get("selector_that_worked", ""),
                selectors_structured=step_tel.get("selectors_structured", []),
            )
        except Exception as e:
            logger.warning(
                "aggregate.selector_memory_failed",
                step=step_tel.get("step_number"),
                error=str(e)[:200],
            )
