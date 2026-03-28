"""Transform raw browser events into StepIntent objects.

Events arrive as JSON from the capture script running in the user's browser.
Groups events into meaningful interactions and maps them to the StepIntent
format used by the execution engine.

Grouping rules:
- click on input + subsequent input events → single INPUT step
- click on button/link → CLICK step
- change on select → SELECT step
- URL change (pushState/navigation) → NAVIGATE step
- form submit → ends current group (button click covers it)
- Enter keypress in input → ends input group

Target resolution priority:
  1. aria_label
  2. element text (buttons/links)
  3. placeholder (inputs)
  4. name attribute
  5. role + id fallback
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from app.worker.intent_schema import (
    ActionIntent,
    ActionType,
    StepIntent,
    TargetDescriptor,
)

logger = structlog.get_logger(__name__)

# Max gap between events to be part of the same interaction window (ms)
_GROUP_GAP_MS = 2000

_INPUT_TAGS = frozenset({"input", "textarea"})
_SELECT_TAGS = frozenset({"select"})
_CLICKABLE_ROLES = frozenset({"button", "link", "menuitem", "tab", "checkbox", "radio"})

# HTML tags → ARIA roles
_ROLE_MAP = {
    "button": "button",
    "a": "link",
    "input": "textbox",
    "textarea": "textbox",
    "select": "combobox",
}


@dataclass
class _EventGroup:
    """Raw events grouped into one logical interaction."""

    events: list[dict] = field(default_factory=list)

    @property
    def primary(self) -> dict:
        return self.events[0]

    @property
    def last(self) -> dict:
        return self.events[-1]

    @property
    def has_input(self) -> bool:
        return any(e["type"] in ("input",) for e in self.events)

    @property
    def input_value(self) -> str:
        """Last captured value from input events in this group."""
        for e in reversed(self.events):
            v = (e.get("element") or {}).get("value", "")
            if v:
                return v
        return ""


def _resolved_role(tag: str, raw_role: str) -> str | None:
    return _ROLE_MAP.get(tag) or raw_role or None


def best_label(element: dict) -> TargetDescriptor:
    """Build the most stable TargetDescriptor from a captured element.

    Priority:
      1. aria_label  → role + name
      2. button/link text → role + name
      3. placeholder  → role + placeholder
      4. name attr    → role + label
      5. element id   → test_id
      6. role only    → role fallback
    """
    tag = (element.get("tag") or "").lower()
    raw_role = (element.get("role") or "").lower()
    role = _resolved_role(tag, raw_role)

    aria_label = (element.get("aria_label") or "").strip()
    text = (element.get("text") or "").strip()[:100]
    placeholder = (element.get("placeholder") or "").strip()
    name = (element.get("name") or "").strip()
    elem_id = (element.get("id") or "").strip()

    if aria_label:
        return TargetDescriptor(role=role, name=aria_label)

    if text and (tag in ("button", "a") or raw_role in ("button", "link")):
        return TargetDescriptor(role=role, name=text)

    if placeholder:
        return TargetDescriptor(role=role, placeholder=placeholder)

    if name:
        return TargetDescriptor(role=role, label=name)

    if elem_id and not elem_id.isdigit():
        return TargetDescriptor(role=role, test_id=elem_id)

    return TargetDescriptor(role=role or "element")


def _group_events(events: list[dict]) -> list[_EventGroup]:
    """Group a flat list of events into logical interaction windows."""
    if not events:
        return []

    sorted_events = sorted(events, key=lambda e: e.get("timestamp", 0))
    groups: list[_EventGroup] = []
    current = _EventGroup()

    for event in sorted_events:
        etype = event.get("type", "")

        # Navigation always gets its own group
        if etype == "navigation":
            if current.events:
                groups.append(current)
                current = _EventGroup()
            nav_group = _EventGroup(events=[event])
            groups.append(nav_group)
            continue

        # Submit ends the current group (button click was already captured)
        if etype == "submit":
            if current.events:
                groups.append(current)
                current = _EventGroup()
            continue

        if current.events:
            gap_ms = event.get("timestamp", 0) - current.last.get("timestamp", 0)
            el = event.get("element") or {}
            prev_el = current.primary.get("element") or {}

            # Check if same element (by id, name, or placeholder)
            same_el = (
                (el.get("id") and el.get("id") == prev_el.get("id"))
                or (el.get("name") and el.get("name") == prev_el.get("name"))
                or (el.get("placeholder") and el.get("placeholder") == prev_el.get("placeholder"))
            )

            if gap_ms > _GROUP_GAP_MS and not same_el:
                groups.append(current)
                current = _EventGroup()

        current.events.append(event)

    if current.events:
        groups.append(current)

    return [g for g in groups if g.events]


def _group_to_intent(group: _EventGroup, step_number: int) -> StepIntent | None:
    """Convert one event group into a StepIntent."""
    primary = group.primary
    etype = primary.get("type", "")
    element = primary.get("element") or {}
    page = primary.get("page") or {}
    tag = (element.get("tag") or "").lower()
    raw_role = (element.get("role") or "").lower()

    # ── NAVIGATE ────────────────────────────────────────────────────────────
    if etype == "navigation":
        url = page.get("url", "")
        if not url:
            return None
        return StepIntent(
            step_number=step_number,
            actions=[ActionIntent(action=ActionType.NAVIGATE, value=url)],
            description=f"Navigate to {url}",
        )

    # ── INPUT ────────────────────────────────────────────────────────────────
    if tag in _INPUT_TAGS or raw_role in ("textbox", "searchbox"):
        input_type = (element.get("input_type") or element.get("type") or "text").lower()

        if input_type == "checkbox":
            checked = element.get("checked", False)
            action = ActionType.CHECK if checked else ActionType.UNCHECK
            target = best_label(element)
            label = target.name or target.placeholder or "checkbox"
            return StepIntent(
                step_number=step_number,
                actions=[ActionIntent(action=action, target=target)],
                description=f"{'Check' if checked else 'Uncheck'} {label}",
            )

        value = group.input_value
        target = best_label(element)
        field_label = target.name or target.placeholder or "field"
        return StepIntent(
            step_number=step_number,
            actions=[ActionIntent(action=ActionType.INPUT, target=target, value=value)],
            description=f"Enter '{value}' into {field_label}",
        )

    # ── SELECT ───────────────────────────────────────────────────────────────
    if tag in _SELECT_TAGS or raw_role in ("listbox", "combobox"):
        value = group.input_value or (element.get("selected_text") or "")
        target = best_label(element)
        label = target.name or target.label or "dropdown"
        return StepIntent(
            step_number=step_number,
            actions=[ActionIntent(action=ActionType.SELECT, target=target, value=value)],
            description=f"Select '{value}' from {label}",
        )

    # ── CLICK ────────────────────────────────────────────────────────────────
    if etype == "click":
        # Skip clicks on inputs (those become INPUT steps)
        if tag in _INPUT_TAGS:
            if not group.has_input:
                return None  # bare click on input with no typing — noise
        target = best_label(element)
        label = target.name or target.text or target.role or "element"
        return StepIntent(
            step_number=step_number,
            actions=[ActionIntent(action=ActionType.CLICK, target=target)],
            description=f"Click {label}",
        )

    return None


def transform_events_to_intents(events: list[dict]) -> list[StepIntent]:
    """Transform raw browser events into ordered StepIntent objects.

    This is the main entry point called by the capture API.

    Args:
        events: Raw events from the capture script, each containing:
                {type, timestamp, element: {...}, page: {...}}

    Returns:
        Ordered list of StepIntent objects ready for execution.
    """
    if not events:
        return []

    groups = _group_events(events)
    intents: list[StepIntent] = []
    step_num = 1

    for group in groups:
        intent = _group_to_intent(group, step_num)
        if intent is not None:
            intents.append(intent)
            step_num += 1

    logger.info(
        "transformer.complete",
        input_events=len(events),
        output_steps=len(intents),
    )
    return intents
