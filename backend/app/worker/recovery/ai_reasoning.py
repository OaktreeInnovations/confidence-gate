"""AI-based failure diagnosis — last resort when deterministic diagnosis is inconclusive.

Sends a targeted prompt with error message + screenshot + element metadata
to a cheap AI model for a structured diagnosis. This is NOT intent regeneration —
it only answers "what went wrong?" and "what should we try?".

Only invoked when:
- Deterministic diagnosis returns confidence < 0.5
- OR failure_type == UNKNOWN
- AND an AI client is available
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import structlog

from app.worker.recovery.diagnosis import (
    FailureDiagnosis,
    FailureType,
    PageState,
)

if TYPE_CHECKING:
    from openai import OpenAI
    from playwright.sync_api import Page

logger = structlog.get_logger(__name__)

_DIAGNOSIS_PROMPT = """\
You are a test automation failure diagnostician. A Playwright test step failed.
Analyze the error and page state to determine what went wrong.

Error message:
{error}

Page URL: {url}
Page title: {title}
Document ready state: {ready_state}

Respond with ONLY a JSON object:
{{
  "failure_type": "<one of: overlay_blocking, dialog_blocking, toast_blocking, \
element_not_found, element_not_visible, element_disabled, wrong_page, \
page_loading, network_error, stale_reference, assertion_failure, unknown>",
  "root_cause": "<1-2 sentence explanation>",
  "is_recoverable": true/false,
  "suggested_recovery": "<one of: dismiss_overlay, dismiss_dialog, dismiss_toast, \
scroll_to_element, wait_and_retry, wait_for_enabled, re_resolve_and_retry, \
navigate_back, refresh_page, none>",
  "confidence": 0.0-1.0
}}
"""

# Map AI response strings to FailureType enum
_TYPE_MAP: dict[str, FailureType] = {
    "overlay_blocking": FailureType.OVERLAY_BLOCKING,
    "dialog_blocking": FailureType.DIALOG_BLOCKING,
    "toast_blocking": FailureType.TOAST_BLOCKING,
    "element_not_found": FailureType.ELEMENT_NOT_FOUND,
    "element_not_visible": FailureType.ELEMENT_NOT_VISIBLE,
    "element_disabled": FailureType.ELEMENT_DISABLED,
    "wrong_page": FailureType.WRONG_PAGE,
    "page_loading": FailureType.PAGE_LOADING,
    "network_error": FailureType.NETWORK_ERROR,
    "stale_reference": FailureType.STALE_REFERENCE,
    "assertion_failure": FailureType.ASSERTION_FAILURE,
    "unknown": FailureType.UNKNOWN,
}


def ai_diagnose_failure(
    ai_client: OpenAI,
    error_message: str,
    page: Page | None = None,
    page_state: PageState | None = None,
    model: str = "gpt-5-mini",
) -> FailureDiagnosis:
    """Use AI to diagnose an ambiguous failure.

    Sends error + optional screenshot to a cheap model for classification.
    Returns a FailureDiagnosis with diagnosis_source="ai".

    Args:
        ai_client: OpenAI client instance.
        error_message: The exception string.
        page: Playwright Page for screenshot (optional).
        page_state: Pre-captured page state (optional).
        model: Model to use (default: gpt-5-mini for cost).

    Returns:
        FailureDiagnosis from AI analysis.
    """
    ps = page_state or PageState()

    prompt = _DIAGNOSIS_PROMPT.format(
        error=error_message[:500],
        url=ps.url,
        title=ps.title,
        ready_state=ps.document_ready_state,
    )

    messages: list[dict] = []

    # Include screenshot if page available
    if page is not None:
        try:
            screenshot = page.screenshot(
                type="jpeg", quality=40, full_page=False,
            )
            b64 = base64.b64encode(screenshot).decode()
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            })
        except Exception:
            messages.append({"role": "user", "content": prompt})
    else:
        messages.append({"role": "user", "content": prompt})

    try:
        response = ai_client.chat.completions.create(
            model=model,
            max_completion_tokens=256,
            messages=messages,
        )
        raw = response.choices[0].message.content.strip()
        return _parse_ai_diagnosis(raw, page_state=ps)

    except Exception as e:
        logger.warning("ai_diagnosis.failed", error=str(e)[:200])
        return FailureDiagnosis(
            failure_type=FailureType.UNKNOWN,
            root_cause=f"AI diagnosis failed: {str(e)[:100]}",
            is_recoverable=True,
            suggested_recovery="wait_and_retry",
            confidence=0.1,
            diagnosis_source="ai",
            page_state=ps,
        )


def _parse_ai_diagnosis(raw: str, page_state: PageState | None = None) -> FailureDiagnosis:
    """Parse AI response JSON into a FailureDiagnosis."""
    import json

    # Strip markdown fences
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)

    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError:
        return FailureDiagnosis(
            failure_type=FailureType.UNKNOWN,
            root_cause=f"AI returned unparseable response: {raw[:200]}",
            is_recoverable=True,
            suggested_recovery="wait_and_retry",
            confidence=0.1,
            diagnosis_source="ai",
            page_state=page_state,
        )

    failure_type = _TYPE_MAP.get(
        data.get("failure_type", "unknown"),
        FailureType.UNKNOWN,
    )

    confidence = data.get("confidence", 0.5)
    if not isinstance(confidence, (int, float)):
        confidence = 0.5
    confidence = max(0.0, min(1.0, float(confidence)))

    return FailureDiagnosis(
        failure_type=failure_type,
        root_cause=str(data.get("root_cause", "Unknown"))[:300],
        is_recoverable=bool(data.get("is_recoverable", True)),
        suggested_recovery=str(data.get("suggested_recovery", "wait_and_retry")),
        confidence=confidence,
        diagnosis_source="ai",
        page_state=page_state,
    )
