"""Isolated IndexTTS-2 task API; unavailable until the worker is configured."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from starlette.responses import FileResponse

import gongji
import local_storage
from database import connect, public_id, reserve_task, transaction
from security import video_merchant
from video_api import _remove_uploaded_inputs, _task_view, _upload
from voice_clone_worker.workflow import validate_request


router = APIRouter(tags=["voice-clone"])
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
MAX_AUDIO_BYTES = 50 * 1024 * 1024


def _price_milli() -> int:
    return max(0, int(os.getenv("H3_VOICE_CLONE_PRICE_MILLI", "0")))


def _available() -> bool:
    return (
        os.getenv("H3_VOICE_CLONE_ENABLED", "0").strip() == "1"
        and gongji.voice_clone_configured()
        and _price_milli() > 0
    )


def _get_task(merchant_id: str, task_id: str):
    with connect() as db:
        return db.execute(
            "SELECT * FROM merchant_tasks WHERE id=? AND merchant_id=? AND kind='voice_clone' AND portal_hidden_at IS NULL",
            (task_id, merchant_id),
        ).fetchone()


def _view(row, *, with_download: bool = True) -> dict:
    value = _task_view(row, with_download=with_download)
    for item in value["artifacts"]:
        if item.get("media") == "audio" and item.get("name", "").endswith(".mp3"):
            item["preview_url"] = f"/api/account/voice-clones/{row['id']}/audio"
    return value


def _file_hash(upload: UploadFile) -> str:
    upload.file.seek(0)
    digest = hashlib.sha256()
    total = 0
    while chunk := upload.file.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_AUDIO_BYTES:
            raise HTTPException(413, "每段参考音频不能超过 50 MB")
        digest.update(chunk)
    upload.file.seek(0)
    if total < 2048:
        raise HTTPException(422, "参考音频太短或为空")
    return digest.hexdigest()


def _check_audio(upload: UploadFile, name: str) -> None:
    if Path(upload.filename or "").suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(422, f"{name}仅支持 MP3、WAV、M4A、FLAC、AAC 或 OGG")


@router.get("/api/account/voice-clones/status")
@router.get("/v1/voice-clones/status")
def voice_clone_status(merchant: Annotated[dict, Depends(video_merchant)]) -> dict:
    return {"available": _available(), "price": _price_milli() / 1000,
            "input_limit_bytes": MAX_AUDIO_BYTES}


@router.post("/api/account/voice-clones", status_code=202)
@router.post("/v1/voice-clones", status_code=202)
async def create_voice_clone(
    merchant: Annotated[dict, Depends(video_merchant)],
    voice: UploadFile = File(...),
    text: str = Form(...),
    emotion: UploadFile | None = File(None),
    emotion_weight: float = Form(1.0),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    if not _available():
        raise HTTPException(503, "声音克隆尚未开放")
    if merchant["status"] != "active" or merchant.get("api_key_status", "active") != "active":
        raise HTTPException(403, "账户未启用或已暂停")
    try:
        clean_text = validate_request(text, emotion_weight)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _check_audio(voice, "音色参考")
    if emotion and emotion.filename:
        _check_audio(emotion, "情绪参考")
    else:
        emotion = None
    identity = {
        "text": clean_text, "emotion_weight": emotion_weight,
        "voice_hash": _file_hash(voice),
        "emotion_hash": _file_hash(emotion) if emotion else None,
    }
    request_hash = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if idempotency_key and len(idempotency_key) > 120:
        raise HTTPException(422, "Idempotency-Key 不能超过 120 个字符")
    idem = idempotency_key or None
    if idem:
        with connect() as db:
            existing = db.execute(
                "SELECT * FROM merchant_tasks WHERE merchant_id=? AND idempotency_key=?",
                (merchant["id"], idem),
            ).fetchone()
        if existing:
            if existing["kind"] != "voice_clone" or existing["request_hash"] != request_hash:
                raise HTTPException(409, "同一个 Idempotency-Key 不能用于不同请求")
            return _view(existing, with_download=False)
    task_id = public_id("voc")
    objects = []
    try:
        objects.append(await _upload(merchant["id"], task_id, 0, "voice", voice, force_local=True,
                                     shrink_videos=False))
        if emotion:
            objects.append(await _upload(merchant["id"], task_id, 1, "emotion", emotion,
                                         force_local=True, shrink_videos=False))
        payload = {
            "_tool": "voice_clone", "prompt": clean_text, "text": clean_text,
            "emotion_weight": emotion_weight, "_output_names": ["speech.mp3"],
            "_transfer_backend": "local",
        }
        now = time.time()
        price = _price_milli()
        with transaction() as db:
            db.execute(
                """INSERT INTO merchant_tasks
                (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,
                 payload,input_objects,price_milli,failure_price_milli,idempotency_key,
                 request_hash,created_at,queued_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, merchant["id"], "voice_clone", "audio", 0, "audio", "声音克隆",
                 "queued", 0, "任务已进入队列", json.dumps(payload, ensure_ascii=False),
                 json.dumps(objects, ensure_ascii=False), price, 0, idem, request_hash, now, now),
            )
            for item in objects:
                db.execute(
                    """INSERT INTO assets
                    (id,merchant_id,task_id,role,bucket,object_key,filename,content_type,
                     size_bytes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (item["id"], merchant["id"], task_id, "input", item["bucket"],
                     item["key"], item["filename"], item["content_type"],
                     item.get("size_bytes"), now),
                )
            reserve_task(db, merchant["id"], task_id, "720p", amount_milli=price)
    except (ValueError, PermissionError) as exc:
        await _remove_uploaded_inputs(objects)
        raise HTTPException(400 if isinstance(exc, ValueError) else 403, str(exc)) from exc
    except sqlite3.IntegrityError:
        await _remove_uploaded_inputs(objects)
        raise HTTPException(409, "任务已存在") from None
    except Exception:
        await _remove_uploaded_inputs(objects)
        raise
    return _view(_get_task(merchant["id"], task_id), with_download=False)


@router.get("/api/account/voice-clones")
@router.get("/v1/voice-clones")
def list_voice_clones(merchant: Annotated[dict, Depends(video_merchant)], limit: int = 50) -> dict:
    with connect() as db:
        rows = db.execute(
            """SELECT * FROM merchant_tasks WHERE merchant_id=? AND kind='voice_clone'
            AND portal_hidden_at IS NULL ORDER BY created_at DESC LIMIT ?""",
            (merchant["id"], min(max(limit, 1), 100)),
        ).fetchall()
    return {"data": [_view(row) for row in rows]}


@router.get("/api/account/voice-clones/{task_id}")
@router.get("/v1/voice-clones/{task_id}")
def get_voice_clone(task_id: str, merchant: Annotated[dict, Depends(video_merchant)]) -> dict:
    row = _get_task(merchant["id"], task_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    return _view(row)


@router.get("/api/account/voice-clones/{task_id}/audio")
@router.get("/v1/voice-clones/{task_id}/audio")
def get_voice_audio(task_id: str, merchant: Annotated[dict, Depends(video_merchant)]):
    row = _get_task(merchant["id"], task_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    if row["status"] != "completed":
        raise HTTPException(409, "声音尚未生成完成")
    if row["output_expires_at"] and row["output_expires_at"] <= time.time():
        raise HTTPException(410, "声音文件已过期")
    artifacts = json.loads(row["artifacts"] or "[]")
    mp3 = next((item for item in artifacts if item.get("storage") == "local"
                and str(item.get("name") or "").lower().endswith(".mp3")), None)
    if not mp3:
        raise HTTPException(404, "音频文件暂不可用")
    try:
        path = local_storage.resolve(mp3["path"])
    except (KeyError, FileNotFoundError, ValueError):
        raise HTTPException(404, "音频文件暂不可用") from None
    return FileResponse(path, media_type="audio/mpeg", filename="speech.mp3",
                        content_disposition_type="inline",
                        headers={"Cache-Control": "private, no-store"})
