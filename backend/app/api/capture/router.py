"""Quick Capture API — browser recording session management.

Endpoints:
  POST /capture/sessions                     — create session, return session_id + script URL
  POST /capture/sessions/{id}/events         — receive raw events from the bookmarklet
  GET  /capture/sessions/{id}                — poll session state (steps, event count)
  POST /capture/sessions/{id}/stop           — stop recording, transform events → steps
  POST /capture/sessions/{id}/save           — save as TestCase, delete session
  GET  /capture/script                       — serve the capture bookmarklet JS
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import urlparse

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
import structlog

from app.api.auth.dependencies import get_current_user
from app.capture.assertion_engine import enrich_intents_with_assertions
from app.capture.intent_normalizer import normalize_intents
from app.capture.recording_transformer import transform_events_to_intents
from app.capture.session_store import (
    append_events,
    create_session,
    delete_session,
    get_session,
    store_steps,
)
from app.capture.stability_enhancer import enhance_stability
from app.dependencies import get_db, get_redis_client
from app.models import (
    TestCase,
    TestCasePriority,
    TestCaseStatus,
    TestStep,
    TestType,
    User,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/capture", tags=["capture"])


# ── Request / Response schemas ─────────────────────────────────────────────────


class CreateSessionRequest(BaseModel):
    project_id: str
    base_url: str = ""
    title: str = ""


class CreateSessionResponse(BaseModel):
    session_id: str
    script_url: str


class EventsPayload(BaseModel):
    events: list[dict] = Field(default_factory=list)


class SessionStateResponse(BaseModel):
    session_id: str
    project_id: str
    base_url: str
    title: str
    started_at: str
    stopped_at: str | None
    event_count: int
    steps: list[dict]


class StopResponse(BaseModel):
    session_id: str
    step_count: int
    steps: list[dict]


class SaveRequest(BaseModel):
    title: str = ""
    base_url: str = ""


class SaveResponse(BaseModel):
    test_case_id: str


# ── Helpers ────────────────────────────────────────────────────────────────────


def _require_session(session: dict | None, session_id: str) -> dict:
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Capture session {session_id!r} not found or expired",
        )
    return session


async def _project_org_id(db: AsyncIOMotorDatabase, project_id: str) -> ObjectId:
    """Return the org_id for a project, or raise 404."""
    try:
        oid = ObjectId(project_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid project_id")
    doc = await db.projects.find_one({"_id": oid}, {"org_id": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found")
    return doc["org_id"]


def _pipeline(events: list[dict]) -> tuple[list[dict], int]:
    """Run the full transform → assert → stabilise → normalise pipeline.

    Returns (serialised step dicts, step count).
    """
    intents = transform_events_to_intents(events)
    intents = enrich_intents_with_assertions(intents, events)
    intents = enhance_stability(intents)
    intents = normalize_intents(intents)
    steps = [s.model_dump(exclude_none=True) for s in intents]
    return steps, len(steps)


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.post("/sessions", response_model=CreateSessionResponse, status_code=201)
async def create_capture_session(
    body: CreateSessionRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> CreateSessionResponse:
    """Start a new recording session."""
    # Verify project exists
    await _project_org_id(db, body.project_id)

    session_id = await create_session(
        redis=redis,
        project_id=body.project_id,
        user_id=str(current_user.id),
        base_url=body.base_url,
        title=body.title,
    )

    # The script URL is relative — the frontend knows the API base URL
    script_url = f"/api/capture/script?session_id={session_id}"

    logger.info(
        "capture.session_started",
        session_id=session_id,
        project_id=body.project_id,
        user_id=str(current_user.id),
    )
    return CreateSessionResponse(session_id=session_id, script_url=script_url)


@router.post("/sessions/{session_id}/events", status_code=202)
async def receive_events(
    session_id: str,
    request: Request,
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
) -> Response:
    """Accept raw browser events from the bookmarklet.

    Accepts both application/json and text/plain (text/plain avoids CORS preflight
    so the capture script can POST cross-origin without needing an OPTIONS round-trip).
    No auth — session_id acts as the bearer token.
    """
    _cors = {"Access-Control-Allow-Origin": "*"}
    try:
        raw = await request.body()
        data = json.loads(raw) if raw else {}
    except Exception:
        return Response(status_code=400, headers=_cors)

    events = data.get("events", [])
    if not events:
        return Response(content='{"accepted":0}', media_type="application/json", headers=_cors)

    try:
        count = await append_events(redis, session_id, events)
    except KeyError:
        return Response(status_code=404, headers=_cors)

    payload = json.dumps({"accepted": len(events), "total": count})
    return Response(content=payload, media_type="application/json", status_code=202, headers=_cors)


@router.get("/sessions/{session_id}", response_model=SessionStateResponse)
async def get_session_state(
    session_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
) -> SessionStateResponse:
    """Poll current session state (steps available after /stop)."""
    session = _require_session(await get_session(redis, session_id), session_id)
    return SessionStateResponse(
        session_id=session["session_id"],
        project_id=session["project_id"],
        base_url=session.get("base_url", ""),
        title=session.get("title", ""),
        started_at=session.get("started_at", ""),
        stopped_at=session.get("stopped_at") or None,
        event_count=session["event_count"],
        steps=session["steps"],
    )


@router.post("/sessions/{session_id}/stop", response_model=StopResponse)
async def stop_session(
    session_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
) -> StopResponse:
    """Stop recording and transform captured events into StepIntents."""
    session = _require_session(await get_session(redis, session_id), session_id)

    raw_events: list[dict] = session["raw_events"]
    steps, step_count = _pipeline(raw_events)

    stopped_at = datetime.now(timezone.utc).isoformat()
    await store_steps(redis, session_id, steps, stopped_at)

    logger.info(
        "capture.session_stopped",
        session_id=session_id,
        events=len(raw_events),
        steps=step_count,
    )
    return StopResponse(session_id=session_id, step_count=step_count, steps=steps)


@router.post("/sessions/{session_id}/save", response_model=SaveResponse, status_code=201)
async def save_session(
    session_id: str,
    body: SaveRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> SaveResponse:
    """Save captured steps as a TestCase and clean up the session."""
    session = _require_session(await get_session(redis, session_id), session_id)

    steps_data: list[dict] = session["steps"]
    if not steps_data:
        raise HTTPException(
            status_code=400,
            detail="No steps captured. Call /stop first to transform events.",
        )

    project_id_str = session["project_id"]
    org_id = await _project_org_id(db, project_id_str)

    title = body.title or session.get("title") or "Captured Test"
    base_url = body.base_url or session.get("base_url", "")

    # Build TestStep list — StepIntent JSON is stored in custom_code
    test_steps: list[TestStep] = []
    for raw_step in steps_data:
        step_number = raw_step.get("step_number", len(test_steps) + 1)
        description = raw_step.get("description", "")
        # Serialise the full StepIntent into custom_code for the executor bypass
        custom_code = json.dumps(raw_step)
        test_steps.append(
            TestStep(
                step_number=step_number,
                action=description,
                expected="",
                custom_code=custom_code,
            )
        )

    test_case = TestCase(
        org_id=org_id,
        project_id=ObjectId(project_id_str),
        test_type=TestType.UI,
        base_url=base_url,
        title=title,
        description="Created via Quick Capture",
        steps=test_steps,
        priority=TestCasePriority.MEDIUM,
        status=TestCaseStatus.DRAFT,
        tags=["captured"],
        created_by=current_user.id,
    )

    doc = test_case.model_dump(by_alias=True, exclude={"id"})
    result = await db.test_cases.insert_one(doc)
    test_case_id = str(result.inserted_id)

    await delete_session(redis, session_id)

    logger.info(
        "capture.session_saved",
        session_id=session_id,
        test_case_id=test_case_id,
        steps=len(test_steps),
    )
    return SaveResponse(test_case_id=test_case_id)


@router.get("/script")
async def serve_capture_script(session_id: str) -> Response:
    """Serve the browser capture bookmarklet JavaScript for a given session."""
    script = _build_capture_script(session_id)
    return Response(
        content=script,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/browser/{session_id}")
async def open_browser(session_id: str, url: str) -> Response:
    """Proxy the target URL, inject the capture script, and return instrumented HTML.

    The frontend opens this endpoint via window.open() so the user interacts
    with their app in a new tab — no DevTools or bookmarklet needed.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="url must be http or https")

    origin = f"{parsed.scheme}://{parsed.netloc}"

    try:
        html = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_html, url
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch {url}: {exc}") from exc

    instrumented = _instrument_html(html, session_id, origin)
    return Response(
        content=instrumented,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (ConfidenceGate Capture)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        charset = "utf-8"
        ct = resp.headers.get_content_charset()
        if ct:
            charset = ct
        return resp.read().decode(charset, errors="replace")


def _instrument_html(html: str, session_id: str, origin: str) -> str:
    """Inject <base> tag + capture script into the HTML page."""
    base_tag = f'<base href="{origin}/">'
    script_tag = (
        f'<script src="http://localhost:8001/api/capture/script'
        f'?session_id={session_id}"></script>'
    )

    # Strip Content-Security-Policy meta tags that would block our script
    import re
    html = re.sub(
        r'<meta[^>]+http-equiv=["\']Content-Security-Policy["\'][^>]*>',
        '',
        html,
        flags=re.IGNORECASE,
    )

    # Inject <base> right after <head> (fixes relative URLs)
    if "<head>" in html.lower():
        html = re.sub(r"(<head[^>]*>)", rf"\1{base_tag}", html, count=1, flags=re.IGNORECASE)
    else:
        html = base_tag + html

    # Inject capture script before </body>
    if "</body>" in html.lower():
        html = re.sub(r"</body>", f"{script_tag}</body>", html, count=1, flags=re.IGNORECASE)
    else:
        html += script_tag

    return html


# ── Capture Script ─────────────────────────────────────────────────────────────


def _build_capture_script(session_id: str) -> str:
    """Generate the capture script with the session_id baked in.

    Event delivery strategy (in priority order):
    1. postMessage → window.opener  (works when opened via Confidence Gate window.open,
                                     avoids mixed-content blocks on HTTPS sites)
    2. Direct fetch POST text/plain  (fallback for same-origin or HTTP sites)
    """
    return f"""
(function() {{
  'use strict';

  if (window.__CG_CAPTURE_ACTIVE__) {{
    console.info('[ConfidenceGate] already active');
    return;
  }}
  window.__CG_CAPTURE_ACTIVE__ = true;

  var SESSION_ID = {repr(session_id)};
  var ENDPOINT = 'http://localhost:8001/api/capture/sessions/' + SESSION_ID + '/events';
  var queue = [];
  var flushTimer = null;

  function ts() {{ return Date.now(); }}

  function captureElement(el) {{
    if (!el) return {{}};
    var tag = (el.tagName || '').toLowerCase();
    // Walk up to find meaningful ancestor for generic elements
    var labelEl = el;
    if (tag !== 'button' && tag !== 'a' && tag !== 'input' && tag !== 'select' && tag !== 'textarea') {{
      var ancestor = el.closest('button,a,[role="button"],[role="link"]');
      if (ancestor) labelEl = ancestor;
    }}
    return {{
      tag: (labelEl.tagName || '').toLowerCase(),
      id: labelEl.id || '',
      name: labelEl.name || '',
      role: labelEl.getAttribute('role') || '',
      aria_label: labelEl.getAttribute('aria-label') || '',
      placeholder: labelEl.placeholder || '',
      text: (labelEl.textContent || labelEl.innerText || '').trim().slice(0, 200),
      value: labelEl.value || '',
      checked: labelEl.checked || false,
      input_type: labelEl.type || '',
      selected_text: labelEl.options && labelEl.selectedIndex >= 0
        ? labelEl.options[labelEl.selectedIndex].text : '',
    }};
  }}

  function capturePage() {{
    return {{ url: location.href, title: document.title }};
  }}

  function enqueue(type, element, extra) {{
    queue.push(Object.assign({{ type: type, timestamp: ts(),
      element: element || {{}}, page: capturePage() }}, extra || {{}}));
    if (!flushTimer) flushTimer = setTimeout(flush, 800);
  }}

  function flush() {{
    flushTimer = null;
    if (!queue.length) return;
    var batch = queue.splice(0, queue.length);

    // Strategy 1: postMessage to opener (no mixed-content issues)
    if (window.opener && !window.opener.closed) {{
      try {{
        window.opener.postMessage(
          {{ type: '__cg_events', session_id: SESSION_ID, events: batch }},
          '*'
        );
        return;
      }} catch(e) {{}}
    }}

    // Strategy 2: direct POST (works for HTTP sites)
    try {{
      fetch(ENDPOINT, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'text/plain' }},
        body: JSON.stringify({{ events: batch }}),
      }}).catch(function() {{}});
    }} catch(e) {{}}
  }}

  function flushSync() {{
    if (!queue.length) return;
    var batch = queue.splice(0, queue.length);
    if (window.opener && !window.opener.closed) {{
      try {{ window.opener.postMessage({{ type: '__cg_events', session_id: SESSION_ID, events: batch }}, '*'); return; }} catch(e) {{}}
    }}
    // sendBeacon as last-resort on unload (text/plain, no preflight)
    if (navigator.sendBeacon) {{
      navigator.sendBeacon(ENDPOINT, JSON.stringify({{ events: batch }}));
    }}
  }}

  // ── Event listeners ──────────────────────────────────────────────────────

  document.addEventListener('click', function(e) {{
    enqueue('click', captureElement(e.target));
  }}, true);

  document.addEventListener('input', function(e) {{
    enqueue('input', captureElement(e.target));
  }}, true);

  document.addEventListener('change', function(e) {{
    enqueue('change', captureElement(e.target));
  }}, true);

  document.addEventListener('submit', function(e) {{
    enqueue('submit', captureElement(e.target));
  }}, true);

  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Enter') enqueue('keypress', captureElement(e.target), {{ key: 'Enter' }});
  }}, true);

  // ── Navigation ───────────────────────────────────────────────────────────

  var _push = history.pushState.bind(history);
  history.pushState = function() {{ _push.apply(history, arguments); enqueue('navigation', null, {{ page: capturePage() }}); }};

  var _replace = history.replaceState.bind(history);
  history.replaceState = function() {{ _replace.apply(history, arguments); enqueue('navigation', null, {{ page: capturePage() }}); }};

  window.addEventListener('popstate', function() {{ enqueue('navigation', null, {{ page: capturePage() }}); }});
  window.addEventListener('beforeunload', flushSync);

  // Capture current page as starting point
  enqueue('navigation', null, {{ page: capturePage() }});

  console.info('[ConfidenceGate] Quick Capture active — session:', SESSION_ID);
}})();
""".strip()
