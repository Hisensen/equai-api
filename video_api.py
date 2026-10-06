from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.datastructures import Headers

import gongji
import reference_image
import reference_video
import remix_hypit
import video_projects
from database import (
    FAILURE_PRICE_MILLI,
    PRICE_MILLI,
    connect,
    points,
    public_id,
    reserve_task,
    settle_task,
    transaction,
)
from local_storage import resolve as resolve_local_output
from local_storage import signed_url as signed_local_url
from local_storage import verify as verify_local_url
from object_storage import (
    bucket_name,
    create_upload_grant,
    delete_keys,
    download_path,
    object_metadata,
    presigned_url,
    read_prefix,
    upload_fileobj,
    upload_relay,
    verify_upload_grant,
)
from security import api_merchant, portal_merchant, video_merchant
from transfer_storage import (
    backend as transfer_backend,
)
from transfer_storage import (
    browser_upload_path,
    create_browser_upload_grant,
    verify_browser_upload_grant,
)
from transfer_storage import (
    capacity_ok as local_input_capacity_ok,
)
from transfer_storage import (
    remove_input as remove_local_input,
)
from transfer_storage import (
    store_input as store_local_input,
)

router = APIRouter(tags=["video-generation"])
INPUT_PREFIX = os.getenv("COS_INPUT_PREFIX", "inputs").strip("/")
# H3 spends GPU time proportional to the pixel count of a reference video, not
# its duration, so shrinking the short edge before dispatch is close to free
# money. Measured on one 15.08 s clip at three widths, same prompt/image/seed:
#   736x1280 -> 1142 s GPU, billing 0.7551
#   480x834  ->  542 s GPU, billing 0.4522
#   360x626  ->  374 s GPU, billing 0.2517
# Output sharpness did not track the input: 37.1 / 36.1 / 39.6 (Laplacian
# variance), i.e. the spread is generation noise, not lost detail. The tier
# still decides delivered resolution, which stayed 736x1280 in all three.
# Set to 0 to disable.
log = logging.getLogger("h3.video")
# Browser-staged (direct) uploads are shrunk by the scheduler just before
# dispatch, not here: doing it inside the request made the caller wait for a
# full COS round trip. Multipart uploads still shrink in place below, where the
# bytes are already on this host and shrinking only makes the upload smaller.
INPUT_RETENTION = int(os.getenv("H3_INPUT_RETENTION_SECONDS", "86400"))
OUTPUT_RETENTION = int(os.getenv("H3_OUTPUT_RETENTION_SECONDS", "172800"))
ALLOWED_KINDS = {"text", "image", "reference"}
ALLOWED_DURATION = set(range(5, 16))
MAX_INPUT_BYTES = 512 * 1024 * 1024


class UploadGrantFile(BaseModel):
    field: str
    filename: str = Field(min_length=1, max_length=180)
    content_type: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(gt=0, le=MAX_INPUT_BYTES)


class UploadGrantRequest(BaseModel):
    kind: str
    files: list[UploadGrantFile] = Field(min_length=1, max_length=12)


class PortalTaskNote(BaseModel):
    note: str = Field(default="", max_length=80)


def _task_view(
    row, *, with_download: bool = True, include_prompts: bool = False
) -> dict:
    value = dict(row)
    now = time.time()
    payload = json.loads(value["payload"] or "{}")
    prompt_preview = " ".join(str(
        payload.get("_original_prompt") or payload.get("prompt") or ""
    ).split())[:180]
    artifacts = json.loads(value["artifacts"] or "[]")
    output_expired = bool(value["output_expires_at"] and value["output_expires_at"] <= now)
    visible = []
    for item in artifacts:
        public = {
            "name": item.get("name") or Path(item.get("key", "video.mp4")).name,
            "media": item.get("media", "video"),
            "size_bytes": item.get("size_bytes"),
        }
        if with_download and not output_expired and item.get("storage") == "local":
            public["download_url"] = signed_local_url(
                value["id"], item["path"], public["name"], expires=3600
            )
            public["download_url_expires_in"] = 3600
        elif with_download and not output_expired and item.get("key"):
            public["download_url"] = presigned_url(item["key"], download=True, expires=3600)
            public["download_url_expires_in"] = 3600
        if not output_expired and value["status"] == "completed" and public["media"] in {"video", "image"}:
            base = f"/api/account/tasks/{value['id']}/preview"
            public["preview_url"] = f"{base}/media"
            public["poster_url"] = f"{base}/poster"
        visible.append(public)
    started = value["started_at"]
    finished = value["finished_at"]
    result = {
        "id": value["id"],
        "kind": value["kind"],
        "tool": payload.get("_tool", "video"),
        "quality": value["quality"],
        "duration": value["duration"],
        "ratio": value["ratio"],
        "title": value["title"],
        "prompt_preview": prompt_preview,
        "transcript": str(((payload.get("_remix") or {}).get("analysis") or {}).get("transcript") or "")
        if (payload.get("_remix") or {}).get("version") == 2 else "",
        "note": value.get("portal_note", ""),
        "status": value["status"],
        "progress": value["progress"],
        "message": value["message"],
        "error": value["error"] if value["status"] == "failed" else "",
        "billing_status": value["billing_status"],
        "price": points(value["price_milli"]),
        "charged": points(value.get("charged_milli", 0) if isinstance(value, dict) else 0),
        "created_at": value["created_at"],
        "started_at": started,
        "finished_at": finished,
        "queue_seconds": round((started or now) - value["created_at"], 1),
        "run_seconds": round(((finished or now) - started), 1) if started else None,
        "output_expires_at": value["output_expires_at"],
        "output_expired": output_expired,
        "artifacts": visible,
    }
    if payload.get("_tool") == "reenact":
        result["warnings"] = list((payload.get("_reenact") or {}).get("warnings") or [])[:20]
        result["notes"] = list((payload.get("_reenact") or {}).get("notes") or [])[:10]
        result["template_id"] = (payload.get("_reenact") or {}).get("template_id")
    project = payload.get("_project")
    if isinstance(project, dict) and project.get("project_id"):
        result["project_id"] = project["project_id"]
        result["run_id"] = project.get("run_id")
    if include_prompts:
        result["original_prompt"] = str(
            payload.get("_original_prompt") or payload.get("prompt") or ""
        )
        result["processed_prompt"] = (
            str(payload.get("prompt") or "")
            if payload.get("_prompt_compiled")
            else None
        )
    return result


def _get_task(merchant_id: str, task_id: str):
    with connect() as db:
        return db.execute(
            """SELECT t.*,COALESCE(SUM(l.charged_milli),0) charged_milli
            FROM merchant_tasks t LEFT JOIN point_ledger l ON l.task_id=t.id
            WHERE t.id=? AND t.merchant_id=? GROUP BY t.id""",
            (task_id, merchant_id),
        ).fetchone()


def _get_portal_task(merchant_id: str, task_id: str):
    row = _get_task(merchant_id, task_id)
    return row if row and row["portal_hidden_at"] is None and _retained_output_exists(row) else None


def _retained_output_exists(row) -> bool:
    """Hide completed local outputs whose original file was already removed."""
    if row["status"] != "completed":
        return True
    artifacts = json.loads(row["artifacts"] or "[]")
    deliverables = [item for item in artifacts if item.get("media") in {"video", "image", "audio"}]
    if not deliverables:
        return False
    # The UI selects the first playable deliverable. If that exact file was
    # removed from the local output volume, hide the task instead of showing a
    # card that can never preview, even when a later auxiliary file remains.
    item = next(
        (artifact for artifact in deliverables if artifact.get("media") in {"video", "image"}),
        deliverables[0],
    )
    if item.get("storage") != "local":
        return True  # COS availability is governed by the recorded expiry.
    try:
        resolve_local_output(str(item["path"]))
        return True
    except (KeyError, FileNotFoundError, ValueError):
        return False


def _sniff_header(header: bytes) -> str | None:
    if header.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM")):
        return "image"
    if header.startswith((b"II*\x00", b"MM\x00*")) or (header.startswith(b"RIFF") and header[8:12] == b"WEBP"):
        return "image"
    if header[4:8] == b"ftyp":
        # HEIC/HEIF photos use the ISO-BMFF container too.  Keep them in the
        # image branch so the image decoder can normalize them later.
        brand = header[8:12].lower()
        if brand in {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"msf1"}:
            return "image"
        return "container"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "container"
    if header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        return "video"
    if header.startswith((b"\x00\x00\x01\xb3", b"\x47")):
        return "video"
    if header.startswith((b"ID3", b"fLaC", b"OggS")):
        return "audio"
    if len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0:
        return "audio"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio"
    return None


def _canonical_content_type(field: str, filename: str, content_type: str | None) -> str:
    """Normalize MIME values sent by mobile browsers before signing/validation."""
    value = (content_type or "").split(";", 1)[0].strip().lower()
    suffix = Path(filename or "").suffix.lower()
    images = {".heic":"image/heic", ".heif":"image/heif", ".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".png":"image/png", ".webp":"image/webp", ".gif":"image/gif", ".bmp":"image/bmp", ".tif":"image/tiff", ".tiff":"image/tiff"}
    videos = {".mp4":"video/mp4", ".m4v":"video/x-m4v", ".mov":"video/quicktime", ".qt":"video/quicktime", ".webm":"video/webm", ".3gp":"video/3gpp", ".3g2":"video/3gpp2", ".mkv":"video/x-matroska", ".avi":"video/x-msvideo"}
    if field in {"images", "first_frame", "last_frame"}:
        return images.get(suffix, value if value.startswith("image/") else "image/jpeg")
    if field == "videos":
        return videos.get(suffix, value if value.startswith("video/") else "video/mp4")
    if field == "audios":
        return value if value.startswith("audio/") else (mimetypes.guess_type(filename or "")[0] or "audio/mpeg")
    return value or "application/octet-stream"


def _validate_media_fields(kind: str, fields: list[str]) -> None:
    if kind == "text" and fields:
        raise HTTPException(422, "文生视频不接受上传素材")
    if kind == "image":
        if fields.count("first_frame") != 1:
            raise HTTPException(422, "首尾帧模式必须上传一个 first_frame")
        if any(field not in {"first_frame", "last_frame"} for field in fields):
            raise HTTPException(422, "首尾帧模式只接受 first_frame / last_frame")
        if len(fields) > 2 or fields.count("last_frame") > 1:
            raise HTTPException(422, "首尾帧素材数量不正确")
    if kind == "reference":
        if not fields:
            raise HTTPException(422, "多参考模式至少需要一个素材")
        if len(fields) > 12 or any(field not in {"images", "videos", "audios"} for field in fields):
            raise HTTPException(422, "多参考最多 12 个素材，字段只能是 images / videos / audios")
        if fields.count("images") > 9:
            raise HTTPException(422, "多参考图片最多 9 张")
        if fields.count("videos") > 3:
            raise HTTPException(422, "多参考视频最多 3 个")
        if fields.count("audios") > 3:
            raise HTTPException(422, "多参考音频最多 3 个")
def _expected_media() -> dict[str, str]:
    return {
        "first_frame": "image", "last_frame": "image", "images": "image",
        "videos": "video", "audios": "audio",
    }


def _validate_media(kind: str, fields: list[str], uploads: list[UploadFile]) -> None:
    _validate_media_fields(kind, fields)
    expected = _expected_media()
    total_size = 0
    for field, upload in zip(fields, uploads):
        content_type = _canonical_content_type(field, upload.filename or "", upload.content_type)
        if not content_type.startswith(expected[field] + "/"):
            raise HTTPException(422, f"{field} 的文件类型不正确")
        if upload.size is not None and upload.size > MAX_INPUT_BYTES:
            raise HTTPException(413, "单个素材不能超过 512MB")
        total_size += int(upload.size or 0)
        header = upload.file.read(16)
        upload.file.seek(0)
        detected = _sniff_header(header)
        if detected not in {expected[field], "container" if expected[field] in {"video", "audio"} else ""}:
            raise HTTPException(422, f"{field} 的文件内容与声明类型不一致")
    if total_size > MAX_INPUT_BYTES:
        raise HTTPException(413, "单次请求全部素材合计不能超过 512MB")


@router.post("/api/account/uploads/presign", include_in_schema=False)
def presign_portal_uploads(
    request: UploadGrantRequest,
    account: Annotated[dict, Depends(portal_merchant)],
) -> dict:
    if os.getenv("H3_DIRECT_UPLOADS", "0").strip() != "1":
        return {"enabled": False, "items": []}
    kind = request.kind.strip().lower()
    if kind not in ALLOWED_KINDS:
        raise HTTPException(422, "kind 必须是 text、image 或 reference")
    fields = [item.field for item in request.files]
    _validate_media_fields(kind, fields)
    expected = _expected_media()
    total_size = sum(item.size_bytes for item in request.files)
    if total_size > MAX_INPUT_BYTES:
        raise HTTPException(413, "单次请求全部素材合计不能超过 512MB")
    if account["status"] != "active":
        raise HTTPException(403, "账户未启用或已暂停")
    if transfer_backend() == "local" and os.getenv("H3_LOCAL_DIRECT_UPLOADS", "0").strip() == "1":
        grants = []
        for item in request.files:
            content_type = _canonical_content_type(item.field, item.filename, item.content_type)
            if not content_type.startswith(expected[item.field] + "/"):
                raise HTTPException(422, f"{item.field} 的文件类型不正确")
            grants.append(create_browser_upload_grant(
                account["id"], item.field, item.filename,
                content_type, item.size_bytes,
            ))
        return {"enabled": True, "items": grants}
    relay = upload_relay()
    if relay == "":
        # Relays are configured but none is healthy: the browser sends the files to this API instead.
        return {"enabled": False, "items": []}
    grants = []
    for item in request.files:
        content_type = _canonical_content_type(item.field, item.filename, item.content_type)
        if not content_type.startswith(expected[item.field] + "/"):
            raise HTTPException(422, f"{item.field} 的文件类型不正确")
        grant = create_upload_grant(
            account["id"], item.field, item.filename, content_type, item.size_bytes
        )
        if relay:
            payload = {key: value for key, value in grant.items() if key != "upload_url"}
            token = base64.urlsafe_b64encode(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            ).decode().rstrip("=")
            grant["upload_url"] = f"{relay}/upload?token={token}"
            grant["chunk_bytes"] = 4 * 1024 * 1024
            grant["parallelism"] = 4
        grants.append(grant)
    return {"enabled": True, "items": grants}


async def _validated_direct_assets(merchant_id: str, kind: str, raw: str) -> list[dict]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(422, "直传素材信息无效") from None
    if not isinstance(values, list) or not 1 <= len(values) <= 12:
        raise HTTPException(422, "直传素材数量无效")
    if not all(isinstance(item, dict) for item in values):
        raise HTTPException(422, "直传素材信息无效")
    fields = [str(item.get("field") or "") for item in values]
    _validate_media_fields(kind, fields)
    expected = _expected_media()

    async def validate_one(item: dict) -> dict:
        if item.get("storage") == "local":
            if not verify_browser_upload_grant(item, merchant_id):
                raise HTTPException(403, "素材上传授权无效或已过期")
            key = str(item["key"])
            with connect() as db:
                if db.execute("SELECT 1 FROM assets WHERE object_key=?", (key,)).fetchone():
                    raise HTTPException(409, "该素材已经用于其他任务")
            try:
                path = browser_upload_path(key)
                declared_size = int(item["size_bytes"])
                if path.stat().st_size != declared_size:
                    raise HTTPException(422, "素材上传不完整，请重新上传")
                with path.open("rb") as handle:
                    header = handle.read(16)
            except HTTPException:
                raise
            except (FileNotFoundError, OSError, ValueError):
                raise HTTPException(422, "没有找到完整上传的素材") from None
            field = str(item["field"])
            detected = _sniff_header(header)
            allowed = {
                expected[field],
                "container" if expected[field] in {"video", "audio"} else "",
            }
            if detected not in allowed:
                raise HTTPException(422, f"{field} 的文件内容与声明类型不一致")
            return {
                "id": public_id("ast"), "storage": "local", "bucket": "local-input",
                "key": key, "filename": Path(str(item["filename"])).name[:180],
                "content_type": _canonical_content_type(field, str(item["filename"]), str(item.get("content_type") or "")),
                "field": field, "size_bytes": declared_size,
            }
        if not verify_upload_grant(item, merchant_id):
            raise HTTPException(403, "素材上传授权无效或已过期")
        key = str(item["key"])
        with connect() as db:
            if db.execute("SELECT 1 FROM assets WHERE object_key=?", (key,)).fetchone():
                raise HTTPException(409, "该素材已经用于其他任务")
        try:
            metadata, header = await asyncio.gather(
                asyncio.to_thread(object_metadata, key),
                asyncio.to_thread(read_prefix, key, 16),
            )
        except Exception as exc:
            raise HTTPException(422, "没有找到完整上传的素材") from exc
        declared_size = int(item["size_bytes"])
        if metadata["size"] != declared_size:
            raise HTTPException(422, "素材上传不完整，请重新上传")
        field = str(item["field"])
        detected = _sniff_header(header)
        allowed = {
            expected[field],
            "container" if expected[field] in {"video", "audio"} else "",
        }
        if detected not in allowed:
            raise HTTPException(422, f"{field} 的文件内容与声明类型不一致")
        return {
            "id": public_id("ast"),
            "storage": "cos",
            "bucket": bucket_name(),
            "key": key,
            "filename": Path(str(item["filename"])).name[:180],
            "content_type": _canonical_content_type(field, str(item["filename"]), str(item.get("content_type") or "")),
            "field": field,
            "size_bytes": declared_size,
        }

    return list(await asyncio.gather(*(validate_one(item) for item in values)))


async def direct_assets_as_uploads(
    merchant_id: str, kind: str, raw: str, *, rename: dict[str, str] | None = None,
) -> tuple[list[dict], list[UploadFile]]:
    """Validate browser-staged assets and expose them as UploadFile.

    Direct upload exists because a multipart POST to equai.vip crosses the Hong
    Kong proxy at ~0.2 MB/s and dies on its 60-second ceiling: a single 11 MB
    image already fails. The staged bytes are already on this host, so handing
    the endpoints an UploadFile over the local file lets every existing
    validator keep working while nothing travels twice.

    ``rename`` maps an endpoint's own field name onto the one the grant uses
    (digital human posts ``image``/``audio``; grants speak ``images``/``audios``).
    """
    objects = await _validated_direct_assets(merchant_id, kind, raw)
    back = {value: key for key, value in (rename or {}).items()}
    uploads: list[UploadFile] = []
    for item in objects:
        if item.get("storage") == "local":
            handle = browser_upload_path(str(item["key"])).open("rb")
        else:
            # COS fallback: production stages to this host's disk, so this path
            # only runs if the transfer backend is switched back to object storage.
            staged = Path(tempfile.mkdtemp(prefix="direct-")) / "asset"
            await asyncio.to_thread(download_path, str(item["key"]), staged)
            handle = staged.open("rb")
        upload = UploadFile(
            file=handle, size=int(item.get("size_bytes") or 0),
            filename=str(item.get("filename") or "asset"),
            headers=Headers({"content-type": str(item.get("content_type") or "application/octet-stream")}),
        )
        upload.field = back.get(str(item["field"]), str(item["field"]))  # type: ignore[attr-defined]
        uploads.append(upload)
    return objects, uploads


async def restage_normalized(objects: list[dict], uploads: list[UploadFile]) -> None:
    """Persist validator rewrites back onto the staged asset.

    Several validators re-encode images in place (normalize_upload swaps
    upload.file, size and filename). For a multipart request that rewritten
    buffer is what gets stored; for a staged asset the bytes already sit on
    disk, so the rewrite has to be written back or the task would run on the
    original file while the record describes the normalised one.
    """
    for item, upload in zip(objects, uploads):
        upload.file.seek(0)
        data = upload.file.read()
        upload.file.seek(0)
        if int(item.get("size_bytes") or 0) == len(data) and item.get("filename") == upload.filename:
            continue
        if item.get("storage") == "local":
            await asyncio.to_thread(browser_upload_path(str(item["key"])).write_bytes, data)
        else:
            await asyncio.to_thread(
                upload_fileobj, io.BytesIO(data), str(item["key"]),
                str(item.get("content_type") or "application/octet-stream"),
            )
        item["size_bytes"] = len(data)
        item["filename"] = Path(upload.filename or item["filename"]).name[:180]


def _uploads_named(uploads: list[UploadFile], field: str) -> list[UploadFile]:
    return [item for item in uploads if getattr(item, "field", "") == field]


def _validate_video_audio_flags(kind: str, fields: list[str], value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if kind != "reference":
        raise HTTPException(422, "video_audio_flags 只用于多参考模式")
    flags = [item.strip() for item in value.split(",")]
    video_count = fields.count("videos")
    if len(flags) != video_count or any(item not in {"0", "1"} for item in flags):
        raise HTTPException(
            422,
            f"video_audio_flags 必须为每个参考视频提供一个 0 或 1，当前视频数量为 {video_count}",
        )
    return ",".join(flags)


async def _upload(
    merchant_id: str,
    task_id: str,
    index: int,
    field: str,
    upload: UploadFile,
    *,
    force_local: bool = False,
    shrink_videos: bool = True,
    shrink_images: bool = False,
) -> dict:
    safe = Path(upload.filename or f"file-{index}").name[:180]
    content_type = _canonical_content_type(field, safe, upload.content_type)
    asset_id = public_id("ast")
    shrunk: tempfile.TemporaryDirectory | None = None
    if shrink_images and field == "images" and content_type.startswith("image/"):
        async with reference_image.slots:
            shrunk, safe = await asyncio.to_thread(
                _shrink_reference_image_upload, upload, safe
            )
        content_type = _canonical_content_type(field, safe, None)
    if shrink_videos and field == "videos" and content_type.startswith("video/"):
        async with reference_video.slots:
            shrunk = await asyncio.to_thread(_shrink_reference_upload, upload, safe)
    try:
        return await _store_upload(
            merchant_id, task_id, index, field, upload, safe, content_type,
            asset_id, force_local=force_local,
        )
    finally:
        if shrunk is not None:
            shrunk.cleanup()


def _shrink_reference_image_upload(upload: UploadFile, safe: str):
    """Replace a person image with its normalized small model-input version."""
    holder = tempfile.TemporaryDirectory(prefix="refimg-")
    root = Path(holder.name)
    source = root / Path(safe).name
    try:
        upload.file.seek(0)
        with source.open("wb") as handle:
            shutil.copyfileobj(upload.file, handle)
        upload.file.seek(0)
        target = reference_image.downscale_file(source, root)
        if target is None:
            raise HTTPException(422, "人物图处理失败，请重新上传清晰的 PNG、JPEG 或 WebP 图片")
        old_file = upload.file
        upload.file = target.open("rb")
        upload.size = target.stat().st_size
        upload.filename = f"{Path(safe).stem}{target.suffix}"
        old_file.close()
        log.info("Reference image %s normalized to %s bytes", safe, upload.size)
        return holder, upload.filename
    except HTTPException:
        holder.cleanup()
        raise
    except OSError as exc:
        holder.cleanup()
        raise HTTPException(422, "人物图处理失败，请重新上传清晰的 PNG、JPEG 或 WebP 图片") from exc


def _shrink_reference_upload(upload: UploadFile, safe: str):
    """Replace upload.file with a downscaled copy when it is worth doing."""
    holder = tempfile.TemporaryDirectory(prefix="refvid-")
    root = Path(holder.name)
    source, target = root / "source.mp4", root / "small.mp4"
    try:
        upload.file.seek(0)
        with source.open("wb") as handle:
            shutil.copyfileobj(upload.file, handle)
        upload.file.seek(0)
        if not reference_video.downscale_file(source, target):
            holder.cleanup()
            return None
        handle = target.open("rb")
        upload.file = handle
        upload.size = target.stat().st_size
        log.info("Reference video %s downscaled to %s bytes", safe, upload.size)
        return holder
    except OSError:
        log.warning("Reference video staging failed for %s", safe, exc_info=True)
        holder.cleanup()
        return None


async def _store_upload(
    merchant_id: str,
    task_id: str,
    index: int,
    field: str,
    upload: UploadFile,
    safe: str,
    content_type: str,
    asset_id: str,
    *,
    force_local: bool = False,
) -> dict:
    if force_local or transfer_backend() == "local":
        if not local_input_capacity_ok(upload.size or 0, force=force_local):
            raise RuntimeError("临时上传空间不足，请稍后重试")
        relative, size = await asyncio.to_thread(
            store_local_input, upload.file, merchant_id, task_id, index, safe
        )
        return {
            "id": asset_id, "storage": "local", "bucket": "local-input", "key": relative,
            "filename": safe, "content_type": content_type, "field": field, "size_bytes": size,
        }
    key = f"{INPUT_PREFIX}/{merchant_id}/{task_id}/{index:02d}-{safe}"
    await asyncio.to_thread(upload_fileobj, upload.file, key, content_type)
    return {
        "id": asset_id, "storage": "cos", "bucket": bucket_name(), "key": key,
        "filename": safe, "content_type": content_type, "field": field,
    }


async def _remove_uploaded_inputs(objects: list[dict]) -> None:
    local = [item["key"] for item in objects if item.get("storage") == "local"]
    remote = [item["key"] for item in objects if item.get("storage") != "local"]
    for relative in local:
        await asyncio.to_thread(remove_local_input, relative)
    if remote:
        await asyncio.to_thread(delete_keys, remote)


def _uploaded_audio_seconds(upload: UploadFile) -> float:
    upload.file.seek(0)
    holder = tempfile.TemporaryDirectory(prefix="digital-human-audio-")
    path = Path(holder.name) / (Path(upload.filename or "voice.wav").name or "voice.wav")
    try:
        with path.open("wb") as target:
            shutil.copyfileobj(upload.file, target)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            check=True, capture_output=True, text=True, timeout=30,
        )
        duration = float(probe.stdout.strip())
        if not 1 <= duration <= 60:
            raise HTTPException(422, "音频时长须为 1–60 秒")
        return duration
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, "无法读取音频时长，请使用 MP3、WAV、M4A 或 FLAC") from exc
    finally:
        upload.file.seek(0)
        holder.cleanup()


@router.post("/api/account/digital-human/720p", status_code=202, include_in_schema=False)
async def create_digital_human(
    request: Request,
    merchant: Annotated[dict, Depends(video_merchant)],
    mode: Annotated[str, Form()],
    # Optional so a direct_assets request carries no file parts at all; the
    # pair is required either way and checked below.
    image: Annotated[UploadFile | None, File()] = None,
    audio: Annotated[UploadFile | None, File()] = None,
    # The staged-upload grant speaks the shared media vocabulary (images/audios),
    # so the form uses those names and falls back to multipart with them intact.
    images: Annotated[list[UploadFile] | None, File()] = None,
    audios: Annotated[list[UploadFile] | None, File()] = None,
    ratio: Annotated[str, Form()] = "16:9",
    prompt: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "数字人",
    direct_assets: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    from long_video_api import (
        DIGITAL_HUMAN_MIN_DURATION, MAX_DURATION, PRICE_PER_SEGMENT_MILLI,
        digital_human_segment_count,
    )

    mode, ratio = mode.strip().lower(), ratio.strip()
    if image is None and images:
        image = images[0]
    if audio is None and audios:
        audio = audios[0]
    if (images and len(images) > 1) or (audios and len(audios) > 1):
        raise HTTPException(422, "数字人只接受一张人物图和一段音频")
    if direct_assets:
        # An audio track may be 300 MB. Through the Hong Kong proxy that is
        # eleven minutes at best and dies on its 60-second ceiling long before
        # then, so the browser stages both files on this host first.
        if (image is not None and image.filename) or (audio is not None and audio.filename):
            raise HTTPException(422, "直传素材不能与 multipart 素材混用")
        _staged, staged = await direct_assets_as_uploads(
            merchant["id"], "reference", direct_assets,
            rename={"image": "images", "audio": "audios"},
        )
        pictures, voices = _uploads_named(staged, "image"), _uploads_named(staged, "audio")
        if len(pictures) != 1 or len(voices) != 1:
            raise HTTPException(422, "数字人需要一张人物图和一段音频")
        image, audio = pictures[0], voices[0]
    from digital_human_prompt import compose_digital_human_workflow_prompt

    direction = prompt.strip()
    if len(direction) > 30000:
        raise HTTPException(422, "额外提示词不能超过 30000 个字符")
    try:
        workflow_prompt = compose_digital_human_workflow_prompt(direction)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        raise HTTPException(500, "数字人工作流提示词模板无法读取") from exc
    if mode != "audio":
        raise HTTPException(422, "数字人目前仅支持上传音频驱动")
    if ratio not in {"16:9", "9:16"}:
        raise HTTPException(422, "数字人画幅只能是 16:9 或 9:16")
    if image is None or not image.filename:
        raise HTTPException(422, "请上传一张人物图")
    if Path(image.filename).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or (image.size or 0) > 30 * 1024**2:
        raise HTTPException(422, "请上传 30 MB 以内的 PNG、JPEG 或 WebP 人物图")
    image.file.seek(0)
    if _sniff_header(image.file.read(16)) != "image":
        raise HTTPException(422, "人物图文件内容不是有效的 PNG、JPEG 或 WebP 图片")
    image.file.seek(0)
    if not audio or not audio.filename:
        raise HTTPException(422, "请上传一段音频")
    if Path(audio.filename).suffix.lower() not in {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"} or (audio.size or 0) > 300 * 1024**2:
        raise HTTPException(422, "请上传 300 MB 以内的 MP3、WAV、M4A、FLAC、AAC 或 OGG")
    duration = _uploaded_audio_seconds(audio)
    if not DIGITAL_HUMAN_MIN_DURATION <= duration <= MAX_DURATION:
        raise HTTPException(422, f"数字人音频时长须为 {DIGITAL_HUMAN_MIN_DURATION}–{MAX_DURATION} 秒")
    render_duration = duration
    segments = digital_human_segment_count(duration)
    price_milli = PRICE_PER_SEGMENT_MILLI * segments
    identity = {"mode": mode, "duration": duration, "ratio": ratio,
                "prompt": direction,
                "workflow_id": "digital_human_image_audio_v1",
                "image": [image.filename, image.size],
                "audio": [audio.filename, audio.size] if audio and audio.filename else None}
    request_hash = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if idempotency_key:
        idempotency_key = idempotency_key.strip()[:120]
        if not idempotency_key:
            raise HTTPException(422, "Idempotency-Key 不能为空")
        with connect() as db:
            existing = db.execute("SELECT * FROM merchant_tasks WHERE merchant_id=? AND idempotency_key=?", (merchant["id"], idempotency_key)).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise HTTPException(409, "同一个 Idempotency-Key 不能用于不同请求")
            return _task_view(existing, with_download=False)
    if merchant["status"] != "active":
        raise HTTPException(403, "账户未启用或已暂停")
    if merchant["api_key_status"] != "active" or merchant["balance_milli"] < price_milli:
        raise HTTPException(402, f"该数字人任务需要 {points(price_milli)} 积分")

    task_id, objects = public_id("vid"), []
    try:
        objects.append(await _upload(merchant["id"], task_id, 0, "images", image,
                                     force_local=request.url.path.startswith("/api/account/"),
                                     shrink_images=True))
        objects.append(await _upload(merchant["id"], task_id, 1, "audios", audio,
                                     force_local=request.url.path.startswith("/api/account/")))
        payload = {"mode": "digital_human_workflow", "workflow_id": "digital_human_image_audio_v1", "tier": "720p",
                   "duration": duration, "source_audio_duration": duration,
                   "ratio": ratio, "quality": 0.9, "megapixels": 1.0,
                   "prompt": workflow_prompt,
                   # With a direction the scheduler merges it into the template
                   # through DeepSeek before dispatch; workflow_prompt above is
                   # the literal-append fallback it starts from. With no
                   # direction the pinned template is used unchanged and no
                   # prompt-service call is made.
                   "_direction": direction,
                   "_tool": "digital_human", "_prompt_compiled": not direction,
                   "_transfer_backend": "local"}
        now = time.time()
        with transaction() as db:
            db.execute(
                """INSERT INTO merchant_tasks
                (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,payload,
                 input_objects,price_milli,failure_price_milli,idempotency_key,request_hash,created_at,queued_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, merchant["id"], "long_text", "720p", render_duration, ratio, title[:100],
                 "queued", 5, "数字人任务已进入队列", json.dumps(payload, ensure_ascii=False),
                 json.dumps(objects, ensure_ascii=False), price_milli, 0, idempotency_key,
                 request_hash, now, now),
            )
            for item in objects:
                db.execute("""INSERT INTO assets
                    (id,merchant_id,task_id,role,bucket,object_key,filename,content_type,size_bytes,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (item["id"], merchant["id"], task_id, "input", item["bucket"], item["key"],
                     item["filename"], item["content_type"], item.get("size_bytes"), now))
            reserve_task(db, merchant["id"], task_id, "720p", amount_milli=price_milli)
    except sqlite3.IntegrityError:
        if objects:
            await _remove_uploaded_inputs(objects)
        if idempotency_key:
            with connect() as db:
                existing = db.execute(
                    "SELECT * FROM merchant_tasks WHERE merchant_id=? AND idempotency_key=?",
                    (merchant["id"], idempotency_key),
                ).fetchone()
            if existing and existing["request_hash"] == request_hash:
                return _task_view(existing, with_download=False)
        raise
    except Exception:
        if objects:
            await _remove_uploaded_inputs(objects)
        raise
    return _task_view(_get_task(merchant["id"], task_id), with_download=False)


@router.post("/v1/videos/720p", status_code=202)
@router.post("/v1/videos/1080p", status_code=202)
@router.post("/v1/videos/2k", status_code=202)
@router.post("/api/account/videos/720p", status_code=202, include_in_schema=False)
@router.post("/api/account/videos/1080p", status_code=202, include_in_schema=False)
@router.post("/api/account/videos/2k", status_code=202, include_in_schema=False)
@router.post("/api/account/remix/720p", status_code=202, include_in_schema=False)
@router.post("/api/account/remix2/720p", status_code=202, include_in_schema=False)
@router.post("/api/account/commerce-remix/720p", status_code=202, include_in_schema=False)
@router.post("/api/account/video-edits/720p", status_code=202, include_in_schema=False)
@router.post("/v1/remix/text/720p", status_code=202)
@router.post("/v1/remix/image/720p", status_code=202)
@router.post("/v1/remix/reference/720p", status_code=202)
async def create_video(
    request: Request,
    merchant: Annotated[dict, Depends(video_merchant)],
    kind: Annotated[str, Form()],
    prompt: Annotated[str, Form()] = "",
    duration: Annotated[int, Form()] = 15,
    ratio: Annotated[str, Form()] = "16:9",
    title: Annotated[str, Form()] = "EQUAI video",
    note: Annotated[str, Form()] = "",
    seed: Annotated[int, Form()] = 0,
    video_audio_flags: Annotated[str, Form()] = "",
    first_frame: Annotated[UploadFile | None, File()] = None,
    last_frame: Annotated[UploadFile | None, File()] = None,
    uploads: Annotated[list[UploadFile] | None, File()] = None,
    upload_fields: Annotated[list[str] | None, Form()] = None,
    images: Annotated[list[UploadFile] | None, File()] = None,
    videos: Annotated[list[UploadFile] | None, File()] = None,
    audios: Annotated[list[UploadFile] | None, File()] = None,
    direct_assets: Annotated[str, Form()] = "",
    remix_roles: Annotated[str, Form()] = "[]",
    remix_template: Annotated[str, Form()] = "auto",
    remix_audio: Annotated[str, Form()] = "original",
    edit_mode: Annotated[str, Form()] = "edit",
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    kind = kind.strip().lower()
    quality = request.url.path.rsplit("/", 1)[-1]
    prompt = prompt.strip()
    note = note.strip()
    # Empty prompt is the new fixed “视频复刻” UX. A non-empty legacy request
    # remains on the analysis-backed editor path for compatibility.
    is_v2_remix = request.url.path.startswith("/api/account/remix2/")
    is_commerce_remix = request.url.path.startswith("/api/account/commerce-remix/")
    is_simple_portal_remix = is_v2_remix or (
        request.url.path.startswith("/api/account/remix/") and not prompt
    )
    is_video_editor = request.url.path.startswith("/api/account/video-edits/")
    is_remix = request.url.path.startswith(("/api/account/remix/", "/api/account/remix2/", "/api/account/commerce-remix/", "/api/account/video-edits/", "/v1/remix/"))
    is_direct_remix = request.url.path.startswith("/v1/remix/")
    remix = None
    # Set only when an actual video asset is present. Pure image/text requests
    # must not enter the video 360p preflight path.
    shrink_reference = False
    direct_objects: list[dict] = []
    task_price_milli = PRICE_MILLI[quality]
    if is_remix:
        from commerce import (
            remix_direction, simple_remix_prompt, generic_editor_prompt, infer_editor_roles, remix_price_milli, video_editor_price_milli,
            commerce_remix_price_milli, validate_commerce_remix_uploads,
            validate_editor_assets, validate_editor_uploads, validate_remix, validate_remix_assets,
        )
        if is_direct_remix:
            endpoint_kind = request.url.path.split("/")[-2]
            expected_kind = {"text": "text", "image": "image", "reference": "reference"}.get(endpoint_kind)
            if expected_kind is None or kind != expected_kind:
                raise HTTPException(422, f"该 remix 接口要求 kind={expected_kind or 'text|image|reference'}")
            if direct_assets:
                if videos or images or audios or first_frame or last_frame or uploads or upload_fields:
                    raise HTTPException(422, "直传素材不能与 multipart 素材混用")
                direct_objects = await _validated_direct_assets(
                    merchant["id"], kind, direct_assets
                )
            remix = {
                "direct": True, "roles": [], "template": "direct",
                "audio": "model", "source_seconds": float(duration),
            }
            title = "remix（支持视频编辑）"
            task_price_milli = remix_price_milli(quality)
        elif is_commerce_remix:
            if kind != "reference" or uploads or upload_fields or first_frame or last_frame or audios:
                raise HTTPException(422, "电商复刻需要一条原视频和可选的人物、商品、场景图片")
            if direct_assets:
                if videos or images:
                    raise HTTPException(422, "直传素材不能与 multipart 素材混用")
                direct_objects, staged = await direct_assets_as_uploads(
                    merchant["id"], kind, direct_assets,
                )
                remix = await validate_commerce_remix_uploads(
                    _uploads_named(staged, "videos"), _uploads_named(staged, "images"), remix_roles,
                )
                await restage_normalized(direct_objects, staged)
            else:
                remix = await validate_commerce_remix_uploads(
                    videos or [], images or [], remix_roles,
                )
            duration, ratio = remix["duration"], remix["ratio"]
            prompt = "根据原片的分镜、动作、声音时间轴和深度参考视频生成电商复刻视频。"
            video_audio_flags = "1" if remix.get("has_audio") else "0"
            remix.update({"source_seconds": remix["source_seconds"], "mode": "commerce_depth",
                          "finalize_original_audio": True, "version": 3})
            title = "电商复刻"
            task_price_milli = commerce_remix_price_milli()
            shrink_reference = False
        elif is_simple_portal_remix:
            if kind != "reference" or uploads or upload_fields or first_frame or last_frame or audios:
                raise HTTPException(422, "视频复刻需要参考视频和替换图片")
            if direct_assets:
                if videos or images:
                    raise HTTPException(422, "直传素材不能与 multipart 素材混用")
                direct_objects = await _validated_direct_assets(
                    merchant["id"], kind, direct_assets
                )
                remix = await validate_remix_assets(
                    direct_objects, remix_roles, remix_audio, remix_template,
                )
            else:
                remix = await validate_remix(
                    videos or [], images or [], remix_roles, remix_audio, remix_template,
                )
            duration, ratio = remix["duration"], remix["ratio"]
            prompt = simple_remix_prompt(remix["template"], duration, ratio)
            video_audio_flags = "0"
            remix.update({"direct": not is_v2_remix, "mode": "clone", "finalize_original_audio": True,
                          "version": 2 if is_v2_remix else 1})
            title = "电商视频复刻 2" if is_v2_remix else "视频复刻"
            task_price_milli = remix_price_milli(quality)
        elif is_video_editor:
            if kind != "reference" or uploads or upload_fields or first_frame or last_frame or audios:
                raise HTTPException(422, "视频编辑需要原视频和可选参考图片")
            reenact_mode = edit_mode.strip().lower() == "reenact"
            if direct_assets:
                if videos or images:
                    raise HTTPException(422, "直传素材不能与 multipart 素材混用")
                # Reenactment needs the full-resolution source (it samples frames for the new pictures)
                # and reads it on the GPU side anyway: no 360p rewrite, no full download to probe it.
                direct_objects = await _validated_direct_assets(
                    merchant["id"], kind, direct_assets
                )
                remix = await validate_editor_assets(direct_objects, remix_audio)
            else:
                remix = await validate_editor_uploads(videos or [], images or [], remix_audio)
            duration, ratio = remix["duration"], remix["ratio"]
            if not prompt:
                raise HTTPException(422, "请填写编辑 Prompt")
            user_prompt = prompt
            uploaded_roles = (
                infer_editor_roles(user_prompt, len(remix["roles"]))[0]
                if remix["roles"] else []
            )
            roles, template = infer_editor_roles(user_prompt, len(remix["roles"]))
            prompt = user_prompt
            video_audio_flags = "0"
            remix.update({"roles": roles, "uploaded_roles": uploaded_roles,
                          "template": template, "editor_mode": "generic",
                          "direct": False,
                          "mode": "generic_editor", "finalize_original_audio": True})
            title = "视频编辑项目"
            task_price_milli = video_editor_price_milli(float(remix["source_seconds"]))
            if reenact_mode:
                from commerce import reenact_price_milli
                remix = {"source_seconds": remix["source_seconds"], "has_audio": remix.get("has_audio"),
                         "image_sha256": remix.get("image_sha256") or [], "prompt": user_prompt,
                         "mode": "reenact"}
                title = "视频重演"
                shrink_reference = False
                task_price_milli = reenact_price_milli(float(remix["source_seconds"]))
        else:
            if kind != "reference" or uploads or upload_fields or first_frame or last_frame or audios:
                raise HTTPException(422, "视频复刻需要参考视频和可选的替换图片")
            if direct_assets:
                if videos or images:
                    raise HTTPException(422, "直传素材不能与 multipart 素材混用")
                # The legacy portal flow feeds this video to Qwen-VL for shot
                # analysis. Downscaling is unverified for that stage, so it is
                # left at full resolution until measured separately. The hypit
                # path hands this video to H3 as well, so it takes the R88 downscale.
                direct_objects = await _validated_direct_assets(
                    merchant["id"], kind, direct_assets
                )
                remix = await validate_remix_assets(
                    direct_objects, remix_roles, remix_audio, remix_template,
                )
            else:
                remix = await validate_remix(
                    videos or [], images or [], remix_roles, remix_audio, remix_template,
                )
            duration, ratio = remix["duration"], remix["ratio"]
            # Only the hypit path hands this video to H3; the legacy analysis
            # stage still reads it at full resolution.
            shrink_reference = remix_hypit.enabled()
            prompt = remix_direction(remix["template"], prompt)
            # Soundtrack copying is handled at delivery, not by the video model.
            video_audio_flags = "0" if remix_audio in {"original", "mute"} else "1"
            title = "视频编辑"
            task_price_milli = remix_price_milli(quality)
    if kind not in ALLOWED_KINDS:
        raise HTTPException(422, "kind 必须是 text、image 或 reference")
    if (duration not in ALLOWED_DURATION and not (is_video_editor and duration >= 5)):
        raise HTTPException(422, "duration 只能是 5–15 秒之间的整数")
    if ratio not in {"16:9", "9:16"}:
        raise HTTPException(422, "ratio 只能是 16:9 或 9:16")
    # Legacy portal callers may omit an instruction; the fixed remix wrapper
    # supplies the actual H3 direction. Generic video-editor requests reject
    # empty Prompt earlier in their dedicated branch.
    if not prompt and is_remix and not is_direct_remix:
        prompt = "保留原视频的镜头、动作、节奏和声音，按照参考素材完成编辑。"
    if not prompt or len(prompt) > 30000:
        raise HTTPException(422, "prompt 长度需要 1–30000 字符")
    if len(note) > 80:
        raise HTTPException(422, "备注不能超过 80 个字符")
    uploads = uploads or []
    upload_fields = upload_fields or []
    named_reference_files = (images or []) + (videos or []) + (audios or [])
    if named_reference_files and (uploads or upload_fields):
        raise HTTPException(422, "多参考请使用 images / videos / audios，不能与 uploads / upload_fields 混用")
    for field, files in (("images", images or []), ("videos", videos or []), ("audios", audios or [])):
        uploads.extend(files)
        upload_fields.extend([field] * len(files))
    if first_frame is not None:
        uploads.append(first_frame)
        upload_fields.append("first_frame")
    if last_frame is not None:
        uploads.append(last_frame)
        upload_fields.append("last_frame")
    if len(uploads) != len(upload_fields):
        raise HTTPException(422, "uploads 与 upload_fields 数量不一致")
    if direct_assets and uploads:
        raise HTTPException(422, "直传素材不能与 multipart 素材混用")
    if direct_assets and not direct_objects:
        direct_objects = await _validated_direct_assets(
            merchant["id"], kind, direct_assets
        )
    elif not direct_assets:
        _validate_media(kind, upload_fields, uploads)
    direct_fields = [item["field"] for item in direct_objects]
    video_audio_flags = _validate_video_audio_flags(
        kind, direct_fields if direct_objects else upload_fields, video_audio_flags
    )
    request_identity = {
        "kind": kind,
        "quality": quality,
        "prompt": prompt,
        "duration": duration,
        "ratio": ratio,
        "seed": seed,
        "video_audio_flags": video_audio_flags,
        "title": title[:100],
        "note": note,
        "files": [
            {"field": field, "name": Path(upload.filename or "").name, "size": upload.size}
            for field, upload in zip(upload_fields, uploads)
        ] + [
            {"field": item["field"], "name": item["filename"], "size": item["size_bytes"]}
            for item in direct_objects
        ],
    }
    if remix is not None:
        request_identity["remix"] = remix
    request_hash = hashlib.sha256(
        json.dumps(request_identity, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    if idempotency_key:
        idempotency_key = idempotency_key.strip()[:120]
        if not idempotency_key:
            raise HTTPException(422, "Idempotency-Key 不能为空")
        with connect() as db:
            existing = db.execute(
                "SELECT * FROM merchant_tasks WHERE merchant_id=? AND idempotency_key=?",
                (merchant["id"], idempotency_key),
            ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise HTTPException(409, "同一个 Idempotency-Key 不能用于不同请求")
            return _task_view(existing, with_download=False)

    if merchant["status"] != "active":
        raise HTTPException(403, "账户未启用或已暂停")
    if merchant["api_key_status"] != "active":
        raise HTTPException(402, "积分不足，API Key 已暂停生成权限")
    if merchant["balance_milli"] < task_price_milli:
        raise HTTPException(402, f"{quality} 任务需要 {points(task_price_milli)} 积分")

    task_id = public_id("vid")
    objects: list[dict] = list(direct_objects)
    try:
        for index, (field, upload) in enumerate(zip(upload_fields, uploads)):
            # Portal uploads have already crossed the user's network once.
            # Persist them on the private HK transfer volume and return the
            # task promptly instead of synchronously sending every file from
            # HK to Guangzhou COS before the 202 response. External /v1 calls
            # keep the configured transfer backend behavior.
            objects.append(await _upload(
                merchant["id"], task_id, index, field, upload,
                force_local=request.url.path.startswith("/api/account/"),
                # The legacy portal remix flow sends this video to Qwen-VL for
                # shot analysis. Keep it at full resolution here so multipart
                # and direct uploads behave identically on that path.
                shrink_videos=(not is_remix) or is_simple_portal_remix
                or (is_video_editor and edit_mode.strip().lower() != "reenact"),
            ))

        # Ref2VA picture numbering is semantic, not upload-order dependent.
        # Normalize every simple-clone task to Picture 1=person,
        # Picture 2=product, Picture 3=scene before dispatch.
        if is_simple_portal_remix and remix is not None:
            try:
                supplied_roles = json.loads(remix_roles)
                by_role = {
                    role: item for role, item in zip(supplied_roles, [o for o in objects if o.get("field") == "images"])
                }
                ordered_images = [by_role[role] for role in ("person", "product", "scene") if role in by_role]
                objects = [o for o in objects if o.get("field") != "images"] + ordered_images
            except (TypeError, ValueError, KeyError):
                raise HTTPException(422, "参考图片用途与文件顺序不一致") from None
        if is_commerce_remix and remix is not None:
            try:
                roles = remix["roles"]
                by_role = {
                    role: item for role, item in zip(
                        roles, [o for o in objects if o.get("field") == "images"]
                    )
                }
                ordered_images = [by_role[role] for role in ("person", "product", "scene") if role in by_role]
                objects = [o for o in objects if o.get("field") != "images"] + ordered_images
            except (TypeError, ValueError, KeyError):
                raise HTTPException(422, "参考图片用途与文件顺序不一致") from None

        # Every workflow that carries a video uses the same 360p preflight
        # before entering a generation/analysis provider. This deliberately
        # includes reenactment: the browser may upload the original, but the
        # scheduler rewrites the local input before dispatching it downstream.
        # Keeping this decision after direct and multipart assets are merged
        # makes both upload paths behave identically.
        if any(item.get("field") == "videos" for item in objects) and not is_commerce_remix:
            shrink_reference = True

        worker_mode = f"{kind}_latent_hd" if quality in {"1080p", "2k"} else kind
        refine_ladder = {
            "720p": [],
            "1080p": ["1920x1088"],
            "2k": ["1920x1088", "2560x1440"],
        }[quality]
        payload = {
            "mode": worker_mode,
            "tier": quality,
            "prompt": prompt,
            "duration": duration,
            "ratio": ratio,
            "quality": 0.9,
            "seed": seed,
            "video_audio_flags": video_audio_flags,
            "output_quality": 2.0,
            "refine_denoise": 0.35,
            "refine_steps": 2,
            "refine_extra_steps": 0,
            "refine_ladder": refine_ladder,
            # The editor owns its source clips, intermediate Takes and final
            # Timeline on the Guangzhou disk. Keep the public video APIs
            # configurable, but never let an editor project silently fall
            # back to COS.
            "_transfer_backend": "local" if is_video_editor else transfer_backend(),
            "_shrink_reference": shrink_reference,
        }
        if remix is not None and remix.get("mode") == "reenact":
            payload["_tool"] = "reenact"
            payload["_reenact"] = remix
        elif is_commerce_remix and remix is not None:
            payload["_tool"] = "commerce_remix"
            payload["_commerce_remix"] = remix
        elif remix is not None:
            payload["_tool"] = "remix"
            payload["_remix"] = remix
            if is_simple_portal_remix and not is_v2_remix:
                payload["_prompt_compiled"] = True
        now = time.time()
        project_ids: tuple[str, str] | None = None
        with transaction() as db:
            db.execute(
                """INSERT INTO merchant_tasks
                (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,payload,portal_note,
                 input_objects,price_milli,failure_price_milli,idempotency_key,request_hash,created_at,queued_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, merchant["id"], kind, quality, duration, ratio, title[:100],
                    "queued", 5, "任务已进入队列", json.dumps(payload, ensure_ascii=False),
                    note, json.dumps(objects, ensure_ascii=False), task_price_milli,
                    FAILURE_PRICE_MILLI[quality], idempotency_key, request_hash, now, now,
                ),
            )
            for item in objects:
                db.execute(
                    """INSERT INTO assets
                    (id,merchant_id,task_id,role,bucket,object_key,filename,content_type,size_bytes,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item["id"], merchant["id"], task_id, "input", item["bucket"], item["key"],
                        item["filename"], item["content_type"], item.get("size_bytes"), now,
                    ),
                    )
            if is_video_editor:
                project_id, run_id = video_projects.create_for_editor(
                    db,
                    merchant_id=merchant["id"],
                    task_id=task_id,
                    title=title,
                    brief=prompt,
                    input_objects=objects,
                    prompt=prompt,
                    duration=duration,
                    ratio=ratio,
                )
                project_ids = (project_id, run_id)
                payload["_project"] = {
                    "project_id": project_id,
                    "run_id": run_id,
                    "version": 1,
                    "mode": "hypit_project",
                }
                db.execute(
                    "UPDATE merchant_tasks SET payload=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), task_id),
                )
            reserve_task(
                db, merchant["id"], task_id, quality,
                amount_milli=task_price_milli,
            )
    except sqlite3.IntegrityError:
        if objects:
            await _remove_uploaded_inputs(objects)
        if idempotency_key:
            with connect() as db:
                existing = db.execute(
                    "SELECT * FROM merchant_tasks WHERE merchant_id=? AND idempotency_key=?",
                    (merchant["id"], idempotency_key),
                ).fetchone()
            if existing and existing["request_hash"] == request_hash:
                return _task_view(existing, with_download=False)
        raise
    except PermissionError as exc:
        if objects:
            await _remove_uploaded_inputs(objects)
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        if objects:
            await _remove_uploaded_inputs(objects)
        raise HTTPException(402, str(exc)) from exc
    except RuntimeError as exc:
        if objects:
            await _remove_uploaded_inputs(objects)
        raise HTTPException(429, str(exc)) from exc
    except Exception:
        if objects:
            await _remove_uploaded_inputs(objects)
        raise

    row = _get_task(merchant["id"], task_id)
    return _task_view(row, with_download=False)


@router.get("/v1/videos")
def list_videos(
    merchant: Annotated[dict, Depends(api_merchant)],
    status: str | None = None,
    limit: int = 100,
) -> dict:
    return _list_for(merchant["id"], status, limit)


@router.get("/v1/videos/{task_id}")
def get_video(task_id: str, merchant: Annotated[dict, Depends(api_merchant)]) -> dict:
    row = _get_task(merchant["id"], task_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    return _task_view(row, include_prompts=True)


@router.get("/v1/videos/{task_id}/artifacts/{filename}", include_in_schema=False)
def download_artifact(
    task_id: str,
    filename: str,
    expires: int,
    signature: str,
) -> FileResponse:
    with connect() as db:
        row = db.execute(
            "SELECT artifacts,output_expires_at FROM merchant_tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    if not row or not row["output_expires_at"] or row["output_expires_at"] <= time.time():
        raise HTTPException(404, "视频不存在或已过期")
    for item in json.loads(row["artifacts"] or "[]"):
        if item.get("storage") != "local" or item.get("name") != filename:
            continue
        relative = item.get("path", "")
        if not verify_local_url(task_id, relative, expires, signature):
            raise HTTPException(403, "下载链接无效或已过期")
        try:
            path = resolve_local_output(relative)
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "视频文件不存在") from None
        return FileResponse(
            path,
            media_type="image/png" if item.get("media") == "image" else "video/mp4",
            filename=filename,
            headers={"Cache-Control": "private, no-store"},
        )
    raise HTTPException(404, "视频文件不存在")


@router.post("/v1/videos/{task_id}/cancel")
async def cancel_video(task_id: str, merchant: Annotated[dict, Depends(api_merchant)]) -> dict:
    row = _get_task(merchant["id"], task_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    if row["kind"] == "image_edit":
        raise HTTPException(409, "图片编辑暂不支持取消，请等待处理结果")
    if row["status"] not in {"queued", "dispatching", "running"}:
        raise HTTPException(409, "任务已结束，不能取消")
    started = bool(row["provider_started"])
    if row["remote_id"] == "reenact":
        from reenact import runner as reenact_runner
        await reenact_runner.cancel(task_id)
    elif row["remote_id"] == "commerce_remix":
        from commerce_remix import cancel
        cancel(task_id)
    elif row["remote_id"]:
        try:
            provider = await gongji.job_status(row["remote_id"])
            started = started or provider["status"] in {"running", "completed"} or bool(provider.get("running_seconds"))
        except Exception:
            # If provider status cannot be verified, a dispatched task is charged to prevent cancel abuse.
            started = True
        try:
            await gongji.stop_job(row["remote_id"])
        except Exception:
            pass
    with transaction() as db:
        row = db.execute(
            "SELECT * FROM merchant_tasks WHERE id=? AND merchant_id=?", (task_id, merchant["id"])
        ).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        if row["status"] not in {"queued", "dispatching", "running"}:
            raise HTTPException(409, "任务已结束，不能取消")
        input_expires = time.time() + INPUT_RETENTION
        db.execute(
            "UPDATE merchant_tasks SET status='cancelled',message='用户已取消',finished_at=?,input_expires_at=? WHERE id=?",
            (time.time(), input_expires, task_id),
        )
        db.execute(
            "UPDATE assets SET expires_at=? WHERE task_id=? AND role='input' AND expires_at IS NULL",
            (input_expires, task_id),
        )
        settle_task(db, task_id, "cancel_after_start" if started else "cancel_before_start", note="用户主动取消")
    return _task_view(_get_task(merchant["id"], task_id), with_download=False)


@router.get("/api/account/tasks")
def portal_tasks(
    account: Annotated[dict, Depends(portal_merchant)],
    status: str | None = None,
    limit: int = 100,
    tool: str | None = None,
) -> dict:
    allowed = {"video", "remix", "commerce_remix", "reenact", "image_edit", "digital_human"}
    if tool is not None and (not tool.strip() or any(item.strip() not in allowed for item in tool.split(","))):
        raise HTTPException(422, "任务类型不正确")
    return _list_for(account["id"], status, limit, hide_from_portal=True, tool=tool)


@router.get("/api/account/submissions/{idempotency_key}", include_in_schema=False)
def portal_submission(
    idempotency_key: str,
    account: Annotated[dict, Depends(portal_merchant)],
) -> dict:
    if not 1 <= len(idempotency_key) <= 180:
        raise HTTPException(404, "任务不存在")
    with connect() as db:
        row = db.execute(
            """SELECT id FROM merchant_tasks
            WHERE merchant_id=? AND idempotency_key=? AND portal_hidden_at IS NULL""",
            (account["id"], idempotency_key),
        ).fetchone()
    task = _get_portal_task(account["id"], row["id"]) if row else None
    if not task:
        raise HTTPException(404, "任务不存在")
    return _task_view(task, with_download=False)


@router.get("/api/account/tasks/{task_id}")
def portal_task(task_id: str, account: Annotated[dict, Depends(portal_merchant)]) -> dict:
    row = _get_portal_task(account["id"], task_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    return _task_view(row, include_prompts=True)


@router.get("/api/account/tasks/{task_id}/preview/{mode}")
def portal_task_preview(
    task_id: str, mode: str, account: Annotated[dict, Depends(portal_merchant)],
) -> FileResponse:
    from media_preview import create_preview

    if mode not in {"poster", "media"}:
        raise HTTPException(404, "预览不存在")
    row = _get_portal_task(account["id"], task_id)
    if not row or row["status"] != "completed" or (row["output_expires_at"] and row["output_expires_at"] <= time.time()):
        raise HTTPException(404, "作品不存在或已过期")
    artifact = next((item for item in json.loads(row["artifacts"] or "[]") if item.get("media") in {"video", "image"}), None)
    if not artifact:
        raise HTTPException(404, "作品文件不存在")
    try:
        path, content_type = create_preview(account["id"], task_id, artifact, mode)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "作品文件不存在") from None
    except Exception:
        log.exception("Task preview generation failed for %s", task_id)
        raise HTTPException(503, "预览暂不可用，请稍后重试") from None
    return FileResponse(path, media_type=content_type, content_disposition_type="inline", headers={"Cache-Control": "private, max-age=3600"})


@router.patch("/api/account/tasks/{task_id}/note")
def update_portal_task_note(
    task_id: str,
    body: PortalTaskNote,
    account: Annotated[dict, Depends(portal_merchant)],
) -> dict:
    note = body.note.strip()
    with transaction() as db:
        row = db.execute(
            """SELECT portal_hidden_at FROM merchant_tasks
            WHERE id=? AND merchant_id=?""",
            (task_id, account["id"]),
        ).fetchone()
        if not row or row["portal_hidden_at"] is not None:
            raise HTTPException(404, "任务不存在")
        db.execute(
            "UPDATE merchant_tasks SET portal_note=? WHERE id=? AND merchant_id=?",
            (note, task_id, account["id"]),
        )
    return _task_view(_get_portal_task(account["id"], task_id), with_download=False)


@router.delete("/api/account/tasks/{task_id}", status_code=204)
def hide_portal_task(
    task_id: str, account: Annotated[dict, Depends(portal_merchant)]
) -> Response:
    with transaction() as db:
        row = db.execute(
            """SELECT status,portal_hidden_at FROM merchant_tasks
            WHERE id=? AND merchant_id=?""",
            (task_id, account["id"]),
        ).fetchone()
        if not row or row["portal_hidden_at"] is not None:
            raise HTTPException(404, "任务不存在")
        if row["status"] in {"queued", "dispatching", "running", "music_queued", "music_dispatching", "music_running", "awaiting_lyrics"}:
            raise HTTPException(409, "任务正在生成，请先取消后再删除")
        db.execute(
            "UPDATE merchant_tasks SET portal_hidden_at=? WHERE id=? AND merchant_id=?",
            (time.time(), task_id, account["id"]),
        )
    return Response(status_code=204)


@router.get("/api/account/tasks/{task_id}/events")
async def portal_task_events(
    task_id: str,
    request: Request,
    account: Annotated[dict, Depends(portal_merchant)],
) -> StreamingResponse:
    """Stream one task to the signed-in browser with polling as a fallback only."""
    if not _get_portal_task(account["id"], task_id):
        raise HTTPException(404, "任务不存在")

    async def events():
        previous = ""
        while not await request.is_disconnected():
            row = _get_portal_task(account["id"], task_id)
            if not row:
                yield "event: error\ndata: {\"detail\":\"任务不存在\"}\n\n"
                return
            task = _task_view(row)
            encoded = json.dumps(task, ensure_ascii=False, separators=(",", ":"))
            if encoded != previous:
                yield f"event: task\ndata: {encoded}\n\n"
                previous = encoded
            if task["status"] in {"completed", "failed", "cancelled"}:
                return
            await asyncio.sleep(1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _list_for(
    merchant_id: str,
    status: str | None,
    limit: int,
    *,
    hide_from_portal: bool = False,
    tool: str | None = None,
) -> dict:
    limit = min(max(limit, 1), 200)
    params: list = [merchant_id]
    where = "WHERE t.merchant_id=?"
    if hide_from_portal:
        where += """ AND t.portal_hidden_at IS NULL
        AND (t.output_expires_at IS NULL OR t.output_expires_at>?)"""
        params.append(time.time())
    if status:
        where += " AND t.status=?"
        params.append(status)
    if tool is not None:
        tools = [item.strip() for item in tool.split(",") if item.strip()][:5] or [tool]
        where += (" AND COALESCE(json_extract(t.payload,'$._tool'),'video') IN ("
                  + ",".join("?" * len(tools)) + ")")
        params.extend(tools)
    params.append(limit)
    with connect() as db:
        rows = db.execute(
            f"""SELECT t.*,COALESCE(SUM(l.charged_milli),0) charged_milli
            FROM merchant_tasks t LEFT JOIN point_ledger l ON l.task_id=t.id
            {where} GROUP BY t.id ORDER BY t.created_at DESC LIMIT ?""",
            params,
        ).fetchall()
    return {"items": [_task_view(row, with_download=False) for row in rows if _retained_output_exists(row)]}
