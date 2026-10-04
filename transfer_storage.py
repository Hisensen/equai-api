"""Private local transport storage used between the API and ephemeral GPU jobs."""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

def _input_root() -> Path:
    return Path(os.getenv("H3_LOCAL_INPUT_DIR", "/data/inputs")).resolve()


def _result_root() -> Path:
    return Path(os.getenv("H3_LOCAL_RESULT_DIR", "/data/job-results")).resolve()


def runtime_root() -> Path:
    return Path(os.getenv("H3_LOCAL_RUNTIME_DIR", "/data/runtime")).resolve()
PUBLIC_BASE_URL = (
    os.getenv("H3_TRANSFER_BASE_URL") or os.getenv("H3_PUBLIC_BASE_URL", "")
).rstrip("/")
# Optional dedicated upload origin. Pointing this at a Guangzhou endpoint
# removes an extra proxy hop while downloads and task APIs keep their public
# origin. It falls back to the existing public origin until DNS and TLS are
# configured for the dedicated host.
UPLOAD_BASE_URL = (
    os.getenv("H3_UPLOAD_BASE_URL") or PUBLIC_BASE_URL
).rstrip("/")
MAX_GRANT_SECONDS = int(os.getenv("H3_TRANSFER_MAX_GRANT_SECONDS", str(26 * 3600)))


def backend() -> str:
    value = os.getenv("H3_WORKER_TRANSFER_BACKEND", "cos").strip().lower()
    if value not in {"cos", "local"}:
        raise RuntimeError("H3_WORKER_TRANSFER_BACKEND 只能是 cos 或 local")
    return value


def local_enabled() -> bool:
    return backend() == "local"


def configured() -> bool:
    if not local_enabled():
        return True
    return bool(PUBLIC_BASE_URL and len(_secret_value()) >= 32)


def callback_configured() -> bool:
    """A result callback is useful for both local and object-storage outputs."""
    return bool(PUBLIC_BASE_URL and len(_secret_value()) >= 32)


def capacity_ok(required_bytes: int = 0, *, force: bool = False) -> bool:
    if not force and not local_enabled():
        return True
    root = _input_root()
    root.mkdir(parents=True, exist_ok=True)
    minimum = int(os.getenv("H3_MIN_FREE_TRANSFER_BYTES", str(2 * 1024**3)))
    return shutil.disk_usage(root).free - max(0, required_bytes) >= minimum


def _secret_value() -> str:
    return (
        os.getenv("H3_TRANSFER_SECRET")
        or os.getenv("H3_DOWNLOAD_SECRET", "")
    ).strip()


def _secret() -> bytes:
    value = _secret_value()
    if len(value) < 32:
        raise RuntimeError("H3_TRANSFER_SECRET 必须至少为 32 个字符")
    return value.encode()


def create_browser_upload_grant(
    merchant_id: str, field: str, filename: str, content_type: str,
    size_bytes: int, *, expires: int = 3600,
) -> dict:
    safe_name = Path(filename).name[:180] or "asset"
    key = f"staged/{merchant_id}/{uuid.uuid4().hex}/{safe_name}"
    value = {
        "merchant_id": merchant_id, "field": field, "filename": safe_name,
        "content_type": content_type, "size_bytes": int(size_bytes),
        "key": key, "storage": "local", "expires_at": int(time.time()) + expires,
        # Keep browser uploads below intermediary request limits. The
        # server assembles these parts atomically after the final part lands.
        # Keep each request below common reverse-proxy limits while reducing
        # round trips for large browser uploads. The browser receives these
        # values in the signed grant and can resume/retry individual parts.
        "chunk_bytes": 8 * 1024 * 1024,
        "parallelism": 4,
    }
    value["signature"] = hmac.new(
        _secret(), json.dumps(value, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode(), hashlib.sha256,
    ).hexdigest()
    token = base64.urlsafe_b64encode(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    value["upload_url"] = f"{UPLOAD_BASE_URL}/internal/uploads?token={token}"
    # Keep the normal public origin as a last-resort fallback.  Mainland
    # users usually reach the dedicated Tencent upload origin faster, while
    # a mobile carrier or corporate network may only allow the public site
    # origin.  Both URLs use the same signed grant and local disk target.
    if PUBLIC_BASE_URL and PUBLIC_BASE_URL != UPLOAD_BASE_URL:
        value["fallback_upload_url"] = f"{PUBLIC_BASE_URL}/internal/uploads?token={token}"
    return value


def verify_browser_upload_grant(value: dict, merchant_id: str) -> bool:
    try:
        if value.get("merchant_id") != merchant_id:
            return False
        if int(value["expires_at"]) < int(time.time()):
            return False
        if not str(value["key"]).startswith(f"staged/{merchant_id}/"):
            return False
        signed = {key: value[key] for key in (
            "merchant_id", "field", "filename", "content_type", "size_bytes",
            "key", "storage", "expires_at",
        )}
        # Chunk metadata is part of newer grants. Include it when present,
        # while keeping verification backwards-compatible with older grants
        # that were signed before chunked browser uploads were introduced.
        for key in ("chunk_bytes", "parallelism"):
            if key in value:
                signed[key] = value[key]
        expected = hmac.new(
            _secret(), json.dumps(signed, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode(), hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, str(value.get("signature") or ""))
    except (KeyError, TypeError, ValueError):
        return False


def browser_upload_path(relative: str) -> Path:
    return _safe(_input_root(), relative)


def _safe(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("非法的传输文件路径") from exc
    return candidate


def _atomic_copy(fileobj: BinaryIO, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    try:
        fileobj.seek(0)
        with temporary.open("wb") as handle:
            shutil.copyfileobj(fileobj, handle, length=1024 * 1024)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target.stat().st_size


def store_input(
    fileobj: BinaryIO,
    merchant_id: str,
    task_id: str,
    index: int,
    filename: str,
) -> tuple[str, int]:
    safe_name = Path(filename).name[:180] or f"file-{index}"
    relative = f"{merchant_id}/{task_id}/{index:02d}-{safe_name}"
    size = _atomic_copy(fileobj, _safe(_input_root(), relative))
    return relative, size


def resolve_input(relative: str) -> Path:
    target = _safe(_input_root(), relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    return target


def remove_input(relative: str) -> None:
    root = _input_root()
    _remove_empty_parents(_safe(root, relative), root)


def result_relative(merchant_id: str, task_id: str) -> str:
    return f"{merchant_id}/{task_id}/_job_result.json"


def store_result(relative: str, value: dict) -> int:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    root = _result_root()
    target = _safe(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_bytes(raw)
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return len(raw)


def read_result(relative: str) -> dict | None:
    target = _safe(_result_root(), relative)
    if not target.is_file():
        return None
    raw = target.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("任务结果文件超过大小限制")
    value = json.loads(raw.decode("utf-8"))
    return value if isinstance(value, dict) else None


def remove_result(relative: str) -> None:
    root = _result_root()
    _remove_empty_parents(_safe(root, relative), root)


def _remove_empty_parents(target: Path, root: Path) -> None:
    target.unlink(missing_ok=True)
    current = target.parent
    while current != root:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _signature(purpose: str, task_id: str, subject: str, expires_at: int) -> str:
    payload = f"{purpose}\n{task_id}\n{subject}\n{expires_at}".encode()
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def verify(purpose: str, task_id: str, subject: str, expires_at: int, signature: str) -> bool:
    now = int(time.time())
    if expires_at < now or expires_at > now + MAX_GRANT_SECONDS:
        return False
    expected = _signature(purpose, task_id, subject, expires_at)
    return hmac.compare_digest(expected, signature)


def _signed(
    path: str, purpose: str, task_id: str, subject: str, expires: int,
    *, base_url: str | None = None,
) -> str:
    expires_at = int(time.time()) + expires
    signature = _signature(purpose, task_id, subject, expires_at)
    origin = PUBLIC_BASE_URL if base_url is None else base_url.rstrip("/")
    if base_url is not None and origin and not origin.startswith("https://"):
        raise ValueError("签名传输地址必须使用 HTTPS")
    return f"{origin}{path}?expires={expires_at}&signature={signature}"


def signed_input_url(
    task_id: str, asset_id: str, *, expires: int, base_url: str | None = None,
) -> str:
    path = (
        f"/internal/worker/tasks/{quote(task_id, safe='')}/inputs/"
        f"{quote(asset_id, safe='')}"
    )
    return _signed(path, "input-get", task_id, asset_id, expires, base_url=base_url)


def signed_output_url(
    task_id: str, slot: int, *, expires: int, base_url: str | None = None,
) -> str:
    subject = f"{slot:02d}"
    path = f"/internal/worker/tasks/{quote(task_id, safe='')}/outputs/{slot}"
    return _signed(path, "output-put", task_id, subject, expires, base_url=base_url)


def signed_result_url(
    task_id: str, *, expires: int, base_url: str | None = None,
) -> str:
    path = f"/internal/worker/tasks/{quote(task_id, safe='')}/result"
    return _signed(path, "result-put", task_id, "result", expires, base_url=base_url)


def signed_runtime_url(
    task_id: str, relative: str, *, expires: int, base_url: str | None = None,
) -> str:
    """Signed read URL for a worker runtime bundle stored on the media host."""
    safe = str(Path(relative))
    if safe.startswith("../") or safe in {".", ""}:
        raise ValueError("非法的运行包路径")
    url = _signed(
        f"/internal/runtime/{quote(safe, safe='/')}",
        "runtime-get", task_id, safe, expires, base_url=base_url,
    )
    # Unlike task input/output URLs, the runtime path doesn't carry the task
    # id, but the endpoint needs it to validate the per-task signature.
    return f"{url}&task_id={quote(task_id, safe='')}"
