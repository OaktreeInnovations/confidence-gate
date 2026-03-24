"""Tests for target-coherence penalty in selector scoring.

Validates that _target_coherence_penalty detects cross-attribute
contradictions in the TargetDescriptor and penalizes candidates
whose metadata conflicts with target fields.
"""

import pytest

from app.worker.selector_engine.scoring import _target_coherence_penalty
from app.worker.selector_resilience import SelectorStrategy


class TestTargetCoherencePenalty:
    """Unit tests for _target_coherence_penalty()."""

    def test_placeholder_contradicts_label(self):
        """PLACEHOLDER match penalized when element label differs from target label."""
        meta = {
            "aria_label": "Email",
            "placeholder": "you@example.com",
        }
        target_fields = {
            "label": "Password",
            "placeholder": "you@example.com",
        }
        penalty = _target_coherence_penalty(
            meta, SelectorStrategy.PLACEHOLDER, target_fields,
        )
        # Element's aria_label="Email" contradicts target label="Password"
        assert penalty == pytest.approx(0.3)

    def test_no_penalty_single_field(self):
        """No penalty when target has only one field."""
        meta = {
            "aria_label": "Email",
            "placeholder": "you@example.com",
        }
        target_fields = {
            "placeholder": "you@example.com",
        }
        penalty = _target_coherence_penalty(
            meta, SelectorStrategy.PLACEHOLDER, target_fields,
        )
        assert penalty == 0.0

    def test_no_penalty_consistent(self):
        """No penalty when element attributes match all target fields."""
        meta = {
            "aria_label": "Password",
            "placeholder": "Enter password",
        }
        target_fields = {
            "label": "Password",
            "placeholder": "Enter password",
        }
        penalty = _target_coherence_penalty(
            meta, SelectorStrategy.LABEL, target_fields,
        )
        assert penalty == 0.0

    def test_no_penalty_missing_attribute(self):
        """Element missing an attribute is not a contradiction."""
        meta = {
            "aria_label": "Password",
            # no placeholder in element metadata
        }
        target_fields = {
            "label": "Password",
            "placeholder": "you@example.com",
        }
        penalty = _target_coherence_penalty(
            meta, SelectorStrategy.LABEL, target_fields,
        )
        # placeholder field checked but element has no placeholder → not a contradiction
        assert penalty == 0.0

    def test_label_contradicts_placeholder(self):
        """LABEL match penalized when element placeholder differs from target placeholder."""
        meta = {
            "aria_label": "Email",
            "placeholder": "you@example.com",
        }
        target_fields = {
            "label": "Email",
            "placeholder": "Enter password",
        }
        penalty = _target_coherence_penalty(
            meta, SelectorStrategy.LABEL, target_fields,
        )
        # Element placeholder="you@example.com" contradicts target placeholder="Enter password"
        assert penalty == pytest.approx(0.3)

    def test_penalty_capped(self):
        """Total penalty never exceeds 0.4."""
        # Element contradicts multiple target fields
        meta = {
            "aria_label": "Username",
            "placeholder": "wrong@placeholder.com",
            "text": "Wrong Text",
            "role": "textbox",
        }
        target_fields = {
            "label": "Password",
            "placeholder": "Enter password",
            "text": "Correct Text",
            "role": "textbox",
        }
        penalty = _target_coherence_penalty(
            meta, SelectorStrategy.ROLE, target_fields,
        )
        # Multiple contradictions (label, placeholder, text) but capped at 0.4
        assert penalty <= 0.4
        assert penalty > 0.0

    def test_associated_label_detects_contradiction(self):
        """Contradiction detected via associated_label when aria_label is empty.

        This is the real-world case: <input> elements have no aria-label or
        text content, but their associated <label for="..."> element has the
        label text.  The coherence penalty must check associated_label to
        detect when the element's label differs from the target's label.
        """
        meta = {
            "aria_label": "",        # input has no explicit aria-label
            "text": "",              # input has no textContent
            "placeholder": "you@example.com",
            "associated_label": "Email",  # from <label for="email">Email</label>
        }
        target_fields = {
            "label": "Password",
            "placeholder": "you@example.com",
        }
        penalty = _target_coherence_penalty(
            meta, SelectorStrategy.PLACEHOLDER, target_fields,
        )
        # associated_label="Email" contradicts target label="Password"
        assert penalty == pytest.approx(0.3)

    def test_associated_label_no_penalty_when_matching(self):
        """No penalty when associated_label matches the target label."""
        meta = {
            "aria_label": "",
            "text": "",
            "placeholder": "Enter password",
            "associated_label": "Password",
        }
        target_fields = {
            "label": "Password",
            "placeholder": "Enter password",
        }
        penalty = _target_coherence_penalty(
            meta, SelectorStrategy.PLACEHOLDER, target_fields,
        )
        assert penalty == 0.0
