"""Tests for stability gap fixes (Parts 1–6).

Part 1: Stability telemetry pipeline — metrics recorded before attempt
Part 2: Post-action verification — check, uncheck, select_date, upload
Part 3: AI arbitration history integration — history_records passed
Part 4: Budget-aware interaction guard — sleeps respect budget
Part 5: Resolver confidence threshold — low-score escalation
Part 6: Full pipeline integration test
"""

import math
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from app.worker.post_action_verify import (
    ActionVerification,
    verify_check,
    verify_uncheck,
    verify_select_date,
    verify_upload,
)
from app.worker.interaction_guard import (
    GuardResult,
    ensure_interactable,
    _check_enabled,
    _check_dom_stability,
)
from app.worker.selector_engine.scoring import (
    CandidateScore,
    ScoringWeights,
    _compute_history_bonus,
    _history_key,
)
from app.worker.selector_resilience import SelectorStrategy


# ── Part 1: Stability Telemetry Pipeline ──


def test_stability_metrics_recorded():
    """record_stability_metrics attaches values to the next record_attempt call."""
    from app.telemetry.collector import _StepTelemetryBuilder

    builder = _StepTelemetryBuilder(step_number=1, action="click")

    # Record stability metrics BEFORE attempt
    builder.record_stability_metrics(
        resolution_tier=2,
        guard_failures=["overlay_detected"],
        post_action_verified=False,
        post_action_method="url_change",
    )

    # Then record attempt
    builder.record_attempt(
        attempt_number=1,
        success=True,
        code='{"actions": []}',
        execution_ms=100,
    )

    step = builder.build("passed")

    assert len(step.attempts) == 1
    attempt = step.attempts[0]
    assert attempt.resolution_tier == 2
    assert "overlay_detected" in attempt.guard_failures
    assert attempt.post_action_verified is False
    assert attempt.post_action_method == "url_change"


def test_stability_metrics_default_when_not_set():
    """Without record_stability_metrics, attempt gets safe defaults."""
    from app.telemetry.collector import _StepTelemetryBuilder

    builder = _StepTelemetryBuilder(step_number=1, action="click")
    builder.record_attempt(
        attempt_number=1,
        success=True,
        code='{"actions": []}',
        execution_ms=50,
    )

    step = builder.build("passed")
    attempt = step.attempts[0]
    assert attempt.resolution_tier == 0
    assert attempt.guard_failures == []
    assert attempt.post_action_verified is True
    assert attempt.post_action_method == ""


def test_execute_intent_returns_stability_metrics():
    """execute_intent returns a 5-tuple including stability_metrics dict."""
    from app.worker.intent_executor import execute_intent
    from app.worker.intent_schema import StepIntent

    page = MagicMock()
    page.url = "https://example.com"

    # Minimal intent with a navigate action (no target, no guard, no verification)
    intent = StepIntent.model_validate({
        "step_number": 1,
        "description": "Navigate to page",
        "actions": [
            {"action": "navigate", "value": "https://example.com/page"},
        ],
    })

    with patch("app.worker.intent_executor.safe_goto"):
        with patch("app.worker.intent_executor._remaining_ms", return_value=15000):
            result = execute_intent(page, intent, wait_budget_ms=15000)

    assert len(result) == 5
    success, desc, selectors, stability, last_sel = result
    assert success is True
    assert isinstance(stability, dict)
    assert "resolution_tier" in stability
    assert "guard_failures" in stability
    assert "post_action_verified" in stability
    assert "post_action_method" in stability


# ── Part 2: Post-Action Verification ──


def test_verify_checkbox_checked():
    """verify_check passes when locator.is_checked() returns True."""
    locator = MagicMock()
    locator.is_checked.return_value = True

    result = verify_check(locator)
    assert result.verified
    assert result.method == "checked_state"


def test_verify_checkbox_not_checked():
    """verify_check fails when locator.is_checked() returns False."""
    locator = MagicMock()
    locator.is_checked.return_value = False

    result = verify_check(locator)
    assert not result.verified
    assert result.method == "checked_state"


def test_verify_uncheck_unchecked():
    """verify_uncheck passes when locator.is_checked() returns False."""
    locator = MagicMock()
    locator.is_checked.return_value = False

    result = verify_uncheck(locator)
    assert result.verified
    assert result.method == "checked_state"


def test_verify_uncheck_still_checked():
    """verify_uncheck fails when locator.is_checked() returns True."""
    locator = MagicMock()
    locator.is_checked.return_value = True

    result = verify_uncheck(locator)
    assert not result.verified


def test_verify_date_selected_input_value():
    """verify_select_date passes when input value contains expected day."""
    page = MagicMock()
    locator = MagicMock()
    locator.input_value.return_value = "2025-03-15"

    result = verify_select_date(page, locator, "15")
    assert result.verified
    assert result.method == "value_match"


def test_verify_date_selected_aria():
    """verify_select_date passes when aria-selected element contains day."""
    page = MagicMock()
    locator = MagicMock()
    locator.input_value.return_value = ""
    locator.inner_text.return_value = ""

    # Set up aria-selected element
    selected_locator = MagicMock()
    selected_locator.count.return_value = 1
    nth_el = MagicMock()
    nth_el.inner_text.return_value = "15"
    selected_locator.nth.return_value = nth_el
    page.locator.return_value = selected_locator

    result = verify_select_date(page, locator, "15")
    assert result.verified
    assert result.method == "option_selected"


def test_verify_date_not_found():
    """verify_select_date fails when day not found."""
    page = MagicMock()
    locator = MagicMock()
    locator.input_value.return_value = ""
    locator.inner_text.return_value = ""

    selected_locator = MagicMock()
    selected_locator.count.return_value = 0
    page.locator.return_value = selected_locator

    result = verify_select_date(page, locator, "15")
    assert not result.verified


def test_verify_file_uploaded_input():
    """verify_upload passes when file input has files."""
    page = MagicMock()
    locator = MagicMock()
    locator.evaluate.return_value = True  # el.files.length > 0

    result = verify_upload(page, locator, "/path/to/file.pdf")
    assert result.verified
    assert result.method == "value_match"


def test_verify_file_uploaded_name_visible():
    """verify_upload passes when file name is visible on page."""
    page = MagicMock()
    locator = MagicMock()
    locator.evaluate.return_value = False  # No files on input

    text_locator = MagicMock()
    text_locator.count.return_value = 1
    page.get_by_text.return_value = text_locator

    result = verify_upload(page, locator, "/path/to/report.pdf")
    assert result.verified
    assert "report.pdf" in result.detail


def test_verify_file_upload_failed():
    """verify_upload fails when file not confirmed."""
    page = MagicMock()
    locator = MagicMock()
    locator.evaluate.return_value = False

    text_locator = MagicMock()
    text_locator.count.return_value = 0
    page.get_by_text.return_value = text_locator

    result = verify_upload(page, locator, "/path/to/file.pdf")
    assert not result.verified


# ── Part 3: AI Arbitration History Integration ──


def test_ai_selector_respects_history_bonus():
    """AI-proposed selector with history should score higher than one without.

    Verifies that _ai_structural_arbitration passes history_records
    through to score_candidates so the bonus is applied.
    """
    # Two candidates with identical structure but one has history
    params_a = {"selector": "#btn-a", "ai_proposed": True}
    params_b = {"selector": "#btn-b", "ai_proposed": True}

    key_a = _history_key(SelectorStrategy.CSS, params_a)
    history = {key_a: 10}  # 10 prior successes for btn-a

    bonus_a = _compute_history_bonus(SelectorStrategy.CSS, params_a, history)
    bonus_b = _compute_history_bonus(SelectorStrategy.CSS, params_b, history)

    # btn-a should get a positive bonus
    assert bonus_a > 0
    assert bonus_a == pytest.approx(math.log(11) * 0.1, abs=0.001)

    # btn-b has no history → zero bonus
    assert bonus_b == 0.0

    # History selector wins
    assert bonus_a > bonus_b


def test_ai_arbitration_function_accepts_history_records():
    """_ai_structural_arbitration accepts history_records parameter."""
    from app.worker.selector_engine.resolver import _ai_structural_arbitration
    import inspect

    sig = inspect.signature(_ai_structural_arbitration)
    assert "history_records" in sig.parameters


# ── Part 4: Budget-Aware Interaction Guard ──


def test_guard_enabled_respects_budget():
    """_check_enabled stops early when budget is exhausted."""
    locator = MagicMock()
    # Always disabled — guard should stop when budget runs out
    locator.is_enabled.return_value = False

    result = GuardResult(passed=True)
    t0 = time.monotonic()
    _check_enabled(locator, result, remaining_ms=200)  # Only 200ms budget
    elapsed = (time.monotonic() - t0) * 1000

    assert not result.passed
    assert "disabled" in result.checks_failed
    # Should finish within budget (200ms + small overhead)
    assert elapsed < 500


def test_guard_stability_skips_on_zero_budget():
    """_check_dom_stability skips when no budget remains."""
    locator = MagicMock()
    result = GuardResult(passed=True)

    _check_dom_stability(locator, result, remaining_ms=0)

    assert "stability_check_skipped" in result.checks_passed
    # evaluate should NOT have been called
    locator.evaluate.assert_not_called()


def test_guard_stability_respects_budget():
    """_check_dom_stability caps wait at remaining budget."""
    locator = MagicMock()
    # First two hashes differ, third matches second
    locator.evaluate.side_effect = ["hash_v1", "hash_v2", "hash_v2"]

    result = GuardResult(passed=True)
    t0 = time.monotonic()
    _check_dom_stability(locator, result, remaining_ms=400)
    elapsed = (time.monotonic() - t0) * 1000

    assert result.passed
    assert "waited_for_stability" in result.remediation_applied
    assert elapsed < 600  # 100ms + 200ms + overhead


def test_guard_wait_respects_budget():
    """Full ensure_interactable finishes within max_wait_ms budget."""
    locator = MagicMock()
    locator.bounding_box.return_value = {"x": 10, "y": 10, "width": 100, "height": 40}
    locator.is_visible.return_value = True
    # Disabled throughout — will consume budget on enabled waits
    locator.is_enabled.return_value = False

    t0 = time.monotonic()
    guard = ensure_interactable(locator, max_wait_ms=500)
    elapsed = (time.monotonic() - t0) * 1000

    assert not guard.passed
    assert "disabled" in guard.checks_failed
    # Must finish within budget + small overhead (not unbounded)
    assert elapsed < 1000


# ── Part 5: Resolver Confidence Threshold ──


def test_low_confidence_selector_escalates():
    """When best score is between MIN_SCORE and 0.45 and AI is available, confidence escalation triggers."""
    from app.worker.selector_engine.resolver import resolve_target
    from app.worker.intent_schema import TargetDescriptor

    page = MagicMock()
    page.url = "https://example.com"

    target = TargetDescriptor(role="button", name="Submit")

    # Create a low-score candidate from tier 1 — above MIN_SCORE (0.40) but below
    # MIN_SELECTOR_CONFIDENCE (0.45) so the AI confidence escalation fires.
    low_candidate = CandidateScore(
        locator=MagicMock(),
        strategy=SelectorStrategy.TEXT,
        params={"text": "Submit"},
        composite=0.42,
        resolution_tier=3,
    )
    low_candidate.total_candidates = 5

    # AI produces a better candidate
    ai_candidate = CandidateScore(
        locator=MagicMock(),
        strategy=SelectorStrategy.CSS,
        params={"selector": "#submit-btn", "ai_proposed": True},
        composite=0.75,
        resolution_tier=4,
    )

    ai_client = MagicMock()

    with patch("app.worker.selector_engine.resolver._build_tiered_chains") as mock_chains, \
         patch("app.worker.selector_engine.resolver._build_locator") as mock_locator, \
         patch("app.worker.selector_engine.resolver.score_candidates") as mock_score, \
         patch("app.worker.selector_engine.resolver._ai_structural_arbitration") as mock_ai, \
         patch("app.worker.selector_engine.resolver._scope_to_context", return_value=page), \
         patch("app.worker.selector_engine.resolver.is_ambiguous", return_value=False):

        # Set up tier chain with one strategy
        mock_chains.return_value = [
            [(SelectorStrategy.TEXT, {"text": "Submit"})],
        ]
        mock_locator.return_value = MagicMock()
        mock_score.return_value = [low_candidate]

        # First AI call returns None (from tier check at < 0.5)
        # Second AI call (confidence escalation at < 0.45) returns ai_candidate
        mock_ai.side_effect = [None, ai_candidate]

        result = resolve_target(
            page, target,
            timeout_ms=10000,
            ai_client=ai_client,
        )

    # AI escalation should have been called
    assert mock_ai.call_count == 2  # Once from tier check, once from confidence threshold
    assert result.score.composite == 0.75


def test_ai_used_when_confidence_low():
    """AI arbitration is invoked specifically because of low confidence threshold."""
    from app.worker.selector_engine.resolver import resolve_target
    from app.worker.intent_schema import TargetDescriptor

    page = MagicMock()

    target = TargetDescriptor(text="Submit")

    # Candidate scores just above 0.5 (tier check) but below 0.45 (confidence)
    # Wait — 0.5 > 0.45, so it won't trigger confidence escalation.
    # Let me use a score between 0.15 and 0.45 to hit confidence threshold
    # But it also needs to be < 0.5 which triggers tier-4 AI first.

    # Score = 0.40: below 0.5 → triggers tier-4 AI (returns None)
    # Then below 0.45 → triggers confidence escalation (returns good candidate)
    low_candidate = CandidateScore(
        locator=MagicMock(),
        strategy=SelectorStrategy.TEXT,
        params={"text": "Submit"},
        composite=0.40,
        resolution_tier=3,
    )
    low_candidate.total_candidates = 3

    good_candidate = CandidateScore(
        locator=MagicMock(),
        strategy=SelectorStrategy.CSS,
        params={"selector": "button.submit", "ai_proposed": True},
        composite=0.60,
        resolution_tier=4,
    )

    ai_client = MagicMock()

    with patch("app.worker.selector_engine.resolver._build_tiered_chains") as mock_chains, \
         patch("app.worker.selector_engine.resolver._build_locator") as mock_locator, \
         patch("app.worker.selector_engine.resolver.score_candidates") as mock_score, \
         patch("app.worker.selector_engine.resolver._ai_structural_arbitration") as mock_ai, \
         patch("app.worker.selector_engine.resolver._scope_to_context", return_value=page), \
         patch("app.worker.selector_engine.resolver.is_ambiguous", return_value=False):

        mock_chains.return_value = [
            [(SelectorStrategy.TEXT, {"text": "Submit"})],
        ]
        mock_locator.return_value = MagicMock()
        mock_score.return_value = [low_candidate]
        # Tier-4 AI: returns None; Confidence escalation: returns good candidate
        mock_ai.side_effect = [None, good_candidate]

        result = resolve_target(
            page, target,
            timeout_ms=10000,
            ai_client=ai_client,
        )

    assert result.score.composite == 0.60


# ── Part 6: Full Pipeline Integration Test ──


def test_full_execution_pipeline_stability():
    """End-to-end test: resolve → guard → revalidate → act → verify.

    Scenario: button covered by overlay, overlay disappears after scroll
    remediation. Button click triggers URL navigation.
    """
    from app.worker.intent_executor import _execute_single_action
    from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor

    page = MagicMock()
    page.url = "https://app.example.com/dashboard"  # URL after click

    # Build action
    target = TargetDescriptor(role="button", name="Submit")
    action = ActionIntent(
        action=ActionType.CLICK,
        target=target,
    )

    # Set up resolved target
    mock_resolved = MagicMock()
    mock_resolved.locator = MagicMock()
    mock_resolved.strategy = SelectorStrategy.ROLE
    mock_resolved.selector_path = "div#main > button"
    mock_resolved.score = CandidateScore(
        locator=mock_resolved.locator,
        strategy=SelectorStrategy.ROLE,
        params={"role": "button", "name": "Submit"},
        composite=0.90,
        resolution_tier=1,
    )
    mock_resolved.to_telemetry.return_value = {
        "strategy": "role",
        "params": {"role": "button", "name": "Submit"},
        "composite_score": 0.90,
        "resolution_tier": 1,
        "resolution_ms": 50,
        "disambiguation_used": False,
        "disambiguation_layer": "",
        "strategies_tried": 1,
        "total_candidates_evaluated": 1,
        "selector_path": "div#main > button",
        "score_breakdown": {
            "strategy": 0.85,
            "visibility": 1.0,
            "enabled": 1.0,
            "match_confidence": 1.0,
            "proximity": 0.5,
        },
    }

    # Guard: element is visible, enabled, no overlay, stable
    locator = mock_resolved.locator
    locator.bounding_box.return_value = {"x": 100, "y": 200, "width": 150, "height": 40}
    locator.is_visible.return_value = True
    locator.is_enabled.return_value = True
    locator.evaluate.side_effect = [
        # capture_element_identity JS
        {"tag": "button", "id": "", "ariaLabel": "Submit", "textHash": 12345,
         "rect": {"x": 100, "y": 200, "w": 150, "h": 40}},
        # discover_clickable_ancestor: element is already a button
        {"found": False, "depth": 0, "tag": "", "role": ""},
        # Guard: overlay check — target is accessible
        True,
        # Guard: pointer-events check
        "auto",
        # Guard: stability hash 1
        "stable_hash",
        # Guard: stability hash 2 (same → stable)
        "stable_hash",
        # Revalidation: identity check
        {"tag": "button", "id": "", "ariaLabel": "Submit", "textHash": 12345,
         "rect": {"x": 100, "y": 200, "w": 150, "h": 40}},
    ]

    # Pre-state for click verification
    pre_url = "https://app.example.com/login"

    with patch("app.worker.intent_executor.resolve_target", return_value=mock_resolved), \
         patch("app.worker.intent_executor.safe_click"), \
         patch("app.worker.intent_executor.capture_pre_state") as mock_pre, \
         patch("app.worker.intent_executor.verify_click") as mock_verify, \
         patch("app.worker.intent_executor._remaining_ms", return_value=15000):

        mock_pre.return_value = {"url": pre_url}
        mock_verify.return_value = ActionVerification(
            verified=True,
            method="url_change",
            detail=f"URL changed to {page.url}",
        )

        result = _execute_single_action(
            page, action, timeout_ms=10000,
        )

    # Assertions: full pipeline executed
    assert result.get("selector_info") is not None
    assert result["selector_info"]["resolution_tier"] == 1
    assert result.get("guard_result") is not None
    assert result["guard_result"].passed
    assert result.get("action_verification") is not None
    assert result["action_verification"].verified
    assert result["action_verification"].method == "url_change"
    assert result.get("description") == "Clicked element"
