"""Deterministic failure diagnosis engine.

Analyzes an execution error + current page state to produce a structured
FailureDiagnosis explaining WHY a step failed and whether recovery is possible.

Two-phase approach:
1. Error pattern matching — classify the exception string (fast, no page access)
2. Page state inspection — check for overlays, dialogs, navigation state (needs page)

Phase 2 only runs when Phase 1 produces an ambiguous or recoverable result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Detection selectors — ordered generic-first, framework-specific last
# ---------------------------------------------------------------------------

_OVERLAY_SELECTORS = [
    '.fixed.inset-0',
    '[class*="overlay"]',
    '[class*="backdrop"]',
    '[class*="modal-backdrop"]',
    # Framework-specific
    '[data-radix-popper-content-wrapper]',
    '.ReactModal__Overlay',
]

_TOAST_SELECTORS = [
    '[role="status"]',
    '[role="alert"]',
    '[class*="toast"]',
    '[class*="notification"]',
    '[class*="snackbar"]',
    # Framework-specific
    '[class*="Toastify"]',
    '[data-sonner-toast]',
]


class FailureType(StrEnum):
    """Classified failure types — more granular than RetryReason."""

    # Selector issues
    ELEMENT_NOT_FOUND = "element_not_found"
    ELEMENT_NOT_VISIBLE = "element_not_visible"
    ELEMENT_DISABLED = "element_disabled"
    ELEMENT_DETACHED = "element_detached"
    SELECTOR_AMBIGUOUS = "selector_ambiguous"

    # Blocking / overlay
    OVERLAY_BLOCKING = "overlay_blocking"
    DIALOG_BLOCKING = "dialog_blocking"
    TOAST_BLOCKING = "toast_blocking"

    # Navigation / page state
    NAVIGATION_TIMEOUT = "navigation_timeout"
    PAGE_CRASH = "page_crash"
    WRONG_PAGE = "wrong_page"
    PAGE_LOADING = "page_loading"

    # Network
    NETWORK_ERROR = "network_error"
    API_ERROR = "api_error"

    # Framework / execution
    STALE_REFERENCE = "stale_reference"
    FRAME_DETACHED = "frame_detached"
    JS_ERROR = "js_error"
    ASSERTION_FAILURE = "assertion_failure"

    # Budget
    TIMEOUT_BUDGET_EXCEEDED = "timeout_budget_exceeded"

    # Unknown
    UNKNOWN = "unknown"


@dataclass
class PageState:
    """Snapshot of relevant page state at failure time."""

    url: str = ""
    title: str = ""
    has_overlay: bool = False
    has_dialog: bool = False
    has_toast: bool = False
    is_loading: bool = False
    has_error_banner: bool = False
    visible_dialogs: list[str] = field(default_factory=list)
    overlay_selector: str = ""
    document_ready_state: str = ""


@dataclass
class FailureDiagnosis:
    """Structured diagnosis of a step execution failure."""

    failure_type: FailureType
    root_cause: str
    is_recoverable: bool
    suggested_recovery: str = ""
    page_state: PageState | None = None
    confidence: float = 1.0  # 0.0–1.0, how sure we are of this diagnosis
    diagnosis_source: str = "deterministic"  # "deterministic" or "ai"

    def to_dict(self) -> dict:
        return {
            "failure_type": self.failure_type.value,
            "root_cause": self.root_cause,
            "is_recoverable": self.is_recoverable,
            "suggested_recovery": self.suggested_recovery,
            "confidence": round(self.confidence, 2),
            "diagnosis_source": self.diagnosis_source,
            "page_state": {
                "url": self.page_state.url,
                "has_overlay": self.page_state.has_overlay,
                "has_dialog": self.page_state.has_dialog,
                "has_toast": self.page_state.has_toast,
                "is_loading": self.page_state.is_loading,
            } if self.page_state else None,
        }


def diagnose_failure(
    error_message: str,
    page: Page | None = None,
    expected_url: str | None = None,
    timeout_budget_exceeded: bool = False,
) -> FailureDiagnosis:
    """Diagnose an execution failure from error + page state.

    Phase 1: Classify error string (always runs).
    Phase 2: Inspect page state (only if page provided and Phase 1 is ambiguous).

    Args:
        error_message: Exception string from the failed execution.
        page: Playwright Page (optional — enables page state inspection).
        expected_url: If set, checks for wrong-page condition.
        timeout_budget_exceeded: True if the step's time budget was exhausted.

    Returns:
        FailureDiagnosis with type, cause, recoverability, and suggested recovery.
    """
    if timeout_budget_exceeded:
        return FailureDiagnosis(
            failure_type=FailureType.TIMEOUT_BUDGET_EXCEEDED,
            root_cause="Step execution budget exhausted",
            is_recoverable=False,
            suggested_recovery="none",
            confidence=1.0,
        )

    # Phase 1: Error pattern matching
    diagnosis = _classify_error_string(error_message)

    # Phase 2: Page state inspection (enriches Phase 1 when possible)
    if page is not None:
        page_state = _inspect_page_state(page)
        diagnosis.page_state = page_state

        # Overlay detection can override Phase 1 diagnosis
        # Only override specific types where an overlay is the likely cause.
        # Do NOT override UNKNOWN — let it go through AI diagnosis instead.
        if page_state.has_overlay and diagnosis.failure_type in (
            FailureType.ELEMENT_NOT_VISIBLE,
            FailureType.NAVIGATION_TIMEOUT,
        ):
            diagnosis.failure_type = FailureType.OVERLAY_BLOCKING
            diagnosis.root_cause = f"Overlay blocking interaction: {page_state.overlay_selector}"
            diagnosis.is_recoverable = True
            diagnosis.suggested_recovery = "dismiss_overlay"
            diagnosis.confidence = 0.9

        # Dialog detection
        if page_state.has_dialog and diagnosis.failure_type in (
            FailureType.ELEMENT_NOT_VISIBLE,
        ):
            dialog_desc = ", ".join(page_state.visible_dialogs[:3])
            diagnosis.failure_type = FailureType.DIALOG_BLOCKING
            diagnosis.root_cause = f"Dialog blocking: {dialog_desc}"
            diagnosis.is_recoverable = True
            diagnosis.suggested_recovery = "dismiss_dialog"
            diagnosis.confidence = 0.85

        # Toast/notification detection
        if page_state.has_toast and diagnosis.failure_type == FailureType.OVERLAY_BLOCKING:
            diagnosis.failure_type = FailureType.TOAST_BLOCKING
            diagnosis.root_cause = "Toast notification blocking interaction"
            diagnosis.is_recoverable = True
            diagnosis.suggested_recovery = "dismiss_toast"
            diagnosis.confidence = 0.85

        # Wrong page detection
        if expected_url and page_state.url:
            if not _urls_match(page_state.url, expected_url):
                if diagnosis.failure_type in (
                    FailureType.ELEMENT_NOT_FOUND,
                    FailureType.UNKNOWN,
                ):
                    diagnosis.failure_type = FailureType.WRONG_PAGE
                    diagnosis.root_cause = (
                        f"Page URL mismatch: expected '{expected_url}', "
                        f"got '{page_state.url}'"
                    )
                    diagnosis.is_recoverable = True
                    diagnosis.suggested_recovery = "navigate_back"
                    diagnosis.confidence = 0.9

        # Loading state detection
        if page_state.is_loading and diagnosis.failure_type in (
            FailureType.ELEMENT_NOT_FOUND,
            FailureType.NAVIGATION_TIMEOUT,
        ):
            diagnosis.failure_type = FailureType.PAGE_LOADING
            diagnosis.root_cause = (
                f"Page still loading (readyState={page_state.document_ready_state})"
            )
            diagnosis.is_recoverable = True
            diagnosis.suggested_recovery = "wait_and_retry"
            diagnosis.confidence = 0.8

    logger.info(
        "diagnosis.result",
        failure_type=diagnosis.failure_type.value,
        is_recoverable=diagnosis.is_recoverable,
        confidence=diagnosis.confidence,
        source=diagnosis.diagnosis_source,
    )

    return diagnosis


# --- Phase 1: Error pattern classification ---

def _classify_error_string(error_message: str) -> FailureDiagnosis:
    """Classify failure from error message alone (no page access needed)."""
    msg = error_message.lower()

    # Overlay / pointer interception (most specific first)
    if "intercepts pointer events" in msg or "other element would receive" in msg:
        return FailureDiagnosis(
            failure_type=FailureType.OVERLAY_BLOCKING,
            root_cause=_extract_blocking_element(error_message),
            is_recoverable=True,
            suggested_recovery="dismiss_overlay",
            confidence=0.95,
        )

    # Strict mode / ambiguous selector
    if any(kw in msg for kw in ("strict mode violation", "resolved to", "multiple elements")):
        return FailureDiagnosis(
            failure_type=FailureType.SELECTOR_AMBIGUOUS,
            root_cause="Multiple elements match the selector",
            is_recoverable=True,
            suggested_recovery="disambiguate_selector",
            confidence=0.95,
        )

    # Element detached (stale reference)
    if "element is detached" in msg or "element handle refers to" in msg:
        return FailureDiagnosis(
            failure_type=FailureType.ELEMENT_DETACHED,
            root_cause="Element was removed from DOM after location",
            is_recoverable=True,
            suggested_recovery="re_resolve_and_retry",
            confidence=0.95,
        )

    # Element not visible
    if any(kw in msg for kw in ("not visible", "is hidden", "not in the viewport")):
        return FailureDiagnosis(
            failure_type=FailureType.ELEMENT_NOT_VISIBLE,
            root_cause="Element exists but is not visible",
            is_recoverable=True,
            suggested_recovery="scroll_to_element",
            confidence=0.85,
        )

    # Element disabled
    if "element is disabled" in msg or "aria-disabled" in msg:
        return FailureDiagnosis(
            failure_type=FailureType.ELEMENT_DISABLED,
            root_cause="Target element is disabled",
            is_recoverable=True,
            suggested_recovery="wait_for_enabled",
            confidence=0.9,
        )

    # Element not found
    if any(kw in msg for kw in (
        "waiting for selector", "locator resolved to", "no element found",
        "waiting for locator", "could not find", "all selector strategies failed",
        "found no candidates",  # selector_engine resolver
    )):
        return FailureDiagnosis(
            failure_type=FailureType.ELEMENT_NOT_FOUND,
            root_cause="Target element not found in DOM",
            is_recoverable=True,
            suggested_recovery="re_resolve_and_retry",
            confidence=0.85,
        )

    # Locator visibility timeout (distinct from navigation timeout)
    # "Locator.wait_for: Timeout 15000ms exceeded... waiting for get_by_text(...) to be visible"
    if ("locator" in msg or "get_by_" in msg or "to be visible" in msg) and "timeout" in msg:
        return FailureDiagnosis(
            failure_type=FailureType.ELEMENT_NOT_FOUND,
            root_cause="Element not found within timeout (locator wait)",
            is_recoverable=True,
            suggested_recovery="re_resolve_and_retry",
            confidence=0.85,
        )

    # Page crash
    if "page crash" in msg or "target closed" in msg:
        return FailureDiagnosis(
            failure_type=FailureType.PAGE_CRASH,
            root_cause="Browser page crashed",
            is_recoverable=False,
            confidence=1.0,
        )

    # Frame detached
    if "frame was detached" in msg or "frame detached" in msg:
        return FailureDiagnosis(
            failure_type=FailureType.FRAME_DETACHED,
            root_cause="Frame was detached from page",
            is_recoverable=False,
            confidence=1.0,
        )

    # Network error
    if any(kw in msg for kw in ("net::", "err_connection", "err_name", "dns")):
        return FailureDiagnosis(
            failure_type=FailureType.NETWORK_ERROR,
            root_cause="Network connectivity error",
            is_recoverable=True,
            suggested_recovery="wait_and_retry",
            confidence=0.9,
        )

    # Timeout
    if "timeout" in msg and "exceeded" in msg:
        return FailureDiagnosis(
            failure_type=FailureType.NAVIGATION_TIMEOUT,
            root_cause="Operation timed out",
            is_recoverable=True,
            suggested_recovery="wait_and_retry",
            confidence=0.7,
        )

    # JS error
    if any(kw in msg for kw in ("evaluation failed", "javascript error", "syntax error")):
        return FailureDiagnosis(
            failure_type=FailureType.JS_ERROR,
            root_cause="JavaScript execution error",
            is_recoverable=True,
            suggested_recovery="re_resolve_and_retry",
            confidence=0.8,
        )

    # Assertion failure
    if any(kw in msg for kw in ("assert", "expect", "verification")):
        return FailureDiagnosis(
            failure_type=FailureType.ASSERTION_FAILURE,
            root_cause="Step assertion did not pass",
            is_recoverable=False,
            suggested_recovery="none",
            confidence=0.8,
        )

    # Unknown
    return FailureDiagnosis(
        failure_type=FailureType.UNKNOWN,
        root_cause=f"Unclassified error: {error_message[:200]}",
        is_recoverable=True,
        suggested_recovery="ai_diagnose",
        confidence=0.3,
    )


# --- Phase 2: Page state inspection ---

def _build_page_state_js() -> str:
    """Build the page state inspection JS with selectors from Python constants."""
    import json
    overlay_json = json.dumps(_OVERLAY_SELECTORS)
    toast_json = json.dumps(_TOAST_SELECTORS)
    return f"""() => {{
    const state = {{
        url: window.location.href,
        title: document.title,
        readyState: document.readyState,
        isLoading: document.readyState !== 'complete',
        hasOverlay: false,
        overlaySelector: '',
        hasDialog: false,
        visibleDialogs: [],
        hasToast: false,
        hasErrorBanner: false,
    }};

    // Check for overlays (fixed/absolute positioned elements covering viewport)
    const overlaySelectors = {overlay_json};
    for (const sel of overlaySelectors) {{
        const els = document.querySelectorAll(sel);
        for (const el of els) {{
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            const visible = style.display !== 'none'
                && style.visibility !== 'hidden'
                && parseFloat(style.opacity || '1') > 0.1;
            const coversViewport = rect.width > window.innerWidth * 0.5
                && rect.height > window.innerHeight * 0.3;
            if (visible && coversViewport) {{
                state.hasOverlay = true;
                state.overlaySelector = sel;
                break;
            }}
        }}
        if (state.hasOverlay) break;
    }}

    // Check for visible dialogs
    const dialogs = document.querySelectorAll(
        '[role="dialog"], [role="alertdialog"], dialog[open]'
    );
    for (const d of dialogs) {{
        const style = window.getComputedStyle(d);
        if (style.display !== 'none' && style.visibility !== 'hidden') {{
            state.hasDialog = true;
            const title = d.getAttribute('aria-label')
                || d.querySelector('[role="heading"]')?.textContent
                || d.querySelector('h1,h2,h3')?.textContent
                || 'Untitled dialog';
            state.visibleDialogs.push(title.trim().substring(0, 80));
        }}
    }}

    // Check for toast notifications
    const toastSelectors = {toast_json};
    for (const sel of toastSelectors) {{
        const el = document.querySelector(sel);
        if (el) {{
            const rect = el.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {{
                state.hasToast = true;
                break;
            }}
        }}
    }}

    // Check for error banners
    const errorSelectors = [
        '[role="alert"]',
        '[class*="error-banner"]',
        '[class*="error-message"]',
        '.alert-danger',
        '.bg-destructive',
    ];
    for (const sel of errorSelectors) {{
        const el = document.querySelector(sel);
        if (el) {{
            const rect = el.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {{
                state.hasErrorBanner = true;
                break;
            }}
        }}
    }}

    return state;
}}"""


_PAGE_STATE_JS = _build_page_state_js()


def _inspect_page_state(page: Page) -> PageState:
    """Inspect current page for overlays, dialogs, toasts, and loading state."""
    try:
        state = page.evaluate(_PAGE_STATE_JS)
        return PageState(
            url=state.get("url", ""),
            title=state.get("title", ""),
            has_overlay=state.get("hasOverlay", False),
            has_dialog=state.get("hasDialog", False),
            has_toast=state.get("hasToast", False),
            is_loading=state.get("isLoading", False),
            has_error_banner=state.get("hasErrorBanner", False),
            visible_dialogs=state.get("visibleDialogs", []),
            overlay_selector=state.get("overlaySelector", ""),
            document_ready_state=state.get("readyState", ""),
        )
    except Exception as e:
        logger.warning("diagnosis.page_state_failed", error=str(e)[:200])
        return PageState()


def _extract_blocking_element(error_message: str) -> str:
    """Extract the blocking element info from a Playwright interception error."""
    # Playwright error format: "<element> intercepts pointer events"
    # or "...other element <div class='overlay'> would receive the click"
    msg = error_message
    if "intercepts pointer events" in msg:
        # Extract element description before "intercepts"
        idx = msg.find("intercepts pointer events")
        return f"Element blocking: {msg[max(0, idx - 100):idx].strip()}"
    if "would receive" in msg:
        idx = msg.find("would receive")
        return f"Element blocking: {msg[max(0, idx - 100):idx].strip()}"
    return "Unknown blocking element"


def _urls_match(actual: str, expected: str) -> bool:
    """Flexible URL comparison — ignores trailing slash and query params."""
    from urllib.parse import urlparse
    a = urlparse(actual)
    e = urlparse(expected)
    return a.path.rstrip("/") == e.path.rstrip("/")
