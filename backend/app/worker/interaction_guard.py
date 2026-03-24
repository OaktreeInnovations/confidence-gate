"""Universal interaction guard — pre-action interactability checks.

Called before every interactive action (click, fill, select, etc.) that
targets an element. Ensures the element is visible, enabled, not covered
by overlays, and DOM-stable before allowing interaction.

6 checks in order:
1. Bounding box > 0 (element has physical dimensions)
2. Visible (scroll into view if needed)
3. Enabled (wait briefly for enabled state)
4. Not covered by overlay (elementFromPoint check)
5. pointer-events not disabled (CSS check)
6. DOM stable (element hash stable over 100ms window)

Each check can attempt remediation before failing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import Locator

logger = structlog.get_logger(__name__)

# JS to compute a lightweight identity hash of an element
_ELEMENT_HASH_JS = """(el) => {
    const key = el.tagName + el.id + el.className +
        (el.textContent || '').substring(0, 50);
    let h = 0;
    for (let i = 0; i < key.length; i++) {
        h = (h * 31 + key.charCodeAt(i)) | 0;
    }
    return h;
}"""


@dataclass
class GuardResult:
    """Result of interaction guard checks."""

    passed: bool
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    remediation_applied: list[str] = field(default_factory=list)
    elapsed_ms: int = 0


def ensure_interactable(
    locator: Locator,
    *,
    require_enabled: bool = True,
    require_visible: bool = True,
    check_overlay: bool = True,
    check_stability: bool = True,
    max_wait_ms: int = 3000,
) -> GuardResult:
    """Verify element is interactable before performing an action.

    Runs up to 5 checks in order, attempting remediation for failures.
    Short-circuits on first non-remediable failure.

    Args:
        locator: Playwright locator pointing to the target element.
        require_enabled: Check that element is enabled.
        require_visible: Check bounding box and visibility.
        check_overlay: Check that no overlay covers the element center.
        check_stability: Check that DOM is stable (no re-renders).
        max_wait_ms: Maximum time budget for all checks + remediation.
    """
    t0 = time.monotonic()
    result = GuardResult(passed=True)
    budget_s = max_wait_ms / 1000

    def _over_budget() -> bool:
        return time.monotonic() - t0 > budget_s

    # Check 1: Bounding box > 0
    if require_visible:
        _check_bounding_box(locator, result, max_wait_ms)
        if not result.passed or _over_budget():
            result.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return result

    # Check 1b: Bounding box stable (no layout shift)
    if require_visible and result.passed:
        _check_bbox_stability(locator, result)
        if not result.passed or _over_budget():
            result.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return result

    # Check 2: Visible
    if require_visible:
        _check_visible(locator, result)
        if not result.passed or _over_budget():
            result.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return result

    # Check 3: Enabled
    if require_enabled:
        elapsed = int((time.monotonic() - t0) * 1000)
        _check_enabled(locator, result, max(max_wait_ms - elapsed, 500))
        if not result.passed or _over_budget():
            result.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return result

    # Check 4: Not covered by overlay
    if check_overlay:
        _check_overlay(locator, result)
        if not result.passed or _over_budget():
            result.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return result

    # Check 5: pointer-events not disabled
    if check_overlay:
        _check_pointer_events(locator, result)
        if not result.passed or _over_budget():
            result.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return result

    # Check 6: DOM stability
    if check_stability:
        elapsed = int((time.monotonic() - t0) * 1000)
        _check_dom_stability(locator, result, max(max_wait_ms - elapsed, 0))

    result.elapsed_ms = int((time.monotonic() - t0) * 1000)
    return result


def _check_bounding_box(
    locator: Locator, result: GuardResult, max_wait_ms: int,
) -> None:
    """Check element has non-zero bounding box. Scroll into view if needed."""
    try:
        bbox = locator.bounding_box(timeout=min(max_wait_ms, 2000))
        if bbox and bbox["width"] > 0 and bbox["height"] > 0:
            result.checks_passed.append("bounding_box")
            return

        # Remediate: scroll into view
        try:
            locator.scroll_into_view_if_needed(timeout=min(max_wait_ms, 2000))
            result.remediation_applied.append("scrolled_into_view")
        except Exception:
            pass

        bbox = locator.bounding_box(timeout=1000)
        if bbox and bbox["width"] > 0 and bbox["height"] > 0:
            result.checks_passed.append("bounding_box")
            return

        result.passed = False
        result.checks_failed.append("bounding_box_zero")
    except Exception:
        result.passed = False
        result.checks_failed.append("bounding_box_error")


_BBOX_SHIFT_THRESHOLD_PX = 5


def _check_bbox_stability(locator: Locator, result: GuardResult) -> None:
    """Check bounding box is stable (no layout shift in progress).

    Captures bbox, waits 50ms, re-captures. If position shifted by
    more than 5px in any direction, the element is still moving.
    """
    try:
        bbox1 = locator.bounding_box(timeout=1000)
        if not bbox1:
            return  # Already handled by bounding_box check
        time.sleep(0.05)
        bbox2 = locator.bounding_box(timeout=1000)
        if not bbox2:
            return

        dx = abs(bbox2["x"] - bbox1["x"])
        dy = abs(bbox2["y"] - bbox1["y"])

        if dx > _BBOX_SHIFT_THRESHOLD_PX or dy > _BBOX_SHIFT_THRESHOLD_PX:
            # Element is still moving — wait 200ms and re-check
            time.sleep(0.2)
            bbox3 = locator.bounding_box(timeout=1000)
            if bbox3:
                dx2 = abs(bbox3["x"] - bbox2["x"])
                dy2 = abs(bbox3["y"] - bbox2["y"])
                if dx2 > _BBOX_SHIFT_THRESHOLD_PX or dy2 > _BBOX_SHIFT_THRESHOLD_PX:
                    result.passed = False
                    result.checks_failed.append("bbox_layout_shift")
                    return
            result.remediation_applied.append("waited_for_layout_stable")

        result.checks_passed.append("bbox_stable")
    except Exception:
        # Non-fatal — skip check
        result.checks_passed.append("bbox_stability_skipped")


def _check_visible(locator: Locator, result: GuardResult) -> None:
    """Check element is visible. Scroll into view if not."""
    try:
        if locator.is_visible():
            result.checks_passed.append("visible")
            return

        # Remediate: scroll into view
        try:
            locator.scroll_into_view_if_needed(timeout=2000)
            if "scrolled_into_view" not in result.remediation_applied:
                result.remediation_applied.append("scrolled_into_view")
        except Exception:
            pass

        if locator.is_visible():
            result.checks_passed.append("visible")
            return

        result.passed = False
        result.checks_failed.append("not_visible")
    except Exception:
        result.passed = False
        result.checks_failed.append("visibility_error")


def _check_enabled(
    locator: Locator, result: GuardResult, remaining_ms: int,
) -> None:
    """Check element is enabled. Wait briefly if disabled.

    Uses budget-aware intervals: each 300ms wait checks remaining budget
    before sleeping. Max 6 retries, capped by remaining_ms.
    """
    try:
        if locator.is_enabled():
            result.checks_passed.append("enabled")
            return

        # Wait and retry up to remaining budget (300ms intervals, max 6 retries)
        max_retries = min(int(remaining_ms / 300), 6)
        t0 = time.monotonic()
        for _ in range(max_retries):
            elapsed = (time.monotonic() - t0) * 1000
            wait_ms = min(300, remaining_ms - elapsed)
            if wait_ms <= 0:
                break
            time.sleep(wait_ms / 1000)
            if locator.is_enabled():
                result.remediation_applied.append("waited_for_enabled")
                result.checks_passed.append("enabled")
                return

        result.passed = False
        result.checks_failed.append("disabled")
    except Exception:
        result.passed = False
        result.checks_failed.append("enabled_error")


def _check_overlay(locator: Locator, result: GuardResult) -> None:
    """Check no overlay covers the element center via elementFromPoint."""
    try:
        bbox = locator.bounding_box()
        if not bbox:
            result.checks_passed.append("overlay_check_skipped")
            return

        cx = bbox["x"] + bbox["width"] / 2
        cy = bbox["y"] + bbox["height"] / 2

        is_target = locator.evaluate(
            """(el, point) => {
                const hit = document.elementFromPoint(point.x, point.y);
                if (!hit) return true;
                return el === hit || el.contains(hit) || hit.contains(el);
            }""",
            {"x": cx, "y": cy},
        )

        if is_target:
            result.checks_passed.append("no_overlay")
        else:
            result.passed = False
            result.checks_failed.append("overlay_detected")
    except Exception:
        # Can't verify — skip (non-fatal)
        result.checks_passed.append("overlay_check_skipped")


def _check_pointer_events(locator: Locator, result: GuardResult) -> None:
    """Check element does not have pointer-events: none.

    Retries up to 5 times with 300ms intervals (1.5s total) to handle
    transient pointer-events: none during CSS animations (sidebar expand,
    accordion reveal, staggered sub-item transitions).

    If pointer-events: none persists, this is non-fatal — many SPAs
    (Next.js, Radix) use JS event handlers on elements with
    pointer-events: none.  The click attempt should still proceed so
    safe_click can try its JS dispatch fallback.
    """
    max_attempts = 5
    interval_s = 0.3
    try:
        for attempt in range(max_attempts):
            pe = locator.evaluate(
                "(el) => window.getComputedStyle(el).pointerEvents"
            )
            if pe != "none":
                if attempt > 0:
                    result.remediation_applied.append("waited_for_pointer_events")
                result.checks_passed.append("pointer_events_ok")
                return
            if attempt < max_attempts - 1:
                time.sleep(interval_s)

        # Non-fatal: let the click attempt proceed with JS fallback
        result.checks_passed.append("pointer_events_none_allowed")
        result.remediation_applied.append("pointer_events_none_bypassed")
    except Exception:
        # Can't verify — skip (non-fatal)
        result.checks_passed.append("pointer_events_skipped")


def _check_dom_stability(
    locator: Locator, result: GuardResult, remaining_ms: int = 500,
) -> None:
    """Check DOM is stable by comparing element hash over 100ms window.

    Budget-aware: skips stability check if remaining budget is too low.
    """
    if remaining_ms < 100:
        result.checks_passed.append("stability_check_skipped")
        return

    try:
        hash1 = locator.evaluate(_ELEMENT_HASH_JS)
        wait1 = min(100, remaining_ms) / 1000
        time.sleep(wait1)
        hash2 = locator.evaluate(_ELEMENT_HASH_JS)

        if hash1 == hash2:
            result.checks_passed.append("dom_stable")
            return

        # Element changed — wait up to 200ms more and re-check
        remaining_after = remaining_ms - 100
        if remaining_after < 50:
            result.passed = False
            result.checks_failed.append("dom_unstable")
            return

        wait2 = min(200, remaining_after) / 1000
        time.sleep(wait2)
        hash3 = locator.evaluate(_ELEMENT_HASH_JS)

        if hash2 == hash3:
            result.remediation_applied.append("waited_for_stability")
            result.checks_passed.append("dom_stable")
        else:
            result.passed = False
            result.checks_failed.append("dom_unstable")
    except Exception:
        # Element may have been replaced — non-fatal skip
        result.checks_passed.append("stability_check_skipped")
