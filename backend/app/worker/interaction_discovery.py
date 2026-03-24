"""Clickable element discovery — walks up DOM to find the nearest interactive ancestor.

Problem:
    Many apps use non-semantic wrappers:
        <div class="dropdown" onclick="toggle()">
            <span>Open</span>
        </div>
    The selector engine correctly resolves the <span> by text, but clicking it
    does nothing — the click handler lives on the parent <div>.

Solution:
    After resolving an element for a click action, check if it is itself clickable.
    If not, walk up the DOM (max 6 levels) to find the nearest ancestor that is
    interactive. Return it as the click target instead.

Clickable signals (checked in order):
    1. Semantic tag: button, a, summary, details
    2. ARIA role: button, link, menuitem, tab, radio, checkbox, switch, option, combobox
    3. tabindex >= 0 (explicitly made focusable/clickable)
    4. onclick / onpointerdown handler present
    5. cursor: pointer computed style (commonly used for clickable elements)

Integration:
    Called from intent_executor._execute_single_action() after resolve_target()
    and BEFORE interaction guard, only for CLICK actions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import Locator

logger = structlog.get_logger(__name__)

# Maximum DOM levels to walk upward
_MAX_ANCESTOR_DEPTH = 6

# JS that checks if an element is clickable and, if not, walks up to find one.
# Returns: {found: bool, depth: int, tag: str, role: str}
# The JS runs in the browser — single evaluate call for performance.
_DISCOVER_CLICKABLE_JS = """(el) => {
    const CLICKABLE_TAGS = new Set([
        'button', 'a', 'summary', 'details', 'select', 'option',
    ]);
    const CLICKABLE_ROLES = new Set([
        'button', 'link', 'menuitem', 'tab', 'radio', 'checkbox',
        'switch', 'option', 'combobox', 'menuitemcheckbox', 'menuitemradio',
    ]);
    const MAX_DEPTH = 6;

    function isClickable(node) {
        if (!node || node === document.body || node === document.documentElement) {
            return false;
        }
        const tag = node.tagName.toLowerCase();
        if (CLICKABLE_TAGS.has(tag)) return true;

        const role = (node.getAttribute('role') || '').toLowerCase();
        if (CLICKABLE_ROLES.has(role)) return true;

        const tabindex = node.getAttribute('tabindex');
        if (tabindex !== null && parseInt(tabindex, 10) >= 0) return true;

        if (node.onclick || node.onpointerdown) return true;

        try {
            const style = window.getComputedStyle(node);
            if (style.cursor === 'pointer') return true;
        } catch (_) {}

        return false;
    }

    // Check if the element itself is already clickable
    if (isClickable(el)) {
        return {found: false, depth: 0, tag: '', role: ''};
    }

    // Walk up the DOM
    let current = el.parentElement;
    for (let depth = 1; depth <= MAX_DEPTH && current; depth++) {
        if (current === document.body || current === document.documentElement) {
            break;
        }
        if (isClickable(current)) {
            return {
                found: true,
                depth: depth,
                tag: current.tagName.toLowerCase(),
                role: current.getAttribute('role') || '',
            };
        }
        current = current.parentElement;
    }

    return {found: false, depth: 0, tag: '', role: ''};
}"""


def discover_clickable_ancestor(locator: Locator) -> Locator:
    """Check if the resolved element is clickable; if not, find its clickable ancestor.

    Runs a single JS evaluation to walk up the DOM. If a clickable ancestor
    is found, returns a new locator scoped to that ancestor via XPath traversal.
    Otherwise returns the original locator unchanged.

    Args:
        locator: Resolved Playwright Locator for the target element.

    Returns:
        The original locator if already clickable, or a new locator pointing
        to the nearest clickable ancestor.
    """
    try:
        result = locator.evaluate(_DISCOVER_CLICKABLE_JS)
    except Exception as e:
        logger.debug(
            "discovery.evaluate_failed",
            error=str(e)[:150],
        )
        return locator

    if not isinstance(result, dict) or not result.get("found"):
        return locator

    depth = result["depth"]
    ancestor_tag = result.get("tag", "")
    ancestor_role = result.get("role", "")

    logger.info(
        "discovery.clickable_ancestor_found",
        depth=depth,
        tag=ancestor_tag,
        role=ancestor_role,
    )

    # Build XPath to reach the ancestor: "../" repeated `depth` times
    xpath = "/".join([".."] * depth)
    ancestor_locator = locator.locator(f"xpath={xpath}")

    try:
        # Verify the ancestor locator resolves to exactly one element
        if ancestor_locator.count() == 1:
            return ancestor_locator
        # Multiple matches — try .first but only if visible
        if ancestor_locator.count() > 1 and ancestor_locator.first.is_visible():
            return ancestor_locator.first
    except Exception as e:
        logger.debug(
            "discovery.ancestor_locator_failed",
            error=str(e)[:150],
        )

    # Fallback: return original
    return locator
