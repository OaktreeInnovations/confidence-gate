"""Test: Selector memory recency decay.

Scenario: Old selectors at the same success_rate should rank below
recent ones due to exponential decay weighting.
"""

import math
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from app.intelligence.selector_memory import (
    get_known_good_selectors,
    _DECAY_HALF_LIFE_DAYS,
    _STALE_THRESHOLD_DAYS,
)


def _make_selector_doc(selectors: list[dict]) -> dict:
    """Create a mock selector_memory document."""
    return {
        "test_case_id": "tc_1",
        "step_number": 1,
        "org_id": "org_1",
        "selectors": selectors,
    }


def test_recent_selector_ranks_higher():
    """A recently used selector should rank higher than an old one at the same success rate."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=60)

    doc = _make_selector_doc([
        {
            "selector": "role:button:Submit",
            "strategy": "role",
            "params": {"role": "button", "name": "Submit"},
            "success_count": 8,
            "failure_count": 2,
            "last_used": now,
            "last_succeeded": now,
        },
        {
            "selector": "css:#submit-btn",
            "strategy": "css",
            "params": {"selector": "#submit-btn"},
            "success_count": 8,
            "failure_count": 2,
            "last_used": old,
            "last_succeeded": old,
        },
    ])

    db = MagicMock()
    db.selector_memory.find_one.return_value = doc

    results = get_known_good_selectors(db, "tc_1", "org_1", 1)

    assert len(results) == 2
    # Recent selector should rank first
    assert results[0]["selector"] == "role:button:Submit"
    assert results[0]["weighted_score"] > results[1]["weighted_score"]


def test_stale_selector_pruned():
    """Selectors not used in 90+ days should be auto-pruned."""
    now = datetime.now(timezone.utc)
    stale = now - timedelta(days=100)

    doc = _make_selector_doc([
        {
            "selector": "role:button:Submit",
            "strategy": "role",
            "params": {"role": "button", "name": "Submit"},
            "success_count": 10,
            "failure_count": 0,
            "last_used": stale,
            "last_succeeded": stale,
        },
    ])

    db = MagicMock()
    db.selector_memory.find_one.return_value = doc

    results = get_known_good_selectors(db, "tc_1", "org_1", 1)

    assert len(results) == 0  # Pruned due to staleness


def test_decay_math_correct():
    """Recency weight should follow exponential decay formula."""
    # At t=0 days, weight = 1.0
    weight_0 = math.exp(-0 / _DECAY_HALF_LIFE_DAYS)
    assert abs(weight_0 - 1.0) < 0.001

    # At t=30 days (one half-life), weight ≈ 0.368
    weight_30 = math.exp(-30 / _DECAY_HALF_LIFE_DAYS)
    assert abs(weight_30 - math.exp(-1)) < 0.001

    # At t=60 days, weight ≈ 0.135
    weight_60 = math.exp(-60 / _DECAY_HALF_LIFE_DAYS)
    assert weight_60 < weight_30 < weight_0


def test_weighted_score_combines_rate_and_recency():
    """Weighted score should be success_rate * recency_weight."""
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    doc = _make_selector_doc([
        {
            "selector": "high_rate_old",
            "strategy": "role",
            "params": {"role": "button"},
            "success_count": 10,
            "failure_count": 0,  # 100% success rate
            "last_used": thirty_days_ago,
            "last_succeeded": thirty_days_ago,
        },
        {
            "selector": "lower_rate_recent",
            "strategy": "label",
            "params": {"label": "Submit"},
            "success_count": 7,
            "failure_count": 3,  # 70% success rate
            "last_used": now,
            "last_succeeded": now,
        },
    ])

    db = MagicMock()
    db.selector_memory.find_one.return_value = doc

    results = get_known_good_selectors(db, "tc_1", "org_1", 1)

    assert len(results) == 2
    # The recent one at 70% should rank above the old one at 100%
    # because 0.7 * 1.0 > 1.0 * exp(-1) ≈ 0.368
    assert results[0]["selector"] == "lower_rate_recent"


def test_no_last_succeeded_fallback():
    """When last_succeeded is None, should use last_used for decay."""
    now = datetime.now(timezone.utc)

    doc = _make_selector_doc([
        {
            "selector": "no_success_date",
            "strategy": "role",
            "params": {"role": "button"},
            "success_count": 5,
            "failure_count": 0,
            "last_used": now,
            "last_succeeded": None,
        },
    ])

    db = MagicMock()
    db.selector_memory.find_one.return_value = doc

    results = get_known_good_selectors(db, "tc_1", "org_1", 1)
    assert len(results) == 1
    assert results[0]["weighted_score"] > 0


def test_empty_memory_returns_empty():
    """No selector memory should return empty list."""
    db = MagicMock()
    db.selector_memory.find_one.return_value = None

    results = get_known_good_selectors(db, "tc_1", "org_1", 1)
    assert results == []
