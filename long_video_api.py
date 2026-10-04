"""API-only long-form MiniMax H3 jobs.

Long videos are rendered as latent-linked clips of roughly fifteen seconds.
The worker removes the 22-frame overlap from every continuation clip and
returns one concatenated MP4, so callers see a normal asynchronous video task.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from math import ceil
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from database import connect, points, public_id, reserve_task, transaction
from security import api_merchant
from transfer_storage import backend as transfer_backend
from video_api import _get_task, _task_view


router = APIRouter(prefix="/v1/long-videos", tags=["long-video-generation"])

FPS = 24
CONTEXT_FRAMES = 22
FIRST_SEGMENT_FRAMES = 362
CONTINUATION_CONTRIBUTION_FRAMES = 357
MIN_DURATION = 16
DIGITAL_HUMAN_MIN_DURATION = 1
MAX_DURATION = int(os.getenv("H3_LONG_MAX_DURATION_SECONDS", "60"))
PRICE_PER_SEGMENT_MILLI = int(os.getenv("H3_LONG_PRICE_PER_SEGMENT_MILLI", "1000"))
FAILURE_PRICE_PER_SEGMENT_MILLI = 0


class LongVideoRequest(BaseModel):
    duration: int = Field(ge=MIN_DURATION)
    ratio: str = "16:9"
    segment_prompts: list[str] = Field(min_length=2, max_length=32)
    title: str = Field(default="EQUAI long video", min_length=1, max_length=100)
    seed: int = 0


def segment_count(duration: float) -> int:
    """Avoid a zero-work H3 continuation for a sub-minimum final tail."""
    target_frames = ceil(duration * FPS)
    # A continuation needs at least 17 newly generated frames in addition to
    # the 22-frame context. Let a shorter tail fit in the first H3 window.
    if target_frames <= 362 + 17:
        return 1
    return max(1, ceil(duration / 15))


def digital_human_render_duration(duration: float) -> float:
    """Drop a sub-second tail after each complete 15-second avatar window."""
    complete_windows = int(duration // 15)
    if complete_windows and 0 < duration - complete_windows * 15 < 1:
        return float(complete_windows * 15)
    return duration


def digital_human_segment_count(duration: float) -> int:
    return max(1, ceil(digital_human_render_duration(duration) / 15))


def validate_long_request(body: LongVideoRequest) -> int:
    if body.duration > MAX_DURATION:
        raise HTTPException(
            422, f"duration 当前支持 {MIN_DURATION}–{MAX_DURATION} 秒"
        )
    if body.ratio not in {"16:9", "9:16"}:
        raise HTTPException(422, "ratio 只能是 16:9 或 9:16")
    required = segment_count(body.duration)
    if len(body.segment_prompts) != required:
        raise HTTPException(
            422,
            f"{body.duration} 秒需要 {required} 段 segment_prompts，"
            f"当前收到 {len(body.segment_prompts)} 段",
        )
    total = 0
    for index, prompt in enumerate(body.segment_prompts, 1):
        length = len(prompt.strip())
        if not 1 <= length <= 30000:
            raise HTTPException(422, f"第 {index} 段提示词长度需要 1–30000 字符")
        total += length
    if total > 120000:
        raise HTTPException(422, "全部分段提示词合计不能超过 120000 字符")
    return required


@router.get("/capabilities")
def long_video_capabilities(
    _: Annotated[dict, Depends(api_merchant)],
) -> dict:
    return {
        "quality": "720p",
        "duration": {"min": MIN_DURATION, "max": MAX_DURATION},
        "ratios": ["16:9", "9:16"],
        "fps": FPS,
        "segment_seconds": 15,
        "motion_context_frames": CONTEXT_FRAMES,
        "pricing": {
            "success_per_segment": points(PRICE_PER_SEGMENT_MILLI),
            "runtime_failure_per_segment": points(FAILURE_PRICE_PER_SEGMENT_MILLI),
        },
    }


@router.post("/720p", status_code=202)
def create_long_video(
    body: LongVideoRequest,
    merchant: Annotated[dict, Depends(api_merchant)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    segments = validate_long_request(body)
    prompts = [item.strip() for item in body.segment_prompts]
    price_milli = PRICE_PER_SEGMENT_MILLI * segments
    failure_price_milli = FAILURE_PRICE_PER_SEGMENT_MILLI * segments
    request_identity = {
        "mode": "long_text",
        "duration": body.duration,
        "ratio": body.ratio,
        "segment_prompts": prompts,
        "seed": body.seed,
        "title": body.title,
    }
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
    if merchant["balance_milli"] < price_milli:
        raise HTTPException(402, f"该长视频任务需要 {points(price_milli)} 积分")

    task_id = public_id("vid")
    now = time.time()
    payload = {
        "mode": "long_text",
        "tier": "720p",
        "prompt": prompts[0],
        "segment_prompts": prompts,
        "duration": body.duration,
        "ratio": body.ratio,
        "quality": 0.9,
        "seed": body.seed,
        "_prompt_compiled": True,
        "_original_prompt": "\n\n--- SEGMENT ---\n\n".join(prompts),
        "_transfer_backend": transfer_backend(),
    }
    try:
        with transaction() as db:
            db.execute(
                """INSERT INTO merchant_tasks
                (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,
                 payload,input_objects,price_milli,failure_price_milli,idempotency_key,
                 request_hash,created_at,queued_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, merchant["id"], "long_text", "720p", body.duration,
                    body.ratio, body.title, "queued", 5, "长视频任务已进入队列",
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    "[]", price_milli, failure_price_milli, idempotency_key,
                    request_hash, now, now,
                ),
            )
            reserve_task(
                db, merchant["id"], task_id, "720p", amount_milli=price_milli
            )
    except sqlite3.IntegrityError:
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
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(402, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(429, str(exc)) from exc

    return _task_view(_get_task(merchant["id"], task_id), with_download=False)
