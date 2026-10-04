"""Paid execution: Image2 pictures, H3 performances, ASR, and project storage.

Every request is idempotent at the node level: the runner records the remote id before
waiting, so a restart polls the same request instead of paying for a new one.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import httpx

MIN_OUTPUT_BYTES = 50 * 1024


class ProviderError(RuntimeError):
    pass


# ── storage ─────────────────────────────────────────────────────────────────────────

def input_key(row: dict, name: str) -> str:
    prefix = os.getenv("COS_INPUT_PREFIX", "inputs").strip("/")
    return f"{prefix}/{row['merchant_id']}/{row['id']}/reenact/{name}"


async def put(
    row: dict, path: Path, name: str, content_type: str,
    *, transfer_base_url: str | None = None,
) -> dict:
    """Store an intermediate file on the active transfer backend."""
    from database import connect, public_id
    import transfer_storage

    asset_id = public_id("ast")
    if transfer_storage.backend() == "local":
        with path.open("rb") as handle:
            key, size = await asyncio.to_thread(
                transfer_storage.store_input, handle, row["merchant_id"], row["id"],
                abs(hash(name)) % 10000, name,
            )
        bucket = "local-input"
        url_kwargs = {"base_url": transfer_base_url} if transfer_base_url else {}
        url = transfer_storage.signed_input_url(
            row["id"], asset_id, expires=24 * 3600, **url_kwargs,
        )
    else:
        import object_storage
        key = input_key(row, name)
        await asyncio.to_thread(object_storage.upload_path, path, key, content_type)
        size = path.stat().st_size
        bucket = object_storage.bucket_name()
        url = object_storage.presigned_url(key, expires=24 * 3600)
    with connect() as db:
        db.execute(
            """INSERT OR IGNORE INTO assets
            (id,merchant_id,task_id,role,bucket,object_key,filename,content_type,size_bytes,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (asset_id, row["merchant_id"], row["id"], "input", bucket, key,
             name, content_type, size, time.time()),
        )
    return {"id": asset_id, "key": key, "name": name, "content_type": content_type,
            "size_bytes": size, "storage": "local" if bucket == "local-input" else "cos",
            "url": url}


def url_for(stored: dict, expires: int = 24 * 3600) -> str:
    if stored.get("url"):
        return stored["url"]
    import object_storage
    return object_storage.presigned_url(stored["key"], expires=expires)


async def fetch(stored: dict, target: Path) -> Path:
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        if stored.get("storage") == "local":
            import transfer_storage
            await asyncio.to_thread(shutil.copyfile, transfer_storage.resolve_input(stored["key"]), target)
        else:
            import object_storage
            await asyncio.to_thread(object_storage.download_path, stored["key"], target)
    return target


async def fetch_input(row: dict, item: dict, target: Path) -> Path:
    from commerce import copy_asset
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(copy_asset, item, target)
    return target


def input_url(row: dict, item: dict) -> str:
    from scheduler import _input_url
    return _input_url(row, item)


# ── Image2 ──────────────────────────────────────────────────────────────────────────

async def image_submit(prompt: str, ratio: str, refs: list[Path], idempotency: str) -> str:
    from commerce import _provider_upload, image_settings, provider_data, provider_task_id
    from .kits import IMAGE_PROMPT_LIMIT, clip

    if len(prompt) > IMAGE_PROMPT_LIMIT:  # the service rejects prompts over 5000 characters
        prompt = clip(prompt, IMAGE_PROMPT_LIMIT)

    settings = image_settings()
    if not settings["key"]:
        raise ProviderError("图片生成接口未配置")
    headers = {"Authorization": f"Bearer {settings['key']}"}
    last = ""
    # The image service rate-limits bursts (429); parallel candidates and tasks back off and retry.
    for attempt in range(10):  # about nine minutes of patience before a picture gives up
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                uploads = [await _provider_upload(client, headers, path, i) for i, path in enumerate(refs, 1)]
                response = await client.post(
                    settings["base"] + "/v1/images/tasks",
                    headers={**headers, "Idempotency-Key": idempotency[:120]},
                    json={"model": settings["model"], "prompt": prompt, "aspect_ratio": ratio,
                          "upload_ids": uploads, "image_urls": [], "count": 1},
                )
                if response.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {response.status_code}"
                    await asyncio.sleep(min(90, 5 * 2 ** attempt))
                    continue
                remote = provider_task_id(provider_data(response))
        except (httpx.HTTPError, ValueError) as exc:
            last = str(exc)
            if "429" in last or isinstance(exc, httpx.HTTPError):
                await asyncio.sleep(min(90, 5 * 2 ** attempt))
                continue
            raise ProviderError(f"图片服务请求失败：{last}") from exc
        if not remote:
            raise ProviderError("图片服务未返回任务编号")
        return remote
    raise ProviderError(f"图片服务繁忙，多次重试仍失败（{last}）")


async def image_wait(remote: str, timeout: float = 900) -> tuple[bytes, dict]:
    from commerce import fetch_image, image_settings, provider_data

    settings = image_settings()
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30) as client:
        while time.monotonic() - started < timeout:
            try:
                response = await client.get(
                    settings["base"] + "/v1/images/tasks/" + quote(remote, safe=""),
                    headers={"Authorization": f"Bearer {settings['key']}"},
                )
                value = provider_data(response)
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(5)
                continue
            status = str(value.get("status", "")).lower()
            if status in {"failed", "cancelled", "canceled"}:
                raise ProviderError(f"图片生成失败: {value.get('error') or value.get('message') or status}")
            if status in {"completed", "succeeded", "success"}:
                url = str((value.get("result") or {}).get("url") or "")
                if not url:
                    raise ProviderError("图片服务没有返回图片")
                return await fetch_image(url), {"cost": value.get("billing", value.get("cost"))}
            await asyncio.sleep(4)
    raise ProviderError("图片生成超时")


# ── H3 performance ──────────────────────────────────────────────────────────────────

async def h3_submit(row: dict, job_id: str, prompt: str, duration: int, ratio: str, seed: int,
                    images: list[str], audios: list[str], videos: list[str] | None = None) -> str:
    import json
    import gongji

    payload = json.loads(row["payload"] or "{}")
    worker_input = {k: v for k, v in payload.items() if not k.startswith("_") and k not in {"media", "video_audio_flags"}}
    media = [{"field": "images", "filename": f"picture-{i}.png", "url": url} for i, url in enumerate(images, 1)]
    media += [{"field": "videos", "filename": f"motion-{i}.mp4", "url": url} for i, url in enumerate(videos or [], 1)]
    media += [{"field": "audios", "filename": f"voice-{i}.wav", "url": url} for i, url in enumerate(audios, 1)]
    worker_input.update({
        "mode": "reference", "tier": row["quality"], "duration": int(duration), "ratio": ratio,
        "prompt": prompt, "seed": int(seed), "video_audio_flags": ",".join("0" for _ in videos or []),
        "media": media,
    })
    remote = await gongji.create_job(job_id, row["merchant_id"], worker_input, row["quality"],
                                     payload.get("_transfer_backend") or "cos",
                                     workload="remix" if videos else "video")
    return str(remote)


async def h3_wait(row: dict, job_id: str, remote: str, timeout: float = 3600) -> tuple[dict, float]:
    """Wait for one H3 job; returns (artifact, provider billing)."""
    import gongji
    from local_storage import list_task_videos
    from object_storage import bucket_name, list_objects

    started = time.monotonic()
    info: dict = {}
    while time.monotonic() - started < timeout:
        try:
            info = await gongji.job_status(remote)
        except Exception:  # noqa: BLE001 - provider status is retried
            await asyncio.sleep(10)
            continue
        if info["status"] in {"queued", "running"}:
            await asyncio.sleep(10)
            continue
        break
    else:
        try:
            await gongji.stop_job(remote)
        except Exception:  # noqa: BLE001
            pass
        raise ProviderError("H3 生成超时")
    billing = float(info.get("billing") or 0)
    for _ in range(18):
        artifacts = await asyncio.to_thread(list_task_videos, row["merchant_id"], job_id, MIN_OUTPUT_BYTES)
        if artifacts:
            return artifacts[0], billing
        prefix = f"{os.getenv('COS_OUTPUT_PREFIX', 'outputs').strip('/')}/{row['merchant_id']}/{job_id}/"
        objects = [o for o in await asyncio.to_thread(list_objects, prefix)
                   if str(o.get("key", "")).endswith(".mp4") and int(o.get("size") or 0) >= MIN_OUTPUT_BYTES]
        if objects:
            item = max(objects, key=lambda o: int(o.get("size") or 0))
            return {"name": item["key"].rsplit("/", 1)[-1], "bucket": bucket_name(), "key": item["key"],
                    "media": "video", "size_bytes": int(item["size"])}, billing
        if info.get("status") == "failed":
            break
        await asyncio.sleep(10)
    from object_storage import read_json, relay_failed
    try:
        report = read_json(f"{os.getenv('COS_OUTPUT_PREFIX', 'outputs').strip('/')}/{row['merchant_id']}/{job_id}/_job_result.json")
    except Exception:  # noqa: BLE001 - the report is a diagnosis, not the result
        report = None
    detail = str((report or {}).get("error") or "")
    if detail:
        relay_failed(detail)
    raise ProviderError(f"H3 任务结束但没有输出（{info.get('reason') or info.get('status')}）" + (f"：{detail[:300]}" if detail else ""))


async def h3_stop(remote: str) -> None:
    import gongji
    try:
        await gongji.stop_job(remote)
    except Exception:  # noqa: BLE001
        pass


def artifact_url(artifact: dict, task_id: str | None = None, *, expires: int = 3600) -> str:
    """Return a provider-readable URL for a generated artifact.

    Local transfer/output storage is private.  Returning the filesystem path
    (the old behavior) works for local ffmpeg inspection but is not a valid
    ``file_url`` for DashScope ASR or a remote QA provider.  Use the normal
    task-scoped signed download URL when the artifact is local.
    """
    import object_storage
    if artifact.get("storage") == "local":
        if not task_id:
            raise ProviderError("本地视频缺少任务编号，无法生成签名访问地址")
        from local_storage import signed_url
        return signed_url(
            task_id,
            artifact["path"],
            artifact.get("name") or Path(artifact["path"]).name,
            expires=min(int(expires), 3600),
        )
    return object_storage.presigned_url(artifact["key"], expires=expires)


async def fetch_artifact(artifact: dict, target: Path) -> Path:
    from commerce import copy_asset
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(copy_asset, artifact, target, output=True)
    return target


# ── ASR ─────────────────────────────────────────────────────────────────────────────

async def transcribe(url: str) -> dict:
    import asr_service
    result = await asr_service.transcribe(url)
    output = result.get("output") or {}
    data = output.get("transcript_data")
    if isinstance(data, dict):
        return data
    raise ProviderError("语音转写没有返回逐词结果")
