"""Healing Pipeline Orchestrator — coordinates all repair layers.

Orchestrates the healing layers in sequence with budget awareness:

1. Check budget — if < 2s remaining, skip healing
2. If selector-related failure:
   Layer 1: repair_selector() — budget: 40% of remaining
   If repaired: try original action with repaired locator
3. If action-related failure (or after selector repair):
   Layer 2: repair_action() — budget: 30% of remaining
4. If still failing and AI available:
   Layer 3: ai_structural_repair() — budget: 30% of remaining

Hard rules:
- Deterministic always has priority (layers 1-2 before layer 3)
- AI results go through guard + scoring (enforced by ai_repair module)
- Total healing budget capped at budget_remaining_ms
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from app.worker.healing.action_repair import ActionRepairResult, repair_action
from app.worker.healing.ai_repair import AIRepairResult, ai_structural_repair
from app.worker.healing.selector_repair import RepairResult, repair_selector
from app.worker.healing.step_reconstruction import (
    StepReconstructionResult,
    reconstruct_step,
)
from app.worker.recovery.diagnosis import FailureType

if TYPE_CHECKING:
    from app.worker.ai_provider import AIProvider
    from app.worker.intent_schema import ActionIntent
    from app.worker.recovery.diagnosis import FailureDiagnosis
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)

# Minimum budget to attempt healing (milliseconds)
_MIN_HEALING_BUDGET_MS = 2000

# Failure types that indicate a selector issue
_SELECTOR_FAILURE_TYPES = frozenset({
    FailureType.ELEMENT_NOT_FOUND,
    FailureType.ELEMENT_DETACHED,
    FailureType.STALE_REFERENCE,
    FailureType.ELEMENT_NOT_VISIBLE,
    FailureType.SELECTOR_AMBIGUOUS,
})

# Failure types that indicate an action issue (element found but action failed)
_ACTION_FAILURE_TYPES = frozenset({
    FailureType.OVERLAY_BLOCKING,
    FailureType.ELEMENT_DISABLED,
    FailureType.UNKNOWN,
})


@dataclass
class HealingResult:
    """Result of the healing pipeline."""

    healed: bool
    layer_used: int = 0  # 0=none, 1=selector, 2=action, 3=ai, 4=reconstruction
    strategy_used: str = ""
    attempts_total: int = 0
    elapsed_ms: int = 0
    locator: Locator | None = None
    action_override: str = ""

    # Layer 4 output: reconstructed actions for executor to consume
    reconstructed_actions: list[ActionIntent] | None = None

    # Sub-results for telemetry
    selector_repair_result: RepairResult | None = None
    action_repair_result: ActionRepairResult | None = None
    ai_repair_result: AIRepairResult | None = None
    step_reconstruction_result: StepReconstructionResult | None = None


def execute_healing(
    page: Page,
    action: ActionIntent,
    error: Exception,
    diagnosis: FailureDiagnosis,
    locator: Locator | None = None,
    ai_client: AIProvider | None = None,
    budget_remaining_ms: int = 10000,
    pre_state: dict | None = None,
) -> HealingResult:
    """Execute the healing pipeline.

    Args:
        page: Playwright Page instance.
        action: The action that failed.
        error: The exception from the failed attempt.
        diagnosis: Failure diagnosis.
        locator: Resolved locator (None if selector failed).
        ai_client: AI provider for Layer 3 (optional).
        budget_remaining_ms: Total budget for healing.
        pre_state: Pre-action state for verification.

    Returns:
        HealingResult with healed locator/action or healed=False.
    """
    t0 = time.monotonic()

    # Budget check — skip healing if too little time remaining
    if budget_remaining_ms < _MIN_HEALING_BUDGET_MS:
        logger.debug(
            "healing.budget_insufficient",
            budget_ms=budget_remaining_ms,
            min_ms=_MIN_HEALING_BUDGET_MS,
        )
        return HealingResult(healed=False, elapsed_ms=0)

    result = HealingResult(healed=False)
    total_attempts = 0

    # Layer 1: Selector Repair (deterministic)
    if diagnosis.failure_type in _SELECTOR_FAILURE_TYPES and action.target:
        selector_budget = int(budget_remaining_ms * 0.4)
        selector_result = repair_selector(
            page, action.target, diagnosis,
            timeout_ms=selector_budget,
        )
        result.selector_repair_result = selector_result
        total_attempts += selector_result.candidates_evaluated

        if selector_result.repaired and selector_result.locator is not None:
            result.healed = True
            result.layer_used = 1
            result.strategy_used = selector_result.strategy_used
            result.locator = selector_result.locator
            result.attempts_total = total_attempts
            result.elapsed_ms = int((time.monotonic() - t0) * 1000)

            logger.info(
                "healing.selector_repaired",
                strategy=selector_result.strategy_used,
                confidence=round(selector_result.confidence, 3),
            )
            return result

        # Update locator if repair partially found something
        if selector_result.locator is not None:
            locator = selector_result.locator

    # Layer 2: Action Repair (deterministic)
    if locator is not None and (
        diagnosis.failure_type in _ACTION_FAILURE_TYPES
        or diagnosis.failure_type in _SELECTOR_FAILURE_TYPES
    ):
        elapsed = int((time.monotonic() - t0) * 1000)
        action_budget = int((budget_remaining_ms - elapsed) * 0.5)
        if action_budget > 500:
            action_result = repair_action(
                page, locator, action,
                pre_state=pre_state or {},
                max_attempts=3,
            )
            result.action_repair_result = action_result
            total_attempts += action_result.attempts

            if action_result.repaired:
                result.healed = True
                result.layer_used = 2
                result.strategy_used = action_result.method_used
                result.locator = locator
                result.attempts_total = total_attempts
                result.elapsed_ms = int((time.monotonic() - t0) * 1000)

                logger.info(
                    "healing.action_repaired",
                    method=action_result.method_used,
                )
                return result

    # Layer 3: AI Structural Repair (last resort)
    if ai_client is not None:
        elapsed = int((time.monotonic() - t0) * 1000)
        ai_budget = budget_remaining_ms - elapsed
        if ai_budget > 1000:
            failed_selectors = _collect_failed_selectors(result)
            ai_result = ai_structural_repair(
                page, action.target, action, diagnosis,
                ai_client,
                failed_selectors=failed_selectors,
                timeout_ms=ai_budget,
            )
            result.ai_repair_result = ai_result
            total_attempts += 1

            if ai_result.repaired:
                result.healed = True
                result.layer_used = 3
                result.strategy_used = "ai_structural"
                result.locator = ai_result.locator
                result.action_override = ai_result.action_override
                result.attempts_total = total_attempts
                result.elapsed_ms = int((time.monotonic() - t0) * 1000)

                logger.info(
                    "healing.ai_repaired",
                    selector=ai_result.selector,
                    confidence=round(ai_result.confidence, 3),
                )
                return result

    # Layer 4: Step Reconstruction (last resort before full regeneration)
    if ai_client is not None:
        elapsed = int((time.monotonic() - t0) * 1000)
        recon_budget = budget_remaining_ms - elapsed
        if recon_budget > 1500:
            failed_selectors = _collect_failed_selectors(result)
            recon_result = reconstruct_step(
                page, action, diagnosis,
                ai_client,
                failed_selectors=failed_selectors,
                timeout_ms=recon_budget,
            )
            result.step_reconstruction_result = recon_result
            total_attempts += 1

            if recon_result.reconstructed:
                result.healed = True
                result.layer_used = 4
                result.strategy_used = "step_reconstruction"
                result.reconstructed_actions = recon_result.reconstructed_actions
                result.attempts_total = total_attempts
                result.elapsed_ms = int((time.monotonic() - t0) * 1000)

                logger.info(
                    "healing.step_reconstructed",
                    num_actions=len(recon_result.reconstructed_actions),
                    confidence=round(recon_result.confidence, 3),
                    elapsed_ms=result.elapsed_ms,
                )
                return result

    result.attempts_total = total_attempts
    result.elapsed_ms = int((time.monotonic() - t0) * 1000)

    logger.info(
        "healing.failed",
        attempts=total_attempts,
        elapsed_ms=result.elapsed_ms,
        failure_type=diagnosis.failure_type.value,
    )
    return result


def _collect_failed_selectors(result: HealingResult) -> list[str]:
    """Collect selector strings that were tried and failed."""
    failed = []
    if result.selector_repair_result and not result.selector_repair_result.repaired:
        failed.append(f"repair:{result.selector_repair_result.strategy_used or 'all'}")
    return failed
