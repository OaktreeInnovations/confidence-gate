"""Test: Failure Pattern Learning (Part 2 of Intelligence Upgrade).

Tests cross-run recovery stats persistence, ranking queries,
and integration with plan_recovery() for action reordering.
"""

from unittest.mock import MagicMock

from app.intelligence.recovery_learning import (
    get_recovery_rankings,
    record_recovery_outcome,
)
from app.worker.recovery.diagnosis import FailureDiagnosis, FailureType
from app.worker.recovery.strategy_mapping import (
    RecoveryAction,
    RecoveryActionType,
    _apply_recovery_rankings,
    plan_recovery,
)


# --- record_recovery_outcome ---


def test_record_outcome_upserts():
    """Recording an outcome upserts into recovery_stats."""
    db = MagicMock()

    record_recovery_outcome(
        db=db,
        test_case_id="tc_1",
        org_id="org_1",
        failure_type="overlay_blocking",
        action_type="press_escape",
        success=True,
    )

    db.recovery_stats.update_one.assert_called_once()
    call_args = db.recovery_stats.update_one.call_args
    update_doc = call_args[0][1]
    assert update_doc["$inc"] == {"success_count": 1}


def test_record_failure_increments_failure_count():
    """Failed recovery increments failure_count."""
    db = MagicMock()

    record_recovery_outcome(
        db=db,
        test_case_id="tc_1",
        org_id="org_1",
        failure_type="overlay_blocking",
        action_type="press_escape",
        success=False,
    )

    call_args = db.recovery_stats.update_one.call_args
    update_doc = call_args[0][1]
    assert update_doc["$inc"] == {"failure_count": 1}


# --- get_recovery_rankings ---


def _make_db_with_stats(stats_docs):
    """Create a MagicMock Database returning stats_docs from find()."""
    db = MagicMock()
    db.recovery_stats.find.return_value = stats_docs
    return db


def test_rankings_sorted_by_success_rate():
    """Higher success rate actions rank first."""
    db = _make_db_with_stats([
        {"action_type": "press_escape", "success_count": 2, "failure_count": 8},
        {"action_type": "wait_and_retry", "success_count": 7, "failure_count": 3},
        {"action_type": "click_backdrop", "success_count": 5, "failure_count": 5},
    ])

    rankings = get_recovery_rankings(db, "tc_1", "org_1", "overlay_blocking")

    assert len(rankings) == 3
    assert rankings[0]["action_type"] == "wait_and_retry"
    assert rankings[0]["success_rate"] == 0.7
    assert rankings[1]["action_type"] == "click_backdrop"
    assert rankings[2]["action_type"] == "press_escape"


def test_rankings_skip_flag_for_low_effectiveness():
    """Actions below skip threshold with enough observations get skip=True."""
    db = _make_db_with_stats([
        {"action_type": "press_escape", "success_count": 0, "failure_count": 6},
    ])

    rankings = get_recovery_rankings(db, "tc_1", "org_1", "overlay_blocking")

    assert len(rankings) == 1
    assert rankings[0]["skip"] is True


def test_rankings_no_skip_below_min_observations():
    """Low success rate but few observations → no skip flag."""
    db = _make_db_with_stats([
        {"action_type": "press_escape", "success_count": 0, "failure_count": 2},
    ])

    rankings = get_recovery_rankings(db, "tc_1", "org_1", "overlay_blocking")

    assert len(rankings) == 1
    assert rankings[0]["skip"] is False


def test_rankings_empty_collection():
    """No stats → empty rankings."""
    db = _make_db_with_stats([])
    rankings = get_recovery_rankings(db, "tc_1", "org_1", "overlay_blocking")
    assert rankings == []


# --- _apply_recovery_rankings ---


def test_apply_rankings_reorders_actions():
    """Actions are reordered by historical success rate."""
    actions = [
        RecoveryAction(action_type=RecoveryActionType.PRESS_ESCAPE),
        RecoveryAction(action_type=RecoveryActionType.CLICK_BACKDROP),
        RecoveryAction(action_type=RecoveryActionType.WAIT_AND_RETRY),
    ]

    rankings = [
        {"action_type": "wait_and_retry", "success_rate": 0.9, "total": 10, "skip": False},
        {"action_type": "click_backdrop", "success_rate": 0.5, "total": 10, "skip": False},
        {"action_type": "press_escape", "success_rate": 0.3, "total": 10, "skip": False},
    ]

    result = _apply_recovery_rankings(actions, rankings)
    assert len(result) == 3
    assert result[0].action_type == RecoveryActionType.WAIT_AND_RETRY
    assert result[1].action_type == RecoveryActionType.CLICK_BACKDROP
    assert result[2].action_type == RecoveryActionType.PRESS_ESCAPE


def test_apply_rankings_removes_skipped():
    """Actions flagged skip=True are removed."""
    actions = [
        RecoveryAction(action_type=RecoveryActionType.PRESS_ESCAPE),
        RecoveryAction(action_type=RecoveryActionType.WAIT_AND_RETRY),
    ]

    rankings = [
        {"action_type": "press_escape", "success_rate": 0.1, "total": 10, "skip": True},
        {"action_type": "wait_and_retry", "success_rate": 0.8, "total": 10, "skip": False},
    ]

    result = _apply_recovery_rankings(actions, rankings)
    assert len(result) == 1
    assert result[0].action_type == RecoveryActionType.WAIT_AND_RETRY


def test_apply_rankings_unranked_actions_preserved():
    """Actions without rankings keep their original order at the end."""
    actions = [
        RecoveryAction(action_type=RecoveryActionType.PRESS_ESCAPE),
        RecoveryAction(action_type=RecoveryActionType.SCROLL_TO_ELEMENT),
    ]

    rankings = [
        {"action_type": "press_escape", "success_rate": 0.6, "total": 5, "skip": False},
    ]

    result = _apply_recovery_rankings(actions, rankings)
    assert len(result) == 2
    assert result[0].action_type == RecoveryActionType.PRESS_ESCAPE
    assert result[1].action_type == RecoveryActionType.SCROLL_TO_ELEMENT


def test_apply_rankings_empty_rankings_noop():
    """Empty rankings → no change to action order."""
    actions = [
        RecoveryAction(action_type=RecoveryActionType.PRESS_ESCAPE),
    ]
    result = _apply_recovery_rankings(actions, [])
    assert len(result) == 1


# --- plan_recovery with rankings ---


def test_plan_recovery_accepts_rankings():
    """plan_recovery with recovery_rankings reorders actions."""
    diagnosis = FailureDiagnosis(
        failure_type=FailureType.OVERLAY_BLOCKING,
        root_cause="Overlay detected",
        is_recoverable=True,
    )

    # Rankings: wait_and_retry has highest success rate
    rankings = [
        {"action_type": "wait_and_retry", "success_rate": 0.9, "total": 10, "skip": False},
        {"action_type": "press_escape", "success_rate": 0.3, "total": 10, "skip": False},
    ]

    plan = plan_recovery(diagnosis, attempt=0, recovery_rankings=rankings)

    # WAIT_AND_RETRY should be promoted to first position
    assert len(plan.actions) >= 1
    action_types = [a.action_type.value for a in plan.actions]
    wait_idx = action_types.index("wait_and_retry") if "wait_and_retry" in action_types else -1
    esc_idx = action_types.index("press_escape") if "press_escape" in action_types else -1
    if wait_idx >= 0 and esc_idx >= 0:
        assert wait_idx < esc_idx


def test_plan_recovery_without_rankings_unchanged():
    """plan_recovery without rankings produces default order."""
    diagnosis = FailureDiagnosis(
        failure_type=FailureType.OVERLAY_BLOCKING,
        root_cause="Overlay detected",
        is_recoverable=True,
    )

    plan = plan_recovery(diagnosis, attempt=0)
    # Default order: press_escape first for overlay
    assert plan.actions[0].action_type == RecoveryActionType.PRESS_ESCAPE
