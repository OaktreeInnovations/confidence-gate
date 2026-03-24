"""Framework-Agnostic Component Strategy Registry.

Provides a plugin-based system where component interaction strategies
(date picker, dropdown, autocomplete, file upload) are registered per
UI framework and selected at runtime via DOM detection.

Detection flow:
1. Inspect page DOM for framework-specific attributes/classes
2. Select the matching strategy set (Radix/Shadcn, MUI, Ant Design, generic)
3. Fall back to generic Playwright primitives if no framework detected

Adding a new framework:
    @component_registry.register("mui")
    class MuiStrategies(ComponentStrategies):
        def pick_date(self, page, trigger, day, **kw): ...
        def select_dropdown(self, page, trigger, option_text, **kw): ...

The existing component_strategies.py functions are wrapped as the "radix"
strategy set. Generic strategies provide basic Playwright-only fallbacks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Union

import structlog

from app.worker.component_helpers import resolve_single
from app.worker.execution_budget import budget_wait

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


class ComponentStrategies(ABC):
    """Abstract interface for framework-specific component interactions."""

    framework_name: str = "unknown"

    @abstractmethod
    def pick_date(
        self,
        page: Page,
        trigger: Union[Locator, str],
        day: Union[int, str],
        *,
        month_label: str = "",
        **kwargs,
    ) -> None:
        """Interact with a date picker."""
        ...

    @abstractmethod
    def select_dropdown(
        self,
        page: Page,
        trigger: Union[Locator, str],
        option_text: str,
        *,
        search_text: str = "",
        **kwargs,
    ) -> None:
        """Select from a dropdown/combobox."""
        ...

    @abstractmethod
    def fill_autocomplete(
        self,
        page: Page,
        input_locator: Locator,
        search_text: str,
        select_text: str = "",
        *,
        debounce_ms: int = 500,
        **kwargs,
    ) -> None:
        """Fill an autocomplete/typeahead input."""
        ...

    @abstractmethod
    def upload_file(
        self,
        page: Page,
        file_path: str,
        trigger: Locator | None = None,
        **kwargs,
    ) -> None:
        """Upload a file."""
        ...


class ComponentRegistry:
    """Registry of framework-specific component strategies."""

    def __init__(self) -> None:
        self._strategies: dict[str, ComponentStrategies] = {}
        self._detection_cache: dict[int, str] = {}  # page_id → framework

    def register(self, framework: str, strategies: ComponentStrategies) -> None:
        """Register a strategy set for a framework."""
        strategies.framework_name = framework
        self._strategies[framework] = strategies
        logger.debug("component_registry.registered", framework=framework)

    def get_strategies(self, page: Page) -> ComponentStrategies:
        """Detect the UI framework and return the matching strategies.

        Caches detection per page to avoid repeated DOM inspection.
        Falls back to 'generic' if no framework detected.
        """
        page_id = id(page)
        if page_id in self._detection_cache:
            framework = self._detection_cache[page_id]
        else:
            framework = detect_framework(page)
            self._detection_cache[page_id] = framework

        strategies = self._strategies.get(framework)
        if strategies is None:
            strategies = self._strategies.get("generic")
        if strategies is None:
            raise RuntimeError(
                f"No component strategies registered for framework '{framework}' "
                f"and no generic fallback available"
            )

        return strategies

    def invalidate_cache(self, page: Page | None = None) -> None:
        """Clear framework detection cache (e.g., after navigation)."""
        if page is not None:
            self._detection_cache.pop(id(page), None)
        else:
            self._detection_cache.clear()

    @property
    def registered_frameworks(self) -> list[str]:
        return list(self._strategies.keys())


# Global singleton
component_registry = ComponentRegistry()


# --- Framework detection ---

_DETECTION_JS = """() => {
    const signals = {
        radix: false,
        mui: false,
        antd: false,
        headless_ui: false,
    };

    // Radix UI / Shadcn: data-radix-*, data-slot, cmdk-* attributes
    if (document.querySelector('[data-radix-collection-item]')
        || document.querySelector('[data-radix-popper-content-wrapper]')
        || document.querySelector('[data-slot]')
        || document.querySelector('[cmdk-root]')
        || document.querySelector('[data-radix-scroll-area-viewport]')) {
        signals.radix = true;
    }

    // MUI: MuiButtonBase, mui-*, MuiInput, etc.
    if (document.querySelector('[class*="MuiButtonBase"]')
        || document.querySelector('[class*="MuiInput"]')
        || document.querySelector('[class*="MuiSelect"]')
        || document.querySelector('.MuiPaper-root')) {
        signals.mui = true;
    }

    // Ant Design: ant-*, anticon, ant-picker
    if (document.querySelector('[class*="ant-btn"]')
        || document.querySelector('[class*="ant-input"]')
        || document.querySelector('[class*="ant-select"]')
        || document.querySelector('.ant-picker')) {
        signals.antd = true;
    }

    // Headless UI: data-headlessui-state
    if (document.querySelector('[data-headlessui-state]')) {
        signals.headless_ui = true;
    }

    return signals;
}"""


def detect_framework(page: Page) -> str:
    """Detect UI framework from DOM attributes.

    Returns the framework identifier string, or 'generic' if none detected.
    """
    try:
        signals = page.evaluate(_DETECTION_JS)
    except Exception:
        return "generic"

    # Priority: radix > mui > antd > headless_ui > generic
    from app.worker.selector_engine.semantic_plugins import register_framework

    if signals.get("radix"):
        register_framework("radix")
        return "radix"
    if signals.get("mui"):
        register_framework("mui")
        return "mui"
    if signals.get("antd"):
        register_framework("antd")
        return "antd"
    if signals.get("headless_ui"):
        register_framework("headless_ui")
        return "headless_ui"
    return "generic"


# --- Radix/Shadcn strategies (wraps existing component_strategies.py) ---

class RadixStrategies(ComponentStrategies):
    """Strategy set for Radix UI / Shadcn components."""

    def pick_date(self, page, trigger, day, *, month_label="", **kwargs):
        from app.worker.component_strategies import pick_date
        pick_date(page, trigger, day, month_label=month_label, **kwargs)

    def select_dropdown(self, page, trigger, option_text, *, search_text="", **kwargs):
        from app.worker.component_strategies import select_dropdown
        select_dropdown(page, trigger, option_text, search_text=search_text)

    def fill_autocomplete(self, page, input_locator, search_text, select_text="", *, debounce_ms=500, **kwargs):
        from app.worker.component_strategies import fill_autocomplete
        fill_autocomplete(page, input_locator, search_text, select_text, debounce_ms=debounce_ms)

    def upload_file(self, page, file_path, trigger=None, **kwargs):
        from app.worker.component_strategies import upload_file
        upload_file(page, file_path, trigger=trigger)


# --- Generic strategies (Playwright primitives only) ---

class GenericStrategies(ComponentStrategies):
    """Generic strategy set using standard Playwright primitives.

    Works with native HTML elements and most frameworks as a fallback.
    No framework-specific selectors or interaction patterns.
    """

    def pick_date(self, page, trigger, day, *, month_label="", **kwargs):
        """Generic date picker: click trigger, type date, press Enter."""
        if isinstance(trigger, str):
            loc = page.get_by_label(trigger)
        else:
            loc = trigger
        loc.click()
        budget_wait(page, 500, "generic_date_open")

        # Try native date input
        try:
            input_type = loc.get_attribute("type")
            if input_type == "date":
                loc.fill(str(day))
                return
        except Exception:
            pass

        # Look for a calendar grid and click the day
        try:
            grid = resolve_single(page.locator('[role="grid"]'), context="generic_calendar_grid", timeout_ms=3000)
            grid.wait_for(state="visible", timeout=3000)
            day_cell = resolve_single(grid.get_by_text(str(day), exact=True), context=f"generic_day_{day}")
            day_cell.click()
        except Exception:
            # Last resort: type the date and press Enter
            loc.fill(str(day))
            page.keyboard.press("Enter")

    def select_dropdown(self, page, trigger, option_text, *, search_text="", **kwargs):
        """Generic dropdown: click trigger, find option, click it."""
        if isinstance(trigger, str):
            loc = page.get_by_label(trigger)
        else:
            loc = trigger
        loc.click()
        budget_wait(page, 500, "generic_dropdown_open")

        # Try native <select>
        try:
            tag = loc.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                loc.select_option(label=option_text)
                return
        except Exception:
            pass

        # Try listbox/option pattern
        try:
            option = resolve_single(page.get_by_role("option", name=option_text), context=f"generic_option:{option_text[:20]}", timeout_ms=3000)
            option.wait_for(state="visible", timeout=3000)
            option.click()
            return
        except Exception:
            pass

        # Try text match in any visible dropdown content
        try:
            option = resolve_single(page.get_by_text(option_text, exact=True), context=f"generic_text:{option_text[:20]}", timeout_ms=2000)
            option.wait_for(state="visible", timeout=2000)
            option.click()
        except Exception:
            # Keyboard fallback
            page.keyboard.type(option_text[:3], delay=100)
            budget_wait(page, 300, "generic_dropdown_keyboard")
            page.keyboard.press("Enter")

    def fill_autocomplete(self, page, input_locator, search_text, select_text="", *, debounce_ms=500, **kwargs):
        """Generic autocomplete: type, wait for suggestions, click match."""
        target = select_text or search_text
        input_locator.click()
        input_locator.fill("")
        input_locator.press_sequentially(search_text, delay=50)
        budget_wait(page, debounce_ms, "generic_autocomplete_debounce")

        # Try listbox option
        try:
            option = resolve_single(page.get_by_role("option", name=target), context=f"generic_ac_option:{target[:20]}", timeout_ms=3000)
            option.wait_for(state="visible", timeout=3000)
            option.click()
            return
        except Exception:
            pass

        # Try text match
        try:
            option = resolve_single(page.get_by_text(target, exact=True), context=f"generic_ac_text:{target[:20]}", timeout_ms=2000)
            option.wait_for(state="visible", timeout=2000)
            option.click()
        except Exception:
            page.keyboard.press("Enter")

    def upload_file(self, page, file_path, trigger=None, **kwargs):
        """Generic file upload."""
        if trigger:
            with page.expect_file_chooser(timeout=10000) as fc_info:
                trigger.click()
            fc_info.value.set_files(file_path)
        else:
            resolve_single(page.locator("input[type='file']"), context="generic_file_input").set_input_files(file_path)
        budget_wait(page, 1000, "generic_file_upload")


# --- Auto-register built-in strategies ---

def _register_builtins() -> None:
    """Register built-in strategy sets."""
    component_registry.register("radix", RadixStrategies())
    component_registry.register("generic", GenericStrategies())


_register_builtins()
