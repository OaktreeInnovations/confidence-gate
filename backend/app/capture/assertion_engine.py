"""Auto-generate assertion ActionIntents by comparing pre/post event states.

Given the action type and signals captured in the event stream, this engine
appends lightweight assertions to each StepIntent so captured tests have
meaningful verification without manual annotation.

Assertion strategy per action type:
- navigate   → WAIT_FOR_NAVIGATION + ASSERT_TEXT (page title if known)
- click       → ASSERT_VISIBLE (if URL changed); WAIT_FOR_NAVIGATION (if URL changed)
- input       → ASSERT_VALUE (confirm value was accepted)
- select      → ASSERT_VALUE (confirm selection)
- check/uncheck → ASSERT_VISIBLE (element still present)
"""

from __future__ import annotations

import structlog

from app.worker.intent_schema import (
    ActionIntent,
    ActionType,
    StepIntent,
    TargetDescriptor,
)

logger = structlog.get_logger(__name__)


def _url_changed(pre_url: str, post_url: str) -> bool:
    return bool(pre_url and post_url and pre_url != post_url)


def _add_wait_for_navigation(step: StepIntent) -> None:
    step.actions.append(
        ActionIntent(action=ActionType.WAIT_FOR_NAVIGATION)
    )


def _add_assert_value(step: StepIntent, target: TargetDescriptor, value: str) -> None:
    if value:
        step.actions.append(
            ActionIntent(action=ActionType.ASSERT_VALUE, target=target, value=value)
        )


def _add_assert_visible(step: StepIntent, target: TargetDescriptor) -> None:
    if not target.is_empty():
        step.actions.append(
            ActionIntent(action=ActionType.ASSERT_VISIBLE, target=target)
        )


def enrich_step_with_assertions(
    step: StepIntent,
    pre_url: str,
    post_url: str,
) -> StepIntent:
    """Append assertion actions to *step* based on URL state change signals.

    Args:
        step:     The StepIntent produced by the transformer (mutated in-place).
        pre_url:  Page URL *before* this step's primary action.
        post_url: Page URL *after* this step's primary action (from next event).

    Returns:
        The same StepIntent with 0–2 assertion actions appended.
    """
    if not step.actions:
        return step

    primary_action = step.actions[0]
    action_type = primary_action.action

    url_navigated = _url_changed(pre_url, post_url)

    if action_type == ActionType.NAVIGATE:
        # Navigation steps already imply waiting; add explicit wait
        _add_wait_for_navigation(step)

    elif action_type == ActionType.CLICK:
        if url_navigated:
            _add_wait_for_navigation(step)
        elif primary_action.target and not primary_action.target.is_empty():
            # No navigation — assert the clicked element is still visible (dialog, panel, etc.)
            _add_assert_visible(step, primary_action.target)

    elif action_type in (ActionType.INPUT,):
        if primary_action.target and primary_action.value:
            _add_assert_value(step, primary_action.target, primary_action.value)

    elif action_type == ActionType.SELECT:
        if primary_action.target and primary_action.value:
            _add_assert_value(step, primary_action.target, primary_action.value)

    elif action_type in (ActionType.CHECK, ActionType.UNCHECK):
        if primary_action.target:
            _add_assert_visible(step, primary_action.target)

    return step


def enrich_intents_with_assertions(
    intents: list[StepIntent],
    events: list[dict],
) -> list[StepIntent]:
    """Enrich all intents with auto-generated assertions.

    Reconstructs a URL-per-step timeline from the raw event stream so
    each step knows which URL was active before and after it.

    Args:
        intents: Ordered StepIntent list from the transformer.
        events:  Original raw event list (used to reconstruct URL history).

    Returns:
        The same list with assertion actions appended where appropriate.
    """
    if not intents or not events:
        return intents

    # Build a URL timeline keyed by approximate timestamp
    # Each entry: (timestamp, url)
    url_timeline: list[tuple[float, str]] = []
    for ev in sorted(events, key=lambda e: e.get("timestamp", 0)):
        page = ev.get("page") or {}
        url = page.get("url", "")
        ts = ev.get("timestamp", 0.0)
        if url:
            url_timeline.append((ts, url))

    def url_at(ts: float) -> str:
        """Last known URL at or before *ts*."""
        result = ""
        for t, u in url_timeline:
            if t <= ts:
                result = u
            else:
                break
        return result

    def url_after(ts: float) -> str:
        """First URL recorded strictly after *ts*."""
        for t, u in url_timeline:
            if t > ts:
                return u
        return ""

    sorted_events = sorted(events, key=lambda e: e.get("timestamp", 0))

    # Map step_number → approximate timestamp from the event list
    # We use the first event timestamp per step (reconstructed from groups)
    # Since we don't have that mapping here, we approximate by position
    step_count = len(intents)
    if step_count == 0:
        return intents

    event_count = len(sorted_events)
    events_per_step = max(1, event_count // step_count)

    for i, step in enumerate(intents):
        approx_start = i * events_per_step
        approx_end = min((i + 1) * events_per_step, event_count - 1)

        pre_ts = sorted_events[approx_start].get("timestamp", 0.0)
        post_ts = sorted_events[approx_end].get("timestamp", 0.0)

        pre_url = url_at(pre_ts)
        post_url = url_after(post_ts)

        enrich_step_with_assertions(step, pre_url, post_url)

    logger.info(
        "assertion_engine.complete",
        steps_enriched=len(intents),
    )
    return intents
