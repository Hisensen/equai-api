"""Small, private browser previews of retained task outputs.

Original artifacts remain untouched and are used by the download links.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from PIL import Image, ImageOps

import local_storage
from object_storage import download_path

log = logging.getLogger("h3.preview")


def _cache_dir(merchant_id: str, task_id: str) -> Path:
    key = hashlib.sha256(f"{merchant_id}/{task_id}".encode()).hexdigest()
    return local_storage._output_root() / "_previews" / key


def remove_task_previews(merchant_id: str, task_id: str) -> None:
    shutil.rmtree(_cache_dir(merchant_id, task_id), ignore_errors=True)


def create_preview(merchant_id: str, task_id: str, artifact: dict, mode: str) -> tuple[Path, str]:
    media = artifact.get("media")
    if media not in {"video", "image"} or mode not in {"poster", "media"}:
        raise ValueError("不支持的预览类型")
    folder = _cache_dir(merchant_id, task_id)
    folder.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(str(artifact.get("path") or artifact.get("key") or "").encode()).hexdigest()[:16]
    suffix = ".webp" if media == "image" else ".jpg" if mode == "poster" else ".mp4"
    target = folder / f"{identity}-{mode}{suffix}"
    content_type = "image/webp" if media == "image" else "image/jpeg" if mode == "poster" else "video/mp4"
    if target.is_file() and target.stat().st_size:
        return target, content_type

    with tempfile.TemporaryDirectory(prefix="preview-source-", dir=folder) as temporary:
        if artifact.get("storage") == "local":
            source = local_storage.resolve(str(artifact["path"]))
        elif artifact.get("key"):
            source = Path(temporary) / Path(str(artifact["key"])).name
            download_path(str(artifact["key"]), source)
        else:
            raise FileNotFoundError("作品文件不存在")
        staged = folder / f".{uuid.uuid4().hex}{suffix}"
        try:
            if media == "image":
                with Image.open(source) as original:
                    image = ImageOps.exif_transpose(original)
                    image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                    normalized = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                    normalized.save(staged, "WEBP", quality=78, method=5)
            else:
                command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
                if mode == "poster":
                    command += ["-ss", "0.2", "-i", str(source), "-frames:v", "1",
                                "-vf", "scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2",
                                "-q:v", "5", str(staged)]
                else:
                    command += ["-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
                                "-vf", "scale=960:960:force_original_aspect_ratio=decrease:force_divisible_by=2",
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "29", "-pix_fmt", "yuv420p",
                                "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(staged)]
                subprocess.run(command, check=True, timeout=180, capture_output=True)
            if not staged.is_file() or not staged.stat().st_size:
                raise RuntimeError("预览生成失败")
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)
    return target, content_type


async def warm_task_previews(merchant_id: str, task_id: str, artifacts: list[dict]) -> None:
    for artifact in artifacts:
        if artifact.get("media") not in {"video", "image"}:
            continue
        for mode in ("poster", "media"):
            try:
                await asyncio.to_thread(create_preview, merchant_id, task_id, artifact, mode)
            except Exception:
                log.warning("Could not prepare %s preview for %s", mode, task_id, exc_info=True)
        break
