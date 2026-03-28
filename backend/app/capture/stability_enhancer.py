"""Add stability hints to captured StepIntents.

After assertions are added, this pass inspects each step and prepends
WAIT_FOR_NAVIGATION or WAIT_FOR_TEXT guards where the signal suggests
the page may be in transition before the next action.

Heuristics:
- Step follows a NAVIGATE or a CLICK-with-navigation → ensure navigation settled
- Step follows a form submit signal (click on button type=submit) → guard
- Two consecutive INPUT steps on different fields → no extra wait needed
- Step target has aria-expanded-like label (menu, dialog, modal) → short guard
"""

from __future__ import annotations

import structlog

from app.worker.intent_schema import (
    ActionIntent,
    ActionType,
    StepIntent,
)

logger = structlog.get_logger(__name__)

# Labels that suggest the step opens a dynamic overlay
_OVERLAY_HINTS = frozenset({
    "menu", "dialog", "modal", "dropdown", "popover", "tooltip",
    "drawer", "sidebar", "sheet", "panel",
})


def _has_overlay_hint(step: StepIntent) -> bool:
    if not step.actions:
        return False
    target = step.actions[0].target
    if not target:
        return False
    label_parts = " ".join(filter(None, [target.name, target.label, target.text, target.role]))
    return any(hint in label_parts.lower() for hint in _OVERLAY_HINTS)


def _already_has_wait(step: StepIntent) -> bool:
    return any(
        a.action in (ActionType.WAIT_FOR_NAVIGATION, ActionType.WAIT_FOR_TEXT)
        for a in step.actions
    )


def _previous_was_navigation(prev: StepIntent | None) -> bool:
    if not prev or not prev.actions:
        return False
    return prev.actions[0].action in (ActionType.NAVIGATE, ActionType.CLICK)


def _previous_triggered_navigation(prev: StepIntent | None) -> bool:
    """True if prev step already has a WAIT_FOR_NAVIGATION action appended."""
    if not prev:
        return False
    return any(a.action == ActionType.WAIT_FOR_NAVIGATION for a in prev.actions)


def enhance_stability(intents: list[StepIntent]) -> list[StepIntent]:
    """Prepend stability guards to steps that follow navigation-triggering actions.

    Mutates steps in-place. Returns the same list.
    """
    for i, step in enumerate(intents):
        prev = intents[i - 1] if i > 0 else None

        # Skip if already has a wait guard
        if _already_has_wait(step):
            continue

        # If the previous step triggered navigation, prepend WAIT_FOR_NAVIGATION
        if _previous_triggered_navigation(prev):
            step.actions.insert(
                0,
                ActionIntent(action=ActionType.WAIT_FOR_NAVIGATION),
            )
            continue

        # If this step targets an overlay-like element, prepend a text guard
        if _has_overlay_hint(step) and prev is not None:
            target = step.actions[0].target if step.actions else None
            hint_text = (
                (target.name or target.text or target.label or "")
                if target
                else ""
            )
            if hint_text:
                step.actions.insert(
                    0,
                    ActionIntent(
                        action=ActionType.WAIT_FOR_TEXT,
                        value=hint_text[:80],
                    ),
                )

    logger.info("stability_enhancer.complete", steps=len(intents))
    return intents
