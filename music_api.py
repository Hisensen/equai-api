"""Music cover API using the existing merchant, billing and task storage."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from starlette.responses import FileResponse

from database import connect, public_id, reserve_task, transaction
from security import portal_merchant
from video_api import (
    _remove_uploaded_inputs, _task_view, _upload,
    direct_assets_as_uploads, _uploads_named,
)
import local_storage

router = APIRouter(tags=["music-generation"])
MUSIC_ENABLED = os.getenv("H3_MUSIC_ENABLED", "0").strip() == "1"
MUSIC_PRICE_MILLI = int(os.getenv("H3_MUSIC_PRICE_MILLI", "500"))
MUSIC_FAILURE_PRICE_MILLI = 0
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".mp4", ".m4a", ".flac"}
MAX_BYTES = int(os.getenv("H3_MUSIC_MAX_INPUT_BYTES", str(512 * 1024 * 1024)))


class LyricsUpdate(BaseModel):
    lyrics: str = Field(default="", max_length=20000)


def _get_task(merchant_id: str, task_id: str):
    with connect() as db:
        return db.execute("SELECT * FROM merchant_tasks WHERE id=? AND merchant_id=? AND portal_hidden_at IS NULL", (task_id, merchant_id)).fetchone()


def _music_available(_merchant_id: str = "") -> bool:
    return MUSIC_ENABLED and bool(os.getenv("GONGJI_MUSIC_IMAGE", "").strip())


def _require_music(_merchant_id: str = "") -> None:
    if not MUSIC_ENABLED:
        raise HTTPException(503, "音乐功能尚未开放")
    if not os.getenv("GONGJI_MUSIC_IMAGE", "").strip():
        raise HTTPException(503, "音乐 Worker 尚未就绪")


def _music_task_view(row, *, with_download: bool = True) -> dict:
    view = _task_view(row, with_download=with_download)
    payload = json.loads(row["payload"] or "{}")
    view["lyrics"] = str(payload.get("lyrics") or "")
    view["can_edit_lyrics"] = row["status"] == "awaiting_lyrics"
    # Music jobs publish one deliverable: MP3. Hide legacy FLAC/extra-slot
    # artifacts from both the account API and the portal task card.
    view["artifacts"] = [
        artifact for artifact in view["artifacts"]
        if str(artifact.get("name") or "").lower().endswith(".mp3")
    ]
    for artifact in view["artifacts"]:
        if str(artifact.get("name") or "").lower().endswith(".mp3"):
            artifact["preview_url"] = f"/api/account/music/tasks/{row['id']}/preview"
            if with_download:
                artifact["download_url"] = f"/api/account/music/tasks/{row['id']}/download"
    return view


@router.get("/api/account/music/status")
def music_status(merchant: Annotated[dict, Depends(portal_merchant)]) -> dict:
    return {"available": _music_available(str(merchant["id"])), "price": MUSIC_PRICE_MILLI / 1000}


@router.post("/api/account/music", status_code=202)
async def create_music(
    merchant: Annotated[dict, Depends(portal_merchant)],
    source: UploadFile | None = File(None),
    direct_assets: str = Form(""),
    style: str = Form("摇滚"),
    voice: str = Form("不指定"),
    prompt: str = Form(""),
    lyrics: str = Form(""),
    title: str = Form("音乐翻唱"),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    _require_music(str(merchant["id"]))
    if direct_assets:
        if source is not None and source.filename:
            raise HTTPException(422, "音乐素材不能同时使用直传和 multipart 上传")
        _staged, staged = await direct_assets_as_uploads(
            merchant["id"], "reference", direct_assets,
        )
        audio_files = _uploads_named(staged, "audios")
        if len(audio_files) != 1:
            raise HTTPException(422, "音乐编辑需要一段音频")
        source = audio_files[0]
    if source is None or not source.filename:
        raise HTTPException(422, "请选择音频文件")
    safe = Path(source.filename or "source.mp3").name[:180]
    if Path(safe).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(422, "请上传 MP3、WAV 或 MP4 音频文件")
    if source.size and source.size > MAX_BYTES:
        raise HTTPException(413, "音频文件不能超过 512 MB")
    if merchant["status"] != "active":
        raise HTTPException(403, "账户未启用或已暂停")
    task_id = public_id("mus")
    identity = json.dumps({"name": safe, "size": source.size, "style": style, "voice": voice, "prompt": prompt, "lyrics": lyrics}, ensure_ascii=False, sort_keys=True)
    request_hash = hashlib.sha256(identity.encode()).hexdigest()
    instrumental = voice == "无人声" or style == "纯器乐"
    music_stage = "lyrics_review" if not lyrics.strip() and not instrumental else "generate"
    if idempotency_key:
        with connect() as db:
            existing = db.execute("SELECT * FROM merchant_tasks WHERE merchant_id=? AND idempotency_key=?", (merchant["id"], idempotency_key[:120])).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise HTTPException(409, "同一个 Idempotency-Key 不能用于不同请求")
            return _music_task_view(existing, with_download=False)
    objects = []
    try:
        objects.append(await _upload(merchant["id"], task_id, 0, "audios", source, force_local=True, shrink_videos=False))
        payload = {
            "_tool": "music", "style": style[:100], "voice": voice[:40], "prompt": prompt[:2000],
            "lyrics": lyrics[:20000], "source_filename": safe,
            # Keep slot 2 named for older prewarmed workers, which still upload
            # their local FLAC intermediate. The account API exposes MP3 only.
            "_output_names": ["song.mp3", "song.flac"], "music_stage": music_stage,
            "_transfer_backend": "local",
        }
        now = time.time()
        with transaction() as db:
            db.execute(
                """INSERT INTO merchant_tasks
                (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,payload,portal_note,input_objects,price_milli,failure_price_milli,idempotency_key,request_hash,created_at,queued_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, merchant["id"], "music", "music", 0, "audio", title[:100], "music_queued", 5, "任务已进入队列", json.dumps(payload, ensure_ascii=False), "", json.dumps(objects, ensure_ascii=False), MUSIC_PRICE_MILLI, MUSIC_FAILURE_PRICE_MILLI, idempotency_key[:120] if idempotency_key else None, request_hash, now, now),
            )
            for item in objects:
                db.execute("INSERT INTO assets (id,merchant_id,task_id,role,bucket,object_key,filename,content_type,size_bytes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (item["id"], merchant["id"], task_id, "input", item["bucket"], item["key"], item["filename"], item["content_type"], item.get("size_bytes"), now))
            reserve_task(db, merchant["id"], task_id, "music", amount_milli=MUSIC_PRICE_MILLI)
    except sqlite3.IntegrityError:
        await _remove_uploaded_inputs(objects)
        raise HTTPException(409, "任务已存在") from None
    except Exception:
        await _remove_uploaded_inputs(objects)
        raise
    return _music_task_view(_get_task(merchant["id"], task_id), with_download=False)


@router.get("/api/account/music/tasks")
def list_music(merchant: Annotated[dict, Depends(portal_merchant)], limit: int = 50) -> dict:
    _require_music(str(merchant["id"]))
    with connect() as db:
        rows = db.execute("SELECT * FROM merchant_tasks WHERE merchant_id=? AND kind='music' AND portal_hidden_at IS NULL ORDER BY created_at DESC LIMIT ?", (merchant["id"], min(max(limit, 1), 100))).fetchall()
    now = time.time()
    visible = []
    for row in rows:
        if row["status"] == "completed":
            if row["output_expires_at"] and row["output_expires_at"] <= now:
                continue
            artifacts = json.loads(row["artifacts"] or "[]")
            mp3 = next((item for item in artifacts if str(item.get("name") or "").lower().endswith(".mp3")), None)
            if not mp3:
                continue
            if mp3.get("storage") == "local":
                try:
                    local_storage.resolve(str(mp3["path"]))
                except (KeyError, FileNotFoundError, ValueError):
                    continue
        visible.append(_music_task_view(row, with_download=True))
    return {"items": visible}


@router.get("/api/account/music/tasks/{task_id}")
def music_detail(task_id: str, merchant: Annotated[dict, Depends(portal_merchant)]) -> dict:
    _require_music(str(merchant["id"]))
    row = _get_task(merchant["id"], task_id)
    if not row or row["kind"] != "music":
        raise HTTPException(404, "任务不存在")
    return _music_task_view(row)


def _music_mp3_path(task_id: str, merchant_id: str) -> Path:
    _require_music(merchant_id)
    row = _get_task(merchant_id, task_id)
    if not row or row["kind"] != "music":
        raise HTTPException(404, "任务不存在")
    if row["status"] != "completed":
        raise HTTPException(409, "音乐尚未生成完成")
    if row["output_expires_at"] and row["output_expires_at"] <= time.time():
        raise HTTPException(410, "音乐文件已过期")
    artifacts = json.loads(row["artifacts"] or "[]")
    mp3 = next((item for item in artifacts if str(item.get("name") or "").lower().endswith(".mp3") and item.get("storage") == "local"), None)
    if not mp3:
        raise HTTPException(404, "MP3 文件暂不可用")
    try:
        return local_storage.resolve(mp3["path"])
    except (KeyError, FileNotFoundError, ValueError):
        raise HTTPException(404, "MP3 文件暂不可用") from None


@router.get("/api/account/music/tasks/{task_id}/preview")
def music_preview(task_id: str, merchant: Annotated[dict, Depends(portal_merchant)]):
    path = _music_mp3_path(task_id, str(merchant["id"]))
    return FileResponse(path, media_type="audio/mpeg", filename=path.name, content_disposition_type="inline", headers={"Cache-Control": "private, max-age=300"})


@router.get("/api/account/music/tasks/{task_id}/download")
def music_download(task_id: str, merchant: Annotated[dict, Depends(portal_merchant)]):
    path = _music_mp3_path(task_id, str(merchant["id"]))
    return FileResponse(path, media_type="audio/mpeg", filename=path.name, content_disposition_type="attachment", headers={"Cache-Control": "private, no-store"})


@router.patch("/api/account/music/tasks/{task_id}/lyrics")
def update_lyrics(task_id: str, body: LyricsUpdate, merchant: Annotated[dict, Depends(portal_merchant)]) -> dict:
    _require_internal_music(str(merchant["id"]))
    with transaction() as db:
        row = db.execute("SELECT * FROM merchant_tasks WHERE id=? AND merchant_id=? AND kind='music'", (task_id, merchant["id"])).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        if row["status"] != "awaiting_lyrics":
            raise HTTPException(409, "任务已经开始生成，不能修改歌词")
        payload = json.loads(row["payload"] or "{}")
        payload["lyrics"] = body.lyrics
        db.execute("UPDATE merchant_tasks SET payload=?,message='歌词已更新' WHERE id=?", (json.dumps(payload, ensure_ascii=False), task_id))
        row = db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
    return _music_task_view(row, with_download=False)


@router.post("/api/account/music/tasks/{task_id}/generate")
def confirm_lyrics(task_id: str, merchant: Annotated[dict, Depends(portal_merchant)]) -> dict:
    _require_internal_music(str(merchant["id"]))
    if merchant["status"] != "active":
        raise HTTPException(403, "账户未启用或已暂停")
    with transaction() as db:
        row = db.execute(
            "SELECT * FROM merchant_tasks WHERE id=? AND merchant_id=? AND kind='music'",
            (task_id, merchant["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        if row["status"] != "awaiting_lyrics":
            raise HTTPException(409, "任务当前不能确认歌词")
        payload = json.loads(row["payload"] or "{}")
        if not str(payload.get("lyrics") or "").strip():
            raise HTTPException(422, "请先填写或确认歌词")
        payload["music_stage"] = "generate"
        now = time.time()
        db.execute(
            "UPDATE merchant_tasks SET status='music_queued',progress=5,message='歌词已确认，任务进入生成队列',payload=?,queued_at=? WHERE id=? AND status='awaiting_lyrics'",
            (json.dumps(payload, ensure_ascii=False), now, task_id),
        )
        row = db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
    return _music_task_view(row, with_download=False)
