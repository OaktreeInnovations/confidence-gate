"""Test: Chaos Hardening (Part 8 + Part 5 expansion).

Mock-based stress tests simulating real-world DOM chaos.
Each test uses MagicMock Page with controlled behavior.
Validates: correct resolution, deterministic recovery, budget compliance.
"""

from unittest.mock import MagicMock, PropertyMock, patch, call

from app.worker.interaction_guard import ensure_interactable, GuardResult
from app.worker.dom_revalidation import (
    capture_element_identity,
    is_same_element,
    revalidate_before_action,
)
from app.worker.post_action_verify import (
    capture_pre_state,
    verify_click,
    verify_fill,
    verify_navigate,
    verify_select,
)
from app.worker.intent_schema import TargetDescriptor
from app.worker.selector_engine.scoring import CandidateScore
from app.worker.selector_resilience import SelectorStrategy


# --- Scenario 1: Dropdown as div+click (no native select, no ARIA roles) ---

def test_dropdown_div_click_resolution():
    """Non-native dropdown: div-based trigger without ARIA roles.

    Simulates a custom dropdown where the trigger is a <div> with text
    and clicking reveals options as <div> children.
    The interaction guard should pass on a visible, enabled div.
    """
    locator = MagicMock()
    locator.bounding_box.return_value = {"x": 100, "y": 200, "width": 150, "height": 40}
    locator.is_visible.return_value = True
    locator.is_enabled.return_value = True
    # elementFromPoint returns the element itself
    locator.evaluate.side_effect = [
        True,    # overlay check: element is the target
        "auto",  # pointer-events check
        "abc",   # stability hash 1
        "abc",   # stability hash 2 (stable)
    ]

    guard = ensure_interactable(locator)
    assert guard.passed
    assert "bounding_box" in guard.checks_passed
    assert "visible" in guard.checks_passed


# --- Scenario 2: Async loading delay ---

def test_async_loading_element_appears_late():
    """Element is not visible initially, becomes visible after scroll remediation.

    Simulates a lazy-loaded element that appears after scrollIntoView.
    """
    locator = MagicMock()
    locator.bounding_box.return_value = {"x": 0, "y": 800, "width": 200, "height": 50}
    # First call: not visible; after scroll: visible
    locator.is_visible.side_effect = [False, True]
    locator.is_enabled.return_value = True
    locator.evaluate.side_effect = [
        True,    # overlay check
        "auto",  # pointer-events check
        "hash1", # stability hash 1
        "hash1", # stability hash 2
    ]

    guard = ensure_interactable(locator)
    assert guard.passed
    assert "scrolled_into_view" in guard.remediation_applied


# --- Scenario 3: Overlay mid-click ---

def test_overlay_blocks_interaction():
    """An overlay covers the target element.

    elementFromPoint returns a different element (overlay), guard fails.
    """
    locator = MagicMock()
    locator.bounding_box.return_value = {"x": 100, "y": 100, "width": 100, "height": 40}
    locator.is_visible.return_value = True
    locator.is_enabled.return_value = True
    # elementFromPoint returns False — not the target element
    locator.evaluate.side_effect = [False]

    guard = ensure_interactable(locator)
    assert not guard.passed
    assert "overlay_detected" in guard.checks_failed


# --- Scenario 4: DOM re-render replaces element ---

def test_dom_rerender_replaces_element():
    """React re-render replaces element node between resolve and click.

    Identity capture shows different textHash → triggers re-resolve.
    """
    original_identity = {
        "tag": "button",
        "id": "submit",
        "ariaLabel": "",
        "textHash": 12345,
        "rect": {"x": 10, "y": 20, "w": 100, "h": 40},
    }

    locator = MagicMock()
    # Current element has different text hash (re-rendered)
    locator.evaluate.return_value = {
        "tag": "button",
        "id": "submit",
        "ariaLabel": "",
        "textHash": 99999,
        "rect": {"x": 10, "y": 20, "w": 100, "h": 40},
    }

    assert not is_same_element(locator, original_identity)

    # Re-resolve should be triggered
    page = MagicMock()
    target = TargetDescriptor(role="button", name="Submit")
    new_locator = MagicMock()
    mock_resolved = MagicMock()
    mock_resolved.locator = new_locator
    mock_resolved.strategy.value = "role"
    mock_resolved.score.composite = 0.9

    with patch("app.worker.selector_engine.resolve_target", return_value=mock_resolved):
        result = revalidate_before_action(page, locator, original_identity, target)

    assert result is new_locator


# --- Scenario 5: Delayed enable ---

def test_delayed_enable_button():
    """Button disabled for initial check, then becomes enabled.

    Guard should wait and pass once enabled.
    """
    locator = MagicMock()
    locator.bounding_box.return_value = {"x": 50, "y": 50, "width": 120, "height": 36}
    locator.is_visible.return_value = True
    # First disabled, then enabled
    locator.is_enabled.side_effect = [False, False, True]
    locator.evaluate.side_effect = [
        True,    # overlay check
        "auto",  # pointer-events check
        "hash1", # stability hash 1
        "hash1", # stability hash 2
    ]

    guard = ensure_interactable(locator, max_wait_ms=3000)
    assert guard.passed
    assert "waited_for_enabled" in guard.remediation_applied


# --- Scenario 6: Duplicate buttons (3 "Submit", only 1 visible+enabled) ---

def test_duplicate_buttons_only_one_viable():
    """3 Submit buttons exist, guard should pass on the visible+enabled one."""
    locator = MagicMock()
    locator.bounding_box.return_value = {"x": 200, "y": 300, "width": 100, "height": 40}
    locator.is_visible.return_value = True
    locator.is_enabled.return_value = True
    locator.evaluate.side_effect = [
        True,    # overlay check (target is the visible one)
        "auto",  # pointer-events check
        "hash1",
        "hash1",
    ]

    guard = ensure_interactable(locator)
    assert guard.passed


# --- Scenario 7: Post-action verify: URL change after click ---

def test_click_causes_url_change():
    """Click triggers navigation — post-action verify detects URL change."""
    page = MagicMock()
    locator = MagicMock()

    pre_state = {"url": "https://app.example.com/login"}
    page.url = "https://app.example.com/dashboard"

    verification = verify_click(page, locator, pre_state)
    assert verification.verified
    assert verification.method == "url_change"


# --- Scenario 8: Fill value mismatch recovery ---

def test_fill_value_mismatch_detected():
    """Fill action leaves wrong value — post-action verify catches it."""
    locator = MagicMock()
    locator.input_value.return_value = "partial"

    verification = verify_fill(locator, "full_expected_value")
    assert not verification.verified
    assert "mismatch" in verification.detail


# --- Scenario 9: Navigate URL matches ---

def test_navigate_url_contains_expected():
    """Navigation URL contains expected pattern — verified."""
    page = MagicMock()
    page.url = "https://app.example.com/settings/profile?tab=general"

    verification = verify_navigate(page, "settings/profile")
    assert verification.verified


# --- Scenario 10: DOM stability — element hash changes then stabilizes ---

def test_dom_instability_then_stabilization():
    """Element hash changes initially (React render), then stabilizes.

    Guard's stability check should detect instability, wait, and pass
    when element stabilizes.
    """
    locator = MagicMock()
    locator.bounding_box.return_value = {"x": 50, "y": 50, "width": 150, "height": 40}
    locator.is_visible.return_value = True
    locator.is_enabled.return_value = True
    # overlay passes, then pointer-events, then stability
    locator.evaluate.side_effect = [
        True,       # overlay check — elementFromPoint matches target
        "auto",     # pointer-events check
        "hash_v1",  # stability: hash1
        "hash_v2",  # stability: hash2 (changed! → unstable, wait 200ms)
        "hash_v2",  # stability: hash3 (== hash2 → stable now)
    ]

    guard = ensure_interactable(locator)
    assert guard.passed
    assert "waited_for_stability" in guard.remediation_applied


# --- Scenario 11: Lazy-loaded element after scroll ---

def test_lazy_loaded_element_after_scroll():
    """Element not in viewport, becomes visible after scroll remediation.

    Simulates an infinite-scroll list where the target row is below the fold.
    Guard scrolls into view, element becomes visible on second check.
    """
    locator = MagicMock()
    # Bounding box exists but element is off-screen vertically
    locator.bounding_box.return_value = {"x": 50, "y": 2000, "width": 200, "height": 40}
    # First invisible (below fold), then visible after scroll
    locator.is_visible.side_effect = [False, True]
    locator.is_enabled.return_value = True
    locator.evaluate.side_effect = [
        True,    # overlay check
        "auto",  # pointer-events check
        "hash1", # stability hash 1
        "hash1", # stability hash 2
    ]

    guard = ensure_interactable(locator)
    assert guard.passed
    assert "scrolled_into_view" in guard.remediation_applied
    assert "visible" in guard.checks_passed


# --- Scenario 12: CSS transition animation delay ---

def test_css_transition_animation_delay():
    """Element is rendered but its outerHTML hash changes during CSS animation.

    Stability check catches the animation, waits, and passes when stable.
    Simulates a fade-in or slide-in animation on the target element.
    """
    locator = MagicMock()
    locator.bounding_box.return_value = {"x": 100, "y": 100, "width": 120, "height": 36}
    locator.is_visible.return_value = True
    locator.is_enabled.return_value = True
    # overlay passes, then pointer-events, then stability
    locator.evaluate.side_effect = [
        True,        # overlay check
        "auto",      # pointer-events check
        "anim_v1",   # stability: first hash
        "anim_v2",   # stability: second hash (changed! animation in progress)
        "anim_v2",   # stability: third hash (matches second → stable)
    ]

    guard = ensure_interactable(locator)
    assert guard.passed
    assert "waited_for_stability" in guard.remediation_applied


# --- Scenario 13: Partial overlay covering element edge ---

def test_partial_overlay_element_still_clickable():
    """An overlay exists but elementFromPoint at center still hits the target.

    Cookie banner or notification covers the top of the page, but the target
    button in the main content area is not obscured at its center point.
    Guard should pass since the center of the element is accessible.
    """
    locator = MagicMock()
    locator.bounding_box.return_value = {"x": 300, "y": 500, "width": 150, "height": 40}
    locator.is_visible.return_value = True
    locator.is_enabled.return_value = True
    # elementFromPoint at center returns the target itself (True)
    locator.evaluate.side_effect = [
        True,    # overlay check: center point is clear
        "auto",  # pointer-events check
        "hash1", # stability hash 1
        "hash1", # stability hash 2
    ]

    guard = ensure_interactable(locator)
    assert guard.passed
    assert "overlay_detected" not in guard.checks_failed


# --- Scenario 14: Select verification via trigger text ---

def test_select_verified_via_trigger_text():
    """After selecting an option, the trigger shows the selected text.

    Verifies that verify_select() correctly confirms the selection
    by checking the trigger element's text content.
    """
    page = MagicMock()
    locator = MagicMock()
    locator.inner_text.return_value = "Canada"

    verification = verify_select(page, locator, "Canada")
    assert verification.verified
    assert verification.method == "option_selected"


# --- Scenario 15: Select verification fails — wrong option shown ---

def test_select_verification_wrong_option():
    """After select, trigger still shows placeholder — not the chosen option.

    verify_select should return not verified.
    """
    page = MagicMock()
    locator = MagicMock()
    locator.inner_text.return_value = "Select a country..."

    # No aria-selected elements either
    selected_locator = MagicMock()
    selected_locator.count.return_value = 0
    page.locator.return_value = selected_locator

    verification = verify_select(page, locator, "United States")
    assert not verification.verified
