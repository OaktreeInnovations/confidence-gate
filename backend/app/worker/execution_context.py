"""Execution Context Memory — tracks DOM-level awareness across actions.

Maintains a running model of what the user has interacted with, where they
are on the page, and what UI state is active. This context feeds into the
scorer's proximity dimension and helps disambiguate between similar elements.

Updated after each action by ``update_context()``. Read by the resolver via
``last_action_selector`` and by the scorer via ``context_hints``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


@dataclass
class ExecutionContext:
    """Tracks contextual state across actions within a step or run."""

    # Last element the user interacted with (CSS path for proximity)
    last_element_selector: str = ""

    # Page section context (e.g., "header", "sidebar", "main")
    last_page_section: str = ""

    # Active modal/dialog context
    active_modal: str = ""

    # Whether a dropdown is currently open
    open_dropdown: bool = False
    open_dropdown_trigger: str = ""

    # Last navigation URL
    last_navigation_url: str = ""

    # Visible text anchor — last significant text near the interaction area
    last_visible_text_anchor: str = ""

    # History of sections visited (for flow awareness)
    sections_visited: list[str] = field(default_factory=list)

    def get_proximity_hint(self) -> str | None:
        """Return the best proximity hint for the resolver."""
        return self.last_element_selector or None

    def get_context_hints(self) -> dict:
        """Return context hints for scoring/disambiguation."""
        return {
            "section": self.last_page_section,
            "modal": self.active_modal,
            "dropdown_open": self.open_dropdown,
            "text_anchor": self.last_visible_text_anchor,
        }


def update_context(
    ctx: ExecutionContext,
    page: Page,
    locator: Locator | None,
    action_type: str,
    selector_path: str = "",
) -> None:
    """Update execution context after an action completes.

    Captures the current DOM state to inform future resolution.
    All checks are non-fatal — context is advisory only.
    """
    # Update last element
    if selector_path:
        ctx.last_element_selector = selector_path

    # Update navigation URL
    try:
        ctx.last_navigation_url = page.url
    except Exception:
        pass

    if locator is None:
        return

    # Detect page section from element position
    try:
        section = locator.evaluate("""(el) => {
            const rect = el.getBoundingClientRect();
            const vh = window.innerHeight;
            const vw = window.innerWidth;
            if (rect.y < vh * 0.1) return 'header';
            if (rect.x < vw * 0.15) return 'sidebar';
            if (rect.y > vh * 0.85) return 'footer';
            return 'main';
        }""")
        if section and section != ctx.last_page_section:
            ctx.last_page_section = section
            if section not in ctx.sections_visited:
                ctx.sections_visited.append(section)
    except Exception:
        pass

    # Detect modal context
    try:
        has_modal = page.evaluate("""() => {
            const modal = document.querySelector('[role="dialog"], [role="alertdialog"], [class*="modal"]:not([style*="display: none"])');
            if (!modal) return '';
            return modal.getAttribute('aria-label') || modal.querySelector('h2,h3')?.textContent?.trim()?.substring(0, 50) || 'modal';
        }""")
        ctx.active_modal = has_modal or ""
    except Exception:
        pass

    # Detect open dropdown
    try:
        expanded = locator.get_attribute("aria-expanded", timeout=300)
        if expanded == "true":
            ctx.open_dropdown = True
            ctx.open_dropdown_trigger = selector_path
        elif action_type == "select":
            ctx.open_dropdown = False
            ctx.open_dropdown_trigger = ""
    except Exception:
        pass

    # Capture nearby text anchor
    try:
        anchor = locator.evaluate("""(el) => {
            let p = el.parentElement;
            for (let i = 0; i < 3 && p; i++) {
                const text = p.textContent?.trim() || '';
                if (text.length > 5 && text.length < 100) return text.substring(0, 80);
                p = p.parentElement;
            }
            return '';
        }""")
        if anchor:
            ctx.last_visible_text_anchor = anchor
    except Exception:
        pass
