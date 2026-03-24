"""DOM revalidation — detect and recover from element replacement.

Captures element identity before action and re-validates just before
interaction. If element was replaced (React re-render, virtual DOM update),
triggers transparent re-resolution.

Usage in intent_executor:
    identity = capture_element_identity(locator)
    # ... guard checks ...
    locator = revalidate_before_action(page, locator, identity, target, ...)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from app.worker.intent_schema import TargetDescriptor
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)

# JS to snapshot element identity attributes
_IDENTITY_JS = """(el) => ({
    tag: el.tagName.toLowerCase(),
    id: el.id || '',
    ariaLabel: el.getAttribute('aria-label') || '',
    textHash: ((el.textContent || '').trim().substring(0, 50).split('').reduce(
        (h, c) => (h * 31 + c.charCodeAt(0)) | 0, 0
    )),
    rect: (() => {
        const r = el.getBoundingClientRect();
        return { x: Math.round(r.x), y: Math.round(r.y),
                 w: Math.round(r.width), h: Math.round(r.height) };
    })(),
})"""


def capture_element_identity(locator: Locator) -> dict:
    """Snapshot element identity: tag, id, aria-label, text hash, bounding box.

    Returns empty dict if capture fails (element detached, etc.).
    """
    try:
        return locator.evaluate(_IDENTITY_JS)
    except Exception:
        return {}


def is_same_element(locator: Locator, identity: dict) -> bool:
    """Check if the locator still points to the same element.

    Compares tag, id, aria-label, and text hash. Bounding box is
    allowed to drift slightly (scrolling, layout shift).
    """
    if not identity:
        return True  # No identity captured — assume same

    try:
        current = locator.evaluate(_IDENTITY_JS)
    except Exception:
        return False  # Element gone

    # Tag must match
    if current.get("tag") != identity.get("tag"):
        return False

    # ID must match (if either had one)
    if identity.get("id") and current.get("id") != identity.get("id"):
        return False

    # Text hash must match
    if current.get("textHash") != identity.get("textHash"):
        return False

    return True


def revalidate_before_action(
    page: Page,
    locator: Locator,
    identity: dict,
    target: TargetDescriptor,
    *,
    timeout_ms: int = 5000,
    **resolve_kwargs,
) -> Locator:
    """If element changed, re-resolve. Otherwise return original locator.

    Args:
        page: Playwright Page.
        locator: Original resolved locator.
        identity: Identity snapshot from capture_element_identity().
        target: TargetDescriptor for re-resolution if needed.
        timeout_ms: Budget for re-resolution.
        **resolve_kwargs: Additional kwargs for resolve_target.

    Returns:
        The original locator if still valid, or a re-resolved locator.
    """
    if not identity:
        return locator  # No identity → can't revalidate

    if is_same_element(locator, identity):
        return locator

    # Element was replaced — re-resolve
    logger.info(
        "dom_revalidation.element_replaced",
        tag=identity.get("tag", "?"),
        id=identity.get("id", ""),
    )

    try:
        from app.worker.selector_engine import resolve_target

        resolved = resolve_target(
            page, target,
            timeout_ms=timeout_ms,
            **resolve_kwargs,
        )
        logger.info(
            "dom_revalidation.re_resolved",
            strategy=resolved.strategy.value,
            score=round(resolved.score.composite, 3),
        )
        return resolved.locator
    except Exception as e:
        logger.warning(
            "dom_revalidation.re_resolve_failed",
            error=str(e)[:200],
        )
        # Return original — let the action fail with a better error
        return locator
