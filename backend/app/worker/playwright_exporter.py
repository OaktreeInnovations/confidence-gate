"""Convert stored StepIntent JSON into executable Playwright TypeScript test code.

Each StepResult.generated_code is a JSON-serialised StepIntent produced by the
AI executor. This module translates those intents back into readable TypeScript
that can be dropped into a Playwright project and run directly.

Selector priority matches the runtime resolver:
  data-testid → role+name → label → placeholder → text → css
"""

from __future__ import annotations

import json
import re


# ---------------------------------------------------------------------------
# Locator helpers
# ---------------------------------------------------------------------------

def _js(value: str) -> str:
    """Return a double-quoted JS string literal, escaping inner quotes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _build_locator(target: dict, page_ref: str = "page") -> str:
    """Convert a TargetDescriptor dict to the best Playwright locator expression."""
    test_id = target.get("test_id")
    role = target.get("role")
    name = target.get("name")
    label = target.get("label")
    placeholder = target.get("placeholder")
    text = target.get("text")
    css = target.get("css")
    iframe = target.get("iframe")
    shadow_host = target.get("shadow_host")

    # Scope to iframe first
    if iframe:
        base = f'page.frameLocator({_js(iframe)})'
    else:
        base = page_ref

    # Build locator in priority order
    if test_id:
        locator = f'{base}.getByTestId({_js(test_id)})'
    elif role and name:
        locator = f'{base}.getByRole({_js(role)}, {{ name: {_js(name)} }})'
    elif role:
        locator = f'{base}.getByRole({_js(role)})'
    elif label:
        locator = f'{base}.getByLabel({_js(label)})'
    elif placeholder:
        locator = f'{base}.getByPlaceholder({_js(placeholder)})'
    elif text:
        locator = f'{base}.getByText({_js(text)})'
    elif css:
        locator = f'{base}.locator({_js(css)})'
    else:
        locator = f'{base}.locator("*")'

    # Scope into shadow DOM (best-effort — wrap in a filter)
    if shadow_host:
        locator = f'/* shadow host: {shadow_host} */ {locator}'

    return locator


# ---------------------------------------------------------------------------
# Action → TypeScript line
# ---------------------------------------------------------------------------

def _action_to_ts(action: dict, indent: str = "  ", override_css: str | None = None) -> str:
    """Return one or more TypeScript statement lines for a single ActionIntent.

    If override_css is provided it takes priority over the intent's target descriptor
    for all element-targeting actions — this gives a 1:1 match with the CSS selector
    that actually resolved and succeeded at runtime (last_selector_path).
    """
    action_type = action.get("action", "")
    target: dict = action.get("target") or {}
    value: str = action.get("value") or ""

    def loc() -> str:
        if override_css:
            return f'page.locator({_js(override_css)})'
        return _build_locator(target)

    if action_type == "navigate":
        return f"{indent}await page.goto({_js(value)});"

    elif action_type == "input":
        return f"{indent}await {loc()}.fill({_js(value)});"

    elif action_type == "click":
        return f"{indent}await {loc()}.click();"

    elif action_type == "select":
        return f"{indent}await {loc()}.selectOption({_js(value)});"

    elif action_type == "select_date":
        return f"{indent}await {loc()}.fill({_js(value)});  // date field"

    elif action_type == "upload_file":
        return f"{indent}await {loc()}.setInputFiles({_js(value)});  // replace with real file path"

    elif action_type == "check":
        return f"{indent}await {loc()}.check();"

    elif action_type == "uncheck":
        return f"{indent}await {loc()}.uncheck();"

    elif action_type == "wait_for_text":
        return f"{indent}await page.waitForSelector({_js('text=' + value)});"

    elif action_type == "wait_for_navigation":
        return f"{indent}await page.waitForURL('**/*');"

    elif action_type == "assert_text":
        return f"{indent}await expect({loc()}).toContainText({_js(value)});"

    elif action_type == "assert_visible":
        return f"{indent}await expect({loc()}).toBeVisible();"

    elif action_type == "assert_value":
        return f"{indent}await expect({loc()}).toHaveValue({_js(value)});"

    elif action_type == "extract_value":
        safe_var = "extracted" + str(action.get("step_number", ""))
        return f"{indent}const {safe_var} = await {loc()}.inputValue();"

    else:
        return f"{indent}// TODO: unsupported action '{action_type}'"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def step_results_to_playwright_ts(
    test_case_title: str,
    results: list[dict],
    base_url: str = "",
) -> str:
    """Generate a complete Playwright TypeScript spec from a list of StepResult dicts.

    Each result must have a 'generated_code' field containing StepIntent JSON.
    Steps without generated_code (e.g. skipped) are emitted as comments.
    """
    lines: list[str] = []

    # File header
    safe_title = re.sub(r"[^\w\s-]", "", test_case_title).strip()
    lines.append("import { test, expect } from '@playwright/test';")
    lines.append("")
    if base_url:
        lines.append(f"// Base URL: {base_url}")
    lines.append(f"// Generated by Confidence Gate — test case: {safe_title}")
    lines.append("")
    lines.append(f"test({_js(safe_title)}, async ({{ page }}) => {{")

    for result in sorted(results, key=lambda r: r.get("step_number", 0)):
        step_num = result.get("step_number", "?")
        generated_code = result.get("generated_code")
        step_status = result.get("status", "")
        error_msg = result.get("error_message", "")

        lines.append(f"  // ── Step {step_num}" + (f": {result.get('action', '')}" if result.get("action") else "") + " ──")

        if not generated_code:
            lines.append(f"  // (no intent recorded for step {step_num})")
            lines.append("")
            continue

        try:
            intent = json.loads(generated_code)
        except (json.JSONDecodeError, TypeError):
            lines.append(f"  // (could not parse intent for step {step_num})")
            lines.append("")
            continue

        description = intent.get("description", "")
        if description:
            lines.append(f"  // {description}")

        if step_status in ("failed", "error") and error_msg:
            lines.append(f"  // ⚠ This step failed during recording: {error_msg[:120]}")

        # Use the runtime-resolved CSS selector when available so the export is
        # a 1:1 match with the selector that actually succeeded at execution time.
        last_selector = result.get("last_selector_path") or None

        actions = intent.get("actions", [])
        for action in actions:
            ts_line = _action_to_ts(action, override_css=last_selector)
            lines.append(ts_line)

        lines.append("")

    lines.append("});")
    lines.append("")

    return "\n".join(lines)


def batch_results_to_playwright_ts(
    runs: list[dict],
    batch_name: str = "Batch",
) -> str:
    """Generate a multi-test Playwright spec from multiple test runs.

    Each run dict must have: test_case_title, results (list of StepResult dicts).
    """
    lines: list[str] = []
    lines.append("import { test, expect } from '@playwright/test';")
    lines.append("")
    lines.append(f"// Generated by Confidence Gate — batch: {batch_name}")
    lines.append("")

    for run in runs:
        title = run.get("test_case_title", "Unnamed Test")
        results = run.get("results", [])
        base_url = run.get("base_url", "")

        # Produce the test block for this run (strip the import/header)
        single = step_results_to_playwright_ts(title, results, base_url)
        # Skip the import + header lines (first 4 lines) and join the rest
        body_lines = single.splitlines()
        # Find where `test(` starts
        for i, l in enumerate(body_lines):
            if l.startswith("test("):
                lines.extend(body_lines[i:])
                break
        lines.append("")

    return "\n".join(lines)
