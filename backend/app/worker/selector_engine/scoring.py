"""Multi-criteria candidate scoring for selector resolution.

Each candidate element gets a composite score (0.0–1.0+) based on:
- Strategy priority (test_id > role+name > label > placeholder > text > css)
- Visibility (visible in viewport vs hidden)
- Enabled state (enabled vs disabled/aria-disabled)
- Match confidence (exact match vs partial, single vs multiple matches)
- DOM proximity to the last interacted element (optional)
- History bonus: additive boost from cross-run selector memory

Scoring is entirely deterministic — no AI involved.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from app.worker.selector_resilience import SelectorStrategy

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


# Strategy priority weights — higher = more reliable selector
_STRATEGY_WEIGHTS: dict[SelectorStrategy, float] = {
    SelectorStrategy.TEST_ID: 1.0,
    SelectorStrategy.ROLE: 0.85,
    SelectorStrategy.LABEL: 0.75,
    SelectorStrategy.PLACEHOLDER: 0.60,
    SelectorStrategy.INTERACTIVE_ANCESTOR: 0.55,
    SelectorStrategy.ARIA_WIDGET: 0.55,
    SelectorStrategy.NEAR_LABEL: 0.50,
    SelectorStrategy.TEXT: 0.45,
    SelectorStrategy.CSS: 0.30,
}


@dataclass
class ScoringWeights:
    """Tunable weights for the composite score calculation."""

    strategy: float = 0.30
    visibility: float = 0.25
    enabled: float = 0.10
    match_confidence: float = 0.20
    proximity: float = 0.15


@dataclass
class CandidateScore:
    """Scored candidate element with breakdown."""

    locator: Locator
    strategy: SelectorStrategy
    params: dict
    composite: float = 0.0

    # Score breakdown
    strategy_score: float = 0.0
    visibility_score: float = 0.0
    enabled_score: float = 0.0
    match_confidence_score: float = 0.0
    proximity_score: float = 0.0
    history_bonus: float = 0.0
    resolution_tier: int = 0

    # Metadata for telemetry
    element_tag: str = ""
    element_text: str = ""
    candidate_index: int = 0
    total_candidates: int = 0

    def to_dict(self) -> dict:
        """Serialize for telemetry/logging."""
        return {
            "strategy": self.strategy.value,
            "params": self.params,
            "composite": round(self.composite, 3),
            "breakdown": {
                "strategy": round(self.strategy_score, 3),
                "visibility": round(self.visibility_score, 3),
                "enabled": round(self.enabled_score, 3),
                "match_confidence": round(self.match_confidence_score, 3),
                "proximity": round(self.proximity_score, 3),
                "history_bonus": round(self.history_bonus, 3),
            },
            "element_tag": self.element_tag,
            "candidate_index": self.candidate_index,
            "total_candidates": self.total_candidates,
        }


def score_candidates(
    page: Page,
    locator: Locator,
    strategy: SelectorStrategy,
    params: dict,
    weights: ScoringWeights | None = None,
    last_action_selector: str | None = None,
    timeout_ms: int = 3000,
    history_records: dict[str, int] | None = None,
    action_hint: str = "",
    used_selector_paths: frozenset[str] | None = None,
    target_fields: dict[str, str] | None = None,
) -> list[CandidateScore]:
    """Score all elements matched by a locator.

    Collects every element the locator resolves to, evaluates each on
    multiple criteria, and returns them sorted by composite score (descending).

    Args:
        page: Playwright Page.
        locator: Multi-element locator (before .first).
        strategy: Which strategy produced this locator.
        params: Strategy params for telemetry.
        weights: Scoring weights (defaults to ScoringWeights()).
        last_action_selector: CSS selector path of the last interacted element,
            used for DOM proximity scoring. None on first action.
        timeout_ms: Max time to wait for at least one match.
        history_records: Cross-run selector memory mapping
            ``"strategy:params_key"`` → ``success_count``.
            Used to compute an additive history bonus:
            ``log(success_count + 1) * 0.1``.

    Returns:
        Sorted list of CandidateScore (highest first). Empty if no matches.
    """
    w = weights or ScoringWeights()

    # Wait briefly for at least one element to appear
    try:
        locator.first.wait_for(state="attached", timeout=timeout_ms)
    except Exception:
        return []

    count = locator.count()
    if count == 0:
        return []

    # Collect element metadata via a single JS evaluation for performance
    metadata_list = _collect_element_metadata(locator, count)

    candidates: list[CandidateScore] = []
    strategy_weight = _STRATEGY_WEIGHTS.get(strategy, 0.3)

    for i, meta in enumerate(metadata_list):
        cs = CandidateScore(
            locator=locator.nth(i),
            strategy=strategy,
            params=params,
            candidate_index=i,
            total_candidates=count,
            element_tag=meta.get("tag", ""),
            element_text=meta.get("text", "")[:80],
        )

        # 1) Strategy priority score
        cs.strategy_score = strategy_weight

        # 2) Visibility score
        cs.visibility_score = _score_visibility(meta)

        # 3) Enabled state
        cs.enabled_score = _score_enabled(meta)

        # 4) Match confidence (penalize when many candidates share the same strategy)
        cs.match_confidence_score = _score_match_confidence(count, meta, strategy, params)

        # 5) DOM proximity to last action
        if last_action_selector:
            cs.proximity_score = _score_proximity(
                meta, last_action_selector, page,
            )
        else:
            # No prior context — neutral score
            cs.proximity_score = 0.5

        # 6) History bonus from cross-run selector memory (additive)
        cs.history_bonus = _compute_history_bonus(strategy, params, history_records)

        # 7) Action-type penalty (reduce score for element/action mismatches)
        action_penalty = _action_type_penalty(meta, action_hint)

        # 8) Used-element penalty: heavily penalize elements already used in this step
        used_penalty = _used_element_penalty(meta, used_selector_paths)

        # 9) Target coherence: penalize when element contradicts other target fields
        coherence_penalty = _target_coherence_penalty(
            meta, strategy, target_fields or {},
        )

        # Composite score
        cs.composite = (
            w.strategy * cs.strategy_score
            + w.visibility * cs.visibility_score
            + w.enabled * cs.enabled_score
            + w.match_confidence * cs.match_confidence_score
            + w.proximity * cs.proximity_score
            + cs.history_bonus
            - action_penalty
            - used_penalty
            - coherence_penalty
        )

        candidates.append(cs)

    candidates.sort(key=lambda c: c.composite, reverse=True)
    return candidates


_ELEMENT_METADATA_JS = """(el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    function getSelectorPath(node) {
        const parts = [];
        for (let d = 0; d < 5 && node && node !== document.body; d++) {
            let s = node.tagName.toLowerCase();
            if (node.id) {
                s += '#' + node.id;
            } else {
                const nm = node.getAttribute('name');
                const tp = node.getAttribute('type');
                if (nm) s += '[name="' + nm + '"]';
                else if (tp && node.tagName === 'INPUT') s += '[type="' + tp + '"]';
                const parent = node.parentElement;
                if (parent) {
                    const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
                    if (sibs.length > 1) s += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
                }
            }
            parts.unshift(s);
            node = node.parentElement;
        }
        return parts.join(' > ');
    }
    return {
        tag: el.tagName.toLowerCase(),
        text: (el.textContent || '').trim().substring(0, 80),
        visible: rect.width > 0 && rect.height > 0
            && style.visibility !== 'hidden'
            && style.display !== 'none'
            && parseFloat(style.opacity || '1') > 0.1,
        in_viewport: rect.top < window.innerHeight
            && rect.bottom > 0
            && rect.left < window.innerWidth
            && rect.right > 0,
        enabled: !el.disabled
            && el.getAttribute('aria-disabled') !== 'true'
            && !el.classList.contains('disabled'),
        rect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
        test_id: el.getAttribute('data-testid') || '',
        role: el.getAttribute('role') || '',
        aria_label: el.getAttribute('aria-label') || '',
        name_attr: el.getAttribute('name') || '',
        placeholder: el.getAttribute('placeholder') || '',
        id: el.id || '',
        selector_path: getSelectorPath(el),
        associated_label: (() => {
            if (el.labels && el.labels.length > 0)
                return (el.labels[0].textContent || '').trim().substring(0, 80);
            if (el.id) {
                var lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (lbl) return (lbl.textContent || '').trim().substring(0, 80);
            }
            return '';
        })(),
    };
}"""


def _collect_element_metadata(locator: Locator, count: int) -> list[dict]:
    """Extract metadata from all matched elements via per-element JS evaluation.

    Returns list of dicts with: tag, text, visible, enabled, rect, attributes.
    Caps at 20 candidates to bound evaluation time.
    """
    results = []
    for i in range(min(count, 20)):
        try:
            meta = locator.nth(i).evaluate(_ELEMENT_METADATA_JS)
            results.append(meta)
        except Exception:
            results.append({})
    return results


def _score_visibility(meta: dict) -> float:
    """Score element visibility: 1.0 visible+in_viewport, 0.6 visible but off-screen, 0.0 hidden."""
    if not meta.get("visible", False):
        return 0.0
    if meta.get("in_viewport", False):
        return 1.0
    return 0.6


def _score_enabled(meta: dict) -> float:
    """Score enabled state: 1.0 enabled, 0.1 disabled."""
    return 1.0 if meta.get("enabled", True) else 0.1


def _score_match_confidence(
    total_count: int,
    meta: dict,
    strategy: SelectorStrategy,
    params: dict,
) -> float:
    """Score match confidence based on uniqueness and attribute alignment.

    Single match = high confidence. Multiple matches = penalized.
    Extra credit if the matched element's attributes strongly align with params.
    """
    # Base: uniqueness (1 match = 1.0, 2 = 0.7, 3+ = 0.5, 10+ = 0.3)
    if total_count == 1:
        uniqueness = 1.0
    elif total_count == 2:
        uniqueness = 0.7
    elif total_count <= 5:
        uniqueness = 0.5
    elif total_count <= 10:
        uniqueness = 0.3
    else:
        uniqueness = 0.2

    # Attribute alignment bonus: check if element attrs match the strategy params
    alignment = _attribute_alignment(meta, strategy, params)

    return min(1.0, uniqueness * 0.6 + alignment * 0.4)


def _normalize_ws(s: str) -> str:
    """Collapse all whitespace to single spaces and strip."""
    return " ".join(s.split()).lower()


def _attribute_alignment(
    meta: dict, strategy: SelectorStrategy, params: dict,
) -> float:
    """Check how well an element's attributes align with the target params.

    Uses whitespace-normalized comparison so "Sign  In" matches "Sign In".
    """
    score = 0.5  # baseline

    if strategy == SelectorStrategy.TEST_ID:
        if meta.get("test_id") == params.get("test_id", ""):
            score = 1.0

    elif strategy == SelectorStrategy.ROLE:
        role_match = _normalize_ws(meta.get("role", "")) == _normalize_ws(params.get("role", ""))
        name_param = _normalize_ws(params.get("name", ""))
        name_match = (
            name_param in _normalize_ws(meta.get("aria_label", ""))
            or name_param in _normalize_ws(meta.get("text", ""))
        ) if name_param else True
        if role_match and name_match:
            score = 1.0
        elif role_match:
            score = 0.7

    elif strategy == SelectorStrategy.LABEL:
        label_param = _normalize_ws(params.get("label", ""))
        if label_param and (
            label_param in _normalize_ws(meta.get("aria_label", ""))
            or label_param in _normalize_ws(meta.get("text", ""))
        ):
            score = 1.0

    elif strategy == SelectorStrategy.PLACEHOLDER:
        ph_param = _normalize_ws(params.get("placeholder", ""))
        if ph_param and ph_param in _normalize_ws(meta.get("placeholder", "")):
            score = 1.0

    elif strategy == SelectorStrategy.TEXT:
        text_param = _normalize_ws(params.get("text", ""))
        if text_param and text_param in _normalize_ws(meta.get("text", "")):
            score = 1.0

    elif strategy == SelectorStrategy.INTERACTIVE_ANCESTOR:
        text_param = _normalize_ws(params.get("text", ""))
        if text_param and text_param in _normalize_ws(meta.get("text", "")):
            score = 1.0
        elif text_param and text_param in _normalize_ws(meta.get("aria_label", "")):
            score = 0.8

    elif strategy == SelectorStrategy.ARIA_WIDGET:
        text_param = _normalize_ws(params.get("text", ""))
        if text_param and text_param in _normalize_ws(meta.get("text", "")):
            score = 1.0
        elif text_param and text_param in _normalize_ws(meta.get("aria_label", "")):
            score = 0.8

    elif strategy == SelectorStrategy.NEAR_LABEL:
        label_param = _normalize_ws(params.get("label", ""))
        if label_param and (
            label_param in _normalize_ws(meta.get("aria_label", ""))
            or label_param in _normalize_ws(meta.get("text", ""))
        ):
            score = 1.0

    return score


# Action-type compatibility maps for scoring penalties
_ACTION_COMPATIBLE_TAGS: dict[str, frozenset[str]] = {
    "input": frozenset({"input", "textarea", "select"}),
    "click": frozenset({"button", "a", "input", "summary", "details", "label"}),
    "select": frozenset({"select", "input", "button", "div"}),
    "check": frozenset({"input"}),
    "uncheck": frozenset({"input"}),
}

_ACTION_COMPATIBLE_ROLES: dict[str, frozenset[str]] = {
    "input": frozenset({"textbox", "searchbox", "spinbutton", "combobox"}),
    "click": frozenset({"button", "link", "menuitem", "tab", "radio", "checkbox", "switch", "option"}),
    "select": frozenset({"combobox", "listbox", "select"}),
    "check": frozenset({"checkbox", "switch"}),
    "uncheck": frozenset({"checkbox", "switch"}),
}


def _action_type_penalty(meta: dict, action_hint: str) -> float:
    """Return a penalty (0.0 to 0.35) when element type mismatches the action.

    Uses tag and ARIA role to detect obvious mismatches:
    - input action on a button/link → 0.35 penalty
    - click action on a textarea → 0.2 penalty
    - No action hint → 0.0 (no penalty)
    """
    if not action_hint:
        return 0.0

    action_type = action_hint.split()[0].lower()

    compatible_tags = _ACTION_COMPATIBLE_TAGS.get(action_type)
    compatible_roles = _ACTION_COMPATIBLE_ROLES.get(action_type)

    if compatible_tags is None and compatible_roles is None:
        return 0.0  # Unknown action type, no penalty

    tag = meta.get("tag", "").lower()
    role = meta.get("role", "").lower()

    # Check if element matches by tag OR role
    tag_ok = compatible_tags and tag in compatible_tags
    role_ok = compatible_roles and role in compatible_roles

    if tag_ok or role_ok:
        return 0.0  # Compatible

    # Specific high-penalty: input action targeting a non-input element
    if action_type == "input" and tag in ("button", "a", "span", "div", "label"):
        return 0.35

    # General mismatch penalty
    return 0.2


def _used_element_penalty(
    meta: dict,
    used_selector_paths: frozenset[str] | None,
) -> float:
    """Return a heavy penalty (0.5) if this element was already used in the current step.

    Prevents the resolver from picking the same input twice (e.g., typing
    the password into the email field because both are <input> elements).
    """
    if not used_selector_paths:
        return 0.0

    candidate_path = meta.get("selector_path", "")
    if candidate_path and candidate_path in used_selector_paths:
        logger.debug(
            "scoring.used_element_penalty",
            selector_path=candidate_path,
        )
        return 0.5

    return 0.0


# Maps each target descriptor field to element metadata keys for cross-checking.
_FIELD_TO_META: dict[str, tuple[str, ...]] = {
    "label": ("aria_label", "text", "associated_label"),
    "placeholder": ("placeholder",),
    "name": ("aria_label", "text", "name_attr"),
    "text": ("text",),
    "role": ("role",),
}

# Maps each selector strategy to the target field it was built from.
_STRATEGY_ORIGIN_FIELD: dict[SelectorStrategy, str] = {
    SelectorStrategy.LABEL: "label",
    SelectorStrategy.PLACEHOLDER: "placeholder",
    SelectorStrategy.ROLE: "role",
    SelectorStrategy.TEXT: "text",
    SelectorStrategy.NEAR_LABEL: "label",
}


def _target_coherence_penalty(
    meta: dict,
    strategy: SelectorStrategy,
    target_fields: dict[str, str],
) -> float:
    """Penalize candidates whose attributes contradict other target descriptor fields.

    When a candidate matches on one strategy (e.g., PLACEHOLDER), check if
    the element's other attributes are inconsistent with the rest of the
    TargetDescriptor.  For example, if target says label="Password" but the
    matched element's label text says "Email", that's a contradiction.

    Rules:
    - Only triggers when target has 2+ fields.
    - Skips the field that the current strategy was built from.
    - Element *missing* an attribute is NOT a contradiction.
    - Element *having a different value* IS a contradiction.
    - Penalty capped at 0.4.
    """
    if len(target_fields) < 2:
        return 0.0

    origin_field = _STRATEGY_ORIGIN_FIELD.get(strategy)
    penalty = 0.0

    for field_name, field_value in target_fields.items():
        # Skip the field this strategy was built from (already validated)
        if field_name == origin_field:
            continue

        meta_keys = _FIELD_TO_META.get(field_name)
        if not meta_keys:
            continue

        target_val = _normalize_ws(field_value)
        if not target_val:
            continue

        for meta_key in meta_keys:
            element_val = _normalize_ws(meta.get(meta_key, ""))
            if not element_val:
                continue  # Element lacks this attribute — not a contradiction
            if target_val not in element_val and element_val not in target_val:
                # Element has a value that doesn't match the target field
                penalty = max(penalty, 0.3)
                break

    return min(penalty, 0.4)


def _score_proximity(
    meta: dict,
    last_action_selector: str,
    page: Page,
) -> float:
    """Score DOM proximity to the last interacted element.

    Uses a lightweight JS check: count shared ancestors in selector paths.
    If the last action element is no longer in DOM, returns neutral 0.5.
    """
    candidate_path = meta.get("selector_path", "")
    if not candidate_path or not last_action_selector:
        return 0.5

    # Simple heuristic: count shared path prefix segments
    last_parts = last_action_selector.split(" > ")
    cand_parts = candidate_path.split(" > ")

    shared = 0
    for lp, cp in zip(last_parts, cand_parts):
        if lp == cp:
            shared += 1
        else:
            break

    max_depth = max(len(last_parts), len(cand_parts), 1)
    # More shared ancestors = closer in DOM = higher score
    return min(1.0, shared / max_depth + 0.3)


def _compute_history_bonus(
    strategy: SelectorStrategy,
    params: dict,
    history_records: dict[str, int] | None,
) -> float:
    """Compute additive bonus from cross-run selector memory.

    Formula: ``log(success_count + 1) * 0.1``

    A selector with 10 prior successes gets +0.24 bonus.
    A selector with 0 prior successes gets +0.0 bonus.
    """
    if not history_records:
        return 0.0

    key = _history_key(strategy, params)
    success_count = history_records.get(key, 0)
    if success_count <= 0:
        return 0.0

    return math.log(success_count + 1) * 0.1


def _history_key(strategy: SelectorStrategy, params: dict) -> str:
    """Build a lookup key for history_records matching the format used by the resolver."""
    return f"{strategy.value}:{params}"
