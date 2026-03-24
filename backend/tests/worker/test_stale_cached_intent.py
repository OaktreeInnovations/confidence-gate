"""Test: Stale cached intent — regeneration produces new intent on retry.

Scenario: A cached intent from a previous run fails because the page
layout changed. The retry loop should regenerate a new intent.
"""

from app.worker.intent_schema import (
    ActionIntent,
    ActionType,
    StepIntent,
    TargetDescriptor,
)


def test_intent_json_roundtrip():
    """StepIntent should serialize to JSON and back without data loss."""
    intent = StepIntent(
        step_number=1,
        description="Fill email and click submit",
        actions=[
            ActionIntent(
                action=ActionType.INPUT,
                target=TargetDescriptor(label="Email"),
                value="test@example.com",
            ),
            ActionIntent(
                action=ActionType.CLICK,
                target=TargetDescriptor(role="button", name="Submit"),
            ),
        ],
    )

    json_str = intent.to_json_str()
    restored = StepIntent.from_json_str(json_str)

    assert restored.description == intent.description
    assert len(restored.actions) == 2
    assert restored.actions[0].action == ActionType.INPUT
    assert restored.actions[0].value == "test@example.com"
    assert restored.actions[1].action == ActionType.CLICK


def test_intent_hash_differs_for_different_targets():
    """Different targets should produce different intent hashes."""
    from app.intelligence.code_reuse import code_hash

    intent_a = StepIntent(
        step_number=1,
        description="Click A",
        actions=[ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(role="button", name="A"))],
    )
    intent_b = StepIntent(
        step_number=1,
        description="Click B",
        actions=[ActionIntent(action=ActionType.CLICK, target=TargetDescriptor(role="button", name="B"))],
    )

    hash_a = code_hash(intent_a.to_json_str())
    hash_b = code_hash(intent_b.to_json_str())

    assert hash_a != hash_b


def test_extract_value_action_type():
    """EXTRACT_VALUE should be a valid action type."""
    intent = StepIntent(
        step_number=1,
        description="Extract order ID",
        actions=[
            ActionIntent(
                action=ActionType.EXTRACT_VALUE,
                target=TargetDescriptor(role="heading", name="Order ID"),
                value="order_id",
            ),
        ],
    )

    assert intent.actions[0].action == ActionType.EXTRACT_VALUE
    json_str = intent.to_json_str()
    assert "extract_value" in json_str


def test_deterministic_backoff():
    """Deterministic backoff should produce consistent results."""
    from app.worker.ai_executor import _deterministic_backoff

    # attempt 0: base_s * 2^0 = 1.0
    b0 = _deterministic_backoff(0)
    b0_again = _deterministic_backoff(0)
    assert b0 == b0_again  # Deterministic — no random jitter

    # attempt 1: base_s * 2^1 = 2.0
    b1 = _deterministic_backoff(1)
    assert b1 > b0

    # Should be capped at max_s
    b_large = _deterministic_backoff(100)
    assert b_large <= 10.0  # Default max_s


def test_page_state_hash_deterministic():
    """Page state hashing should be deterministic for same inputs."""
    from app.worker.ai_executor import _hash_page_state

    h1 = _hash_page_state('{"action": "click"}', "https://example.com")
    h2 = _hash_page_state('{"action": "click"}', "https://example.com")
    h3 = _hash_page_state('{"action": "fill"}', "https://example.com")

    assert h1 == h2
    assert h1 != h3
