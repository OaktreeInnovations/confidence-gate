"""Shared auth session per batch.

When test cases run as a batch, the first worker to claim the lock
runs the login steps, captures browser storage_state, and uploads it
to MinIO. Subsequent workers download the storage_state and inject it
into their browser context, skipping redundant login.

Storage: MinIO at '{org_id}/batch_sessions/{batch_id}/storage_state.json'
Lock:    Redis SETNX 'batch_auth:{batch_id}' with 300s TTL
"""

from __future__ import annotations

import json
import os
import tempfile
import time

import structlog

logger = structlog.get_logger(__name__)

# Keywords that identify login steps — same as _CRITICAL_KEYWORDS in execute_test_run.py
_LOGIN_KEYWORDS = (
    "login", "log in", "sign in", "signin", "authenticate",
    "enter credentials", "enter password", "enter username",
    "enter email", "click log in", "click sign in", "click login",
)

_LOCK_TTL_SECONDS = 300
_POLL_INTERVAL_SECONDS = 3
_MAX_POLL_SECONDS = 120


def _build_storage_state_key(org_id: str, batch_id: str) -> str:
    """MinIO key: '{org_id}/batch_sessions/{batch_id}/storage_state.json'"""
    return f"{org_id}/batch_sessions/{batch_id}/storage_state.json"


def is_login_test_case(steps: list[dict]) -> bool:
    """True if step 0 or 1 action matches login keywords.

    These test cases skip session injection (they ARE the login test).
    """
    for step in steps[:2]:
        action = step.get("action", "").lower()
        if any(kw in action for kw in _LOGIN_KEYWORDS):
            return True
    return False


def get_batch_storage_state(s3_client, bucket: str, org_id: str, batch_id: str) -> str | None:
    """Download storage_state JSON from MinIO to temp file.

    Returns local file path or None if not found.
    """
    key = _build_storage_state_key(org_id, batch_id)
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        data = response["Body"].read()
        # Write to temp file
        fd, path = tempfile.mkstemp(suffix=".json", prefix="batch_auth_")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        logger.info("batch_auth.downloaded", batch_id=batch_id, path=path)
        return path
    except s3_client.exceptions.NoSuchKey:
        return None
    except Exception as e:
        logger.warning("batch_auth.download_failed", batch_id=batch_id, error=str(e)[:200])
        return None


def _upload_storage_state(s3_client, bucket: str, org_id: str, batch_id: str, state_json: str) -> None:
    """Upload storage_state JSON to MinIO."""
    key = _build_storage_state_key(org_id, batch_id)
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=state_json.encode("utf-8"),
        ContentType="application/json",
    )
    logger.info("batch_auth.uploaded", batch_id=batch_id, key=key)


def _find_login_test_case(db, batch_id: str) -> dict | None:
    """Find a test case in the batch that has login steps."""
    # Find test runs in this batch
    runs = list(db.test_runs.find({"batch_id": batch_id}, {"test_case_id": 1}).limit(20))
    for run in runs:
        tc = db.test_cases.find_one({"_id": run["test_case_id"]})
        if tc:
            steps = tc.get("steps", [])
            if is_login_test_case(steps):
                return tc
    return None


def ensure_batch_auth_session(
    s3_client,
    bucket: str,
    org_id: str,
    batch_id: str,
    db,
    ai_provider,
    model: str,
    redis_client,
) -> None:
    """Lazy-create auth session with distributed lock.

    Uses Redis SETNX 'batch_auth:{batch_id}' (TTL 300s).
    First worker: finds a login-capable test case in the batch,
    launches Playwright, executes login steps, saves storage_state to MinIO.
    Other workers: poll MinIO every 3s for up to 120s.
    """
    lock_key = f"batch_auth:{batch_id}"

    # Check if storage state already exists
    key = _build_storage_state_key(org_id, batch_id)
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        logger.info("batch_auth.already_exists", batch_id=batch_id)
        return  # Already created
    except Exception:
        pass  # Not yet created, proceed

    # Try to acquire the lock
    acquired = redis_client.set(lock_key, "1", nx=True, ex=_LOCK_TTL_SECONDS)

    if acquired:
        # We are the leader — run login and upload storage state
        logger.info("batch_auth.leader", batch_id=batch_id)
        try:
            _run_login_and_upload(s3_client, bucket, org_id, batch_id, db, ai_provider, model)
        except Exception as e:
            logger.error("batch_auth.leader_failed", batch_id=batch_id, error=str(e)[:300])
            # Delete lock so another worker can try
            redis_client.delete(lock_key)
    else:
        # Another worker is the leader — poll for the storage state
        logger.info("batch_auth.waiting", batch_id=batch_id)
        deadline = time.monotonic() + _MAX_POLL_SECONDS
        while time.monotonic() < deadline:
            try:
                s3_client.head_object(Bucket=bucket, Key=key)
                logger.info("batch_auth.ready", batch_id=batch_id)
                return
            except Exception:
                time.sleep(_POLL_INTERVAL_SECONDS)
        logger.warning("batch_auth.timeout", batch_id=batch_id)


def _run_login_and_upload(s3_client, bucket, org_id, batch_id, db, ai_provider, model):
    """Find a login test case, execute its login steps, upload storage state."""
    from playwright.sync_api import sync_playwright

    tc = _find_login_test_case(db, batch_id)
    if not tc:
        logger.warning("batch_auth.no_login_test_case", batch_id=batch_id)
        return

    steps = tc.get("steps", [])
    # Only execute the first few steps (login steps)
    login_steps = []
    for step in steps[:5]:
        login_steps.append(step)
        action_lower = step.get("action", "").lower()
        # Stop after we've clicked a login/submit button
        if any(kw in action_lower for kw in ("click log in", "click sign in", "click login", "submit")):
            break

    if not login_steps:
        return

    test_case_id = str(tc["_id"])

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=True,
        )
        page = context.new_page()
        page.set_default_navigation_timeout(30000)
        page.set_default_timeout(15000)

        from app.worker.ai_executor import execute_step

        previous_actions: list[str] = []
        for step in login_steps:
            try:
                execute_step(
                    page=page,
                    ai_client=ai_provider,
                    step=step,
                    org_id=org_id,
                    test_run_id=f"batch_auth_{batch_id}",
                    s3_client=s3_client,
                    bucket=bucket,
                    previous_actions=previous_actions,
                    model=model,
                )
                previous_actions.append(step.get("action", ""))
            except Exception as e:
                logger.warning(
                    "batch_auth.step_failed",
                    batch_id=batch_id,
                    step=sn,
                    error=str(e)[:200],
                )
                break

        # Give the page a moment to settle after login
        page.wait_for_load_state("networkidle", timeout=5000)

        # Save storage state
        state = context.storage_state()
        state_json = json.dumps(state)
        _upload_storage_state(s3_client, bucket, org_id, batch_id, state_json)

        browser.close()
