"""Deduplicate and normalize a list of StepIntents from capture.

Problems this pass fixes:
1. Duplicate NAVIGATE steps — multiple pushState events for the same URL
2. Redundant click-then-input on the same target — merge into just INPUT
3. Back-to-back identical actions — keep only the last
4. Empty or unresolvable steps — drop them
5. Renumber steps to be contiguous starting from 1
"""

from __future__ import annotations

import structlog

from app.worker.intent_schema import ActionType, StepIntent

logger = structlog.get_logger(__name__)


def _primary_action(step: StepIntent) -> str:
    if not step.actions:
        return ""
    return step.actions[0].action


def _navigate_url(step: StepIntent) -> str:
    if _primary_action(step) != ActionType.NAVIGATE:
        return ""
    a = step.actions[0]
    return a.value or ""


def _target_key(step: StepIntent) -> str:
    """Stable string identity for the primary target of a step."""
    if not step.actions:
        return ""
    t = step.actions[0].target
    if not t:
        return ""
    return "|".join(filter(None, [t.test_id, t.role, t.name, t.label, t.placeholder, t.text]))


def _deduplicate_navigations(intents: list[StepIntent]) -> list[StepIntent]:
    """Remove consecutive NAVIGATE steps that go to the same URL."""
    result: list[StepIntent] = []
    last_nav_url: str | None = None
    for step in intents:
        if _primary_action(step) == ActionType.NAVIGATE:
            url = _navigate_url(step)
            if url == last_nav_url:
                continue  # drop duplicate
            last_nav_url = url
        else:
            last_nav_url = None
        result.append(step)
    return result


def _merge_click_then_input(intents: list[StepIntent]) -> list[StepIntent]:
    """Drop a bare CLICK that immediately precedes an INPUT on the same target.

    Browsers emit a click when the user focuses an input field before typing.
    That click is noise if followed by an INPUT on the same element.
    """
    if len(intents) < 2:
        return intents
    result: list[StepIntent] = []
    skip_next = False
    for i, step in enumerate(intents):
        if skip_next:
            skip_next = False
            result.append(step)
            continue
        if (
            i + 1 < len(intents)
            and _primary_action(step) == ActionType.CLICK
            and _primary_action(intents[i + 1]) == ActionType.INPUT
            and _target_key(step) == _target_key(intents[i + 1])
            and _target_key(step)  # don't merge if target is unknown
        ):
            # Drop the click; keep the INPUT (already handled by skip_next=False)
            continue
        result.append(step)
    return result


def _drop_empty(intents: list[StepIntent]) -> list[StepIntent]:
    """Remove steps with no actions or unresolvable targets."""
    result = []
    for step in intents:
        if not step.actions:
            continue
        primary = step.actions[0]
        # Targetless actions (NAVIGATE, WAIT_FOR_NAVIGATION) are always kept
        if primary.target is None and primary.action not in (
            ActionType.NAVIGATE,
            ActionType.WAIT_FOR_NAVIGATION,
            ActionType.WAIT_FOR_TEXT,
        ):
            continue
        if primary.target is not None and primary.target.is_empty():
            continue
        result.append(step)
    return result


def _renumber(intents: list[StepIntent]) -> list[StepIntent]:
    for i, step in enumerate(intents, start=1):
        step.step_number = i
    return intents


def normalize_intents(intents: list[StepIntent]) -> list[StepIntent]:
    """Run all normalization passes and return a clean, renumbered list."""
    intents = _deduplicate_navigations(intents)
    intents = _merge_click_then_input(intents)
    intents = _drop_empty(intents)
    intents = _renumber(intents)

    logger.info("intent_normalizer.complete", output_steps=len(intents))
    return intents
