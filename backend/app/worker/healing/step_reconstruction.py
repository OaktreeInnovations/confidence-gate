"""Step Reconstruction — Healing Layer 4.

When Layers 1-3 all fail, this layer asks AI to decompose the failing action
into 1-3 smaller sub-actions that achieve the same goal using elements
currently visible on the page.

Each reconstructed action is validated through:
1. resolve_target() — scoring pipeline (composite >= 0.5)
2. ensure_interactable() — interaction guard

If ANY reconstructed action fails validation, the entire reconstruction
is rejected (all-or-nothing).

Hard rules:
- AI never directly executes browser actions
- All targets through scoring pipeline
- Max 3 reconstructed actions
- Guard checks required for interactive actions
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from app.worker.healing.ai_repair import _get_a11y_tree, _get_interactive_html
from app.worker.interaction_guard import ensure_interactable
from app.worker.intent_schema import (
    ActionIntent,
    ActionType,
    TargetDescriptor,
    _TARGETLESS_ACTIONS,
)
from app.worker.selector_engine import resolve_target

if TYPE_CHECKING:
    from app.worker.ai_provider import AIProvider
    from app.worker.recovery.diagnosis import FailureDiagnosis
    from playwright.sync_api import Page

logger = structlog.get_logger(__name__)

# --- Constants ---
_MAX_RECONSTRUCTED_ACTIONS = 3
_MIN_RECONSTRUCTION_CONFIDENCE = 0.6
_MIN_VALIDATION_SCORE = 0.5

# Interactive actions that require guard checks
_INTERACTIVE_ACTIONS = frozenset({
    ActionType.CLICK, ActionType.INPUT, ActionType.SELECT,
    ActionType.SELECT_DATE, ActionType.CHECK, ActionType.UNCHECK,
    ActionType.UPLOAD_FILE,
})

_RECONSTRUCTION_PROMPT = """You are a test step reconstruction specialist. A test automation action failed because the UI element or interaction pattern has changed.

Original failing action:
{action_desc}

Target element:
{target_desc}

Failure diagnosis:
{diagnosis_desc}

Previously tried selectors that failed:
{failed_selectors}

Current page accessibility tree:
{a11y_tree}

Current page interactive elements:
{interactive_html}

Your task: Decompose this failing action into 1-3 smaller sub-actions that achieve the same end goal using elements CURRENTLY VISIBLE on the page. Each sub-action must target a distinct, real element from the accessibility tree or interactive elements above.

Return ONLY a JSON object:
{{"actions": [{{"action": "click|input|select|check|uncheck|wait_for_text|navigate", "target": {{"role": "...", "name": "...", "label": "...", "text": "...", "test_id": "...", "css": "...", "placeholder": "..."}}, "value": "optional"}}], "confidence": 0.0-1.0, "reasoning": "brief explanation"}}

Rules:
- Maximum 3 actions
- Each action must reference a real element visible in the page context
- Only use allowed action types: click, input, select, check, uncheck, wait_for_text, navigate
- Do not invent elements that are not in the accessibility tree or interactive elements
- confidence reflects how certain you are these actions achieve the original goal"""


@dataclass
class StepReconstructionResult:
    """Result of a step reconstruction attempt."""

    reconstructed: bool
    original_action: ActionIntent | None = None
    reconstructed_actions: list[ActionIntent] = field(default_factory=list)
    confidence: float = 0.0
    ai_reasoning: str = ""
    validation_scores: list[float] = field(default_factory=list)
    elapsed_ms: int = 0


def reconstruct_step(
    page: Page,
    action: ActionIntent,
    diagnosis: FailureDiagnosis,
    ai_client: AIProvider,
    failed_selectors: list[str] | None = None,
    timeout_ms: int = 5000,
) -> StepReconstructionResult:
    """Attempt to reconstruct a failing action as 1-3 sub-actions.

    Args:
        page: Playwright Page instance.
        action: The action that failed.
        diagnosis: Failure diagnosis.
        ai_client: AI provider with raw_client access.
        failed_selectors: Selectors already tried and failed.
        timeout_ms: Budget for this reconstruction attempt.

    Returns:
        StepReconstructionResult with reconstructed actions or reconstructed=False.
    """
    t0 = time.monotonic()

    # Early exit if no AI available
    raw = ai_client.raw_client
    if raw is None:
        return StepReconstructionResult(reconstructed=False, elapsed_ms=0)

    try:
        # Capture page context
        a11y_tree = _get_a11y_tree(page)
        interactive_html = _get_interactive_html(page)

        target_desc = (
            action.target.model_dump(exclude_none=True)
            if action.target else {}
        )
        prompt = _RECONSTRUCTION_PROMPT.format(
            action_desc=f"{action.action.value} {action.value or ''}".strip(),
            target_desc=json.dumps(target_desc),
            diagnosis_desc=f"{diagnosis.failure_type.value}: {diagnosis.root_cause}",
            failed_selectors=json.dumps(failed_selectors or []),
            a11y_tree=a11y_tree[:2000],
            interactive_html=interactive_html[:2000],
        )

        # Call AI
        response = raw.chat.completions.create(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=500,
        )

        text = response.choices[0].message.content.strip()

        # Handle markdown code fences
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result_data = json.loads(text)
        raw_actions = result_data.get("actions", [])
        confidence = float(result_data.get("confidence", 0.0))
        reasoning = result_data.get("reasoning", "")

        # Confidence check
        if confidence < _MIN_RECONSTRUCTION_CONFIDENCE:
            logger.debug(
                "step_reconstruction.low_confidence",
                confidence=confidence,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return StepReconstructionResult(
                reconstructed=False,
                original_action=action,
                confidence=confidence,
                ai_reasoning=reasoning,
                elapsed_ms=elapsed_ms,
            )

        # Action count check
        if not raw_actions or len(raw_actions) > _MAX_RECONSTRUCTED_ACTIONS:
            logger.debug(
                "step_reconstruction.invalid_count",
                count=len(raw_actions),
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return StepReconstructionResult(
                reconstructed=False,
                original_action=action,
                confidence=confidence,
                ai_reasoning=reasoning,
                elapsed_ms=elapsed_ms,
            )

        # Parse into ActionIntent objects
        parsed_actions = _parse_actions(raw_actions)
        if parsed_actions is None:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return StepReconstructionResult(
                reconstructed=False,
                original_action=action,
                confidence=confidence,
                ai_reasoning=reasoning,
                elapsed_ms=elapsed_ms,
            )

        # Validate all actions (all-or-nothing)
        validated, scores = _validate_actions(
            page, parsed_actions, timeout_ms,
        )
        if validated is None:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return StepReconstructionResult(
                reconstructed=False,
                original_action=action,
                confidence=confidence,
                ai_reasoning=reasoning,
                validation_scores=scores,
                elapsed_ms=elapsed_ms,
            )

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "step_reconstruction.success",
            num_actions=len(validated),
            confidence=confidence,
            scores=[round(s, 3) for s in scores],
            elapsed_ms=elapsed_ms,
        )

        return StepReconstructionResult(
            reconstructed=True,
            original_action=action,
            reconstructed_actions=validated,
            confidence=confidence,
            ai_reasoning=reasoning,
            validation_scores=scores,
            elapsed_ms=elapsed_ms,
        )

    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.warning(
            "step_reconstruction.error",
            error=str(e)[:200],
            elapsed_ms=elapsed_ms,
        )
        return StepReconstructionResult(
            reconstructed=False,
            original_action=action,
            elapsed_ms=elapsed_ms,
        )


def _parse_actions(raw_actions: list[dict]) -> list[ActionIntent] | None:
    """Parse raw action dicts from AI into ActionIntent objects.

    Returns None if any action fails to parse.
    """
    parsed = []
    for raw in raw_actions:
        try:
            action_type = ActionType(raw["action"])
            target_data = raw.get("target") or {}
            # Filter out None/empty values
            target_fields = {k: v for k, v in target_data.items() if v}
            target = TargetDescriptor(**target_fields) if target_fields else None
            parsed.append(ActionIntent(
                action=action_type,
                target=target,
                value=raw.get("value"),
            ))
        except (KeyError, ValueError) as e:
            logger.debug(
                "step_reconstruction.parse_failed",
                raw_action=str(raw)[:200],
                error=str(e),
            )
            return None
    return parsed


def _validate_actions(
    page: Page,
    actions: list[ActionIntent],
    timeout_ms: int,
) -> tuple[list[ActionIntent] | None, list[float]]:
    """Validate all reconstructed actions through scoring pipeline + guard.

    Returns:
        (validated_actions, scores) if all pass, or (None, partial_scores) if any fail.
    """
    scores: list[float] = []
    per_action_timeout = max(timeout_ms // len(actions), 1500)

    for ai_action in actions:
        # Targetless actions skip resolution and guard
        if ai_action.target is None or ai_action.target.is_empty():
            if ai_action.action in _TARGETLESS_ACTIONS or (
                ai_action.action == ActionType.WAIT_FOR_TEXT and ai_action.value
            ):
                scores.append(1.0)
                continue
            # Non-targetless action without a target — invalid
            logger.debug(
                "step_reconstruction.missing_target",
                action=ai_action.action.value,
            )
            return None, scores

        # Resolve target through scoring pipeline
        try:
            resolved = resolve_target(
                page, ai_action.target,
                timeout_ms=min(per_action_timeout, 3000),
            )
        except (RuntimeError, TimeoutError) as e:
            logger.debug(
                "step_reconstruction.resolve_failed",
                action=ai_action.action.value,
                error=str(e)[:200],
            )
            return None, scores

        if resolved.score.composite < _MIN_VALIDATION_SCORE:
            logger.debug(
                "step_reconstruction.low_score",
                action=ai_action.action.value,
                composite=round(resolved.score.composite, 3),
            )
            return None, scores

        # Interaction guard for interactive actions
        if ai_action.action in _INTERACTIVE_ACTIONS:
            guard = ensure_interactable(
                resolved.locator,
                max_wait_ms=min(per_action_timeout, 2000),
            )
            if not guard.passed:
                logger.debug(
                    "step_reconstruction.guard_failed",
                    action=ai_action.action.value,
                    checks_failed=guard.checks_failed,
                )
                return None, scores

        scores.append(resolved.score.composite)

    return actions, scores
