"""Test: Budget enforcement — budget_wait caps, component_budget enforces.

Scenario: Component interactions and waits should be capped by the
step execution budget to prevent budget overruns.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from app.worker.execution_budget import budget_wait, component_budget, ComponentBudgetExceeded
from app.worker.stability_wrappers import (
    _remaining_ms,
    set_execution_deadline,
)


def test_budget_wait_caps_at_remaining(mock_page):
    """budget_wait should cap the wait at remaining budget."""
    # Set a deadline 0.5s from now
    set_execution_deadline(time.monotonic() + 0.5)

    try:
        # Request 5000ms wait but only ~500ms remains
        budget_wait(mock_page, 5000, "test_cap")

        # Should have called wait_for_timeout with a value <= 500
        call_args = mock_page.wait_for_timeout.call_args
        actual_ms = call_args[0][0] if call_args[0] else call_args[1].get("timeout", 0)
        assert actual_ms <= 600  # Allow some margin
    finally:
        set_execution_deadline(None)


def test_budget_wait_no_deadline(mock_page):
    """Without deadline, budget_wait should use the requested time."""
    set_execution_deadline(None)

    budget_wait(mock_page, 1000, "test_no_deadline")

    call_args = mock_page.wait_for_timeout.call_args
    actual_ms = call_args[0][0] if call_args[0] else call_args[1].get("timeout", 0)
    assert actual_ms == 1000


def test_remaining_ms_no_deadline():
    """Without deadline, _remaining_ms should return large default."""
    set_execution_deadline(None)
    remaining = _remaining_ms()
    assert remaining == 30_000


def test_remaining_ms_with_deadline():
    """With deadline, _remaining_ms should return time until deadline."""
    set_execution_deadline(time.monotonic() + 2.0)
    try:
        remaining = _remaining_ms()
        assert 1500 <= remaining <= 2100  # ~2000ms with margin
    finally:
        set_execution_deadline(None)


def test_remaining_ms_exceeded_raises():
    """When deadline is passed, _remaining_ms should raise TimeoutError."""
    set_execution_deadline(time.monotonic() - 1.0)  # Already past
    try:
        import pytest
        with pytest.raises(TimeoutError, match="budget exceeded"):
            _remaining_ms()
    finally:
        set_execution_deadline(None)


def test_component_budget_enforces_by_default():
    """component_budget should raise ComponentBudgetExceeded when exceeded."""
    # Set a tight deadline so the allowed_ms is very small
    set_execution_deadline(time.monotonic() + 0.1)
    try:
        with pytest.raises(ComponentBudgetExceeded):
            with component_budget(max_pct=0.01) as cb:
                # Sleep longer than 0.01 * 100ms = 1ms allowed
                time.sleep(0.05)
    finally:
        set_execution_deadline(None)


def test_component_budget_enforce_false_no_raise():
    """component_budget with enforce=False should not raise."""
    set_execution_deadline(time.monotonic() + 0.1)
    try:
        with component_budget(max_pct=0.01, enforce=False) as cb:
            time.sleep(0.05)
        # Should not raise — just logs warning
    finally:
        set_execution_deadline(None)
