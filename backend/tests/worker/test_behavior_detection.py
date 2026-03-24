"""Tests for behavior effect detection.

Validates that detect_behavior_effect() correctly identifies:
- URL changes
- DOM content mutations
- aria-expanded toggles
- Listbox/menu/dialog appearances
- Focus changes
- Input value changes
- No-effect scenarios
"""

from unittest.mock import MagicMock

from app.worker.behavior_detection import (
    BehaviorEffect,
    PageSnapshot,
    capture_page_snapshot,
    detect_behavior_effect,
)


def _make_page(snapshot_result=None):
    """Build a mock page with configurable snapshot JS result."""
    page = MagicMock()
    page.url = "https://example.com/app"
    if snapshot_result is not None:
        page.evaluate.return_value = snapshot_result
    else:
        page.evaluate.return_value = {
            "url": "https://example.com/app",
            "domHash": 12345,
            "activeTag": "body",
            "activeId": "",
            "listboxCount": 0,
            "menuCount": 0,
            "dialogCount": 0,
        }
    return page


def _make_locator(aria_expanded=None, input_value=""):
    """Build a mock locator."""
    loc = MagicMock()
    loc.get_attribute.return_value = aria_expanded
    loc.input_value.return_value = input_value
    return loc


# --- capture_page_snapshot tests ---


def test_snapshot_captures_page_state():
    """Snapshot captures URL, DOM hash, active element, popup counts."""
    page = _make_page({
        "url": "https://example.com/dashboard",
        "domHash": 99999,
        "activeTag": "input",
        "activeId": "email",
        "listboxCount": 1,
        "menuCount": 0,
        "dialogCount": 2,
    })
    snap = capture_page_snapshot(page)
    assert snap.url == "https://example.com/dashboard"
    assert snap.dom_hash == 99999
    assert snap.active_tag == "input"
    assert snap.active_id == "email"
    assert snap.listbox_count == 1
    assert snap.dialog_count == 2


def test_snapshot_captures_locator_state():
    """Snapshot captures aria-expanded and input value from locator."""
    page = _make_page()
    locator = _make_locator(aria_expanded="false", input_value="hello")
    snap = capture_page_snapshot(page, locator)
    assert snap.aria_expanded == "false"
    assert snap.element_value == "hello"


def test_snapshot_handles_js_failure():
    """JS evaluation failure falls back to page.url."""
    page = MagicMock()
    page.url = "https://fallback.com"
    page.evaluate.side_effect = RuntimeError("Page crashed")
    snap = capture_page_snapshot(page)
    assert snap.url == "https://fallback.com"
    assert snap.dom_hash == 0


def test_snapshot_handles_non_dict_result():
    """Non-dict JS result is handled gracefully."""
    page = _make_page(snapshot_result=True)
    snap = capture_page_snapshot(page)
    # Should not crash; fields stay at defaults
    assert snap.dom_hash == 0


# --- detect_behavior_effect tests ---


def test_url_change_detected():
    """URL change after action is detected as effect."""
    pre = PageSnapshot(url="https://example.com/login", dom_hash=100)
    page = _make_page({
        "url": "https://example.com/dashboard",
        "domHash": 100,
        "activeTag": "body",
        "activeId": "",
        "listboxCount": 0,
        "menuCount": 0,
        "dialogCount": 0,
    })
    effect = detect_behavior_effect(page, None, pre)
    assert effect.detected
    assert "url_change" in effect.signals


def test_dom_mutation_detected():
    """DOM content hash change is detected as effect."""
    pre = PageSnapshot(url="https://example.com", dom_hash=100)
    page = _make_page({
        "url": "https://example.com",
        "domHash": 999,
        "activeTag": "body",
        "activeId": "",
        "listboxCount": 0,
        "menuCount": 0,
        "dialogCount": 0,
    })
    effect = detect_behavior_effect(page, None, pre)
    assert effect.detected
    assert "dom_mutation" in effect.signals


def test_aria_expanded_toggled():
    """aria-expanded toggle is detected."""
    pre = PageSnapshot(
        url="https://example.com", dom_hash=100,
        aria_expanded="false",
    )
    locator = _make_locator(aria_expanded="true")
    page = _make_page({
        "url": "https://example.com",
        "domHash": 100,
        "activeTag": "body",
        "activeId": "",
        "listboxCount": 0,
        "menuCount": 0,
        "dialogCount": 0,
    })
    effect = detect_behavior_effect(page, locator, pre)
    assert effect.detected
    assert "aria_expanded" in effect.signals


def test_listbox_appeared():
    """New listbox appearing is detected."""
    pre = PageSnapshot(url="https://example.com", dom_hash=100, listbox_count=0)
    page = _make_page({
        "url": "https://example.com",
        "domHash": 100,
        "activeTag": "body",
        "activeId": "",
        "listboxCount": 1,
        "menuCount": 0,
        "dialogCount": 0,
    })
    effect = detect_behavior_effect(page, None, pre)
    assert effect.detected
    assert "listbox_appeared" in effect.signals


def test_menu_appeared():
    """New menu appearing is detected."""
    pre = PageSnapshot(url="https://example.com", dom_hash=100, menu_count=0)
    page = _make_page({
        "url": "https://example.com",
        "domHash": 100,
        "activeTag": "body",
        "activeId": "",
        "listboxCount": 0,
        "menuCount": 2,
        "dialogCount": 0,
    })
    effect = detect_behavior_effect(page, None, pre)
    assert effect.detected
    assert "menu_appeared" in effect.signals


def test_dialog_appeared():
    """New dialog appearing is detected."""
    pre = PageSnapshot(url="https://example.com", dom_hash=100, dialog_count=0)
    page = _make_page({
        "url": "https://example.com",
        "domHash": 100,
        "activeTag": "body",
        "activeId": "",
        "listboxCount": 0,
        "menuCount": 0,
        "dialogCount": 1,
    })
    effect = detect_behavior_effect(page, None, pre)
    assert effect.detected
    assert "dialog_appeared" in effect.signals


def test_focus_change_detected():
    """Active element change is detected."""
    pre = PageSnapshot(
        url="https://example.com", dom_hash=100,
        active_tag="body", active_id="",
    )
    page = _make_page({
        "url": "https://example.com",
        "domHash": 100,
        "activeTag": "input",
        "activeId": "email",
        "listboxCount": 0,
        "menuCount": 0,
        "dialogCount": 0,
    })
    effect = detect_behavior_effect(page, None, pre)
    assert effect.detected
    assert "focus_change" in effect.signals


def test_value_change_detected():
    """Input value change on target element is detected."""
    pre = PageSnapshot(
        url="https://example.com", dom_hash=100,
        element_value="",
    )
    locator = _make_locator(input_value="typed text")
    page = _make_page({
        "url": "https://example.com",
        "domHash": 100,
        "activeTag": "body",
        "activeId": "",
        "listboxCount": 0,
        "menuCount": 0,
        "dialogCount": 0,
    })
    effect = detect_behavior_effect(page, locator, pre)
    assert effect.detected
    assert "value_change" in effect.signals


def test_no_effect_detected():
    """No changes in any signal → no effect."""
    pre = PageSnapshot(
        url="https://example.com", dom_hash=100,
        active_tag="body", active_id="",
        aria_expanded="false", element_value="hello",
    )
    locator = _make_locator(aria_expanded="false", input_value="hello")
    page = _make_page({
        "url": "https://example.com",
        "domHash": 100,
        "activeTag": "body",
        "activeId": "",
        "listboxCount": 0,
        "menuCount": 0,
        "dialogCount": 0,
    })
    effect = detect_behavior_effect(page, locator, pre)
    assert not effect.detected
    assert effect.signals == []
    assert "No observable effect" in effect.detail


def test_multiple_signals_detected():
    """Multiple signals fire simultaneously."""
    pre = PageSnapshot(
        url="https://example.com/login", dom_hash=100,
        active_tag="body", active_id="",
        listbox_count=0,
    )
    page = _make_page({
        "url": "https://example.com/dashboard",
        "domHash": 999,
        "activeTag": "input",
        "activeId": "search",
        "listboxCount": 1,
        "menuCount": 0,
        "dialogCount": 0,
    })
    effect = detect_behavior_effect(page, None, pre)
    assert effect.detected
    assert len(effect.signals) >= 3
    assert "url_change" in effect.signals
    assert "dom_mutation" in effect.signals
    assert "focus_change" in effect.signals


def test_behavior_effect_to_dict():
    """BehaviorEffect.to_dict() serializes correctly."""
    effect = BehaviorEffect(
        detected=True,
        signals=["url_change", "dom_mutation"],
        detail="url_change, dom_mutation",
    )
    d = effect.to_dict()
    assert d["detected"] is True
    assert d["signals"] == ["url_change", "dom_mutation"]
    assert d["detail"] == "url_change, dom_mutation"
