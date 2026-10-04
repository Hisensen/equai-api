"""Commerce tools using the existing merchant queue, accounting and private assets."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from io import BytesIO
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit, urljoin, quote

import httpx
import video_projects
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from PIL import Image, ImageOps

# iPhone HEIC/HEIF files are decoded through Pillow's optional plugin.  Keep
# startup compatible with existing images while the dependency is rolled out.
try:
    from pillow_heif import register_heif_opener
    register_heif_opener(thumbnails=False)
except ImportError:
    register_heif_opener = None

from database import (
    DATA_DIR,
    PRICE_MILLI,
    connect,
    public_id,
    reserve_task,
    settle_task,
    transaction,
)
from security import video_merchant
import local_storage
import object_storage
import transfer_storage

router = APIRouter(tags=["commerce"])
ROLE_NAMES = {"person": "person", "product": "product", "scene": "environment"}
REMIX_TEMPLATES = {
    "person": {"person"},
    "product": {"product"},
    "scene": {"scene"},
    "person_product": {"person", "product"},
    "all": {"person", "product", "scene"},
}
REMIX_TEMPLATE_LABELS = {
    "person": "只换人物", "product": "只换商品", "scene": "只换场景",
    "person_product": "人物＋商品", "all": "人物＋商品＋场景",
}
REMIX_EXTRA_TEMPLATES = {
    "person_scene": {"person", "scene"},
    "product_scene": {"product", "scene"},
}
REMIX_TEMPLATE_LABELS.update({"person_scene": "人物＋场景", "product_scene": "商品＋场景"})
MAX_IMAGE_BYTES = 30 * 1024**2
MAX_VIDEO_BYTES = 300 * 1024**2
REMIX_SURCHARGE_MILLI = int(os.getenv("H3_REMIX_SURCHARGE_MILLI", "500"))
VIDEO_EDITOR_15S_PRICE_MILLI = int(os.getenv("H3_VIDEO_EDITOR_15S_PRICE_MILLI", "2000"))
log = logging.getLogger("h3.commerce")


def remix_price_milli(quality: str) -> int:
    return PRICE_MILLI[quality] + REMIX_SURCHARGE_MILLI


def commerce_remix_price_milli() -> int:
    """Fixed public price for one depth-guided e-commerce remake."""
    return 1500


def video_editor_price_milli(source_seconds: float) -> int:
    """Charge 2 points per started 15-second source-video interval."""
    if not math.isfinite(source_seconds) or source_seconds < 4.9:
        raise ValueError("视频编辑视频时长不能少于 5 秒")
    blocks = max(1, math.ceil((source_seconds - .1) / 15))
    return blocks * VIDEO_EDITOR_15S_PRICE_MILLI


def reenact_price_milli(source_seconds: float) -> int:
    """Reenactment regenerates every passage and its pictures: priced per started 15 s."""
    if not math.isfinite(source_seconds) or source_seconds < 4.9:
        raise ValueError("视频重演原视频不能少于 5 秒")
    blocks = max(1, math.ceil((source_seconds - .1) / 15))
    return blocks * int(os.getenv("H3_REENACT_15S_PRICE_MILLI", "4000"))


def image_settings() -> dict:
    path = DATA_DIR / "image-service.json"
    stored = json.loads(path.read_text()) if path.is_file() else {}
    return {
        "key": os.getenv("H3_IMAGE_API_KEY") or stored.get("api_key", ""),
        "base": (os.getenv("H3_IMAGE_BASE_URL") or stored.get("base_url") or "https://image.own-jarvis.com").rstrip("/"),
        "model": os.getenv("H3_IMAGE_MODEL") or stored.get("model") or "gpt-image-2",
        "price": int(os.getenv("H3_IMAGE_EDIT_PRICE_MILLI", "0")),
    }


@router.get("/api/account/creative/config")
def creative_config(merchant: Annotated[dict, Depends(video_merchant)]) -> dict:
    import gongji

    settings = image_settings()
    music_price = int(os.getenv("H3_MUSIC_PRICE_MILLI", "1000"))
    music_internal_ids = {value.strip() for value in os.getenv("H3_MUSIC_INTERNAL_MERCHANT_IDS", "").split(",") if value.strip()}
    music_enabled = os.getenv("H3_MUSIC_ENABLED", "0").strip() == "1"
    return {"image_edit": {"available": bool(settings["key"] and settings["price"] > 0),
                            "price": settings["price"] / 1000 if settings["price"] > 0 else None},
            "remix": {"available": True, "max_duration": 15,
                      "prices": {"720p": remix_price_milli("720p") / 1000}},
            "remix2": {"available": True, "max_duration": 15,
                       "prices": {"720p": remix_price_milli("720p") / 1000}},
            "video_editor": {"available": True,
                      "prices": {"per_15_seconds": VIDEO_EDITOR_15S_PRICE_MILLI / 1000},
                      "reenact_prices": {"per_15_seconds": int(os.getenv("H3_REENACT_15S_PRICE_MILLI", "4000")) / 1000}},
            "digital_human": {"available": gongji.long_configured(),
                      "min_duration": 1,
                      "max_duration": int(os.getenv("H3_LONG_MAX_DURATION_SECONDS", "60")),
                      "price_per_segment": int(os.getenv("H3_LONG_PRICE_PER_SEGMENT_MILLI", "1000")) / 1000},
            "music": {"available": music_enabled and merchant["id"] in music_internal_ids and bool(os.getenv("GONGJI_MUSIC_IMAGE", "").strip()),
                      "price": music_price / 1000}
            }


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


def _inspect_remix_path(path: Path, digest: str, *, max_duration: int | None = 15) -> dict:
    return _inspect_remix_info(probe(path), path.stat().st_size, digest, max_duration=max_duration)


def _inspect_remix_info(info: dict, size: int, digest: str, *, max_duration: int | None = 15) -> dict:
    if not 0 < size <= MAX_VIDEO_BYTES:
        raise ValueError("参考视频需要在 300 MB 以内")
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not video:
        raise ValueError("未找到视频画面")
    seconds = float(video.get("duration") or info["format"]["duration"])
    if not math.isfinite(seconds) or seconds < 4.9 or (max_duration is not None and seconds > max_duration + .1):
        raise ValueError(f"请上传至少 5 秒的参考视频" if max_duration is None else f"请上传 5–{max_duration} 秒的参考视频")
    width, height = video["width"], video["height"]
    if width <= 0 or height <= 0 or width * height > 9_000_000:
        raise ValueError("请上传 4K 以内的参考视频")
    rotation = int(float(video.get("tags", {}).get("rotate", 0)))
    for side in video.get("side_data_list", []):
        rotation = int(side.get("rotation", rotation))
    if abs(rotation) % 180 == 90:
        width, height = height, width
    ratio = "16:9" if width >= height else "9:16"
    desired = 16 / 9 if width >= height else 9 / 16
    if abs(width / height / desired - 1) > .08:
        raise ValueError("当前复刻支持 16:9 横屏或 9:16 竖屏，请先裁切视频")
    return {
        "duration": max(5, math.ceil(seconds - .001)) if max_duration is None else min(max_duration, max(5, math.ceil(seconds - .001))),
        "source_seconds": seconds,
        "source_sha256": digest,
        "ratio": ratio,
        "has_audio": any(s["codec_type"] == "audio" for s in info["streams"]),
    }


def inspect_remix(upload: UploadFile, *, max_duration: int | None = 15) -> dict:
    with tempfile.TemporaryDirectory(prefix="equai-probe-") as folder:
        path = Path(folder) / "source.mp4"
        upload.file.seek(0)
        digest = hashlib.sha256()
        total = 0
        with path.open("wb") as dst:
            while chunk := upload.file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    upload.file.seek(0)
                    raise ValueError("参考视频需要在 300 MB 以内")
                digest.update(chunk)
                dst.write(chunk)
        upload.file.seek(0)
        return _inspect_remix_path(path, digest.hexdigest(), max_duration=max_duration)


def probe_remote(key: str) -> dict:
    """ffprobe a COS object over a signed URL: only the container header is fetched, not the file."""
    url = object_storage.presigned_url(key, expires=600)
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "https,tls,tcp,http,crypto",
         "-format_whitelist", "mov,matroska,webm", "-show_streams", "-show_format", "-of", "json", url],
        capture_output=True, timeout=60, check=True,
    )
    return json.loads(result.stdout)


def inspect_remix_remote(asset: dict, *, max_duration: int | None = None) -> dict:
    """Validate a browser-staged video without downloading it through the slow HK <-> COS link."""
    digest = hashlib.sha256(f'{asset["key"]}:{asset.get("size_bytes", 0)}'.encode()).hexdigest()
    return _inspect_remix_info(probe_remote(asset["key"]), int(asset.get("size_bytes") or 0), digest,
                               max_duration=max_duration)


def inspect_remix_local(asset: dict, *, max_duration: int | None = None) -> dict:
    """Probe a browser-staged video already assembled on the local input disk."""
    path = transfer_storage.resolve_input(str(asset["key"]))
    declared_size = int(asset.get("size_bytes") or 0)
    if path.stat().st_size != declared_size:
        raise ValueError("素材上传不完整，请重新上传")
    digest = hashlib.sha256(f'{asset["key"]}:{declared_size}'.encode()).hexdigest()
    return _inspect_remix_info(probe(path), declared_size, digest, max_duration=max_duration)


def inspect_remix_asset(asset: dict, *, max_duration: int | None = 15) -> dict:
    """Download a browser-staged COS video for trusted server-side probing."""
    with tempfile.TemporaryDirectory(prefix="equai-probe-") as folder:
        path = Path(folder) / "source.mp4"
        object_storage.download_path(asset["key"], path)
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return _inspect_remix_path(path, digest.hexdigest(), max_duration=max_duration)


def _validate_remix_roles(raw_roles: str, image_count: int, template: str) -> tuple[list[str], str]:
    if not 1 <= image_count <= 3:
        raise HTTPException(422, "请上传一条参考视频，并至少上传一张人物、商品或场景参考图")
    try:
        roles = json.loads(raw_roles)
        if not isinstance(roles, list) or any(not isinstance(r, str) or r not in ROLE_NAMES for r in roles):
            raise ValueError()
        if len(roles) != image_count or len(set(roles)) != len(roles):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(422, "每张替换图片需要唯一的人物、商品或场景用途") from None
    template = template.strip().lower()
    if template in {"", "auto"}:
        role_set = set(roles)
        template = next(
            (name for name, expected in {**REMIX_TEMPLATES, **REMIX_EXTRA_TEMPLATES}.items() if role_set == expected),
            "",
        )
    if template not in {**REMIX_TEMPLATES, **REMIX_EXTRA_TEMPLATES}:
        raise HTTPException(422, "复刻模板不正确")
    expected = {**REMIX_TEMPLATES, **REMIX_EXTRA_TEMPLATES}[template]
    if expected is not None and set(roles) != expected:
        missing = expected - set(roles)
        extra = set(roles) - expected
        role_labels = {"person": "人物", "product": "商品", "scene": "场景"}
        details = []
        if missing:
            details.append("缺少" + "、".join(role_labels[item] for item in sorted(missing)) + "图片")
        if extra:
            details.append("当前模板不使用" + "、".join(role_labels[item] for item in sorted(extra)) + "图片")
        raise HTTPException(422, f"“{REMIX_TEMPLATE_LABELS[template]}”模板：" + "；".join(details))
    order = {"person": 0, "product": 1, "scene": 2}
    return sorted(roles, key=lambda role: order[role]), template


async def validate_remix(videos: list, images: list, raw_roles: str, audio: str,
                         template: str = "auto") -> dict:
    if len(videos) != 1 or not 1 <= len(images) <= 3:
        raise HTTPException(422, "请上传一条参考视频，并至少上传一张人物、商品或场景参考图")
    roles, template = _validate_remix_roles(raw_roles, len(images), template)
    if audio != "original":
        raise HTTPException(422, "视频复刻第一版固定保留原片声音")
    if videos[0].size is not None and videos[0].size > MAX_VIDEO_BYTES:
        raise HTTPException(413, "参考视频不能超过 300 MB")
    try:
        info = await asyncio.to_thread(inspect_remix, videos[0])
        hashes = []
        for upload in images:
            normalized = await asyncio.to_thread(normalize_upload, upload)
            hashes.append(hashlib.sha256(normalized).hexdigest())
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        raise HTTPException(422, str(exc) if isinstance(exc, ValueError) else "无法读取素材，请检查视频或图片格式") from None
    return {**info, "roles": roles, "audio": audio, "template": template, "image_sha256": hashes}


async def validate_commerce_remix_uploads(videos: list, images: list, raw_roles: str) -> dict:
    """Validate optional, explicitly role-bound references for the depth workflow."""
    if len(videos) != 1 or len(images) > 3:
        raise HTTPException(422, "需要一条视频，人物、商品、场景图片各可选一张")
    try:
        roles = json.loads(raw_roles or "[]")
        if (not isinstance(roles, list) or len(roles) != len(images)
                or len(set(roles)) != len(roles)
                or any(role not in ROLE_NAMES for role in roles)):
            raise ValueError()
        order = {"person": 0, "product": 1, "scene": 2}
        if roles != sorted(roles, key=order.__getitem__):
            raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(422, "参考图片需按人物、商品、场景顺序填写用途") from None
    if videos[0].size is not None and videos[0].size > MAX_VIDEO_BYTES:
        raise HTTPException(413, "参考视频不能超过 300 MB")
    try:
        info = await asyncio.to_thread(inspect_remix, videos[0], max_duration=15)
        # Container metadata for a nominal 15-second clip can run a few frames
        # long. H3 rejects generation durations above 15 even though upload
        # validation deliberately accepts that small recording tolerance.
        info["source_seconds"] = min(float(info["source_seconds"]), 15.0)
        hashes = []
        for upload in images:
            normalized = await asyncio.to_thread(normalize_upload, upload)
            hashes.append(hashlib.sha256(normalized).hexdigest())
            upload.file.close()
            upload.file = BytesIO(normalized)
            upload.size = len(normalized)
            extension = ".png" if normalized.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg"
            upload.filename = Path(upload.filename or "reference").stem + extension
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, str(exc) if isinstance(exc, ValueError) else "无法读取素材，请检查视频或图片格式") from None
    return {**info, "roles": roles, "audio": "original", "template": "commerce_depth",
            "image_sha256": hashes}


async def validate_remix_assets(objects: list[dict], raw_roles: str, audio: str,
                                template: str = "auto") -> dict:
    videos = [item for item in objects if item.get("field") == "videos"]
    images = [item for item in objects if item.get("field") == "images"]
    if len(videos) != 1:
        raise HTTPException(422, "请上传一条参考视频，并至少上传一张人物、商品或场景参考图")
    roles, template = _validate_remix_roles(raw_roles, len(images), template)
    if audio != "original":
        raise HTTPException(422, "视频复刻第一版固定保留原片声音")
    if int(videos[0].get("size_bytes") or 0) > MAX_VIDEO_BYTES:
        raise HTTPException(413, "参考视频不能超过 300 MB")
    if any(int(item.get("size_bytes") or 0) > MAX_IMAGE_BYTES for item in images):
        raise HTTPException(413, "参考图片每张不能超过 30 MB")
    try:
        inspector = inspect_remix_local if videos[0].get("storage") == "local" else inspect_remix_remote
        info = await asyncio.to_thread(inspector, videos[0], max_duration=15)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        raise HTTPException(422, str(exc) if isinstance(exc, ValueError) else "无法读取素材，请检查视频或图片格式") from None
    hashes = [
        hashlib.sha256(f'{item["key"]}:{item.get("size_bytes", 0)}'.encode()).hexdigest()
        for item in images
    ]
    return {**info, "roles": roles, "audio": audio, "template": template, "image_sha256": hashes}


async def validate_editor_assets(objects: list[dict], audio: str = "original", *, remote_probe: bool = True) -> dict:
    """Validate generic numbered references for the 视频编辑 project tool."""
    videos = [item for item in objects if item.get("field") == "videos"]
    images = [item for item in objects if item.get("field") == "images"]
    if len(videos) != 1 or len(images) > 3:
        raise HTTPException(422, "视频编辑需要一条视频，参考图最多 3 张")
    if audio != "original":
        raise HTTPException(422, "视频编辑第一版固定保留原片声音")
    if int(videos[0].get("size_bytes") or 0) > MAX_VIDEO_BYTES:
        raise HTTPException(413, "参考视频不能超过 300 MB")
    if any(int(item.get("size_bytes") or 0) > MAX_IMAGE_BYTES for item in images):
        raise HTTPException(413, "参考图片每张不能超过 30 MB")
    try:
        if videos[0].get("storage") == "local":
            inspect = inspect_remix_local
        else:
            inspect = inspect_remix_remote if remote_probe else inspect_remix_asset
        info = await asyncio.to_thread(inspect, videos[0], max_duration=None)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        raise HTTPException(422, str(exc) if isinstance(exc, ValueError) else "无法读取素材，请检查视频或图片格式") from None
    hashes = [hashlib.sha256(f'{item["key"]}:{item.get("size_bytes", 0)}'.encode()).hexdigest() for item in images]
    return {**info, "roles": [f"reference_{i}" for i in range(1, len(images) + 1)],
            "audio": audio, "template": "generic", "image_sha256": hashes}


async def validate_editor_uploads(videos: list, images: list, audio: str = "original") -> dict:
    if len(videos) != 1 or len(images) > 3:
        raise HTTPException(422, "视频编辑需要一条视频，参考图最多 3 张")
    if audio != "original":
        raise HTTPException(422, "视频编辑第一版固定保留原片声音")
    try:
        info = await asyncio.to_thread(inspect_remix, videos[0], max_duration=None)
        hashes = []
        for upload in images:
            normalized = await asyncio.to_thread(normalize_upload, upload)
            hashes.append(hashlib.sha256(normalized).hexdigest())
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        raise HTTPException(422, str(exc) if isinstance(exc, ValueError) else "无法读取素材，请检查视频或图片格式") from None
    return {**info, "roles": [f"reference_{i}" for i in range(1, len(images) + 1)],
            "audio": audio, "template": "generic", "image_sha256": hashes}


def infer_editor_roles(prompt: str, count: int) -> tuple[list[str], str]:
    """Map numbered editor references to the existing role-aware Hypit path.

    The UI stays generic; explicit words in the user's Prompt provide the
    semantic mapping. Unmentioned slots fall back to canonical order so the
    legacy analyzer/compiler can still operate deterministically.
    """
    text = prompt.lower()
    role_words = {
        "person": ("人物", "人", "person", "presenter", "模特"),
        "product": ("商品", "产品", "物品", "product", "服装", "道具"),
        "scene": ("场景", "背景", "环境", "scene", "background", "environment"),
    }
    found = []
    for role, words in role_words.items():
        if any(word in text for word in words):
            found.append(role)
    # Uploaded pictures are optional for the generic editor.  With no
    # uploads, retain explicit roles from the Prompt so the post-analysis
    # planner can decide whether to extract a frame or call Image2.
    roles = found if count == 0 else found[:count]
    if count == 0 and not roles:
        return [], "generic"
    for role in ("person", "product", "scene"):
        if len(roles) >= count:
            break
        if role not in roles:
            roles.append(role)
    if count > 0:
        roles = roles[:count]
    template = next((name for name, expected in {**REMIX_TEMPLATES, **REMIX_EXTRA_TEMPLATES}.items()
                     if expected == set(roles)), "")
    if not template:
        raise HTTPException(422, "Prompt 中的参考图用途目前只支持人物、商品和场景")
    return roles, template


def remix_direction(template: str, extra: str = "") -> str:
    directions = {
        "person": "人物外观、身份和服装以人物参考图为唯一视觉依据；严格沿用拆解脚本的时间轴、镜头、动作、商品交互和场景布局；不要增加人物、字幕或额外动作。",
        "product": "商品外观以商品参考图为唯一视觉依据；严格沿用拆解脚本的时间轴、镜头、人物动作、商品展示方式和场景布局；不要增加商品、字幕或额外动作。",
        "scene": "环境与背景以场景参考图为唯一视觉依据；严格沿用拆解脚本的时间轴、镜头、人物动作和商品交互；不要增加场景切换、字幕或额外动作。",
        "person_product": "人物外观与服装以人物参考图为准，商品外观以商品参考图为准；严格沿用拆解脚本的时间轴、镜头、展示动作和场景布局；不要增加人物、商品、字幕或额外动作。",
        "all": "人物、商品和环境分别以对应参考图为唯一视觉依据；严格沿用拆解脚本的时间轴、镜头、动作、交互和转场；不要增加主体、字幕或额外动作。",
    }
    base = directions.get(template, directions["person"])
    return base + ((" 用户补充要求：" + extra.strip()) if extra.strip() else "")


def simple_remix_prompt(template: str, duration: int, ratio: str) -> str:
    """Deterministic one-shot Ref2VA prompt; no analysis or invented shots."""
    roles = {
        "person": [("Picture 1", "PERSON")],
        "product": [("Picture 1", "PRODUCT")],
        "scene": [("Picture 1", "ENVIRONMENT")],
        "person_product": [("Picture 1", "PERSON"), ("Picture 2", "PRODUCT")],
        "person_scene": [("Picture 1", "PERSON"), ("Picture 2", "ENVIRONMENT")],
        "product_scene": [("Picture 1", "PRODUCT"), ("Picture 2", "ENVIRONMENT")],
        "all": [("Picture 1", "PERSON"), ("Picture 2", "PRODUCT"), ("Picture 3", "ENVIRONMENT")],
    }.get(template, [])
    definitions = " ".join(
        f"<{picture}> is the {kind} reference and the sole visual authority for the replacement {kind.lower()}."
        for picture, kind in roles
    )
    return (
        "[Reference definitions] <Video 1> is the source video for the target video edit. "
        + definitions + " <Audio 1> is the synchronized original audio track to reuse.\n"
        "[Summary] [video editing + reference generation + audio reuse] Create a one-shot edited version of <Video 1> at "
        f"{duration} seconds, {ratio}. Preserve the complete source timeline, shot order, framing, camera movement, "
        "actions, interactions, cuts and pacing. Replace only the selected roles with their numbered picture references.\n"
        "[Retention and removal] Fully preserve every non-selected person, product, environment, action and camera property. "
        "<Video 1>: partially_preserved — preserve its temporal and cinematic structure, but remove only the selected roles. "
        "Each listed Picture: fully_preserved as the identity/appearance authority for its role. <Audio 1>: fully_copy. "
        "Do not add subjects, captions, logos, transitions, shots or actions that are not present in the source.\n"
        "[Detailed description] [Shot 1] Use <Video 1> as the exact temporal reference for the entire target. "
        "Maintain natural motion, continuity, composition, lighting and realistic contact between retained and replaced elements. "
        "Keep the original number and timing of shots; do not invent a storyboard.\n"
        "[Sound] Reuse <Audio 1> unchanged and synchronized to the final video."
    )


def generic_editor_prompt(prompt: str, count: int, duration: int, ratio: str) -> str:
    refs = " ".join(
        f"<Picture {i}> is reference image {i}; use it only according to the user's numbered instruction."
        for i in range(1, count + 1)
    )
    return (
        f"[Reference definitions] <Video 1> is the source video. {refs} "
        "<Audio 1> is the original synchronized audio.\n"
        f"[Summary] Create a {duration}-second {ratio} video edit using the source video as the temporal, camera and action reference. "
        f"User edit instruction: {prompt.strip()}\n"
        "[Retention] Preserve the source shot order, framing, camera movement, action timing, lighting, sound and all elements "
        "not explicitly changed. Apply each numbered picture only to the object or role named in the instruction. "
        "Do not invent additional subjects, shots, captions or transitions.\n"
        "[Sound] Reuse <Audio 1> unchanged and synchronized."
    )
def _blueprint_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return ", ".join(
            f"{key}: {_blueprint_text(item)}" for key, item in value.items()
            if item not in (None, "", [], {})
        )
    if isinstance(value, list):
        return ", ".join(filter(None, (_blueprint_text(item) for item in value)))
    return str(value).strip()


def remix_shot_contract(shot: dict, index: int, duration: float) -> str:
    """One deterministic visual contract shared by image and video prompts."""
    # Qwen Plus returns ``start``/``end``; the Gongji analyzer normalizes to
    # ``*_seconds``. Accept both so the final Ref2VA script keeps real shot
    # boundaries instead of expanding every shot to the full video duration.
    start_value = shot.get("start_seconds")
    if start_value is None:
        start_value = shot.get("start")
    end_value = shot.get("end_seconds")
    if end_value is None:
        end_value = shot.get("end")
    start = max(0.0, float(start_value or 0))
    end = min(duration, float(end_value if end_value is not None else duration))
    details = []
    labels = {
        "shot_size": "shot size", "camera": "camera", "composition": "composition",
        "action": "action", "subject_interaction": "subject interaction",
        "props": "props", "environment_layout": "environment layout",
        "props_product": "props and product", "environment": "environment",
        "action_beats": "action beats", "transition": "transition",
        "transition_effects": "transition",
    }
    for key, label in labels.items():
        text = _blueprint_text(shot.get(key))
        if text and text.lower() not in {"unknown", "none", "n/a"}:
            details.append(f"{label}: {text}")
    if not details:
        details.append("action: natural motion matching the source frame")
    return f"[Shot {index}] {start:.3f}s-{end:.3f}s; " + "; ".join(details) + "."


def _visible_replacement_roles(shot: dict, roles: list[str]) -> list[str]:
    """Infer which requested roles are visibly present from one Qwen shot."""
    text = json.dumps(shot, ensure_ascii=False).lower()
    negatives = {
        "person": ("no person", "without person", "person absent", "无人", "无人物", "仅商品"),
        "product": ("no product", "without product", "product absent", "无商品", "无产品"),
    }
    positives = {
        "person": (
            "person", "people", "presenter", "human", "man", "woman", "boy", "girl",
            "人物", "人像", "模特", "男子", "女子",
        ),
        "product": (
            "product", "item", "goods", "package", "bottle", "phone", "car", "shoe",
            "商品", "产品", "物品", "道具",
        ),
    }
    visible = []
    for role in roles:
        if role == "scene":
            visible.append(role)
        elif not any(term in text for term in negatives[role]) and any(
            term in text for term in positives[role]
        ):
            visible.append(role)
    return visible


def plan_remix_keyframes(blueprint: dict, duration: float,
                         roles: list[str] | None = None,
                         max_frames: int = 8) -> list[dict]:
    """Choose one midpoint anchor per scene, bounded by H3's picture limit."""
    requested_roles = list(roles or [])
    shots = [item for item in (blueprint or {}).get("shots") or [] if isinstance(item, dict)]
    if not shots:
        shots = [{"start_seconds": 0, "end_seconds": duration}]
    normalized = []
    for index, shot in enumerate(shots, 1):
        # Qwen/Hypit responses use either `start`/`end` or the normalized
        # `*_seconds` names. Accept both so every shot gets its own anchor.
        start_value = shot.get("start_seconds")
        if start_value is None:
            start_value = shot.get("start")
        end_value = shot.get("end_seconds")
        if end_value is None:
            end_value = shot.get("end")
        start = min(max(float(start_value or 0), 0), duration)
        end = min(max(float(end_value or duration), start + .001), duration)
        if end > start:
            normalized.append((index, shot, start, end))
    if not normalized:
        normalized = [(1, shots[0], 0.0, float(duration))]
    count = min(max(1, max_frames), len(normalized))
    if len(normalized) <= count:
        selected = [item + (.5,) for item in normalized]
    else:
        selected = [normalized[min(
            len(normalized) - 1,
            math.floor((slot + .5) * len(normalized) / count),
        )] + (.5,) for slot in range(count)]

    result = []
    for index, (shot_index, shot, start, end, fraction) in enumerate(selected, 1):
        result.append({
            "index": index, "kind": "keyframe",
            "timestamp": round(start + (end - start) * fraction, 3),
            "shot_index": shot_index,
            "visible_roles": _visible_replacement_roles(shot, requested_roles),
            "contract": remix_shot_contract(shot, shot_index, duration),
        })
    return result


def remix_keyframe_prompt(plan: dict, roles: list[str], ratio: str,
                          generated_visual_prompt: str = "",
                          reference_roles: list[str] | None = None,
                          identity_context: str = "",
                          wardrobe_context: str = "") -> str:
    reference_roles = set(reference_roles or roles)
    role_lines = []
    for index, role in enumerate((role for role in roles if role in reference_roles), 2):
        role_lines.append(
            f"Input image {index} is the sole appearance authority for the {ROLE_NAMES[role]}. "
            f"Apply it only to the already-visible {ROLE_NAMES[role]} in Input image 1."
        )
    role_action = (
        "Replace only the listed visible roles while preserving every unlisted person, product and environment exactly."
        if roles else
        "No selected replacement role is visible in this scene; faithfully reconstruct Input image 1 without changing any subject."
    )
    generated_direction = (
        " Apply this requested new visual treatment to the target frame while preserving the source camera, pose and composition: "
        + generated_visual_prompt.strip()
        if generated_visual_prompt.strip() else ""
    )
    identity_direction = (
        " PERSON IDENTITY LOCK: the target person reference is the identity anchor. Keep the same face, apparent age, "
        "skin tone, hair, body proportions and recognizable identity in every frame. Do not blend identities, invent "
        "a second person, or change the person's ethnicity or age."
        if "person" in roles else ""
    )
    if identity_context.strip() and "person" in roles:
        identity_direction += " Identity description from the analyzed script: " + identity_context.strip() + "."
    wardrobe_direction = (" " + wardrobe_context.strip()) if wardrobe_context.strip() else ""
    return (
        f"Create one photorealistic {ratio} final target scene frame at exactly "
        f"{float(plan['timestamp']):.3f}s. Input image 1 is an extracted source-video frame; "
        "it is the complete and sole authority for camera, framing, crop, visible subject count, "
        "composition, pose, action, spatial layout and lighting direction. "
        + role_action + "\n" + "\n".join(role_lines)
        + identity_direction + wardrobe_direction + "\n"
        + "\n"
        "Visual-evidence lock: do not reinterpret or expand Input image 1 from a written storyboard. "
          "If no person is visible in Input image 1, show no person. "
          "If no product is visible, show no product. Never add a person or product that is not visibly present. "
          "An off-frame hand or arm must remain equally partial and must never become a complete extra person."
        + generated_direction
        + "\nRender the final target frame directly. Keep anatomy, hands, face and product geometry natural and coherent. "
          "No comparison layout, split screen, captions, typography, logo additions, watermark or border."
    )


def _remix_wardrobe_context(analysis: dict, shot_index: int) -> str:
    """Tell the image model whether clothing should stay stable or follow a scripted change."""
    shots = list((analysis or {}).get("shots") or [])
    change_terms = ("different outfit", "outfit change", "wardrobe change", "changes clothes",
                    "换装", "换衣服", "不同服装", "更换穿着")
    selected_shot = next((item for item in shots if int(item.get("index") or 0) == shot_index), None)
    if selected_shot is None and 1 <= shot_index <= len(shots):
        selected_shot = shots[shot_index - 1]
    shot_text = json.dumps(selected_shot or {}, ensure_ascii=False).lower()
    all_text = json.dumps(shots, ensure_ascii=False).lower()
    if any(term in shot_text for term in change_terms):
        return (" Wardrobe continuity rule: the script contains a wardrobe change. For this shot, follow the source "
                "frame's own outfit state and keep that outfit consistent through the shot; preserve each scripted "
                "outfit change while keeping the same target person's identity.")
    if any(term in all_text for term in change_terms):
        return (" Wardrobe continuity rule: this shot is between scripted wardrobe changes. Keep the outfit visible "
                "in this source frame stable and do not move the change earlier or later in the timeline.")
    return (" Wardrobe continuity rule: the script has no wardrobe change. Keep the target person's outfit, colours "
            "and accessories consistent across all shots unless the source frame clearly requires otherwise.")


def remix_prompt(roles: list[str], duration: float, ratio: str, audio: str,
                 blueprint: dict | None = None, brief: str = "",
                 keyframes: list[dict] | None = None) -> str:
    """Compile an image-only Ref2VA fallback from a Qwen video blueprint.

    The source video is evidence for Qwen only.  It must never become an H3
    reference asset or appear as ``<Video 1>`` in the generation prompt.
    """
    definitions = []
    retention = []
    for i, role in enumerate(roles, 1):
        name = ROLE_NAMES[role]
        definitions.append(
            f"<Subject {i}> is the {name} shown in <Picture {i}>. "
            f"Use <Picture {i}> as the only visual authority for this {name} and preserve all clearly visible details."
        )
        retention.append(
            f"<Subject {i}> (appears wherever the {name} is required): fully_preserved - "
            f"preserve the visible {name} from <Picture {i}> consistently."
        )
    picture_number = len(roles) + 1
    shots = []
    keyframes = keyframes or []
    anchors: dict[int, list[str]] = {}
    for offset, frame in enumerate(keyframes, picture_number):
        shot_index = int(frame["shot_index"])
        definitions.append(
            f"<Picture {offset}> is the final target appearance and composition anchor for Shot {shot_index}. "
            "Preserve every visible unselected person, product and environment exactly as shown."
        )
        anchors.setdefault(shot_index, []).append(
            f"At {float(frame['timestamp']):.3f}s match <Picture {offset}> as the exact target appearance, composition, pose and layout anchor"
        )
    for index, shot in enumerate((blueprint or {}).get("shots") or [], 1):
        line = remix_shot_contract(shot, index, duration)
        if anchors.get(index):
            line += " " + "; ".join(anchors[index]) + "."
        shots.append(line)
    if not shots:
        shots.append(
            f"[Shot 1] At 0.000s-{duration:.3f}s, use the supplied reference subjects in a natural, "
            "coherent shot while following the requested action."
        )
    if brief.strip():
        shots.append("User direction: " + brief.strip())
    return ("subject_definitions:\n" + "\n".join(definitions)
            + f"\n\nsummary:\n[reference generation] Generate one continuous {duration:.2f}-second {ratio} result from the uploaded role pictures and one target frame per selected source scene."
            + "\n\nretention_analysis:\n" + "\n".join(retention)
            + "\n\ndetailed_description:\n" + "\n".join(shots)
            + " Keep every referenced person, product, and environment consistent across cuts and occlusions. "
              "Uploaded role pictures are the identity authority for selected roles; target scene frames govern all visible final appearances, pose, composition and spatial layout. "
              "Never introduce a complete person or product not visibly present in the corresponding target scene frame; "
              "an off-frame hand or arm must stay partial and must never become an extra subject."
            + "\n\noverall_soundscape:\n" + ("Natural sound appropriate to the referenced actions, respecting user instructions." if audio == "generate" else "N/A")
            + "\n\nnon_diegetic_music:\nN/A")


def normalize_upload(upload: UploadFile) -> bytes:
    if upload.size is not None and upload.size > MAX_IMAGE_BYTES:
        raise ValueError("每张图片不能超过 30 MB")
    upload.file.seek(0)
    data = upload.file.read(MAX_IMAGE_BYTES + 1)
    upload.file.seek(0)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("每张图片不能超过 30 MB")
    with Image.open(BytesIO(data)) as im:
        if im.format not in {"PNG", "JPEG", "WEBP", "HEIF", "HEIC", "GIF", "BMP", "TIFF"} or im.width * im.height > 32_000_000:
            raise ValueError("请使用 3200 万像素以内的 JPG、PNG、WebP 或手机照片格式")
        im = ImageOps.exif_transpose(im)
        has_alpha = "transparency" in im.info
        if "A" in im.getbands():
            alpha = im.getchannel("A")
            # Some image generators leave a tiny, barely translucent fringe
            # on otherwise opaque photographs. Treat that as a photo; keeping
            # those few pixels would force a 20+ MB PNG through both network
            # hops. Real transparent assets retain PNG.
            materially_transparent = sum(alpha.histogram()[:250])
            has_alpha = materially_transparent > im.width * im.height // 100
        out = BytesIO()
        if has_alpha:
            im.convert("RGBA").save(out, format="PNG")
        else:
            # Photos often expand several times when forced to PNG. Keep the
            # original dimensions and visual detail, but use an efficient
            # high-quality photographic representation before the HK -> COS
            # and COS -> image-provider transfers.
            if "A" in im.getbands():
                rgba = im.convert("RGBA")
                rgb = Image.alpha_composite(
                    Image.new("RGBA", rgba.size, "white"), rgba
                ).convert("RGB")
            else:
                rgb = im.convert("RGB")
            rgb.save(out, format="JPEG", quality=95, subsampling=0)
        if out.tell() > MAX_IMAGE_BYTES:
            raise ValueError("图片解码后过大，请缩小图片尺寸再试")
        return out.getvalue()


def normalized_upload(data: bytes, index: int) -> UploadFile:
    suffix = ".jpg" if data.startswith(b"\xff\xd8\xff") else ".png"
    return UploadFile(BytesIO(data), filename=f"image-{index + 1}{suffix}")


def copy_asset(item: dict, target: Path, *, output: bool = False) -> None:
    if item.get("storage") == "local":
        source = local_storage.resolve(item["path"]) if output else transfer_storage.resolve_input(item["key"])
        shutil.copyfile(source, target)
    else:
        object_storage.download_path(item["key"], target)


def store_output(row: dict, path: Path, media: str) -> dict:
    if local_storage.enabled() or json.loads(row["payload"]).get("_transfer_backend") == "local":
        relative = f'{row["merchant_id"]}/{row["id"]}/{path.name}'
        target = local_storage._safe_relative(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".part")
        shutil.copyfile(path, temp)
        temp.replace(target)
        return {"name": path.name, "media": media, "storage": "local", "path": relative, "size_bytes": path.stat().st_size}
    prefix = os.getenv("COS_OUTPUT_PREFIX", "outputs").strip("/")
    key = f'{prefix}/{row["merchant_id"]}/{row["id"]}/{path.name}'
    object_storage.upload_path(path, key, "image/png" if media == "image" else "video/mp4")
    return {"name": path.name, "media": media, "key": key, "bucket": object_storage.bucket_name(), "size_bytes": path.stat().st_size}


def finalize_remix(row: dict, artifacts: list[dict]) -> list[dict]:
    ready = [a for a in artifacts if a["name"] == "remix-final.mp4"]
    if ready:
        return ready
    settings = json.loads(row["payload"])["_remix"]
    source_item = next(a for a in json.loads(row["input_objects"]) if a["field"] == "videos")
    with tempfile.TemporaryDirectory(prefix="equai-remix-") as directory:
        root = Path(directory)
        source, rendered, output = root / "source.mp4", root / "generated.mp4", root / "remix-final.mp4"
        copy_asset(artifacts[0], rendered, output=True)
        command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(rendered)]
        if settings["audio"] == "original" and settings["has_audio"]:
            copy_asset(source_item, source)
            command += ["-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac"]
        elif settings["audio"] == "generate":
            command += ["-map", "0:v:0", "-map", "0:a?", "-c:a", "copy"]
        else:
            command += ["-map", "0:v:0", "-an"]
        command += ["-c:v", "copy", "-t", str(settings["source_seconds"]), "-movflags", "+faststart", str(output)]
        subprocess.run(command, check=True, capture_output=True, timeout=180)
        actual = float(probe(output)["format"]["duration"])
        if actual < settings["source_seconds"] - .25:
            raise ValueError("生成视频长度不足，未交付截短结果")
        return [store_output(row, output, "video")]


@router.post("/api/account/image-edits", status_code=202)
async def create_image_edit(request: Request, merchant: Annotated[dict, Depends(video_merchant)],
                            prompt: Annotated[str, Form()], images: Annotated[list[UploadFile] | None, File()] = None,
                            note: Annotated[str, Form()] = "",
                            idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> dict:
    from video_api import _upload, _remove_uploaded_inputs, _get_task, _task_view

    images = images or []
    settings = image_settings()
    if not settings["key"] or settings["price"] <= 0:
        raise HTTPException(503, "图片编辑正在配置中，暂未开放提交")
    if len(images) > 4 or not 1 <= len(prompt.strip()) <= 8000 or len(note) > 80:
        raise HTTPException(422, "图片最多 4 张，描述 1–8000 字，备注最多 80 字")
    if idempotency_key and len(idempotency_key) > 180:
        raise HTTPException(422, "请求标识过长")
    normalized = []
    try:
        for upload in images:
            normalized.append(await asyncio.to_thread(normalize_upload, upload))
    except (OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "请上传有效的 PNG、JPG 或 WebP 图片，每张不超过 30 MB / 3200 万像素") from None
    identity = hashlib.sha256(json.dumps([prompt.strip(), note, [hashlib.sha256(x).hexdigest() for x in normalized]], ensure_ascii=False).encode()).hexdigest()
    if idempotency_key:
        with connect() as db:
            existing = db.execute("SELECT * FROM merchant_tasks WHERE merchant_id=? AND idempotency_key=?", (merchant["id"], idempotency_key)).fetchone()
        if existing:
            if existing["request_hash"] != identity:
                raise HTTPException(409, "同一请求标识不能用于不同图片任务")
            return _task_view(existing, with_download=False)
    if merchant["status"] != "active":
        raise HTTPException(403, "账户尚未启用或已暂停")
    if merchant["balance_milli"] < settings["price"]:
        raise HTTPException(402, "积分不足")
    task_id, objects = public_id("img"), []
    try:
        for i, data in enumerate(normalized):
            upload = normalized_upload(data, i)
            objects.append(await _upload(
                merchant["id"], task_id, i, "images", upload, force_local=True,
            ))
        now = time.time()
        if normalized:
            with Image.open(BytesIO(normalized[0])) as image:
                ratios = {"1:1": 1, "3:2": 1.5, "4:3": 4/3, "5:4": 1.25, "16:9": 16/9,
                          "21:9": 21/9, "2:3": 2/3, "3:4": .75, "4:5": .8, "9:16": 9/16, "9:21": 9/21}
                ratio = min(ratios, key=lambda r: abs(ratios[r] - image.width / image.height))
        else:
            ratio = "1:1"
        payload = {"_tool": "image_edit", "prompt": prompt.strip(), "_transfer_backend": transfer_storage.backend()}
        with transaction() as db:
            db.execute("""INSERT INTO merchant_tasks
                (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,payload,input_objects,
                 price_milli,failure_price_milli,idempotency_key,request_hash,created_at,queued_at,portal_note)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, merchant["id"], "image_edit", "image", 0, ratio, "图片编辑", "queued", 0, "图片已接收，等待处理",
                 json.dumps(payload, ensure_ascii=False), json.dumps(objects), settings["price"], 0, idempotency_key, identity, now, now, note))
            for item in objects:
                db.execute("""INSERT INTO assets(id,merchant_id,task_id,role,bucket,object_key,filename,content_type,size_bytes,created_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?)""", (item["id"], merchant["id"], task_id, "input", item["bucket"], item["key"], item["filename"], item["content_type"], item.get("size_bytes"), now))
            reserve_task(db, merchant["id"], task_id, "图片编辑", amount_milli=settings["price"])
    except Exception as exc:
        await _remove_uploaded_inputs(objects)
        if isinstance(exc, sqlite3.IntegrityError) and idempotency_key:
            with connect() as db:
                existing = db.execute("SELECT * FROM merchant_tasks WHERE merchant_id=? AND idempotency_key=?", (merchant["id"], idempotency_key)).fetchone()
            if existing and existing["request_hash"] == identity:
                return _task_view(existing, with_download=False)
            raise HTTPException(409, "同一请求标识不能用于不同任务") from None
        if isinstance(exc, (ValueError, PermissionError, RuntimeError)):
            raise HTTPException(409, str(exc)) from None
        raise
    return _task_view(_get_task(merchant["id"], task_id), with_download=False)


def provider_data(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        raise ValueError(f"图片服务请求失败（{response.status_code}）")
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("图片服务返回格式异常")
    data = value.get("data", value)
    if not isinstance(data, dict):
        raise ValueError("图片服务返回格式异常")
    return data


def provider_task_id(value: dict) -> str:
    task_id = value.get("id") or value.get("task_id")
    if not task_id:
        tasks = value.get("tasks")
        if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict):
            task_id = tasks[0].get("id")
    return str(task_id) if task_id else ""


def provider_image_payload(path: Path, index: int) -> tuple[bytes, str, str]:
    """Keep alpha lossless; shrink opaque photos before the external upload."""
    original = path.read_bytes()
    with Image.open(BytesIO(original)) as image:
        has_alpha = (
            "A" in image.getbands()
            and image.getchannel("A").getextrema()[0] < 255
        )
        if has_alpha:
            return original, f"{index}.png", "image/png"
        output = BytesIO()
        image.convert("RGB").save(
            output, format="JPEG", quality=95, subsampling=0,
        )
    encoded = output.getvalue()
    if len(encoded) >= len(original):
        return original, f"{index}.png", "image/png"
    return encoded, f"{index}.jpg", "image/jpeg"


async def _provider_upload(client: httpx.AsyncClient, headers: dict,
                           path: Path, index: int) -> str:
    content, filename, content_type = await asyncio.to_thread(
        provider_image_payload, path, index
    )
    response = await client.post(
        image_settings()["base"] + "/v1/files",
        headers={**headers, "X-File-Name": filename, "Content-Type": content_type},
        content=content,
    )
    upload_id = provider_data(response).get("id")
    if not upload_id:
        raise ValueError("图片上传未返回编号")
    return str(upload_id)


def _extract_remix_frame(source: Path, target: Path, timestamp: float) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{timestamp:.3f}",
         "-protocol_whitelist", "file,pipe", "-i", str(source), "-frames:v", "1",
         "-vf", "setsar=1", str(target)],
        check=True, capture_output=True, timeout=90,
    )
    if not target.is_file() or target.stat().st_size < 100:
        raise ValueError("无法提取视频关键帧")


async def dispatch_remix_keyframes(row: dict) -> None:
    """Render source-timeline anchors with the user's role references."""
    from scheduler import _terminal_failure

    try:
        settings = image_settings()
        if not settings["key"]:
            raise ValueError("图片生成接口未配置")
        payload = json.loads(row["payload"] or "{}")
        options = payload["_remix"]
        source_duration = float(options.get("source_seconds") or row["duration"])
        keyframe_plan = plan_remix_keyframes(
            options["analysis"], source_duration, options["roles"],
            max_frames=max(1, 9 - len(options["roles"])),
        )
        inputs = json.loads(row["input_objects"] or "[]")
        source_item = next(item for item in inputs if item.get("field") == "videos")
        requested_roles = list(options.get("roles") or [])
        uploaded_roles_value = options.get("uploaded_roles")
        uploaded_roles = list(
            requested_roles if uploaded_roles_value is None else uploaded_roles_value
        )
        role_items = [
            item for item in inputs
            if item.get("field") == "images"
            and not item.get("generated_kind")
            and not item.get("source_evidence")
        ]
        if len(role_items) != len(uploaded_roles):
            raise ValueError("复刻参考图与用途数量不一致")
        now = time.time()
        with connect() as db:
            changed = db.execute(
                """UPDATE merchant_tasks SET status='running',remote_id='remix-images:submitting',
                started_at=?,progress=38,message='正在准备复刻素材' WHERE id=? AND status='dispatching'""",
                (now, row["id"]),
            )
        if not changed.rowcount:
            return
        metrics = options.setdefault("metrics", {})
        metrics["image_stage_started_at"] = now
        metrics["image_api_calls"] = len(keyframe_plan)
        options["keyframe_plan"] = keyframe_plan
        payload["_remix"] = options
        with connect() as db:
            db.execute(
                "UPDATE merchant_tasks SET payload=? WHERE id=? AND status='running'",
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), row["id"]),
            )
            project = payload.get("_project") or {}
            if project.get("project_id"):
                video_projects.update_keyframes(
                    db, str(project["project_id"]), keyframe_plan, status="generating"
                )
        headers = {"Authorization": f'Bearer {settings["key"]}'}
        tasks = []
        async with httpx.AsyncClient(timeout=180) as client:
            with tempfile.TemporaryDirectory(prefix="equai-remix-frames-") as directory:
                root = Path(directory)
                source = root / "source.mp4"
                await asyncio.to_thread(copy_asset, source_item, source)
                role_uploads = {}
                for index, (role, item) in enumerate(zip(uploaded_roles, role_items), 1):
                    role_path = root / f"role-{index}.png"
                    await asyncio.to_thread(copy_asset, item, role_path)
                    role_uploads[role] = await _provider_upload(
                        client, headers, role_path, index,
                    )
                extracted: list[tuple[dict, Path]] = []
                for sequence, frame in enumerate(keyframe_plan, 1):
                    frame_path = root / f'frame-{sequence}.png'
                    await asyncio.to_thread(
                        _extract_remix_frame, source, frame_path,
                        float(frame["timestamp"]),
                    )
                    # Keep the extracted source frame as a deterministic
                    # fallback.  An image provider may reject an individual
                    # replacement prompt; that must not fail the whole
                    # commerce remix.
                    fallback_relative, fallback_size = transfer_storage.store_input(
                        BytesIO(frame_path.read_bytes()), row["merchant_id"], row["id"],
                        len(inputs) + sequence, f"source-scene-{sequence:02d}.png",
                    )
                    frame["fallback_key"] = fallback_relative
                    frame["fallback_size"] = fallback_size
                    extracted.append((frame, frame_path))
                for sequence, (frame, frame_path) in enumerate(extracted, 1):
                    frame_upload = await _provider_upload(
                        client, headers, frame_path, 100 + sequence,
                    )
                    visible_roles = [
                        role for role in requested_roles
                        if role in frame.get("visible_roles", [])
                    ]
                    asset_plan = options.get("asset_plan") or {}
                    planned_prompts = asset_plan.get("image_prompts") or []
                    # Apply a generated replacement only when the requested
                    # role is actually visible in this source frame. Product
                    # and detail-only shots must remain untouched; sending a
                    # new-person prompt to those frames can trigger provider
                    # policy rejection and would incorrectly fail the whole
                    # edit.
                    generated_visual_prompt = (
                        str(planned_prompts[0])
                        if planned_prompts and visible_roles else ""
                    )
                    analysis = options.get("analysis") or {}
                    original_person = (analysis.get("original") or {}).get("person") or ""
                    target_person = (analysis.get("targets") or {}).get("person") or ""
                    identity_context = " ".join(part.strip() for part in (target_person, original_person) if part.strip())
                    prompt = remix_keyframe_prompt(
                        frame, visible_roles, row["ratio"], generated_visual_prompt,
                        reference_roles=list(role_uploads),
                        identity_context=identity_context,
                        wardrobe_context=_remix_wardrobe_context(analysis, int(frame.get("shot_index") or 0)),
                    )
                    response = await client.post(
                        settings["base"] + "/v1/images/tasks",
                        headers={**headers, "Idempotency-Key": f'{row["id"]}-{frame["kind"]}-{frame.get("role") or frame["index"]}'},
                        json={
                            "model": settings["model"],
                            "prompt": prompt,
                            "aspect_ratio": row["ratio"],
                            "upload_ids": [frame_upload] + [
                                role_uploads[role] for role in visible_roles if role in role_uploads
                            ],
                            "image_urls": [], "count": 1,
                        },
                    )
                    remote_id = provider_task_id(provider_data(response))
                    if not remote_id:
                        raise ValueError("图片服务未返回任务编号")
                    tasks.append({**frame, "remote_id": remote_id, "submitted_at": time.time()})
                    options["keyframe_tasks"] = tasks
                    payload["_remix"] = options
                    with connect() as db:
                        db.execute(
                            "UPDATE merchant_tasks SET payload=? WHERE id=? AND status='running'",
                            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), row["id"]),
                        )
        with connect() as db:
            db.execute(
                """UPDATE merchant_tasks SET remote_id='remix-images',progress=45,
                message='正在准备复刻素材' WHERE id=? AND status='running'""",
                (row["id"],),
            )
    except Exception as exc:  # Never replay a possibly accepted image request.
        log.exception("Remix keyframe dispatch failed for %s", row["id"])
        _terminal_failure(row["id"], f"复刻素材准备失败: {exc}", False)


def _store_remix_keyframes(row: dict, values: list[bytes],
                           tasks: list[dict]) -> tuple[list[dict], list[str]]:
    inputs = json.loads(row["input_objects"] or "[]")
    stored, paths = [], []
    for offset, (data, task) in enumerate(zip(values, tasks), len(inputs)):
        output = BytesIO()
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 64_000_000:
                raise ValueError("关键帧图片尺寸过大")
            image.convert("RGB").save(output, format="PNG")
        filename = f'scene-{int(task["shot_index"]):02d}-target.png'
        relative, size = transfer_storage.store_input(
            output, row["merchant_id"], row["id"], offset, filename,
        )
        paths.append(relative)
        stored.append({
            "id": public_id("ast"), "storage": "local", "bucket": "local-input",
            "key": relative, "filename": filename, "content_type": "image/png",
            "field": "images", "size_bytes": size, "generated_kind": task["kind"],
            "shot_index": int(task.get("shot_index") or 0),
            "timestamp": float(task["timestamp"]), "contract": task.get("contract", ""),
            "visible_roles": list(task.get("visible_roles") or []),
        })
    return stored, paths


async def poll_remix_keyframes(row: dict) -> None:
    from scheduler import _completion_locks, _terminal_failure

    async with _completion_locks[hash(row["id"]) % len(_completion_locks)]:
        with connect() as db:
            current = db.execute(
                "SELECT status,payload,input_objects FROM merchant_tasks WHERE id=?", (row["id"],)
            ).fetchone()
        if not current or current["status"] != "running":
            return
        payload = json.loads(current["payload"] or "{}")
        tasks = list((payload.get("_remix") or {}).get("keyframe_tasks") or [])
        if not tasks or row["remote_id"] == "remix-images:submitting":
            return
        if time.time() - (row["started_at"] or row["created_at"]) > 1200:
            _terminal_failure(row["id"], "复刻素材准备超时，积分已释放", False)
            return
        settings = image_settings()
        completed = []
        fallback_values = []
        provider_metrics = []
        async with httpx.AsyncClient(timeout=30) as client:
            for task in tasks:
                response = await client.get(
                    settings["base"] + "/v1/images/tasks/" + quote(str(task["remote_id"]), safe=""),
                    headers={"Authorization": f'Bearer {settings["key"]}'},
                )
                value = provider_data(response)
                status = str(value.get("status", "")).lower()
                if status in {"failed", "cancelled", "canceled"}:
                    fallback_key = str(task.get("fallback_key") or "")
                    if not fallback_key:
                        _terminal_failure(row["id"], "复刻关键帧生成失败，积分已释放", False)
                        return
                    # Preserve the original timeline frame when a single
                    # replacement is rejected by provider policy.
                    fallback_values.append(transfer_storage.resolve_input(fallback_key).read_bytes())
                    provider_metrics.append({
                        "kind": task["kind"], "role": task.get("role"),
                        "submitted_at": task.get("submitted_at"),
                        "observed_completed_at": time.time(),
                        "fallback": "source_frame",
                        "provider_status": status,
                    })
                    continue
                if status not in {"completed", "succeeded", "success"}:
                    return
                url = str((value.get("result") or {}).get("url") or "")
                if not url:
                    _terminal_failure(row["id"], "复刻关键帧没有返回图片，积分已释放", False)
                    return
                completed.append((url, task))
                provider_metrics.append({
                    "kind": task["kind"], "role": task.get("role"),
                    "submitted_at": task.get("submitted_at"),
                    "observed_completed_at": time.time(),
                    "provider_cost": value.get("billing", value.get("cost")),
                    "provider_usage": value.get("usage"),
                })
        generated_values = await asyncio.gather(*(fetch_image(url) for url, _task in completed))
        # Keep task order aligned with the keyframe plan. Failed provider
        # calls are represented by their source-frame fallback above.
        generated_iter = iter(generated_values)
        fallback_iter = iter(fallback_values)
        values = []
        for task in tasks:
            if any(task is done_task for _url, done_task in completed):
                values.append(next(generated_iter))
            else:
                values.append(next(fallback_iter))
        current_row = {**row, "input_objects": current["input_objects"]}
        stored, paths = await asyncio.to_thread(
            _store_remix_keyframes, current_row, values, tasks
        )
        now = time.time()
        try:
            inputs = json.loads(current["input_objects"] or "[]") + stored
            options = payload["_remix"]
            options["keyframes"] = [{
                "asset_id": item["id"], "filename": item["filename"],
                "shot_index": item["shot_index"], "timestamp": item["timestamp"],
                "contract": item["contract"], "visible_roles": item["visible_roles"],
            } for item in stored]
            metrics = options.setdefault("metrics", {})
            metrics["image_stage_completed_at"] = now
            metrics["image_stage_seconds"] = round(
                now - float(metrics.get("image_stage_started_at") or now), 3
            )
            metrics["image_calls"] = provider_metrics
            payload["_remix"] = options
            with transaction() as db:
                changed = db.execute(
                    """UPDATE merchant_tasks SET status='queued',remote_id=NULL,provider_started=0,
                    started_at=NULL,progress=55,message='复刻素材已准备，等待生成',payload=?,
                    input_objects=?,queued_at=? WHERE id=? AND status='running'""",
                    (json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                     json.dumps(inputs, ensure_ascii=False, separators=(",", ":")),
                     now, row["id"]),
                )
                if not changed.rowcount:
                    raise RuntimeError("任务状态已经发生变化")
                for item in stored:
                    db.execute(
                        """INSERT INTO assets(id,merchant_id,task_id,role,bucket,object_key,
                        filename,content_type,size_bytes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (item["id"], row["merchant_id"], row["id"], "input",
                         item["bucket"], item["key"], item["filename"],
                        item["content_type"], item["size_bytes"], now),
                    )
                project = payload.get("_project") or {}
                if project.get("project_id"):
                    video_projects.update_keyframes(
                        db, str(project["project_id"]), options["keyframes"], status="ready"
                    )
        except Exception:
            for path in paths:
                transfer_storage.remove_input(path)
            raise


async def dispatch_image(row: dict) -> None:
    from scheduler import _terminal_failure

    try:
        settings = image_settings()
        if not settings["key"]:
            raise ValueError("图片编辑接口未配置")
        headers = {"Authorization": f'Bearer {settings["key"]}'}
        uploads = []
        async with httpx.AsyncClient(timeout=180) as client:
            with tempfile.TemporaryDirectory(prefix="equai-image-input-") as directory:
                for index, item in enumerate(json.loads(row["input_objects"])):
                    path = Path(directory) / f"{index}.png"
                    await asyncio.to_thread(copy_asset, item, path)
                    content, filename, content_type = await asyncio.to_thread(
                        provider_image_payload, path, index
                    )
                    for attempt in range(2):
                        try:
                            response = await client.post(
                                settings["base"] + "/v1/files",
                                headers={**headers, "X-File-Name": filename, "Content-Type": content_type},
                                content=content,
                            )
                            break
                        except (httpx.ReadError, httpx.ConnectError) as exc:
                            if attempt:
                                raise
                            log.warning("Retrying image file upload for %s after %s", row["id"], type(exc).__name__)
                            await asyncio.sleep(.5)
                    upload_id = provider_data(response).get("id")
                    if not upload_id:
                        raise ValueError("图片上传未返回编号")
                    uploads.append(str(upload_id))
            with connect() as db:
                changed = db.execute("UPDATE merchant_tasks SET status='running',remote_id='image:submitting',started_at=?,progress=20,message='正在提交图片编辑' WHERE id=? AND status='dispatching'", (time.time(), row["id"]))
            if not changed.rowcount:
                return
            response = await client.post(settings["base"] + "/v1/images/tasks", headers={**headers, "Idempotency-Key": row["id"]}, json={
                "model": settings["model"], "prompt": json.loads(row["payload"])["prompt"],
                "aspect_ratio": row["ratio"], "upload_ids": uploads, "image_urls": [], "count": 1,
            })
            value = provider_data(response)
            remote_id = provider_task_id(value)
            if not remote_id:
                raise ValueError("图片服务未返回任务编号")
            with connect() as db:
                db.execute("UPDATE merchant_tasks SET remote_id=?,progress=35,message='图片正在编辑' WHERE id=? AND status='running'", ("image:" + str(remote_id), row["id"]))
    except Exception as exc:
        # Never replay a possibly accepted provider request automatically.
        log.warning("Image edit dispatch failed for %s: %s", row["id"], type(exc).__name__)
        _terminal_failure(row["id"], "图片编辑提交未确认成功，积分已释放；请联系管理员核对后再试", False)


def _public_image_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("图片服务返回了无效的结果地址")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("图片结果地址不能访问内部网络")


async def fetch_image(url: str) -> bytes:
    # Never forward the provider API key to an output CDN.
    async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
        for _ in range(5):
            await asyncio.to_thread(_public_image_url, url)
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                result = bytearray()
                async for chunk in response.aiter_bytes():
                    result.extend(chunk)
                    if len(result) > 64 * 1024**2:
                        raise ValueError("图片结果超过大小限制")
                return bytes(result)
    raise ValueError("图片结果重定向次数过多")


async def poll_image(row: dict) -> None:
    from scheduler import _terminal_failure, _completion_locks

    async with _completion_locks[hash(row["id"]) % len(_completion_locks)]:
        with connect() as db:
            current = db.execute("SELECT status FROM merchant_tasks WHERE id=?", (row["id"],)).fetchone()
        if not current or current["status"] != "running":
            return
        if time.time() - (row["started_at"] or row["created_at"]) > 900:
            _terminal_failure(row["id"], "图片服务处理超时，积分已释放；可联系管理员核对结果", False)
            return
        remote = row["remote_id"].removeprefix("image:")
        if remote == "submitting":
            return
        settings = image_settings()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(settings["base"] + "/v1/images/tasks/" + quote(remote, safe=""), headers={"Authorization": f'Bearer {settings["key"]}'})
        value = provider_data(response)
        status = str(value.get("status", "")).lower()
        if status in {"failed", "cancelled", "canceled"}:
            _terminal_failure(row["id"], "图片编辑未成功，积分已释放；请调整素材或描述后重试", False)
            return
        if status not in {"completed", "succeeded", "success"}:
            return
        data = await fetch_image(str((value.get("result") or {}).get("url") or ""))
        with tempfile.TemporaryDirectory(prefix="equai-image-result-") as directory:
            output = Path(directory) / "edited.png"
            with Image.open(BytesIO(data)) as image:
                if image.width * image.height > 64_000_000:
                    raise ValueError("图片结果尺寸过大")
                image.convert("RGBA" if "A" in image.getbands() else "RGB").save(output, format="PNG")
            artifact = await asyncio.to_thread(store_output, row, output, "image")
        now, expiry = time.time(), time.time() + 72 * 3600
        with transaction() as db:
            changed = db.execute("UPDATE merchant_tasks SET status='completed',progress=100,message='图片编辑完成',artifacts=?,finished_at=?,input_expires_at=?,output_expires_at=? WHERE id=? AND status='running'", (json.dumps([artifact]), now, now + 86400, expiry, row["id"]))
            if not changed.rowcount:
                return
            db.execute("UPDATE assets SET expires_at=? WHERE task_id=? AND role='input'", (now + 86400, row["id"]))
            db.execute("INSERT OR IGNORE INTO assets(id,merchant_id,task_id,role,bucket,object_key,filename,content_type,size_bytes,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (public_id("ast"), row["merchant_id"], row["id"], "output", "local" if artifact.get("storage") == "local" else artifact["bucket"], artifact.get("path") or artifact["key"], "edited.png", "image/png", artifact["size_bytes"], now, expiry))
            settle_task(db, row["id"], "success", note="图片编辑成功")
