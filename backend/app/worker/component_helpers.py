"""Shared helpers for component interaction strategies.

Provides deterministic element resolution when locators match multiple elements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import Locator

logger = structlog.get_logger(__name__)


def resolve_single(
    locator: Locator,
    context: str = "",
    timeout_ms: int = 3000,
) -> Locator:
    """Resolve a locator to exactly one element, scoring by visibility if ambiguous.

    If only one match exists, returns it directly.
    If multiple matches exist, picks the most visible+enabled one.
    If no matches exist, waits briefly then retries.

    Args:
        locator: Playwright Locator that may match 0, 1, or N elements.
        context: Description for logging (e.g., "day_cell_15").
        timeout_ms: Max wait time if zero elements found initially.

    Returns:
        A Locator pointing to exactly one element.

    Raises:
        RuntimeError: If no matching element is found.
    """
    count = locator.count()

    if count == 0:
        try:
            locator.first.wait_for(state="attached", timeout=timeout_ms)
        except Exception:
            pass
        count = locator.count()

    if count == 0:
        raise RuntimeError(f"No element found: {context}")

    if count == 1:
        return locator.first

    # Multiple matches: score by visibility + enabled state
    best_idx = 0
    best_score = -1
    for i in range(min(count, 10)):
        score = 0
        try:
            candidate = locator.nth(i)
            if candidate.is_visible(timeout=500):
                score += 2
            if candidate.is_enabled(timeout=500):
                score += 1
        except Exception:
            pass
        if score > best_score:
            best_score = score
            best_idx = i

    if count > 1:
        logger.info(
            "resolve_single.ambiguous",
            context=context,
            count=count,
            picked=best_idx,
        )

    return locator.nth(best_idx)
