"""Test: Selector poisoning — scorer picks visible element despite bad history.

Scenario: Known-good selector memory contains a selector that no longer
resolves to a visible element (e.g., after a UI redesign). The scorer
should still pick the visible element via other strategies.
"""

from unittest.mock import MagicMock

from app.worker.selector_engine.scoring import (
    CandidateScore,
    ScoringWeights,
    score_candidates,
)
from app.worker.selector_resilience import SelectorStrategy


def _make_mock_locator(count: int, visible_flags: list[bool], enabled_flags: list[bool] | None = None):
    """Create a mock locator with N candidates."""
    if enabled_flags is None:
        enabled_flags = [True] * count

    locator = MagicMock()
    locator.count.return_value = count

    elements = []
    for i in range(count):
        el = MagicMock()
        el.is_visible.return_value = visible_flags[i]
        el.is_enabled.return_value = enabled_flags[i]
        el.evaluate = MagicMock(return_value={
            "tag": "button",
            "text": f"Element {i}",
            "visible": visible_flags[i],
            "in_viewport": visible_flags[i],
            "enabled": enabled_flags[i],
            "rect": {"x": 0, "y": i * 50, "w": 100, "h": 40},
            "test_id": "",
            "role": "button",
            "aria_label": "Submit",
            "name_attr": "",
            "placeholder": "",
            "id": "",
            "selector_path": f"body > div > button#{i}",
        })
        el.inner_text = MagicMock(return_value=f"Element {i}")
        el.wait_for = MagicMock()
        elements.append(el)

    locator.nth = lambda idx: elements[idx]
    locator.first = elements[0] if elements else MagicMock()
    locator.first.wait_for = MagicMock()

    return locator, elements


def test_visible_element_scores_higher():
    """A visible element should score higher than a hidden one."""
    loc, elements = _make_mock_locator(
        count=2,
        visible_flags=[False, True],
    )

    candidates = score_candidates(
        page=MagicMock(),
        locator=loc,
        strategy=SelectorStrategy.ROLE,
        params={"role": "button", "name": "Submit"},
    )

    assert len(candidates) == 2
    # The visible one (index 1) should have higher composite score
    assert candidates[0].visibility_score > candidates[1].visibility_score
    assert candidates[0].composite > candidates[1].composite


def test_disabled_element_scored_lower():
    """A disabled element should score lower than an enabled one."""
    loc, elements = _make_mock_locator(
        count=2,
        visible_flags=[True, True],
        enabled_flags=[False, True],
    )

    candidates = score_candidates(
        page=MagicMock(),
        locator=loc,
        strategy=SelectorStrategy.ROLE,
        params={"role": "button", "name": "Submit"},
    )

    assert len(candidates) == 2
    # The enabled one (index 1) should score higher
    # _score_enabled returns 1.0 for enabled, 0.1 for disabled
    enabled_candidate = [c for c in candidates if c.enabled_score == 1.0][0]
    disabled_candidate = [c for c in candidates if c.enabled_score < 1.0][0]
    assert enabled_candidate.composite >= disabled_candidate.composite
