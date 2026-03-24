"""Structured Action Intent schema for deterministic test execution.

Instead of AI generating executable Playwright Python code, AI produces
structured JSON describing WHAT to do (action type + target descriptor).
A deterministic runtime then handles HOW (selector resolution, waits, retries).

This module defines:
- ActionType: 13 allowed atomic actions
- TargetDescriptor: semantic element descriptor (no raw CSS from AI)
- ActionIntent: one atomic action with target + value
- StepIntent: complete intent for a test step (1+ actions)
- ExecutionMode: FAST / STANDARD / DEEP configurations
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ActionType(StrEnum):
    """Allowed atomic actions. Maps 1:1 to deterministic wrappers."""

    NAVIGATE = "navigate"
    INPUT = "input"
    CLICK = "click"
    SELECT = "select"
    SELECT_DATE = "select_date"
    UPLOAD_FILE = "upload_file"
    CHECK = "check"
    UNCHECK = "uncheck"
    WAIT_FOR_TEXT = "wait_for_text"
    WAIT_FOR_NAVIGATION = "wait_for_navigation"
    ASSERT_TEXT = "assert_text"
    ASSERT_VISIBLE = "assert_visible"
    ASSERT_VALUE = "assert_value"
    EXTRACT_VALUE = "extract_value"


# Actions that do not require a target element
_TARGETLESS_ACTIONS = frozenset({
    ActionType.NAVIGATE,
    ActionType.WAIT_FOR_NAVIGATION,
})


class TargetDescriptor(BaseModel):
    """Describes HOW to find an element using semantic attributes.

    AI populates whichever fields are available from the page context.
    The selector engine resolves these in priority order:
    data-testid → role+name → label → placeholder → text → css

    Shadow DOM / iframe support:
    - shadow_host: CSS selector for shadow DOM host element
    - iframe: frame name, URL pattern, or CSS selector for the iframe
    When set, the resolver scopes element search within that context.
    """

    test_id: str | None = None
    role: str | None = None
    name: str | None = None
    label: str | None = None
    placeholder: str | None = None
    text: str | None = None
    css: str | None = None

    # Shadow DOM / iframe scoping
    shadow_host: str | None = None  # CSS selector for shadow host
    iframe: str | None = None       # frame name, URL pattern, or CSS selector

    def is_empty(self) -> bool:
        return all(
            v is None
            for v in (self.test_id, self.role, self.name, self.label,
                      self.placeholder, self.text, self.css)
        )


class ActionIntent(BaseModel):
    """One atomic action within a step."""

    action: ActionType
    target: TargetDescriptor | None = None
    value: str | None = None

    # select_date extras
    day: str | None = None
    month_label: str | None = None

    # autocomplete extras
    search_text: str | None = None
    select_text: str | None = None


class StepIntent(BaseModel):
    """Complete structured intent for one test step.

    AI generates this JSON. Usually contains 1 action, but multi-action
    steps (e.g. fill + click submit) produce 2+ actions.
    """

    step_number: int
    actions: list[ActionIntent]
    description: str = ""

    def to_json_str(self) -> str:
        """Serialize to compact JSON for storage in generated_code field."""
        return self.model_dump_json(exclude_none=True)

    @classmethod
    def from_json_str(cls, raw: str) -> StepIntent:
        """Deserialize from JSON string."""
        return cls.model_validate_json(raw)


class ExecutionMode(StrEnum):
    """Execution mode presets controlling wait budgets, retries, and vision."""

    FAST = "FAST"
    STANDARD = "STANDARD"
    DEEP = "DEEP"


EXECUTION_MODE_CONFIG: dict[ExecutionMode, dict] = {
    ExecutionMode.FAST: {
        "vision_enabled": False,
        "wait_budget_ms": 10_000,
        "max_retries": 2,
        "stability_wait_ms": 1000,
    },
    ExecutionMode.STANDARD: {
        "vision_enabled": True,
        "wait_budget_ms": 20_000,
        "max_retries": 3,
        "stability_wait_ms": 2000,
    },
    ExecutionMode.DEEP: {
        "vision_enabled": True,
        "wait_budget_ms": 20_000,
        "max_retries": 3,
        "stability_wait_ms": 3000,
    },
}
