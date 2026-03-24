"""AI Structural Repair — AI-driven selector repair as last resort.

Only invoked after deterministic repair strategies fail. Sends AI:
- DOM snapshot + a11y tree
- Failure diagnosis + failed selectors
- Target description

AI returns a selector + optional action override + confidence.
Result MUST pass through score_candidates() and ensure_interactable()
before being accepted. AI cannot bypass guards.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from app.worker.interaction_guard import ensure_interactable
from app.worker.selector_engine.scoring import (
    CandidateScore,
    score_candidates,
)
from app.worker.selector_resilience import SelectorStrategy

if TYPE_CHECKING:
    from app.worker.ai_provider import AIProvider
    from app.worker.intent_schema import ActionIntent, TargetDescriptor
    from app.worker.recovery.diagnosis import FailureDiagnosis
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)

# Minimum AI confidence to accept a repair
_MIN_AI_CONFIDENCE = 0.6

# Minimum composite score after pipeline validation
_MIN_PIPELINE_SCORE = 0.5


@dataclass
class AIRepairResult:
    """Result of an AI structural repair attempt."""

    repaired: bool
    locator: Locator | None = None
    selector: str = ""
    action_override: str = ""
    confidence: float = 0.0
    pipeline_score: float = 0.0
    ai_reasoning: str = ""
    elapsed_ms: int = 0


_AI_REPAIR_PROMPT = """You are a DOM element repair specialist. A test automation step failed to find or interact with an element.

Target element description:
{target_desc}

Action that failed:
{action_desc}

Failure diagnosis:
{diagnosis_desc}

Selectors that were already tried and failed:
{failed_selectors}

Current page accessibility tree:
{a11y_tree}

Current page interactive elements:
{interactive_html}

Propose a CSS selector to locate the target element. If the action type should change
(e.g., click → keyboard interaction), suggest an action override.

Return ONLY a JSON object:
{{"selector": "your-css-selector", "confidence": 0.0-1.0, "reasoning": "brief explanation", "action_override": ""}}

confidence should reflect certainty that this selector uniquely identifies the correct element.
action_override should be empty string unless the interaction method needs to change."""


def ai_structural_repair(
    page: Page,
    target: TargetDescriptor,
    action: ActionIntent,
    diagnosis: FailureDiagnosis,
    ai_client: AIProvider,
    failed_selectors: list[str] | None = None,
    timeout_ms: int = 5000,
) -> AIRepairResult:
    """Attempt AI-driven structural repair.

    Args:
        page: Playwright Page instance.
        target: Original target descriptor.
        action: The action that failed.
        diagnosis: Failure diagnosis.
        ai_client: AI provider with raw_client access.
        failed_selectors: Selectors already tried.
        timeout_ms: Budget for this repair attempt.

    Returns:
        AIRepairResult with validated locator or repaired=False.
    """
    t0 = time.monotonic()

    raw = ai_client.raw_client
    if raw is None:
        return AIRepairResult(repaired=False, elapsed_ms=0)

    if target is None:
        return AIRepairResult(repaired=False, elapsed_ms=0)

    try:
        # Capture page context
        a11y_tree = _get_a11y_tree(page)
        interactive_html = _get_interactive_html(page)

        target_desc = target.model_dump(exclude_none=True)
        prompt = _AI_REPAIR_PROMPT.format(
            target_desc=json.dumps(target_desc),
            action_desc=f"{action.action.value} {action.value or ''}".strip(),
            diagnosis_desc=f"{diagnosis.failure_type.value}: {diagnosis.root_cause}",
            failed_selectors=json.dumps(failed_selectors or []),
            a11y_tree=a11y_tree[:2000],
            interactive_html=interactive_html[:2000],
        )

        response = raw.chat.completions.create(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=300,
        )

        text = response.choices[0].message.content.strip()
        # Handle markdown code fences
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)
        selector = result.get("selector", "")
        confidence = result.get("confidence", 0.0)
        reasoning = result.get("reasoning", "")
        action_override = result.get("action_override", "")

        # Hard rule: reject low confidence
        if not selector or confidence < _MIN_AI_CONFIDENCE:
            logger.debug(
                "ai_repair.low_confidence",
                selector=selector,
                confidence=confidence,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return AIRepairResult(
                repaired=False,
                confidence=confidence,
                ai_reasoning=reasoning,
                elapsed_ms=elapsed_ms,
            )

        # Hard rule: validate through scoring pipeline
        locator = page.locator(selector)
        candidates = score_candidates(
            page=page,
            locator=locator,
            strategy=SelectorStrategy.CSS,
            params={"selector": selector, "ai_repair": True},
            timeout_ms=min(timeout_ms, 3000),
        )

        if not candidates or candidates[0].composite < _MIN_PIPELINE_SCORE:
            logger.debug(
                "ai_repair.pipeline_rejected",
                selector=selector,
                score=candidates[0].composite if candidates else 0.0,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return AIRepairResult(
                repaired=False,
                selector=selector,
                confidence=confidence,
                pipeline_score=candidates[0].composite if candidates else 0.0,
                ai_reasoning=reasoning,
                elapsed_ms=elapsed_ms,
            )

        best = candidates[0]

        # Hard rule: validate through interaction guard
        guard = ensure_interactable(best.locator, max_wait_ms=min(timeout_ms, 2000))
        if not guard.passed:
            logger.debug(
                "ai_repair.guard_rejected",
                selector=selector,
                guard_failures=guard.checks_failed,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return AIRepairResult(
                repaired=False,
                selector=selector,
                confidence=confidence,
                pipeline_score=best.composite,
                ai_reasoning=reasoning,
                elapsed_ms=elapsed_ms,
            )

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "ai_repair.success",
            selector=selector,
            confidence=confidence,
            pipeline_score=round(best.composite, 3),
            elapsed_ms=elapsed_ms,
        )

        return AIRepairResult(
            repaired=True,
            locator=best.locator,
            selector=selector,
            action_override=action_override,
            confidence=confidence,
            pipeline_score=best.composite,
            ai_reasoning=reasoning,
            elapsed_ms=elapsed_ms,
        )

    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.warning(
            "ai_repair.error",
            error=str(e)[:200],
            elapsed_ms=elapsed_ms,
        )
        return AIRepairResult(repaired=False, elapsed_ms=elapsed_ms)


def _get_a11y_tree(page: Page) -> str:
    """Extract accessibility tree as compact text."""
    try:
        snapshot = page.accessibility.snapshot()
        if not snapshot:
            return "(empty)"
        return json.dumps(snapshot, indent=None, default=str)[:3000]
    except Exception:
        return "(unavailable)"


def _get_interactive_html(page: Page) -> str:
    """Extract interactive elements HTML."""
    try:
        return page.evaluate("""() => {
            const selectors = [
                'input', 'textarea', 'select', 'button', 'a[href]',
                '[role="button"]', '[role="checkbox"]', '[role="combobox"]',
                '[role="option"]', '[role="tab"]', '[data-testid]',
            ];
            const els = document.querySelectorAll(selectors.join(','));
            const results = [];
            for (const el of els) {
                const tag = el.tagName.toLowerCase();
                const id = el.id ? ' id="' + el.id + '"' : '';
                const role = el.getAttribute('role') ? ' role="' + el.getAttribute('role') + '"' : '';
                const label = el.getAttribute('aria-label') ? ' aria-label="' + el.getAttribute('aria-label') + '"' : '';
                const text = (el.textContent || '').trim().substring(0, 50);
                results.push('<' + tag + id + role + label + '>' + text + '</' + tag + '>');
                if (results.length >= 50) break;
            }
            return results.join('\\n');
        }""")
    except Exception:
        return "(unavailable)"
