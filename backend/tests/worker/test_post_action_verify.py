"""Test: Post-Action State Verification (Part 3).

Validates per-action verification:
- click: URL change, aria-expanded toggle, listbox appearance
- fill: value match
- navigate: URL match
"""

from unittest.mock import MagicMock, PropertyMock

from app.worker.post_action_verify import (
    capture_pre_state,
    verify_click,
    verify_fill,
    verify_navigate,
)


def test_click_url_change_detected():
    """URL changes after click is verified."""
    page = MagicMock()
    locator = MagicMock()

    pre_state = {"url": "https://example.com/page1"}
    page.url = "https://example.com/page2"

    result = verify_click(page, locator, pre_state)

    assert result.verified
    assert result.method == "url_change"


def test_click_aria_expanded_toggled():
    """aria-expanded flips after click is verified."""
    page = MagicMock()
    page.url = "https://example.com"
    locator = MagicMock()
    locator.get_attribute.return_value = "true"

    pre_state = {
        "url": "https://example.com",
        "aria_expanded": "false",
        "listbox_count": 0,
        "menu_count": 0,
    }

    result = verify_click(page, locator, pre_state)

    assert result.verified
    assert result.method == "aria_expanded"


def test_click_listbox_appeared():
    """New listbox visible after click is verified."""
    page = MagicMock()
    page.url = "https://example.com"
    locator = MagicMock()
    locator.get_attribute.return_value = None  # No aria-expanded

    # Listbox count increased
    page.get_by_role.return_value.count.return_value = 1

    pre_state = {
        "url": "https://example.com",
        "aria_expanded": None,
        "listbox_count": 0,
        "menu_count": 0,
    }

    result = verify_click(page, locator, pre_state)

    assert result.verified
    assert result.method == "dom_mutation"


def test_click_no_change_detected():
    """Nothing changed after click is not verified (warn)."""
    page = MagicMock()
    page.url = "https://example.com"
    locator = MagicMock()
    locator.get_attribute.return_value = None

    page.get_by_role.return_value.count.return_value = 0

    pre_state = {
        "url": "https://example.com",
        "aria_expanded": None,
        "listbox_count": 0,
        "menu_count": 0,
    }

    result = verify_click(page, locator, pre_state)

    assert not result.verified
    assert result.method == "none"


def test_fill_value_matches():
    """input.value matches expected is verified."""
    locator = MagicMock()
    locator.input_value.return_value = "test@example.com"

    result = verify_fill(locator, "test@example.com")

    assert result.verified
    assert result.method == "value_match"


def test_fill_value_mismatch():
    """Value doesn't match is not verified."""
    locator = MagicMock()
    locator.input_value.return_value = "wrong_value"

    result = verify_fill(locator, "expected_value")

    assert not result.verified
    assert result.method == "value_match"
    assert "mismatch" in result.detail


def test_navigate_url_matches():
    """URL matches pattern is verified."""
    page = MagicMock()
    page.url = "https://example.com/dashboard"

    result = verify_navigate(page, "https://example.com/dashboard")

    assert result.verified
    assert result.method == "url_change"
