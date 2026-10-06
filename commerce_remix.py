"""Depth-guided e-commerce remix preflight and orchestration.

The original clip is copied to a 360px-short-edge working file, read by Qwen/ASR,
and converted to a grayscale depth video. Only that derived video and explicitly
role-mapped pictures are handed to H3.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger("h3.commerce_remix")
RUNNING: dict[str, asyncio.Task] = {}


def _input_url(row: dict, item: dict) -> str:
    """Create the signed provider URL for a stored source or role image."""
    if item.get("storage") == "local":
        import transfer_storage
        base_url = os.getenv("H3_COMMERCE_TRANSFER_BASE_URL", "").strip()
        kwargs = {"base_url": base_url} if base_url else {}
        return transfer_storage.signed_input_url(row["id"], item["id"], expires=24 * 3600, **kwargs)
    import object_storage
    return object_storage.presigned_url(item["key"], expires=24 * 3600)


def ensure(task_id: str) -> None:
    task = RUNNING.get(task_id)
    if task is None or task.done():
        RUNNING[task_id] = asyncio.create_task(_guarded(task_id), name=f"commerce-remix-{task_id}")


async def _guarded(task_id: str) -> None:
    try:
        await _run(task_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Depth-guided commerce remix failed for %s", task_id)
        from scheduler import _terminal_failure
        _terminal_failure(task_id, f"电商复刻失败：{exc}", False)
        _clear_work(task_id)
    finally:
        RUNNING.pop(task_id, None)


def cancel(task_id: str) -> None:
    task = RUNNING.pop(task_id, None)
    if task is not None:
        task.cancel()
    asyncio.create_task(asyncio.to_thread(_clear_work, task_id))


def _clear_work(task_id: str) -> None:
    shutil.rmtree(Path(os.getenv("H3_COMMERCE_REMIX_WORK_DIR", "/data/commerce-remix")) / task_id,
                  ignore_errors=True)


def _set_stage(task_id: str, progress: int, message: str) -> dict:
    from database import connect
    with connect() as db:
        db.execute("UPDATE merchant_tasks SET progress=?,message=? WHERE id=? AND status='running'",
                   (progress, message, task_id))
        row = db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
    if not row or row["status"] != "running":
        raise RuntimeError("任务已取消或状态已结束")
    return dict(row)


def _persist(task_id: str, payload: dict) -> None:
    from database import connect
    with connect() as db:
        db.execute("UPDATE merchant_tasks SET payload=? WHERE id=? AND status='running'",
                   (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), task_id))


def _load(task_id: str) -> dict:
    from database import connect
    with connect() as db:
        row = db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise RuntimeError("任务不存在")
    return dict(row)


def _media_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return float(result.stdout.strip())


def _short_edge(path: Path) -> int:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "json", str(path)], capture_output=True, text=True, check=True, timeout=30,
    )
    streams = json.loads(result.stdout).get("streams") or []
    if not streams:
        raise RuntimeError("原视频没有可读的视频流")
    return min(int(streams[0]["width"]), int(streams[0]["height"]))


def _ensure_360(source: Path, target: Path) -> None:
    """Create the dedicated 360px-short-edge work copy without touching the upload."""
    import reference_video
    if _short_edge(source) > reference_video.SHORT_EDGE:
        if not reference_video.downscale_file(source, target):
            raise RuntimeError("原视频 360p 预处理失败")
    else:
        shutil.copyfile(source, target)
    if _short_edge(target) > reference_video.SHORT_EDGE:
        raise RuntimeError("360p 工作副本校验失败")


def _trim_360(target: Path) -> None:
    """Keep a nominal 15-second upload inside H3's strict duration limit."""
    if _media_duration(target) <= 15.0:
        return
    trimmed = target.with_name("source-360-trim.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(target),
             "-t", "15", "-map", "0:v:0", "-map", "0:a:0?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", str(trimmed)],
            check=True, capture_output=True, timeout=180,
        )
        if not trimmed.is_file() or not 0 < _media_duration(trimmed) <= 15.1:
            raise RuntimeError("15 秒工作视频裁剪失败")
        trimmed.replace(target)
    finally:
        trimmed.unlink(missing_ok=True)


def depth_gray(depth):
    """Map relative inverse-depth values to grayscale, near white and far black."""
    import numpy as np
    values = np.asarray(depth, dtype=np.float32)
    low, high = np.percentile(values, (2, 98))
    if not np.isfinite(low) or not np.isfinite(high) or high - low < 1e-6:
        return np.zeros(values.shape, dtype=np.uint8)
    normalized = np.clip((values - low) / (high - low), 0, 1)
    return np.round(normalized * 255).astype(np.uint8)


def _prompt(analysis: dict, roles: list[str]) -> str:
    names = {"person": "人物", "product": "商品", "scene": "场景"}
    tokens = {"person": "PERSON", "product": "PRODUCT", "scene": "PLACE"}
    original = analysis.get("original") or {}
    picture_no = {role: index for index, role in enumerate(roles, 1)}
    bound_names = {
        role: {
            "person": f"the person shown in <Picture {index}>",
            "product": f"the product shown in <Picture {index}>",
            "scene": f"the environment shown in <Picture {index}>",
        }[role]
        for role, index in picture_no.items()
    }
    depth_definition = (
        "<Video 1> is the 360p grayscale depth-map version of the source video. Near areas are white and far areas are black. "
        "Use it for the original subject's pose and movement, camera framing and movement, shot timing and shot order. "
    )
    if "scene" in picture_no:
        depth_definition += "Do NOT use its background geometry or scene layout as the target environment; the uploaded scene picture replaces that. "
    else:
        depth_definition += "Use its background geometry and scene layout to preserve the original environment because no replacement scene picture was uploaded. "
    depth_definition += "It is NOT a color or identity reference. The original color video is not provided to this generation request. Its original soundtrack is embedded in Video 1 and must be preserved."
    definitions = [
        depth_definition
    ]
    for role in ("person", "product", "scene"):
        token = tokens[role]
        if role in picture_no:
            index = picture_no[role]
            definitions.append(
                f"<Picture {index}> is the user's {names[role]} reference image ({names[role]}参考图 {index}) and the sole appearance authority for {names[role]}. "
                f"In every shot, {token} means {bound_names[role]}; use <Picture {index}> only for {token}. "
                "Recreate this role from that picture throughout the entire video. This picture overrides conflicting descriptions in the source analysis, shot text and audio transcript. "
                "Never swap this image with another role's picture or use it to change any other role."
            )
        else:
            description = str(original.get(role) or "")
            definitions.append(
                f"No {names[role]} picture was uploaded for {token}; preserve the original {names[role]} from the source video and do not replace it or borrow another role's picture. "
                f"Use the source-video description as its appearance reference: {description or 'preserve what is visible in the source video.'}"
            )
    script = []
    for index, shot in enumerate(analysis.get("shots") or [], 1):
        start = float(shot.get("start") or 0)
        end = float(shot.get("end") or analysis.get("duration_seconds") or 0)
        text = str(shot.get("text") or "").strip()
        purpose = str(shot.get("purpose") or "").strip()
        for role, bound_name in bound_names.items():
            token = tokens[role]
            source_names = sorted(
                {str(original.get(role) or "").strip(), str(original.get(role + "_short") or "").strip()} - {""},
                key=len, reverse=True,
            )
            for source_name in source_names:
                source_pattern = re.escape(source_name)
                text = re.sub(rf"\b(?:a|an|the)\s+{source_pattern}\b", bound_name, text, flags=re.I)
                purpose = re.sub(rf"\b(?:a|an|the)\s+{source_pattern}\b", bound_name, purpose, flags=re.I)
                text = re.sub(source_pattern, bound_name, text, flags=re.I)
                purpose = re.sub(source_pattern, bound_name, purpose, flags=re.I)
            text = re.sub(rf"\{{{token}\}}|\b{token}\b", bound_name, text)
            purpose = re.sub(rf"\{{{token}\}}|\b{token}\b", bound_name, purpose)
        script.append(f"[{start:.2f}-{end:.2f}s · 镜头{index} · {purpose}] {text}")
    speech = "\n".join(
        f"{float(item.get('start') or 0):.2f}-{float(item.get('end') or 0):.2f}s: {item.get('text','')}"
        for item in analysis.get("transcript_timeline") or []
    ) or str(analysis.get("transcript") or "未识别到清晰对白")
    # The summary is free-form source-video prose and can repeat original identity, product or
    # location details. Detailed shot actions below already carry the useful timing and blocking.
    summary = "Follow only the shot-by-shot actions and pacing below; source descriptions never override the mapped reference pictures."
    style = "Use camera framing, movement and edit rhythm from the shot descriptions only. Do not copy source color grading or appearance."
    if "scene" in picture_no:
        style += " The scene reference replaces the source environment in every shot."
    return (
        "Reference definitions:\n" + "\n".join(definitions)
        + "\n\n图片编号绑定是固定映射，所有镜头严格执行：PERSON 只对应人物参考图，PRODUCT 只对应商品参考图，PLACE 只对应场景参考图。禁止按图片内容猜测或交换角色。"
        + ("\n场景替换要求：每个镜头都必须用场景参考图 <Picture " + str(picture_no["scene"]) + "> 完整替换原视频中的地点、背景、环境物体、布景和背景光线；不得保留、复现或混合原视频中的背景。只保留原视频的镜头时序、主体动作和摄影机运动，并让新环境适配原镜头。" if "scene" in picture_no else "")
        + ("\n商品动作适配：保留原片展示商品的动作意图，但根据商品参考图 <Picture " + str(picture_no["product"]) + "> 中实际可见的结构调整手部交互；不要复现原商品独有的盖子、开口、配件或外形，不要给新商品增加图片中没有的部件。" if "product" in picture_no else "")
        + "\n\n分镜摘要：\n" + summary
        + "\n\n镜头语言：\n" + style
        + "\n\n按原片顺序、动作和时间执行分镜：\n" + "\n".join(script)
        + "\n\n原声对白及时间戳，按时序保留语义和停顿；对白只用于语音，不得据此改变参考图中的人物、商品或场景外观：\n" + speech
        + "\n\n输出一条连续视频。严格按照上传图片与人物/商品/场景的语义绑定；没有上传图片的类别按源视频描述保留原有视觉身份或环境，不要用其他参考图替换。"
    )


async def _run(task_id: str) -> None:
    from database import connect
    from reenact import providers
    from commerce import copy_asset
    import remix_vision
    import reference_video

    row = _load(task_id)
    payload = json.loads(row["payload"] or "{}")
    options = payload.get("_commerce_remix") or {}
    generation_seconds = min(float(options["source_seconds"]), 15.0)
    objects = json.loads(row["input_objects"] or "[]")
    original = next((item for item in objects if item.get("field") == "videos" and not item.get("commerce_depth_source")), None)
    if not original:
        raise RuntimeError("任务找不到原视频")
    depth_source = next((item for item in objects if item.get("commerce_depth_source")), None)
    role_objects = [item for item in objects if item.get("field") == "images"]
    roles = list(options.get("roles") or [])
    if len(roles) != len(role_objects):
        raise RuntimeError("人物、商品、场景图片与角色信息不匹配")
    work = Path(os.getenv("H3_COMMERCE_REMIX_WORK_DIR", "/data/commerce-remix")) / task_id
    work.mkdir(parents=True, exist_ok=True)
    source_path = work / "original-upload.mp4"
    work_360 = work / "source-360.mp4"

    if not work_360.is_file():
        _set_stage(task_id, 10, "正在制作 360p 工作视频")
        await asyncio.to_thread(copy_asset, original, source_path)
        await asyncio.to_thread(_ensure_360, source_path, work_360)
    else:
        await asyncio.to_thread(copy_asset, original, source_path)
    await asyncio.to_thread(_trim_360, work_360)

    analysis = options.get("analysis")
    analysis_cost = float(options.get("analysis_billing") or 0)
    if not isinstance(analysis, dict):
        _set_stage(task_id, 14, "正在识别多帧画面与对白时间轴")
        source360 = await providers.put(
            row, work_360, "commerce-source-360.mp4", "video/mp4",
            transfer_base_url=os.getenv("H3_COMMERCE_TRANSFER_BASE_URL") or None,
        )
        targets = []
        for role, item in zip(roles, role_objects):
            targets.append({"role": role, "url": _input_url(row, item)})
        analysis, analysis_cost, runtime = await remix_vision.analyse({
            "source": {"name": "commerce-source-360.mp4", "size": source360["size_bytes"], "url": source360["url"]},
            "duration": generation_seconds, "roles": roles, "targets": targets,
            "role_only_references": True,
            "require_asr": bool(options.get("has_audio")),
        })
        asr_runtime = runtime.get("asr") or {}
        if options.get("has_audio") and asr_runtime.get("error"):
            # Losing the transcript costs dialogue fidelity, not the video. The
            # ordinary remix path has always continued here, and failing a paid
            # task because the speech service was unavailable is the worse
            # outcome; the reason is kept on the task for support.
            log.warning("Commerce remix %s continues without a transcript: %s",
                        row["id"], asr_runtime["error"])
            options["asr_error"] = str(asr_runtime["error"])[:200]
        analysis["commerce_depth"] = True
        analysis["remix2"] = True
        options["analysis"] = analysis
        options["analysis_billing"] = analysis_cost
        options["analysis_runtime"] = runtime
        options["depth_source_asset"] = {
            "id": source360["id"], "key": source360["key"],
            "filename": "commerce-source-360.mp4", "content_type": "video/mp4",
            "size_bytes": source360["size_bytes"], "storage": source360["storage"],
            "field": "videos", "commerce_depth_source": True,
        }
        payload["_commerce_remix"] = options
        _persist(task_id, payload)
    if not depth_source:
        depth_source = options.get("depth_source_asset")
        if not isinstance(depth_source, dict):
            source360 = await providers.put(
                row, work_360, "commerce-source-360.mp4", "video/mp4",
                transfer_base_url=os.getenv("H3_COMMERCE_TRANSFER_BASE_URL") or None,
            )
            depth_source = {
                "id": source360["id"], "key": source360["key"],
                "filename": "commerce-source-360.mp4", "content_type": "video/mp4",
                "size_bytes": source360["size_bytes"], "storage": source360["storage"],
                "field": "videos", "commerce_depth_source": True,
            }
            options["depth_source_asset"] = depth_source
            payload["_commerce_remix"] = options
            _persist(task_id, payload)
    generated_inputs = list(objects)
    if not any(item.get("commerce_depth_source") for item in generated_inputs):
        generated_inputs.append(dict(depth_source))
    remix = {
        "roles": roles, "template": "commerce_depth", "audio": "original",
        "source_seconds": generation_seconds, "has_audio": bool(options.get("has_audio")),
        "finalize_original_audio": True, "version": 3, "analysis": analysis,
        "commerce_depth_worker": True,
        "analysis_billing": analysis_cost, "keyframes": [],
        "metrics": {"preflight_completed_at": time.time(), "depth_model": "depth-anything/Depth-Anything-V2-Small-hf",
                    "reference_short_edge": reference_video.SHORT_EDGE},
    }
    payload.pop("_commerce_remix", None)
    payload["_tool"] = "remix"
    payload["_remix"] = remix
    payload["_prompt_compiled"] = True
    payload["prompt"] = _prompt(analysis, roles)
    payload["duration"] = generation_seconds
    payload["video_audio_flags"] = "1" if options.get("has_audio") else "0"
    now = time.time()
    with connect() as db:
        changed = db.execute(
            """UPDATE merchant_tasks SET payload=?,input_objects=?,status='queued',remote_id=NULL,
            provider_started=0,progress=35,message='腾讯云已完成360p与分镜，等待深度处理和生成',queued_at=?
            WHERE id=? AND status='running' AND remote_id='commerce_remix'""",
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
             json.dumps(generated_inputs, ensure_ascii=False, separators=(",", ":")), now, task_id),
        )
    if not changed.rowcount:
        raise RuntimeError("任务状态已改变，未提交生成")
    _clear_work(task_id)
