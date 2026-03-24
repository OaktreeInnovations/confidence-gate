"""Tests for AI Structural Repair (Healing Layer 3).

Validates:
- AI proposes valid selector → passes scoring + guard → accepted
- AI low confidence → rejected
- AI selector fails guard → rejected
- AI unavailable → returns gracefully
"""

import json
from unittest.mock import MagicMock, patch

from app.worker.healing.ai_repair import (
    AIRepairResult,
    ai_structural_repair,
    _MIN_AI_CONFIDENCE,
    _MIN_PIPELINE_SCORE,
)
from app.worker.intent_schema import ActionIntent, ActionType, TargetDescriptor
from app.worker.interaction_guard import GuardResult
from app.worker.recovery.diagnosis import FailureDiagnosis, FailureType
from app.worker.selector_engine.scoring import CandidateScore
from app.worker.selector_resilience import SelectorStrategy


def _make_target():
    return TargetDescriptor(role="button", name="Submit")


def _make_action():
    return ActionIntent(action=ActionType.CLICK, target=_make_target())


def _make_diagnosis():
    return FailureDiagnosis(
        failure_type=FailureType.ELEMENT_NOT_FOUND,
        root_cause="Element not found",
        is_recoverable=True,
    )


def _make_ai_client(response_json=None, raw_available=True):
    """Create mock AI provider that returns controlled responses."""
    client = MagicMock()
    if not raw_available:
        client.raw_client = None
        return client

    raw = MagicMock()
    client.raw_client = raw

    if response_json is not None:
        completion = MagicMock()
        completion.choices = [MagicMock()]
        completion.choices[0].message.content = json.dumps(response_json)
        raw.chat.completions.create.return_value = completion

    return client


def test_ai_repair_proposes_valid_selector():
    """AI returns high-confidence selector that passes scoring and guard."""
    page = MagicMock()
    page.accessibility.snapshot.return_value = {"role": "WebArea"}
    page.evaluate.return_value = "<button>Submit</button>"

    ai_client = _make_ai_client({
        "selector": "#submit-btn",
        "confidence": 0.85,
        "reasoning": "Found button by ID",
        "action_override": "",
    })

    mock_locator = MagicMock()
    page.locator.return_value = mock_locator

    mock_candidate = CandidateScore(
        locator=mock_locator,
        strategy=SelectorStrategy.CSS,
        params={"selector": "#submit-btn"},
        composite=0.80,
    )

    mock_guard = GuardResult(passed=True)

    with patch("app.worker.healing.ai_repair.score_candidates",
               return_value=[mock_candidate]), \
         patch("app.worker.healing.ai_repair.ensure_interactable",
               return_value=mock_guard):
        result = ai_structural_repair(
            page, _make_target(), _make_action(), _make_diagnosis(),
            ai_client, failed_selectors=["role:button"],
        )

    assert result.repaired
    assert result.selector == "#submit-btn"
    assert result.confidence == 0.85
    assert result.pipeline_score == 0.80
    assert result.ai_reasoning == "Found button by ID"


def test_ai_repair_low_confidence_rejected():
    """AI returns confidence < 0.6 — rejected without pipeline validation."""
    page = MagicMock()
    page.accessibility.snapshot.return_value = {"role": "WebArea"}
    page.evaluate.return_value = ""

    ai_client = _make_ai_client({
        "selector": ".maybe-btn",
        "confidence": 0.3,
        "reasoning": "Uncertain match",
        "action_override": "",
    })

    result = ai_structural_repair(
        page, _make_target(), _make_action(), _make_diagnosis(),
        ai_client,
    )

    assert not result.repaired
    assert result.confidence == 0.3


def test_ai_repair_fails_guard_rejected():
    """AI selector passes scoring but fails interaction guard — rejected."""
    page = MagicMock()
    page.accessibility.snapshot.return_value = {"role": "WebArea"}
    page.evaluate.return_value = ""

    ai_client = _make_ai_client({
        "selector": "#hidden-btn",
        "confidence": 0.8,
        "reasoning": "Found by ID",
        "action_override": "",
    })

    mock_locator = MagicMock()
    page.locator.return_value = mock_locator

    mock_candidate = CandidateScore(
        locator=mock_locator,
        strategy=SelectorStrategy.CSS,
        params={"selector": "#hidden-btn"},
        composite=0.70,
    )

    mock_guard = GuardResult(passed=False)
    mock_guard.checks_failed = ["bounding_box_zero"]

    with patch("app.worker.healing.ai_repair.score_candidates",
               return_value=[mock_candidate]), \
         patch("app.worker.healing.ai_repair.ensure_interactable",
               return_value=mock_guard):
        result = ai_structural_repair(
            page, _make_target(), _make_action(), _make_diagnosis(),
            ai_client,
        )

    assert not result.repaired
    assert result.pipeline_score == 0.70


def test_ai_repair_unavailable_returns_none():
    """AI not available — returns repaired=False gracefully."""
    page = MagicMock()
    ai_client = _make_ai_client(raw_available=False)

    result = ai_structural_repair(
        page, _make_target(), _make_action(), _make_diagnosis(),
        ai_client,
    )

    assert not result.repaired
    assert result.elapsed_ms == 0


def test_ai_repair_pipeline_score_too_low():
    """AI selector scores below pipeline threshold — rejected."""
    page = MagicMock()
    page.accessibility.snapshot.return_value = {"role": "WebArea"}
    page.evaluate.return_value = ""

    ai_client = _make_ai_client({
        "selector": ".weak-match",
        "confidence": 0.7,
        "reasoning": "Partial match",
        "action_override": "",
    })

    mock_locator = MagicMock()
    page.locator.return_value = mock_locator

    mock_candidate = CandidateScore(
        locator=mock_locator,
        strategy=SelectorStrategy.CSS,
        params={"selector": ".weak-match"},
        composite=0.30,  # Below _MIN_PIPELINE_SCORE
    )

    with patch("app.worker.healing.ai_repair.score_candidates",
               return_value=[mock_candidate]):
        result = ai_structural_repair(
            page, _make_target(), _make_action(), _make_diagnosis(),
            ai_client,
        )

    assert not result.repaired
    assert result.pipeline_score == 0.30
