"""Test: Run-scoped adaptive memory persists across steps.

Validates that InRunSelectorMemory and RecoveryEffectivenessTracker
accumulate data across multiple steps when shared at the run level.
"""

from app.worker.in_run_memory import InRunSelectorMemory, RecoveryEffectivenessTracker
from app.worker.intent_executor import _parse_selector_key


def test_memory_persists_across_steps():
    """Shared InRunSelectorMemory retains blacklists across steps."""
    memory = InRunSelectorMemory()

    # Step 1: two failures on action 0 → blacklisted
    memory.record_failure(0, "role:{\"role\": \"button\"}")
    memory.record_failure(0, "role:{\"role\": \"button\"}")
    assert memory.is_blacklisted(0, "role:{\"role\": \"button\"}")

    # Step 2: different action index, same selector — NOT blacklisted
    assert not memory.is_blacklisted(1, "role:{\"role\": \"button\"}")

    # The blacklist from step 1 should still be there
    assert memory.blacklist_count == 1


def test_promoted_selector_injected_as_known_good():
    """_parse_selector_key should parse promoted selectors into resolver hints."""
    # Valid role selector key
    hint = _parse_selector_key('role:{"role": "button", "name": "Submit"}')
    assert hint is not None
    assert hint["strategy"] == "role"
    assert hint["params"] == {"role": "button", "name": "Submit"}
    assert hint["success_rate"] == 1.0

    # Valid CSS selector key
    hint = _parse_selector_key("css:#my-button")
    assert hint is not None
    assert hint["strategy"] == "css"
    assert hint["params"] == {"selector": "#my-button"}

    # Invalid key
    assert _parse_selector_key("invalid") is None
    assert _parse_selector_key("") is None


def test_recovery_tracker_accumulates_across_steps():
    """RecoveryEffectivenessTracker.should_skip fires after cross-step observations."""
    tracker = RecoveryEffectivenessTracker()

    # Step 1: 2 failures
    tracker.record("element_not_found", "dismiss_overlay", False)
    tracker.record("element_not_found", "dismiss_overlay", False)

    # Not enough observations yet (min 3)
    assert not tracker.should_skip("element_not_found", "dismiss_overlay")

    # Step 2: 1 more failure → now 3 observations, all failures (0% < 30%)
    tracker.record("element_not_found", "dismiss_overlay", False)
    assert tracker.should_skip("element_not_found", "dismiss_overlay")


def test_promotion_persists():
    """Promoted selectors persist across step boundaries when memory is shared."""
    memory = InRunSelectorMemory()

    # Step 1: promote a selector for action 0
    memory.record_success(0, "role:{\"role\": \"button\", \"name\": \"Submit\"}")

    # Step 2: preferred selector should still be available
    preferred = memory.get_preferred(0)
    assert preferred == "role:{\"role\": \"button\", \"name\": \"Submit\"}"
    assert memory.promotion_count == 1
