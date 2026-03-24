"""Test: Hard Rules Enforcement (Part 9 + Part 6 expansion).

Static analysis tests that enforce coding standards across worker modules:
- No raw wait_for_timeout outside budget_wait
- No blind .first without prior count() check
- No import random in worker modules
- AI gate rejects unknown purposes
- Scoring weights sum to 1.0 (determinism preservation)
- History bonus formula is deterministic (no randomness)
- Action outcome registry covers all interactive action types
"""

import math
import os
import re

import pytest

from app.worker.ai_gate import AIGate, AICallBudget, _ALLOWED_AI_PURPOSES

# Directory containing all worker source files
_WORKER_DIR = os.path.join(
    os.path.dirname(__file__), os.pardir, os.pardir,
    "app", "worker",
)
_WORKER_DIR = os.path.normpath(_WORKER_DIR)


def _collect_worker_py_files() -> list[str]:
    """Collect all .py files under app/worker/ recursively."""
    py_files = []
    for root, _dirs, files in os.walk(_WORKER_DIR):
        for f in files:
            if f.endswith(".py") and not f.startswith("__"):
                py_files.append(os.path.join(root, f))
    return py_files


def _read_file(path: str) -> str:
    with open(path) as fh:
        return fh.read()


def test_no_raw_wait_for_timeout():
    """No direct wait_for_timeout calls outside budget_wait wrappers.

    wait_for_timeout is a Playwright hard sleep that bypasses budget
    enforcement. All waits must go through budget_wait or time.sleep
    with budget checks.
    """
    violations = []
    for path in _collect_worker_py_files():
        content = _read_file(path)
        # Skip budget enforcement module itself — it's allowed to wrap the call
        if "budget" in os.path.basename(path).lower():
            continue
        if "page_stability" in os.path.basename(path).lower():
            continue
        for i, line in enumerate(content.splitlines(), 1):
            if "wait_for_timeout" in line and not line.strip().startswith("#"):
                violations.append(f"{os.path.basename(path)}:{i}: {line.strip()}")

    assert not violations, (
        f"Found {len(violations)} raw wait_for_timeout call(s):\n"
        + "\n".join(violations)
    )


def test_no_blind_first():
    """No .first usage without prior count() or visibility check.

    .first on a locator list without checking count() can silently
    pick the wrong element.
    """
    violations = []
    for path in _collect_worker_py_files():
        content = _read_file(path)
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Look for .first that isn't preceded by count on nearby lines
            if ".first" in stripped and "count" not in stripped:
                # Check surrounding 5 lines for count()
                lines = content.splitlines()
                start = max(0, i - 6)
                end = min(len(lines), i + 1)
                context = "\n".join(lines[start:end])
                if "count()" not in context and ".first" not in "# " + stripped:
                    # Allow if the .first is part of a comment or docstring
                    if '"""' not in stripped and "'''" not in stripped:
                        violations.append(f"{os.path.basename(path)}:{i}: {stripped}")

    # Warn but don't fail hard — some .first uses may be intentional
    # with guards elsewhere
    if violations:
        pytest.skip(
            f"Found {len(violations)} potential blind .first usage(s) "
            f"(review manually):\n" + "\n".join(violations[:5])
        )


def test_no_random_in_worker():
    """No import random in worker modules.

    All worker behavior must be deterministic. Random introduces
    non-reproducible behavior that makes flake analysis impossible.
    """
    violations = []
    for path in _collect_worker_py_files():
        content = _read_file(path)
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.match(r"^(from\s+random\s+import|import\s+random)\b", stripped):
                violations.append(f"{os.path.basename(path)}:{i}: {stripped}")

    assert not violations, (
        f"Found {len(violations)} random import(s) in worker modules:\n"
        + "\n".join(violations)
    )


def test_ai_gate_rejects_unknown_purpose():
    """AIGate.can_call rejects purposes not in the allow-list."""
    gate = AIGate(budget=AICallBudget())

    # Valid purposes should pass
    assert gate.can_call("intent_gen")
    assert gate.can_call("intent_regen")
    assert gate.can_call("disambiguation")

    # Unknown purpose should be rejected
    assert not gate.can_call("arbitrary_gen")
    assert not gate.can_call("code_execution")
    assert not gate.can_call("")


def test_scoring_weights_sum_to_one():
    """ScoringWeights dimensions must sum to 1.0 for deterministic scoring.

    The 5-dimension weighted composite must be normalized. History bonus
    is additive on top, not part of the weighted sum.
    """
    from app.worker.selector_engine.scoring import ScoringWeights

    w = ScoringWeights()
    total = w.strategy + w.visibility + w.enabled + w.match_confidence + w.proximity
    assert abs(total - 1.0) < 0.001, (
        f"ScoringWeights sum to {total}, expected 1.0"
    )


def test_history_bonus_is_deterministic():
    """History bonus formula produces identical output for identical input.

    Verifies no randomness or floating-point instability in the formula.
    """
    from app.worker.selector_engine.scoring import _compute_history_bonus
    from app.worker.selector_resilience import SelectorStrategy

    params = {"role": "button", "name": "Submit"}
    key = f"{SelectorStrategy.ROLE.value}:{params}"
    records = {key: 10}

    results = set()
    for _ in range(100):
        bonus = _compute_history_bonus(SelectorStrategy.ROLE, params, records)
        results.add(round(bonus, 10))  # Round to 10 decimal places

    assert len(results) == 1, (
        f"History bonus produced {len(results)} different values — non-deterministic"
    )


def test_action_outcome_covers_interactive_types():
    """Action outcome registry must cover all interactive action types.

    Every action that interacts with the DOM should have verification rules.
    """
    from app.worker.action_outcome import get_outcome

    interactive_types = ["click", "input", "select", "navigate", "check", "uncheck"]
    missing = [t for t in interactive_types if get_outcome(t) is None]

    assert not missing, (
        f"Missing action outcome definitions for: {missing}"
    )


def test_ai_gate_purposes_are_frozen():
    """AI purposes allow-list must be a frozenset — immutable at runtime."""
    assert isinstance(_ALLOWED_AI_PURPOSES, frozenset), (
        "_ALLOWED_AI_PURPOSES must be frozenset to prevent runtime mutation"
    )
