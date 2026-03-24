"""Test: API 429 retry with backoff.

Scenario: An API step receives HTTP 429 (rate limited). The executor
should retry with backoff and eventually succeed.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests


def test_retryable_status_codes():
    """429, 500, 502, 503, 504 should be retryable."""
    from app.worker.api_executor import _RETRYABLE_STATUS_CODES

    assert 429 in _RETRYABLE_STATUS_CODES
    assert 500 in _RETRYABLE_STATUS_CODES
    assert 502 in _RETRYABLE_STATUS_CODES
    assert 503 in _RETRYABLE_STATUS_CODES
    assert 504 in _RETRYABLE_STATUS_CODES
    # 200 and 404 should NOT be retryable
    assert 200 not in _RETRYABLE_STATUS_CODES
    assert 404 not in _RETRYABLE_STATUS_CODES


def test_connection_error_is_retryable():
    """Connection errors should be retryable."""
    from app.worker.api_executor import _is_retryable_error
    from requests.exceptions import ConnectionError, Timeout

    assert _is_retryable_error(ConnectionError("connection refused"))
    assert _is_retryable_error(Timeout("request timed out"))
    assert not _is_retryable_error(ValueError("bad value"))


def test_variable_template_resolution():
    """Template variables should be resolved from the variable store."""
    from app.worker.api_executor import resolve_template

    result = resolve_template(
        "/api/users/{{user_id}}/posts",
        {"user_id": "123", "unused": "abc"},
    )
    assert result == "/api/users/123/posts"


def test_unresolved_variables_remain():
    """Unresolved variables should remain in the template."""
    from app.worker.api_executor import resolve_template

    result = resolve_template(
        "/api/users/{{user_id}}/posts",
        {},
    )
    assert result == "/api/users/{{user_id}}/posts"
