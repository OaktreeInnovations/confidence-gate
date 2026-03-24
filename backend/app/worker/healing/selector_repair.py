"""Selector Repair Engine — deterministic similarity-based selector recovery.

When resolve_target() fails (ELEMENT_NOT_FOUND) or the resolved element fails
interaction guard, this module tries 5 strategies to find the element through
similarity matching:

1. Text similarity — fuzzy match textContent/aria-label against target
2. Aria-label similarity — match against all [aria-label] elements
3. Label association — follow label[for] or find interactive descendant
4. Interactive ancestor — find text, walk up to nearest interactive parent
5. Element type match — find same tag/role near expected position

Each strategy uses fuzzy matching as a pre-filter, then validates candidates
through score_candidates() for confidence scoring. Accept if composite >= 0.45.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

import structlog

from app.worker.selector_engine.scoring import CandidateScore, _normalize_ws, score_candidates
from app.worker.selector_resilience import SelectorStrategy

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page
    from app.worker.intent_schema import TargetDescriptor
    from app.worker.recovery.diagnosis import FailureDiagnosis

logger = structlog.get_logger(__name__)

# Minimum similarity ratio for fuzzy text matching (pre-filter)
_MIN_SIMILARITY = 0.75

# Minimum pipeline composite score to accept a repaired locator
# (matches resolver's MIN_SELECTOR_CONFIDENCE)
_MIN_REPAIR_CONFIDENCE = 0.45

# JS to collect interactive elements with metadata (capped at 50)
_INTERACTIVE_ELEMENTS_JS = """() => {
    const selectors = [
        'button', 'a[href]', 'input', 'select', 'textarea',
        '[role="button"]', '[role="link"]', '[role="menuitem"]',
        '[role="option"]', '[role="tab"]', '[role="combobox"]',
        '[role="checkbox"]', '[role="radio"]', '[role="switch"]',
        '[tabindex]:not([tabindex="-1"])', '[contenteditable="true"]',
    ];
    const elements = document.querySelectorAll(selectors.join(','));
    const results = [];
    const seen = new Set();
    for (const el of elements) {
        if (seen.has(el)) continue;
        seen.add(el);
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        const visible = rect.width > 0 && rect.height > 0
            && style.display !== 'none'
            && style.visibility !== 'hidden';
        if (!visible) continue;
        results.push({
            tag: el.tagName.toLowerCase(),
            text: (el.textContent || '').trim().substring(0, 120),
            ariaLabel: el.getAttribute('aria-label') || '',
            role: el.getAttribute('role') || '',
            id: el.id || '',
            name: el.getAttribute('name') || '',
            placeholder: el.getAttribute('placeholder') || '',
            testId: el.getAttribute('data-testid') || '',
            rect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
            index: results.length,
        });
        if (results.length >= 50) break;
    }
    return results;
}"""

# JS to collect label elements with their associations
_LABEL_ELEMENTS_JS = """() => {
    const labels = document.querySelectorAll('label');
    const results = [];
    for (const label of labels) {
        const text = (label.textContent || '').trim().substring(0, 120);
        const forAttr = label.getAttribute('for') || '';
        const rect = label.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        results.push({
            text: text,
            forAttr: forAttr,
            rect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
        });
        if (results.length >= 30) break;
    }
    return results;
}"""


@dataclass
class RepairResult:
    """Result of a selector repair attempt."""

    repaired: bool
    locator: Locator | None = None
    strategy_used: str = ""
    confidence: float = 0.0
    candidates_evaluated: int = 0
    elapsed_ms: int = 0


def repair_selector(
    page: Page,
    target: TargetDescriptor,
    diagnosis: FailureDiagnosis,
    timeout_ms: int = 5000,
) -> RepairResult:
    """Attempt to repair a broken selector using similarity strategies.

    Tries 5 strategies in order, returns the first viable match.
    Each strategy pre-filters via fuzzy matching, then validates through
    score_candidates() pipeline. Accept if composite >= 0.45.

    Args:
        page: Playwright Page instance.
        target: Original target descriptor that failed resolution.
        diagnosis: Failure diagnosis from the failed attempt.
        timeout_ms: Total budget for all repair strategies.

    Returns:
        RepairResult with repaired locator or repaired=False.
    """
    t0 = time.monotonic()
    total_candidates = 0

    # Extract search terms from target
    search_name = _normalize_ws(target.name or "")
    search_text = _normalize_ws(target.text or "")
    search_label = _normalize_ws(target.label or "")
    search_term = search_name or search_text or search_label

    if not search_term:
        return RepairResult(repaired=False, elapsed_ms=0)

    # Collect interactive elements once for strategies 1, 2, 4, 5
    try:
        elements = page.evaluate(_INTERACTIVE_ELEMENTS_JS)
    except Exception:
        elements = []

    strategies = [
        ("text_similarity", _repair_text_similarity),
        ("aria_label_similarity", _repair_aria_label_similarity),
        ("label_association", _repair_label_association),
        ("interactive_ancestor", _repair_interactive_ancestor),
        ("element_type_match", _repair_element_type_match),
    ]

    for strategy_name, strategy_fn in strategies:
        elapsed = int((time.monotonic() - t0) * 1000)
        if elapsed >= timeout_ms:
            break

        remaining = timeout_ms - elapsed
        try:
            locator, fuzzy_score, evaluated = strategy_fn(
                page, target, search_term, elements, remaining,
            )
            total_candidates += evaluated

            # Pre-filter: fuzzy match must meet similarity threshold
            if locator is not None and fuzzy_score >= _MIN_SIMILARITY:
                # Validate through scoring pipeline (visibility, enabled, etc.)
                pipeline_score, scored = _validate_through_pipeline(
                    page, locator, strategy_name, remaining,
                )
                if scored is not None:
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    logger.info(
                        "selector_repair.success",
                        strategy=strategy_name,
                        fuzzy_score=round(fuzzy_score, 3),
                        pipeline_score=round(pipeline_score, 3),
                        candidates=total_candidates,
                        elapsed_ms=elapsed_ms,
                    )
                    return RepairResult(
                        repaired=True,
                        locator=scored.locator,
                        strategy_used=strategy_name,
                        confidence=pipeline_score,
                        candidates_evaluated=total_candidates,
                        elapsed_ms=elapsed_ms,
                    )
                else:
                    logger.debug(
                        "selector_repair.pipeline_rejected",
                        strategy=strategy_name,
                        fuzzy_score=round(fuzzy_score, 3),
                        pipeline_score=round(pipeline_score, 3),
                    )
        except Exception as e:
            logger.debug(
                "selector_repair.strategy_error",
                strategy=strategy_name,
                error=str(e)[:200],
            )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "selector_repair.failed",
        candidates=total_candidates,
        elapsed_ms=elapsed_ms,
    )
    return RepairResult(
        repaired=False,
        candidates_evaluated=total_candidates,
        elapsed_ms=elapsed_ms,
    )


def _similarity_ratio(a: str, b: str) -> float:
    """Compute normalized similarity ratio between two strings."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _validate_through_pipeline(
    page: Page,
    locator: Locator,
    strategy_name: str,
    timeout_ms: int = 2000,
) -> tuple[float, CandidateScore | None]:
    """Validate a repair candidate through the scoring pipeline.

    Runs the locator through score_candidates() to get visibility, enabled,
    match_confidence, and proximity scores — the same checks the resolver uses.

    Returns:
        (composite_score, best_candidate) if composite >= _MIN_REPAIR_CONFIDENCE,
        otherwise (composite_score, None).
    """
    try:
        candidates = score_candidates(
            page=page,
            locator=locator,
            strategy=SelectorStrategy.CSS,
            params={"selector": f"repair:{strategy_name}"},
            timeout_ms=min(timeout_ms, 2000),
        )
        if candidates and candidates[0].composite >= _MIN_REPAIR_CONFIDENCE:
            return candidates[0].composite, candidates[0]
        return (candidates[0].composite if candidates else 0.0), None
    except Exception:
        return 0.0, None


def _repair_text_similarity(
    page: Page,
    target: TargetDescriptor,
    search_term: str,
    elements: list[dict],
    timeout_ms: int,
) -> tuple[Locator | None, float, int]:
    """Strategy 1: Find interactive elements with similar text content."""
    best_locator = None
    best_score = 0.0
    evaluated = 0

    for el in elements:
        evaluated += 1
        el_text = _normalize_ws(el.get("text", ""))
        if not el_text:
            continue

        ratio = _similarity_ratio(search_term, el_text)
        if ratio >= _MIN_SIMILARITY and ratio > best_score:
            # Build locator from the element's unique attributes
            locator = _build_locator_from_element(page, el)
            if locator is not None:
                best_locator = locator
                best_score = ratio

    return best_locator, best_score, evaluated


def _repair_aria_label_similarity(
    page: Page,
    target: TargetDescriptor,
    search_term: str,
    elements: list[dict],
    timeout_ms: int,
) -> tuple[Locator | None, float, int]:
    """Strategy 2: Find elements with similar aria-label."""
    best_locator = None
    best_score = 0.0
    evaluated = 0

    for el in elements:
        evaluated += 1
        aria = _normalize_ws(el.get("ariaLabel", ""))
        if not aria:
            continue

        ratio = _similarity_ratio(search_term, aria)
        if ratio >= _MIN_SIMILARITY and ratio > best_score:
            locator = _build_locator_from_element(page, el)
            if locator is not None:
                best_locator = locator
                best_score = ratio

    return best_locator, best_score, evaluated


def _repair_label_association(
    page: Page,
    target: TargetDescriptor,
    search_term: str,
    elements: list[dict],
    timeout_ms: int,
) -> tuple[Locator | None, float, int]:
    """Strategy 3: Find labels matching text, follow for attribute."""
    evaluated = 0

    try:
        labels = page.evaluate(_LABEL_ELEMENTS_JS)
    except Exception:
        return None, 0.0, 0

    for label_info in labels:
        evaluated += 1
        label_text = _normalize_ws(label_info.get("text", ""))
        if not label_text:
            continue

        ratio = _similarity_ratio(search_term, label_text)
        if ratio < _MIN_SIMILARITY:
            continue

        # Follow the for attribute
        for_attr = label_info.get("forAttr", "")
        if for_attr:
            try:
                locator = page.locator(f"#{for_attr}")
                if locator.count() == 1 and locator.is_visible():
                    return locator, ratio, evaluated
            except Exception:
                pass

        # No for attribute — find interactive descendant in the same container
        try:
            label_rect = label_info.get("rect", {})
            if label_rect:
                # Find interactive element closest to this label
                for el in elements:
                    el_rect = el.get("rect", {})
                    if not el_rect:
                        continue
                    # Check proximity: within 200px horizontally, 100px vertically
                    dx = abs(el_rect.get("x", 0) - label_rect.get("x", 0))
                    dy = abs(el_rect.get("y", 0) - label_rect.get("y", 0))
                    if dx < 200 and dy < 100:
                        tag = el.get("tag", "")
                        if tag in ("input", "select", "textarea"):
                            locator = _build_locator_from_element(page, el)
                            if locator is not None:
                                return locator, ratio * 0.9, evaluated
        except Exception:
            pass

    return None, 0.0, evaluated


def _repair_interactive_ancestor(
    page: Page,
    target: TargetDescriptor,
    search_term: str,
    elements: list[dict],
    timeout_ms: int,
) -> tuple[Locator | None, float, int]:
    """Strategy 4: Find text on page, walk up to nearest interactive parent."""
    evaluated = 0

    try:
        # Find elements containing the text
        text_locator = page.get_by_text(target.name or target.text or target.label or "")
        count = text_locator.count()
        if count == 0:
            return None, 0.0, 0

        for i in range(min(count, 10)):
            evaluated += 1
            try:
                el = text_locator.nth(i)
                # Walk up to find interactive ancestor via JS
                ancestor = el.evaluate("""(el) => {
                    const interactive = [
                        'BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA',
                    ];
                    const interactiveRoles = [
                        'button', 'link', 'menuitem', 'option', 'tab',
                        'combobox', 'checkbox', 'radio', 'switch',
                    ];
                    let node = el;
                    for (let d = 0; d < 8 && node; d++) {
                        if (interactive.includes(node.tagName)) {
                            return {
                                tag: node.tagName.toLowerCase(),
                                id: node.id || '',
                                ariaLabel: node.getAttribute('aria-label') || '',
                                testId: node.getAttribute('data-testid') || '',
                                text: (node.textContent || '').trim().substring(0, 120),
                            };
                        }
                        const role = node.getAttribute('role');
                        if (role && interactiveRoles.includes(role)) {
                            return {
                                tag: node.tagName.toLowerCase(),
                                id: node.id || '',
                                role: role,
                                ariaLabel: node.getAttribute('aria-label') || '',
                                testId: node.getAttribute('data-testid') || '',
                                text: (node.textContent || '').trim().substring(0, 120),
                            };
                        }
                        node = node.parentElement;
                    }
                    return null;
                }""")

                if ancestor is not None:
                    locator = _build_locator_from_element(page, ancestor)
                    if locator is not None:
                        # Score based on text match quality
                        ancestor_text = _normalize_ws(ancestor.get("text", ""))
                        ratio = _similarity_ratio(search_term, ancestor_text)
                        if ratio >= 0.5:  # Lower threshold for ancestors
                            return locator, max(ratio, _MIN_REPAIR_CONFIDENCE), evaluated
            except Exception:
                continue

    except Exception:
        pass

    return None, 0.0, evaluated


def _repair_element_type_match(
    page: Page,
    target: TargetDescriptor,
    search_term: str,
    elements: list[dict],
    timeout_ms: int,
) -> tuple[Locator | None, float, int]:
    """Strategy 5: Find same type elements near the expected position."""
    evaluated = 0
    target_role = _normalize_ws(target.role or "")
    if not target_role:
        return None, 0.0, 0

    best_locator = None
    best_score = 0.0

    for el in elements:
        evaluated += 1
        el_tag = el.get("tag", "")
        el_role = _normalize_ws(el.get("role", ""))

        # Match by role or tag-to-role mapping
        tag_role_map = {
            "button": "button",
            "a": "link",
            "input": "textbox",
            "select": "combobox",
            "textarea": "textbox",
        }
        effective_role = el_role or tag_role_map.get(el_tag, "")

        if effective_role != target_role and el_tag != target_role:
            continue

        # Score by attribute overlap
        score = 0.0
        overlap_count = 0

        # Check text similarity (lower weight since text changed)
        el_text = _normalize_ws(el.get("text", ""))
        if el_text and search_term:
            text_ratio = _similarity_ratio(search_term, el_text)
            score += text_ratio * 0.4
            overlap_count += 1

        # Check aria-label
        el_aria = _normalize_ws(el.get("ariaLabel", ""))
        if el_aria and search_term:
            aria_ratio = _similarity_ratio(search_term, el_aria)
            score += aria_ratio * 0.3
            overlap_count += 1

        # Bonus for matching test_id or name
        if target.test_id and el.get("testId") == target.test_id:
            score += 0.3
            overlap_count += 1
        if target.placeholder and _normalize_ws(el.get("placeholder", "")) == _normalize_ws(target.placeholder):
            score += 0.2
            overlap_count += 1

        # Must have at least some attribute overlap
        if overlap_count == 0:
            continue

        if score > best_score:
            locator = _build_locator_from_element(page, el)
            if locator is not None:
                best_locator = locator
                best_score = score

    return best_locator, best_score, evaluated


def _build_locator_from_element(page: Page, el: dict) -> Locator | None:
    """Build a Playwright locator from element metadata.

    Tries attributes in priority order: test_id > id > aria-label > role+text.
    Returns None if no viable locator can be built.
    """
    try:
        # data-testid (most stable)
        test_id = el.get("testId", "")
        if test_id:
            loc = page.get_by_test_id(test_id)
            if loc.count() == 1:
                return loc

        # id (stable if not auto-generated)
        el_id = el.get("id", "")
        if el_id and not _looks_autogenerated(el_id):
            loc = page.locator(f"#{el_id}")
            if loc.count() == 1:
                return loc

        # aria-label
        aria = el.get("ariaLabel", "")
        if aria:
            role = el.get("role", "")
            if role:
                loc = page.get_by_role(role, name=aria)
            else:
                loc = page.get_by_label(aria)
            if loc.count() >= 1:
                return loc.first

        # role + text
        role = el.get("role", "")
        text = el.get("text", "")[:60]
        if role and text:
            loc = page.get_by_role(role, name=text)
            if loc.count() >= 1:
                return loc.first

        # Tag + text fallback
        tag = el.get("tag", "")
        if tag and text:
            loc = page.locator(tag).filter(has_text=text)
            if loc.count() >= 1:
                return loc.first

        return None
    except Exception:
        return None


def _looks_autogenerated(id_value: str) -> bool:
    """Check if an ID looks auto-generated (React, Radix, etc.)."""
    import re
    patterns = [
        r"^:r[0-9a-z]+:",         # Radix
        r"^react-select-\d+",     # React Select
        r"-[a-f0-9]{6,}$",        # Hex hash suffix
        r"^rc-",                   # rc-* auto IDs
    ]
    return any(re.search(p, id_value) for p in patterns)
