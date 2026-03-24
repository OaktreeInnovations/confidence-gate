"""Autocomplete / typeahead interaction strategy."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from app.worker.execution_budget import budget_wait

from .dropdown import _select_option_from_list

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


def fill_autocomplete(
    page: Page,
    input_locator: Locator,
    search_text: str,
    select_text: str = "",
    *,
    debounce_ms: int = 500,
) -> None:
    """Interact with an autocomplete/typeahead input.

    1. Focus and clear the input.
    2. Type the search text character-by-character (triggers debounce).
    3. Wait for suggestion list to appear.
    4. Select the matching suggestion.

    Args:
        page: Playwright Page (or Locator — auto-extracts .page for modal compat).
        input_locator: Locator for the autocomplete input.
        search_text: Text to type to trigger suggestions.
        select_text: The suggestion text to click. Defaults to search_text.
        debounce_ms: Wait time after typing for suggestions to appear.
    """
    # Support being called from interact_in_modal where first arg is a Locator
    if not hasattr(page, "goto"):
        page = page.page

    target = select_text or search_text
    input_locator.wait_for(state="visible", timeout=15000)
    input_locator.click()
    input_locator.fill("")
    input_locator.press_sequentially(search_text, delay=50)
    budget_wait(page, debounce_ms, "autocomplete_debounce")

    # Wait for and select from suggestion list
    _select_option_from_list(page, target)
    budget_wait(page, 300, "autocomplete_option_select")
