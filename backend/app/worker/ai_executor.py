"""Step execution engine using structured JSON intents.

Provides the sole execution entry point `execute_step()` which:
1. Generates a StepIntent (JSON) via AI or loads a cached intent
2. Executes it deterministically via `execute_intent()`
3. Validates the result (DOM + optional vision verification)
4. Collects evidence (screenshot, console, DOM, network)

Also provides page-context capture helpers and vision verification
used by the intent generator and validation modules.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import TYPE_CHECKING

from playwright.sync_api import Page
import structlog

from app.intelligence.code_reuse import code_hash as compute_code_hash
from app.worker.ai_gate import AIGate, AICallBudget
from app.worker.ai_provider import AINotAvailable
from app.worker.component_helpers import resolve_single
from app.worker.evidence_collector import EvidenceCollector, upload_evidence_bundle
from app.worker.intent_schema import EXECUTION_MODE_CONFIG, ExecutionMode, StepIntent
from app.worker.page_stability import wait_for_page_ready
from app.worker.stability_wrappers import _budget_sleep, set_execution_deadline

if TYPE_CHECKING:
    from openai import OpenAI
    from app.intelligence.strategy import StepStrategy
    from app.telemetry.collector import _StepTelemetryBuilder
    from app.worker.ai_provider import AIProvider
    from app.worker.in_run_memory import InRunSelectorMemory, RecoveryEffectivenessTracker
    from app.worker.shared_context import SharedExecutionContext

logger = structlog.get_logger(__name__)

# Hard per-step timeout (seconds) — prevents cascading waits
STEP_HARD_TIMEOUT_S = 90

# Hard cap for wait budget scaling (milliseconds)
_STEP_HARD_CAP_MS = 30_000


def _deterministic_backoff(attempt: int, base_s: float = 1.0, max_s: float = 8.0) -> float:
    """Pure deterministic exponential backoff. Returns sleep duration in seconds."""
    return min(base_s * (2 ** attempt), max_s)


import re as _re

_TEMPLATE_RE = _re.compile(r"\$\{(\w+)\}")


def _builtin_token(key: str) -> str | None:
    """Return a generated value for built-in dynamic tokens, or None if not a built-in."""
    import uuid as _uuid
    import secrets as _secrets
    import string as _string
    from datetime import date as _date

    _charset = _string.ascii_lowercase + _string.digits

    if key == "uuid":
        return str(_uuid.uuid4())
    if key == "timestamp":
        import time as _time
        return str(int(_time.time()))
    if key == "date":
        return _date.today().isoformat()
    if key == "random_int":
        return str(_secrets.randbelow(900000) + 100000)
    if key == "random_string":
        return "".join(_secrets.choice(_charset) for _ in range(8))
    if key == "random_email":
        suffix = "".join(_secrets.choice(_charset) for _ in range(6))
        return f"test_{suffix}@example.com"
    if key == "random_name":
        suffix = "".join(_secrets.choice(_charset) for _ in range(6))
        return f"User_{suffix}"
    return None


def _resolve_templates(intent: StepIntent, test_data: dict[str, str] | None) -> None:
    """Resolve ${key} template references in intent action values.

    Supports two sources (in priority order):
    1. Built-in dynamic tokens (${uuid}, ${timestamp}, ${date}, ${random_email},
       ${random_name}, ${random_int}, ${random_string}) — generated fresh each run.
    2. Test data key-value pairs defined on the test case.

    Mutates intent in-place.
    """
    # Cache generated values per token so the same token produces the same value
    # within a single step execution (e.g. two ${uuid} refs get the same UUID).
    _generated: dict[str, str] = {}

    def _resolve_key(key: str) -> str:
        if key in _generated:
            return _generated[key]
        # Built-in tokens take priority over test_data keys
        built_in = _builtin_token(key)
        if built_in is not None:
            _generated[key] = built_in
            return built_in
        # Fall back to static test_data
        if test_data and key in test_data:
            return test_data[key]
        return f"${{{key}}}"  # unresolved — leave as-is

    def _sub(s: str) -> str:
        return _TEMPLATE_RE.sub(lambda m: _resolve_key(m.group(1)), s)

    for action in intent.actions:
        if action.value and _TEMPLATE_RE.search(action.value):
            action.value = _sub(action.value)
        # Also resolve templates in target fields (text, name, label, placeholder)
        if action.target:
            for field in ("text", "name", "label", "placeholder"):
                val = getattr(action.target, field, None)
                if val and _TEMPLATE_RE.search(val):
                    setattr(action.target, field, _sub(val))


def _pre_validate_intent(
    intent: StepIntent,
    page_context: dict | None,
    page: Page | None = None,
) -> str | None:
    """Pre-validate intent targets against the current accessibility tree.

    Checks whether each action's target can plausibly be found on the page.
    Returns None if all targets look valid, or a feedback string describing
    which targets are missing (used to trigger immediate regeneration).

    Only checks actions that require a target (skip navigate, wait_for_*).
    Skips validation if no page context / a11y tree is available.

    When a target's text/name isn't found in the a11y+HTML corpus, falls back
    to Playwright's ``get_by_text()`` / ``get_by_role()`` to check the full DOM
    (catches non-semantic elements like styled spans or bare <a> tags).
    """
    from app.worker.intent_schema import ActionType

    if not page_context:
        return None

    a11y_tree = page_context.get("a11y_tree", "")
    interactive_html = page_context.get("interactive_html", "")
    if not a11y_tree and not interactive_html:
        return None

    # Combine a11y + html into one search corpus (lowered for matching)
    corpus = (a11y_tree + "\n" + interactive_html).lower()

    # Actions that don't need a target on the page
    skip_actions = frozenset({
        ActionType.NAVIGATE,
        ActionType.WAIT_FOR_TEXT,
        ActionType.WAIT_FOR_NAVIGATION,
    })

    # Track whether a wait_for_text precedes an action — subsequent actions
    # target elements that aren't visible yet (e.g., sidebar submenu items
    # that appear after expanding).  Skip pre-validation for those.
    seen_wait_for_text = False

    missing = []
    for i, action in enumerate(intent.actions):
        if action.action == ActionType.WAIT_FOR_TEXT:
            seen_wait_for_text = True
        if action.action in skip_actions:
            continue
        if seen_wait_for_text:
            continue  # Element expected to appear after wait; skip check
        if not action.target or action.target.is_empty():
            continue

        # Build search terms from the target's non-null fields
        search_terms = []
        if action.target.test_id:
            search_terms.append(action.target.test_id.lower())
        if action.target.name:
            search_terms.append(action.target.name.lower())
        if action.target.label:
            search_terms.append(action.target.label.lower())
        if action.target.placeholder:
            search_terms.append(action.target.placeholder.lower())
        if action.target.text:
            search_terms.append(action.target.text.lower())

        if not search_terms:
            continue

        # At least one search term must appear in the page corpus
        found = any(term in corpus for term in search_terms)

        # Fallback: check the full DOM via Playwright when corpus misses
        # non-semantic elements (styled spans, bare <a>, divs with onclick)
        if not found and page is not None:
            for term in search_terms:
                try:
                    loc = page.get_by_text(term, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        found = True
                        logger.debug(
                            "structured.pre_validate_playwright_fallback",
                            action=i,
                            term=term,
                        )
                        break
                except Exception:
                    continue

        if not found:
            target_desc = ", ".join(
                f"{k}={v}" for k, v in action.target.model_dump(exclude_none=True).items()
            )
            missing.append(
                f"Action {i} ({action.action.value}): target ({target_desc}) "
                f"not found in accessibility tree or interactive HTML"
            )

    if not missing:
        return None

    return (
        "Pre-validation failed — some targets are not visible on the current page:\n"
        + "\n".join(f"  - {m}" for m in missing)
        + "\n\nRegenerate the intent using only elements visible in the "
        "accessibility tree and interactive HTML provided."
    )


_DATA_ENTRY_KEYWORDS = frozenset({
    "fill", "enter", "type", "input", "set", "provide", "specify",
})


def _filter_test_data_for_step(
    test_data: dict[str, str] | None,
    step_action: str,
) -> dict[str, str] | None:
    """Filter test_data to keys relevant to this step's action text.

    Reduces AI confusion by hiding irrelevant keys (e.g., password keys
    when the step is "Click Save").  If the step implies data entry but
    no keys match by name, returns all keys as fallback.
    """
    if not test_data or not step_action:
        return test_data

    action_lower = step_action.lower()
    filtered = {}
    for key in test_data:
        key_lower = key.lower()
        key_words = key_lower.replace("_", " ")
        if key_lower in action_lower or key_words in action_lower:
            filtered[key] = test_data[key]

    # Fallback: if step implies data entry but no keys matched, show all
    if not filtered and any(kw in action_lower for kw in _DATA_ENTRY_KEYWORDS):
        return test_data

    return filtered or None


def _targets_overlap(a, b) -> bool:
    """Check if two TargetDescriptors likely refer to the same element."""
    if a is None or b is None:
        return False
    if a.test_id and b.test_id:
        return a.test_id == b.test_id
    if a.label and b.label:
        return a.label.lower() == b.label.lower()
    if a.role and b.role and a.name and b.name:
        return a.role == b.role and a.name.lower() == b.name.lower()
    if a.placeholder and b.placeholder:
        return a.placeholder.lower() == b.placeholder.lower()
    return False


def _guard_intent_consistency(
    new_intent: StepIntent,
    original_intent: StepIntent,
    completed_count: int,
) -> StepIntent:
    """Fix regenerated intent if it contradicts completed actions.

    When an intent fails partway (e.g., 2 of 3 actions succeed), the
    regenerated intent should not repeat those actions with different
    values.  This guard detects conflicting values on the same target
    and silently fixes them to the original value.
    """
    if completed_count <= 0:
        return new_intent

    completed_originals = original_intent.actions[:completed_count]
    for orig in completed_originals:
        for new_act in new_intent.actions:
            if (
                new_act.action == orig.action
                and _targets_overlap(new_act.target, orig.target)
                and new_act.value != orig.value
                and new_act.value is not None
                and orig.value is not None
            ):
                logger.warning(
                    "structured.intent_consistency_fix",
                    action=orig.action.value,
                    original_value=orig.value[:50],
                    conflicting_value=new_act.value[:50],
                )
                new_act.value = orig.value
    return new_intent


# Regex to parse completed action count from enriched error messages
_COMPLETED_RE = _re.compile(r"\((\d+)/\d+ actions completed\)")


# --- A11y tree pruning by action type ---

_ACTION_ROLE_PRIORITY: dict[str, set[str]] = {
    "input": {"textbox", "searchbox", "spinbutton", "combobox", "textarea"},
    "click": {"button", "link", "menuitem", "treeitem", "tab", "switch"},
    "select": {"combobox", "listbox", "option", "radio", "radiogroup"},
    "check": {"checkbox", "switch"},
    "navigate": set(),
}

_ACTION_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "input": ("enter", "type", "fill", "input", "set", "provide"),
    "click": ("click", "press", "tap", "submit", "confirm", "dismiss"),
    "select": ("select", "choose", "pick", "dropdown"),
    "check": ("check", "uncheck", "toggle", "enable", "disable"),
    "navigate": ("navigate", "go to", "open", "visit"),
}


def _infer_action_category(step_action: str) -> str | None:
    """Infer the primary action category from a step description."""
    action_lower = step_action.lower()
    for category, keywords in _ACTION_CATEGORY_KEYWORDS.items():
        if any(kw in action_lower for kw in keywords):
            return category
    return None


def _hash_page_state(intent_json: str, page_url: str) -> str:
    """Hash intent + page URL for retry deduplication."""
    return hashlib.sha256(f"{intent_json}|{page_url}".encode()).hexdigest()[:16]


VISION_SYSTEM_PROMPT = """You are a QA test verification expert. You analyze screenshots of web pages to determine whether a test step's expected result has been achieved.

You will receive:
1. A screenshot of the current page state (after the action was executed)
2. The action that was performed
3. The expected result to verify

Respond with ONLY a JSON object (no markdown, no explanation) in this exact format:
{"status": "passed", "actual": "description of what is visible"}

Or if the expected result is NOT met:
{"status": "failed", "actual": "description of what is actually visible instead"}

IMPORTANT — Verification rules:
- ONLY evaluate whether the EXPECTED RESULT is met. Do NOT check whether the action's target element is still visible — after a click or navigation, the page state changes and the original element may no longer exist.
- If the expected result says "page displays X" and you can see X anywhere on the page, mark as PASSED.
- If the page shows the right content but with minor differences (different wording, extra elements, slightly different layout), mark as PASSED.
- If the action was "navigate to X" or "click X" and the page shows content related to X, mark as PASSED.
- If the page shows a loading spinner, "Loading...", "Verifying...", or similar transitional state, respond with {"status": "inconclusive", "actual": "Page is in a loading/transitional state — outcome not yet determined"}.
- Only mark as FAILED if the page clearly shows something COMPLETELY DIFFERENT from what was expected (e.g., an error page, still on the previous page with no change, or entirely wrong content).
- Only mark as PASSED if the expected result is clearly and definitively met.
- If uncertain, mark as "inconclusive" rather than guessing.

Be precise and factual. Describe only what you see."""


# --- Page analysis helpers ---


def _get_accessibility_tree(page: Page, max_chars: int = 3000, action_hint: str = "") -> str:
    """Extract the accessibility tree as a compact text representation.

    When action_hint is provided, prunes non-priority leaf nodes to reduce
    token usage and improve AI focus on relevant elements.
    """
    # Derive priority roles from action hint
    priority_roles: set[str] | None = None
    if action_hint:
        category = _infer_action_category(action_hint)
        if category and category in _ACTION_ROLE_PRIORITY:
            priority_roles = _ACTION_ROLE_PRIORITY[category]
            if not priority_roles:
                priority_roles = None  # navigate → no filtering

    try:
        snapshot = page.accessibility.snapshot()
        if not snapshot:
            return "(empty accessibility tree)"
        tree = _format_a11y_node(snapshot, depth=0, priority_roles=priority_roles)
        if len(tree) > max_chars:
            tree = tree[:max_chars] + "\n... (truncated)"
        return tree
    except Exception:
        return "(failed to capture accessibility tree)"


_STRUCTURAL_ROLES = frozenset({
    "generic", "paragraph", "group", "navigation", "main",
    "region", "form", "dialog", "tree", "list", "toolbar",
    "tablist", "menu", "menubar", "banner", "contentinfo",
})


def _format_a11y_node(
    node: dict, depth: int, max_depth: int = 8,
    priority_roles: set[str] | None = None,
) -> str:
    """Recursively format accessibility tree nodes, capped at max_depth.

    When priority_roles is set, non-priority leaf nodes (no children)
    are pruned to reduce tree size. Structural container roles are
    always kept since they may contain priority children.
    """
    if depth > max_depth:
        return ""

    indent = "  " * depth
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")
    checked = node.get("checked")
    disabled = node.get("disabled")
    expanded = node.get("expanded")

    # Prune non-priority leaf nodes when action-type filtering is active
    if (
        priority_roles is not None
        and role not in priority_roles
        and role not in _STRUCTURAL_ROLES
        and not node.get("children")
    ):
        return ""

    # Skip generic/noise nodes without useful info
    if role in ("generic", "paragraph", "StaticText") and not name:
        child_lines = []
        for child in node.get("children", []):
            child_text = _format_a11y_node(child, depth, max_depth, priority_roles)
            if child_text:
                child_lines.append(child_text)
        return "\n".join(child_lines)

    parts = [f"{indent}[{role}]"]
    if name:
        parts.append(f'"{name[:80]}"')
    if value:
        parts.append(f"value={value[:40]}")
    if checked is not None:
        parts.append(f"checked={checked}")
    if disabled:
        parts.append("DISABLED")
    if expanded is not None:
        parts.append(f"expanded={expanded}")

    line = " ".join(parts)
    lines = [line]

    for child in node.get("children", []):
        child_text = _format_a11y_node(child, depth + 1, max_depth, priority_roles)
        if child_text:
            lines.append(child_text)

    return "\n".join(lines)


def _get_interactive_html(page: Page) -> str:
    """Extract HTML of interactive elements (inputs, buttons, links, selects, etc.)."""
    try:
        html = page.evaluate("""() => {
            const selectors = [
                'input', 'textarea', 'select', 'button', 'a',
                '[role="button"]', '[role="checkbox"]', '[role="radio"]',
                '[role="combobox"]', '[role="listbox"]', '[role="option"]',
                '[role="tab"]', '[role="switch"]', '[role="menuitem"]',
                '[role="link"]',
                '[data-testid]', '[aria-label]',
                '[tabindex]:not([tabindex="-1"])',
                '[onclick]',
                'label'
            ];
            const elements = document.querySelectorAll(selectors.join(','));
            const results = [];
            const seen = new Set();
            for (const el of elements) {
                const tag = el.tagName.toLowerCase();
                const id = el.id ? ` id="${el.id}"` : '';
                const name = el.getAttribute('name') ? ` name="${el.getAttribute('name')}"` : '';
                const type = el.getAttribute('type') ? ` type="${el.getAttribute('type')}"` : '';
                const role = el.getAttribute('role') ? ` role="${el.getAttribute('role')}"` : '';
                const ariaLabel = el.getAttribute('aria-label') ? ` aria-label="${el.getAttribute('aria-label')}"` : '';
                const placeholder = el.getAttribute('placeholder') ? ` placeholder="${el.getAttribute('placeholder')}"` : '';
                const testid = el.getAttribute('data-testid') ? ` data-testid="${el.getAttribute('data-testid')}"` : '';
                const disabled = el.disabled ? ' disabled' : '';
                const text = el.textContent?.trim().substring(0, 80) || '';
                const className = el.className ? ` class="${String(el.className).substring(0, 60)}"` : '';

                const key = `${tag}${id}${name}${type}${role}${text}`;
                if (seen.has(key)) continue;
                seen.add(key);

                let repr = `<${tag}${id}${name}${type}${role}${ariaLabel}${placeholder}${testid}${disabled}${className}>`;
                if (text && tag !== 'textarea') repr += text.substring(0, 50);
                repr += `</${tag}>`;
                results.push(repr);
                if (results.length >= 80) break;
            }
            return results.join('\\n');
        }""")
        return html if html else "(no interactive elements found)"
    except Exception:
        return "(failed to extract interactive HTML)"


def _detect_visible_modal(page: Page) -> bool:
    """Check if a modal dialog is currently visible on the page."""
    try:
        dialogs = page.get_by_role("dialog")
        for i in range(dialogs.count()):
            if dialogs.nth(i).is_visible():
                return True
    except Exception:
        pass
    try:
        alert = resolve_single(page.get_by_role("alertdialog"), context="page_has_dialog:alertdialog")
        if alert.is_visible():
            return True
    except Exception:
        pass
    return False


def _capture_page_context(page: Page, action_hint: str = "") -> dict:
    """Capture page context: viewport screenshot, accessibility tree, and interactive HTML.

    When action_hint is provided, the a11y tree is pruned to prioritize
    roles relevant to the action type.
    """
    screenshot = None
    try:
        screenshot = page.screenshot(type="png", full_page=False)
    except Exception:
        pass

    a11y_tree = _get_accessibility_tree(page, action_hint=action_hint)
    interactive_html = _get_interactive_html(page)
    has_modal = _detect_visible_modal(page)

    return {
        "screenshot": screenshot,
        "a11y_tree": a11y_tree,
        "interactive_html": interactive_html,
        "has_modal": has_modal,
    }


# --- Vision verification ---


def verify_step_with_vision(
    client: OpenAI,
    screenshot_bytes: bytes,
    expected: str,
    action: str,
    model: str = "gpt-5-mini",
) -> dict:
    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    messages = [
        {"role": "system", "content": VISION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{screenshot_b64}",
                    },
                },
                {
                    "type": "text",
                    "text": f"Action performed: {action}\nExpected result: {expected}\n\nAnalyze the screenshot and verify if the expected result is achieved.",
                },
            ],
        },
    ]

    # Retry once on empty/non-JSON response (transient API issues)
    for attempt in range(2):
        response = client.chat.completions.create(
            model=model,
            max_completion_tokens=1024,
            messages=messages,
        )

        content = response.choices[0].message.content or ""
        text = _strip_code_fences(content.strip())

        if not text and attempt == 0:
            logger.warning("structured.vision_empty_response", attempt=attempt + 1)
            continue

        try:
            result = json.loads(text)
            return {
                "status": result.get("status", "failed"),
                "actual": result.get("actual", "Unable to parse verification result"),
            }
        except json.JSONDecodeError:
            # Try to extract JSON object from mixed text
            m = _re.search(r'\{[^{}]*"status"\s*:\s*"[^"]+?"[^{}]*\}', text)
            if m:
                try:
                    result = json.loads(m.group(0))
                    return {
                        "status": result.get("status", "failed"),
                        "actual": result.get("actual", "Unable to parse verification result"),
                    }
                except json.JSONDecodeError:
                    pass
            if attempt == 0:
                logger.warning("structured.vision_non_json_retry", attempt=attempt + 1, text=text[:100])
                continue
            return {
                "status": "failed",
                "actual": f"Vision verification returned non-JSON: {text[:200]}",
            }

    return {
        "status": "failed",
        "actual": "Vision verification returned empty response after retries",
    }


# --- Selector hint filtering ---


def _match_hints_to_actions(
    hints: list[dict],
    intent: StepIntent,
) -> dict[int, list[dict]]:
    """Match known-good selector hints to specific actions by target overlap.

    Selector hints are stored per-step, but multi-action intents need each
    action to only receive hints relevant to *its* target.  Without filtering,
    a hint for Action-1's target (e.g. "Continue" button) can be incorrectly
    applied to Action-0, causing the resolver to click the wrong element.

    Matching logic: a hint is relevant to an action if ANY of the hint's
    text-bearing params (name, text, label, placeholder) appear in (or contain)
    the action target's text-bearing fields.
    """
    if not hints or not intent.actions:
        return {}

    result: dict[int, list[dict]] = {}

    for i, action in enumerate(intent.actions):
        tgt = action.target
        if tgt is None:
            continue

        # Collect target's text-bearing values (lowered)
        target_texts = set()
        for val in (tgt.name, tgt.text, tgt.label, tgt.placeholder):
            if val:
                target_texts.add(val.strip().lower())

        target_role = (tgt.role or "").strip().lower()

        matched: list[dict] = []
        for hint in hints:
            params = hint.get("params", {})
            hint_texts = set()
            for key in ("name", "text", "label", "placeholder"):
                v = params.get(key, "")
                if v:
                    hint_texts.add(v.strip().lower())

            hint_role = params.get("role", "").strip().lower()

            # If both have roles and they differ, skip immediately
            if hint_role and target_role and hint_role != target_role:
                continue

            # Check for textual overlap (substring match in either direction)
            has_text_overlap = False
            for ht in hint_texts:
                for tt in target_texts:
                    if ht in tt or tt in ht:
                        has_text_overlap = True
                        break
                if has_text_overlap:
                    break

            # Match if text overlaps, or if no text on either side but roles match
            if has_text_overlap:
                matched.append(hint)
            elif not hint_texts and not target_texts and hint_role and hint_role == target_role:
                matched.append(hint)

        if matched:
            result[i] = matched

    return result


# --- Step execution ---


def execute_step(
    page: Page,
    ai_client: AIProvider | None,
    step: dict,
    org_id: str,
    test_run_id: str,
    s3_client,
    bucket: str,
    previous_actions: list[str],
    test_data: dict[str, str] | None = None,
    custom_code: str | None = None,
    model: str = "gpt-5-mini",
    tel: _StepTelemetryBuilder | None = None,
    selector_hints: list[dict] | None = None,
    intelligence_context: str | None = None,
    step_strategy: StepStrategy | None = None,
    cached_code: dict | None = None,
    execution_mode: str = "STANDARD",
    shared_context: SharedExecutionContext | None = None,
    selector_memory: InRunSelectorMemory | None = None,
    recovery_tracker: RecoveryEffectivenessTracker | None = None,
    last_step_selector: str | None = None,
) -> dict:
    """Execute a single test step using structured JSON intents.

    Flow:
    1. Look up execution mode config
    2. Check code cache for cached intent
    3. If no cache: capture page context → generate_step_intent()
    4. Execute: execute_intent() with unified wait budget
    5. On failure: retry (attempt 0 → increase wait, attempt 1 → regenerate)
    6. On success: validate via validate_step() (DOM → optional vision)
    7. Collect evidence, return result dict
    """
    # Legacy custom_code path removed — all execution uses intent engine
    if custom_code:
        logger.warning(
            "structured.custom_code_deprecated",
            step=step.get("step_number"),
            code_length=len(custom_code),
        )

    step_number = step["step_number"]
    # Lazy imports to avoid circular dependency
    from app.worker.intent_executor import execute_intent
    from app.worker.validation import validate_step
    from app.worker.execution_budget import StepExecutionBudget
    from app.worker.in_run_memory import InRunSelectorMemory, RecoveryEffectivenessTracker

    action = step.get("action", "")
    expected = step.get("expected", "")
    step_start = time.monotonic()

    # Capture pre-step URL for validation (detect if navigation occurred)
    try:
        pre_step_url = page.url
    except Exception:
        pre_step_url = ""

    # In-run adaptive memory (use provided run-scoped instances or create fresh)
    if selector_memory is None:
        selector_memory = InRunSelectorMemory()
    if recovery_tracker is None:
        recovery_tracker = RecoveryEffectivenessTracker()

    # Initialize strict budget enforcement
    budget = StepExecutionBudget(total_s=STEP_HARD_TIMEOUT_S)
    budget.start()

    # Track last selector for cross-step proximity propagation
    final_selector: str | None = None

    # Resolve execution mode config
    try:
        mode = ExecutionMode(execution_mode)
    except ValueError:
        mode = ExecutionMode.STANDARD
    mode_config = EXECUTION_MODE_CONFIG[mode]

    wait_budget_ms = mode_config["wait_budget_ms"]
    max_retries = mode_config["max_retries"]
    stability_wait_ms = mode_config["stability_wait_ms"]
    mode_vision_enabled = mode_config["vision_enabled"]

    # Apply strategy overrides
    if step_strategy:
        if step_strategy.wait_budget_multiplier > 1.0:
            wait_budget_ms = int(wait_budget_ms * step_strategy.wait_budget_multiplier)
        if step_strategy.retry_budget_override is not None:
            max_retries = step_strategy.retry_budget_override
        if step_strategy.preemptive_retry_enabled:
            max_retries = max(max_retries, 4)
        if step_strategy.selector_priority_override:
            selector_hints = step_strategy.selector_priority_override
        if step_strategy.aggressive_stability_mode:
            stability_wait_ms = max(stability_wait_ms, 3000)
        # Adaptive wait budget: use P95 * 1.5 as floor when historical data exists
        if step_strategy.timing_baseline_ms:
            adaptive_base_ms = int(step_strategy.timing_baseline_ms * 1.5)
            wait_budget_ms = min(max(wait_budget_ms, adaptive_base_ms), _STEP_HARD_CAP_MS)
    max_retries = min(max_retries, 5)

    gate = AIGate(budget=AICallBudget(allow_vision=mode_vision_enabled))

    evidence = EvidenceCollector()
    evidence.start_step(page, step_number)

    # Filter test_data for the prompt — AI only sees keys relevant to this step
    prompt_test_data = _filter_test_data_for_step(test_data, action)
    page_context: dict = {}  # populated lazily — may be set in AI gen block or pre-validate

    # custom_code always takes priority — it contains template tokens (e.g. ${timestamp})
    # that must be resolved fresh on every run. Never let the cache override it.
    intent = None
    is_cached = False

    # Quick Capture bypass: custom_code contains a pre-built StepIntent JSON
    # Skip AI generation entirely — the captured intent is already structured.
    if custom_code:
        try:
            intent = StepIntent.from_json_str(custom_code)
            is_cached = True  # treat as "cached" — no AI call needed
            if tel:
                tel.set_reused_code(True, "captured")
            logger.info(
                "structured.using_captured_intent",
                step=step_number,
            )
        except Exception:
            logger.warning(
                "structured.captured_intent_parse_failed",
                step=step_number,
            )
            # Fall through to cache / AI generation

    # No custom_code — check the code cache for a previously successful intent
    if intent is None and cached_code and cached_code.get("last_successful_code") and cached_code.get("code_type") == "intent":
        try:
            intent = StepIntent.from_json_str(cached_code["last_successful_code"])
            is_cached = True
            if tel:
                tel.set_reused_code(True, cached_code.get("code_hash", ""))
            logger.info(
                "structured.reusing_cached_intent",
                step=step_number,
                stability_score=cached_code.get("stability_score", 0),
            )
        except Exception:
            logger.warning("structured.cache_parse_failed", step=step_number)
            intent = None

    # Generate intent if not captured or cached
    if intent is None:
        ai_available = ai_client is not None and gate.can_call("intent_gen")

        if ai_available:
            budget.check_budget("before context capture")

            # Pre-capture stability wait
            with budget.phase("context_capture", max_pct=0.20):
                t0 = time.monotonic()
                stability = wait_for_page_ready(page, timeout_ms=stability_wait_ms)
                if tel:
                    tel.record_phase("pre_capture_stability", stability["total_ms"])

                page_context = _capture_page_context(page, action_hint=action)
                if tel:
                    tel.record_phase("context_capture", int((time.monotonic() - t0) * 1000))

            t0 = time.monotonic()
            try:
                intent, prompt_tokens, completion_tokens = ai_client.generate_intent(
                    action=action, step_number=step_number,
                    previous_actions=previous_actions,
                    page_context=page_context, model=model,
                    test_data=prompt_test_data, selector_hints=selector_hints,
                    intelligence_context=intelligence_context,
                )
                gate.record_call("intent_gen", model, prompt_tokens + completion_tokens)
                gen_ms = int((time.monotonic() - t0) * 1000)
                if tel:
                    tel.record_phase("code_generation", gen_ms)
                    tel.record_ai_call("intent_gen", model, gen_ms, prompt_tokens, completion_tokens)
            except Exception as e:
                gen_ms = int((time.monotonic() - t0) * 1000)
                if tel:
                    tel.record_phase("code_generation", gen_ms)
                    tel.record_ai_call("intent_gen", model, gen_ms, success=False, error=str(e)[:200])
                logger.warning(
                    "structured.ai_gen_failed",
                    step=step_number,
                    error=str(e)[:200],
                )
                # AI failed — will try heuristic below

        # If AI wasn't available or failed, try heuristic fallback
        if intent is None:
            from app.worker.heuristic_intent import generate_heuristic_intent

            if not ai_available:
                # Capture page context for heuristic (AI path captures it above)
                page_context = _capture_page_context(page)

            intent = generate_heuristic_intent(action, step_number, page_context)

            if intent is not None:
                logger.info(
                    "structured.heuristic_fallback",
                    step=step_number,
                    action_type=intent.actions[0].action.value,
                )
            else:
                reason = "AI unavailable" if not ai_available else "AI generation failed"
                duration = int((time.monotonic() - step_start) * 1000)
                return {
                    "step_number": step_number,
                    "status": "error",
                    "action": action,
                    "expected": expected,
                    "actual": "",
                    "error_message": f"{reason}, no cached intent, and step too complex for heuristic",
                    "evidence_url": "",
                    "generated_code": "",
                    "duration_ms": duration,
                    "verification_mode": "skipped",
                    "last_selector_path": None,
                    "was_cached": is_cached,
                }

    # Resolve ${key} template references from test_data
    _resolve_templates(intent, test_data)

    # Pre-validate intent targets against current page.
    # For cached intents, also run this check — if the primary target is
    # missing from the page the cache entry is stale (UI changed since it
    # was written) and we must regenerate rather than execute blindly.
    _pre_validate_regen_reason = "pre_validate_failed" if not is_cached else "cached_intent_stale"
    if ai_client is not None and gate.can_call("intent_regen"):
        if not page_context:
            try:
                page_context = _capture_page_context(page, action_hint=action)
            except Exception:
                page_context = {}
        validation_feedback = _pre_validate_intent(intent, page_context, page=page)
        if validation_feedback:
            if is_cached:
                logger.warning(
                    "structured.cached_intent_stale",
                    step=step_number,
                    feedback=validation_feedback[:300],
                )
                # Invalidate this cache entry so it isn't reused next run
                try:
                    from app.intelligence.code_reuse import invalidate_code_cache
                    invalidate_code_cache(
                        db=db,
                        test_case_id=test_case_id,
                        step_number=step_number,
                    )
                except Exception:
                    pass  # Non-fatal; stale cache will be overwritten on next successful run
                is_cached = False  # Treat remainder of this step as non-cached
            else:
                logger.warning(
                    "structured.pre_validate_failed",
                    step=step_number,
                    feedback=validation_feedback[:300],
                )
            # Regenerate with feedback about missing targets
            try:
                t_regen = time.monotonic()
                intent, p_tok, c_tok = ai_client.regenerate_intent(
                    action=action, step_number=step_number,
                    previous_actions=previous_actions,
                    error_context=validation_feedback,
                    page_context=page_context, model=model,
                    test_data=prompt_test_data,
                    selector_hints=selector_hints,
                    intelligence_context=intelligence_context,
                )
                _resolve_templates(intent, test_data)
                gate.record_call("intent_regen", model, p_tok + c_tok)
                regen_ms = int((time.monotonic() - t_regen) * 1000)
                if tel:
                    tel.set_regeneration_reason(_pre_validate_regen_reason)
                    tel.record_ai_call("intent_regen", model, regen_ms, p_tok, c_tok)
                logger.info(
                    "structured.pre_validate_regen",
                    step=step_number,
                    latency_ms=regen_ms,
                    was_cached=is_cached,
                )
            except Exception as e:
                logger.warning(
                    "structured.pre_validate_regen_failed",
                    step=step_number,
                    error=str(e)[:200],
                )
                # Continue with current intent — execution retry loop may fix it

    # Scale wait budget for multi-action intents (dropdowns, wizards, sidebar nav)
    action_count = len(intent.actions) if intent else 1
    if action_count >= 3:
        wait_budget_ms = min(int(wait_budget_ms * 1.5), _STEP_HARD_CAP_MS)

    intent_json = intent.to_json_str()
    if tel:
        tel.set_reused_code(is_cached, compute_code_hash(intent_json))

    # Execute with retry loop
    original_intent = intent  # Saved for consistency guard after regeneration
    last_error = None
    screenshot_bytes = None
    evidence_url = ""
    selectors_used: list[dict] = []
    prev_state_hash: str | None = None  # For retry deduplication
    heal_count = 0  # Track healing successes for result reporting
    final_attempt = 0  # Track which attempt we end on
    last_completed_count = 0  # Actions completed before last failure

    # Set execution deadline so safe_* wrappers respect step budget
    set_execution_deadline(time.monotonic() + budget.remaining_s())

    for attempt in range(max_retries):
        final_attempt = attempt
        # Hard timeout guard via budget
        if budget.is_exceeded():
            logger.warning(
                "structured.budget_exceeded",
                step=step_number,
                attempt=attempt + 1,
                breakdown=budget.phase_summary(),
            )
            last_error = last_error or TimeoutError(
                f"Step budget exceeded: {budget.phase_summary()}"
            )
            break

        # Cap Playwright timeouts to remaining budget
        budget.cap_playwright_timeout(page, max_ms=15000)

        t0 = time.monotonic()
        try:
            logger.info(
                "structured.exec",
                step=step_number,
                attempt=attempt + 1,
                actions=len(intent.actions),
                cached=is_cached,
            )

            # Build known-good selector hints for resolver (per-action filtered)
            known_good_for_resolver = None
            if selector_hints:
                structured_hints = [
                    h for h in selector_hints
                    if h.get("strategy") and h.get("params")
                ]
                if structured_hints:
                    known_good_for_resolver = _match_hints_to_actions(
                        structured_hints, intent,
                    )

            success, desc, selectors_used, stability_metrics, final_selector = execute_intent(
                page, intent, wait_budget_ms=wait_budget_ms,
                known_good_selectors=known_good_for_resolver,
                ai_client=ai_client,
                in_run_memory=selector_memory,
                shared_context=shared_context,
                last_step_selector=last_step_selector,
            )

            exec_ms = int((time.monotonic() - t0) * 1000)

            # Post-exec stability wait
            post_stability = wait_for_page_ready(page, timeout_ms=stability_wait_ms)
            if tel:
                tel.record_phase("post_exec_stability", post_stability["total_ms"])

            last_error = None
            _behavior_failures_now = stability_metrics.get("behavior_failures", 0)
            _behavior_signals_now = stability_metrics.get("behavior_signals") or []

            if tel:
                # Record stability metrics BEFORE record_attempt so they
                # attach to this attempt's ExecutionAttempt record
                tel.record_stability_metrics(
                    resolution_tier=stability_metrics.get("resolution_tier", 0),
                    guard_failures=stability_metrics.get("guard_failures"),
                    post_action_verified=stability_metrics.get("post_action_verified", True),
                    post_action_method=stability_metrics.get("post_action_method", ""),
                    behavior_failures=_behavior_failures_now,
                    interaction_attempts=stability_metrics.get("interaction_attempts", 0),
                    resolution_retries=stability_metrics.get("resolution_retries", 0),
                    behavior_signals=_behavior_signals_now,
                    mutation_total=stability_metrics.get("mutation_total", 0),
                    mutation_significant=stability_metrics.get("mutation_significant", False),
                )
                tel.record_attempt(
                    attempt + 1, True, intent_json, exec_ms,
                    selectors=selectors_used,
                )
                if attempt == 0:
                    tel.record_phase("code_execution", exec_ms)
                tel.record_disambiguation(
                    used=stability_metrics.get("disambiguation_used", False),
                    layer=stability_metrics.get("disambiguation_layer", ""),
                )
                tel.record_budget_breakdown(budget.get_breakdown())

            # --- Behavior-failure healing path ---
            # execute_intent() returned success=True but behavior detection
            # fired (DOM never changed).  This means the action was dispatched
            # but had no observable effect — the element was likely the wrong
            # one or was intercepted by an overlay.  Run the healing pipeline
            # here (same as the exception path) so we attempt a repair before
            # letting the no-behavior INCONCLUSIVE gate mark the step.
            if (
                _behavior_failures_now >= 1
                and not _behavior_signals_now  # no confirmed signals
                and attempt < max_retries - 1
                and not budget.is_exceeded()
                and budget.remaining_s() > 3.0
            ):
                from app.worker.recovery.diagnosis import diagnose_failure
                from app.worker.healing.pipeline import execute_healing

                _behavior_error = RuntimeError(
                    f"Behavior detection failure: action executed but no observable "
                    f"DOM effect detected after {_behavior_failures_now} attempt(s)"
                )
                _diag = diagnose_failure(
                    error_message=str(_behavior_error),
                    page=page,
                    timeout_budget_exceeded=False,
                )

                logger.info(
                    "structured.behavior_failure_healing",
                    step=step_number,
                    attempt=attempt + 1,
                    behavior_failures=_behavior_failures_now,
                )

                _healing_budget = int(budget.remaining_s() * 1000)
                if _healing_budget > 2000 and intent.actions:
                    _failed_action = intent.actions[-1] if intent.actions else None
                    if _failed_action is not None:
                        _healing_locator = None
                        _healing_pre_state = None
                        if _failed_action.target:
                            try:
                                from app.worker.selector_engine import resolve_target
                                from app.worker.post_action_verify import capture_pre_state

                                _resolved = resolve_target(
                                    page, _failed_action.target,
                                    timeout_ms=min(2000, _healing_budget // 4),
                                )
                                _healing_locator = _resolved.locator
                                _healing_pre_state = capture_pre_state(
                                    page, _healing_locator,
                                    _failed_action.action.value,
                                )
                            except Exception:
                                pass

                        _healing = execute_healing(
                            page, _failed_action, _behavior_error, _diag,
                            locator=_healing_locator,
                            ai_client=ai_client,
                            budget_remaining_ms=min(_healing_budget, 8000),
                            pre_state=_healing_pre_state,
                        )
                        if tel:
                            tel.record_healing_metrics(
                                healing_attempted=True,
                                healing_success=_healing.healed,
                                healing_layer_used=_healing.layer_used,
                                healing_strategy=_healing.strategy_used,
                                healing_elapsed_ms=_healing.elapsed_ms,
                                selector_repaired=(
                                    _healing.selector_repair_result is not None
                                    and _healing.selector_repair_result.repaired
                                ),
                                action_repaired=(
                                    _healing.action_repair_result is not None
                                    and _healing.action_repair_result.repaired
                                ),
                            )

                        if _healing.healed:
                            heal_count += 1
                            logger.info(
                                "structured.behavior_healing_success",
                                step=step_number,
                                layer=_healing.layer_used,
                                strategy=_healing.strategy_used,
                            )
                            if _healing.reconstructed_actions:
                                intent.actions = _healing.reconstructed_actions
                                intent_json = intent.to_json_str()
                            # Retry with healed intent
                            continue
                        else:
                            logger.info(
                                "structured.behavior_healing_failed",
                                step=step_number,
                            )
                # Healing not available or failed — fall through to validation
                # (the no-behavior INCONCLUSIVE gate will handle the step status)

            break

        except Exception as e:
            exec_ms = int((time.monotonic() - t0) * 1000)
            last_error = e

            # Parse completed action count from enriched error message
            m = _COMPLETED_RE.search(str(e))
            if m:
                last_completed_count = int(m.group(1))

            # --- Diagnosis-driven recovery ---
            from app.worker.recovery.diagnosis import diagnose_failure
            from app.worker.recovery.strategy_mapping import (
                execute_recovery_plan,
                plan_recovery,
            )

            diagnosis = diagnose_failure(
                error_message=str(e),
                page=page,
                timeout_budget_exceeded=budget.is_exceeded(),
            )

            # If deterministic diagnosis is low-confidence, try AI diagnosis
            if (
                diagnosis.confidence < 0.5
                and ai_client is not None
                and not budget.is_exceeded()
            ):
                try:
                    ai_diag = ai_client.diagnose_failure(
                        error_message=str(e), page=page,
                        page_state=diagnosis.page_state,
                    )
                    if ai_diag.confidence > diagnosis.confidence:
                        diagnosis = ai_diag
                except (AINotAvailable, Exception):
                    pass  # AI diagnosis is best-effort

            logger.warning(
                "structured.attempt_failed",
                step=step_number,
                attempt=attempt + 1,
                failure_type=diagnosis.failure_type.value,
                is_recoverable=diagnosis.is_recoverable,
                confidence=round(diagnosis.confidence, 2),
                error=str(e)[:200],
            )

            # Record failure diagnosis to telemetry
            if tel:
                try:
                    tel.record_failure_diagnosis({
                        "attempt": attempt + 1,
                        "failure_type": diagnosis.failure_type.value,
                        "is_recoverable": diagnosis.is_recoverable,
                        "confidence": round(diagnosis.confidence, 2),
                        "error": str(e)[:200],
                    })
                except Exception:
                    pass

            if not diagnosis.is_recoverable:
                if tel:
                    tel.record_attempt(attempt + 1, False, intent_json, exec_ms, str(e)[:500])
                    tel.record_budget_breakdown(budget.get_breakdown())
                break

            if attempt < max_retries - 1:
                # Plan and execute recovery actions
                recovery_plan = plan_recovery(diagnosis, attempt, max_retries)

                if recovery_plan.actions:
                    # Filter out ineffective recovery actions
                    ft = diagnosis.failure_type.value
                    effective_actions = [
                        a for a in recovery_plan.actions
                        if not recovery_tracker.should_skip(ft, a.action_type.value)
                    ]
                    if effective_actions:
                        recovery_plan.actions = effective_actions
                    recovery_results = execute_recovery_plan(page, recovery_plan)
                    # Record outcomes for future skipping decisions and telemetry
                    for rec_action, succeeded in recovery_results:
                        recovery_tracker.record(ft, rec_action.action_type.value, succeeded)
                        if tel:
                            try:
                                tel.record_recovery_action({
                                    "action_type": rec_action.action_type.value,
                                    "attempt": attempt + 1,
                                    "succeeded": succeeded,
                                    "failure_type": ft,
                                })
                            except Exception:
                                pass

                # Check time budget after recovery
                if budget.is_exceeded():
                    if tel:
                        tel.record_attempt(attempt + 1, False, intent_json, exec_ms, str(e)[:500])
                    break

                # Healing: targeted repair before full intent regeneration
                from app.worker.healing.pipeline import execute_healing

                healing_budget = int(budget.remaining_s() * 1000)
                if healing_budget > 2000 and intent.actions:
                    # Determine which action failed (last one attempted)
                    failed_action = intent.actions[-1] if intent.actions else None
                    if failed_action is not None:
                        # Try to re-resolve target for action repair (Layer 2)
                        healing_locator = None
                        healing_pre_state = None
                        if failed_action.target:
                            try:
                                from app.worker.selector_engine import resolve_target
                                from app.worker.post_action_verify import capture_pre_state

                                resolved = resolve_target(
                                    page, failed_action.target,
                                    timeout_ms=min(2000, healing_budget // 4),
                                )
                                healing_locator = resolved.locator
                                healing_pre_state = capture_pre_state(
                                    page, healing_locator,
                                    failed_action.action.value,
                                )
                            except Exception:
                                pass  # Layer 1 will produce its own locator

                        healing = execute_healing(
                            page, failed_action, e, diagnosis,
                            locator=healing_locator,
                            ai_client=ai_client,
                            budget_remaining_ms=min(healing_budget, 8000),
                            pre_state=healing_pre_state,
                        )
                        # Record healing telemetry regardless of outcome
                        if tel:
                            tel.record_healing_metrics(
                                healing_attempted=True,
                                healing_success=healing.healed,
                                healing_layer_used=healing.layer_used,
                                healing_strategy=healing.strategy_used,
                                healing_elapsed_ms=healing.elapsed_ms,
                                selector_repaired=(
                                    healing.selector_repair_result is not None
                                    and healing.selector_repair_result.repaired
                                ),
                                action_repaired=(
                                    healing.action_repair_result is not None
                                    and healing.action_repair_result.repaired
                                ),
                            )

                        if healing.healed:
                            heal_count += 1
                            logger.info(
                                "structured.healing_success",
                                step=step_number,
                                layer=healing.layer_used,
                                strategy=healing.strategy_used,
                            )
                            # Layer 4: replace intent actions with reconstructed sub-actions
                            if healing.reconstructed_actions:
                                intent.actions = healing.reconstructed_actions
                                intent_json = intent.to_json_str()
                                logger.info(
                                    "structured.intent_reconstructed",
                                    step=step_number,
                                    new_actions=len(healing.reconstructed_actions),
                                )
                            # Healing succeeded — retry (same intent for L1-3, new actions for L4)
                            if tel:
                                tel.record_attempt(
                                    attempt + 1, False, intent_json,
                                    exec_ms, str(e)[:500],
                                )
                            continue

                # Regenerate intent if recovery plan says so (typically attempt 1+)
                if recovery_plan.should_regenerate_intent:
                    if ai_client is None or not gate.can_call("intent_regen"):
                        logger.info(
                            "structured.regen_skipped",
                            step=step_number,
                            attempt=attempt + 1,
                            reason="no_ai" if ai_client is None else "budget_exceeded",
                        )
                        # No AI or budget exceeded — retry same intent with recovery actions applied
                    else:
                        if tel:
                            tel.set_regeneration_reason(
                                f"recovery:{diagnosis.failure_type.value}"
                            )

                        wait_for_page_ready(page, timeout_ms=stability_wait_ms)
                        retry_context = _capture_page_context(page, action_hint=action)

                        t_regen = time.monotonic()
                        try:
                            intent, p_tok, c_tok = ai_client.regenerate_intent(
                                action=action, step_number=step_number,
                                previous_actions=previous_actions,
                                error_context=str(e),
                                page_context=retry_context, model=model,
                                test_data=prompt_test_data,
                                selector_hints=selector_hints,
                                intelligence_context=intelligence_context,
                            )
                            _resolve_templates(intent, test_data)
                            # Guard: prevent regenerated intent from contradicting
                            # actions that already completed successfully
                            intent = _guard_intent_consistency(
                                intent, original_intent, last_completed_count,
                            )
                            gate.record_call("intent_regen", model, p_tok + c_tok)
                            intent_json = intent.to_json_str()
                            is_cached = False
                            regen_ms = int((time.monotonic() - t_regen) * 1000)
                            if tel:
                                tel.record_ai_call("intent_regen", model, regen_ms, p_tok, c_tok)
                        except Exception:
                            regen_ms = int((time.monotonic() - t_regen) * 1000)
                            if tel:
                                tel.record_ai_call("intent_regen", model, regen_ms, success=False)
                                tel.record_attempt(attempt + 1, False, intent_json, exec_ms, str(e)[:500])
                            break
                else:
                    # No regen needed — recovery actions fix page state, retry same intent
                    # Optionally increase wait budget for timeout-related failures
                    if diagnosis.failure_type.value in (
                        "navigation_timeout", "page_loading", "element_not_found",
                    ):
                        wait_budget_ms = min(int(wait_budget_ms * 1.3), _STEP_HARD_CAP_MS)

                    # State dedup: if page state is identical to previous attempt,
                    # skip sleep and force intent regeneration on next attempt
                    try:
                        current_url = page.url
                    except Exception:
                        current_url = ""
                    state_hash = _hash_page_state(intent_json, current_url)
                    if state_hash == prev_state_hash:
                        logger.info(
                            "structured.state_duplicate",
                            step=step_number,
                            attempt=attempt + 1,
                        )
                        # Skip backoff — escalate immediately
                    else:
                        backoff_s = _deterministic_backoff(attempt)
                        remaining = budget.remaining_s()
                        actual_backoff = min(backoff_s, remaining) if remaining > 0 else 0
                        if actual_backoff > 0:
                            time.sleep(actual_backoff)
                    prev_state_hash = state_hash

            if tel:
                tel.record_attempt(attempt + 1, False, intent_json, exec_ms, str(e)[:500])
                if attempt == 0:
                    tel.record_phase("code_execution", exec_ms)

    # Clear execution deadline — retry loop done
    set_execution_deadline(None)

    # Record AI gate metrics to telemetry
    if tel:
        tel.record_ai_gate_metrics(gate.get_metrics())

    # Handle failure
    if last_error is not None:
        try:
            screenshot_bytes = page.screenshot(type="png", full_page=False)
        except Exception:
            pass

        t0 = time.monotonic()
        step_evidence = evidence.finish_step(page, screenshot=screenshot_bytes)
        uploaded = upload_evidence_bundle(s3_client, bucket, org_id, test_run_id, step_evidence)
        evidence_url = uploaded.get("screenshot", "")
        if tel and screenshot_bytes:
            upload_ms = int((time.monotonic() - t0) * 1000)
            tel.record_evidence(len(screenshot_bytes), upload_ms)
            tel.record_phase("evidence_upload", upload_ms)

        duration = int((time.monotonic() - step_start) * 1000)
        return {
            "step_number": step_number,
            "status": "error",
            "action": action,
            "expected": expected,
            "actual": "",
            "error_message": f"Execution failed: {last_error}",
            "evidence_url": evidence_url,
            "generated_code": intent_json,
            "duration_ms": duration,
            "verification_mode": "skipped",
            "retry_count": final_attempt,
            "ai_heal_attempts": heal_count,
            "last_selector_path": final_selector,
            "was_cached": is_cached,
        }

    # Success — capture screenshot
    t0 = time.monotonic()
    try:
        screenshot_bytes = page.screenshot(type="png", full_page=False)
        if tel:
            tel.record_phase("screenshot", int((time.monotonic() - t0) * 1000))
    except Exception as e:
        if tel:
            tel.record_phase("screenshot", int((time.monotonic() - t0) * 1000))
        logger.warning("structured.screenshot_failed", step=step_number, error=str(e)[:200])
        evidence.finish_step(page)
        duration = int((time.monotonic() - step_start) * 1000)
        return {
            "step_number": step_number,
            "status": "passed",
            "action": action,
            "expected": expected,
            "actual": "Step executed successfully (screenshot unavailable)",
            "error_message": "",
            "evidence_url": "",
            "generated_code": intent_json,
            "duration_ms": duration,
            "verification_mode": "skipped",
            "retry_count": final_attempt,
            "ai_heal_attempts": heal_count,
            "last_selector_path": final_selector,
            "was_cached": is_cached,
        }

    # Upload evidence
    t0 = time.monotonic()
    step_evidence = evidence.finish_step(page, screenshot=screenshot_bytes)
    uploaded = upload_evidence_bundle(s3_client, bucket, org_id, test_run_id, step_evidence)
    evidence_url = uploaded.get("screenshot", "")
    if tel:
        upload_ms = int((time.monotonic() - t0) * 1000)
        tel.record_evidence(len(screenshot_bytes), upload_ms)
        tel.record_phase("evidence_upload", upload_ms)

    # Validation
    t0 = time.monotonic()
    status, actual, verification_mode = validate_step(
        page, intent, expected, screenshot_bytes,
        ai_client=ai_client,
        vision_enabled=mode_vision_enabled,
        model=model,
        pre_step_url=pre_step_url,
        step_action=action,
    )
    if tel:
        tel.record_phase("verification", int((time.monotonic() - t0) * 1000))

    # --- Behavior-based override ---
    # When vision says "failed" but the behavior detector confirmed
    # DOM mutations (e.g. wizard step transition), AND the vision's own
    # description of the page matches the expected keywords with high
    # confidence (≥0.90 ratio AND ≥3 matched words), trust the behavior
    # detector and override to passed.  Weak matches become INCONCLUSIVE
    # to avoid silent false positives on error pages with coincidental text.
    if (
        status == "failed"
        and verification_mode == "ai"
        and actual
    ):
        _had_mutation = (
            stability_metrics.get("mutation_significant", False)
            or stability_metrics.get("mutation_total", 0) > 0
            or "dom_mutation" in (stability_metrics.get("behavior_signals") or [])
        )
        if _had_mutation and expected:
            _exp_lower = expected.lower()
            _act_lower = actual.lower()

            # Extract significant words (3+ chars) from expected and check
            # keyword overlap with the vision actual description.
            _exp_words = [w for w in _re.findall(r'\b\w{3,}\b', _exp_lower) if w not in {
                'the', 'and', 'are', 'with', 'from', 'that', 'this', 'for',
                'has', 'have', 'was', 'were', 'will', 'been', 'page', 'displayed',
                'visible', 'shows', 'should', 'modal', 'step',
            }]
            if _exp_words:
                _match_count = sum(1 for w in _exp_words if w in _act_lower)
                _match_ratio = _match_count / len(_exp_words)
                # Check for explicit negation — keywords appearing in a negative context
                # (e.g. "there is no table", "no columns visible") must not override to passed.
                _negation_phrases = [
                    'there is no', 'there are no', 'not visible', 'not displayed',
                    'no table', 'no column', 'not shown', 'cannot be seen', 'is not shown',
                    'does not show', 'not present', 'no data', 'not found',
                ]
                _has_explicit_negation = any(phrase in _act_lower for phrase in _negation_phrases)
                # High-confidence override: ≥0.90 ratio AND at least 3 matched words AND no negation
                if _match_ratio >= 0.90 and _match_count >= 3 and not _has_explicit_negation:
                    logger.info(
                        "structured.behavior_override_passed",
                        step=step_number,
                        match_ratio=round(_match_ratio, 2),
                        matched=_match_count,
                        total=len(_exp_words),
                    )
                    status = "passed"
                    verification_mode = "behavior_override"
                elif _match_ratio >= 0.70:
                    # Partial match — action had effect but outcome is uncertain.
                    # Do NOT pass: keywords may appear on an error page or wrong page.
                    logger.info(
                        "structured.behavior_override_inconclusive",
                        step=step_number,
                        match_ratio=round(_match_ratio, 2),
                        matched=_match_count,
                        total=len(_exp_words),
                    )
                    status = "inconclusive"
                    verification_mode = "behavior_override"
                else:
                    logger.info(
                        "structured.behavior_override_skipped_low_match",
                        step=step_number,
                        match_ratio=round(_match_ratio, 2),
                        actual_snippet=_act_lower[:150],
                    )

    # --- No-behavior = INCONCLUSIVE gate ---
    # If the action executor reported repeated behavior failures (DOM never
    # changed after clicking), and vision passed or is inconclusive, the
    # action likely had no real effect. Downgrade to INCONCLUSIVE to prevent
    # silent false positives from being cached or counted as reliable passes.
    _behavior_failures = stability_metrics.get("behavior_failures", 0)
    _behavior_signals = stability_metrics.get("behavior_signals") or []
    if (
        status == "passed"
        and _behavior_failures >= 2
        and not _behavior_signals  # no confirmed signals from any attempt
    ):
        logger.info(
            "structured.no_behavior_inconclusive",
            step=step_number,
            behavior_failures=_behavior_failures,
        )
        status = "inconclusive"
        verification_mode = verification_mode or "no_behavior"
        actual = actual or "Action executed but no observable DOM effect detected"

    # --- Verification-driven retry ---
    # When vision says "failed" but execution succeeded, the cause is either:
    #   A) Timing — page hasn't finished loading/navigating yet
    #   B) Wrong element — resolver picked the wrong target
    # Phase 1: wait-and-revalidate (handles timing)
    # Phase 2: blacklist selectors and re-execute (handles wrong element)
    if (
        status == "failed"
        and verification_mode == "ai"
        and not budget.is_exceeded()
        and budget.remaining_s() > 5.0
    ):
        heal_count += 1

        # --- Phase 1: Wait for page to settle and re-validate ---
        # Many "failed" verifications are timing issues (screenshot taken
        # before navigation/render completes).  Wait and re-check first.
        logger.info(
            "structured.verification_revalidate",
            step=step_number,
        )
        try:
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            _budget_sleep(1.0, "revalidation_settle")

            reval_screenshot = page.screenshot(type="png", full_page=False)
            reval_status, reval_actual, reval_vmode = validate_step(
                page, intent, expected, reval_screenshot,
                ai_client=ai_client,
                vision_enabled=mode_vision_enabled,
                model=model,
                pre_step_url=pre_step_url,
                step_action=action,
            )

            if reval_status == "passed":
                logger.info("structured.verification_revalidate_success", step=step_number)
                status, actual, verification_mode = reval_status, reval_actual, reval_vmode
                # Upload updated evidence
                from app.worker.evidence_collector import StepEvidence
                reval_evidence = StepEvidence(
                    step_number=step_number, screenshot=reval_screenshot,
                )
                reval_uploaded = upload_evidence_bundle(
                    s3_client, bucket, org_id, test_run_id, reval_evidence,
                )
                evidence_url = reval_uploaded.get("screenshot", evidence_url)
            else:
                logger.info(
                    "structured.verification_revalidate_still_failed",
                    step=step_number,
                    reval_actual=reval_actual[:200] if reval_actual else "",
                )
        except Exception as e:
            logger.warning(
                "structured.verification_revalidate_error",
                step=step_number,
                error=str(e)[:200],
            )

        # --- Phase 2: Blacklist selectors and re-execute ---
        # If still failed after re-validation, the resolver likely picked
        # the wrong element.  Blacklist and try again.
        if (
            status == "failed"
            and not budget.is_exceeded()
            and budget.remaining_s() > 5.0
        ):
            logger.info(
                "structured.verification_retry",
                step=step_number,
                selectors_blacklisted=len(selectors_used),
            )

            for idx, sel_info in enumerate(selectors_used):
                sel_key = f"{sel_info.get('strategy', '')}:{sel_info.get('params', '')}"
                selector_memory.record_failure(idx, sel_key)
                selector_memory.record_failure(idx, sel_key)

            try:
                budget.cap_playwright_timeout(page, max_ms=15000)

                known_good_for_resolver = None
                if selector_hints:
                    structured_hints = [
                        h for h in selector_hints
                        if h.get("strategy") and h.get("params")
                    ]
                    if structured_hints:
                        known_good_for_resolver = _match_hints_to_actions(
                            structured_hints, intent,
                        )

                retry_success, retry_desc, retry_selectors, retry_metrics, final_selector = execute_intent(
                    page, intent, wait_budget_ms=int(budget.remaining_s() * 1000),
                    known_good_selectors=known_good_for_resolver,
                    ai_client=ai_client,
                    in_run_memory=selector_memory,
                    shared_context=shared_context,
                    last_step_selector=last_step_selector,
                )

                wait_for_page_ready(page, timeout_ms=stability_wait_ms)

                retry_screenshot = page.screenshot(type="png", full_page=False)

                retry_status, retry_actual, retry_vmode = validate_step(
                    page, intent, expected, retry_screenshot,
                    ai_client=ai_client,
                    vision_enabled=mode_vision_enabled,
                    model=model,
                    pre_step_url=pre_step_url,
                    step_action=action,
                )

                if retry_status == "passed":
                    logger.info("structured.verification_retry_success", step=step_number)
                    status, actual, verification_mode = retry_status, retry_actual, retry_vmode
                    selectors_used = retry_selectors
                    from app.worker.evidence_collector import StepEvidence
                    retry_evidence = StepEvidence(
                        step_number=step_number, screenshot=retry_screenshot,
                    )
                    retry_uploaded = upload_evidence_bundle(
                        s3_client, bucket, org_id, test_run_id, retry_evidence,
                    )
                    evidence_url = retry_uploaded.get("screenshot", evidence_url)
                else:
                    logger.info(
                        "structured.verification_retry_failed",
                        step=step_number,
                        retry_actual=retry_actual[:200] if retry_actual else "",
                    )

            except Exception as e:
                logger.warning(
                    "structured.verification_retry_error",
                    step=step_number,
                    error=str(e)[:200],
                )
                # Keep original failure result

    # Record final verification level to telemetry
    if tel:
        tel.record_verification_level(verification_mode or "skipped")

    duration = int((time.monotonic() - step_start) * 1000)
    return {
        "step_number": step_number,
        "status": status,
        "action": action,
        "expected": expected,
        "actual": actual,
        "error_message": (
            f"Verification failed: {actual[:200]}"
            if status == "failed" and actual
            else (
                f"Inconclusive: {actual[:200]}"
                if status == "inconclusive" and actual
                else ""
            )
        ),
        "evidence_url": evidence_url,
        "generated_code": intent_json,
        "duration_ms": duration,
        "verification_mode": verification_mode,
        "retry_count": final_attempt,
        "ai_heal_attempts": heal_count,
        "last_selector_path": final_selector,
        "was_cached": is_cached,
    }


# --- Utilities ---


def _strip_code_fences(code: str) -> str:
    """Remove markdown code fences if present."""
    if code.startswith("```"):
        lines = code.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        code = "\n".join(lines)
    return code
