"""Action Outcome Model — declarative verification rules per action type.

Defines what state changes are expected for each ActionType and how to verify
them. Used by post_action_verify to run the correct checks after each action.

Each ActionOutcome declares:
- expected_state_changes: human-readable list of what should change
- verification_methods: ordered list of verification strategies to try
- timeout_budget_ms: max time to spend on verification
- required: whether verification failure should be treated as a warning or error
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class VerificationMethod(StrEnum):
    """Strategies for verifying an action had its intended effect."""

    URL_CHANGE = "url_change"
    ARIA_EXPANDED = "aria_expanded"
    LISTBOX_APPEARED = "listbox_appeared"
    MENU_APPEARED = "menu_appeared"
    VALUE_MATCH = "value_match"
    OPTION_SELECTED = "option_selected"
    CHECKED_STATE = "checked_state"
    ELEMENT_VISIBLE = "element_visible"
    ELEMENT_HIDDEN = "element_hidden"
    TEXT_APPEARED = "text_appeared"
    NONE = "none"


@dataclass
class ActionOutcome:
    """Declarative verification rules for one action type."""

    action_type: str
    expected_state_changes: list[str] = field(default_factory=list)
    verification_methods: list[VerificationMethod] = field(default_factory=list)
    timeout_budget_ms: int = 2000
    required: bool = False  # If True, verification failure triggers retry


# Registry of outcomes per action type
_OUTCOME_REGISTRY: dict[str, ActionOutcome] = {
    "click": ActionOutcome(
        action_type="click",
        expected_state_changes=[
            "URL may change (navigation)",
            "aria-expanded may toggle (dropdown/accordion)",
            "New listbox/menu may appear",
        ],
        verification_methods=[
            VerificationMethod.URL_CHANGE,
            VerificationMethod.ARIA_EXPANDED,
            VerificationMethod.LISTBOX_APPEARED,
            VerificationMethod.MENU_APPEARED,
        ],
        timeout_budget_ms=2000,
        required=False,  # Clicks can have side effects not captured by these checks
    ),
    "input": ActionOutcome(
        action_type="input",
        expected_state_changes=["Input value matches expected text"],
        verification_methods=[VerificationMethod.VALUE_MATCH],
        timeout_budget_ms=1000,
        required=True,  # Fill must result in correct value
    ),
    "navigate": ActionOutcome(
        action_type="navigate",
        expected_state_changes=["URL matches expected pattern"],
        verification_methods=[VerificationMethod.URL_CHANGE],
        timeout_budget_ms=3000,
        required=True,
    ),
    "select": ActionOutcome(
        action_type="select",
        expected_state_changes=[
            "Selected option text visible in trigger",
            "aria-selected attribute set on option",
        ],
        verification_methods=[VerificationMethod.OPTION_SELECTED],
        timeout_budget_ms=2000,
        required=True,
    ),
    "select_date": ActionOutcome(
        action_type="select_date",
        expected_state_changes=["Date value visible in input/trigger"],
        verification_methods=[VerificationMethod.VALUE_MATCH],
        timeout_budget_ms=2000,
        required=False,
    ),
    "check": ActionOutcome(
        action_type="check",
        expected_state_changes=["Checkbox checked state is true"],
        verification_methods=[VerificationMethod.CHECKED_STATE],
        timeout_budget_ms=1000,
        required=True,
    ),
    "uncheck": ActionOutcome(
        action_type="uncheck",
        expected_state_changes=["Checkbox checked state is false"],
        verification_methods=[VerificationMethod.CHECKED_STATE],
        timeout_budget_ms=1000,
        required=True,
    ),
    "wait_for_text": ActionOutcome(
        action_type="wait_for_text",
        expected_state_changes=["Text is visible on page"],
        verification_methods=[VerificationMethod.TEXT_APPEARED],
        timeout_budget_ms=0,  # Already verified by the wait itself
        required=False,
    ),
    "assert_visible": ActionOutcome(
        action_type="assert_visible",
        expected_state_changes=["Element is visible"],
        verification_methods=[VerificationMethod.ELEMENT_VISIBLE],
        timeout_budget_ms=0,
        required=False,
    ),
}


def get_outcome(action_type: str) -> ActionOutcome | None:
    """Get the verification outcome model for an action type."""
    return _OUTCOME_REGISTRY.get(action_type)


def get_all_outcomes() -> dict[str, ActionOutcome]:
    """Get all registered action outcomes."""
    return dict(_OUTCOME_REGISTRY)
