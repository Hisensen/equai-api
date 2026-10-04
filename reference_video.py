"""Reference-video downscaling, kept off the request path.

H3's GPU time and price scale with the reference video's pixel count, not its
duration, so a 1080p source is shrunk to a 360 short edge before it reaches the
pool (measured: 1142s/0.755 -> 399s/0.252 per task, with no visible difference
in three A/B renders).

The work used to run inside create_video, which made the caller wait for a full
COS round trip on a 0.7 MB/s link. It now runs once in the scheduler, just
before a task is dispatched, so the browser gets its task id immediately.

Nothing here is fatal: on any failure the original object stays untouched and
the job proceeds at full resolution, paying more but still delivering.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("h3.reference")


# Compatibility shims keep migration tests and one-off maintenance scripts able
# to monkey-patch the COS operations without importing the COS client on the
# local-storage request path.
def presigned_url(*args, **kwargs):
    from object_storage import presigned_url as _presigned_url
    return _presigned_url(*args, **kwargs)


def download_path(*args, **kwargs):
    from object_storage import download_path as _download_path
    return _download_path(*args, **kwargs)


def upload_path(*args, **kwargs):
    from object_storage import upload_path as _upload_path
    return _upload_path(*args, **kwargs)

# Set to 0 to disable shrinking entirely.
SHORT_EDGE = int(os.getenv("H3_REFERENCE_VIDEO_SHORT_EDGE", "360"))
# The semaphore only stops a burst from starving the loop on a 2-core box;
# throughput sits far above what the GPU pool can consume.
TRANSCODE_SLOTS = int(os.getenv("H3_REFERENCE_VIDEO_TRANSCODE_SLOTS", "2"))
slots = asyncio.Semaphore(max(1, TRANSCODE_SLOTS))


def short_edge_of(key: str) -> int:
    """The object's short edge read over a signed URL: only the container header is fetched.

    Returns 0 when it cannot be determined, which callers treat as "leave it alone".
    """
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error",
             "-protocol_whitelist", "https,tls,tcp,http,crypto",
             "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "json", presigned_url(key, expires=900)],
            capture_output=True, timeout=60, check=True,
        )
        streams = json.loads(probe.stdout).get("streams") or []
        if not streams:
            return 0
        return min(int(streams[0]["width"]), int(streams[0]["height"]))
    except (OSError, ValueError, KeyError, IndexError,
            subprocess.SubprocessError, json.JSONDecodeError):
        log.warning("Reference video probe failed for %s", key, exc_info=True)
        return 0


def downscale_file(source: Path, target: Path) -> bool:
    """Shrink a local video's short edge. Returns True when target was written.

    Keeps aspect ratio, frame rate and audio untouched; only the spatial
    resolution changes.
    """
    if SHORT_EDGE <= 0:
        return False
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(source)],
            capture_output=True, timeout=30, check=True,
        )
        streams = json.loads(probe.stdout).get("streams") or []
        if not streams:
            return False
        width, height = int(streams[0]["width"]), int(streams[0]["height"])
        if min(width, height) <= SHORT_EDGE:
            return False
        scale = (
            f"scale='if(gt(iw,ih),-2,{SHORT_EDGE})':'if(gt(iw,ih),{SHORT_EDGE},-2)'"
        )
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
             "-vf", scale, "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-c:a", "copy", str(target)],
            check=True, capture_output=True, timeout=180,
        )
        return target.is_file() and target.stat().st_size > 0
    except (OSError, ValueError, KeyError, IndexError,
            subprocess.SubprocessError, json.JSONDecodeError):
        log.warning("Reference video downscale failed for %s", source.name, exc_info=True)
        return False


def rewrite_object(key: str, content_type: str) -> int:
    """Replace a COS object with its downscaled self. Returns the new size, or 0 when kept.

    The object keeps its key, so every signed URL already handed out stays valid.
    """
    with tempfile.TemporaryDirectory(prefix="refcos-") as root:
        source, target = Path(root) / "source.mp4", Path(root) / "small.mp4"
        try:
            download_path(key, source)
            if not downscale_file(source, target):
                return 0
            upload_path(target, key, content_type)
        except Exception:  # noqa: BLE001 - never fatal, the original stays usable
            log.warning("Reference video rewrite failed for %s", key, exc_info=True)
            return 0
        size = target.stat().st_size
        log.info("Reference video %s rewritten to %s bytes", key, size)
        return size


def rewrite_local(relative: str) -> int:
    """Downscale a local input in place, keeping its signed object key stable."""
    from transfer_storage import resolve_input

    source = resolve_input(relative)
    with tempfile.TemporaryDirectory(prefix="reflocal-", dir=str(source.parent)) as root:
        target = Path(root) / source.name
        if not downscale_file(source, target):
            return 0
        size = target.stat().st_size
        replacement = source.with_name(f".{source.name}.{os.getpid()}.small")
        try:
            target.replace(replacement)
            replacement.replace(source)
        finally:
            replacement.unlink(missing_ok=True)
        log.info("Local reference video %s rewritten to %s bytes", relative, size)
        return size


async def shrink(key: str, content_type: str) -> int:
    """Downscale one already-uploaded reference video in place, off the event loop."""
    if SHORT_EDGE <= 0:
        return 0
    async with slots:
        return await asyncio.to_thread(rewrite_object, key, content_type)


async def shrink_inputs(inputs: list[dict]) -> int:
    """Shrink every reference video in a task's inputs.

    Safe to call again after a restart: a probe over the signed URL skips
    anything already at or below the target short edge, so a retry costs one
    header fetch instead of a full transcode.
    """
    if SHORT_EDGE <= 0:
        return 0
    targets = [
        item for item in inputs
        if item.get("field") == "videos"
        and item.get("storage") in {"cos", "local"}
        and str(item.get("content_type") or "").startswith("video/")
    ]
    if not targets:
        return 0
    written = 0
    for item in targets:
        key = str(item["key"])
        is_local = item.get("storage") == "local"
        if is_local:
            try:
                from transfer_storage import resolve_input
                probe_target = resolve_input(key)
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "json", str(probe_target)],
                    capture_output=True, timeout=30, check=True,
                )
                streams = json.loads(probe.stdout).get("streams") or []
                edge = min(int(streams[0]["width"]), int(streams[0]["height"])) if streams else 0
            except (OSError, ValueError, KeyError, IndexError,
                    subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
                edge = 0
        else:
            edge = await asyncio.to_thread(short_edge_of, key)
        if edge and edge <= SHORT_EDGE:
            continue
        if is_local:
            try:
                result = await asyncio.to_thread(rewrite_local, key)
                if result <= 0:
                    raise RuntimeError(f"本地视频无法压缩到短边 {SHORT_EDGE}px: {key}")
                written += result
            except Exception:  # noqa: BLE001 - input remains usable at original size
                log.exception("Local reference video rewrite failed for %s", key)
                raise
        else:
            result = await shrink(key, str(item.get("content_type") or "video/mp4"))
            if result <= 0:
                raise RuntimeError(f"视频无法压缩到短边 {SHORT_EDGE}px: {key}")
            written += result
    return written
