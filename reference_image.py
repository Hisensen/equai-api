"""Resize person/reference images in the API tier before GPU upload."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger("h3.reference_image")
MAX_EDGE = max(256, int(os.getenv("H3_REFERENCE_IMAGE_MAX_EDGE", "1280")))
JPEG_QUALITY = min(95, max(60, int(os.getenv("H3_REFERENCE_IMAGE_JPEG_QUALITY", "86"))))
slots = asyncio.Semaphore(max(1, int(os.getenv("H3_REFERENCE_IMAGE_PROCESS_SLOTS", "2"))))


def downscale_file(source: Path, output_dir: Path) -> Path | None:
    """Save an orientation-correct, max-edge-capped image for model input."""
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
            image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            if has_alpha:
                image = image.convert("RGBA")
                target = output_dir / "reference.webp"
                image.save(target, "WEBP", quality=JPEG_QUALITY, method=6)
            else:
                image = image.convert("RGB")
                target = output_dir / "reference.jpg"
                image.save(target, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
        return target if target.is_file() and target.stat().st_size > 0 else None
    except (OSError, ValueError, Image.DecompressionBombError, subprocess.SubprocessError):
        log.warning("Reference image processing failed for %s", source.name, exc_info=True)
        return None
