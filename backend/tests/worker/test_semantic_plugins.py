"""Test: Framework-Agnostic Semantic Plugin Registry (Part 5).

Validates that:
- Core expansions return only ARIA-standard selectors when no framework registered
- Framework registration adds framework-specific selectors
- Unknown semantic names return empty list
"""

from app.worker.selector_engine.semantic_plugins import (
    _active_frameworks,
    clear_frameworks,
    get_semantic_expansions,
    register_framework,
)


def setup_function():
    """Clear active frameworks before each test."""
    clear_frameworks()


def test_core_only_returns_aria():
    """No framework registered: only ARIA-standard selectors returned."""
    selectors = get_semantic_expansions("today")

    assert "[aria-current='date']" in selectors
    assert "[data-today='true']" in selectors
    # Generic td.today is always included
    assert "td.today" in selectors
    # Framework-specific should NOT be present
    assert ".rdp-day_today" not in selectors
    assert ".MuiPickersDay-today" not in selectors
    assert ".react-datepicker__day--today" not in selectors


def test_radix_registered_adds_rdp():
    """Radix registered: includes .rdp-day_today."""
    register_framework("radix")
    selectors = get_semantic_expansions("today")

    # Core selectors present
    assert "[aria-current='date']" in selectors
    # Radix-specific selector present
    assert ".rdp-day_today" in selectors
    # MUI should NOT be present (not registered)
    assert ".MuiPickersDay-today" not in selectors


def test_unknown_name_returns_empty():
    """Unknown semantic names return empty list."""
    assert get_semantic_expansions("Submit") == []
    assert get_semantic_expansions("Cancel") == []
    assert get_semantic_expansions("") == []
