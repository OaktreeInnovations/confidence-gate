"""Celery task: generate a comprehensive PDF report for a test run.

Gathers data from MongoDB and MinIO, renders HTML via report_template,
converts to PDF using Playwright, and uploads the result to MinIO.
"""

from __future__ import annotations

import base64
import io

import boto3
from bson import ObjectId
from PIL import Image
from playwright.sync_api import sync_playwright
import structlog

from app.clients.sync_mongo import get_db as _get_sync_db
from app.config import Settings
from app.worker.celery_app import celery_app
from app.worker.report_template import render_report_html

settings = Settings()
logger = structlog.get_logger(__name__)


def _get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=f"http{'s' if settings.minio_use_ssl else ''}://{settings.minio_endpoint}",
        aws_access_key_id=settings.minio_root_user,
        aws_secret_access_key=settings.minio_root_password,
        region_name="us-east-1",
    )


def _resize_screenshot(png_bytes: bytes, max_width: int = 800) -> bytes:
    """Resize a screenshot to max_width while maintaining aspect ratio."""
    img = Image.open(io.BytesIO(png_bytes))
    if img.width <= max_width:
        return png_bytes
    ratio = max_width / img.width
    new_size = (max_width, int(img.height * ratio))
    img = img.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@celery_app.task(
    name="cg.generate_report",
    bind=True,
    soft_time_limit=120,
    time_limit=150,
)
def generate_report(self, test_run_id: str, org_id: str) -> dict:
    """Generate a PDF report for a completed test run.

    Args:
        test_run_id: MongoDB ObjectId string for the test run.
        org_id: MongoDB ObjectId string for the organization.

    Returns:
        Dict with status, MinIO key, and PDF size in bytes.
    """
    logger.info("report.generate_start", test_run_id=test_run_id)

    try:
        db = _get_sync_db()
        s3 = _get_s3_client()
        bucket = settings.minio_default_bucket

        # --- Load documents ---
        run_doc = db.test_runs.find_one({"_id": ObjectId(test_run_id)})
        if not run_doc:
            return {"status": "error", "message": "Test run not found"}

        if run_doc.get("status") not in ("passed", "failed", "error"):
            return {"status": "error", "message": "Test run is not yet complete"}

        tc_doc = None
        tc_id = run_doc.get("test_case_id")
        if tc_id:
            tc_doc = db.test_cases.find_one({"_id": tc_id})

        user_doc = None
        triggered_by = run_doc.get("triggered_by")
        if triggered_by:
            user_doc = db.users.find_one({"_id": triggered_by})

        org_doc = db.organizations.find_one({"_id": ObjectId(org_id)})

        # Telemetry (optional)
        tel_doc = db.execution_telemetry.find_one({"test_run_id": test_run_id})

        # Execution profile (optional)
        profile_doc = None
        if tc_id:
            profile_doc = db.execution_profiles.find_one({
                "test_case_id": str(tc_id),
                "org_id": org_id,
            })

        # --- Download screenshots ---
        screenshots: dict[int, str] = {}
        results = run_doc.get("results", [])
        for r in results:
            step_num = r.get("step_number", 0)
            evidence_url = r.get("evidence_url", "")
            if not evidence_url:
                continue

            key = f"{org_id}/{test_run_id}/step_{step_num}.png"
            try:
                resp = s3.get_object(Bucket=bucket, Key=key)
                raw = resp["Body"].read()
                resized = _resize_screenshot(raw)
                b64 = base64.b64encode(resized).decode("ascii")
                screenshots[step_num] = f"data:image/png;base64,{b64}"
            except Exception:
                logger.debug("report.screenshot_missing", step=step_num, key=key)

        logger.info(
            "report.data_loaded",
            test_run_id=test_run_id,
            screenshots=len(screenshots),
            has_telemetry=tel_doc is not None,
            has_profile=profile_doc is not None,
        )

        # --- Render HTML ---
        html = render_report_html(
            test_run=run_doc,
            test_case=tc_doc,
            user=user_doc,
            org=org_doc,
            telemetry=tel_doc,
            profile=profile_doc,
            screenshots=screenshots,
        )

        # --- Convert HTML to PDF via Playwright ---
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            pdf_bytes = page.pdf(
                format="A4",
                print_background=True,
                margin={
                    "top": "18mm",
                    "bottom": "22mm",
                    "left": "15mm",
                    "right": "15mm",
                },
            )
            browser.close()

        # --- Upload to MinIO ---
        pdf_key = f"{org_id}/{test_run_id}/report.pdf"
        s3.put_object(
            Bucket=bucket,
            Key=pdf_key,
            Body=pdf_bytes,
            ContentType="application/pdf",
        )

        logger.info(
            "report.generate_complete",
            test_run_id=test_run_id,
            key=pdf_key,
            size_bytes=len(pdf_bytes),
        )

        return {
            "status": "ok",
            "key": pdf_key,
            "size_bytes": len(pdf_bytes),
        }

    except Exception as e:
        logger.error("report.generate_failed", test_run_id=test_run_id, error=str(e)[:500])
        return {"status": "error", "message": str(e)[:500]}
