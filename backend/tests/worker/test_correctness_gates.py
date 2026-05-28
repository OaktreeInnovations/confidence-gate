"""Phase 8 killer tests — correctness gate verification.

Each test targets a specific false-positive scenario that the P0 rewrite
was designed to eliminate. These tests MUST FAIL (return non-pass status)
for the scenarios described.

Scenarios:
  1. Wrong element clicked but vision says passed → MUST return inconclusive/failed
  2. Loading spinner visible → MUST NOT return passed
  3. Action executed but no observable DOM effect → MUST return inconclusive
  4. Healing attempted but never succeeds → score MUST be penalised
  5. Behavior override with weak keyword match → MUST return inconclusive, not passed
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.telemetry.models import (
    ExecutionAttempt,
    PhaseTimings,
    StepTelemetry,
)
from app.telemetry.confidence_scorer import compute_step_confidence


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_attempt(
    *,
    success: bool = True,
    behavior_failed: bool = False,
    behavior_failures: int = 0,
    healing_attempted: bool = False,
    healing_success: bool = False,
) -> ExecutionAttempt:
    return ExecutionAttempt(
        attempt_number=1,
        success=success,
        behavior_failed=behavior_failed,
        behavior_failures=behavior_failures,
        healing_attempted=healing_attempted,
        healing_success=healing_success,
    )


def _make_step(
    *,
    status: str = "passed",
    verification_level: str = "ai",
    total_attempts: int = 1,
    attempts: list[ExecutionAttempt] | None = None,
    verification_ms: int = 100,
) -> StepTelemetry:
    if attempts is None:
        attempts = [_make_attempt()]
    return StepTelemetry(
        step_number=1,
        action="click Sign In",
        status=status,
        verification_level=verification_level,
        total_attempts=total_attempts,
        attempts=attempts,
        timings=PhaseTimings(verification_ms=verification_ms, total_ms=2000),
    )


# ── Test 1: Inconclusive step → confidence must be 0 ─────────────────────────


class TestInconclusiveStepConfidence:
    """An inconclusive step must never contribute to run confidence."""

    def test_inconclusive_step_scores_zero(self):
        """compute_step_confidence must return 0.0 for inconclusive steps."""
        step = _make_step(status="inconclusive", verification_level="behavior_override")
        score = compute_step_confidence(step)
        assert score == 0.0, (
            f"Inconclusive step returned confidence {score} — "
            "must be 0.0 to prevent false positive run scores"
        )

    def test_failed_step_scores_zero(self):
        """A failed step must score 0.0."""
        step = _make_step(status="failed")
        score = compute_step_confidence(step)
        assert score == 0.0

    def test_passed_step_scores_above_zero(self):
        """A clean passed step with AI verification must score above 0.6."""
        step = _make_step(status="passed", verification_level="ai")
        score = compute_step_confidence(step)
        assert score > 0.6, f"Clean passed step scored {score} — suspiciously low"


# ── Test 2: Loading spinner → must not pass ───────────────────────────────────


class TestVisionSpinnerNotPass:
    """Vision prompt must return inconclusive for loading states, not passed."""

    def test_vision_prompt_rejects_spinner(self):
        """The vision system prompt must not contain 'mark as PASSED' for spinners."""
        from app.worker.ai_executor import VISION_SYSTEM_PROMPT

        # The old bad instruction was:
        # "mark as PASSED — the action was executed and the page is processing"
        assert "spinner" not in VISION_SYSTEM_PROMPT.lower() or (
            "inconclusive" in VISION_SYSTEM_PROMPT.lower()
        ), (
            "Vision prompt still tells AI to PASS on loading spinners without "
            "first offering 'inconclusive' as the response"
        )

        # The new instruction must use "inconclusive" for loading states
        assert "inconclusive" in VISION_SYSTEM_PROMPT, (
            "Vision prompt must include 'inconclusive' as a valid response "
            "for loading/transitional page states"
        )

    def test_vision_prompt_removed_lean_toward_passed(self):
        """The 'lean toward PASSED' instruction must be removed."""
        from app.worker.ai_executor import VISION_SYSTEM_PROMPT

        assert "lean toward passed" not in VISION_SYSTEM_PROMPT.lower(), (
            "'lean toward PASSED' still present in vision prompt — "
            "this causes false positives on error pages"
        )


# ── Test 3: Behavior override — weak match → MUST be inconclusive ────────────


class TestBehaviorOverrideThreshold:
    """behavior_override must not fire on weak keyword matches."""

    def test_behavior_override_penalty_applied(self):
        """A step that passed via behavior_override must be penalised in confidence."""
        step = _make_step(status="passed", verification_level="behavior_override")
        score = compute_step_confidence(step)

        # A clean AI-verified step of the same profile
        clean_step = _make_step(status="passed", verification_level="ai")
        clean_score = compute_step_confidence(clean_step)

        assert score < clean_score, (
            f"behavior_override step scored {score} vs clean AI step {clean_score} — "
            "override must be penalised vs full AI verification"
        )

    def test_behavior_override_score_well_below_ai(self):
        """behavior_override confidence must be meaningfully lower than AI-verified."""
        step = _make_step(status="passed", verification_level="behavior_override")
        score = compute_step_confidence(step)

        # With _BEHAVIOR_OVERRIDE_PENALTY=0.25 and multiplier=0.85, the score
        # should be no higher than ~0.70 for a clean otherwise-perfect step
        assert score <= 0.70, (
            f"behavior_override step scored {score} — too high; "
            "override penalty must meaningfully reduce confidence"
        )


# ── Test 4: No-behavior = INCONCLUSIVE signal in scoring ─────────────────────


class TestBehaviorFailurePenalty:
    """Steps where the action had no observable DOM effect must be penalised."""

    def test_behavior_failed_attempt_penalises_score(self):
        """A passed step with a behavior-failed attempt must score lower."""
        clean = _make_step(
            status="passed",
            verification_level="ai",
            attempts=[_make_attempt(behavior_failed=False)],
        )
        with_behavior_fail = _make_step(
            status="passed",
            verification_level="ai",
            attempts=[_make_attempt(behavior_failed=True, behavior_failures=1)],
        )

        clean_score = compute_step_confidence(clean)
        fail_score = compute_step_confidence(with_behavior_fail)

        assert fail_score < clean_score, (
            f"Behavior-failed step scored {fail_score} vs clean {clean_score} — "
            "behavior failure must reduce confidence"
        )

    def test_behavior_failure_penalty_magnitude(self):
        """The behavior failure penalty must be at least 0.15."""
        clean = _make_step(
            status="passed",
            verification_level="ai",
            attempts=[_make_attempt(behavior_failed=False)],
        )
        fail = _make_step(
            status="passed",
            verification_level="ai",
            attempts=[_make_attempt(behavior_failed=True, behavior_failures=2)],
        )

        delta = compute_step_confidence(clean) - compute_step_confidence(fail)
        assert delta >= 0.15, (
            f"Behavior failure only reduced confidence by {delta:.3f} — "
            "must be at least 0.15 to be meaningful"
        )


# ── Test 5: Healing silent fail → score penalised ────────────────────────────


class TestHealingFailurePenalty:
    """Healing that is attempted but never succeeds must cost more than healing that works."""

    def test_healing_failure_penalised_more_than_success(self):
        """Failed healing must score lower than successful healing."""
        healed = _make_step(
            status="passed",
            verification_level="ai",
            attempts=[_make_attempt(healing_attempted=True, healing_success=True)],
        )
        not_healed = _make_step(
            status="passed",
            verification_level="ai",
            attempts=[_make_attempt(healing_attempted=True, healing_success=False)],
        )

        healed_score = compute_step_confidence(healed)
        not_healed_score = compute_step_confidence(not_healed)

        assert not_healed_score < healed_score, (
            f"Failed healing scored {not_healed_score} vs successful healing {healed_score} — "
            "failed healing must cost more than successful healing"
        )

    def test_healing_failed_penalty_magnitude(self):
        """The extra penalty for failed healing must be at least 0.10."""
        healed = _make_step(
            status="passed",
            verification_level="ai",
            attempts=[_make_attempt(healing_attempted=True, healing_success=True)],
        )
        not_healed = _make_step(
            status="passed",
            verification_level="ai",
            attempts=[_make_attempt(healing_attempted=True, healing_success=False)],
        )

        delta = compute_step_confidence(healed) - compute_step_confidence(not_healed)
        assert delta >= 0.10, (
            f"Failed healing only costs {delta:.3f} more than successful healing — "
            "must be at least 0.10"
        )


# ── Test 6: MIN_SCORE threshold in resolver ───────────────────────────────────


class TestResolverMinScore:
    """Resolver must refuse to use candidates below MIN_SCORE=0.40."""

    def test_resolver_raises_below_min_score(self):
        """resolve_target must raise when best candidate < 0.40."""
        from app.worker.selector_engine.resolver import resolve_target
        from app.worker.intent_schema import TargetDescriptor
        from app.worker.selector_engine.scoring import CandidateScore
        from app.worker.selector_resilience import SelectorStrategy

        page = MagicMock()
        page.url = "https://example.com"
        target = TargetDescriptor(role="button", name="Sign In")

        weak_candidate = CandidateScore(
            locator=MagicMock(),
            strategy=SelectorStrategy.TEXT,
            params={"text": "Sign out"},
            composite=0.15,  # < MIN_SCORE
            resolution_tier=3,
        )
        weak_candidate.total_candidates = 1

        with patch("app.worker.selector_engine.resolver._build_tiered_chains") as mock_chains, \
             patch("app.worker.selector_engine.resolver._build_locator") as mock_locator, \
             patch("app.worker.selector_engine.resolver.score_candidates") as mock_score, \
             patch("app.worker.selector_engine.resolver._scope_to_context", return_value=page), \
             patch("app.worker.selector_engine.resolver.is_ambiguous", return_value=False):

            mock_chains.return_value = [
                [(SelectorStrategy.TEXT, {"text": "Sign out"})],
            ]
            mock_locator.return_value = MagicMock()
            mock_score.return_value = [weak_candidate]

            with pytest.raises(RuntimeError, match="No viable selector found"):
                resolve_target(page, target, timeout_ms=5000, ai_client=None)

    def test_resolver_accepts_above_min_score(self):
        """resolve_target must succeed when best candidate >= 0.40."""
        from app.worker.selector_engine.resolver import resolve_target
        from app.worker.intent_schema import TargetDescriptor
        from app.worker.selector_engine.scoring import CandidateScore
        from app.worker.selector_resilience import SelectorStrategy

        page = MagicMock()
        page.url = "https://example.com"
        target = TargetDescriptor(role="button", name="Sign In")

        strong_candidate = CandidateScore(
            locator=MagicMock(),
            strategy=SelectorStrategy.TEXT,
            params={"text": "Sign In"},
            composite=0.85,
            resolution_tier=1,
        )
        strong_candidate.total_candidates = 1

        with patch("app.worker.selector_engine.resolver._build_tiered_chains") as mock_chains, \
             patch("app.worker.selector_engine.resolver._build_locator") as mock_locator, \
             patch("app.worker.selector_engine.resolver.score_candidates") as mock_score, \
             patch("app.worker.selector_engine.resolver._scope_to_context", return_value=page), \
             patch("app.worker.selector_engine.resolver.is_ambiguous", return_value=False):

            mock_chains.return_value = [
                [(SelectorStrategy.TEXT, {"text": "Sign In"})],
            ]
            mock_locator.return_value = MagicMock()
            mock_score.return_value = [strong_candidate]

            result = resolve_target(page, target, timeout_ms=5000, ai_client=None)
            assert result.score.composite >= 0.40


# ── Test 7: Gate rules in final_score_engine ─────────────────────────────────


class TestFinalScoreGates:
    """Hard gate rules must override numeric score when quality failures occur."""

    def test_inconclusive_steps_force_block(self):
        """Any inconclusive step in a batch must force recommendation to 'block'."""
        from bson import ObjectId
        from app.intelligence.final_score_engine import compute_final_score

        org_id = str(ObjectId())
        batch_id = "batch-test-001"
        project_id = str(ObjectId())
        run_id = str(ObjectId())

        # Runs with one inconclusive step
        runs = [
            {
                "_id": ObjectId(run_id),
                "status": "failed",
                "batch_id": batch_id,
                "org_id": ObjectId(org_id),
                "results": [
                    {"step_number": 1, "status": "passed"},
                    {"step_number": 2, "status": "inconclusive"},
                ],
            }
        ]

        db = MagicMock()
        db.test_runs.find.return_value = runs
        db.execution_telemetry.find.return_value = []
        db.execution_profiles.find.return_value = []
        db.release_validations.find_one.return_value = None
        db.projects.find_one.return_value = None
        db.test_cases.find.return_value = iter([])

        # Mock the base score engine to return a passing score
        with patch("app.intelligence.final_score_engine._base_score") as mock_base, \
             patch("app.intelligence.final_score_engine.compute_instability", return_value={"instability_penalty": 0, "instability_index": 0, "components": {}}), \
             patch("app.intelligence.final_score_engine.compute_coverage", return_value={"coverage_penalty": 0, "coverage_score": 1.0}), \
             patch("app.intelligence.final_score_engine.compute_risk_adjustment", return_value={"risk_adjustment": 0, "high_risk_steps": []}), \
             patch("app.intelligence.final_score_engine.detect_anomalies", return_value=[]), \
             patch("app.intelligence.final_score_engine.compute_delta", return_value={"score_delta": None, "pass_rate_delta": None, "reasons": [], "has_previous": False}), \
             patch("app.intelligence.final_score_engine.compute_trajectory", return_value={"trend": "stable", "scores": [], "labels": []}), \
             patch("app.intelligence.final_score_engine.compute_score_confidence", return_value={"score_confidence": 0.8, "data_quality": "HIGH"}), \
             patch("app.intelligence.final_score_engine.compute_failure_probability", return_value={"predicted_failure_probability": 0.1, "calibration_confidence": 0.5, "calibration_source": "model"}), \
             patch("app.intelligence.final_score_engine.apply_time_decay", return_value={"freshness": "fresh", "decayed_score": 85, "hours_since_run": 1.0}):

            mock_report = MagicMock()
            mock_report.confidence_score = 85
            mock_report.score_version = "v2"
            mock_report.trend = "stable"
            mock_report.historical_confidence = 0.9
            mock_report.risk_delta = 0
            mock_report.blockers = []
            mock_report.to_dict.return_value = {
                "per_run_results": [],
                "signals": [],
                "root_causes": [],
                "blockers": [],
                "batch_summary": {},
                "feature_risks": [],
                "flaky_tests": [],
                "timing_anomalies": [],
                "healing_penalty": 0,
            }
            mock_base.return_value = mock_report

            result = compute_final_score(
                db=db,
                org_id=org_id,
                batch_id=batch_id,
                project_id=project_id,
            )

        assert result["recommendation"] == "block", (
            f"Got recommendation='{result['recommendation']}' with inconclusive steps — "
            "must be 'block'"
        )
        assert result["confidence_score"] < 70, (
            f"Score {result['confidence_score']} should be below 70 when inconclusive steps exist"
        )
        assert result["inconclusive_steps"] == 1
        assert len(result["gate_blocks"]) > 0
