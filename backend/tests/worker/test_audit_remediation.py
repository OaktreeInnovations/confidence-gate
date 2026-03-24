"""Tests for audit remediation fixes (P0/P1/P2).

P0-1: Unverified click soft retry
P0-2: Unstable CSS selector rejection filter
P1-1: pointer-events CSS check in interaction guard
P1-2: Bounding box stability check (layout shift detection)
P2-1: Whitespace normalization in attribute alignment
"""

import time
from unittest.mock import MagicMock, patch

from app.worker.interaction_guard import (
    GuardResult,
    _check_pointer_events,
    _check_bbox_stability,
    ensure_interactable,
)
from app.worker.selector_resilience import SelectorStrategy, _is_unstable_selector
from app.worker.selector_engine.scoring import _attribute_alignment, _normalize_ws


# ── P0-1: Unverified Click Soft Retry ──


def test_unverified_click_triggers_soft_retry():
    """When verify_click returns verified=False, a soft retry is attempted."""
    from app.worker.intent_executor import _execute_single_action
    from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
    from app.worker.post_action_verify import ActionVerification
    from app.worker.selector_engine.scoring import CandidateScore

    page = MagicMock()
    page.url = "https://example.com"

    target = TargetDescriptor(role="button", name="Submit")
    action = ActionIntent(action=ActionType.CLICK, target=target)

    mock_resolved = MagicMock()
    mock_resolved.locator = MagicMock()
    mock_resolved.strategy = SelectorStrategy.ROLE
    mock_resolved.selector_path = "div > button"
    mock_resolved.score = CandidateScore(
        locator=mock_resolved.locator,
        strategy=SelectorStrategy.ROLE,
        params={"role": "button", "name": "Submit"},
        composite=0.90,
        resolution_tier=1,
    )
    mock_resolved.to_telemetry.return_value = {
        "strategy": "role", "params": {}, "composite_score": 0.90,
        "resolution_tier": 1, "resolution_ms": 50,
        "disambiguation_used": False, "disambiguation_layer": "",
        "strategies_tried": 1, "total_candidates_evaluated": 1,
        "selector_path": "div > button",
        "score_breakdown": {},
    }

    locator = mock_resolved.locator
    locator.bounding_box.return_value = {"x": 10, "y": 10, "width": 100, "height": 40}
    locator.is_visible.return_value = True
    locator.is_enabled.return_value = True

    _identity_dict = {"tag": "button", "id": "", "ariaLabel": "Submit",
                      "textHash": 12345, "rect": {"x": 10, "y": 10, "w": 100, "h": 40}}

    def evaluate_side_effect(js, *args, **kwargs):
        if "elementFromPoint" in js:
            return True
        if "charCodeAt" in js and "tagName" not in js:
            return 12345
        if "pointerEvents" in js:
            return "auto"
        if "tagName" in js and "textHash" in js:
            return _identity_dict
        return _identity_dict

    locator.evaluate.side_effect = evaluate_side_effect

    # First verify_click: not verified. After retry: verified.
    verify_results = [
        ActionVerification(verified=False, method="none", detail="No observable state change"),
        ActionVerification(verified=True, method="url_change", detail="URL changed"),
    ]
    verify_call_count = {"n": 0}

    def mock_verify(*args, **kwargs):
        idx = verify_call_count["n"]
        verify_call_count["n"] += 1
        return verify_results[min(idx, len(verify_results) - 1)]

    with patch("app.worker.intent_executor.resolve_target", return_value=mock_resolved), \
         patch("app.worker.intent_executor.safe_click"), \
         patch("app.worker.intent_executor.verify_click", side_effect=mock_verify), \
         patch("app.worker.intent_executor.capture_pre_state", return_value={"url": "https://example.com"}), \
         patch("app.worker.intent_executor._remaining_ms", return_value=15000):

        result = _execute_single_action(page, action, timeout_ms=10000)

    # verify_click called twice (initial + retry)
    assert verify_call_count["n"] == 2
    # Final verification should be the successful retry
    assert result["action_verification"].verified


def test_unverified_click_no_retry_when_already_verified():
    """When verify_click returns verified=True, no retry occurs."""
    from app.worker.intent_executor import _execute_single_action
    from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
    from app.worker.post_action_verify import ActionVerification
    from app.worker.selector_engine.scoring import CandidateScore

    page = MagicMock()
    page.url = "https://example.com/next"

    target = TargetDescriptor(role="button", name="Next")
    action = ActionIntent(action=ActionType.CLICK, target=target)

    mock_resolved = MagicMock()
    mock_resolved.locator = MagicMock()
    mock_resolved.strategy = SelectorStrategy.ROLE
    mock_resolved.selector_path = "button"
    mock_resolved.score = CandidateScore(
        locator=mock_resolved.locator,
        strategy=SelectorStrategy.ROLE,
        params={"role": "button", "name": "Next"},
        composite=0.90,
        resolution_tier=1,
    )
    mock_resolved.to_telemetry.return_value = {
        "strategy": "role", "params": {}, "composite_score": 0.90,
        "resolution_tier": 1, "resolution_ms": 50,
        "disambiguation_used": False, "disambiguation_layer": "",
        "strategies_tried": 1, "total_candidates_evaluated": 1,
        "selector_path": "button",
        "score_breakdown": {},
    }

    locator = mock_resolved.locator
    locator.bounding_box.return_value = {"x": 10, "y": 10, "width": 100, "height": 40}
    locator.is_visible.return_value = True
    locator.is_enabled.return_value = True

    _identity_dict = {"tag": "button", "id": "", "ariaLabel": "Next",
                      "textHash": 999, "rect": {"x": 10, "y": 10, "w": 100, "h": 40}}

    def evaluate_side_effect(js, *args, **kwargs):
        if "elementFromPoint" in js:
            return True
        if "charCodeAt" in js and "tagName" not in js:
            return 12345
        if "pointerEvents" in js:
            return "auto"
        if "tagName" in js and "textHash" in js:
            return _identity_dict
        return _identity_dict

    locator.evaluate.side_effect = evaluate_side_effect

    verify_call_count = {"n": 0}

    def mock_verify(*args, **kwargs):
        verify_call_count["n"] += 1
        return ActionVerification(verified=True, method="url_change", detail="URL changed")

    with patch("app.worker.intent_executor.resolve_target", return_value=mock_resolved), \
         patch("app.worker.intent_executor.safe_click"), \
         patch("app.worker.intent_executor.verify_click", side_effect=mock_verify), \
         patch("app.worker.intent_executor.capture_pre_state", return_value={"url": "https://example.com"}), \
         patch("app.worker.intent_executor._remaining_ms", return_value=15000):

        result = _execute_single_action(page, action, timeout_ms=10000)

    # verify_click called only once — no retry needed
    assert verify_call_count["n"] == 1
    assert result["action_verification"].verified


# ── P0-2: Unstable CSS Selector Rejection ──


def test_unstable_css_modules_rejected():
    """CSS Modules hash pattern rejected: styles_header__xY2z."""
    assert _is_unstable_selector(".styles_header__xY2z3")


def test_unstable_css_in_js_rejected():
    """CSS-in-JS hash pattern rejected: css-1a2b3c."""
    assert _is_unstable_selector(".css-1a2b3c4")


def test_unstable_styled_components_rejected():
    """Styled-components pattern rejected: sc-dkzDqf."""
    assert _is_unstable_selector(".sc-dkzDqf")


def test_unstable_emotion_rejected():
    """Emotion CSS pattern rejected: emotion-xyz123."""
    assert _is_unstable_selector(".emotion-xyz123a")


def test_unstable_react_select_rejected():
    """React Select auto-generated ID rejected."""
    assert _is_unstable_selector("#react-select-3-option-0")


def test_unstable_radix_id_rejected():
    """Radix auto-generated ID rejected: radix-:r0:."""
    assert _is_unstable_selector("[id='radix-:r0:']")


def test_unstable_hex_hash_rejected():
    """Generic hex hash class rejected: btn-a3f2c1b."""
    assert _is_unstable_selector(".btn-a3f2c1b0")


def test_stable_css_accepted():
    """Normal CSS selectors pass the filter."""
    assert not _is_unstable_selector("button.primary")
    assert not _is_unstable_selector("#submit-btn")
    assert not _is_unstable_selector("[data-testid='login']")
    assert not _is_unstable_selector(".nav-item > a")
    assert not _is_unstable_selector("input[type='email']")


def test_build_locator_rejects_unstable_css():
    """_build_locator returns None for unstable CSS selectors."""
    from app.worker.selector_resilience import _build_locator

    page = MagicMock()

    # Unstable selector should be rejected
    result = _build_locator(page, SelectorStrategy.CSS, {"selector": ".css-1a2b3c4"})
    assert result is None

    # Stable selector should work
    result = _build_locator(page, SelectorStrategy.CSS, {"selector": "#submit-btn"})
    assert result is not None


# ── P1-1: pointer-events CSS Check ──


def test_pointer_events_none_bypassed():
    """Element with pointer-events: none is non-fatal (bypassed for JS click)."""
    locator = MagicMock()
    locator.evaluate.return_value = "none"

    result = GuardResult(passed=True)
    _check_pointer_events(locator, result)

    assert result.passed
    assert "pointer_events_none_allowed" in result.checks_passed
    assert "pointer_events_none_bypassed" in result.remediation_applied


def test_pointer_events_auto_passes_guard():
    """Element with pointer-events: auto passes the guard."""
    locator = MagicMock()
    locator.evaluate.return_value = "auto"

    result = GuardResult(passed=True)
    _check_pointer_events(locator, result)

    assert result.passed
    assert "pointer_events_ok" in result.checks_passed


def test_pointer_events_error_skips():
    """Evaluation error during pointer-events check is non-fatal."""
    locator = MagicMock()
    locator.evaluate.side_effect = Exception("Element detached")

    result = GuardResult(passed=True)
    _check_pointer_events(locator, result)

    assert result.passed
    assert "pointer_events_skipped" in result.checks_passed


def test_pointer_events_none_integrated_bypassed():
    """Full guard with pointer-events: none still passes (non-fatal)."""
    locator = MagicMock()
    locator.bounding_box.return_value = {"x": 10, "y": 10, "width": 100, "height": 40}
    locator.is_visible.return_value = True
    locator.is_enabled.return_value = True

    def evaluate_side_effect(js, *args, **kwargs):
        if "elementFromPoint" in js:
            return True
        if "pointerEvents" in js:
            return "none"
        if "charCodeAt" in js:
            return 12345
        return None

    locator.evaluate.side_effect = evaluate_side_effect

    result = ensure_interactable(locator)

    assert result.passed
    assert "pointer_events_none_allowed" in result.checks_passed
    assert "pointer_events_none_bypassed" in result.remediation_applied


# ── P1-2: Bounding Box Stability Check ──


def test_bbox_stable_passes():
    """Element with stable bounding box passes stability check."""
    locator = MagicMock()
    locator.bounding_box.return_value = {"x": 10, "y": 20, "width": 100, "height": 40}

    result = GuardResult(passed=True)
    _check_bbox_stability(locator, result)

    assert result.passed
    assert "bbox_stable" in result.checks_passed


def test_bbox_shifting_fails():
    """Element actively shifting position fails stability check."""
    locator = MagicMock()

    # Bounding box shifts significantly on each call
    call_count = {"n": 0}

    def bbox_side_effect(*args, **kwargs):
        call_count["n"] += 1
        return {"x": 10 + call_count["n"] * 20, "y": 20, "width": 100, "height": 40}

    locator.bounding_box.side_effect = bbox_side_effect

    result = GuardResult(passed=True)
    _check_bbox_stability(locator, result)

    assert not result.passed
    assert "bbox_layout_shift" in result.checks_failed


def test_bbox_settles_after_wait():
    """Element that shifts once then settles passes with remediation."""
    locator = MagicMock()

    # First two calls: different positions. Third call: same as second.
    positions = [
        {"x": 10, "y": 20, "width": 100, "height": 40},
        {"x": 30, "y": 20, "width": 100, "height": 40},
        {"x": 30, "y": 20, "width": 100, "height": 40},
    ]
    call_count = {"n": 0}

    def bbox_side_effect(*args, **kwargs):
        idx = call_count["n"]
        call_count["n"] += 1
        return positions[min(idx, len(positions) - 1)]

    locator.bounding_box.side_effect = bbox_side_effect

    result = GuardResult(passed=True)
    _check_bbox_stability(locator, result)

    assert result.passed
    assert "waited_for_layout_stable" in result.remediation_applied
    assert "bbox_stable" in result.checks_passed


def test_bbox_stability_error_skips():
    """Error during bbox stability check is non-fatal skip."""
    locator = MagicMock()
    locator.bounding_box.side_effect = Exception("Frame detached")

    result = GuardResult(passed=True)
    _check_bbox_stability(locator, result)

    assert result.passed
    assert "bbox_stability_skipped" in result.checks_passed


# ── P2-1: Whitespace Normalization ──


def test_normalize_ws_collapses():
    """_normalize_ws collapses whitespace and lowercases."""
    assert _normalize_ws("Sign  In") == "sign in"
    assert _normalize_ws("  Submit   Form  ") == "submit form"
    assert _normalize_ws("Next\n\tStep") == "next step"
    assert _normalize_ws("") == ""


def test_attribute_alignment_whitespace_role():
    """Role+name matching handles extra whitespace in text."""
    meta = {
        "role": "button",
        "aria_label": "Sign  In",
        "text": "Sign  In",
    }

    score = _attribute_alignment(
        meta, SelectorStrategy.ROLE,
        {"role": "button", "name": "Sign In"},
    )
    assert score == 1.0


def test_attribute_alignment_whitespace_label():
    """Label matching handles whitespace in aria_label."""
    meta = {
        "aria_label": "Email  Address",
        "text": "Email  Address",
    }

    score = _attribute_alignment(
        meta, SelectorStrategy.LABEL,
        {"label": "Email Address"},
    )
    assert score == 1.0


def test_attribute_alignment_whitespace_text():
    """Text matching normalizes whitespace."""
    meta = {"text": "Click   Here   Now"}

    score = _attribute_alignment(
        meta, SelectorStrategy.TEXT,
        {"text": "Click Here Now"},
    )
    assert score == 1.0


def test_attribute_alignment_whitespace_placeholder():
    """Placeholder matching normalizes whitespace."""
    meta = {"placeholder": "Enter  email  address"}

    score = _attribute_alignment(
        meta, SelectorStrategy.PLACEHOLDER,
        {"placeholder": "Enter email address"},
    )
    assert score == 1.0


def test_attribute_alignment_whitespace_interactive_ancestor():
    """Interactive ancestor text matching normalizes whitespace."""
    meta = {"text": "Add  to  Cart", "aria_label": ""}

    score = _attribute_alignment(
        meta, SelectorStrategy.INTERACTIVE_ANCESTOR,
        {"text": "Add to Cart"},
    )
    assert score == 1.0
