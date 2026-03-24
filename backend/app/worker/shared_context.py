"""SharedExecutionContext for mixed UI + API orchestration.

Provides a shared state object that flows between UI steps (Playwright)
and API steps (requests). Enables:
- Cookie sync: browser cookies extracted and injected into API requests
- Header forwarding: auth/CSRF headers shared across step types
- Variable extraction: values extracted from UI/API stored for later steps
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

logger = structlog.get_logger(__name__)


@dataclass
class SharedExecutionContext:
    """Shared state flowing between UI and API steps within a test run."""

    variables: dict[str, Any] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    csrf_token: str | None = None

    def sync_cookies_from_browser(self, context: BrowserContext) -> None:
        """Extract cookies from browser context for API step injection."""
        try:
            for cookie in context.cookies():
                self.cookies[cookie["name"]] = cookie["value"]
                # Auto-detect CSRF tokens
                name_lower = cookie["name"].lower()
                if "csrf" in name_lower or "xsrf" in name_lower:
                    self.csrf_token = cookie["value"]
            logger.debug(
                "shared_context.cookies_synced",
                count=len(self.cookies),
                has_csrf=self.csrf_token is not None,
            )
        except Exception as e:
            logger.warning("shared_context.cookie_sync_failed", error=str(e)[:200])

    def apply_to_session(self, session_kwargs: dict) -> None:
        """Inject shared cookies and headers into a requests.request() kwargs dict."""
        if self.cookies:
            session_kwargs.setdefault("cookies", {}).update(self.cookies)
        if self.headers:
            session_kwargs.setdefault("headers", {}).update(self.headers)
        if self.csrf_token:
            headers = session_kwargs.setdefault("headers", {})
            # Common CSRF header names
            if "X-CSRF-Token" not in headers and "X-XSRF-Token" not in headers:
                headers["X-CSRF-Token"] = self.csrf_token

    def set_variable(self, name: str, value: Any) -> None:
        """Store a variable for use by subsequent steps."""
        self.variables[name] = value
        logger.debug("shared_context.variable_set", name=name, value_type=type(value).__name__)

    def get_variable(self, name: str, default: Any = None) -> Any:
        """Retrieve a stored variable."""
        return self.variables.get(name, default)
