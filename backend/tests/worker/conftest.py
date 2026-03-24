"""Shared fixtures for worker test suite.

Provides mock Playwright page, AI provider, database, and
common test data used across all validation test files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from app.worker.ai_provider import (
    AICircuitBreaker,
    AINotAvailable,
    DeterministicFallbackProvider,
)
from app.worker.in_run_memory import InRunSelectorMemory, RecoveryEffectivenessTracker
from app.worker.intent_schema import ActionIntent, ActionType, StepIntent, TargetDescriptor
from app.worker.shared_context import SharedExecutionContext


# ---------------------------------------------------------------------------
# Mock Playwright Page
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_page():
    """Mock Playwright Page with common methods."""
    page = MagicMock()
    page.url = "https://example.com/app"
    page.title.return_value = "Test App"
    page.wait_for_timeout = MagicMock()
    page.wait_for_url = MagicMock()
    page.keyboard = MagicMock()
    page.goto = MagicMock()
    page.screenshot = MagicMock(return_value=b"fake_png_data")
    page.evaluate = MagicMock(return_value=None)
    page.wait_for_load_state = MagicMock()
    return page


@pytest.fixture
def mock_locator():
    """Mock Playwright Locator."""
    loc = MagicMock()
    loc.count.return_value = 1
    loc.first = loc
    loc.nth.return_value = loc
    loc.is_visible.return_value = True
    loc.is_enabled.return_value = True
    loc.click = MagicMock()
    loc.fill = MagicMock()
    loc.inner_text.return_value = "mock text"
    loc.input_value.return_value = "mock value"
    loc.wait_for = MagicMock()
    loc.scroll_into_view_if_needed = MagicMock()
    loc.page = MagicMock()
    return loc


# ---------------------------------------------------------------------------
# Mock AI Provider
# ---------------------------------------------------------------------------

class MockAIProvider:
    """Controllable mock AI provider for testing."""

    def __init__(self):
        self.generate_calls = 0
        self.diagnose_calls = 0
        self.should_fail = False
        self._raw_client = MagicMock()

    @property
    def raw_client(self):
        return self._raw_client

    def generate_intent(self, **kwargs):
        self.generate_calls += 1
        if self.should_fail:
            raise RuntimeError("AI generation failed")
        return _make_click_intent(), 100, 50

    def regenerate_intent(self, **kwargs):
        self.generate_calls += 1
        if self.should_fail:
            raise RuntimeError("AI regeneration failed")
        return _make_click_intent(), 100, 50

    def diagnose_failure(self, **kwargs):
        self.diagnose_calls += 1
        from app.worker.recovery.diagnosis import FailureDiagnosis, FailureType
        return FailureDiagnosis(
            failure_type=FailureType.UNKNOWN,
            root_cause="test failure",
            is_recoverable=True,
            confidence=0.8,
        )

    def disambiguate(self, **kwargs):
        return None

    def verify_vision(self, **kwargs):
        return {"status": "passed", "actual": "mock verification"}

    def verify_api_response(self, **kwargs):
        return {"status": "passed", "actual": "mock API verification"}


@pytest.fixture
def mock_ai_provider():
    return MockAIProvider()


@pytest.fixture
def failing_ai_provider():
    """AI provider that always fails — for circuit breaker tests."""
    provider = MockAIProvider()
    provider.should_fail = True
    return provider


# ---------------------------------------------------------------------------
# In-Run Memory
# ---------------------------------------------------------------------------

@pytest.fixture
def in_run_memory():
    return InRunSelectorMemory()


@pytest.fixture
def recovery_tracker():
    return RecoveryEffectivenessTracker()


# ---------------------------------------------------------------------------
# Shared Context
# ---------------------------------------------------------------------------

@pytest.fixture
def shared_context():
    return SharedExecutionContext()


# ---------------------------------------------------------------------------
# Sample Intents
# ---------------------------------------------------------------------------

def _make_click_intent(
    role: str = "button",
    name: str = "Submit",
) -> StepIntent:
    return StepIntent(
        step_number=1,
        description=f"Click {name}",
        actions=[
            ActionIntent(
                action=ActionType.CLICK,
                target=TargetDescriptor(role=role, name=name),
            ),
        ],
    )


def _make_fill_intent(
    label: str = "Email",
    value: str = "test@example.com",
) -> StepIntent:
    return StepIntent(
        step_number=1,
        description=f"Fill {label}",
        actions=[
            ActionIntent(
                action=ActionType.INPUT,
                target=TargetDescriptor(label=label),
                value=value,
            ),
        ],
    )


@pytest.fixture
def sample_click_intent():
    return _make_click_intent()


@pytest.fixture
def sample_fill_intent():
    return _make_fill_intent()


# ---------------------------------------------------------------------------
# Mock Database
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db():
    """Mock MongoDB database with selector_memory collection."""
    db = MagicMock()
    db.selector_memory = MagicMock()
    db.selector_memory.find_one = MagicMock(return_value=None)
    db.selector_memory.update_one = MagicMock()
    db.selector_memory.find = MagicMock(return_value=[])
    return db
