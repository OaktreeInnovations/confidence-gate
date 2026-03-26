"""Telemetry data models for execution observability.

These models capture the full execution story for every step:
- What happened in each phase (timing breakdown)
- Why retries occurred (classified reasons)
- What the AI service did (tokens, latency, model)
- What the environment looked like (browser, viewport, memory)

Storage: separate `execution_telemetry` collection in MongoDB,
keyed by test_run_id. Not embedded in test_run to avoid polling bloat.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from bson import ObjectId
from pydantic import BaseModel, Field


class RetryReason(StrEnum):
    """Classified reason why a step execution attempt failed."""
    SELECTOR_NOT_FOUND = "selector_not_found"
    SELECTOR_NOT_VISIBLE = "selector_not_visible"
    SELECTOR_AMBIGUOUS = "selector_ambiguous"
    NAVIGATION_TIMEOUT = "navigation_timeout"
    PAGE_CRASH = "page_crash"
    ELEMENT_DETACHED = "element_detached"
    FRAME_DETACHED = "frame_detached"
    NETWORK_ERROR = "network_error"
    JS_ERROR = "js_error"
    CODE_GEN_FAILURE = "code_gen_failure"
    ASSERTION_FAILURE = "assertion_failure"
    UNKNOWN = "unknown"


class AICallMetrics(BaseModel):
    """Metrics for a single OpenAI API call."""
    phase: str  # "code_gen", "code_regen", "vision_verify", "api_verify"
    model: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    success: bool = True
    error: str = ""


class PhaseTimings(BaseModel):
    """Granular timing breakdown for a single step execution.

    Each field is milliseconds spent in that phase.
    Sum of all phases ≈ total step duration.
    """
    pre_capture_stability_ms: int = 0  # DOM/network/animation stability before context capture
    context_capture_ms: int = 0        # screenshot + a11y tree + HTML extraction
    code_generation_ms: int = 0        # AI code gen (first attempt)
    code_execution_ms: int = 0         # exec() of generated code
    post_exec_stability_ms: int = 0    # DOM/network/animation stability after exec()
    screenshot_ms: int = 0             # post-action screenshot capture
    evidence_upload_ms: int = 0        # S3/MinIO upload
    verification_ms: int = 0           # vision/API verification
    total_retry_ms: int = 0            # time spent across all retries (regen + re-exec)
    total_ms: int = 0                  # wall clock for the entire step


class ExecutionAttempt(BaseModel):
    """Record of a single execution attempt within a step.

    A step with max_attempts=3 may produce up to 3 of these.
    Attempt 1 is the initial try; 2+ are retries with regenerated code.
    """
    attempt_number: int
    success: bool = False
    code_length: int = 0           # len(generated_code)
    execution_ms: int = 0          # exec() duration for this attempt
    error_type: RetryReason = RetryReason.UNKNOWN
    error_message: str = ""
    regeneration_ms: int = 0       # AI regen duration (0 for attempt 1)
    backoff_ms: int = 0            # sleep between retries

    # Stability metrics (Part 6)
    resolution_tier: int = 0                    # 1-4: which tier resolved the element
    guard_failures: list[str] = Field(default_factory=list)  # e.g. ["overlay_detected"]
    post_action_verified: bool = True           # False if post-action check failed
    post_action_method: str = ""                # "url_change", "aria_expanded", etc.

    # BDRE metrics (behavior-driven resolution engine)
    behavior_failures: int = 0                  # actions with no detected behavior effect
    behavior_failed: bool = False               # True if any behavior failures occurred
    interaction_attempts: int = 0               # total interaction attempts (incl. repair)
    resolution_retries: int = 0                 # in-attempt selector re-resolutions
    behavior_signals: list[str] = Field(default_factory=list)  # e.g. ["url_change", "dom_mutation"]
    mutation_total: int = 0                     # total DOM mutations during action
    mutation_significant: bool = False          # whether mutations were structurally significant

    # Healing metrics
    healing_attempted: bool = False
    healing_success: bool = False               # True if healing actually resolved the issue
    healing_layer_used: int = 0                 # 0=none, 1=selector, 2=action, 3=ai
    healing_strategy: str = ""                  # e.g. "text_similarity", "focus_enter"
    healing_elapsed_ms: int = 0
    selector_repaired: bool = False
    action_repaired: bool = False


class StepTelemetry(BaseModel):
    """Complete telemetry for one step within a test run.

    This is the primary telemetry record. One per step per run.
    """
    step_number: int
    action: str = ""
    status: str = ""               # passed/failed/error

    # Phase-level timing
    timings: PhaseTimings = Field(default_factory=PhaseTimings)

    # All execution attempts (includes retries)
    attempts: list[ExecutionAttempt] = Field(default_factory=list)
    total_attempts: int = 0
    used_custom_code: bool = False

    # Code reuse tracking
    reused_code: bool = False              # True if step used cached code (no AI gen)
    code_hash: str = ""                    # SHA256 hash of the executed code
    regeneration_reason: str = ""          # Why code was regenerated (if applicable)

    # AI service metrics for all calls in this step
    ai_calls: list[AICallMetrics] = Field(default_factory=list)

    # Selector analysis (extracted from generated code)
    selectors_tried: list[str] = Field(default_factory=list)
    selector_that_worked: str = ""
    used_selector_fallback: bool = False   # True if fallback selector was needed
    selectors_structured: list[dict] = Field(default_factory=list)  # Full strategy+params dicts

    # Evidence metadata
    evidence_size_bytes: int = 0
    evidence_upload_ms: int = 0

    # Selector resolution metrics (Part 1, 9)
    selector_scores: list[dict] = Field(default_factory=list)  # CandidateScore.to_dict()
    disambiguation_used: bool = False
    disambiguation_layer: str = ""

    # Execution budget metrics (Part 5, 9)
    budget_breakdown: dict = Field(default_factory=dict)  # StepExecutionBudget.get_breakdown()
    budget_exceeded: bool = False

    # AI dependency metrics (Part 8, 9)
    ai_gate_metrics: dict = Field(default_factory=dict)  # AIGate.get_metrics()

    # Recovery diagnosis metrics (Part 2, 9)
    failure_diagnoses: list[dict] = Field(default_factory=list)  # FailureDiagnosis.to_dict()
    recovery_actions_taken: list[dict] = Field(default_factory=list)

    # Verification outcome (Phase 5)
    verification_level: str = ""               # final verification mode: "ai", "dom-only", "behavior_override", "skipped", "inconclusive"


class EnvironmentSnapshot(BaseModel):
    """Captured once per test run at browser launch.

    Provides the execution context needed to reproduce a flake.
    """
    browser_name: str = ""         # "chromium"
    browser_version: str = ""
    viewport_width: int = 0
    viewport_height: int = 0
    platform: str = ""             # "linux", "darwin"
    user_agent: str = ""
    locale: str = ""
    timezone: str = ""
    headless: bool = True
    worker_hostname: str = ""
    worker_pid: int = 0
    chromium_args: list[str] = Field(default_factory=list)
    default_timeout_ms: int = 0
    default_nav_timeout_ms: int = 0


class RunTelemetry(BaseModel):
    """Top-level telemetry document stored per test run.

    Stored in `execution_telemetry` collection.
    One document per test run. Contains environment + all step telemetry.
    """
    id: ObjectId | None = Field(default=None, alias="_id")
    test_run_id: str
    org_id: str
    test_type: str = "ui"          # "ui" or "api"

    # Environment context (captured at run start)
    environment: EnvironmentSnapshot = Field(default_factory=EnvironmentSnapshot)

    # Per-step telemetry
    steps: list[StepTelemetry] = Field(default_factory=list)

    # Run-level aggregates
    total_ai_calls: int = 0
    total_ai_latency_ms: int = 0
    total_retries: int = 0
    flake_indicators: list[str] = Field(default_factory=list)

    # Confidence scoring (computed at finalization)
    confidence: dict = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str},
    }
