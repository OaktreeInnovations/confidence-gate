"""Disambiguation logic for ambiguous selector matches.

When the top candidates have scores within a threshold (ambiguity zone),
disambiguation kicks in with three layers:

1. Strategy combination: Try intersecting multiple strategies
   (e.g., role+name AND label both matching narrows to one element)
2. DOM proximity tiebreak: Prefer the candidate closest to the last action
3. AI disambiguation (last resort): Send candidate metadata + screenshot
   to AI for a targeted "which element?" decision — NOT regeneration

All layers are deterministic except Layer 3, which is explicitly gated.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import structlog

from app.worker.selector_engine.scoring import CandidateScore

if TYPE_CHECKING:
    from app.worker.ai_provider import AIProvider
    from playwright.sync_api import Page

logger = structlog.get_logger(__name__)

# Score difference threshold below which candidates are "ambiguous"
AMBIGUITY_THRESHOLD = 0.08


def is_ambiguous(candidates: list[CandidateScore]) -> bool:
    """Check if the top candidates are too close in score to pick one."""
    if len(candidates) < 2:
        return False
    return (candidates[0].composite - candidates[1].composite) < AMBIGUITY_THRESHOLD


def disambiguate(
    page: Page,
    candidates: list[CandidateScore],
    action_description: str = "",
    ai_client: AIProvider | None = None,
    last_action_selector: str | None = None,
) -> CandidateScore | None:
    """Attempt to disambiguate between close-scored candidates.

    Layers (tried in order):
    1. Strategy combination — cross-reference attributes to narrow
    2. DOM proximity tiebreak
    3. AI disambiguation (only if ai_client provided)

    Returns the winning candidate, or None if disambiguation fails.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Only consider candidates within the ambiguity zone of the top score
    top_score = candidates[0].composite
    contenders = [
        c for c in candidates
        if (top_score - c.composite) < AMBIGUITY_THRESHOLD
    ]

    if len(contenders) == 1:
        return contenders[0]

    logger.info(
        "disambiguate.start",
        num_contenders=len(contenders),
        scores=[round(c.composite, 3) for c in contenders],
        action=action_description[:60],
    )

    # Layer 1: Strategy combination
    result = _disambiguate_by_strategy_combination(contenders)
    if result:
        logger.info("disambiguate.resolved", layer="strategy_combination")
        return result

    # Layer 2: DOM proximity tiebreak
    if last_action_selector:
        result = _disambiguate_by_proximity(contenders)
        if result:
            logger.info("disambiguate.resolved", layer="proximity")
            return result

    # Layer 3: AI disambiguation (gated)
    if ai_client is not None:
        result = _disambiguate_by_ai(
            page, contenders, action_description, ai_client,
        )
        if result:
            logger.info("disambiguate.resolved", layer="ai")
            return result

    logger.warning(
        "disambiguate.failed",
        num_contenders=len(contenders),
        action=action_description[:60],
    )
    return None


def _disambiguate_by_strategy_combination(
    contenders: list[CandidateScore],
) -> CandidateScore | None:
    """Narrow by cross-referencing element attributes across strategies.

    If one candidate has a data-testid while others don't, it wins.
    If one candidate has both role match AND label match, it wins.
    """
    # Boost candidates with more identifying attributes
    boosted = []
    for c in contenders:
        bonus = 0.0
        meta_keys = [
            ("test_id", 0.15),
            ("aria_label", 0.08),
            ("id", 0.10),
            ("name_attr", 0.05),
            ("placeholder", 0.05),
        ]
        # These come from element evaluation in scoring
        # Check if the element has additional identifying attributes
        # that aren't part of the primary strategy
        try:
            attrs = c.locator.evaluate("""(el) => ({
                test_id: el.getAttribute('data-testid') || '',
                aria_label: el.getAttribute('aria-label') || '',
                id: el.id || '',
                name_attr: el.getAttribute('name') || '',
                placeholder: el.getAttribute('placeholder') || '',
            })""")
            for attr_key, weight in meta_keys:
                if attrs.get(attr_key):
                    bonus += weight
        except Exception:
            pass

        boosted.append((c, c.composite + bonus))

    boosted.sort(key=lambda x: x[1], reverse=True)

    # Only resolve if the boosted gap exceeds the threshold
    if len(boosted) >= 2:
        gap = boosted[0][1] - boosted[1][1]
        if gap >= AMBIGUITY_THRESHOLD:
            return boosted[0][0]

    return None


def _disambiguate_by_proximity(
    contenders: list[CandidateScore],
) -> CandidateScore | None:
    """Pick the candidate with the highest proximity score.

    Only resolves if there's a clear proximity winner (gap > half threshold).
    """
    sorted_by_prox = sorted(
        contenders, key=lambda c: c.proximity_score, reverse=True,
    )
    if len(sorted_by_prox) >= 2:
        gap = sorted_by_prox[0].proximity_score - sorted_by_prox[1].proximity_score
        if gap >= AMBIGUITY_THRESHOLD / 2:
            return sorted_by_prox[0]
    return None


_AI_DISAMBIGUATE_PROMPT = """\
You are a test automation element disambiguator.

Given a list of candidate HTML elements and the action being performed,
respond with ONLY the 0-based index of the correct element to interact with.

Action being performed: {action}

Candidate elements:
{candidates}

Rules:
- Output ONLY a single integer (the index).
- Pick the element that most likely matches the intended action.
- Prefer interactive elements (buttons, inputs, links) over containers.
- Prefer visible, enabled elements.
- If truly unable to determine, output 0.
"""


def _disambiguate_by_ai(
    page: Page,
    contenders: list[CandidateScore],
    action_description: str,
    ai_client: AIProvider,
) -> CandidateScore | None:
    """Use AI to pick the correct element from ambiguous candidates.

    Sends element metadata (NOT full page context) to minimize token cost.
    This is a targeted disambiguation call, not intent regeneration.
    """
    candidate_descs = []
    for i, c in enumerate(contenders[:5]):  # Cap at 5 candidates
        try:
            attrs = c.locator.evaluate("""(el) => ({
                tag: el.tagName.toLowerCase(),
                text: (el.textContent || '').trim().substring(0, 100),
                role: el.getAttribute('role') || '',
                aria_label: el.getAttribute('aria-label') || '',
                test_id: el.getAttribute('data-testid') || '',
                placeholder: el.getAttribute('placeholder') || '',
                type: el.getAttribute('type') || '',
                href: el.getAttribute('href') || '',
                class: (el.className || '').toString().substring(0, 80),
                visible: el.getBoundingClientRect().width > 0,
            })""")
            desc_parts = [f"[{i}] <{attrs.get('tag', '?')}>"]
            for key in ["role", "aria_label", "test_id", "text", "placeholder", "type", "href"]:
                val = attrs.get(key, "")
                if val:
                    desc_parts.append(f"  {key}={val!r}")
            desc_parts.append(f"  visible={attrs.get('visible', '?')}")
            desc_parts.append(f"  score={c.composite:.3f}")
            candidate_descs.append("\n".join(desc_parts))
        except Exception:
            candidate_descs.append(f"[{i}] <unknown> score={c.composite:.3f}")

    prompt = _AI_DISAMBIGUATE_PROMPT.format(
        action=action_description,
        candidates="\n\n".join(candidate_descs),
    )

    # Optional: include a small screenshot for visual context
    messages: list[dict] = []
    try:
        screenshot = page.screenshot(type="jpeg", quality=40, full_page=False)
        b64 = base64.b64encode(screenshot).decode()
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
                {"type": "text", "text": prompt},
            ],
        })
    except Exception:
        messages.append({"role": "user", "content": prompt})

    try:
        raw_client = ai_client.raw_client if hasattr(ai_client, "raw_client") else ai_client
        if raw_client is None:
            return None
        response = raw_client.chat.completions.create(
            model="gpt-4o-mini",  # Cheap model for disambiguation
            max_completion_tokens=16,
            messages=messages,
        )
        raw = response.choices[0].message.content.strip()
        # Parse the index
        idx = int(raw.strip().split()[0])
        if 0 <= idx < len(contenders):
            logger.info(
                "disambiguate.ai_picked",
                index=idx,
                score=contenders[idx].composite,
            )
            return contenders[idx]
    except Exception as e:
        logger.warning("disambiguate.ai_failed", error=str(e)[:200])

    return None
