"""TelemetryCollector — instruments step execution without altering return contracts.

Usage:
    collector = TelemetryCollector(test_run_id, org_id, test_type)

    # Capture environment once at browser launch
    collector.capture_environment(browser, page)

    # For each step:
    step_tel = collector.begin_step(step_number, action)
    step_tel.record_phase("context_capture", duration_ms)
    step_tel.record_ai_call(phase, model, latency_ms, tokens, ...)
    step_tel.record_attempt(attempt_num, success, exec_ms, error, ...)
    step_tel.record_phase("verification", duration_ms)
    collector.finish_step(step_tel, status)

    # At run end:
    telemetry_doc = collector.finalize()
    db.execution_telemetry.insert_one(telemetry_doc)
"""

from __future__ import annotations

import os
import platform
import re
import socket
import time
from typing import TYPE_CHECKING

import structlog

from app.telemetry.confidence_scorer import compute_run_confidence
from app.telemetry.models import (
    AICallMetrics,
    EnvironmentSnapshot,
    ExecutionAttempt,
    PhaseTimings,
    RetryReason,
    RunTelemetry,
    StepTelemetry,
)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page

logger = structlog.get_logger(__name__)


def classify_error(error_message: str) -> RetryReason:
    """Classify an execution error into a retry reason category.

    This drives flake analysis — knowing WHY a step failed
    is more valuable than knowing THAT it failed.

    Pattern matching is ordered from most-specific to least-specific
    to avoid misclassification (e.g. "timeout" matching non-timeout errors).
    """
    msg = error_message.lower()

    # --- Selector issues (most common flake source) ---
    if any(kw in msg for kw in ("strict mode violation", "resolved to", "multiple elements")):
        return RetryReason.SELECTOR_AMBIGUOUS
    if any(kw in msg for kw in ("not visible", "is hidden", "not in the viewport")):
        return RetryReason.SELECTOR_NOT_VISIBLE
    if any(kw in msg for kw in (
        "waiting for selector", "locator resolved to", "no element found",
        "waiting for locator", "could not find",
    )):
        return RetryReason.SELECTOR_NOT_FOUND

    # --- Page lifecycle (before timeout check — more specific) ---
    if "page crash" in msg or "target closed" in msg:
        return RetryReason.PAGE_CRASH
    if "element is detached" in msg:
        return RetryReason.ELEMENT_DETACHED
    if "frame was detached" in msg or "frame detached" in msg:
        return RetryReason.FRAME_DETACHED

    # --- Network (before timeout — "net::" errors often contain "timeout") ---
    if any(kw in msg for kw in ("net::", "err_connection", "err_name", "dns")):
        return RetryReason.NETWORK_ERROR

    # --- Navigation/action timeout (compound patterns to reduce false positives) ---
    if "timeout" in msg:
        # Genuine Playwright timeouts
        if any(kw in msg for kw in (
            "navigation", "waiting for", "page.goto", "load state",
            "time limit", "exceeded the time",
        )):
            return RetryReason.NAVIGATION_TIMEOUT
        # Generic timeout (e.g. "Timeout 30000ms exceeded")
        if "exceeded" in msg:
            return RetryReason.NAVIGATION_TIMEOUT
        return RetryReason.NAVIGATION_TIMEOUT

    # --- JS errors ---
    if any(kw in msg for kw in ("evaluation failed", "javascript error", "syntax error")):
        return RetryReason.JS_ERROR

    # --- AI / code gen ---
    if "code generation failed" in msg:
        return RetryReason.CODE_GEN_FAILURE

    # --- Assertion ---
    if any(kw in msg for kw in ("assert", "expect", "verification")):
        return RetryReason.ASSERTION_FAILURE

    return RetryReason.UNKNOWN


def extract_selectors_from_code(code: str) -> list[str]:
    """Extract Playwright selectors from generated code.

    Used for selector success/failure tracking.
    Returns deduplicated list in order of appearance.
    """
    patterns = [
        r'get_by_role\(\s*"([^"]+)"',
        r'get_by_text\(\s*"([^"]+)"',
        r'get_by_label\(\s*"([^"]+)"',
        r'get_by_placeholder\(\s*"([^"]+)"',
        r'get_by_test_id\(\s*"([^"]+)"',
        r'locator\(\s*"([^"]+)"',
        r'query_selector\(\s*"([^"]+)"',
    ]

    seen: set[str] = set()
    selectors: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, code):
            selector = match.group(0)  # Full call like get_by_role("button"
            if selector not in seen:
                seen.add(selector)
                selectors.append(selector)

    return selectors


class _StepTelemetryBuilder:
    """Mutable builder for a single step's telemetry.

    Created by TelemetryCollector.begin_step(), finalized by finish_step().
    Provides a clean API for recording phase timings and attempts
    without leaking telemetry concerns into the executor.
    """

    def __init__(self, step_number: int, action: str) -> None:
        self.step_number = step_number
        self.action = action
        self._start = time.monotonic()
        self._phase_timings: dict[str, int] = {}
        self._attempts: list[ExecutionAttempt] = []
        self._ai_calls: list[AICallMetrics] = []
        self._selectors_tried: list[str] = []
        self._selectors_structured: list[dict] = []
        self._selector_that_worked: str = ""
        self._evidence_size: int = 0
        self._evidence_upload_ms: int = 0
        self._used_custom_code: bool = False
        self._reused_code: bool = False
        self._code_hash: str = ""
        self._regeneration_reason: str = ""
        self._used_selector_fallback: bool = False

    def record_phase(self, phase: str, duration_ms: int) -> None:
        """Record timing for a named phase."""
        self._phase_timings[phase] = duration_ms

    def record_ai_call(
        self,
        phase: str,
        model: str,
        latency_ms: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        success: bool = True,
        error: str = "",
    ) -> None:
        """Record metrics for an AI API call."""
        self._ai_calls.append(AICallMetrics(
            phase=phase,
            model=model,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            success=success,
            error=error,
        ))

    def record_attempt(
        self,
        attempt_number: int,
        success: bool,
        code: str,
        execution_ms: int,
        error_message: str = "",
        regeneration_ms: int = 0,
        backoff_ms: int = 0,
        selectors: list[str | dict] | None = None,
    ) -> None:
        """Record one execution attempt (initial or retry).

        Args:
            selectors: Selector data from the structured execution path.
                      Accepts strategy name strings or full telemetry dicts
                      (with strategy, params, composite_score, etc.).
                      When dicts are provided, both strategy strings and
                      structured data are stored for downstream use.
        """
        error_type = RetryReason.UNKNOWN
        if not success and error_message:
            error_type = classify_error(error_message)

        if selectors is not None:
            for s in selectors:
                if isinstance(s, dict):
                    # Full telemetry dict from resolve_target()
                    strategy_str = s.get("strategy", "")
                    if strategy_str and strategy_str not in self._selectors_tried:
                        self._selectors_tried.append(strategy_str)
                    self._selectors_structured.append(s)
                else:
                    # Plain strategy name string
                    if s not in self._selectors_tried:
                        self._selectors_tried.append(s)
            if success and selectors:
                last = selectors[-1]
                self._selector_that_worked = (
                    last.get("strategy", "") if isinstance(last, dict) else last
                )
        else:
            # Fallback: extract selectors from code via regex
            extracted = extract_selectors_from_code(code)
            for s in extracted:
                if s not in self._selectors_tried:
                    self._selectors_tried.append(s)
            if success and extracted:
                self._selector_that_worked = extracted[-1]

        _bf = getattr(self, "_last_behavior_failures", 0)
        self._attempts.append(ExecutionAttempt(
            attempt_number=attempt_number,
            success=success,
            code_length=len(code),
            execution_ms=execution_ms,
            error_type=error_type,
            error_message=error_message[:500],
            regeneration_ms=regeneration_ms,
            backoff_ms=backoff_ms,
            resolution_tier=getattr(self, "_last_resolution_tier", 0),
            guard_failures=list(getattr(self, "_last_guard_failures", [])),
            post_action_verified=getattr(self, "_last_post_action_verified", True),
            post_action_method=getattr(self, "_last_post_action_method", ""),
            # BDRE metrics (staged via record_stability_metrics)
            behavior_failures=_bf,
            behavior_failed=_bf > 0,
            interaction_attempts=getattr(self, "_last_interaction_attempts", 0),
            resolution_retries=getattr(self, "_last_resolution_retries", 0),
            behavior_signals=list(getattr(self, "_last_behavior_signals", [])),
            mutation_total=getattr(self, "_last_mutation_total", 0),
            mutation_significant=getattr(self, "_last_mutation_significant", False),
            # Healing metrics (staged via record_healing_metrics)
            healing_attempted=getattr(self, "_last_healing_attempted", False),
            healing_success=getattr(self, "_last_healing_success", False),
            healing_layer_used=getattr(self, "_last_healing_layer_used", 0),
            healing_strategy=getattr(self, "_last_healing_strategy", ""),
            healing_elapsed_ms=getattr(self, "_last_healing_elapsed_ms", 0),
            selector_repaired=getattr(self, "_last_selector_repaired", False),
            action_repaired=getattr(self, "_last_action_repaired", False),
        ))

        # Reset staged healing metrics to prevent bleed into next attempt
        self._last_healing_attempted = False
        self._last_healing_success = False
        self._last_healing_layer_used = 0
        self._last_healing_strategy = ""
        self._last_healing_elapsed_ms = 0
        self._last_selector_repaired = False
        self._last_action_repaired = False

    def record_evidence(self, size_bytes: int, upload_ms: int) -> None:
        """Record evidence file metadata."""
        self._evidence_size = size_bytes
        self._evidence_upload_ms = upload_ms

    def set_custom_code(self, used: bool) -> None:
        self._used_custom_code = used

    def set_reused_code(self, reused: bool, hash_val: str = "") -> None:
        self._reused_code = reused
        self._code_hash = hash_val

    def set_regeneration_reason(self, reason: str) -> None:
        self._regeneration_reason = reason

    def set_selector_fallback(self, used: bool) -> None:
        self._used_selector_fallback = used

    def record_selector_score(self, score_dict: dict) -> None:
        """Record a selector resolution score for telemetry."""
        if not hasattr(self, "_selector_scores"):
            self._selector_scores: list[dict] = []
        self._selector_scores.append(score_dict)

    def record_disambiguation(self, used: bool, layer: str = "") -> None:
        """Record disambiguation usage."""
        self._disambiguation_used = used
        self._disambiguation_layer = layer

    def record_budget_breakdown(self, breakdown: dict) -> None:
        """Record execution budget breakdown."""
        self._budget_breakdown = breakdown

    def record_budget_exceeded(self, exceeded: bool) -> None:
        self._budget_exceeded = exceeded

    def record_ai_gate_metrics(self, metrics: dict) -> None:
        """Record AI gate usage metrics."""
        self._ai_gate_metrics = metrics

    def record_failure_diagnosis(self, diagnosis_dict: dict) -> None:
        """Record a failure diagnosis for this step."""
        if not hasattr(self, "_failure_diagnoses"):
            self._failure_diagnoses: list[dict] = []
        self._failure_diagnoses.append(diagnosis_dict)

    def record_recovery_action(self, action_dict: dict) -> None:
        """Record a recovery action taken during this step."""
        if not hasattr(self, "_recovery_actions"):
            self._recovery_actions: list[dict] = []
        self._recovery_actions.append(action_dict)

    def record_stability_metrics(
        self,
        resolution_tier: int = 0,
        guard_failures: list[str] | None = None,
        post_action_verified: bool = True,
        post_action_method: str = "",
        behavior_failures: int = 0,
        interaction_attempts: int = 0,
        resolution_retries: int = 0,
        behavior_signals: list[str] | None = None,
        mutation_total: int = 0,
        mutation_significant: bool = False,
    ) -> None:
        """Record stability metrics for the current attempt.

        These values are attached to the next ExecutionAttempt recorded via record_attempt().
        """
        self._last_resolution_tier = resolution_tier
        self._last_guard_failures = guard_failures or []
        self._last_post_action_verified = post_action_verified
        self._last_post_action_method = post_action_method
        # BDRE metrics
        self._last_behavior_failures = behavior_failures
        self._last_interaction_attempts = interaction_attempts
        self._last_resolution_retries = resolution_retries
        self._last_behavior_signals = behavior_signals or []
        self._last_mutation_total = mutation_total
        self._last_mutation_significant = mutation_significant

    def record_healing_metrics(
        self,
        healing_attempted: bool = False,
        healing_success: bool = False,
        healing_layer_used: int = 0,
        healing_strategy: str = "",
        healing_elapsed_ms: int = 0,
        selector_repaired: bool = False,
        action_repaired: bool = False,
    ) -> None:
        """Stage healing metrics for the next record_attempt() call.

        These values are attached to the next ExecutionAttempt recorded via record_attempt().
        Follows the same staging pattern as record_stability_metrics().
        """
        self._last_healing_attempted = healing_attempted
        self._last_healing_success = healing_success
        self._last_healing_layer_used = healing_layer_used
        self._last_healing_strategy = healing_strategy
        self._last_healing_elapsed_ms = healing_elapsed_ms
        self._last_selector_repaired = selector_repaired
        self._last_action_repaired = action_repaired

    def record_verification_level(self, level: str) -> None:
        """Record the final verification mode used for this step."""
        self._verification_level = level

    def build(self, status: str) -> StepTelemetry:
        """Finalize and return the immutable StepTelemetry model."""
        total_ms = int((time.monotonic() - self._start) * 1000)
        total_retry_ms = sum(
            a.execution_ms + a.regeneration_ms + a.backoff_ms
            for a in self._attempts[1:]  # skip first attempt
        )

        timings = PhaseTimings(
            pre_capture_stability_ms=self._phase_timings.get("pre_capture_stability", 0),
            context_capture_ms=self._phase_timings.get("context_capture", 0),
            code_generation_ms=self._phase_timings.get("code_generation", 0),
            code_execution_ms=self._phase_timings.get("code_execution", 0),
            post_exec_stability_ms=self._phase_timings.get("post_exec_stability", 0),
            screenshot_ms=self._phase_timings.get("screenshot", 0),
            evidence_upload_ms=self._phase_timings.get("evidence_upload", 0),
            verification_ms=self._phase_timings.get("verification", 0),
            total_retry_ms=total_retry_ms,
            total_ms=total_ms,
        )

        return StepTelemetry(
            step_number=self.step_number,
            action=self.action,
            status=status,
            timings=timings,
            attempts=self._attempts,
            total_attempts=len(self._attempts),
            used_custom_code=self._used_custom_code,
            reused_code=self._reused_code,
            code_hash=self._code_hash,
            regeneration_reason=self._regeneration_reason,
            ai_calls=self._ai_calls,
            selectors_tried=self._selectors_tried,
            selectors_structured=self._selectors_structured,
            selector_that_worked=self._selector_that_worked,
            used_selector_fallback=self._used_selector_fallback,
            evidence_size_bytes=self._evidence_size,
            evidence_upload_ms=self._evidence_upload_ms,
            # New Part 9 telemetry fields
            selector_scores=getattr(self, "_selector_scores", []),
            disambiguation_used=getattr(self, "_disambiguation_used", False),
            disambiguation_layer=getattr(self, "_disambiguation_layer", ""),
            budget_breakdown=getattr(self, "_budget_breakdown", {}),
            budget_exceeded=getattr(self, "_budget_exceeded", False),
            ai_gate_metrics=getattr(self, "_ai_gate_metrics", {}),
            failure_diagnoses=getattr(self, "_failure_diagnoses", []),
            recovery_actions_taken=getattr(self, "_recovery_actions", []),
            verification_level=getattr(self, "_verification_level", ""),
        )


class TelemetryCollector:
    """Collects telemetry for an entire test run.

    Lifecycle:
        1. Create at run start
        2. capture_environment() after browser launch
        3. begin_step() / finish_step() for each step
        4. finalize() at run end → returns dict for MongoDB insert
    """

    def __init__(self, test_run_id: str, org_id: str, test_type: str = "ui") -> None:
        self._test_run_id = test_run_id
        self._org_id = org_id
        self._test_type = test_type
        self._environment = EnvironmentSnapshot()
        self._steps: list[StepTelemetry] = []
        self._flake_indicators: list[str] = []

    def capture_environment(self, browser: Browser, page: Page) -> None:
        """Snapshot the execution environment at browser launch."""
        try:
            browser_version = browser.version
        except Exception:
            browser_version = "unknown"

        try:
            viewport = page.viewport_size
            vw = viewport["width"] if viewport else 0
            vh = viewport["height"] if viewport else 0
        except Exception:
            vw, vh = 0, 0

        try:
            user_agent = page.evaluate("() => navigator.userAgent")
        except Exception:
            user_agent = ""

        self._environment = EnvironmentSnapshot(
            browser_name="chromium",
            browser_version=browser_version,
            viewport_width=vw,
            viewport_height=vh,
            platform=platform.system().lower(),
            user_agent=user_agent,
            headless=True,
            worker_hostname=socket.gethostname(),
            worker_pid=os.getpid(),
            chromium_args=["--no-sandbox", "--disable-dev-shm-usage"],
            default_timeout_ms=10000,
            default_nav_timeout_ms=30000,
        )

        logger.info(
            "telemetry.environment_captured",
            test_run_id=self._test_run_id,
            browser=browser_version,
            viewport=f"{vw}x{vh}",
        )

    def begin_step(self, step_number: int, action: str) -> _StepTelemetryBuilder:
        """Start telemetry collection for a step. Returns a builder."""
        return _StepTelemetryBuilder(step_number, action)

    def finish_step(self, builder: _StepTelemetryBuilder, status: str) -> StepTelemetry:
        """Finalize a step's telemetry and add it to the run."""
        step_tel = builder.build(status)
        self._steps.append(step_tel)

        # Detect flake indicators
        if step_tel.total_attempts > 1 and status == "passed":
            self._flake_indicators.append(
                f"step_{step_tel.step_number}: passed after {step_tel.total_attempts} attempts"
            )

        if step_tel.timings.total_retry_ms > 5000:
            self._flake_indicators.append(
                f"step_{step_tel.step_number}: {step_tel.timings.total_retry_ms}ms in retries"
            )

        logger.info(
            "telemetry.step_complete",
            test_run_id=self._test_run_id,
            step=step_tel.step_number,
            status=status,
            attempts=step_tel.total_attempts,
            total_ms=step_tel.timings.total_ms,
            code_gen_ms=step_tel.timings.code_generation_ms,
            exec_ms=step_tel.timings.code_execution_ms,
            verify_ms=step_tel.timings.verification_ms,
        )

        return step_tel

    def finalize(self) -> dict:
        """Build the final telemetry document for MongoDB insertion."""
        total_ai_calls = sum(len(s.ai_calls) for s in self._steps)
        total_ai_latency = sum(
            call.latency_ms
            for s in self._steps
            for call in s.ai_calls
        )
        total_retries = sum(
            max(0, s.total_attempts - 1)
            for s in self._steps
        )

        doc = RunTelemetry(
            test_run_id=self._test_run_id,
            org_id=self._org_id,
            test_type=self._test_type,
            environment=self._environment,
            steps=self._steps,
            total_ai_calls=total_ai_calls,
            total_ai_latency_ms=total_ai_latency,
            total_retries=total_retries,
            flake_indicators=self._flake_indicators,
        )

        # Compute confidence scores
        try:
            doc.confidence = compute_run_confidence(doc)
        except Exception as e:
            logger.warning("telemetry.confidence_scoring_failed", error=str(e)[:200])
            doc.confidence = {}

        return doc.model_dump(by_alias=True, exclude={"id"})
