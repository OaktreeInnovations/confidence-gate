"""Maps FailureDiagnosis to concrete recovery actions.

Each RecoveryAction is an executable step the executor can take before retrying.
A RecoveryPlan is an ordered list of actions to attempt.

Recovery actions are deterministic Playwright operations — no AI involved.
The diagnosis (from diagnosis.py or ai_reasoning.py) drives which actions to try.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from app.worker.component_helpers import resolve_single
from app.worker.execution_budget import budget_wait
from app.worker.recovery.diagnosis import FailureDiagnosis, FailureType

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = structlog.get_logger(__name__)


class RecoveryActionType(StrEnum):
    """Atomic recovery actions the executor can perform."""

    DISMISS_OVERLAY = "dismiss_overlay"
    DISMISS_DIALOG = "dismiss_dialog"
    DISMISS_TOAST = "dismiss_toast"
    SCROLL_TO_ELEMENT = "scroll_to_element"
    WAIT_AND_RETRY = "wait_and_retry"
    WAIT_FOR_ENABLED = "wait_for_enabled"
    WAIT_FOR_LOAD = "wait_for_load"
    RE_RESOLVE_SELECTOR = "re_resolve_selector"
    NAVIGATE_BACK = "navigate_back"
    REFRESH_PAGE = "refresh_page"
    PRESS_ESCAPE = "press_escape"
    CLICK_BACKDROP = "click_backdrop"
    NONE = "none"


@dataclass
class RecoveryAction:
    """One concrete recovery step."""

    action_type: RecoveryActionType
    description: str = ""
    params: dict = field(default_factory=dict)
    timeout_ms: int = 5000

    def to_dict(self) -> dict:
        return {
            "action_type": self.action_type.value,
            "description": self.description,
            "params": self.params,
            "timeout_ms": self.timeout_ms,
        }


@dataclass
class RecoveryPlan:
    """Ordered list of recovery actions to attempt before retrying the step."""

    actions: list[RecoveryAction] = field(default_factory=list)
    diagnosis: FailureDiagnosis | None = None
    should_regenerate_intent: bool = False
    adjusted_timeout_ms: int | None = None

    def to_dict(self) -> dict:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "should_regenerate_intent": self.should_regenerate_intent,
            "adjusted_timeout_ms": self.adjusted_timeout_ms,
            "diagnosis": self.diagnosis.to_dict() if self.diagnosis else None,
        }


def plan_recovery(
    diagnosis: FailureDiagnosis,
    attempt: int = 0,
    max_attempts: int = 3,
    recovery_rankings: list[dict] | None = None,
) -> RecoveryPlan:
    """Generate a recovery plan from a failure diagnosis.

    Args:
        diagnosis: Structured failure diagnosis.
        attempt: Current attempt number (0-based).
        max_attempts: Total attempts allowed.
        recovery_rankings: Cross-run rankings from
            ``recovery_learning.get_recovery_rankings()``.
            When provided, actions are reordered by historical effectiveness
            and actions flagged ``skip=True`` are dropped.

    Returns:
        RecoveryPlan with ordered actions and retry guidance.
    """
    if not diagnosis.is_recoverable or attempt >= max_attempts - 1:
        return RecoveryPlan(
            diagnosis=diagnosis,
            actions=[],
            should_regenerate_intent=False,
        )

    actions = _STRATEGY_MAP.get(diagnosis.failure_type, _default_strategy)(
        diagnosis, attempt,
    )

    # Reorder and filter actions using cross-run recovery rankings
    if recovery_rankings:
        actions = _apply_recovery_rankings(actions, recovery_rankings)

    # On later attempts, escalate: add intent regeneration
    should_regen = attempt >= 1 and diagnosis.failure_type not in _NO_REGEN_TYPES

    plan = RecoveryPlan(
        actions=actions,
        diagnosis=diagnosis,
        should_regenerate_intent=should_regen,
    )

    logger.info(
        "recovery.plan",
        failure_type=diagnosis.failure_type.value,
        num_actions=len(actions),
        should_regen=should_regen,
        attempt=attempt,
    )

    return plan


def _apply_recovery_rankings(
    actions: list[RecoveryAction],
    rankings: list[dict],
) -> list[RecoveryAction]:
    """Reorder recovery actions by historical effectiveness.

    Actions flagged as ``skip=True`` in rankings are removed.
    Actions with higher success_rate move to the front.
    Actions not in rankings keep their original relative order at the end.
    """
    if not rankings:
        return actions

    # Build lookup: action_type → {success_rate, skip}
    rank_map = {r["action_type"]: r for r in rankings}

    # Partition: ranked actions vs unranked
    ranked: list[tuple[RecoveryAction, float]] = []
    unranked: list[RecoveryAction] = []

    for action in actions:
        at = action.action_type.value
        info = rank_map.get(at)
        if info is not None:
            if info.get("skip", False):
                logger.debug(
                    "recovery.skipping_low_effectiveness",
                    action=at,
                    success_rate=info.get("success_rate"),
                )
                continue
            ranked.append((action, info["success_rate"]))
        else:
            unranked.append(action)

    # Sort ranked by success_rate descending
    ranked.sort(key=lambda x: -x[1])
    return [a for a, _ in ranked] + unranked


# --- Recovery strategies per failure type ---

def _overlay_recovery(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Dismiss overlay: Escape → click backdrop → wait."""
    actions = [
        RecoveryAction(
            action_type=RecoveryActionType.PRESS_ESCAPE,
            description="Press Escape to dismiss overlay",
            timeout_ms=2000,
        ),
    ]
    if attempt >= 1:
        actions.append(RecoveryAction(
            action_type=RecoveryActionType.CLICK_BACKDROP,
            description="Click page backdrop to dismiss overlay",
            params={"selector": diagnosis.page_state.overlay_selector}
            if diagnosis.page_state else {},
            timeout_ms=2000,
        ))
    actions.append(RecoveryAction(
        action_type=RecoveryActionType.WAIT_AND_RETRY,
        description="Wait for overlay dismiss animation",
        params={"wait_ms": 500},
        timeout_ms=1000,
    ))
    return actions


def _dialog_recovery(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Dismiss dialog: Escape → try close button → wait."""
    actions = [
        RecoveryAction(
            action_type=RecoveryActionType.PRESS_ESCAPE,
            description="Press Escape to dismiss dialog",
            timeout_ms=2000,
        ),
    ]
    if attempt >= 1:
        actions.append(RecoveryAction(
            action_type=RecoveryActionType.DISMISS_DIALOG,
            description="Click dialog close button",
            timeout_ms=3000,
        ))
    actions.append(RecoveryAction(
        action_type=RecoveryActionType.WAIT_AND_RETRY,
        description="Wait for dialog dismiss animation",
        params={"wait_ms": 500},
        timeout_ms=1000,
    ))
    return actions


def _toast_recovery(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Dismiss toast: click dismiss → wait for auto-dismiss."""
    return [
        RecoveryAction(
            action_type=RecoveryActionType.DISMISS_TOAST,
            description="Dismiss toast notification",
            timeout_ms=2000,
        ),
        RecoveryAction(
            action_type=RecoveryActionType.WAIT_AND_RETRY,
            description="Wait for toast to auto-dismiss",
            params={"wait_ms": 2000},
            timeout_ms=3000,
        ),
    ]


def _not_found_recovery(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Element not found: wait for page ready → re-resolve."""
    actions = [
        RecoveryAction(
            action_type=RecoveryActionType.WAIT_FOR_LOAD,
            description="Wait for page to finish loading",
            timeout_ms=5000,
        ),
        RecoveryAction(
            action_type=RecoveryActionType.RE_RESOLVE_SELECTOR,
            description="Re-resolve selector with fresh page state",
            timeout_ms=3000,
        ),
    ]
    if attempt >= 1:
        actions.insert(0, RecoveryAction(
            action_type=RecoveryActionType.SCROLL_TO_ELEMENT,
            description="Scroll page to reveal hidden elements",
            params={"direction": "down"},
            timeout_ms=2000,
        ))
    return actions


def _not_visible_recovery(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Element not visible: scroll to element → wait for visibility."""
    return [
        RecoveryAction(
            action_type=RecoveryActionType.SCROLL_TO_ELEMENT,
            description="Scroll to make element visible",
            timeout_ms=3000,
        ),
        RecoveryAction(
            action_type=RecoveryActionType.WAIT_AND_RETRY,
            description="Wait for CSS transition/animation",
            params={"wait_ms": 500},
            timeout_ms=1000,
        ),
    ]


def _disabled_recovery(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Element disabled: wait for enabled state."""
    wait_ms = 2000 * (attempt + 1)
    return [
        RecoveryAction(
            action_type=RecoveryActionType.WAIT_FOR_ENABLED,
            description="Wait for element to become enabled",
            params={"wait_ms": wait_ms},
            timeout_ms=wait_ms + 1000,
        ),
    ]


def _detached_recovery(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Stale element: wait for DOM settle → re-resolve."""
    return [
        RecoveryAction(
            action_type=RecoveryActionType.WAIT_AND_RETRY,
            description="Wait for DOM to stabilize after re-render",
            params={"wait_ms": 1000},
            timeout_ms=2000,
        ),
        RecoveryAction(
            action_type=RecoveryActionType.RE_RESOLVE_SELECTOR,
            description="Re-resolve element after DOM change",
            timeout_ms=3000,
        ),
    ]


def _wrong_page_recovery(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Wrong page: navigate back."""
    return [
        RecoveryAction(
            action_type=RecoveryActionType.NAVIGATE_BACK,
            description="Navigate back to expected page",
            timeout_ms=5000,
        ),
        RecoveryAction(
            action_type=RecoveryActionType.WAIT_FOR_LOAD,
            description="Wait for page load after navigation",
            timeout_ms=5000,
        ),
    ]


def _loading_recovery(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Page still loading: wait for load complete."""
    return [
        RecoveryAction(
            action_type=RecoveryActionType.WAIT_FOR_LOAD,
            description="Wait for page load to complete",
            timeout_ms=8000,
        ),
    ]


def _network_recovery(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Network error: wait → retry or refresh."""
    actions = [
        RecoveryAction(
            action_type=RecoveryActionType.WAIT_AND_RETRY,
            description="Wait for network recovery",
            params={"wait_ms": 2000 * (attempt + 1)},
            timeout_ms=5000,
        ),
    ]
    if attempt >= 1:
        actions.append(RecoveryAction(
            action_type=RecoveryActionType.REFRESH_PAGE,
            description="Refresh page to reset network state",
            timeout_ms=10000,
        ))
    return actions


def _timeout_recovery(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Timeout: wait longer → retry with increased budget."""
    return [
        RecoveryAction(
            action_type=RecoveryActionType.WAIT_AND_RETRY,
            description="Wait before retry with increased timeout",
            params={"wait_ms": 1000},
            timeout_ms=2000,
        ),
    ]


def _ambiguous_recovery(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Ambiguous selector: re-resolve with disambiguation."""
    return [
        RecoveryAction(
            action_type=RecoveryActionType.RE_RESOLVE_SELECTOR,
            description="Re-resolve with disambiguation enabled",
            params={"enable_disambiguation": True},
            timeout_ms=5000,
        ),
    ]


def _default_strategy(diagnosis: FailureDiagnosis, attempt: int) -> list[RecoveryAction]:
    """Default: wait and retry."""
    return [
        RecoveryAction(
            action_type=RecoveryActionType.WAIT_AND_RETRY,
            description="Wait and retry (unknown failure)",
            params={"wait_ms": 1000 * (attempt + 1)},
            timeout_ms=3000,
        ),
    ]


# Map failure types to recovery strategy functions
_STRATEGY_MAP: dict[FailureType, callable] = {
    FailureType.OVERLAY_BLOCKING: _overlay_recovery,
    FailureType.DIALOG_BLOCKING: _dialog_recovery,
    FailureType.TOAST_BLOCKING: _toast_recovery,
    FailureType.ELEMENT_NOT_FOUND: _not_found_recovery,
    FailureType.ELEMENT_NOT_VISIBLE: _not_visible_recovery,
    FailureType.ELEMENT_DISABLED: _disabled_recovery,
    FailureType.ELEMENT_DETACHED: _detached_recovery,
    FailureType.STALE_REFERENCE: _detached_recovery,
    FailureType.WRONG_PAGE: _wrong_page_recovery,
    FailureType.PAGE_LOADING: _loading_recovery,
    FailureType.NETWORK_ERROR: _network_recovery,
    FailureType.NAVIGATION_TIMEOUT: _timeout_recovery,
    FailureType.SELECTOR_AMBIGUOUS: _ambiguous_recovery,
}

# Failure types where regenerating intent doesn't help
_NO_REGEN_TYPES = frozenset({
    FailureType.OVERLAY_BLOCKING,
    FailureType.DIALOG_BLOCKING,
    FailureType.TOAST_BLOCKING,
    FailureType.WRONG_PAGE,
    FailureType.PAGE_LOADING,
    FailureType.NETWORK_ERROR,
    FailureType.PAGE_CRASH,
    FailureType.FRAME_DETACHED,
})


def execute_recovery_action(page: Page, action: RecoveryAction) -> bool:
    """Execute a single recovery action on the page.

    Returns True if the action succeeded, False otherwise.
    Non-fatal: failure to recover doesn't crash the test.
    """
    try:
        if action.action_type == RecoveryActionType.PRESS_ESCAPE:
            page.keyboard.press("Escape")
            budget_wait(page, 300, "recovery_escape")
            return True

        elif action.action_type == RecoveryActionType.CLICK_BACKDROP:
            # Click at coordinates likely to be outside modal content
            page.mouse.click(10, 10)
            budget_wait(page, 300, "recovery_backdrop")
            return True

        elif action.action_type == RecoveryActionType.DISMISS_DIALOG:
            # Try common dialog close patterns
            close_selectors = [
                '[aria-label="Close"]',
                '[data-dismiss="dialog"]',
                'button:has(svg[class*="close"])',
                'button:has(svg[class*="x"])',
                '[role="dialog"] button:first-of-type',
            ]
            for sel in close_selectors:
                try:
                    btn = resolve_single(page.locator(sel), context=f"recovery_close:{sel[:30]}", timeout_ms=1000)
                    if btn.is_visible(timeout=1000):
                        btn.click()
                        budget_wait(page, 300, "recovery_dismiss_dialog")
                        return True
                except Exception:
                    continue
            # Fallback: Escape
            page.keyboard.press("Escape")
            budget_wait(page, 300, "recovery_dismiss_dialog_escape")
            return True

        elif action.action_type == RecoveryActionType.DISMISS_TOAST:
            # Try to dismiss toast, or wait for auto-dismiss
            toast_selectors = [
                '[data-sonner-toast] button[data-close]',
                '[class*="toast"] button[aria-label="Close"]',
                '[role="status"] button',
            ]
            for sel in toast_selectors:
                try:
                    btn = resolve_single(page.locator(sel), context=f"recovery_toast:{sel[:30]}", timeout_ms=500)
                    if btn.is_visible(timeout=500):
                        btn.click()
                        return True
                except Exception:
                    continue
            # Auto-dismiss wait
            budget_wait(page, action.params.get("wait_ms", 2000), "recovery_toast_autodismiss")
            return True

        elif action.action_type == RecoveryActionType.SCROLL_TO_ELEMENT:
            direction = action.params.get("direction", "down")
            if direction == "down":
                page.mouse.wheel(0, 300)
            else:
                page.mouse.wheel(0, -300)
            budget_wait(page, 300, "recovery_scroll")
            return True

        elif action.action_type == RecoveryActionType.WAIT_AND_RETRY:
            wait_ms = action.params.get("wait_ms", 1000)
            budget_wait(page, wait_ms, "recovery_wait_retry")
            return True

        elif action.action_type == RecoveryActionType.WAIT_FOR_ENABLED:
            budget_wait(page, action.params.get("wait_ms", 2000), "recovery_wait_enabled")
            return True

        elif action.action_type == RecoveryActionType.WAIT_FOR_LOAD:
            try:
                page.wait_for_load_state("networkidle", timeout=action.timeout_ms)
            except Exception:
                page.wait_for_load_state("domcontentloaded", timeout=action.timeout_ms)
            return True

        elif action.action_type == RecoveryActionType.RE_RESOLVE_SELECTOR:
            # This is a signal to the executor — no direct page action
            return True

        elif action.action_type == RecoveryActionType.NAVIGATE_BACK:
            page.go_back(timeout=action.timeout_ms)
            return True

        elif action.action_type == RecoveryActionType.REFRESH_PAGE:
            page.reload(timeout=action.timeout_ms)
            return True

        elif action.action_type == RecoveryActionType.NONE:
            return True

        return False

    except Exception as e:
        logger.warning(
            "recovery.action_failed",
            action=action.action_type.value,
            error=str(e)[:200],
        )
        return False


def execute_recovery_plan(
    page: Page, plan: RecoveryPlan,
) -> list[tuple[RecoveryAction, bool]]:
    """Execute all actions in a recovery plan.

    Returns a list of (action, succeeded) tuples for per-action tracking.
    """
    if not plan.actions:
        return []

    results: list[tuple[RecoveryAction, bool]] = []
    for action in plan.actions:
        success = execute_recovery_action(page, action)
        results.append((action, success))
        logger.debug(
            "recovery.action_result",
            action=action.action_type.value,
            success=success,
        )

    return results
