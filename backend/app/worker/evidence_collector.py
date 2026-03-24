"""Enhanced evidence collection pipeline.

Captures rich evidence during step execution beyond just screenshots:
- Browser console logs (log/warn/error)
- DOM snapshot (serialized HTML)
- Network request/response summaries

All captures are non-fatal — if any capture fails, the step continues
with whatever evidence was successfully collected.

Evidence is uploaded to MinIO under:
    {org_id}/{test_run_id}/step_{N}.png         — screenshot
    {org_id}/{test_run_id}/step_{N}_console.json — console logs
    {org_id}/{test_run_id}/step_{N}_dom.html     — DOM snapshot
    {org_id}/{test_run_id}/step_{N}_network.json — network log
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import ConsoleMessage, Page, Request, Response

logger = structlog.get_logger(__name__)


@dataclass
class ConsoleEntry:
    """Single browser console message."""
    type: str  # "log", "warning", "error", "info", "debug"
    text: str
    timestamp_ms: int = 0


@dataclass
class NetworkEntry:
    """Single network request/response pair."""
    method: str
    url: str
    status: int = 0
    resource_type: str = ""
    duration_ms: int = 0
    request_size: int = 0
    response_size: int = 0
    failed: bool = False
    failure_text: str = ""


@dataclass
class StepEvidence:
    """Bundle of all evidence for a single step."""
    step_number: int
    screenshot: bytes | None = None
    console_logs: list[ConsoleEntry] = field(default_factory=list)
    dom_snapshot: str = ""
    network_log: list[NetworkEntry] = field(default_factory=list)


class EvidenceCollector:
    """Collects browser evidence during step execution.

    Lifecycle per step:
        1. start_step(step_number) — begin capturing console/network
        2. (step executes)
        3. finish_step(page) — capture DOM snapshot, return StepEvidence
        4. upload_evidence(s3, ..., evidence) — upload all files
    """

    def __init__(self) -> None:
        self._console_entries: list[ConsoleEntry] = []
        self._network_entries: list[NetworkEntry] = []
        self._request_times: dict[str, float] = {}
        self._current_step: int = 0
        self._page: Page | None = None
        self._collecting = False

    def start_step(self, page: Page, step_number: int) -> None:
        """Begin evidence collection for a step."""
        self._console_entries = []
        self._network_entries = []
        self._request_times = {}
        self._current_step = step_number
        self._page = page
        self._collecting = True

        try:
            page.on("console", self._on_console)
            page.on("request", self._on_request)
            page.on("response", self._on_response)
            page.on("requestfailed", self._on_request_failed)
        except Exception as e:
            logger.debug("evidence.listener_setup_failed", error=str(e)[:200])

    def _on_console(self, msg: ConsoleMessage) -> None:
        if not self._collecting:
            return
        try:
            self._console_entries.append(ConsoleEntry(
                type=msg.type,
                text=msg.text[:2000],
                timestamp_ms=int(time.time() * 1000),
            ))
        except Exception:
            pass

    def _on_request(self, request: Request) -> None:
        if not self._collecting:
            return
        try:
            self._request_times[request.url] = time.monotonic()
        except Exception:
            pass

    def _on_response(self, response: Response) -> None:
        if not self._collecting:
            return
        try:
            url = response.url
            start = self._request_times.pop(url, None)
            duration_ms = int((time.monotonic() - start) * 1000) if start else 0

            self._network_entries.append(NetworkEntry(
                method=response.request.method,
                url=url[:500],
                status=response.status,
                resource_type=response.request.resource_type,
                duration_ms=duration_ms,
            ))
        except Exception:
            pass

    def _on_request_failed(self, request: Request) -> None:
        if not self._collecting:
            return
        try:
            url = request.url
            start = self._request_times.pop(url, None)
            duration_ms = int((time.monotonic() - start) * 1000) if start else 0

            self._network_entries.append(NetworkEntry(
                method=request.method,
                url=url[:500],
                resource_type=request.resource_type,
                duration_ms=duration_ms,
                failed=True,
                failure_text=(request.failure or "")[:200],
            ))
        except Exception:
            pass

    def finish_step(self, page: Page, screenshot: bytes | None = None) -> StepEvidence:
        """Stop collecting and return the evidence bundle."""
        self._collecting = False

        # Remove listeners
        try:
            page.remove_listener("console", self._on_console)
            page.remove_listener("request", self._on_request)
            page.remove_listener("response", self._on_response)
            page.remove_listener("requestfailed", self._on_request_failed)
        except Exception:
            pass

        # Capture DOM snapshot
        dom_snapshot = ""
        try:
            dom_snapshot = page.evaluate(
                "() => document.documentElement.outerHTML.substring(0, 500000)"
            )
        except Exception as e:
            logger.debug("evidence.dom_snapshot_failed", error=str(e)[:200])

        return StepEvidence(
            step_number=self._current_step,
            screenshot=screenshot,
            console_logs=list(self._console_entries),
            dom_snapshot=dom_snapshot,
            network_log=list(self._network_entries),
        )


def upload_evidence_bundle(
    s3_client,
    bucket: str,
    org_id: str,
    test_run_id: str,
    evidence: StepEvidence,
    evidence_manager=None,
) -> dict[str, str]:
    """Upload all evidence files to MinIO. Returns dict of evidence type → key.

    Non-fatal: if any individual upload fails, it's skipped.

    If evidence_manager is provided, metadata is recorded and compression
    is applied per policy.
    """
    step = evidence.step_number
    prefix = f"{org_id}/{test_run_id}"
    uploaded: dict[str, str] = {}

    def _upload(key: str, data: bytes, content_type: str, evidence_type: str) -> None:
        """Upload a single evidence file with optional compression + metadata tracking."""
        original_size = len(data)
        compression = "none"
        compressed_size = original_size

        if evidence_manager:
            data, content_type, compression, original_size, compressed_size = (
                evidence_manager.prepare_upload(data, content_type, evidence_type)
            )

        try:
            put_kwargs: dict = {
                "Bucket": bucket, "Key": key,
                "Body": data, "ContentType": content_type,
            }
            if compression == "gzip":
                put_kwargs["ContentEncoding"] = "gzip"
            s3_client.put_object(**put_kwargs)
            uploaded[evidence_type] = key

            if evidence_manager:
                evidence_manager.record_upload(
                    org_id=org_id, test_run_id=test_run_id,
                    step_number=step, evidence_type=evidence_type,
                    s3_key=key, s3_bucket=bucket,
                    content_type=content_type, size_bytes=original_size,
                    compressed_size_bytes=compressed_size, compression=compression,
                )
        except Exception as e:
            logger.debug("evidence.upload_failed", type=evidence_type, error=str(e)[:200])

    # Screenshot
    if evidence.screenshot:
        _upload(
            f"{prefix}/step_{step}.png",
            evidence.screenshot, "image/png", "screenshot",
        )

    # Console logs
    if evidence.console_logs:
        data = json.dumps(
            [{"type": e.type, "text": e.text, "timestamp_ms": e.timestamp_ms}
             for e in evidence.console_logs],
            indent=2,
        ).encode("utf-8")
        _upload(
            f"{prefix}/step_{step}_console.json",
            data, "application/json", "console",
        )

    # DOM snapshot
    if evidence.dom_snapshot:
        _upload(
            f"{prefix}/step_{step}_dom.html",
            evidence.dom_snapshot.encode("utf-8"), "text/html", "dom",
        )

    # Network log
    if evidence.network_log:
        data = json.dumps(
            [
                {
                    "method": e.method,
                    "url": e.url,
                    "status": e.status,
                    "resource_type": e.resource_type,
                    "duration_ms": e.duration_ms,
                    "failed": e.failed,
                    "failure_text": e.failure_text,
                }
                for e in evidence.network_log
            ],
            indent=2,
        ).encode("utf-8")
        _upload(
            f"{prefix}/step_{step}_network.json",
            data, "application/json", "network",
        )

    logger.debug(
        "evidence.bundle_uploaded",
        step=step,
        types=list(uploaded.keys()),
    )
    return uploaded
