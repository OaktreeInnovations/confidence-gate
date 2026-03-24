"""Selector resilience utilities for Playwright element location.

Provides:
- SelectorStrategy enum: ordered selector strategies from most to least stable
- _build_locator(): builds a Playwright locator from strategy + params
- build_selector_fingerprint(): extracts identifying attributes from an element

Used by selector_engine/resolver.py for scorer-based element resolution.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


class SelectorStrategy(StrEnum):
    """Ordered selector strategies from most to least stable.

    Tier 1 — Strict: TEST_ID, ROLE, LABEL, PLACEHOLDER
    Tier 2 — Structural: INTERACTIVE_ANCESTOR, ARIA_WIDGET, NEAR_LABEL
    Tier 3 — Proximity: TEXT, CSS
    """

    # Tier 1 — strict semantic match
    TEST_ID = "test_id"
    ROLE = "role"
    LABEL = "label"
    PLACEHOLDER = "placeholder"

    # Tier 2 — structural relaxation
    INTERACTIVE_ANCESTOR = "interactive_ancestor"
    ARIA_WIDGET = "aria_widget"
    NEAR_LABEL = "near_label"

    # Tier 3 — proximity / text match
    TEXT = "text"
    CSS = "css"


# Interactive element selectors used by Tier 2 strategies
_INTERACTIVE_ELEMENTS = (
    "button, a, input, select, textarea, "
    "[role='button'], [role='link'], [role='menuitem'], [role='option'], "
    "[role='tab'], [role='combobox'], [role='listbox'], "
    "[tabindex]:not([tabindex='-1']), "
    "[contenteditable='true']"
)

# ARIA widget selectors for Tier 2 widget discovery
_ARIA_WIDGET_ELEMENTS = (
    "[aria-haspopup], [aria-expanded], "
    "[contenteditable='true'], "
    "[role='combobox'], [role='spinbutton'], [role='slider'], "
    "[role='listbox'], [role='menu'], [role='menubar']"
)


def _build_locator(
    page: Page,
    strategy: SelectorStrategy,
    params: dict,
) -> Locator | None:
    """Build a Playwright locator from strategy + params."""
    if strategy == SelectorStrategy.TEST_ID:
        test_id = params.get("test_id", "")
        if not test_id:
            return None
        return page.get_by_test_id(test_id)

    if strategy == SelectorStrategy.ROLE:
        role = params.get("role", "")
        name = params.get("name", "")
        if not role:
            return None
        if name:
            return page.get_by_role(role, name=name)
        return page.get_by_role(role)

    if strategy == SelectorStrategy.LABEL:
        label = params.get("label", "")
        if not label:
            return None
        return page.get_by_label(label)

    if strategy == SelectorStrategy.TEXT:
        text = params.get("text", "")
        exact = params.get("exact", False)
        if not text:
            return None
        return page.get_by_text(text, exact=exact)

    if strategy == SelectorStrategy.PLACEHOLDER:
        placeholder = params.get("placeholder", "")
        if not placeholder:
            return None
        return page.get_by_placeholder(placeholder)

    if strategy == SelectorStrategy.CSS:
        selector = params.get("selector", "")
        if not selector:
            return None
        if _is_unstable_selector(selector):
            logger.debug(
                "selector_resilience.rejected_unstable_css",
                selector=selector[:120],
            )
            return None
        return page.locator(selector)

    if strategy == SelectorStrategy.INTERACTIVE_ANCESTOR:
        text = params.get("text", "")
        if not text:
            return None
        return page.locator(_INTERACTIVE_ELEMENTS).filter(
            has_text=re.compile(re.escape(text), re.IGNORECASE),
        )

    if strategy == SelectorStrategy.ARIA_WIDGET:
        text = params.get("text", "")
        if not text:
            return None
        return page.locator(_ARIA_WIDGET_ELEMENTS).filter(
            has_text=re.compile(re.escape(text), re.IGNORECASE),
        )

    if strategy == SelectorStrategy.NEAR_LABEL:
        label = params.get("label", "")
        if not label:
            return None
        return _build_near_label_locator(page, label)

    return None


# Patterns that indicate a CSS selector is unstable (generated/dynamic)
_UNSTABLE_CSS_PATTERNS = (
    # CSS-in-JS hashes: css-1a2b3c, sc-dkzDqf, emotion-xyz123
    re.compile(r"\b(?:css|sc|emotion)-[a-zA-Z0-9]{4,}\b"),
    # CSS Modules: _component_abc123, styles_header__xY2z
    re.compile(r"\b\w+_[a-zA-Z0-9]{5,}\b"),
    # Auto-generated IDs: :r0:, :r1a:, react-select-*-option-*
    re.compile(r":r[0-9a-z]+:"),
    re.compile(r"react-select-\d+-"),
    # Radix auto IDs: radix-:r0:
    re.compile(r"radix-:[a-z0-9]+:"),
    # Generic hash classes: 6+ hex/alphanum after a dash (e.g. btn-a3f2c1b)
    re.compile(r"-[a-f0-9]{6,}\b"),
    # Styled-components: class^="sc-" with random suffix
    re.compile(r"\bsc-[a-zA-Z]{5,}\b"),
)


def _is_unstable_selector(selector: str) -> bool:
    """Check if a CSS selector contains dynamic/generated patterns.

    Rejects selectors with CSS-in-JS hashes, auto-generated IDs,
    CSS Modules suffixes, and other patterns that change across
    page reloads or builds.
    """
    return any(p.search(selector) for p in _UNSTABLE_CSS_PATTERNS)


def _build_near_label_locator(page: Page, label: str) -> Locator | None:
    """Find interactive elements near a label via DOM association.

    Strategy order:
    1. label[for] → element by ID
    2. label containing interactive descendant
    3. Interactive sibling within same container
    """
    label_re = re.compile(re.escape(label), re.IGNORECASE)

    # Strategy 1: label[for] → #target
    try:
        label_els = page.locator("label").filter(has_text=label_re)
        count = label_els.count()
        for i in range(min(count, 5)):
            for_attr = label_els.nth(i).get_attribute("for", timeout=1000)
            if for_attr:
                target = page.locator(f"#{for_attr}")
                if target.count() > 0:
                    return target
    except Exception:
        pass

    # Strategy 2: label containing interactive child
    try:
        label_with_input = page.locator("label").filter(has_text=label_re).locator(
            "input, select, textarea, [contenteditable='true']"
        )
        if label_with_input.count() > 0:
            return label_with_input
    except Exception:
        pass

    # Strategy 3: interactive sibling in same container (walk up 3 levels)
    try:
        text_el = page.get_by_text(label, exact=False)
        for depth in ("xpath=..", "xpath=../..", "xpath=../../.."):
            parent = text_el.locator(depth)
            interactive = parent.locator(_INTERACTIVE_ELEMENTS)
            if interactive.count() > 0:
                return interactive
    except Exception:
        pass

    return None


def build_selector_fingerprint(page: Page, locator: Locator) -> dict:
    """Extract identifying attributes from a located element.

    Returns a dict of attributes that can be used to relocate the element
    in a future run. Useful for selector stability tracking.
    """
    try:
        attrs = locator.evaluate("""(el) => ({
            tag: el.tagName.toLowerCase(),
            id: el.id || '',
            name: el.getAttribute('name') || '',
            type: el.getAttribute('type') || '',
            role: el.getAttribute('role') || el.tagName.toLowerCase(),
            ariaLabel: el.getAttribute('aria-label') || '',
            testId: el.getAttribute('data-testid') || '',
            text: (el.textContent || '').trim().substring(0, 80),
            placeholder: el.getAttribute('placeholder') || '',
            className: (el.className || '').toString().substring(0, 100),
        })""")
        return attrs
    except Exception:
        return {}
