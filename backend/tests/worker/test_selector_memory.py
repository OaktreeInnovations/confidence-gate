"""Test: Selector Memory System (Part 1 of Intelligence Upgrade).

Tests cross-run selector memory persistence, decay, pruning, and
scoring integration via the history_bonus.

Uses a MagicMock pymongo Database for persistence tests and direct
unit tests for the scoring formula.
"""

import math
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.intelligence.selector_memory import (
    get_known_good_selectors,
    update_selector_memory,
)
from app.worker.selector_engine.scoring import (
    CandidateScore,
    ScoringWeights,
    _compute_history_bonus,
    _history_key,
)
from app.worker.selector_resilience import SelectorStrategy


# --- History bonus formula tests ---


def test_history_bonus_zero_with_no_records():
    """No history records → zero bonus."""
    bonus = _compute_history_bonus(SelectorStrategy.ROLE, {"role": "button"}, None)
    assert bonus == 0.0


def test_history_bonus_zero_with_empty_records():
    """Empty history_records dict → zero bonus."""
    bonus = _compute_history_bonus(SelectorStrategy.ROLE, {"role": "button"}, {})
    assert bonus == 0.0


def test_history_bonus_zero_for_unknown_selector():
    """Selector not in history_records → zero bonus."""
    records = {"role:{'role': 'link'}": 5}
    bonus = _compute_history_bonus(
        SelectorStrategy.ROLE, {"role": "button"}, records,
    )
    assert bonus == 0.0


def test_history_bonus_formula_correctness():
    """Verify log(success_count + 1) * 0.1 formula."""
    params = {"role": "button", "name": "Submit"}
    key = _history_key(SelectorStrategy.ROLE, params)
    records = {key: 10}

    bonus = _compute_history_bonus(SelectorStrategy.ROLE, params, records)
    expected = math.log(11) * 0.1
    assert abs(bonus - expected) < 0.001


def test_history_bonus_scales_with_success_count():
    """More successes → higher bonus."""
    params = {"role": "button"}
    key = _history_key(SelectorStrategy.ROLE, params)

    bonus_1 = _compute_history_bonus(SelectorStrategy.ROLE, params, {key: 1})
    bonus_5 = _compute_history_bonus(SelectorStrategy.ROLE, params, {key: 5})
    bonus_50 = _compute_history_bonus(SelectorStrategy.ROLE, params, {key: 50})

    assert bonus_1 < bonus_5 < bonus_50
    assert bonus_1 == pytest.approx(math.log(2) * 0.1, abs=0.001)
    assert bonus_50 == pytest.approx(math.log(51) * 0.1, abs=0.001)


def test_history_key_deterministic():
    """Same strategy+params → same key."""
    params = {"role": "button", "name": "Submit"}
    key1 = _history_key(SelectorStrategy.ROLE, params)
    key2 = _history_key(SelectorStrategy.ROLE, params)
    assert key1 == key2


def test_history_key_differs_for_different_strategies():
    """Different strategies → different keys."""
    params = {"text": "Submit"}
    key_text = _history_key(SelectorStrategy.TEXT, params)
    key_css = _history_key(SelectorStrategy.CSS, params)
    assert key_text != key_css


# --- update_selector_memory persistence tests ---


def _make_mock_db(existing_doc=None):
    """Create a MagicMock Database with selector_memory collection."""
    db = MagicMock()
    db.selector_memory.find_one.return_value = existing_doc
    return db


def test_update_creates_new_document():
    """First update for a step creates a new memory document."""
    db = _make_mock_db(existing_doc=None)

    update_selector_memory(
        db=db,
        test_case_id="tc_1",
        org_id="org_1",
        step_number=1,
        action="click",
        selectors_tried=["role:button", "text:Submit"],
        selector_that_worked="role:button",
    )

    db.selector_memory.update_one.assert_called_once()
    call_args = db.selector_memory.update_one.call_args
    update_doc = call_args[0][1]

    # The winning selector should have success_count=1
    selectors = update_doc["$set"]["selectors"]
    winner = next(s for s in selectors if s["selector"] == "role:button")
    loser = next(s for s in selectors if s["selector"] == "text:Submit")

    assert winner["success_count"] == 1
    assert winner["failure_count"] == 0
    assert loser["success_count"] == 0
    assert loser["failure_count"] == 1


def test_update_increments_existing_counts():
    """Second update for a step increments existing counts."""
    existing = {
        "selectors": [
            {
                "selector": "role:button",
                "strategy": "role",
                "params": {"role": "button"},
                "success_count": 3,
                "failure_count": 1,
                "last_used": datetime.now(timezone.utc),
                "last_succeeded": datetime.now(timezone.utc),
            },
        ],
    }
    db = _make_mock_db(existing_doc=existing)

    update_selector_memory(
        db=db,
        test_case_id="tc_1",
        org_id="org_1",
        step_number=1,
        action="click",
        selectors_tried=["role:button"],
        selector_that_worked="role:button",
    )

    call_args = db.selector_memory.update_one.call_args
    selectors = call_args[0][1]["$set"]["selectors"]
    record = next(s for s in selectors if s["selector"] == "role:button")

    assert record["success_count"] == 4
    assert record["failure_count"] == 1


def test_update_enriches_with_structured_data():
    """Structured data from resolver telemetry enriches selector records."""
    db = _make_mock_db(existing_doc=None)

    update_selector_memory(
        db=db,
        test_case_id="tc_1",
        org_id="org_1",
        step_number=1,
        action="click",
        selectors_tried=["role"],
        selector_that_worked="role",
        selectors_structured=[
            {"strategy": "role", "params": {"role": "button", "name": "Submit"}},
        ],
    )

    call_args = db.selector_memory.update_one.call_args
    selectors = call_args[0][1]["$set"]["selectors"]
    record = next(s for s in selectors if s["selector"] == "role")

    assert record["strategy"] == "role"
    assert record["params"] == {"role": "button", "name": "Submit"}


# --- get_known_good_selectors with decay and pruning ---


def test_get_known_good_includes_success_count():
    """Output includes success_count for history bonus integration."""
    now = datetime.now(timezone.utc)
    db = _make_mock_db(existing_doc={
        "selectors": [
            {
                "selector": "role:button",
                "strategy": "role",
                "params": {"role": "button"},
                "success_count": 8,
                "failure_count": 2,
                "last_used": now,
                "last_succeeded": now,
            },
        ],
    })

    results = get_known_good_selectors(db, "tc_1", "org_1", step_number=1)

    assert len(results) == 1
    assert results[0]["success_count"] == 8
    assert results[0]["success_rate"] == 0.8


def test_get_known_good_filters_low_success_rate():
    """Selectors below min_success_rate are excluded."""
    now = datetime.now(timezone.utc)
    db = _make_mock_db(existing_doc={
        "selectors": [
            {
                "selector": "bad_selector",
                "strategy": "css",
                "params": {},
                "success_count": 1,
                "failure_count": 9,
                "last_used": now,
                "last_succeeded": now - timedelta(days=5),
            },
        ],
    })

    results = get_known_good_selectors(
        db, "tc_1", "org_1", step_number=1, min_success_rate=0.5,
    )
    assert len(results) == 0


def test_get_known_good_filters_low_observations():
    """Selectors with < min_observations are excluded."""
    now = datetime.now(timezone.utc)
    db = _make_mock_db(existing_doc={
        "selectors": [
            {
                "selector": "role:button",
                "strategy": "role",
                "params": {"role": "button"},
                "success_count": 1,
                "failure_count": 0,
                "last_used": now,
                "last_succeeded": now,
            },
        ],
    })

    results = get_known_good_selectors(
        db, "tc_1", "org_1", step_number=1, min_observations=2,
    )
    assert len(results) == 0


def test_get_known_good_prunes_stale():
    """Selectors not used in 90+ days are pruned."""
    stale = datetime.now(timezone.utc) - timedelta(days=100)
    db = _make_mock_db(existing_doc={
        "selectors": [
            {
                "selector": "role:button",
                "strategy": "role",
                "params": {"role": "button"},
                "success_count": 50,
                "failure_count": 0,
                "last_used": stale,
                "last_succeeded": stale,
            },
        ],
    })

    results = get_known_good_selectors(db, "tc_1", "org_1", step_number=1)
    assert len(results) == 0


def test_get_known_good_recent_ranks_higher():
    """Recent selector with same rate ranks above older one."""
    now = datetime.now(timezone.utc)
    db = _make_mock_db(existing_doc={
        "selectors": [
            {
                "selector": "old_selector",
                "strategy": "role",
                "params": {"role": "button"},
                "success_count": 5,
                "failure_count": 1,
                "last_used": now - timedelta(days=60),
                "last_succeeded": now - timedelta(days=60),
            },
            {
                "selector": "new_selector",
                "strategy": "label",
                "params": {"label": "Submit"},
                "success_count": 5,
                "failure_count": 1,
                "last_used": now,
                "last_succeeded": now,
            },
        ],
    })

    results = get_known_good_selectors(db, "tc_1", "org_1", step_number=1)
    assert len(results) == 2
    assert results[0]["selector"] == "new_selector"


# --- End-to-end scoring integration ---


def test_history_bonus_boosts_composite_score():
    """Selector with history gets higher composite than identical one without."""
    params = {"role": "button", "name": "Submit"}
    key = _history_key(SelectorStrategy.ROLE, params)
    records = {key: 10}

    bonus_with = _compute_history_bonus(SelectorStrategy.ROLE, params, records)
    bonus_without = _compute_history_bonus(SelectorStrategy.ROLE, params, None)

    assert bonus_with > 0
    assert bonus_without == 0.0
    # A selector with 10 successes should get ~0.24 boost
    assert bonus_with == pytest.approx(math.log(11) * 0.1, abs=0.001)


def test_history_bonus_can_push_across_tier_threshold():
    """History bonus can boost a score across the 0.70 tier threshold.

    Simulates a Tier 2 candidate at 0.60 getting a +0.12 history bonus
    from 3 prior successes, pushing it to 0.72 (above 0.70 threshold).
    """
    params = {"text": "Submit"}
    key = _history_key(SelectorStrategy.INTERACTIVE_ANCESTOR, params)
    records = {key: 3}  # log(4) * 0.1 ≈ 0.139

    bonus = _compute_history_bonus(
        SelectorStrategy.INTERACTIVE_ANCESTOR, params, records,
    )

    base_score = 0.60
    boosted = base_score + bonus

    assert bonus > 0.10
    assert boosted > 0.70


def test_update_empty_selectors_is_noop():
    """Empty selectors_tried list → no database call."""
    db = _make_mock_db()
    update_selector_memory(
        db=db,
        test_case_id="tc_1",
        org_id="org_1",
        step_number=1,
        action="click",
        selectors_tried=[],
        selector_that_worked="",
    )
    db.selector_memory.update_one.assert_not_called()


def test_get_known_good_empty_collection():
    """No document in collection → empty list."""
    db = _make_mock_db(existing_doc=None)
    results = get_known_good_selectors(db, "tc_1", "org_1", step_number=1)
    assert results == []
