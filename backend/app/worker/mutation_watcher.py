"""DOM Mutation Watcher — real-time classified mutation recording.

Installs a MutationObserver via JS before an action, then collects
classified mutation data after the action completes.  This provides
richer signals than the before/after snapshot in behavior_detection.py:

- Mutation counts by type (insertion, removal, attribute, text)
- Which tags were mutated
- Which attributes changed
- Duration of mutation activity
- Whether mutations were "significant" (structural vs. cosmetic)

Usage in the action feedback loop:
    start_mutation_watcher(page)
    ... execute action ...
    summary = collect_mutations(page)
    if summary.has_significant_mutations:
        # Action had a real effect even if DOM hash didn't change
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = structlog.get_logger(__name__)


@dataclass
class MutationSummary:
    """Summary of DOM mutations recorded during an action."""

    node_insertions: int = 0
    node_removals: int = 0
    attribute_changes: int = 0
    text_changes: int = 0
    total_mutations: int = 0
    mutation_duration_ms: int = 0
    mutated_tags: list[str] = field(default_factory=list)
    changed_attributes: list[str] = field(default_factory=list)
    has_significant_mutations: bool = False

    def to_dict(self) -> dict:
        return {
            "node_insertions": self.node_insertions,
            "node_removals": self.node_removals,
            "attribute_changes": self.attribute_changes,
            "text_changes": self.text_changes,
            "total_mutations": self.total_mutations,
            "mutation_duration_ms": self.mutation_duration_ms,
            "mutated_tags": self.mutated_tags,
            "changed_attributes": self.changed_attributes,
            "has_significant_mutations": self.has_significant_mutations,
        }


# JS that installs a MutationObserver recording classified mutations.
# Stored on window.__qMutWatcher so collect can retrieve data.
_START_WATCHER_JS = """() => {
    const w = {
        startTime: Date.now(),
        lastMutTime: Date.now(),
        insertions: 0,
        removals: 0,
        attrChanges: 0,
        textChanges: 0,
        total: 0,
        tags: {},
        attrs: {},
    };

    const observer = new MutationObserver((mutations) => {
        w.lastMutTime = Date.now();
        for (const m of mutations) {
            w.total++;
            if (m.type === 'childList') {
                w.insertions += m.addedNodes.length;
                w.removals += m.removedNodes.length;
                for (const n of m.addedNodes) {
                    if (n.nodeType === 1) {
                        const tag = n.tagName.toLowerCase();
                        w.tags[tag] = (w.tags[tag] || 0) + 1;
                    }
                }
            } else if (m.type === 'attributes') {
                w.attrChanges++;
                const attr = m.attributeName || '';
                if (attr) w.attrs[attr] = (w.attrs[attr] || 0) + 1;
                if (m.target && m.target.nodeType === 1) {
                    const tag = m.target.tagName.toLowerCase();
                    w.tags[tag] = (w.tags[tag] || 0) + 1;
                }
            } else if (m.type === 'characterData') {
                w.textChanges++;
            }
        }
    });

    observer.observe(document.body || document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        characterData: true,
    });

    w.observer = observer;
    window.__qMutWatcher = w;
}"""


# JS that collects recorded mutations and disconnects the observer.
_COLLECT_MUTATIONS_JS = """() => {
    const w = window.__qMutWatcher;
    if (!w) return null;

    // Disconnect to stop recording
    if (w.observer) {
        w.observer.disconnect();
        w.observer = null;
    }

    const durationMs = w.lastMutTime - w.startTime;

    // Top mutated tags (up to 10)
    const tags = Object.entries(w.tags)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 10)
        .map(e => e[0]);

    // Top changed attributes (up to 10)
    const attrs = Object.entries(w.attrs)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 10)
        .map(e => e[0]);

    const result = {
        insertions: w.insertions,
        removals: w.removals,
        attrChanges: w.attrChanges,
        textChanges: w.textChanges,
        total: w.total,
        durationMs: Math.max(durationMs, 0),
        tags: tags,
        attrs: attrs,
    };

    // Clean up
    delete window.__qMutWatcher;
    return result;
}"""

# Cosmetic attribute changes that don't indicate real UI state change
_COSMETIC_ATTRIBUTES = frozenset({
    "style", "class", "data-state", "data-orientation",
})

# Structural tags whose insertion/removal indicates significant change
_SIGNIFICANT_TAGS = frozenset({
    "dialog", "form", "section", "article", "nav", "main",
    "table", "ul", "ol", "li", "option", "tr", "td",
    "input", "select", "textarea", "button", "a",
    "iframe", "video", "img",
})

# Minimum thresholds for "significant" classification
_MIN_SIGNIFICANT_INSERTIONS = 2
_MIN_SIGNIFICANT_TOTAL = 3


def start_mutation_watcher(page: Page) -> bool:
    """Install a MutationObserver to record DOM changes during an action.

    Args:
        page: Playwright Page instance.

    Returns:
        True if watcher was installed successfully.
    """
    try:
        page.evaluate(_START_WATCHER_JS)
        return True
    except Exception as e:
        logger.debug("mutation_watcher.start_failed", error=str(e)[:150])
        return False


def collect_mutations(page: Page) -> MutationSummary:
    """Collect recorded mutations and disconnect the observer.

    Must be called after start_mutation_watcher(). Returns a summary
    even if the watcher was never started (all zeros).

    Args:
        page: Playwright Page instance.

    Returns:
        MutationSummary with classified mutation data.
    """
    summary = MutationSummary()

    try:
        result = page.evaluate(_COLLECT_MUTATIONS_JS)
    except Exception as e:
        logger.debug("mutation_watcher.collect_failed", error=str(e)[:150])
        return summary

    if not isinstance(result, dict):
        return summary

    summary.node_insertions = result.get("insertions", 0)
    summary.node_removals = result.get("removals", 0)
    summary.attribute_changes = result.get("attrChanges", 0)
    summary.text_changes = result.get("textChanges", 0)
    summary.total_mutations = result.get("total", 0)
    summary.mutation_duration_ms = result.get("durationMs", 0)
    summary.mutated_tags = result.get("tags", [])
    summary.changed_attributes = result.get("attrs", [])

    # Determine if mutations are "significant" (structural, not just cosmetic)
    summary.has_significant_mutations = _is_significant(summary)

    if summary.total_mutations > 0:
        logger.debug(
            "mutation_watcher.collected",
            insertions=summary.node_insertions,
            removals=summary.node_removals,
            attr_changes=summary.attribute_changes,
            text_changes=summary.text_changes,
            total=summary.total_mutations,
            duration_ms=summary.mutation_duration_ms,
            significant=summary.has_significant_mutations,
        )

    return summary


def _is_significant(summary: MutationSummary) -> bool:
    """Determine if mutations represent a real UI state change.

    Filters out cosmetic changes (CSS class toggles, style updates)
    from structural changes (element insertion, dialog opening, etc.).
    """
    # Structural insertions/removals are always significant
    if summary.node_insertions >= _MIN_SIGNIFICANT_INSERTIONS:
        return True
    if summary.node_removals >= _MIN_SIGNIFICANT_INSERTIONS:
        return True

    # Significant tag was mutated
    for tag in summary.mutated_tags:
        if tag in _SIGNIFICANT_TAGS:
            return True

    # Non-cosmetic attribute changed
    for attr in summary.changed_attributes:
        if attr not in _COSMETIC_ATTRIBUTES:
            return True

    # High volume of changes suggests structural update
    if summary.total_mutations >= _MIN_SIGNIFICANT_TOTAL * 3:
        return True

    return False
