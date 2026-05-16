import contextlib
import dataclasses
import enum
import os
import re as _re
from datetime import datetime, timezone
import time as _time

import boto3
from bson import ObjectId
from celery.exceptions import SoftTimeLimitExceeded
from openai import OpenAI
from playwright.sync_api import sync_playwright
import structlog

from app.clients.sync_mongo import get_db as _get_sync_db
from app.config import Settings
from app.telemetry import TelemetryCollector
from app.worker.ai_executor import execute_step, _capture_page_context
from app.worker.ai_provider import AICircuitBreaker, DeterministicFallbackProvider, OpenAIProvider
from app.worker.api_executor import execute_api_step
from app.worker.celery_app import celery_app
from app.worker.shared_context import SharedExecutionContext

settings = Settings()
logger = structlog.get_logger(__name__)


def _get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=f"http{'s' if settings.minio_use_ssl else ''}://{settings.minio_endpoint}",
        aws_access_key_id=settings.minio_root_user,
        aws_secret_access_key=settings.minio_root_password,
        region_name="us-east-1",
    )


def _build_ai_provider() -> AICircuitBreaker:
    """Build an AI provider with circuit breaker."""
    if settings.openai_api_key:
        client = OpenAI(api_key=settings.openai_api_key, timeout=120.0)
        primary = OpenAIProvider(client)
    else:
        primary = OpenAIProvider(None)
    return AICircuitBreaker(primary=primary, fallback=DeterministicFallbackProvider())


def _resolve_base_url(db, tc_doc: dict) -> str:
    """Resolve the base URL: test case overrides project."""
    base_url = tc_doc.get("base_url", "")
    if not base_url and tc_doc.get("project_id"):
        project_doc = db.projects.find_one({"_id": tc_doc["project_id"]})
        if project_doc:
            base_url = project_doc.get("base_url", "")
    return base_url


def _resolve_prerequisites(db, tc_doc: dict) -> str:
    """Merge project global_setup, project documents, and test case prerequisites."""
    prerequisites = tc_doc.get("prerequisites", "")
    if tc_doc.get("project_id"):
        project_doc = db.projects.find_one({"_id": tc_doc["project_id"]})
        if project_doc:
            global_setup = project_doc.get("global_setup", "")
            if global_setup:
                if prerequisites:
                    prerequisites = f"{global_setup}\n\n{prerequisites}"
                else:
                    prerequisites = global_setup

        # Inject extracted text from project documents as additional context
        try:
            from bson import ObjectId as _ObjId
            _pid = tc_doc["project_id"]
            _org_id = tc_doc.get("org_id")
            query: dict = {"project_id": _pid, "extracted_text": {"$ne": ""}}
            if _org_id:
                query["org_id"] = _org_id
            docs = list(db.project_documents.find(query, {"name": 1, "type": 1, "extracted_text": 1}).limit(10))
            if docs:
                doc_parts = []
                for d in docs:
                    text = (d.get("extracted_text") or "").strip()
                    if text:
                        doc_parts.append(f"[Document: {d['name']}]\n{text[:8000]}")
                if doc_parts:
                    docs_block = "\n\n---\n\n".join(doc_parts)
                    context_block = f"[Project Reference Documents]\n{docs_block}"
                    prerequisites = f"{prerequisites}\n\n{context_block}" if prerequisites else context_block
        except Exception:
            pass

    return prerequisites


def _make_bson_safe(obj):
    """Recursively convert non-BSON-safe objects (dataclasses, enums) to dicts/strings.

    Prevents InvalidDocument crashes when objects like RecoveryAction
    leak into data written to MongoDB.
    """
    if isinstance(obj, dict):
        return {k: _make_bson_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_bson_safe(item) for item in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _make_bson_safe(dataclasses.asdict(obj))
    if isinstance(obj, enum.Enum):
        return obj.value
    return obj


def _upload_execution_log(s3, bucket, org_id, test_run_id, tc_title, results, final_status, duration_ms, steps):
    """Upload execution log to MinIO."""
    log_lines = [f"Test Run: {test_run_id}", f"Test Case: {tc_title}", ""]
    for r in results:
        log_lines.append(
            f"Step {r['step_number']}: {r['status'].upper()} ({r['duration_ms']}ms)"
        )
        log_lines.append(f"  Action: {r['action']}")
        log_lines.append(f"  Expected: {r['expected']}")
        log_lines.append(f"  Actual: {r['actual']}")
        if r["error_message"]:
            log_lines.append(f"  Error: {r['error_message']}")
        log_lines.append("")

    log_lines.append(f"Result: {final_status.upper()}")
    log_lines.append(f"Duration: {duration_ms}ms")
    log_lines.append(f"Steps: {sum(1 for r in results if r['status'] == 'passed')} passed, {sum(1 for r in results if r['status'] != 'passed')} failed of {len(steps)} total")
    log_content = "\n".join(log_lines)

    log_key = f"{org_id}/{test_run_id}/execution.log"
    try:
        s3.put_object(
            Bucket=bucket,
            Key=log_key,
            Body=log_content.encode("utf-8"),
            ContentType="text/plain",
        )
    except Exception:
        pass  # Non-fatal


def _store_telemetry(db, collector: TelemetryCollector) -> None:
    """Store telemetry document in MongoDB. Non-fatal on failure."""
    try:
        doc = collector.finalize()
        db.execution_telemetry.insert_one(doc)
    except Exception as e:
        logger.warning("telemetry.store_failed", error=str(e))


def _is_api_step(step: dict) -> bool:
    """A step is API if it has a non-empty api_config dict."""
    api_config = step.get("api_config")
    return isinstance(api_config, dict) and bool(api_config)


# Keywords that identify critical steps (login, authentication, setup)
_CRITICAL_KEYWORDS = (
    "login", "log in", "sign in", "signin", "authenticate",
    "enter credentials", "enter password", "enter username",
    "enter email", "click log in", "click sign in", "click login",
)

# Keywords that identify navigation/setup steps (only critical at step 0-1)
_NAVIGATE_KEYWORDS = (
    "navigate to", "go to", "open the", "open url", "visit",
)


def _is_critical_step(action_lower: str, step_index: int) -> bool:
    """Check if a step is critical (failure should abort the run).

    A step is critical if:
    - It's in the first 3 steps AND its action matches login/auth keywords, OR
    - It's step 0 or 1 AND its action matches navigation keywords
      (initial page load failure means nothing else can work)
    """
    if step_index >= 3:
        return False
    if any(kw in action_lower for kw in _CRITICAL_KEYWORDS):
        return True
    if step_index <= 1 and any(kw in action_lower for kw in _NAVIGATE_KEYWORDS):
        return True
    return False


def _execute_test_run_unified(db, run_doc, tc_doc, test_run_id, ai_client, s3, bucket, steps, org_id, model: str = "gpt-5-mini"):
    """Execute a test run with per-step dispatch (UI via Playwright, API via HTTP)."""
    # Determine which step types are present
    has_ui_steps = any(not _is_api_step(s) for s in steps)
    has_api_steps = any(_is_api_step(s) for s in steps)

    if has_ui_steps and has_api_steps:
        effective_test_type = "mixed"
    elif has_api_steps:
        effective_test_type = "api"
    else:
        effective_test_type = "ui"

    test_data = dict(tc_doc.get("test_data", {}))
    # Resolve built-in template variables in test_data values.
    # Supported: ${timestamp} → UTC yymmdd HH:MM:SS, ${timestamp:FMT} → custom strftime
    _now = datetime.now(timezone.utc)
    _builtin_re = _re.compile(r'\$\{timestamp(?::([^}]+))?\}')
    for _k, _v in test_data.items():
        if isinstance(_v, str) and "${timestamp" in _v:
            test_data[_k] = _builtin_re.sub(
                lambda m: _now.strftime(m.group(1) or "%y%m%d %H:%M:%S"),
                _v,
            )
    base_url = _resolve_base_url(db, tc_doc) if has_api_steps else ""
    started_at = datetime.now(timezone.utc)
    test_case_id = str(tc_doc["_id"])

    collector = TelemetryCollector(test_run_id, org_id, effective_test_type)

    # Load execution intelligence for UI steps (non-fatal)
    step_selector_hints: dict[int, list[dict]] = {}
    step_intelligence: dict[int, str] = {}
    step_code_cache: dict[int, dict] = {}
    step_blacklisted_targets: dict[int, list[dict]] = {}  # 1.6
    run_strategy = None
    if has_ui_steps:
        try:
            from app.intelligence.selector_memory import get_known_good_selectors
            from app.intelligence.feedback import build_run_intelligence
            from app.intelligence.code_reuse import get_cached_code, get_all_step_cache
            for step in steps:
                if _is_api_step(step):
                    continue
                sn = step.get("step_number", 0)
                hints = get_known_good_selectors(db, test_case_id, org_id, sn)
                if hints:
                    step_selector_hints[sn] = hints
            step_intelligence = build_run_intelligence(db, test_case_id, org_id)
            # 1.6 Load selector blacklist per step
            for step in steps:
                if _is_api_step(step):
                    continue
                sn = step.get("step_number", 0)
                blacklisted = _load_selector_blacklist(db, test_case_id, org_id, sn)
                if blacklisted:
                    step_blacklisted_targets[sn] = blacklisted
            cache_type = "intent"
            all_cache = get_all_step_cache(db, test_case_id, org_id, code_type=cache_type)
            # Build step_number → action_text mapping for cache versioning
            step_actions = {s.get("step_number", 0): s.get("action", "") for s in steps}
            for sn, cache_doc in all_cache.items():
                cached = get_cached_code(
                    db, test_case_id, org_id, sn,
                    code_type=cache_type,
                    action_text=step_actions.get(sn, ""),
                )
                if cached:
                    step_code_cache[sn] = cached
        except Exception:
            pass  # Intelligence is optional — never blocks execution

        # Apply user-provided intent overrides (from manual edit & re-run)
        intent_overrides = run_doc.get("intent_overrides") or {}
        for step_str, intent_json in intent_overrides.items():
            step_code_cache[int(step_str)] = {
                "last_successful_code": intent_json,
                "code_type": "intent",
                "stability_score": 1.0,
                "code_hash": "",
            }

        # Compute adaptive execution strategy (non-fatal, feature-flagged)
        try:
            if settings.execution_strategy_enabled:
                from app.intelligence.strategy import compute_run_strategy
                profile_doc = db.execution_profiles.find_one({
                    "test_case_id": test_case_id,
                    "org_id": org_id,
                })
                run_strategy = compute_run_strategy(profile_doc, step_selector_hints)
                if run_strategy and run_strategy.has_any_overrides:
                    logger.info(
                        "execute.strategy_applied",
                        test_case_id=test_case_id,
                        steps_with_overrides=run_strategy.steps_with_overrides,
                    )
        except Exception as e:
            logger.warning("execute.strategy_failed", error=str(e)[:200])
            run_strategy = None

    results = []
    passed = 0
    failed = 0
    previous_actions = []
    shared_ctx = SharedExecutionContext()
    # Run-scoped adaptive memory — persists across steps within a single run
    from app.worker.in_run_memory import InRunSelectorMemory, RecoveryEffectivenessTracker
    selector_memory = InRunSelectorMemory()
    recovery_tracker = RecoveryEffectivenessTracker()
    last_step_selector: str | None = None  # Cross-step proximity context
    run_start = _time.monotonic()
    # 6.1 Route coverage — unique paths visited during this run
    routes_visited: set[str] = set()

    # Batch auth: pre-create shared auth session if this run belongs to a batch
    storage_state_path = None
    batch_id = run_doc.get("batch_id", "")
    if batch_id and has_ui_steps:
        try:
            from app.worker.batch_auth import (
                ensure_batch_auth_session, get_batch_storage_state, is_login_test_case,
            )
            from redis import Redis
            redis_client = Redis.from_url(Settings().redis_url, decode_responses=True)
            if not is_login_test_case(steps):
                ensure_batch_auth_session(
                    s3, bucket, org_id, batch_id, db, ai_client, model, redis_client,
                )
                storage_state_path = get_batch_storage_state(s3, bucket, org_id, batch_id)
        except Exception as e:
            logger.warning("execute.batch_auth_failed", error=str(e)[:200])

    # Launch Playwright only if there are UI steps
    pw_cm = sync_playwright() if has_ui_steps else contextlib.nullcontext()

    with pw_cm as pw:
        page = None
        browser = None
        context = None

        if has_ui_steps:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context_kwargs = {
                "viewport": {"width": 1280, "height": 720},
                "ignore_https_errors": True,
            }
            if storage_state_path:
                context_kwargs["storage_state"] = storage_state_path
            context = browser.new_context(**context_kwargs)
            # Clean up temp file
            if storage_state_path:
                try:
                    os.unlink(storage_state_path)
                except OSError:
                    pass
            page = context.new_page()
            page.set_default_navigation_timeout(30000)
            page.set_default_timeout(15000)
            collector.capture_environment(browser, page)

        timed_out = False
        try:
            for step_index, step in enumerate(steps):
                step_num = step.get("step_number", 0)

                # Write current_step BEFORE executing so frontend can show live progress
                db.test_runs.update_one(
                    {"_id": ObjectId(test_run_id)},
                    {"$set": {"current_step": step_num, "updated_at": datetime.now(timezone.utc)}},
                )

                if _is_api_step(step):
                    # --- API step ---
                    api_config = step.get("api_config") or {}
                    step_action = f"{api_config.get('method', 'GET')} {api_config.get('endpoint', '/')}"
                    tel = collector.begin_step(step_num, step_action)
                    t0 = _time.monotonic()

                    celery_remaining_s = 600 - (_time.monotonic() - run_start)
                    steps_remaining = len(steps) - step_index
                    per_step_s = max(min(celery_remaining_s / max(steps_remaining, 1), 60), 10)

                    result = execute_api_step(
                        ai_client=ai_client,
                        step=step,
                        base_url=base_url,
                        org_id=org_id,
                        test_run_id=test_run_id,
                        s3_client=s3,
                        bucket=bucket,
                        variables=shared_ctx.variables,
                        model=model,
                        step_timeout_s=per_step_s,
                        shared_context=shared_ctx,
                    )

                    api_ms = int((_time.monotonic() - t0) * 1000)
                    tel.record_phase("code_execution", api_ms)
                    tel.record_attempt(1, result["status"] == "passed", "", api_ms, result.get("error_message", ""))
                    collector.finish_step(tel, result["status"])

                else:
                    # --- UI step ---
                    step_custom_code = step.get("custom_code", "") or None
                    step_action = step.get("action", "")
                    tel = collector.begin_step(step_num, step_action)
                    step_strat = run_strategy.get_step(step_num) if run_strategy else None

                    # Inter-step stability: wait for page readiness between steps
                    # This handles slow post-navigation transitions (e.g., login → dashboard)
                    if step_index > 0 and page is not None:
                        try:
                            from app.worker.page_stability import wait_for_page_ready
                            wait_for_page_ready(page, timeout_ms=5000)
                        except Exception:
                            pass  # Best-effort; step will retry if page isn't ready

                    # 1.6 Merge blacklisted targets into intelligence context
                    _base_intelligence = step_intelligence.get(step_num) or ""
                    _blacklisted = step_blacklisted_targets.get(step_num, [])
                    if _blacklisted:
                        import json as _bj
                        _avoid_str = "; ".join(
                            _bj.dumps(t, separators=(",", ":")) for t in _blacklisted[:5]
                        )
                        _blacklist_note = f"\n[Selector blacklist — avoid these previously failed targets: {_avoid_str}]"
                        _base_intelligence = (_base_intelligence + _blacklist_note).strip()

                    result = execute_step(
                        page=page,
                        ai_client=ai_client,
                        step=step,
                        org_id=org_id,
                        test_run_id=test_run_id,
                        s3_client=s3,
                        bucket=bucket,
                        previous_actions=previous_actions,
                        test_data=test_data if test_data else None,
                        custom_code=step_custom_code,
                        model=model,
                        tel=tel,
                        selector_hints=step_selector_hints.get(step_num),
                        intelligence_context=_base_intelligence or None,
                        step_strategy=step_strat,
                        cached_code=step_code_cache.get(step_num),
                        execution_mode=settings.execution_mode,
                        shared_context=shared_ctx,
                        selector_memory=selector_memory,
                        recovery_tracker=recovery_tracker,
                        last_step_selector=last_step_selector,
                    )

                    collector.finish_step(tel, result["status"])
                    previous_actions.append(step.get("action", ""))
                    last_step_selector = result.get("last_selector_path")

                    # 6.1 Capture current URL for route coverage tracking
                    if page is not None:
                        try:
                            from urllib.parse import urlparse as _urlparse
                            _raw = page.url or ""
                            if _raw and not _raw.startswith("about:"):
                                _p = _urlparse(_raw)
                                _route = (_p.netloc + _p.path).rstrip("/") or _p.netloc
                                if _route:
                                    routes_visited.add(_route)
                        except Exception:
                            pass

                    # Sync browser cookies for subsequent API steps
                    if context is not None:
                        shared_ctx.sync_cookies_from_browser(context)

                # --- Common post-step ---
                results.append(result)

                if result["status"] == "passed":
                    passed += 1
                    # Save to code cache inline so it persists even if the run
                    # is later revoked/killed (async aggregation may never fire).
                    # Skip cache update when:
                    #   - behavior_override: heuristic pass, may not be correct
                    #   - cached intent reused: don't overwrite tuned cache with
                    #     AI-regenerated code from a retry or different path
                    _vmode = result.get("verification_mode", "")
                    _was_cached = result.get("was_cached", False)
                    if not _is_api_step(step) and _vmode != "behavior_override" and not _was_cached:
                        code = result.get("generated_code", "")
                        if code:
                            try:
                                from app.intelligence.code_reuse import update_code_cache
                                _code_type = "intent"
                                try:
                                    import json as _j
                                    _d = _j.loads(code)
                                    if not (isinstance(_d, dict) and "actions" in _d):
                                        _code_type = "python"
                                except Exception:
                                    _code_type = "python"
                                update_code_cache(
                                    db=db,
                                    test_case_id=test_case_id,
                                    org_id=org_id,
                                    step_number=step_num,
                                    action=step.get("action", ""),
                                    code=code,
                                    status="passed",
                                    first_try_success=True,
                                    execution_ms=result.get("duration_ms", 0),
                                    used_selector_fallback=False,
                                    needed_retry=False,
                                    code_type=_code_type,
                                )
                            except Exception:
                                pass  # Non-fatal
                elif result["status"] == "inconclusive":
                    # inconclusive steps don't count as passed or failed;
                    # they will cause the run to be marked failed at summary time.
                    # Cache the generated intent so the next run reuses it instead
                    # of re-generating via AI — the user may just need to update
                    # the expected result, not change the execution approach.
                    _vmode = result.get("verification_mode", "")
                    _was_cached = result.get("was_cached", False)
                    if not _is_api_step(step) and _vmode != "behavior_override" and not _was_cached:
                        code = result.get("generated_code", "")
                        if code:
                            try:
                                from app.intelligence.code_reuse import update_code_cache
                                _code_type = "intent"
                                try:
                                    import json as _j
                                    _d = _j.loads(code)
                                    if not (isinstance(_d, dict) and "actions" in _d):
                                        _code_type = "python"
                                except Exception:
                                    _code_type = "python"
                                update_code_cache(
                                    db=db,
                                    test_case_id=test_case_id,
                                    org_id=org_id,
                                    step_number=step_num,
                                    action=step.get("action", ""),
                                    code=code,
                                    status="passed",
                                    first_try_success=True,
                                    execution_ms=result.get("duration_ms", 0),
                                    used_selector_fallback=False,
                                    needed_retry=False,
                                    code_type=_code_type,
                                )
                            except Exception:
                                pass  # Non-fatal
                else:
                    failed += 1

                db.test_runs.update_one(
                    {"_id": ObjectId(test_run_id)},
                    {
                        "$set": {
                            "results": _make_bson_safe(results),
                            "passed_steps": passed,
                            "failed_steps": failed,
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                )

                # Critical step abort: if a login/auth step failed or errored,
                # abort the run — all subsequent steps would fail on wrong page
                if result["status"] in ("failed", "error"):
                    step_action_lower = (step.get("action") or "").lower()
                    if _is_critical_step(step_action_lower, step_index):
                        logger.warning(
                            "execute.critical_step_failed",
                            step=step_num,
                            action=step_action_lower[:80],
                            status=result["status"],
                        )
                        # Mark remaining steps as skipped
                        remaining = steps[step_index + 1:]
                        for rem_step in remaining:
                            failed += 1
                            results.append({
                                "step_number": rem_step["step_number"],
                                "status": "error",
                                "action": rem_step.get("action", ""),
                                "expected": rem_step.get("expected", ""),
                                "actual": "",
                                "error_message": (
                                    f"Skipped: critical step {step_num} "
                                    f"({result['status']})"
                                ),
                                "evidence_url": "",
                                "generated_code": "",
                                "duration_ms": 0,
                            })
                        db.test_runs.update_one(
                            {"_id": ObjectId(test_run_id)},
                            {
                                "$set": {
                                    "results": _make_bson_safe(results),
                                    "passed_steps": passed,
                                    "failed_steps": failed,
                                    "updated_at": datetime.now(timezone.utc),
                                }
                            },
                        )
                        break
        except SoftTimeLimitExceeded:
            timed_out = True
            completed_step_numbers = {r["step_number"] for r in results}
            for step in steps:
                if step["step_number"] not in completed_step_numbers:
                    failed += 1
                    if _is_api_step(step):
                        api_config = step.get("api_config") or {}
                        results.append({
                            "step_number": step["step_number"],
                            "status": "error",
                            "action": f"{api_config.get('method', 'GET')} {api_config.get('endpoint', '/')}",
                            "expected": api_config.get("expected_response", ""),
                            "actual": "",
                            "error_message": "Skipped: test run timed out",
                            "evidence_url": "",
                            "generated_code": "",
                            "duration_ms": 0,
                        })
                    else:
                        results.append({
                            "step_number": step["step_number"],
                            "status": "error",
                            "action": step.get("action", ""),
                            "expected": step.get("expected", ""),
                            "actual": "",
                            "error_message": "Skipped: test run timed out",
                            "evidence_url": "",
                            "generated_code": "",
                            "duration_ms": 0,
                        })
        finally:
            if has_ui_steps and context:
                try:
                    page.evaluate("window.localStorage.clear()")
                except Exception:
                    pass
                try:
                    context.clear_cookies()
                except Exception:
                    pass
                context.close()
                browser.close()

    completed_at = datetime.now(timezone.utc)
    duration_ms = int((completed_at - started_at).total_seconds() * 1000)
    _inconclusive_count = sum(1 for r in results if r.get("status") == "inconclusive")
    final_status = "error" if timed_out else ("passed" if failed == 0 and _inconclusive_count == 0 else "failed")

    _upload_execution_log(s3, bucket, org_id, test_run_id, tc_doc["title"], results, final_status, duration_ms, steps)

    # Store telemetry (non-fatal)
    _store_telemetry(db, collector)

    # 1.6 Persistent Selector Blacklist — write failed step targets to blacklist (non-fatal)
    try:
        _update_selector_blacklist(db, test_case_id, org_id, results, steps)
    except Exception as e:
        logger.warning("execute.blacklist_update_failed", error=str(e)[:200])

    # 3.2 Visual Regression Detection (non-fatal)
    visual_changes: list[dict] = []
    try:
        from app.intelligence.visual_regression import process_run_visual_regression
        visual_changes = process_run_visual_regression(
            db=db,
            s3_client=s3._client,
            bucket=bucket,
            test_run_id=test_run_id,
            test_case_id=test_case_id,
            org_id=org_id,
            step_results=results,
        )
    except Exception as e:
        logger.warning("execute.visual_regression_failed", error=str(e)[:200])

    # 3.2 Test quality score — compute from step definitions + execution profile (non-fatal)
    try:
        _update_test_quality_score(db, test_case_id, org_id, steps, results)
    except Exception as e:
        logger.warning("execute.quality_score_failed", error=str(e)[:200])

    # 3.1 Externalize full step results to test_run_steps collection.
    # test_runs.results stores a lean summary (no generated_code) to keep
    # the document well under the 16MB BSON limit.
    safe_results = _make_bson_safe(results)
    try:
        lean_results = [
            {k: v for k, v in r.items() if k != "generated_code"}
            for r in safe_results
        ]
        db.test_run_steps.update_one(
            {"test_run_id": test_run_id},
            {
                "$set": {
                    "test_run_id": test_run_id,
                    "org_id": org_id,
                    "steps": safe_results,
                    "updated_at": completed_at,
                },
                "$setOnInsert": {"created_at": completed_at},
            },
            upsert=True,
        )
    except Exception as e:
        logger.warning("execute.test_run_steps_write_failed", error=str(e)[:200])
        lean_results = safe_results  # fall back to full results

    db.test_runs.update_one(
        {"_id": ObjectId(test_run_id)},
        {
            "$set": {
                "status": final_status,
                "completed_at": completed_at,
                "duration_ms": duration_ms,
                "total_steps": len(steps),
                "passed_steps": passed,
                "failed_steps": failed,
                "results": lean_results,
                "visual_changes": visual_changes,
                "routes_visited": sorted(routes_visited),
                "updated_at": completed_at,
            }
        },
    )

    return {
        "status": final_status,
        "test_run_id": test_run_id,
        "duration_ms": duration_ms,
        "passed": passed,
        "failed": failed,
    }


def _update_test_quality_score(db, test_case_id: str, org_id: str, steps: list, results: list) -> None:
    """Compute and store a test quality score on the execution profile.

    Three components (each 0-100), averaged into a single quality_score:

    1. assertion_score: % of steps that have a non-empty `expected` field.
       Tests without assertions prove execution only, not correctness.

    2. selector_robustness: % of step executions that succeeded on the first
       selector attempt (no healing required). Measured from this run's results.

    3. scenario_depth: ratio of unique action verbs to total steps, scaled 0-100.
       High-depth tests exercise more distinct UI interactions.
    """
    if not steps:
        return

    # 1. Assertion score
    assertion_count = sum(1 for s in steps if str(s.get("expected", "")).strip())
    assertion_score = (assertion_count / len(steps)) * 100

    # 2. Selector robustness: steps that passed with retry_count == 0
    step_results_map = {r.get("step_number"): r for r in results}
    first_try_passes = sum(
        1 for s in steps
        if step_results_map.get(s.get("step_number"), {}).get("status") == "passed"
        and step_results_map.get(s.get("step_number"), {}).get("retry_count", 0) == 0
    )
    total_completed = sum(1 for r in results if r.get("status") in ("passed", "failed"))
    selector_robustness = (first_try_passes / total_completed * 100) if total_completed else 100.0

    # 3. Scenario depth: unique action verbs / total steps (0-100)
    _ACTION_VERBS = {"click", "fill", "type", "select", "navigate", "hover", "scroll",
                     "check", "uncheck", "assert", "verify", "wait", "press", "upload"}
    action_texts = [str(s.get("action", "")).lower() for s in steps]
    unique_verbs_found = {
        verb for verb in _ACTION_VERBS
        if any(verb in a for a in action_texts)
    }
    scenario_depth = (len(unique_verbs_found) / max(len(_ACTION_VERBS), 1)) * 100

    quality_score_raw = (assertion_score + selector_robustness + scenario_depth) / 3
    quality_score = round(quality_score_raw, 1)

    quality_grade = (
        "A" if quality_score >= 80 else
        "B" if quality_score >= 65 else
        "C" if quality_score >= 50 else
        "D"
    )

    db.execution_profiles.update_one(
        {"test_case_id": test_case_id, "org_id": org_id},
        {"$set": {
            "quality_score": quality_score,
            "quality_grade": quality_grade,
            "quality_components": {
                "assertion_score": round(assertion_score, 1),
                "selector_robustness": round(selector_robustness, 1),
                "scenario_depth": round(scenario_depth, 1),
            },
        }},
        upsert=True,
    )


def _update_selector_blacklist(db, test_case_id: str, org_id: str, results: list, steps: list) -> None:
    """Write failed step targets to selector_blacklist.

    For each step that failed, parse the generated_code StepIntent JSON
    and write its target descriptors to the blacklist. Entries expire after 7 days.
    """
    import json as _j
    from datetime import timedelta

    failed_steps = {r["step_number"]: r for r in results if r.get("status") in ("failed", "error") and r.get("generated_code")}
    if not failed_steps:
        return

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=7)

    for step_num, result in failed_steps.items():
        code = result.get("generated_code", "")
        if not code:
            continue
        try:
            intent = _j.loads(code)
            actions = intent.get("actions", []) if isinstance(intent, dict) else []
            for action in actions:
                target = action.get("target")
                if not target or not isinstance(target, dict):
                    continue
                # Serialize target as a stable string key
                target_key = _j.dumps(target, sort_keys=True)
                db.selector_blacklist.update_one(
                    {
                        "test_case_id": test_case_id,
                        "step_number": step_num,
                        "target_key": target_key,
                        "org_id": org_id,
                    },
                    {
                        "$inc": {"failure_count": 1},
                        "$set": {"last_failed_at": now, "expires_at": expires_at},
                        "$setOnInsert": {"created_at": now},
                    },
                    upsert=True,
                )
        except Exception:
            pass  # Non-fatal per step


def _load_selector_blacklist(db, test_case_id: str, org_id: str, step_number: int) -> list[dict]:
    """Load blacklisted targets for a given (test_case_id, step_number).

    Returns list of target dicts to avoid. Only returns entries with failure_count >= 2.
    """
    import json as _j
    now = datetime.now(timezone.utc)
    docs = list(db.selector_blacklist.find({
        "test_case_id": test_case_id,
        "step_number": step_number,
        "org_id": org_id,
        "failure_count": {"$gte": 2},
        "expires_at": {"$gt": now},
    }, {"target_key": 1}).limit(20))
    result = []
    for doc in docs:
        try:
            result.append(_j.loads(doc["target_key"]))
        except Exception:
            pass
    return result


def _replay_step(page, generated_code: str) -> None:
    """Replay a step from saved generated_code (intent JSON only)."""
    import json as _json
    try:
        data = _json.loads(generated_code)
        if isinstance(data, dict) and "actions" in data:
            from app.worker.intent_executor import execute_intent
            from app.worker.intent_schema import StepIntent
            intent = StepIntent.model_validate(data)
            execute_intent(page, intent)
            return
    except (ValueError, _json.JSONDecodeError):
        pass
    # Legacy Python code cannot be replayed — skip with warning
    logger.warning(
        "replay_step.legacy_code_skipped",
        code_length=len(generated_code),
    )


def _get_limiter_and_redis():
    """Return (ExecutionLimiter, sync Redis) if rate limiting is enabled, else (None, None)."""
    if not settings.execution_rate_limit_enabled:
        return None, None
    try:
        from redis import Redis
        from app.execution_limits import ExecutionLimiter
        redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
        limiter = ExecutionLimiter(
            max_concurrent_per_org=settings.execution_max_concurrent_per_org,
            max_queue_depth_per_org=settings.execution_max_queue_depth_per_org,
            global_max_concurrent=settings.execution_global_max_concurrent,
            lock_ttl_seconds=settings.execution_lock_ttl_seconds,
        )
        return limiter, redis_client
    except Exception as e:
        logger.warning("execute_test_run.limiter_init_failed", error=str(e)[:200])
        return None, None


@celery_app.task(name="cg.execute_test_run", bind=True, soft_time_limit=1200, time_limit=1320)
def execute_test_run(self, test_run_id: str) -> dict:
    db = _get_sync_db()
    now = datetime.now(timezone.utc)

    # Load the test run
    run_doc = db.test_runs.find_one({"_id": ObjectId(test_run_id)})
    if not run_doc:
        return {"status": "error", "message": "Test run not found"}

    org_id = str(run_doc["org_id"])

    # Acquire execution slot (rate limiting guard)
    limiter, limiter_redis = _get_limiter_and_redis()
    if limiter and limiter_redis:
        try:
            limiter.acquire(limiter_redis, org_id, test_run_id)
        except Exception as e:
            logger.warning(
                "execute_test_run.rate_limited",
                test_run_id=test_run_id, org_id=org_id, error=str(e)[:200],
            )
            # Re-queue with backoff instead of failing hard
            raise self.retry(countdown=30, max_retries=3, exc=e)

    try:
        # Load the test case to get steps
        tc_doc = db.test_cases.find_one({"_id": run_doc["test_case_id"]})
        if not tc_doc:
            db.test_runs.update_one(
                {"_id": ObjectId(test_run_id)},
                {
                    "$set": {
                        "status": "error",
                        "error_message": "Test case not found",
                        "completed_at": now,
                        "updated_at": now,
                    }
                },
            )
            return {"status": "error", "message": "Test case not found"}

        all_steps = tc_doc.get("steps", [])
        run_until_step = run_doc.get("run_until_step", 0)
        steps = [s for s in all_steps if run_until_step == 0 or s.get("step_number", 0) <= run_until_step]

        # Transition to running
        started_at = datetime.now(timezone.utc)
        db.test_runs.update_one(
            {"_id": ObjectId(test_run_id)},
            {
                "$set": {
                    "status": "running",
                    "started_at": started_at,
                    "total_steps": len(steps),
                    "updated_at": started_at,
                }
            },
        )

        # Initialize clients
        ai_provider = _build_ai_provider()
        if ai_provider.raw_client is None:
            logger.warning("execute_test_run.ai_unavailable", reason="no OpenAI API key configured")
        ai_client = ai_provider  # Pass provider through existing ai_client parameter
        s3 = _get_s3_client()
        bucket = settings.minio_default_bucket

        model = settings.openai_model

        result = _execute_test_run_unified(db, run_doc, tc_doc, test_run_id, ai_client, s3, bucket, steps, org_id, model)

        # Trigger execution profile aggregation asynchronously
        try:
            from app.worker.tasks.aggregate_profiles import aggregate_execution_profile
            aggregate_execution_profile.delay(test_run_id)
        except Exception as e:
            logger.warning("execute_test_run.aggregate_trigger_failed", error=str(e)[:200])

        return result
    finally:
        # Always release the execution slot
        if limiter and limiter_redis:
            try:
                limiter.release(limiter_redis, org_id, test_run_id)
            except Exception as e:
                logger.warning(
                    "execute_test_run.release_failed",
                    test_run_id=test_run_id, error=str(e)[:200],
                )


@celery_app.task(name="cg.rerun_step", bind=True, soft_time_limit=300, time_limit=360)
def rerun_step(self, test_run_id: str, step_number: int, override_intent: str | None = None) -> dict:
    """Re-execute a single step in-place on an existing test run.

    Replays preceding steps using their previously generated code (no AI),
    then re-executes the target step with AI and updates the result in-place.
    """
    db = _get_sync_db()

    run_doc = db.test_runs.find_one({"_id": ObjectId(test_run_id)})
    if not run_doc:
        return {"status": "error", "message": "Test run not found"}

    tc_doc = db.test_cases.find_one({"_id": run_doc["test_case_id"]})
    if not tc_doc:
        return {"status": "error", "message": "Test case not found"}

    steps = tc_doc.get("steps", [])
    test_data = tc_doc.get("test_data", {})
    org_id = str(run_doc["org_id"])
    previous_results = run_doc.get("results", [])

    target_step = next((s for s in steps if s.get("step_number") == step_number), None)
    if not target_step:
        return {"status": "error", "message": f"Step {step_number} not found"}

    # Mark test run as running
    started_at = datetime.now(timezone.utc)
    db.test_runs.update_one(
        {"_id": ObjectId(test_run_id)},
        {"$set": {"status": "running", "updated_at": started_at}},
    )

    ai_client = _build_ai_provider()
    s3 = _get_s3_client()
    bucket = settings.minio_default_bucket
    model = settings.openai_model

    target_is_api = _is_api_step(target_step)
    preceding_steps = [s for s in steps if s.get("step_number", 0) < step_number]
    has_preceding_api = any(_is_api_step(s) for s in preceding_steps)
    need_browser = not target_is_api

    effective_test_type = "api" if target_is_api else "ui"
    collector = TelemetryCollector(test_run_id, org_id, effective_test_type)

    new_result = None
    base_url = _resolve_base_url(db, tc_doc) if (target_is_api or has_preceding_api) else ""
    variables = {}
    previous_actions = []

    pw_cm = sync_playwright() if need_browser else contextlib.nullcontext()

    with pw_cm as pw:
        page = None
        browser = None
        context = None

        if need_browser:
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
            collector.capture_environment(browser, page)

        try:
            # Replay preceding steps by type
            prev_by_step = {r["step_number"]: r for r in previous_results}
            for s in preceding_steps:
                sn = s.get("step_number", 0)
                if _is_api_step(s):
                    # Re-execute API step to rebuild chaining variables
                    execute_api_step(
                        ai_client=ai_client,
                        step=s,
                        base_url=base_url,
                        org_id=org_id,
                        test_run_id=test_run_id,
                        s3_client=s3,
                        bucket=bucket,
                        variables=variables,
                        model=model,
                    )
                elif need_browser:
                    # Replay UI step from saved generated code
                    prev = prev_by_step.get(sn)
                    if prev:
                        code = prev.get("generated_code", "")
                        if code:
                            try:
                                _replay_step(page, code)
                            except Exception:
                                pass  # Best effort replay
                    previous_actions.append(s.get("action", ""))

            # Execute target step
            if target_is_api:
                api_config = target_step.get("api_config") or {}
                step_action = f"{api_config.get('method', 'GET')} {api_config.get('endpoint', '/')}"
                tel = collector.begin_step(step_number, step_action)
                t0 = _time.monotonic()

                new_result = execute_api_step(
                    ai_client=ai_client,
                    step=target_step,
                    base_url=base_url,
                    org_id=org_id,
                    test_run_id=test_run_id,
                    s3_client=s3,
                    bucket=bucket,
                    variables=variables,
                    model=model,
                )

                api_ms = int((_time.monotonic() - t0) * 1000)
                tel.record_phase("code_execution", api_ms)
                tel.record_attempt(1, new_result["status"] == "passed", "", api_ms, new_result.get("error_message", ""))
                collector.finish_step(tel, new_result["status"])

            else:
                step_custom_code = target_step.get("custom_code", "") or None

                # User-provided override intent takes priority over all caches
                override_cached = None
                if override_intent:
                    override_cached = {
                        "last_successful_code": override_intent,
                        "code_type": "intent",
                        "stability_score": 1.0,
                        "code_hash": "",
                    }

                # Load intelligence for this step (non-fatal)
                rerun_selector_hints = None
                rerun_intelligence = None
                rerun_step_strategy = None
                rerun_cached_code = None
                try:
                    from app.intelligence.selector_memory import get_known_good_selectors
                    from app.intelligence.feedback import build_step_intelligence
                    from app.intelligence.code_reuse import get_cached_code
                    rerun_selector_hints = get_known_good_selectors(
                        db, str(tc_doc["_id"]), org_id, step_number
                    ) or None
                    rerun_intelligence = build_step_intelligence(
                        db, str(tc_doc["_id"]), org_id, step_number
                    ) or None
                    rerun_cached_code = get_cached_code(
                        db, str(tc_doc["_id"]), org_id, step_number
                    )
                except Exception:
                    pass

                # Compute strategy for this step (non-fatal, feature-flagged)
                try:
                    if settings.execution_strategy_enabled:
                        from app.intelligence.strategy import compute_run_strategy
                        profile_doc = db.execution_profiles.find_one({
                            "test_case_id": str(tc_doc["_id"]),
                            "org_id": org_id,
                        })
                        hint_map = {step_number: rerun_selector_hints} if rerun_selector_hints else None
                        rerun_run_strategy = compute_run_strategy(profile_doc, hint_map)
                        if rerun_run_strategy:
                            rerun_step_strategy = rerun_run_strategy.get_step(step_number)
                except Exception:
                    pass

                tel = collector.begin_step(step_number, target_step.get("action", ""))

                new_result = execute_step(
                    page=page,
                    ai_client=ai_client,
                    step=target_step,
                    org_id=org_id,
                    test_run_id=test_run_id,
                    s3_client=s3,
                    bucket=bucket,
                    previous_actions=previous_actions,
                    test_data=test_data if test_data else None,
                    custom_code=step_custom_code,
                    model=model,
                    tel=tel,
                    selector_hints=rerun_selector_hints,
                    intelligence_context=rerun_intelligence,
                    step_strategy=rerun_step_strategy,
                    cached_code=override_cached or rerun_cached_code,
                    execution_mode=settings.execution_mode,
                )

                collector.finish_step(tel, new_result["status"])

        except SoftTimeLimitExceeded:
            if target_is_api:
                api_config = target_step.get("api_config") or {}
                new_result = {
                    "step_number": step_number,
                    "status": "error",
                    "action": f"{api_config.get('method', 'GET')} {api_config.get('endpoint', '/')}",
                    "expected": api_config.get("expected_response", ""),
                    "actual": "",
                    "error_message": "Step re-run timed out",
                    "evidence_url": "",
                    "generated_code": "",
                    "duration_ms": 0,
                }
            else:
                new_result = {
                    "step_number": step_number,
                    "status": "error",
                    "action": target_step.get("action", ""),
                    "expected": target_step.get("expected", ""),
                    "actual": "",
                    "error_message": "Step re-run timed out",
                    "evidence_url": "",
                    "generated_code": "",
                    "duration_ms": 0,
                }
        finally:
            if need_browser and context:
                try:
                    page.evaluate("window.localStorage.clear()")
                except Exception:
                    pass
                try:
                    context.clear_cookies()
                except Exception:
                    pass
                context.close()
                browser.close()

    # Store telemetry (non-fatal)
    _store_telemetry(db, collector)

    # Update the step result in-place
    updated_results = []
    for r in previous_results:
        if r["step_number"] == step_number:
            updated_results.append(new_result)
        else:
            updated_results.append(r)

    passed = sum(1 for r in updated_results if r["status"] == "passed")
    failed = len(updated_results) - passed
    final_status = "passed" if failed == 0 else "failed"

    completed_at = datetime.now(timezone.utc)
    safe_updated = _make_bson_safe(updated_results)

    # 3.1 Update test_run_steps with the patched results (non-fatal)
    try:
        lean_updated = [{k: v for k, v in r.items() if k != "generated_code"} for r in safe_updated]
        db.test_run_steps.update_one(
            {"test_run_id": test_run_id},
            {
                "$set": {"steps": safe_updated, "updated_at": completed_at},
                "$setOnInsert": {"created_at": completed_at},
            },
            upsert=True,
        )
    except Exception:
        lean_updated = safe_updated

    db.test_runs.update_one(
        {"_id": ObjectId(test_run_id)},
        {
            "$set": {
                "status": final_status,
                "passed_steps": passed,
                "failed_steps": failed,
                "results": lean_updated,
                "updated_at": completed_at,
            }
        },
    )

    return {
        "status": final_status,
        "test_run_id": test_run_id,
        "step_number": step_number,
        "step_status": new_result["status"] if new_result else "error",
    }
