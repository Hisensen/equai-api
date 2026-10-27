"""Hidden, task-scoped HTTPS transfer endpoints for ephemeral GPU workers."""

from __future__ import annotations

import json
import logging
import os
import base64
import fcntl
import re
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse

from database import connect, public_id
from local_storage import prepare_upload, uploaded_artifact
from transfer_storage import (
    browser_upload_path,
    read_result,
    resolve_input,
    result_relative,
    store_result,
    verify,
    runtime_root,
    verify_browser_upload_grant,
)

router = APIRouter(include_in_schema=False)
log = logging.getLogger("h3.transfer")
MAX_OUTPUT_BYTES = int(os.getenv("H3_MAX_WORKER_OUTPUT_BYTES", str(1024 * 1024 * 1024)))
MAX_RESULT_BYTES = 64 * 1024
MIN_OUTPUT_BYTES = int(os.getenv("H3_MIN_OUTPUT_BYTES", str(64 * 1024)))
INPUT_RETENTION = int(os.getenv("H3_INPUT_RETENTION_SECONDS", "86400"))


def _task(task_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
        if row:
            return row
        # Hypit submits each Take as its own GPU job (for example
        # ``vid_xxx-take-001``), but it deliberately does not create a second
        # merchant task for every Take.  Local transfer endpoints still need
        # to authorize those child jobs against the parent task; COS never had
        # this lookup, which is why the regression only appeared after moving
        # Hypit outputs to the local Tencent disk.
        parent_id = _parent_task_id(task_id)
        if parent_id == task_id:
            return None
        return db.execute("SELECT * FROM merchant_tasks WHERE id=?", (parent_id,)).fetchone()


def _parent_task_id(task_id: str) -> str:
    """Return the merchant task id represented by a worker callback id."""
    # Reenactment reruns add ``-rN`` before the segment id.  The signed
    # output and diagnostic callbacks still belong to the merchant task.
    if re.fullmatch(r"(vid_[^-]+)-take-\d{3}", task_id):
        return task_id.rsplit("-take-", 1)[0]
    match = re.fullmatch(r"(vid_[^-]+)(?:-r\d+)?-seg\d{2}-\d+", task_id)
    if match:
        return match.group(1)
    return task_id


def _browser_grant(token: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        value = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(403, "素材上传授权无效") from None
    if not isinstance(value, dict) or not verify_browser_upload_grant(
        value, str(value.get("merchant_id") or "")
    ):
        raise HTTPException(403, "素材上传授权无效或已过期")
    return value


@router.put("/internal/uploads")
async def browser_upload(request: Request, token: str) -> Response:
    """Receive a browser upload directly into the Guangzhou local input disk."""
    grant = _browser_grant(token)
    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].lower()
    if content_type != str(grant["content_type"]).lower():
        raise HTTPException(422, "素材上传类型不匹配")
    target = browser_upload_path(str(grant["key"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        part = int(request.query_params.get("part", "-1"))
        parts = int(request.query_params.get("parts", "0"))
    except ValueError:
        raise HTTPException(422, "素材分片参数无效") from None
    chunk_bytes = int(grant.get("chunk_bytes") or 0)
    chunked = chunk_bytes > 0 and parts > 0
    if chunked and not (0 <= part < parts <= 4096):
        raise HTTPException(422, "素材分片序号无效")
    declared = request.headers.get("content-length")
    if declared is not None:
        declared_size = int(declared)
        if chunked:
            expected = min(chunk_bytes, int(grant["size_bytes"]) - part * chunk_bytes)
            if expected <= 0 or declared_size != expected:
                raise HTTPException(422, "素材分片大小不匹配")
        elif declared_size != int(grant["size_bytes"]):
            raise HTTPException(422, "素材上传大小不匹配")
    temporary = target.with_name(
        f".{target.name}.{uuid.uuid4().hex}.part" if not chunked
        else f".{target.name}.chunk-{part:04d}.{uuid.uuid4().hex}.part"
    )
    written = 0
    try:
        with temporary.open("wb") as handle:
            async for chunk in request.stream():
                written += len(chunk)
                if written > int(grant["size_bytes"]):
                    raise HTTPException(413, "素材超过授权大小")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if chunked:
            expected = min(chunk_bytes, int(grant["size_bytes"]) - part * chunk_bytes)
            if written != expected:
                raise HTTPException(422, "素材分片不完整")
            temporary.chmod(0o600)
            chunk_paths = [target.with_name(f".{target.name}.chunk-{index:04d}") for index in range(parts)]
            # A chunk only becomes visible after its complete body is durable.
            # Lock assembly so separate API workers cannot read/delete the same
            # set of chunks concurrently.
            lock = target.with_name(f".{target.name}.assemble.lock")
            with lock.open("a+b") as guard:
                fcntl.flock(guard, fcntl.LOCK_EX)
                try:
                    if not (target.is_file() and target.stat().st_size == int(grant["size_bytes"])):
                        temporary.replace(chunk_paths[part])
                        if all(path.is_file() for path in chunk_paths):
                            assembled = target.with_name(f".{target.name}.{uuid.uuid4().hex}.assemble")
                            try:
                                with assembled.open("wb") as output:
                                    for path in chunk_paths:
                                        with path.open("rb") as source:
                                            while data := source.read(1024 * 1024):
                                                output.write(data)
                                    output.flush()
                                    os.fsync(output.fileno())
                                if assembled.stat().st_size != int(grant["size_bytes"]):
                                    raise HTTPException(422, "素材合并大小不匹配")
                                assembled.chmod(0o600)
                                assembled.replace(target)
                            finally:
                                assembled.unlink(missing_ok=True)
                            for path in chunk_paths:
                                path.unlink(missing_ok=True)
                finally:
                    fcntl.flock(guard, fcntl.LOCK_UN)
            return Response(status_code=204)
        if written != int(grant["size_bytes"]):
            raise HTTPException(422, "素材上传不完整")
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return Response(status_code=204)


@router.get("/internal/worker/tasks/{task_id}/inputs/{asset_id}")
def worker_input(
    task_id: str,
    asset_id: str,
    expires: int,
    signature: str,
) -> FileResponse:
    if not verify("input-get", task_id, asset_id, expires, signature):
        raise HTTPException(403, "素材下载授权无效或已过期")
    with connect() as db:
        row = db.execute(
            """SELECT * FROM assets WHERE id=? AND task_id=? AND role='input'
            AND bucket='local-input' AND purged_at IS NULL""",
            (asset_id, task_id),
        ).fetchone()
    if not row or (row["expires_at"] and row["expires_at"] <= time.time()):
        raise HTTPException(404, "素材不存在或已过期")
    try:
        path = resolve_input(row["object_key"])
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "素材文件不存在") from None
    return FileResponse(
        path,
        media_type=row["content_type"] or "application/octet-stream",
        filename=row["filename"],
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/internal/runtime/{relative:path}")
def worker_runtime(
    relative: str,
    task_id: str,
    expires: int,
    signature: str,
) -> FileResponse:
    """Serve a migrated worker runtime bundle from the local media disk."""
    if not verify("runtime-get", task_id, relative, expires, signature):
        raise HTTPException(403, "运行包下载授权无效或已过期")
    try:
        root = runtime_root()
        path = (root / relative).resolve()
        path.relative_to(root)
    except ValueError:
        raise HTTPException(404, "运行包不存在") from None
    if not path.is_file():
        raise HTTPException(404, "运行包不存在")
    return FileResponse(path, media_type="application/gzip", headers={"Cache-Control": "private, no-store"})


@router.put("/internal/worker/tasks/{task_id}/outputs/{slot}", status_code=204)
async def worker_output(
    task_id: str,
    slot: int,
    request: Request,
    expires: int,
    signature: str,
    content_length: Annotated[int | None, Header(alias="Content-Length")] = None,
) -> Response:
    subject = f"{slot:02d}"
    if slot < 0 or slot > 3 or not verify("output-put", task_id, subject, expires, signature):
        raise HTTPException(403, "视频上传授权无效或已过期")
    row = _task(task_id)
    if not row or row["status"] not in {"dispatching", "running", "music_dispatching", "music_running"}:
        raise HTTPException(409, "任务当前不接受视频上传")
    if content_length is not None and content_length > MAX_OUTPUT_BYTES:
        raise HTTPException(413, "输出文件超过大小限制")
    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].lower()
    music = str(row["kind"] or "") == "music"
    allowed = {"video/mp4", "application/octet-stream"}
    if music:
        allowed |= {"audio/mpeg", "audio/flac", "audio/wav", "audio/x-wav"}
    if content_type not in allowed:
        raise HTTPException(415, "输出文件类型不正确")

    output_names = []
    if music:
        try:
            output_names = list(json.loads(row["payload"] or "{}").get("_output_names") or [])
        except (TypeError, json.JSONDecodeError):
            output_names = []
    filename = output_names[slot] if slot < len(output_names) else None

    temporary, target, relative = prepare_upload(
        row["merchant_id"], task_id, slot, uuid.uuid4().hex, filename=filename
    )
    written = 0
    try:
        with temporary.open("wb") as handle:
            async for chunk in request.stream():
                written += len(chunk)
                if written > MAX_OUTPUT_BYTES:
                    raise HTTPException(413, "输出文件超过大小限制")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if written < int(os.getenv("H3_MIN_OUTPUT_BYTES", str(MIN_OUTPUT_BYTES))):
            raise HTTPException(422, "输出文件不完整")
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    # This validates the final path and makes a future scheduler scan deterministic.
    uploaded_artifact(relative)
    return Response(status_code=204)


@router.put("/internal/worker/tasks/{task_id}/result", status_code=204)
async def worker_result(
    task_id: str,
    request: Request,
    expires: int,
    signature: str,
    content_length: Annotated[int | None, Header(alias="Content-Length")] = None,
) -> Response:
    if not verify("result-put", task_id, "result", expires, signature):
        raise HTTPException(403, "任务结果上传授权无效或已过期")
    row = _task(task_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    if content_length is not None and content_length > MAX_RESULT_BYTES:
        raise HTTPException(413, "任务结果超过大小限制")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_RESULT_BYTES:
            raise HTTPException(413, "任务结果超过大小限制")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(422, "任务结果不是有效 JSON") from None
    if not isinstance(value, dict) or str(value.get("job_id") or "") != task_id:
        raise HTTPException(422, "任务结果与任务编号不匹配")

    relative = result_relative(row["merchant_id"], task_id)
    size = store_result(relative, value)
    now = time.time()
    with connect() as db:
        existing = db.execute(
            "SELECT id FROM assets WHERE bucket='local-result' AND object_key=?",
            (relative,),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE assets SET size_bytes=?,expires_at=?,purged_at=NULL WHERE id=?",
                (size, now + INPUT_RETENTION, existing["id"]),
            )
        else:
            db.execute(
                """INSERT INTO assets
                (id,merchant_id,task_id,role,bucket,object_key,filename,content_type,
                 size_bytes,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    public_id("ast"), row["merchant_id"], _parent_task_id(task_id), "diagnostic",
                    "local-result", relative, "_job_result.json", "application/json",
                    size, now, now + INPUT_RETENTION,
                ),
            )
    if value.get("ok") is True:
        # Import lazily to avoid the scheduler -> transfer_api import cycle.
        # Completion still validates the uploaded video before changing state.
        try:
            from scheduler import complete_from_worker

            # A Take callback carries the child GPU id, while scheduler state
            # and billing live on the parent merchant task.
            await complete_from_worker(_parent_task_id(task_id), value)
        except Exception:  # the normal provider poll remains a safe fallback
            log.exception("Could not finalize worker callback for %s", task_id)
    return Response(status_code=204)


def local_worker_result(merchant_id: str, task_id: str) -> dict | None:
    return read_result(result_relative(merchant_id, task_id))
