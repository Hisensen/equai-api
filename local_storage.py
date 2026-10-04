"""Private local storage for completed videos and signed download links."""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import quote

def _output_root() -> Path:
    return Path(os.getenv("H3_LOCAL_OUTPUT_DIR", "/data/videos")).resolve()


def _public_base_url() -> str:
    return os.getenv("H3_PUBLIC_BASE_URL", "").rstrip("/")


def enabled() -> bool:
    return os.getenv("H3_LOCAL_OUTPUTS", "0") == "1"


def configured() -> bool:
    return not enabled() or bool(os.getenv("H3_DOWNLOAD_SECRET", "").strip())


def capacity_ok() -> bool:
    if not enabled():
        return True
    root = _output_root()
    root.mkdir(parents=True, exist_ok=True)
    minimum = int(os.getenv("H3_MIN_FREE_OUTPUT_BYTES", str(2 * 1024**3)))
    return shutil.disk_usage(root).free >= minimum


def _secret() -> bytes:
    value = os.getenv("H3_DOWNLOAD_SECRET", "").strip()
    if len(value) < 32:
        raise RuntimeError("H3_DOWNLOAD_SECRET 必须至少为 32 个字符")
    return value.encode()


def _safe_relative(relative: str) -> Path:
    root = _output_root()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("非法的视频文件路径") from exc
    return candidate


def store_cos_video(
    cos_key: str,
    merchant_id: str,
    task_id: str,
    filename: str,
    expected_size: int,
) -> dict:
    """Atomically copy a generated COS video into private server storage."""
    # Kept only for one-way migration of legacy COS artifacts. The local
    # production path never imports the COS client while serving a task.
    from object_storage import download_path

    safe_name = Path(filename).name[:180] or "video.mp4"
    relative = f"{merchant_id}/{task_id}/{safe_name}"
    target = _safe_relative(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    temporary.unlink(missing_ok=True)
    try:
        download_path(cos_key, temporary)
        actual_size = temporary.stat().st_size
        if actual_size < expected_size or actual_size == 0:
            raise IOError(
                f"视频下载不完整: expected>={expected_size}, actual={actual_size}"
            )
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "name": safe_name,
        "storage": "local",
        "path": relative,
        "media": "video",
        "size_bytes": target.stat().st_size,
    }


def output_relative(merchant_id: str, task_id: str, slot: int, filename: str | None = None) -> str:
    if slot < 0 or slot > 3:
        raise ValueError("非法的输出槽位")
    safe = Path(filename or "output.mp4").name
    if not safe or safe in {".", ".."}:
        safe = "output.mp4"
    return f"{merchant_id}/{task_id}/{slot:02d}-{safe}"


def prepare_upload(
    merchant_id: str,
    task_id: str,
    slot: int,
    upload_id: str | None = None,
    *,
    filename: str | None = None,
) -> tuple[Path, Path, str]:
    """Return a unique temporary path and exact final path for an atomic PUT."""
    relative = output_relative(merchant_id, task_id, slot, filename)
    target = _safe_relative(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{upload_id or uuid.uuid4().hex}.part")
    return temporary, target, relative


def uploaded_artifact(relative: str) -> dict:
    target = resolve(relative)
    return {
        "name": target.name,
        "storage": "local",
        "path": relative,
        "media": "audio" if target.suffix.lower() in {".mp3", ".flac", ".wav", ".m4a"} else "video",
        "content_type": {".mp3": "audio/mpeg", ".flac": "audio/flac", ".wav": "audio/wav", ".m4a": "audio/mp4"}.get(target.suffix.lower(), "video/mp4"),
        "size_bytes": target.stat().st_size,
    }


def list_task_videos(merchant_id: str, task_id: str, minimum_size: int) -> list[dict]:
    folder = _safe_relative(f"{merchant_id}/{task_id}")
    if not folder.is_dir():
        return []
    artifacts = []
    for path in sorted(folder.glob("[0-9][0-9]-output.mp4")):
        if path.is_file() and path.stat().st_size >= minimum_size:
            relative = str(path.relative_to(_output_root()))
            artifacts.append(uploaded_artifact(relative))
    return artifacts


def list_task_media(merchant_id: str, task_id: str, minimum_size: int) -> list[dict]:
    """List worker-delivered media for audio tasks without changing video paths."""
    folder = _safe_relative(f"{merchant_id}/{task_id}")
    if not folder.is_dir():
        return []
    return [
        uploaded_artifact(str(path.relative_to(_output_root())))
        for path in sorted(folder.glob("[0-9][0-9]-*"))
        if path.is_file() and path.suffix.lower() in {".mp3", ".flac", ".wav", ".m4a"}
        and path.stat().st_size >= minimum_size
    ]


def remove(relative: str) -> None:
    target = _safe_relative(relative)
    target.unlink(missing_ok=True)
    current = target.parent
    root = _output_root()
    while current != root:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def resolve(relative: str) -> Path:
    target = _safe_relative(relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    return target


def _signature(task_id: str, relative: str, expires_at: int) -> str:
    payload = f"{task_id}\n{relative}\n{expires_at}".encode()
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def signed_url(task_id: str, relative: str, filename: str, *, expires: int = 3600) -> str:
    expires_at = int(time.time()) + expires
    signature = _signature(task_id, relative, expires_at)
    path = f"/v1/videos/{quote(task_id, safe='')}/artifacts/{quote(filename, safe='')}"
    return f"{_public_base_url()}{path}?expires={expires_at}&signature={signature}"


def verify(task_id: str, relative: str, expires_at: int, signature: str) -> bool:
    if expires_at < int(time.time()) or expires_at > int(time.time()) + 3700:
        return False
    return hmac.compare_digest(_signature(task_id, relative, expires_at), signature)
