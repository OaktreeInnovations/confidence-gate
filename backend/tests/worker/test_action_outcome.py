"""Test: Action Outcome Model (Part 3 of Intelligence Upgrade).

Tests declarative verification rules per action type, verify_select(),
and the outcome registry.
"""

from unittest.mock import MagicMock

from app.worker.action_outcome import (
    ActionOutcome,
    VerificationMethod,
    get_all_outcomes,
    get_outcome,
)
from app.worker.post_action_verify import (
    ActionVerification,
    verify_select,
)


# --- Outcome registry ---


def test_click_outcome_exists():
    """Click action has a registered outcome with verification methods."""
    outcome = get_outcome("click")
    assert outcome is not None
    assert VerificationMethod.URL_CHANGE in outcome.verification_methods
    assert VerificationMethod.ARIA_EXPANDED in outcome.verification_methods
    assert not outcome.required  # Clicks can have side effects not captured


def test_input_outcome_required():
    """Input action verification is required — fill must produce correct value."""
    outcome = get_outcome("input")
    assert outcome is not None
    assert outcome.required
    assert VerificationMethod.VALUE_MATCH in outcome.verification_methods


def test_select_outcome_exists():
    """Select action has option_selected verification."""
    outcome = get_outcome("select")
    assert outcome is not None
    assert VerificationMethod.OPTION_SELECTED in outcome.verification_methods
    assert outcome.required


def test_navigate_outcome_required():
    """Navigate verification is required."""
    outcome = get_outcome("navigate")
    assert outcome is not None
    assert outcome.required


def test_unknown_outcome_returns_none():
    """Unknown action type returns None."""
    assert get_outcome("unknown_action") is None


def test_all_outcomes_has_expected_types():
    """get_all_outcomes returns entries for all core action types."""
    outcomes = get_all_outcomes()
    assert "click" in outcomes
    assert "input" in outcomes
    assert "navigate" in outcomes
    assert "select" in outcomes
    assert "check" in outcomes
    assert "uncheck" in outcomes


def test_check_outcome_uses_checked_state():
    """Check/uncheck actions verify checked state."""
    check_outcome = get_outcome("check")
    assert VerificationMethod.CHECKED_STATE in check_outcome.verification_methods

    uncheck_outcome = get_outcome("uncheck")
    assert VerificationMethod.CHECKED_STATE in uncheck_outcome.verification_methods


# --- verify_select ---


def test_verify_select_trigger_text_match():
    """Select verified when trigger text contains expected option."""
    page = MagicMock()
    locator = MagicMock()
    locator.inner_text.return_value = "United States"

    result = verify_select(page, locator, "United States")

    assert result.verified
    assert result.method == "option_selected"


def test_verify_select_trigger_text_partial_match():
    """Select verified when trigger text contains option as substring (case-insensitive)."""
    page = MagicMock()
    locator = MagicMock()
    locator.inner_text.return_value = "Selected: United States"

    result = verify_select(page, locator, "united states")

    assert result.verified
    assert result.method == "option_selected"


def test_verify_select_aria_selected_match():
    """Select verified via aria-selected element when trigger doesn't match."""
    page = MagicMock()
    locator = MagicMock()
    locator.inner_text.return_value = "Choose country"  # trigger doesn't match

    # Set up aria-selected element
    selected_locator = MagicMock()
    selected_locator.count.return_value = 1
    nth_el = MagicMock()
    nth_el.inner_text.return_value = "United States"
    selected_locator.nth.return_value = nth_el
    page.locator.return_value = selected_locator

    result = verify_select(page, locator, "United States")

    assert result.verified
    assert "aria-selected" in result.detail


def test_verify_select_no_match():
    """Select not verified when neither trigger nor aria-selected matches."""
    page = MagicMock()
    locator = MagicMock()
    locator.inner_text.return_value = "Choose country"

    selected_locator = MagicMock()
    selected_locator.count.return_value = 0
    page.locator.return_value = selected_locator

    result = verify_select(page, locator, "United States")

    assert not result.verified
    assert result.method == "option_selected"


def test_verify_select_empty_expected():
    """Empty expected option → verified (nothing to check)."""
    page = MagicMock()
    result = verify_select(page, None, "")
    assert result.verified


def test_verify_select_no_locator():
    """No trigger locator → falls back to aria-selected check."""
    page = MagicMock()
    selected_locator = MagicMock()
    selected_locator.count.return_value = 1
    nth_el = MagicMock()
    nth_el.inner_text.return_value = "Active"
    selected_locator.nth.return_value = nth_el
    page.locator.return_value = selected_locator

    result = verify_select(page, None, "Active")

    assert result.verified
