"""Erratic Behavior Diagnosis & Recovery Engine.

Three-layer architecture:
1. diagnosis.py — Deterministic failure classification from error + page state
2. ai_reasoning.py — AI-based diagnosis when deterministic is inconclusive
3. strategy_mapping.py — Map diagnosis to concrete recovery actions

Public API:
    diagnose_and_recover(page, error, context) -> RecoveryPlan
"""

from app.worker.recovery.diagnosis import (
    FailureDiagnosis,
    FailureType,
    PageState,
    diagnose_failure,
)
from app.worker.recovery.strategy_mapping import (
    RecoveryAction,
    RecoveryActionType,
    RecoveryPlan,
    plan_recovery,
)

__all__ = [
    "FailureDiagnosis",
    "FailureType",
    "PageState",
    "RecoveryAction",
    "RecoveryActionType",
    "RecoveryPlan",
    "diagnose_failure",
    "plan_recovery",
]
