"""Data models for execution intelligence.

ExecutionProfile aggregates historical execution data for a test case,
providing pass rates, flake rates, timing distributions, and per-step
analysis. Built from existing telemetry + test_runs data.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from pydantic import BaseModel, Field


class FailureRecord(BaseModel):
    """A classified failure type with its frequency."""
    reason: str          # RetryReason value (e.g. "selector_not_found")
    count: int = 0
    example_message: str = ""  # Most recent error message for context


class StepProfile(BaseModel):
    """Aggregated execution profile for a single step across runs."""
    step_number: int
    action: str = ""

    # Timing distributions (ms)
    median_exec_ms: int = 0
    p95_exec_ms: int = 0
    min_exec_ms: int = 0
    max_exec_ms: int = 0

    # Reliability
    pass_rate: float = 0.0       # 0.0 - 1.0
    retry_rate: float = 0.0      # fraction of runs where this step needed retries
    avg_attempts: float = 1.0    # average number of attempts per run

    # Failure analysis
    most_common_failures: list[FailureRecord] = Field(default_factory=list)

    # Selector tracking
    successful_selectors: list[str] = Field(default_factory=list)
    selector_success_rates: dict[str, float] = Field(default_factory=dict)

    # Sample size
    total_observations: int = 0

    # Ambiguous step detection
    ambiguous_step_flag: bool = False
    ambiguous_step_reason: str = ""


class ExecutionProfile(BaseModel):
    """Aggregated execution profile for a test case.

    Stored in `execution_profiles` collection.
    One document per (test_case_id, org_id). Updated after each run.
    """
    id: ObjectId | None = Field(default=None, alias="_id")
    test_case_id: str
    org_id: str

    # Configuration
    window_size: int = 20        # Number of recent runs analyzed

    # Run-level metrics
    total_runs: int = 0
    pass_rate: float = 0.0       # 0.0 - 1.0
    flake_rate: float = 0.0      # fraction of runs with flake indicators
    error_rate: float = 0.0      # fraction of runs that errored (timeout, crash)

    # Timing distributions (ms) — from test_runs.duration_ms
    median_duration_ms: int = 0
    p95_duration_ms: int = 0
    min_duration_ms: int = 0
    max_duration_ms: int = 0

    # Per-step profiles
    step_profiles: list[StepProfile] = Field(default_factory=list)

    # Most problematic step (highest retry_rate or lowest pass_rate)
    weakest_step: int | None = None

    # Trend: is the test getting more or less reliable?
    # Positive = improving, negative = degrading
    reliability_trend: float = 0.0

    # Flake analysis (computed by flake_detector)
    flake_report: dict = Field(default_factory=dict)

    # Timing baselines per step (computed by timing_analyzer)
    timing_baselines: dict = Field(default_factory=dict)

    # Run IDs included in this profile (for traceability)
    run_ids: list[str] = Field(default_factory=list)

    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str},
    }
