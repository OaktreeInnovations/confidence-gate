"""Tests for action-specific field validation in _parse_intent().

Validates:
- select_date without day → ValueError
- select_date with day → valid intent
- select without value → ValueError
- input without value → ValueError
- select_date value dict normalization
"""

import json

import pytest

from app.worker.intent_generator import _parse_intent


def _make_raw(actions, step_number=1, description="test"):
    return json.dumps({
        "step_number": step_number,
        "actions": actions,
        "description": description,
    })


def test_select_date_missing_day_rejected():
    """select_date without day or value → ValueError."""
    raw = _make_raw([{
        "action": "select_date",
        "target": {"label": "Start Date"},
    }])
    with pytest.raises(ValueError, match="select_date action 0 missing required 'day'"):
        _parse_intent(raw, 1)


def test_select_date_with_day_passes():
    """select_date with day → valid intent."""
    raw = _make_raw([{
        "action": "select_date",
        "target": {"label": "Start Date"},
        "day": "15",
    }])
    intent = _parse_intent(raw, 1)
    assert intent.actions[0].day == "15"


def test_select_missing_value_rejected():
    """select without value → ValueError."""
    raw = _make_raw([{
        "action": "select",
        "target": {"label": "Category"},
    }])
    with pytest.raises(ValueError, match="select action 0 missing required 'value'"):
        _parse_intent(raw, 1)


def test_input_missing_value_rejected():
    """input without value → ValueError."""
    raw = _make_raw([{
        "action": "input",
        "target": {"role": "textbox", "name": "Email"},
    }])
    with pytest.raises(ValueError, match="input action 0 missing required 'value'"):
        _parse_intent(raw, 1)


def test_select_date_value_dict_normalized():
    """select_date with value: {day: "15"} → normalized to day: "15"."""
    raw = _make_raw([{
        "action": "select_date",
        "target": {"label": "Start Date"},
        "value": {"day": "15", "month_label": "March 2026"},
    }])
    intent = _parse_intent(raw, 1)
    assert intent.actions[0].day == "15"
    assert intent.actions[0].month_label == "March 2026"
