"""Test: Heuristic intent generator for simple test steps.

Validates that the heuristic generator correctly parses common step
descriptions into StepIntent objects without requiring AI.
"""

from app.worker.heuristic_intent import generate_heuristic_intent
from app.worker.intent_schema import ActionType


def test_navigate_pattern():
    """Navigate step should produce NAVIGATE action with URL."""
    intent = generate_heuristic_intent("Navigate to https://example.com/login", 1)
    assert intent is not None
    assert len(intent.actions) == 1
    assert intent.actions[0].action == ActionType.NAVIGATE
    assert intent.actions[0].value == "https://example.com/login"


def test_navigate_go_to():
    """'Go to' should also match navigate pattern."""
    intent = generate_heuristic_intent("Go to /dashboard", 1)
    assert intent is not None
    assert intent.actions[0].action == ActionType.NAVIGATE
    assert intent.actions[0].value == "/dashboard"


def test_navigate_rejects_non_url():
    """Navigate pattern should reject non-URL targets."""
    intent = generate_heuristic_intent("Navigate to the settings page", 1)
    assert intent is None


def test_click_with_role():
    """Click step should extract role and name from description."""
    intent = generate_heuristic_intent("Click the Submit button", 1)
    assert intent is not None
    assert intent.actions[0].action == ActionType.CLICK
    assert intent.actions[0].target is not None
    assert intent.actions[0].target.role == "button"
    assert intent.actions[0].target.name == "Submit"


def test_click_link():
    """Click link should extract role=link."""
    intent = generate_heuristic_intent("Click the Login link", 1)
    assert intent is not None
    assert intent.actions[0].action == ActionType.CLICK
    assert intent.actions[0].target.role == "link"
    assert intent.actions[0].target.name == "Login"


def test_click_no_role():
    """Click without recognized role should use text targeting."""
    intent = generate_heuristic_intent("Click the Save icon", 1)
    assert intent is not None
    assert intent.actions[0].action == ActionType.CLICK
    assert intent.actions[0].target is not None
    assert intent.actions[0].target.text == "Save icon"


def test_input_pattern():
    """Input step should extract value and field target."""
    intent = generate_heuristic_intent(
        "Enter 'test@example.com' in the Email field", 1
    )
    assert intent is not None
    assert intent.actions[0].action == ActionType.INPUT
    assert intent.actions[0].value == "test@example.com"
    assert intent.actions[0].target is not None
    assert intent.actions[0].target.label == "Email"


def test_input_into():
    """'Type ... into' should also match input pattern."""
    intent = generate_heuristic_intent("Type 'hello' into the search box", 1)
    assert intent is not None
    assert intent.actions[0].action == ActionType.INPUT
    assert intent.actions[0].value == "hello"


def test_assert_text_pattern():
    """Assert text should produce WAIT_FOR_TEXT action."""
    intent = generate_heuristic_intent("Verify page displays 'Welcome back'", 1)
    assert intent is not None
    assert intent.actions[0].action == ActionType.WAIT_FOR_TEXT
    assert intent.actions[0].value == "Welcome back"


def test_assert_visible_pattern():
    """Assert visible should produce ASSERT_VISIBLE action."""
    intent = generate_heuristic_intent("Verify the form is visible", 1)
    assert intent is not None
    assert intent.actions[0].action == ActionType.ASSERT_VISIBLE
    assert intent.actions[0].target is not None


def test_complex_returns_none():
    """Complex actions should return None (requires AI)."""
    assert generate_heuristic_intent("Select March 15 from the calendar", 1) is None
    assert generate_heuristic_intent("Upload the test.pdf file", 1) is None
    assert generate_heuristic_intent("Choose 'Option A' from the dropdown", 1) is None


def test_empty_description_returns_none():
    """Empty or whitespace description should return None."""
    assert generate_heuristic_intent("", 1) is None
    assert generate_heuristic_intent("   ", 1) is None


def test_heuristic_description_prefix():
    """Generated intent description should have [heuristic] prefix."""
    intent = generate_heuristic_intent("Click the OK button", 1)
    assert intent is not None
    assert intent.description.startswith("[heuristic]")
