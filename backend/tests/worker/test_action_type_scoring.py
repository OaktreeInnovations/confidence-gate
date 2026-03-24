"""Tests for action-type aware scoring penalties and used-element penalties.

Validates that the scoring pipeline penalizes element/action mismatches:
- input action on a button → high penalty
- click action on a button → no penalty
- No action hint → no penalty
- Unknown action type → no penalty

Also validates used-element penalty:
- Candidate matching a previously-used selector path → 0.5 penalty
- Candidate not matching → no penalty
- No used paths → no penalty
"""

from app.worker.selector_engine.scoring import _action_type_penalty, _used_element_penalty


def test_input_action_penalizes_button():
    """input action on <button> → 0.35 penalty."""
    meta = {"tag": "button", "role": "button"}
    penalty = _action_type_penalty(meta, "input password123")
    assert penalty == 0.35


def test_input_action_favors_input_element():
    """<input> gets 0 penalty, <button> gets 0.35 → input wins."""
    input_meta = {"tag": "input", "role": "textbox"}
    button_meta = {"tag": "button", "role": "button"}

    input_penalty = _action_type_penalty(input_meta, "input password")
    button_penalty = _action_type_penalty(button_meta, "input password")

    assert input_penalty == 0.0
    assert button_penalty == 0.35
    assert input_penalty < button_penalty


def test_click_action_no_penalty_on_button():
    """click action on <button> → 0 penalty (compatible)."""
    meta = {"tag": "button", "role": "button"}
    penalty = _action_type_penalty(meta, "click")
    assert penalty == 0.0


def test_no_action_hint_no_penalty():
    """Empty action_hint → 0 penalty on any element."""
    meta = {"tag": "button", "role": "button"}
    assert _action_type_penalty(meta, "") == 0.0


def test_select_action_compatible_with_combobox():
    """select action on role=combobox → 0 penalty."""
    meta = {"tag": "div", "role": "combobox"}
    penalty = _action_type_penalty(meta, "select Option A")
    assert penalty == 0.0


def test_unknown_action_no_penalty():
    """Unknown action type (assert_text) → 0 penalty."""
    meta = {"tag": "span", "role": ""}
    penalty = _action_type_penalty(meta, "assert_text some text")
    assert penalty == 0.0


# --- Used-element penalty tests ---


def test_used_element_penalty_matches():
    """Candidate matching a used selector path → 0.5 penalty."""
    meta = {"selector_path": 'div > form > input[name="email"]'}
    used = frozenset({'div > form > input[name="email"]'})
    assert _used_element_penalty(meta, used) == 0.5


def test_used_element_penalty_no_match():
    """Candidate NOT matching any used path → 0 penalty."""
    meta = {"selector_path": 'div > form > input[name="password"]'}
    used = frozenset({'div > form > input[name="email"]'})
    assert _used_element_penalty(meta, used) == 0.0


def test_used_element_penalty_no_used_paths():
    """No used paths → 0 penalty."""
    meta = {"selector_path": 'div > form > input[name="email"]'}
    assert _used_element_penalty(meta, None) == 0.0
    assert _used_element_penalty(meta, frozenset()) == 0.0


def test_used_element_penalty_empty_path():
    """Candidate with empty selector_path → 0 penalty (no match possible)."""
    meta = {"selector_path": ""}
    used = frozenset({'div > form > input[name="email"]'})
    assert _used_element_penalty(meta, used) == 0.0
