"""Test: DOM re-render — in-run memory blacklists stale selector.

Scenario: An element is detached mid-interaction (DOM re-render).
The in-run memory should blacklist the failed selector after 2
failures so retry attempts use alternative selectors.
"""

from app.worker.in_run_memory import InRunSelectorMemory


def test_first_failure_not_blacklisted():
    """A single failure should NOT blacklist the selector."""
    mem = InRunSelectorMemory()
    mem.record_failure(0, "role:{'role': 'button', 'name': 'Submit'}")

    assert not mem.is_blacklisted(0, "role:{'role': 'button', 'name': 'Submit'}")
    assert mem.blacklist_count == 0


def test_second_failure_blacklists():
    """Two failures for the same (action_index, selector) should blacklist."""
    mem = InRunSelectorMemory()
    selector = "role:{'role': 'button', 'name': 'Submit'}"

    mem.record_failure(0, selector)
    mem.record_failure(0, selector)

    assert mem.is_blacklisted(0, selector)
    assert mem.blacklist_count == 1


def test_different_action_index_independent():
    """Failures at different action indices are independent."""
    mem = InRunSelectorMemory()
    selector = "role:{'role': 'button', 'name': 'Submit'}"

    mem.record_failure(0, selector)
    mem.record_failure(0, selector)
    mem.record_failure(1, selector)

    assert mem.is_blacklisted(0, selector)
    assert not mem.is_blacklisted(1, selector)


def test_success_promotes_selector():
    """A successful selector should be promoted as preferred."""
    mem = InRunSelectorMemory()
    selector = "label:{'label': 'Email'}"

    mem.record_success(0, selector)

    assert mem.get_preferred(0) == selector
    assert mem.promotion_count == 1


def test_promoted_selector_not_blacklisted():
    """A promoted selector should not be in the blacklist."""
    mem = InRunSelectorMemory()
    selector = "role:{'role': 'button', 'name': 'OK'}"

    mem.record_success(0, selector)
    assert not mem.is_blacklisted(0, selector)


def test_different_selector_not_affected():
    """Blacklisting one selector should not affect others at the same index."""
    mem = InRunSelectorMemory()
    sel_a = "role:{'role': 'button', 'name': 'Submit'}"
    sel_b = "label:{'label': 'Submit'}"

    mem.record_failure(0, sel_a)
    mem.record_failure(0, sel_a)

    assert mem.is_blacklisted(0, sel_a)
    assert not mem.is_blacklisted(0, sel_b)
