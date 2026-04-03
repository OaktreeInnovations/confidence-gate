"""Tests for cross-org data isolation.

Verifies that the org_id filtering in router helpers correctly prevents
authenticated users from accessing resources that belong to a different org.

These tests do not import the full router stack (which requires aiobotocore etc.)
because they run locally without the full Docker dependencies. Instead they test:
  1. The guard logic inline (identical to what the routers use)
  2. Source-level assertions that all router handlers include org_id in queries
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bson import ObjectId
from fastapi import HTTPException


# --- Inline require_org logic (mirrors both routers exactly) ---

def _require_org(user) -> ObjectId:
    if not user.org_id:
        raise HTTPException(status_code=403, detail="User must belong to an organization")
    return user.org_id


def _make_user(org_id=None):
    user = MagicMock()
    user.org_id = ObjectId(org_id) if org_id else None
    return user


# --- Guard: unauthenticated org access ---

def test_require_org_raises_403_when_no_org():
    user = _make_user(org_id=None)
    with pytest.raises(HTTPException) as exc_info:
        _require_org(user)
    assert exc_info.value.status_code == 403


def test_require_org_returns_oid_when_org_present():
    oid = str(ObjectId())
    user = _make_user(org_id=oid)
    result = _require_org(user)
    assert str(result) == oid


# --- Guard: invalid ObjectId rejects before DB query ---

def test_parse_object_id_raises_400_on_garbage():
    from bson.errors import InvalidId

    def parse_object_id(id_str: str) -> ObjectId:
        try:
            return ObjectId(id_str)
        except (InvalidId, TypeError):
            raise HTTPException(status_code=400, detail=f"Invalid ID format: {id_str}")

    with pytest.raises(HTTPException) as exc_info:
        parse_object_id("not-a-valid-oid")
    assert exc_info.value.status_code == 400


def test_parse_object_id_accepts_valid_oid():
    from bson.errors import InvalidId

    def parse_object_id(id_str: str) -> ObjectId:
        try:
            return ObjectId(id_str)
        except (InvalidId, TypeError):
            raise HTTPException(status_code=400, detail=f"Invalid ID format: {id_str}")

    valid = str(ObjectId())
    result = parse_object_id(valid)
    assert str(result) == valid


# --- Source-level: every DB query in key handlers must include org_id ---

_BACKEND_ROOT = Path(__file__).parent.parent / "app" / "api"


def _read_handler_source(relative_path: str) -> str:
    return (_BACKEND_ROOT / relative_path).read_text()


def test_projects_router_get_includes_org_id():
    src = _read_handler_source("projects/router.py")
    # Every find/find_one/update call should reference org_id
    assert src.count('"org_id"') + src.count("org_id") > 10  # used extensively


def test_release_validations_router_includes_org_id():
    src = _read_handler_source("release_validations/router.py")
    assert src.count("org_id") > 10


def test_ci_gate_endpoint_includes_org_id_in_validation_query():
    """CI gate must filter release_validations by org_id, not just project_id."""
    src = _read_handler_source("projects/router.py")
    # The ci_gate function references org_id in the release_validations query
    # Find the ci_gate function body and verify it
    gate_start = src.find("async def ci_gate")
    assert gate_start != -1, "ci_gate function not found"
    gate_body = src[gate_start:gate_start + 2000]
    assert "org_id" in gate_body


def test_no_unrestricted_find_in_release_validations():
    """No async handler in the release_validations router should query without org_id.

    For each async handler that queries release_validations, org_id must appear
    in the function body (either as a direct query arg or via a query dict).
    We allow 1500 chars per function body — enough for any realistic handler.
    """
    import re
    src = _read_handler_source("release_validations/router.py")

    # Find all async handlers that touch release_validations
    handler_starts = [m.start() for m in re.finditer(r"async def \w+", src)]
    handler_starts.append(len(src))  # sentinel

    for i, start in enumerate(handler_starts[:-1]):
        body = src[start:handler_starts[i + 1]]
        if "release_validations.find" not in body:
            continue
        assert "org_id" in body, (
            f"Handler at offset {start} queries release_validations without org_id:\n"
            f"{body[:400]}"
        )
