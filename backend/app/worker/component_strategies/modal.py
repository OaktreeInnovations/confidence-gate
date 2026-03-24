"""Modal dialog interaction strategy."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from app.worker.component_helpers import resolve_single
from app.worker.execution_budget import budget_wait

from .locale import CLOSE_BUTTON_LABEL

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


def interact_in_modal(
    page: Page,
    action_fn,
    *,
    close_on_done: bool = False,
) -> None:
    """Execute an action scoped inside a visible modal dialog.

    Finds the topmost visible dialog, scopes the action function to it,
    and optionally closes the modal afterward.

    Args:
        page: Playwright Page.
        action_fn: Callable(dialog_locator) that performs interactions inside the modal.
        close_on_done: If True, press Escape after the action to close the modal.
    """
    dialog = _find_visible_dialog(page)
    if dialog is None:
        raise RuntimeError("No visible dialog found on page")

    action_fn(dialog)

    if close_on_done:
        try:
            # Try close button first
            close_btn = resolve_single(dialog.get_by_role("button", name=CLOSE_BUTTON_LABEL), context="interact_in_modal:close_button")
            close_btn.click()
        except Exception:
            page.keyboard.press("Escape")
        budget_wait(page, 500, "modal_close")


def _find_visible_dialog(page: Page) -> Locator | None:
    """Find the topmost visible dialog/modal."""
    # Try role="dialog"
    try:
        dialogs = page.get_by_role("dialog")
        count = dialogs.count()
        if count > 0:
            # Return the last (topmost) visible dialog
            for i in range(count - 1, -1, -1):
                d = dialogs.nth(i)
                if d.is_visible():
                    return d
    except Exception:
        pass

    # Try role="alertdialog"
    try:
        alert = resolve_single(page.get_by_role("alertdialog"), context="find_visible_dialog:alertdialog")
        if alert.is_visible():
            return alert
    except Exception:
        pass

    # Try common modal CSS patterns
    for selector in (
        "[class*='modal'][class*='open']",
        "[class*='modal']:not([class*='hidden'])",
        ".modal.show",
        "[role='presentation'] [class*='modal']",
        ".MuiDialog-root",
    ):
        try:
            loc = resolve_single(page.locator(selector), context="find_visible_dialog:css_modal")
            if loc.is_visible():
                return loc
        except Exception:
            continue

    return None
