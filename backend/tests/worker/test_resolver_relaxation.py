"""Test: Resolver chain auto-relaxation when primary strategies find 0 candidates.

Scenario: The AI generates a target like {role: gridcell, name: Today} but
the DOM has no element with that exact accessible name. The resolver should
relax the chain (e.g., drop name, try text, try semantic CSS) and still
find candidates.

Now tested via _build_tiered_chains — relaxations live in Tier 3.
"""

from app.worker.selector_engine.resolver import (
    _build_tiered_chains,
    _expand_semantic_name,
)
from app.worker.selector_resilience import SelectorStrategy
from app.worker.intent_schema import TargetDescriptor


def test_role_name_relaxes_to_role_only():
    """When role+name is the only strategy, Tier 3 should add role-only and text."""
    target = TargetDescriptor(role="gridcell", name="Today")
    tiers = _build_tiered_chains(target)

    # Tier 3 contains the relaxations
    tier3 = tiers[-1]
    strategies = [(s, p) for s, p in tier3]
    # Should have role-only (no name)
    assert (SelectorStrategy.ROLE, {"role": "gridcell"}) in strategies
    # Should have text fallback
    assert (SelectorStrategy.TEXT, {"text": "Today", "exact": False}) in strategies


def test_semantic_today_produces_css_selectors():
    """Name 'Today' should expand to aria-current and framework CSS selectors."""
    css_selectors = _expand_semantic_name("Today")

    assert len(css_selectors) > 0
    assert "[aria-current='date']" in css_selectors


def test_semantic_unknown_returns_empty():
    """Unknown semantic names should return no CSS selectors."""
    assert _expand_semantic_name("Submit") == []
    assert _expand_semantic_name("Cancel") == []


def test_relaxation_skips_duplicates():
    """Tiered chains should not duplicate strategies across tiers."""
    target = TargetDescriptor(role="button", name="Submit", text="Submit")
    tiers = _build_tiered_chains(target)

    # Collect all (strategy, params) across all tiers
    all_entries = [(s, tuple(sorted(p.items()))) for tier in tiers for s, p in tier]
    # No duplicates
    assert len(all_entries) == len(set(all_entries))

    # Tier 3 should still have role-only relaxation
    tier3 = tiers[-1]
    assert (SelectorStrategy.ROLE, {"role": "button"}) in tier3


def test_no_relaxation_for_role_without_name():
    """Role-only target (no name) should not produce Tier 3 relaxations from role."""
    target = TargetDescriptor(role="button")
    tiers = _build_tiered_chains(target)

    # Tier 1 has ROLE, Tier 2 has structural (no text to search on → empty)
    # There should be no Tier 3 since there's no text/css/name relaxations
    for tier in tiers:
        tier3_role_only = [
            (s, p) for s, p in tier
            if s == SelectorStrategy.TEXT
        ]
        # No TEXT relaxation since there's no name to relax
        if tier is tiers[-1]:
            assert len(tier3_role_only) == 0 or tiers[-1] is tiers[0]


def test_relaxed_chain_includes_semantic_css():
    """Today target should include semantic CSS selectors in Tier 3."""
    target = TargetDescriptor(role="gridcell", name="Today")
    tiers = _build_tiered_chains(target)

    tier3 = tiers[-1]
    css_entries = [p["selector"] for s, p in tier3 if s == SelectorStrategy.CSS]
    assert "[aria-current='date']" in css_entries
