"""Evidence compression utilities.

Provides gzip compression for JSON/text evidence and optional JPEG
quality reduction for screenshots. All functions are synchronous
(designed for Celery worker context).

Compression is best-effort: if it fails, the original data is
returned unchanged so evidence is never lost.
"""

from __future__ import annotations

import gzip
import io

import structlog

logger = structlog.get_logger(__name__)


def compress_json(data: bytes) -> tuple[bytes, str, bool]:
    """Gzip-compress JSON/text bytes.

    Returns:
        (compressed_bytes, content_encoding, was_compressed)

    If compression fails or makes data larger, returns original.
    """
    try:
        compressed = gzip.compress(data, compresslevel=6)
        if len(compressed) < len(data):
            return compressed, "gzip", True
        # Compression didn't help — return original
        return data, "identity", False
    except Exception as e:
        logger.debug("compression.json_failed", error=str(e)[:200])
        return data, "identity", False


def compress_screenshot(
    png_bytes: bytes,
    jpeg_quality: int = 60,
    max_width: int = 800,
) -> tuple[bytes, str, bool]:
    """Convert PNG screenshot to JPEG with quality reduction.

    Returns:
        (image_bytes, content_type, was_compressed)

    Falls back to original PNG if conversion fails or JPEG is larger.
    """
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(png_bytes))

        # Resize if wider than max_width
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.LANCZOS)

        # Convert to RGB (JPEG doesn't support alpha)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        jpeg_bytes = buf.getvalue()

        if len(jpeg_bytes) < len(png_bytes):
            return jpeg_bytes, "image/jpeg", True
        return png_bytes, "image/png", False

    except Exception as e:
        logger.debug("compression.screenshot_failed", error=str(e)[:200])
        return png_bytes, "image/png", False
