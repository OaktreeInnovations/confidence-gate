"""Behavior effect detection — confirms whether an action had any observable effect.

After every interaction the engine must detect whether the intended behavior
actually occurred. This module provides action-agnostic detection: it answers
"did ANYTHING change?" rather than "did the SPECIFIC expected thing happen?"
(the latter is handled by post_action_verify.py).

Signals checked (any one = effect detected):
    1. URL change
    2. DOM content hash change (subtree mutations)
    3. aria-expanded toggle
    4. New listbox / menu / dialog appearance
    5. Active element (focus) change
    6. Input value change on the target element
    7. aria-checked / aria-selected state change  [NEW]
    8. CSS state class change (.active, .open, .selected, .checked)  [NEW]
    9. Network activity triggered by action  [NEW]

Usage in the action feedback loop:
    snapshot = capture_page_snapshot(page, locator)
    ... execute action ...
    effect = detect_behavior_effect(page, locator, snapshot)
    if not effect.detected:
        # Action was ineffective → try alternate strategies
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


@dataclass
class BehaviorEffect:
    """Result of behavior effect detection after an action."""

    detected: bool
    signals: list[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "detected": self.detected,
            "signals": self.signals,
            "detail": self.detail,
        }


# Single JS call to capture all relevant page state in one round-trip.
# Expanded to capture aria-checked, aria-selected, and CSS state classes.
_SNAPSHOT_JS = """() => {
    let domHash = 0;
    try {
        const text = (document.body ? document.body.innerText : '').substring(0, 5000);
        for (let i = 0; i < text.length; i++) {
            domHash = (domHash * 31 + text.charCodeAt(i)) | 0;
        }
    } catch (_) {}

    let activeTag = '';
    let activeId = '';
    try {
        const ae = document.activeElement;
        if (ae) {
            activeTag = ae.tagName.toLowerCase();
            activeId = ae.id || ae.getAttribute('name') || '';
        }
    } catch (_) {}

    let listboxCount = 0;
    let menuCount = 0;
    let dialogCount = 0;
    try {
        listboxCount = document.querySelectorAll('[role="listbox"]').length;
        menuCount = document.querySelectorAll('[role="menu"]').length;
        dialogCount = document.querySelectorAll(
            'dialog[open], [role="dialog"], [role="alertdialog"]'
        ).length;
    } catch (_) {}

    // Signal 7: ARIA state counts (checked + selected)
    let ariaCheckedCount = 0;
    let ariaSelectedCount = 0;
    try {
        ariaCheckedCount = document.querySelectorAll('[aria-checked="true"]').length;
        ariaSelectedCount = document.querySelectorAll('[aria-selected="true"]').length;
    } catch (_) {}

    // Signal 8: CSS state class fingerprint — count elements carrying state classes
    let cssStateFingerprint = 0;
    try {
        const stateSelectors = [
            '.active', '.open', '.selected', '.checked',
            '.is-active', '.is-open', '.is-selected', '.is-checked',
            '[data-state="open"]', '[data-state="active"]', '[data-state="checked"]',
        ];
        for (const sel of stateSelectors) {
            try {
                const count = document.querySelectorAll(sel).length;
                cssStateFingerprint += count;
            } catch (_) {}
        }
    } catch (_) {}

    return {
        url: location.href,
        domHash: domHash,
        activeTag: activeTag,
        activeId: activeId,
        listboxCount: listboxCount,
        menuCount: menuCount,
        dialogCount: dialogCount,
        ariaCheckedCount: ariaCheckedCount,
        ariaSelectedCount: ariaSelectedCount,
        cssStateFingerprint: cssStateFingerprint,
    };
}"""

# JS to capture element-specific ARIA state (checked + selected) from a locator.
_ELEMENT_ARIA_JS = """(el) => ({
    ariaChecked: el.getAttribute('aria-checked'),
    ariaSelected: el.getAttribute('aria-selected'),
})"""


@dataclass
class PageSnapshot:
    """Snapshot of page state for before/after comparison."""

    url: str = ""
    dom_hash: int = 0
    active_tag: str = ""
    active_id: str = ""
    listbox_count: int = 0
    menu_count: int = 0
    dialog_count: int = 0
    aria_expanded: str | None = None
    element_value: str = ""
    # New signals
    aria_checked_count: int = 0        # Signal 7a: page-wide aria-checked="true" count
    aria_selected_count: int = 0       # Signal 7b: page-wide aria-selected="true" count
    css_state_fingerprint: int = 0     # Signal 8: CSS state class count
    element_aria_checked: str | None = None   # Signal 7a on target element
    element_aria_selected: str | None = None  # Signal 7b on target element
    network_request_count: int = 0     # Signal 9: network requests triggered


def capture_page_snapshot(
    page: Page,
    locator: Locator | None = None,
    network_request_count: int = 0,
) -> PageSnapshot:
    """Capture a snapshot of page state before an action.

    Runs a single JS evaluation to collect DOM hash, active element,
    popup counts, URL, and new ARIA/CSS state signals.

    Args:
        page: Playwright Page instance.
        locator: Target element locator (optional).
        network_request_count: Current count of observed network requests (optional).

    Returns:
        PageSnapshot with all captured state.
    """
    snap = PageSnapshot()
    snap.network_request_count = network_request_count

    # Page-level state via single JS call
    try:
        result = page.evaluate(_SNAPSHOT_JS)
        if isinstance(result, dict):
            snap.url = result.get("url", "")
            snap.dom_hash = result.get("domHash", 0)
            snap.active_tag = result.get("activeTag", "")
            snap.active_id = result.get("activeId", "")
            snap.listbox_count = result.get("listboxCount", 0)
            snap.menu_count = result.get("menuCount", 0)
            snap.dialog_count = result.get("dialogCount", 0)
            snap.aria_checked_count = result.get("ariaCheckedCount", 0)
            snap.aria_selected_count = result.get("ariaSelectedCount", 0)
            snap.css_state_fingerprint = result.get("cssStateFingerprint", 0)
    except Exception as e:
        # Fallback: capture URL from Playwright API
        try:
            snap.url = page.url
        except Exception:
            pass
        logger.debug("behavior.snapshot_js_failed", error=str(e)[:150])

    # Element-specific state
    if locator is not None:
        try:
            snap.aria_expanded = locator.get_attribute("aria-expanded", timeout=500)
        except Exception:
            pass
        try:
            snap.element_value = locator.input_value(timeout=500)
        except Exception:
            pass
        # New: element-level ARIA checked/selected
        try:
            aria_result = locator.evaluate(_ELEMENT_ARIA_JS, timeout=500)
            if isinstance(aria_result, dict):
                snap.element_aria_checked = aria_result.get("ariaChecked")
                snap.element_aria_selected = aria_result.get("ariaSelected")
        except Exception:
            pass

    return snap


def detect_behavior_effect(
    page: Page,
    locator: Locator | None,
    pre_snapshot: PageSnapshot,
    post_network_count: int = 0,
) -> BehaviorEffect:
    """Detect whether an action had any observable effect on the page.

    Compares current page state against pre-action snapshot. Any single
    signal change is sufficient to confirm the action had an effect.

    Args:
        page: Playwright Page instance.
        locator: Target element locator (may be None for navigate).
        pre_snapshot: Snapshot captured before the action.
        post_network_count: Network requests observed after the action.

    Returns:
        BehaviorEffect with detected=True if any signal changed.
    """
    signals: list[str] = []

    # Capture post-action snapshot (pass post network count for comparison)
    post = capture_page_snapshot(page, locator, network_request_count=post_network_count)

    # Signal 1: URL change
    if post.url and pre_snapshot.url and post.url != pre_snapshot.url:
        signals.append("url_change")

    # Signal 2: DOM content hash change
    if post.dom_hash != pre_snapshot.dom_hash and post.dom_hash != 0:
        signals.append("dom_mutation")

    # Signal 3: aria-expanded toggle
    if post.aria_expanded != pre_snapshot.aria_expanded:
        signals.append("aria_expanded")

    # Signal 4: New listbox / menu / dialog appeared
    if post.listbox_count > pre_snapshot.listbox_count:
        signals.append("listbox_appeared")
    if post.menu_count > pre_snapshot.menu_count:
        signals.append("menu_appeared")
    if post.dialog_count > pre_snapshot.dialog_count:
        signals.append("dialog_appeared")

    # Signal 5: Focus changed (active element moved)
    focus_changed = (
        post.active_tag != pre_snapshot.active_tag
        or post.active_id != pre_snapshot.active_id
    )
    if focus_changed and post.active_tag:
        signals.append("focus_change")

    # Signal 6: Input value changed on target element
    if (
        post.element_value != pre_snapshot.element_value
        and post.element_value  # ignore empty→empty
    ):
        signals.append("value_change")

    # Signal 7a: aria-checked state changed on target element
    if (
        post.element_aria_checked is not None
        and pre_snapshot.element_aria_checked is not None
        and post.element_aria_checked != pre_snapshot.element_aria_checked
    ):
        signals.append("aria_checked_change")

    # Signal 7b: aria-selected state changed on target element
    if (
        post.element_aria_selected is not None
        and pre_snapshot.element_aria_selected is not None
        and post.element_aria_selected != pre_snapshot.element_aria_selected
    ):
        signals.append("aria_selected_change")

    # Signal 7c: Page-wide aria-checked/selected count changed
    # (catches custom checkboxes, tab panels, option lists)
    if post.aria_checked_count != pre_snapshot.aria_checked_count:
        signals.append("aria_checked_count_change")
    if post.aria_selected_count != pre_snapshot.aria_selected_count:
        signals.append("aria_selected_count_change")

    # Signal 8: CSS state class fingerprint changed
    # (catches .active, .open, .selected, .checked toggles)
    if post.css_state_fingerprint != pre_snapshot.css_state_fingerprint:
        signals.append("css_state_change")

    # Signal 9: Network activity triggered (new requests since pre-snapshot)
    if post_network_count > pre_snapshot.network_request_count:
        signals.append("network_activity")

    detected = len(signals) > 0

    if not detected:
        logger.info("behavior.no_effect_detected")
    else:
        logger.debug(
            "behavior.effect_detected",
            signals=signals,
        )

    return BehaviorEffect(
        detected=detected,
        signals=signals,
        detail=", ".join(signals) if signals else "No observable effect",
    )
