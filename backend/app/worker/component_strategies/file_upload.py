"""File upload interaction strategy."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from app.worker.component_helpers import resolve_single
from app.worker.execution_budget import budget_wait

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


def upload_file(
    page: Page,
    file_path: str,
    trigger: Locator | None = None,
    *,
    input_selector: str = "input[type='file']",
) -> None:
    """Upload a file through a file input element.

    Handles both visible file inputs and hidden inputs triggered by a button.

    Args:
        page: Playwright Page (or Locator — auto-extracts .page for modal compat).
        file_path: Absolute path to the file to upload.
        trigger: Optional button/link that opens the file dialog.
                 If provided, handles the filechooser event.
        input_selector: CSS selector for the file input (used when no trigger).
    """
    # Support being called from interact_in_modal where first arg is a Locator
    if not hasattr(page, "goto"):
        page = page.page

    if trigger:
        # Click the trigger and handle the filechooser event
        with page.expect_file_chooser(timeout=10000) as fc_info:
            trigger.click()
        file_chooser = fc_info.value
        file_chooser.set_files(file_path)
    else:
        # Direct set on the file input (works even if hidden)
        file_input = resolve_single(page.locator(input_selector), context="upload_file:file_input")
        file_input.set_input_files(file_path)

    budget_wait(page, 1000, "file_upload")
