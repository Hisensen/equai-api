"""Merchant task queue, Gongji dispatch, billing settlement, and storage cleanup."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import gongji
import asr_service
import prompt_service
import reference_video
import remix_hypit
import remix_vision
import video_projects
from database import DB, MAX_MERCHANT_CONCURRENCY, connect, public_id, settle_task, transaction
from local_storage import capacity_ok as local_output_capacity_ok
from local_storage import enabled as local_outputs_enabled
from local_storage import list_task_media, list_task_videos, store_cos_video
from local_storage import remove as remove_local_output
from object_storage import (
    bucket_name,
    delete_keys,
    list_objects,
    presigned_url,
    read_json,
    upload_path,
)
from transfer_api import local_worker_result
from transfer_storage import capacity_ok as local_transfer_capacity_ok
from transfer_storage import remove_input as remove_local_input
from transfer_storage import remove_result as remove_local_result
from transfer_storage import signed_input_url

log = logging.getLogger("h3.scheduler")
MAX_CONCURRENCY = int(os.getenv("H3_MAX_CONCURRENCY", "20"))
DISPATCH_PARALLELISM = min(
    MAX_CONCURRENCY, max(1, int(os.getenv("H3_DISPATCH_PARALLELISM", "6")))
)
REMIX_MAX_CONCURRENCY = max(1, int(os.getenv("H3_REMIX_MAX_CONCURRENCY", "4")))
# Reenactments run for many minutes and throttle their own GPU jobs per user.
REENACT_MAX_CONCURRENCY = max(1, int(os.getenv("H3_REENACT_MAX_CONCURRENCY", "10")))
MUSIC_MAX_CONCURRENCY = max(1, int(os.getenv("H3_MUSIC_MAX_CONCURRENCY", "1")))
VOICE_CLONE_MAX_CONCURRENCY = max(1, int(os.getenv("H3_VOICE_CLONE_MAX_CONCURRENCY", "1")))
REMIX_DISPATCH_PARALLELISM = min(
    REMIX_MAX_CONCURRENCY,
    max(1, int(os.getenv("H3_REMIX_DISPATCH_PARALLELISM", "2"))),
)
POLL_PARALLELISM = max(1, int(os.getenv("H3_POLL_PARALLELISM", "12")))
DISPATCH_INTERVAL = float(os.getenv("H3_DISPATCH_INTERVAL", "3"))
POLL_INTERVAL = float(os.getenv("H3_POLL_INTERVAL", "15"))
CLEANUP_INTERVAL = float(os.getenv("H3_CLEANUP_INTERVAL", "600"))
MAX_TASK_SECONDS = int(os.getenv("H3_MAX_TASK_SECONDS", "2700"))
INPUT_RETENTION = int(os.getenv("H3_INPUT_RETENTION_SECONDS", "86400"))
OUTPUT_RETENTION = int(os.getenv("H3_OUTPUT_RETENTION_SECONDS", "172800"))
OUTPUT_PREFIX = os.getenv("COS_OUTPUT_PREFIX", "outputs").strip("/")
MIN_OUTPUT_BYTES = int(os.getenv("H3_MIN_OUTPUT_BYTES", str(64 * 1024)))
MIN_VOICE_CLONE_OUTPUT_BYTES = 2 * 1024
BACKUP_INTERVAL = int(os.getenv("H3_BACKUP_INTERVAL", "86400"))


def recover_interrupted_claims() -> None:
    with connect() as db:
        db.execute(
            """UPDATE merchant_tasks SET status='queued',message='服务重启后重新排队'
            WHERE status='dispatching' AND remote_id IS NULL"""
        )


def _queue_for(value: str | None) -> str:
    """standard / remix / reenact: each queue has its own running-task limit, so long
    reenactments never hold the e-commerce remix queue's slots."""
    try:
        tool = json.loads(value or "{}").get("_tool")
    except (TypeError, json.JSONDecodeError):
        return "standard"
    if tool == "commerce_remix":
        return "remix"
    return tool if tool in {"remix", "reenact", "music", "voice_clone"} else "standard"


def _is_remix_payload(value: str | None) -> bool:
    try:
        return json.loads(value or "{}").get("_tool") == "remix"
    except (TypeError, json.JSONDecodeError):
        return False


def _is_direct_remix_payload(payload: dict) -> bool:
    return payload.get("_tool") == "remix" and bool(
        (payload.get("_remix") or {}).get("direct")
    )


def _needs_remix_finalize(payload: dict) -> bool:
    options = payload.get("_remix") or {}
    return payload.get("_tool") == "remix" and bool(options.get("finalize_original_audio"))


def _reference_has_video(row: dict, payload: dict) -> bool:
    """Video-conditioned Ref2VA needs the 48G remix pool even on /v1/videos."""
    if payload.get("kind") != "reference" and row.get("kind") != "reference":
        return False
    try:
        objects = json.loads(row.get("input_objects") or "[]")
    except (TypeError, json.JSONDecodeError):
        return False
    return any(item.get("field") == "videos" for item in objects if isinstance(item, dict))


def _workload_for(row: dict, payload: dict) -> str:
    if payload.get("_tool") == "remix" or _reference_has_video(row, payload):
        return "remix"
    return "video"


def _claim_next(queue: str = "standard"):
    if queue not in {"standard", "remix", "reenact", "music", "voice_clone"}:
        raise ValueError(f"unknown scheduler queue: {queue}")
    queue_limit = {"remix": REMIX_MAX_CONCURRENCY, "reenact": REENACT_MAX_CONCURRENCY, "music": MUSIC_MAX_CONCURRENCY,
                   "voice_clone": VOICE_CLONE_MAX_CONCURRENCY}.get(queue, MAX_CONCURRENCY)
    if not local_output_capacity_ok() or not local_transfer_capacity_ok():
        log.error("Local transfer/video storage is below the configured free-space reserve")
        return None
    claim_status = "music_queued" if queue == "music" else "queued"
    dispatch_status = "music_dispatching" if queue == "music" else "dispatching"
    with transaction() as db:
        running_rows = db.execute(
            "SELECT payload FROM merchant_tasks WHERE status IN ('dispatching','running','music_dispatching','music_running')"
        ).fetchall()
        running = sum(_queue_for(item["payload"]) == queue for item in running_rows)
        if running >= queue_limit:
            return None
        rows = db.execute(
            """SELECT t.* FROM merchant_tasks t JOIN merchants m ON m.id=t.merchant_id
            WHERE t.status=? AND m.status='active' AND t.queued_at<=?
            ORDER BY t.queued_at ASC LIMIT 50""",
            (claim_status, time.time()),
        ).fetchall()
        for row in rows:
            if _queue_for(row["payload"]) != queue:
                continue
            owner_rows = db.execute(
                "SELECT payload FROM merchant_tasks WHERE merchant_id=? AND status IN ('dispatching','running','music_dispatching','music_running')",
                (row["merchant_id"],),
            ).fetchall()
            # Account concurrency is shared across both resource pools. A
            # merchant with max_concurrency=10 may run any mix of normal and
            # remix jobs, but never more than ten dispatching/running jobs in
            # total.
            owner_running = len(owner_rows)
            account = db.execute("SELECT max_concurrency FROM merchants WHERE id=?", (row["merchant_id"],)).fetchone()
            if owner_running >= min(int(account["max_concurrency"]), MAX_MERCHANT_CONCURRENCY):
                continue
            dispatch_message = "正在提交声音任务" if queue == "voice_clone" else "正在整理生成描述"
            changed = db.execute(
                """UPDATE merchant_tasks SET status=?,progress=10,message=?
                WHERE id=? AND status=?""",
                (dispatch_status, dispatch_message, row["id"], claim_status),
            )
            if changed.rowcount:
                return dict(row)
    return None


def _worker_input(row: dict) -> dict:
    payload = json.loads(row["payload"])
    is_remix = payload.get("_tool") == "remix" and not _is_direct_remix_payload(payload)
    options = payload.get("_remix") or {}
    commerce_depth_worker = bool(options.get("commerce_depth_worker"))
    # Remix 2 is deliberately image-only at generation time. Commerce remix
    # sends only its Tencent 360p working copy to a one-shot worker preflight;
    # that worker replaces it with depth video before calling the H3 graph.
    edits_source = (
        is_remix
        and options.get("version") not in {2, 3}
        and remix_hypit.is_hypit_analysis(options.get("analysis"))
    )
    if edits_source:
        # `[video editing]` prompt: <Video 1> is the edited source and <Audio 1> its soundtrack.
        # finalize_remix still lays the exact original audio over the result afterwards.
        payload["video_audio_flags"] = "1"
    payload.pop("_transfer_backend", None)
    for private_key in (
        "_original_prompt", "_prompt_compiled", "_prompt_mode", "_prompt_provider_used",
        "_tool", "_remix", "_shrink_reference", "_direction", "_prompt_merge_reason",
        "_reenact",
    ):
        payload.pop(private_key, None)
    if commerce_depth_worker:
        # The original upload remains attached to the merchant task so that
        # finalization can preserve its exact soundtrack. H3 receives only the
        # Tencent-generated 360p work copy, which its isolated worker converts
        # to a grayscale depth map before submitting the multi-reference graph.
        payload["_commerce_depth_worker"] = True
        # The public commerce form is a video workflow, but the transformed
        # depth clip enters H3's multi-reference graph (Video 1 + role images).
        # Keep the latent-HD mode for high-resolution tiers so the worker runs
        # its refine ladder after the commerce depth preflight.
        payload["mode"] = (
            "reference_latent_hd"
            if payload.get("tier") in {"1080p", "2k"}
            else "reference"
        )
    inputs = json.loads(row["input_objects"] or "[]")
    media = []
    for item in inputs:
        if commerce_depth_worker and item.get("field") == "videos" and not item.get("commerce_depth_source"):
            continue
        # Qwen-VL consumes the source video during the analysis stage. H3 is a
        # separate image-only Ref2VA stage and must never receive that video.
        if is_remix and not edits_source and options.get("version") != 3 and item.get("field") == "videos":
            continue
        commerce_base_url = os.getenv("H3_COMMERCE_TRANSFER_BASE_URL", "").strip() if commerce_depth_worker else ""
        transfer_ip = os.getenv("H3_TRANSFER_HOST_IP", "").strip()
        # The GPU is pinned to this IP for transfers. Its TLS certificate
        # covers the IP, not the public hostname; using that hostname in a
        # signed media URL fails before the worker can download the image.
        worker_base_url = commerce_base_url or (f"https://{transfer_ip}" if transfer_ip else "")
        url = _input_url(row, item, base_url=worker_base_url or None)
        media.append({"field": item["field"], "filename": item["filename"], "url": url})
    payload["media"] = media
    return payload


def _input_url(row: dict, item: dict, *, base_url: str | None = None) -> str:
    if item.get("storage") == "local":
        if base_url:
            return signed_input_url(row["id"], item["id"], expires=24 * 3600, base_url=base_url)
        return signed_input_url(row["id"], item["id"], expires=24 * 3600)
    return presigned_url(item["key"], expires=24 * 3600)


def _remix_needs_analysis(payload: dict) -> bool:
    return payload.get("_tool") == "remix" and not _is_direct_remix_payload(payload) and not isinstance(
        (payload.get("_remix") or {}).get("analysis"), dict
    )


def _remix_needs_keyframes(payload: dict) -> bool:
    options = payload.get("_remix") or {}
    if options.get("version") == 2:
        return (
            payload.get("_tool") == "remix"
            and not _is_direct_remix_payload(payload)
            and isinstance(options.get("analysis"), dict)
            and not isinstance(options.get("keyframes"), list)
        )
    plan = options.get("asset_plan")
    if options.get("editor_mode") == "generic" and isinstance(plan, dict):
        if not bool(plan.get("need_generated_images")):
            return False
    return (
        payload.get("_tool") == "remix"
        and not _is_direct_remix_payload(payload)
        and isinstance(options.get("analysis"), dict)
        and (not remix_hypit.is_hypit_analysis(options.get("analysis"))
             or options.get("editor_mode") == "generic")
        and not isinstance(options.get("keyframes"), list)
    )


async def _plan_editor_assets(row: dict, payload: dict) -> dict:
    options = payload.get("_remix") or {}
    if options.get("editor_mode") != "generic" or isinstance(options.get("asset_plan"), dict):
        return payload
    inputs = json.loads(row["input_objects"] or "[]")
    uploaded_count = sum(1 for item in inputs if item.get("field") == "images")
    plan = await prompt_service.plan_editor_assets(
        str(payload.get("_original_prompt") or payload.get("prompt") or ""),
        options.get("analysis") or {}, uploaded_count,
    )
    options["asset_plan"] = plan
    options.setdefault("metrics", {})["asset_planner_provider_used"] = bool(plan.get("provider_used"))
    payload["_remix"] = options
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with connect() as db:
        db.execute("UPDATE merchant_tasks SET payload=? WHERE id=? AND status='dispatching'", (encoded, row["id"]))
    return payload


def _remix_keyframes_running(payload: dict) -> bool:
    options = payload.get("_remix") or {}
    return (
        payload.get("_tool") == "remix"
        and isinstance(options.get("keyframe_tasks"), list)
        and not isinstance(options.get("keyframes"), list)
    )


def _analysis_billing(row: dict) -> float:
    try:
        payload = json.loads(row["payload"] or "{}")
        return float((payload.get("_remix") or {}).get("analysis_billing") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0.0


def _worker_report(row: dict) -> dict | None:
    report = local_worker_result(row["merchant_id"], row["id"])
    if report is not None:
        return report
    report_key = f'{OUTPUT_PREFIX}/{row["merchant_id"]}/{row["id"]}/_job_result.json'
    return read_json(report_key)


def _queue_analyzed_remix(row: dict, payload: dict, analysis: dict,
                          billing: float, now: float, from_status: str = "running") -> bool:
    if not isinstance(analysis.get("shots"), list) or not analysis["shots"]:
        return False
    options = payload["_remix"]
    options["analysis"] = analysis
    options["analysis_billing"] = billing
    options["analysis_remote_id"] = str(row.get("remote_id") or "")
    metrics = options.setdefault("metrics", {})
    metrics["analysis_completed_at"] = now
    metrics["analysis_seconds"] = round(
        now - float(metrics.get("analysis_started_at") or now), 3
    )
    metrics["analysis_platform_cost"] = billing
    payload["_remix"] = options
    with connect() as db:
        changed = db.execute(
            """UPDATE merchant_tasks SET status='queued',remote_id=NULL,provider_started=0,
            started_at=NULL,progress=35,message='正在准备复刻素材',
            payload=?,provider_billing=?,queued_at=? WHERE id=? AND status=?""",
            (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                billing, now, row["id"], from_status,
            ),
            )
        project = payload.get("_project") or {}
        if project.get("project_id"):
            video_projects.update_analysis(db, str(project["project_id"]), analysis)
    return bool(changed.rowcount)


async def _compile_task_prompt(row: dict) -> dict:
    payload = json.loads(row["payload"] or "{}")
    if payload.get("_prompt_compiled"):
        return row
    if payload.get("_tool") == "digital_human":
        import digital_human_prompt
        merged = await digital_human_prompt.merge_direction(
            digital_human_prompt.base_prompt(), str(payload.get("_direction") or ""),
            merchant_id=row["merchant_id"],
        )
        result = {"prompt": merged["prompt"], "mode": "Ref2VA",
                  "provider_used": merged["provider_used"]}
        if not merged["provider_used"]:
            log.info("Digital-human prompt merge fell back: %s", merged.get("reason"))
        payload["_prompt_merge_reason"] = str(merged.get("reason") or "")[:200]
        return _store_compiled_prompt(row, payload, result)
    inputs = json.loads(row["input_objects"] or "[]")
    if payload.get("_tool") == "remix" and not _is_direct_remix_payload(payload):
        options = payload["_remix"]
        result = await prompt_service.compile_remix_prompt(
            str(payload.get("prompt") or ""), float(row["duration"]),
            row["ratio"], inputs, options["roles"], options["audio"],
            options["analysis"], options.get("keyframes") or [],
            generic_editor=options.get("editor_mode") == "generic",
            merchant_id=row["merchant_id"],
        )
    else:
        result = await prompt_service.compile_prompt(
            kind=row["kind"], brief=str(payload.get("prompt") or ""),
            duration=float(row["duration"]), ratio=row["ratio"], inputs=inputs,
            merchant_id=row["merchant_id"],
        )
    return _store_compiled_prompt(row, payload, result)


def _store_compiled_prompt(row: dict, payload: dict, result: dict) -> dict:
    payload["_original_prompt"] = str(payload.get("prompt") or "")
    payload["prompt"] = result["prompt"]
    payload["_prompt_compiled"] = True
    payload["_prompt_mode"] = result["mode"]
    payload["_prompt_provider_used"] = bool(result["provider_used"])
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with connect() as db:
        changed = db.execute(
            """UPDATE merchant_tasks SET payload=?,progress=15,message='正在申请 GPU'
            WHERE id=? AND status='dispatching'""",
            (encoded, row["id"]),
        )
    if not changed.rowcount:
        raise prompt_service.PromptServiceError("任务状态已经发生变化")
    return {**row, "payload": encoded}


def _requeue_prompt_retry(row: dict, error: prompt_service.PromptServiceError) -> bool:
    """Keep required prompt integration in front of GPU dispatch, retrying transient provider outages."""
    payload = json.loads(row["payload"] or "{}")
    attempt = int(payload.get("_prompt_retry_count") or 0) + 1
    payload["_prompt_retry_count"] = attempt
    payload.setdefault("_prompt_retry_started_at", time.time())
    delay = min(300, 15 * (2 ** min(attempt - 1, 5)))
    now = time.time()
    message = "提示词服务繁忙，任务已排队并将自动重试"
    with connect() as db:
        changed = db.execute(
            """UPDATE merchant_tasks SET status='queued',progress=0,message=?,payload=?,
            queued_at=?,error='' WHERE id=? AND status='dispatching' AND remote_id IS NULL""",
            (
                message,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                now + delay,
                row["id"],
            ),
        )
    if changed.rowcount:
        log.warning(
            "Prompt provider temporarily unavailable for %s; retry %s in %ss: %s",
            row["id"], attempt, delay, str(error)[:240],
        )
        return True
    return False


def _transfer_backend(row) -> str:
    try:
        return str(json.loads(row["payload"] or "{}").get("_transfer_backend") or "cos")
    except (TypeError, json.JSONDecodeError):
        return "cos"


def _terminal_failure(task_id: str, error: str, provider_started: bool, billing: float = 0) -> None:
    now = time.time()
    with transaction() as db:
        row = db.execute("SELECT status FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row["status"] not in {"queued", "music_queued", "music_dispatching", "music_running", "dispatching", "running"}:
            return
        db.execute(
            """UPDATE merchant_tasks SET status='failed',progress=100,message='任务失败',error=?,
            provider_billing=?,finished_at=?,input_expires_at=? WHERE id=?""",
            (error[-3000:], billing, now, now + INPUT_RETENTION, task_id),
        )
        db.execute(
            "UPDATE assets SET expires_at=? WHERE task_id=? AND role='input' AND expires_at IS NULL",
            (now + INPUT_RETENTION, task_id),
        )
        settle_task(
            db, task_id, "failure_after_start" if provider_started else "dispatch_failure",
            note=error[-500:],
        )
        video_projects.mark_run(db, task_id, status="failed", result={"error": error[-3000:]})


async def _dispatch_once(queue: str = "standard") -> bool:
    row = _claim_next(queue)
    if row is None:
        return False
    if queue == "voice_clone":
        try:
            payload = json.loads(row["payload"] or "{}")
            inputs = json.loads(row["input_objects"] or "[]")
            transfer_ip = os.getenv("H3_TRANSFER_HOST_IP", "").strip()
            worker_base = f"https://{transfer_ip}" if transfer_ip else None
            media = [
                {"field": item["field"], "filename": item["filename"],
                 "url": _input_url(row, item, base_url=worker_base)}
                for item in inputs
            ]
            worker_input = {
                "text": payload["text"],
                "emotion_weight": payload.get("emotion_weight", 1.0),
                "media": media,
                "_output_names": ["speech.mp3"],
            }
            task_id = await gongji.create_voice_clone_job(
                row["id"], row["merchant_id"], worker_input,
            )
        except Exception as exc:
            log.exception("Voice clone dispatch failed for %s", row["id"])
            _terminal_failure(row["id"], f"声音克隆任务投递失败: {exc}", False)
            return True
        with connect() as db:
            db.execute(
                """UPDATE merchant_tasks SET status='running',remote_id=?,provider_started=1,
                started_at=COALESCE(started_at,?),progress=20,message='声音生成中'
                WHERE id=? AND status='dispatching'""",
                (str(task_id), time.time(), row["id"]),
            )
        return True
    if queue == "music":
        try:
            payload = json.loads(row["payload"] or "{}")
            instrumental = (
                str(payload.get("voice") or "") == "无人声"
                or str(payload.get("style") or "") == "纯器乐"
            )
            if instrumental and not str(payload.get("lyrics") or "").strip():
                payload["lyrics"] = "[Instrumental]"
            if payload.get("music_stage") == "lyrics_review":
                if not asr_service.configured():
                    _terminal_failure(row["id"], "歌词识别服务暂不可用，请重试或稍后再试", False)
                    return True
                source = next((item for item in json.loads(row["input_objects"] or "[]") if item.get("field") == "audios"), None)
                if not source:
                    _terminal_failure(row["id"], "音乐源文件不存在", False)
                    return True
                with connect() as db:
                    db.execute("UPDATE merchant_tasks SET progress=10,message='正在识别歌词' WHERE id=?", (row["id"],))
                try:
                    # Music ASR fetches local Tencent media via the origin IP,
                    # bypassing the edge proxy that intermittently rejects
                    # DashScope's file fetch. Keep this override scoped to
                    # music; all video ASR keeps its existing public URL.
                    transcript = await asr_service.transcribe(
                        _input_url(row, source),
                        file_url_base=os.getenv("H3_MUSIC_ASR_FILE_URL_BASE", ""),
                    )
                    payload["lyrics"] = asr_service.transcript_text(transcript)
                    if not payload["lyrics"].strip():
                        raise RuntimeError("没有识别到可用歌词")
                    payload["music_stage"] = "generate"
                    with transaction() as db:
                        db.execute(
                            "UPDATE merchant_tasks SET payload=?,progress=15,message='歌词已识别，正在开始生成' WHERE id=? AND status='music_dispatching'",
                            (json.dumps(payload, ensure_ascii=False), row["id"]),
                        )
                except Exception as exc:
                    _terminal_failure(row["id"], f"歌词识别失败: {exc}", False)
                    return True
            if not str(payload.get("lyrics") or "").strip():
                _terminal_failure(row["id"], "请填写歌词，或选择无人声/纯器乐", False)
                return True
            worker_input = dict(payload)
            worker_input.pop("_tool", None)
            worker_input["job_id"] = row["id"]
            worker_input["merchant_id"] = row["merchant_id"]
            worker_input["output_targets"] = []
            worker_input["media"] = [
                {"field": item["field"], "filename": item["filename"], "url": _input_url(row, item)}
                for item in json.loads(row["input_objects"] or "[]")
            ]
            task_id = await gongji.create_music_job(row["id"], row["merchant_id"], worker_input, _transfer_backend(row))
        except Exception as exc:
            log.exception("Music dispatch failed for %s", row["id"])
            _terminal_failure(row["id"], f"音乐任务投递失败: {exc}", False)
            return True
        with connect() as db:
            db.execute(
                "UPDATE merchant_tasks SET status='music_running',remote_id=?,provider_started=1,started_at=COALESCE(started_at,?),progress=20,message='音乐生成中' WHERE id=? AND status='music_dispatching'",
                (str(task_id), time.time(), row["id"]),
            )
        return True
    if row["kind"] == "image_edit":
        from commerce import dispatch_image
        await dispatch_image(row)
        return True
    payload = json.loads(row["payload"] or "{}")
    if payload.get("_tool") == "commerce_remix":
        with connect() as db:
            changed = db.execute(
                """UPDATE merchant_tasks SET status='running',remote_id='commerce_remix',
                provider_started=0,started_at=COALESCE(started_at,?),progress=8,message='正在准备360p视频'
                WHERE id=? AND status='dispatching'""",
                (time.time(), row["id"]),
            )
        if changed.rowcount:
            from commerce_remix import ensure
            ensure(row["id"])
        return True
    if payload.get("_shrink_reference"):
        # H3 bills by the reference video's pixel count, so it is shrunk to a
        # 360 short edge here rather than in create_video. Never dispatch the
        # original full-resolution input: a failed preflight must fail the task.
        try:
            await reference_video.shrink_inputs(json.loads(row["input_objects"] or "[]"))
        except Exception as exc:  # noqa: BLE001
            log.exception("Reference shrink failed for %s", row["id"])
            _terminal_failure(row["id"], f"视频360预处理失败: {exc}", False)
            return True
    if payload.get("_tool") == "reenact":
        from reenact import runner as reenact_runner
        with connect() as db:
            changed = db.execute(
                """UPDATE merchant_tasks SET status='running',remote_id='reenact',provider_started=1,
                started_at=COALESCE(started_at,?),progress=6,message='视频重演已开始'
                WHERE id=? AND status='dispatching'""",
                (time.time(), row["id"]),
            )
        if changed.rowcount:
            reenact_runner.ensure(row["id"])
        return True
    if _remix_needs_analysis(payload):
        source = next(
            (item for item in json.loads(row["input_objects"] or "[]")
             if item.get("field") == "videos"),
            None,
        )
        if source is None:
            _terminal_failure(row["id"], "视频复刻缺少原视频", False)
            return True
        analysis_input = {
            "source": {
                "name": source["filename"],
                "size": int(source.get("size_bytes") or 0),
                "url": _input_url(row, source),
            },
            # Qwen samples the real source timeline. H3 still receives the
            # separately rounded 5-15 second target duration later.
            "duration": float(payload["_remix"].get("source_seconds") or row["duration"]),
            "target_duration": float(row["duration"]),
            "ratio": row["ratio"],
            "brief": str(payload.get("prompt") or ""),
            # Requested replacement roles are separate from uploaded target
            # pictures.  An empty uploaded list is valid: the source video is
            # analysed first, then the asset planner may call Image2.
            "roles": list((payload["_remix"].get("uploaded_roles")
                            if "uploaded_roles" in payload["_remix"]
                            else payload["_remix"].get("roles")) or []),
            "require_asr": (payload.get("_remix") or {}).get("version") == 2,
        }
        # Some edge proxies stream large media through a browser-oriented
        # hostname and can return a partial/forbidden file to DashScope ASR.
        # Keep Qwen video analysis on the public origin, but let ASR use the
        # existing direct transfer IP when it is configured.
        if analysis_input["require_asr"]:
            transfer_ip = os.getenv("H3_TRANSFER_HOST_IP", "").strip()
            public_origin = os.getenv("H3_TRANSFER_BASE_URL", "").strip().rstrip("/")
            if transfer_ip and public_origin:
                analysis_input["asr_source_url"] = analysis_input["source"]["url"].replace(
                    public_origin, f"https://{transfer_ip}", 1
                )
        v2 = analysis_input["require_asr"]
        if (v2 or remix_hypit.enabled()) and analysis_input["roles"]:
            try:
                analysis_input["targets"] = remix_hypit.analysis_targets(
                    json.loads(row["input_objects"] or "[]"), analysis_input["roles"],
                    lambda item: _input_url(row, item),
                )
            except ValueError as exc:
                _terminal_failure(row["id"], str(exc), False)
                return True
        analysis_started = time.time()
        options = payload["_remix"]
        metrics = options.setdefault("metrics", {})
        metrics["analysis_started_at"] = analysis_started
        payload["_remix"] = options
        if v2 and not remix_vision.configured():
            _terminal_failure(row["id"], "视频分析服务暂时不可用", False)
            return True
        if (v2 or remix_hypit.analysis_via_api()) and remix_vision.configured():
            with connect() as db:
                db.execute(
                    """UPDATE merchant_tasks SET progress=12,message='正在分析参考视频'
                    WHERE id=? AND status='dispatching'""",
                    (row["id"],),
                )
            try:
                analysis, cost, runtime = await remix_vision.analyse(analysis_input)
            except remix_vision.VisionError as exc:
                if v2:
                    _terminal_failure(row["id"], f"Qwen Plus 视频分析失败: {exc}", False)
                    return True
                if not os.getenv("GONGJI_HYPIT_ANALYZER_OVERLAY_KEY", "").strip():
                    _terminal_failure(row["id"], f"视频脚本分析失败: {exc}", False)
                    return True
                log.warning("Vision API failed for %s, falling back to Gongji: %s", row["id"], exc)
                metrics["analysis_api_error"] = str(exc)[:200]
            else:
                if v2:
                    analysis["remix2"] = True
                metrics["analysis_engine"] = runtime["model"]
                metrics["analysis_api_calls"] = runtime["calls"]
                metrics["analysis_hint_seconds"] = runtime["hint_seconds"]
                metrics["analysis_asr"] = runtime.get("asr", {})
                payload["_remix"] = options
                _queue_analyzed_remix(row, payload, analysis, cost, time.time(), from_status="dispatching")
                return True
        try:
            task_id = await gongji.create_remix_analysis_job(
                row["id"], row["merchant_id"], analysis_input,
                _transfer_backend(row),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Qwen-VL dispatch failed for %s", row["id"])
            _terminal_failure(row["id"], f"视频脚本分析投递失败: {exc}", False)
            return True
        with connect() as db:
            db.execute(
                """UPDATE merchant_tasks SET status='running',remote_id=?,progress=12,
                message='正在处理参考视频',payload=? WHERE id=? AND status='dispatching'""",
                (str(task_id), json.dumps(payload, ensure_ascii=False, separators=(",", ":")), row["id"]),
            )
        return True
    if ((payload.get("_remix") or {}).get("editor_mode") == "generic"
            and isinstance((payload.get("_remix") or {}).get("analysis"), dict)
            and not isinstance((payload.get("_remix") or {}).get("asset_plan"), dict)):
        try:
            payload = await _plan_editor_assets(row, payload)
            # The planner persists the new payload; pass it to the next stage
            # in this dispatch cycle as well (otherwise keyframe dispatch
            # reads the stale row and silently skips the asset plan).
            row = {**row, "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}
        except Exception as exc:  # planner failure falls back to prompt-only editing
            log.warning("Editor asset planning failed for %s: %s", row["id"], exc)
            options = payload.get("_remix") or {}
            options["asset_plan"] = {"need_generated_images": False,
                                     "reason": "planner_error_fallback",
                                     "image_prompts": [], "provider_used": False}
            payload["_remix"] = options
            with connect() as db:
                db.execute("UPDATE merchant_tasks SET payload=? WHERE id=? AND status='dispatching'",
                           (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), row["id"]))
    if _remix_needs_keyframes(payload):
        from commerce import dispatch_remix_keyframes
        await dispatch_remix_keyframes(row)
        return True
    try:
        row = await _compile_task_prompt(row)
    except prompt_service.PromptServiceError as exc:
        if exc.retryable:
            return not _requeue_prompt_retry(row, exc)
        log.exception("Prompt middleware failed for %s", row["id"])
        _terminal_failure(row["id"], "生成描述处理中断，任务尚未投递 GPU", False)
        return True
    except Exception:  # noqa: BLE001
        log.exception("Prompt middleware failed for %s", row["id"])
        _terminal_failure(row["id"], "生成描述处理中断，任务尚未投递 GPU", False)
        return True
    compiled_payload = json.loads(row["payload"] or "{}")
    if compiled_payload.get("_project", {}).get("mode") == "hypit_project":
        from video_project_runner import dispatch_project_take
        try:
            await dispatch_project_take(dict(row))
        except Exception as exc:  # noqa: BLE001
            log.exception("Hypit project dispatch failed for %s", row["id"])
            _terminal_failure(row["id"], f"视频编辑项目调度失败：{str(exc)[:500]}", False)
        return True
    try:
        worker_input = _worker_input(row)
        video_submitted = time.time()
        if worker_input.get("mode") in {"long_text", "digital_human_workflow"}:
            task_id = await gongji.create_long_job(
                row["id"], row["merchant_id"], worker_input,
                _transfer_backend(row),
            )
        elif worker_input.get("_commerce_depth_worker"):
            task_id = await gongji.create_commerce_remix_job(
                row["id"], row["merchant_id"], worker_input, row["quality"],
                _transfer_backend(row),
            )
        else:
            task_id = await gongji.create_job(
                row["id"], row["merchant_id"], worker_input, row["quality"],
                _transfer_backend(row),
                workload=_workload_for(row, payload),
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("Gongji dispatch failed for %s", row["id"])
        message = str(exc) if isinstance(exc, gongji.NoGpuAvailableError) else f"投递到 GPU 平台失败: {exc}"
        _terminal_failure(row["id"], message, False)
        return True
    with connect() as db:
        dispatched_payload = json.loads(row["payload"] or "{}")
        if dispatched_payload.get("_tool") == "remix":
            options = dispatched_payload["_remix"]
            metrics = options.setdefault("metrics", {})
            metrics["video_submitted_at"] = video_submitted
            options["metrics"] = metrics
            dispatched_payload["_remix"] = options
        db.execute(
            """UPDATE merchant_tasks SET status='running',remote_id=?,progress=20,
            message='等待 GPU 启动',payload=? WHERE id=? AND status='dispatching'""",
            (str(task_id), json.dumps(dispatched_payload, ensure_ascii=False, separators=(",", ":")), row["id"]),
        )
    return True


async def dispatch_loop(queue: str = "standard") -> None:
    parallelism = (
        REMIX_DISPATCH_PARALLELISM if queue == "remix"
        else MUSIC_MAX_CONCURRENCY if queue == "music"
        else VOICE_CLONE_MAX_CONCURRENCY if queue == "voice_clone"
        else DISPATCH_PARALLELISM
    )
    while True:
        try:
            # Claims remain transactional, while prompt compilation and provider
            # submission run concurrently. This prevents one slow submission from
            # holding every task behind it in the queue.
            while True:
                results = await asyncio.gather(
                    *(_dispatch_once(queue) for _ in range(parallelism))
                )
                if not all(results):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("%s dispatch loop error", queue)
        await asyncio.sleep(DISPATCH_INTERVAL)


_completion_locks = [asyncio.Lock() for _ in range(128)]


async def _complete(row, info: dict) -> bool:
    async with _completion_locks[hash(row["id"]) % len(_completion_locks)]:
        with connect() as db:
            current = db.execute("SELECT status FROM merchant_tasks WHERE id=?", (row["id"],)).fetchone()
        expected_status = "music_running" if json.loads(row["payload"] or "{}").get("_tool") == "music" else "running"
        if not current or current["status"] != expected_status:
            return True
        return await _complete_locked(row, info)


async def _complete_locked(row, info: dict) -> bool:
    backend = _transfer_backend(row)
    initial_payload = json.loads(row["payload"] or "{}")
    is_music = initial_payload.get("_tool") == "music"
    is_voice_clone = initial_payload.get("_tool") == "voice_clone"
    is_audio = is_music or is_voice_clone
    minimum_output_bytes = MIN_VOICE_CLONE_OUTPUT_BYTES if is_voice_clone else MIN_OUTPUT_BYTES
    is_remix = initial_payload.get("_tool") == "remix" and not _is_direct_remix_payload(initial_payload)
    needs_finalize = _needs_remix_finalize(initial_payload) or is_remix
    analysis_billing = _analysis_billing(row)
    if is_remix and analysis_billing <= 0:
        analysis_remote_id = str(
            (initial_payload.get("_remix") or {}).get("analysis_remote_id") or ""
        )
        if analysis_remote_id:
            try:
                analysis_info = await gongji.job_status(analysis_remote_id)
                analysis_billing = float(analysis_info.get("billing") or 0)
            except Exception:  # Final video delivery must not fail on cost telemetry.
                log.warning("Could not reconcile remix analysis cost for %s", row["id"], exc_info=True)
    objects: list[dict] = []
    if backend == "local":
        artifacts = await asyncio.to_thread(
            list_task_media if is_audio else list_task_videos,
            row["merchant_id"], row["id"], minimum_output_bytes,
        )
        if not artifacts:
            return False
    else:
        prefix = f'{OUTPUT_PREFIX}/{row["merchant_id"]}/{row["id"]}/'
        objects = await asyncio.to_thread(list_objects, prefix)
        output_suffixes = {".mp3", ".flac", ".wav", ".m4a"} if is_audio else {".mp4"}
        objects = [
            item for item in objects
            if Path(str(item.get("key") or "")).suffix.lower() in output_suffixes
            and int(item.get("size") or 0) >= minimum_output_bytes
        ]
        if not objects:
            return False
        if local_outputs_enabled():
            artifacts = [
                await asyncio.to_thread(
                    store_cos_video,
                    item["key"], row["merchant_id"], row["id"],
                    item["key"].rsplit("/", 1)[-1], item["size"],
                )
                for item in objects
            ]
        else:
            artifacts = [
                {
                    "name": item["key"].rsplit("/", 1)[-1],
                    "bucket": bucket_name(), "key": item["key"],
                    "media": "audio" if is_audio else "video",
                    "content_type": {
                        ".mp3": "audio/mpeg", ".flac": "audio/flac",
                        ".wav": "audio/wav", ".m4a": "audio/mp4",
                    }.get(Path(item["key"]).suffix.lower(), "video/mp4"),
                    "size_bytes": item["size"],
                }
                for item in objects
            ]
    stored_artifacts = list(artifacts)
    if needs_finalize:
        from commerce import finalize_remix
        artifacts = await asyncio.to_thread(finalize_remix, dict(row), artifacts)
        existing_paths = {a.get("path") or a.get("key") for a in stored_artifacts}
        stored_artifacts.extend(a for a in artifacts if (a.get("path") or a.get("key")) not in existing_paths)
    now = time.time()
    completed_payload = json.loads(row["payload"] or "{}")
    if is_remix or needs_finalize:
        options = completed_payload["_remix"]
        metrics = options.setdefault("metrics", {})
        metrics["video_completed_at"] = now
        metrics["video_wall_seconds"] = round(
            now - float(metrics.get("video_submitted_at") or now), 3
        )
        metrics["video_provider_running_seconds"] = float(
            info.get("running_seconds") or 0
        )
        metrics["video_platform_cost"] = float(info.get("billing") or 0)
        metrics["analysis_platform_cost"] = analysis_billing
        metrics["total_platform_cost"] = analysis_billing + float(info.get("billing") or 0)
        metrics["total_wall_seconds"] = round(now - float(row["created_at"]), 3)
        options["metrics"] = metrics
        completed_payload["_remix"] = options
    # A remix needs its source video through final audio muxing, but it has no
    # reason to retain customer inputs once that step succeeds.
    input_expiry = now if needs_finalize else now + INPUT_RETENTION
    with transaction() as db:
        current = db.execute("SELECT status FROM merchant_tasks WHERE id=?", (row["id"],)).fetchone()
        expected_status = "music_running" if is_music else "running"
        if not current or current["status"] != expected_status:
            return True
        db.execute(
            """UPDATE merchant_tasks SET status='completed',progress=100,message='生成完成',
            artifacts=?,provider_billing=?,finished_at=?,input_expires_at=?,output_expires_at=?,payload=?
            WHERE id=?""",
            (
                json.dumps(artifacts, ensure_ascii=False),
                analysis_billing + float(info.get("billing") or 0), now,
                input_expiry, now + OUTPUT_RETENTION,
                json.dumps(completed_payload, ensure_ascii=False, separators=(",", ":")),
                row["id"],
            ),
        )
        db.execute(
            "UPDATE assets SET expires_at=? WHERE task_id=? AND role='input' AND expires_at IS NULL",
            (input_expiry, row["id"]),
        )
        for item in stored_artifacts:
            db.execute(
                """INSERT OR IGNORE INTO assets
                (id,merchant_id,task_id,role,bucket,object_key,filename,content_type,size_bytes,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    public_id("ast"), row["merchant_id"], row["id"], "output",
                    "local" if item.get("storage") == "local" else item["bucket"],
                    item.get("path") or item["key"], item["name"], item.get("content_type") or ("audio/mpeg" if str(item.get("name", "")).endswith(".mp3") else "audio/flac" if str(item.get("name", "")).endswith(".flac") else "video/mp4"),
                    item["size_bytes"], now,
                    now + OUTPUT_RETENTION,
                ),
            )
        settle_task(db, row["id"], "success", note="音频生成成功" if is_audio else "视频生成成功")
        if completed_payload.get("_project", {}).get("project_id"):
            video_projects.mark_run(
                db, row["id"], status="completed",
                result={"artifacts": artifacts, "metrics": (completed_payload.get("_remix") or {}).get("metrics", {})},
            )
    if not is_audio:
        from media_preview import warm_task_previews
        asyncio.create_task(warm_task_previews(row["merchant_id"], row["id"], artifacts))
    if backend == "cos" and local_outputs_enabled():
        try:
            await asyncio.to_thread(delete_keys, [item["key"] for item in objects])
        except Exception:  # COS lifecycle remains the final cleanup safeguard.
            log.warning("Could not remove copied COS outputs for %s", row["id"], exc_info=True)
    if needs_finalize:
        try:
            await _cleanup_once()
        except Exception:  # Database expiry remains the retryable cleanup source.
            log.warning("Could not immediately purge remix inputs for %s", row["id"], exc_info=True)
    return True


async def complete_from_worker(task_id: str, result: dict) -> bool:
    """Finalize a valid worker success callback without waiting for provider polling.

    The artifact is discovered and validated by the same completion path used by
    polling. Provider cost may arrive later and is reconciled independently.
    """
    if result.get("ok") is not True:
        return False
    with connect() as db:
        row = db.execute(
            """SELECT * FROM merchant_tasks
            WHERE id=? AND status='running' AND remote_id IS NOT NULL""",
            (task_id,),
        ).fetchone()
    if not row:
        return False
    # Remix and Hypit projects use one or more child GPU jobs under one
    # merchant task. Let their project poller compose the Takes and finalize
    # the parent so a child callback can never complete it prematurely.
    callback_payload = json.loads(row["payload"] or "{}")
    if (
        callback_payload.get("_tool") == "remix"
        or (callback_payload.get("_project") or {}).get("project_id")
    ):
        return False
    return await _complete(row, {"billing": float(row["provider_billing"] or 0)})


async def _poll_task(row) -> None:
    payload = json.loads(row["payload"] or "{}")
    analysis_stage = _remix_needs_analysis(payload)
    try:
        info = await gongji.job_status(row["remote_id"])
    except Exception:  # noqa: BLE001
        log.warning("Gongji status failed for %s", row["id"], exc_info=True)
        return
    now = time.time()
    # Pollers receive sqlite3.Row objects from connect(); use indexed access
    # here so terminal provider states can always settle and release billing.
    running_status = "music_running" if row["kind"] == "music" else "running"
    if info["status"] == "running":
        with connect() as db:
            db.execute(
                """UPDATE merchant_tasks SET provider_started=1,started_at=COALESCE(started_at,?),
                progress=45,message='GPU 正在生成',provider_billing=?
                WHERE id=? AND status=?""",
                (
                    now,
                    _analysis_billing(row) + float(info.get("billing") or 0),
                    row["id"], running_status,
                ),
            )
            if analysis_stage:
                db.execute(
                    "UPDATE merchant_tasks SET progress=28,message='正在处理参考视频' WHERE id=? AND status=?",
                    (row["id"], running_status),
                )
    elapsed_from = row["started_at"] or row["created_at"]
    if info["status"] in {"queued", "running"} and now - elapsed_from > MAX_TASK_SECONDS:
        try:
            await gongji.stop_job(row["remote_id"])
        except Exception:  # noqa: BLE001
            log.exception("Could not stop timed out task %s", row["id"])
        _terminal_failure(
            row["id"], "GPU 任务超过最大运行时间，已终止",
            bool(row["provider_started"] or info["status"] == "running"),
            _analysis_billing(row) + float(info.get("billing") or 0),
        )
    elif info["status"] == "completed":
        if analysis_stage:
            try:
                report = await asyncio.to_thread(_worker_report, dict(row))
            except Exception:  # noqa: BLE001
                log.warning("Could not read Qwen analysis for %s", row["id"], exc_info=True)
                report = None
            analysis = (report or {}).get("analysis")
            if not isinstance(analysis, dict) or not analysis.get("shots"):
                if now - (row["started_at"] or row["created_at"]) <= MAX_TASK_SECONDS + 120:
                    return
                detail = str((report or {}).get("error") or "Qwen-VL 未返回完整镜头脚本")
                _terminal_failure(
                    row["id"], detail, True,
                    _analysis_billing(row) + float(info.get("billing") or 0),
                )
                return
            _queue_analyzed_remix(
                dict(row), payload, analysis, float(info.get("billing") or 0), now
            )
            return
        found = await _complete(row, info)
        if not found and now - (row["started_at"] or row["created_at"]) > MAX_TASK_SECONDS + 120:
            missing = "GPU 已结束但未找到输出音频" if row["kind"] == "voice_clone" else "GPU 已结束但未找到输出视频"
            _terminal_failure(row["id"], missing, True, float(info.get("billing") or 0))
    elif info["status"] == "failed":
        if analysis_stage:
            try:
                report = await asyncio.to_thread(_worker_report, dict(row))
            except Exception:  # noqa: BLE001
                report = None
            analysis = (report or {}).get("analysis")
            if (report or {}).get("ok") is True and isinstance(analysis, dict):
                # Gongji occasionally reports a terminal Error even after the
                # worker atomically uploaded a valid script. The validated
                # result is stronger evidence than the coarse container state.
                if _queue_analyzed_remix(
                    dict(row), payload, analysis,
                    float(info.get("billing") or 0), now,
                ):
                    return
            detail = str((report or {}).get("error") or info.get("reason") or "unknown")
            _terminal_failure(
                row["id"], f"Qwen-VL 视频脚本分析失败: {detail}",
                bool(row["provider_started"] or info.get("running_seconds")),
                float(info.get("billing") or 0),
            )
            return
        # A Job can exit non-zero after the video PUT has already completed.
        # A valid, atomically uploaded COS object is stronger evidence than
        # the provider's coarse container reason "Error".
        if await _complete(row, info):
            return
        try:
            report = await asyncio.to_thread(_worker_report, dict(row))
        except Exception:  # noqa: BLE001
            log.warning("Could not read worker result for %s", row["id"], exc_info=True)
            report = None
        if report and report.get("error"):
            log.error("GPU task %s worker failure: %s", row["id"], str(report["error"])[:3000])
            from object_storage import relay_failed
            relay_failed(str(report["error"]))
        _terminal_failure(
            row["id"], f"GPU 任务失败: {info.get('reason') or 'unknown'}",
            bool(row["provider_started"] or info.get("running_seconds")),
            _analysis_billing(row) + float(info.get("billing") or 0),
        )


async def _poll_once(music_only: bool = False) -> None:
    with connect() as db:
        rows = db.execute(
            ("SELECT * FROM merchant_tasks WHERE status='music_running' AND remote_id IS NOT NULL AND kind='music'" if music_only
             else "SELECT * FROM merchant_tasks WHERE status='running' AND remote_id IS NOT NULL")
        ).fetchall()
    semaphore = asyncio.Semaphore(POLL_PARALLELISM)

    async def poll_one(row) -> None:
        try:
            async with semaphore:
                payload = json.loads(row["payload"] or "{}")
                project = payload.get("_project") or {}
                if payload.get("_tool") == "reenact":
                    # The runner owns its child jobs; after a restart it resumes here.
                    from reenact import runner as reenact_runner
                    reenact_runner.ensure(row["id"])
                elif payload.get("_tool") == "commerce_remix":
                    from commerce_remix import ensure
                    ensure(row["id"])
                elif project.get("mode") == "hypit_project" and project.get("stage") == "taking":
                    from video_project_runner import poll_project_take
                    await poll_project_take(dict(row))
                elif _remix_keyframes_running(payload):
                    from commerce import poll_remix_keyframes
                    await poll_remix_keyframes(dict(row))
                elif row["kind"] == "image_edit":
                    from commerce import poll_image
                    await poll_image(dict(row))
                else:
                    await _poll_task(row)
        except Exception:  # noqa: BLE001
            # One corrupt legacy record must never block status and artifact
            # reconciliation for every other merchant task in the same pass.
            log.exception("Task reconciliation failed for %s; continuing", row["id"])

    await asyncio.gather(*(poll_one(row) for row in rows))

    # 共绩的最终成本有时会在任务完成后几十秒才回填。只追查最近一天内
    # 成本仍为 0 的成功任务，避免管理员成本报表永久显示为 0。
    with connect() as db:
        pending_costs = db.execute(
            """SELECT id,remote_id FROM merchant_tasks
            WHERE status='completed' AND remote_id IS NOT NULL AND provider_billing=0 AND kind!='image_edit'
              AND (?=0 OR kind='music')
              AND finished_at>=? ORDER BY finished_at DESC LIMIT 20""",
            (int(music_only), time.time() - 86400),
        ).fetchall()
    async def reconcile_cost(row) -> None:
        try:
            async with semaphore:
                info = await gongji.job_status(row["remote_id"])
        except Exception:  # noqa: BLE001
            log.warning("Gongji cost reconciliation failed for %s", row["id"], exc_info=True)
            return
        billing = float(info.get("billing") or 0)
        if billing > 0:
            with connect() as db:
                db.execute(
                    "UPDATE merchant_tasks SET provider_billing=? WHERE id=? AND provider_billing=0",
                    (billing, row["id"]),
                )

    await asyncio.gather(*(reconcile_cost(row) for row in pending_costs))


async def poll_loop(music_only: bool = False) -> None:
    while True:
        try:
            await _poll_once(music_only=music_only)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Poll loop error")
        await asyncio.sleep(POLL_INTERVAL)


async def _cleanup_once() -> int:
    now = time.time()
    with connect() as db:
        rows = db.execute(
            """SELECT id,merchant_id,task_id,role,bucket,object_key FROM assets
            WHERE expires_at IS NOT NULL AND expires_at<=? AND purged_at IS NULL LIMIT 500""",
            (now,),
        ).fetchall()
    cos_rows = [row for row in rows if row["bucket"] not in {"local", "local-input", "local-result"}]
    local_rows = [row for row in rows if row["bucket"] == "local"]
    local_input_rows = [row for row in rows if row["bucket"] == "local-input"]
    local_result_rows = [row for row in rows if row["bucket"] == "local-result"]
    if cos_rows:
        await asyncio.to_thread(delete_keys, [row["object_key"] for row in cos_rows])
    for row in local_rows:
        await asyncio.to_thread(remove_local_output, row["object_key"])
    for row in local_input_rows:
        await asyncio.to_thread(remove_local_input, row["object_key"])
    for row in local_result_rows:
        await asyncio.to_thread(remove_local_result, row["object_key"])
    if rows:
        from media_preview import remove_task_previews
        for merchant_id, task_id in {(row["merchant_id"], row["task_id"]) for row in rows if row["role"] == "output"}:
            await asyncio.to_thread(remove_task_previews, merchant_id, task_id)
    with connect() as db:
        if rows:
            db.executemany("UPDATE assets SET purged_at=? WHERE id=?", [(now, row["id"]) for row in rows])
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
    try:
        from reenact import templates
        templates.purge_expired()
    except Exception:  # noqa: BLE001 - template cleanup never blocks asset cleanup
        log.warning("Template cleanup failed", exc_info=True)
    return len(rows)


async def cleanup_loop() -> None:
    while True:
        try:
            removed = await _cleanup_once()
            if removed:
                log.info("Purged %s expired input/output objects", removed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Cleanup loop error")
        await asyncio.sleep(CLEANUP_INTERVAL)


def _create_backup() -> Path:
    target = DB.parent / f"merchant-backup-{int(time.time())}.sqlite3"
    source = sqlite3.connect(DB)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    target.chmod(0o600)
    return target


async def backup_loop() -> None:
    while True:
        target: Path | None = None
        try:
            target = await asyncio.to_thread(_create_backup)
            stamp = datetime.now(timezone.utc).strftime("%Y/%m/%d/%H%M%S")
            await asyncio.to_thread(
                upload_path, target, f"backups/merchant-db/{stamp}.sqlite3",
                "application/vnd.sqlite3",
            )
            log.info("Merchant database backup uploaded")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Database backup failed")
        finally:
            if target:
                target.unlink(missing_ok=True)
        await asyncio.sleep(BACKUP_INTERVAL)


def stats() -> dict:
    with connect() as db:
        counts = {
            row["status"]: row["n"]
            for row in db.execute("SELECT status,COUNT(*) n FROM merchant_tasks GROUP BY status")
        }
    return {
        "max_concurrency": MAX_CONCURRENCY,
        "running": counts.get("running", 0) + counts.get("dispatching", 0),
        "queued": counts.get("queued", 0), "counts": counts,
    }
