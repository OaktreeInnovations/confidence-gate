"""Test: Hard budget enforcement for _budget_sleep and budget-aware wrappers.

Validates that _budget_sleep caps sleep at remaining deadline and raises
TimeoutError when the deadline is already past.
"""

import time

import pytest

from app.worker.stability_wrappers import (
    _budget_sleep,
    set_execution_deadline,
)


def test_budget_sleep_caps_at_deadline():
    """_budget_sleep should cap sleep duration at remaining deadline."""
    set_execution_deadline(time.monotonic() + 0.1)
    try:
        t0 = time.monotonic()
        _budget_sleep(5.0, "test_cap")  # Request 5s but only ~0.1s remains
        elapsed = time.monotonic() - t0
        # Should have slept ~0.1s, not 5s
        assert elapsed < 0.5
    finally:
        set_execution_deadline(None)


def test_budget_sleep_raises_when_expired():
    """_budget_sleep should raise TimeoutError when deadline is past."""
    set_execution_deadline(time.monotonic() - 1.0)
    try:
        with pytest.raises(TimeoutError, match="budget exceeded"):
            _budget_sleep(1.0, "test_expired")
    finally:
        set_execution_deadline(None)


def test_budget_sleep_no_deadline():
    """Without deadline, _budget_sleep should sleep the full duration."""
    set_execution_deadline(None)
    t0 = time.monotonic()
    _budget_sleep(0.05, "test_no_deadline")
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.04  # Allow small margin
