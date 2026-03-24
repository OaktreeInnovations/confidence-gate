"""Semantic name expansion plugin registry.

Maps well-known semantic names (e.g. "today") to CSS selectors.
Core expansions use ARIA-standard selectors only. Framework-specific
CSS selectors are registered as plugins driven by component_registry
framework detection.

Usage:
    from app.worker.selector_engine.semantic_plugins import (
        get_semantic_expansions,
        register_framework,
    )

    register_framework("radix")  # After framework detection
    selectors = get_semantic_expansions("today")
    # → ["[aria-current='date']", "[data-today='true']", ".rdp-day_today"]
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

# Core expansions: ARIA-standard selectors only (framework-agnostic)
_CORE_EXPANSIONS: dict[str, list[str]] = {
    "today": ["[aria-current='date']", "[data-today='true']"],
}

# Framework-specific expansions keyed by framework name
_FRAMEWORK_EXPANSIONS: dict[str, dict[str, list[str]]] = {
    "radix": {
        "today": [".rdp-day_today"],
    },
    "generic": {
        "today": ["td.today"],
    },
    "mui": {
        "today": [".MuiPickersDay-today"],
    },
    "react-datepicker": {
        "today": [".react-datepicker__day--today"],
    },
}

# Active frameworks detected on the current page
_active_frameworks: set[str] = set()


def register_framework(name: str) -> None:
    """Register a detected framework for semantic expansion."""
    if name and name != "generic":
        _active_frameworks.add(name)
        logger.debug("semantic_plugins.registered", framework=name)


def clear_frameworks() -> None:
    """Clear all registered frameworks (e.g. on page navigation)."""
    _active_frameworks.clear()


def get_semantic_expansions(name: str) -> list[str]:
    """Return CSS selectors for a well-known semantic name.

    Combines core ARIA selectors with framework-specific selectors
    for any active frameworks.

    Args:
        name: Semantic name to expand (e.g. "today").

    Returns:
        List of CSS selectors. Empty if name is unknown.
    """
    key = name.lower().strip()
    result = list(_CORE_EXPANSIONS.get(key, []))

    # Always include generic expansions
    generic = _FRAMEWORK_EXPANSIONS.get("generic", {})
    result.extend(generic.get(key, []))

    # Add framework-specific expansions
    for fw in _active_frameworks:
        fw_expansions = _FRAMEWORK_EXPANSIONS.get(fw, {})
        result.extend(fw_expansions.get(key, []))

    return result
