"""Test: Universal Resolution Escalation Model (Part 1).

Tests the 4-tier resolution system:
- Tier 1 (Strict): test_id, role+name, label, placeholder
- Tier 2 (Structural): interactive_ancestor, aria_widget, near_label
- Tier 3 (Proximity): text, css, role-only relaxation
- Tier 4 (AI): structural arbitration via AI
"""

from unittest.mock import MagicMock, patch

import pytest

from app.worker.intent_schema import TargetDescriptor
from app.worker.selector_engine.resolver import (
    _build_tiered_chains,
    _TIER_THRESHOLDS,
    resolve_target,
)
from app.worker.selector_engine.scoring import CandidateScore
from app.worker.selector_resilience import SelectorStrategy


def _make_candidate(
    strategy: SelectorStrategy,
    params: dict,
    composite: float,
    total: int = 1,
    tier: int = 0,
) -> CandidateScore:
    """Create a CandidateScore with controlled values."""
    locator = MagicMock()
    locator.evaluate.return_value = "div > button"
    return CandidateScore(
        locator=locator,
        strategy=strategy,
        params=params,
        composite=composite,
        strategy_score=composite,
        visibility_score=1.0,
        enabled_score=1.0,
        match_confidence_score=composite,
        proximity_score=0.5,
        total_candidates=total,
        resolution_tier=tier,
    )


# ─── Chain builder tests ───────────────────────────────────────────────────


def test_tier1_exact_role_name():
    """role+name target produces 3 tiers: strict, structural, relaxations."""
    target = TargetDescriptor(role="button", name="Submit")
    tiers = _build_tiered_chains(target)

    assert len(tiers) == 3

    # Tier 1: ROLE with name
    tier1_strategies = [s for s, _ in tiers[0]]
    assert SelectorStrategy.ROLE in tier1_strategies
    role_entry = next((s, p) for s, p in tiers[0] if s == SelectorStrategy.ROLE)
    assert role_entry[1] == {"role": "button", "name": "Submit"}

    # Tier 2: structural discovery using "Submit" as search text
    tier2_strategies = [s for s, _ in tiers[1]]
    assert SelectorStrategy.INTERACTIVE_ANCESTOR in tier2_strategies
    assert SelectorStrategy.ARIA_WIDGET in tier2_strategies
    assert SelectorStrategy.NEAR_LABEL in tier2_strategies

    # Tier 3: relaxations (role-only, loose text)
    tier3_strategies = [s for s, _ in tiers[2]]
    assert SelectorStrategy.ROLE in tier3_strategies
    assert SelectorStrategy.TEXT in tier3_strategies


def test_tier2_interactive_ancestor():
    """Target with name populates INTERACTIVE_ANCESTOR in Tier 2."""
    target = TargetDescriptor(role="button", name="Add to Cart")
    tiers = _build_tiered_chains(target)

    tier2 = tiers[1]
    ia_entries = [(s, p) for s, p in tier2 if s == SelectorStrategy.INTERACTIVE_ANCESTOR]
    assert len(ia_entries) == 1
    assert ia_entries[0][1] == {"text": "Add to Cart"}


def test_tier2_aria_widget():
    """Target with name populates ARIA_WIDGET in Tier 2."""
    target = TargetDescriptor(role="combobox", name="Country")
    tiers = _build_tiered_chains(target)

    tier2 = tiers[1]
    aw_entries = [(s, p) for s, p in tier2 if s == SelectorStrategy.ARIA_WIDGET]
    assert len(aw_entries) == 1
    assert aw_entries[0][1] == {"text": "Country"}


def test_tier2_near_label():
    """Target with label populates NEAR_LABEL in Tier 2."""
    target = TargetDescriptor(label="Email")
    tiers = _build_tiered_chains(target)

    # Tier 1: LABEL
    tier1_strategies = [s for s, _ in tiers[0]]
    assert SelectorStrategy.LABEL in tier1_strategies

    # Tier 2: NEAR_LABEL using "Email" as search text
    tier2 = tiers[1]
    nl_entries = [(s, p) for s, p in tier2 if s == SelectorStrategy.NEAR_LABEL]
    assert len(nl_entries) == 1
    assert nl_entries[0][1] == {"label": "Email"}


def test_tier3_relaxed_text():
    """role+name target includes loose text match on name in Tier 3."""
    target = TargetDescriptor(role="button", name="Submit")
    tiers = _build_tiered_chains(target)

    tier3 = tiers[2]
    text_entries = [(s, p) for s, p in tier3 if s == SelectorStrategy.TEXT]
    assert len(text_entries) == 1
    assert text_entries[0][1] == {"text": "Submit", "exact": False}


# ─── Resolver integration tests ────────────────────────────────────────────


@patch("app.worker.selector_engine.resolver.score_candidates")
@patch("app.worker.selector_engine.resolver._build_locator")
def test_no_escalation_on_high_confidence(mock_build_locator, mock_score):
    """Tier 1 scores >= 0.85 unique: no escalation to Tier 2."""
    target = TargetDescriptor(role="button", name="Submit")
    page = MagicMock()

    call_count = {"n": 0}

    def build_locator_side_effect(page, strategy, params):
        return MagicMock()

    def score_side_effect(page, locator, strategy, params, **kw):
        call_count["n"] += 1
        if strategy == SelectorStrategy.ROLE:
            return [_make_candidate(strategy, params, 0.90, total=1)]
        return []

    mock_build_locator.side_effect = build_locator_side_effect
    mock_score.side_effect = score_side_effect

    result = resolve_target(page, target, timeout_ms=5000)

    assert result.strategy == SelectorStrategy.ROLE
    assert result.score.composite == 0.90
    assert result.score.resolution_tier == 1
    # Only ROLE scored (early exit in Tier 1)
    assert call_count["n"] == 1


@patch("app.worker.selector_engine.resolver.score_candidates")
@patch("app.worker.selector_engine.resolver._build_locator")
def test_tier_escalation_to_tier2(mock_build_locator, mock_score):
    """Tier 1 candidates below threshold escalates to Tier 2."""
    target = TargetDescriptor(role="button", name="Add to Cart")
    page = MagicMock()

    strategies_scored = []

    def build_locator_side_effect(page, strategy, params):
        return MagicMock()

    def score_side_effect(page, locator, strategy, params, **kw):
        strategies_scored.append(strategy)
        if strategy == SelectorStrategy.ROLE:
            # Tier 1: ambiguous (3 candidates, below threshold)
            return [_make_candidate(strategy, params, 0.60, total=3)]
        if strategy == SelectorStrategy.INTERACTIVE_ANCESTOR:
            # Tier 2: unique interactive ancestor
            return [_make_candidate(strategy, params, 0.75, total=1)]
        return []

    mock_build_locator.side_effect = build_locator_side_effect
    mock_score.side_effect = score_side_effect

    result = resolve_target(page, target, timeout_ms=5000)

    # Should have escalated past Tier 1
    assert SelectorStrategy.INTERACTIVE_ANCESTOR in strategies_scored
    # Winner is from Tier 2 (higher composite)
    assert result.score.composite == 0.75
    assert result.score.resolution_tier == 2


@patch("app.worker.selector_engine.resolver.score_candidates")
@patch("app.worker.selector_engine.resolver._build_locator")
def test_tier4_ai_arbitration_mock(mock_build_locator, mock_score):
    """All tiers fail, AI proposes selector accepted at 0.7 confidence."""
    target = TargetDescriptor(role="button", name="Submit")
    page = MagicMock()

    def build_locator_side_effect(page, strategy, params):
        return MagicMock()

    def score_side_effect(page, locator, strategy, params, **kw):
        # AI-proposed CSS selector scores well
        if params.get("ai_proposed"):
            return [_make_candidate(strategy, params, 0.65, total=1, tier=4)]
        return []

    mock_build_locator.side_effect = build_locator_side_effect
    mock_score.side_effect = score_side_effect

    # Mock AI client
    ai_client = MagicMock()
    raw_mock = MagicMock()
    ai_client.raw_client = raw_mock

    choice = MagicMock()
    choice.message.content = '{"selector": "#submit-btn", "confidence": 0.7}'
    response = MagicMock()
    response.choices = [choice]
    raw_mock.chat.completions.create.return_value = response

    page.accessibility.snapshot.return_value = {"role": "WebArea", "children": []}
    page.evaluate.return_value = "<button>Submit</button>"
    page.locator.return_value = MagicMock()

    result = resolve_target(page, target, timeout_ms=10000, ai_client=ai_client)

    assert result.score.resolution_tier == 4
    raw_mock.chat.completions.create.assert_called_once()


@patch("app.worker.selector_engine.resolver.score_candidates")
@patch("app.worker.selector_engine.resolver._build_locator")
def test_tier4_ai_low_confidence_rejected(mock_build_locator, mock_score):
    """AI proposes at 0.4 confidence: rejected, resolution fails."""
    target = TargetDescriptor(role="button", name="Submit")
    page = MagicMock()

    mock_build_locator.return_value = MagicMock()
    mock_score.return_value = []

    ai_client = MagicMock()
    raw_mock = MagicMock()
    ai_client.raw_client = raw_mock

    choice = MagicMock()
    choice.message.content = '{"selector": "#maybe-submit", "confidence": 0.4}'
    response = MagicMock()
    response.choices = [choice]
    raw_mock.chat.completions.create.return_value = response

    page.accessibility.snapshot.return_value = {}
    page.evaluate.return_value = ""

    with pytest.raises(RuntimeError, match="no candidates"):
        resolve_target(page, target, timeout_ms=10000, ai_client=ai_client)


def test_backward_compat_existing_chain():
    """Full target descriptor produces all strategies across correct tiers."""
    target = TargetDescriptor(
        test_id="login-btn",
        role="button",
        name="Login",
        label="Sign In",
        placeholder="Enter username",
        text="Login",
        css="#login-btn",
    )
    tiers = _build_tiered_chains(target)

    all_strategies = [s for tier in tiers for s, _ in tier]

    # All original strategies present
    assert SelectorStrategy.TEST_ID in all_strategies
    assert SelectorStrategy.ROLE in all_strategies
    assert SelectorStrategy.LABEL in all_strategies
    assert SelectorStrategy.PLACEHOLDER in all_strategies
    assert SelectorStrategy.TEXT in all_strategies
    assert SelectorStrategy.CSS in all_strategies

    # New structural strategies also present
    assert SelectorStrategy.INTERACTIVE_ANCESTOR in all_strategies
    assert SelectorStrategy.ARIA_WIDGET in all_strategies
    assert SelectorStrategy.NEAR_LABEL in all_strategies

    # Tier 1: strict strategies
    tier1_strategies = [s for s, _ in tiers[0]]
    assert SelectorStrategy.TEST_ID in tier1_strategies
    assert SelectorStrategy.ROLE in tier1_strategies
    assert SelectorStrategy.LABEL in tier1_strategies
    assert SelectorStrategy.PLACEHOLDER in tier1_strategies

    # Tier 2: structural strategies
    tier2_strategies = [s for s, _ in tiers[1]]
    assert SelectorStrategy.INTERACTIVE_ANCESTOR in tier2_strategies

    # Tier 3: relaxed strategies
    tier3_strategies = [s for s, _ in tiers[2]]
    assert SelectorStrategy.TEXT in tier3_strategies
    assert SelectorStrategy.CSS in tier3_strategies
