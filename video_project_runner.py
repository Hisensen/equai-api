"""Local media primitives used by the project Shot Runner.

Provider submission is deliberately kept outside this module.  These helpers
make the boundary between generated Takes and the final Timeline deterministic
and independently testable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path

from video_project_engine import plan_shots
import video_projects
from video_projects import fingerprint

log = logging.getLogger("h3.video-project")
OUTPUT_PREFIX = os.getenv("COS_OUTPUT_PREFIX", "outputs").strip("/")


def extract_shot(source: Path, target: Path, start: float, end: float) -> None:
    if end <= start:
        raise ValueError("镜头结束时间必须大于开始时间")
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{start:.3f}",
         "-i", str(source), "-t", f"{end - start:.3f}", "-map", "0:v:0?", "-map", "0:a:0?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac",
         "-movflags", "+faststart", str(target)],
        check=True, capture_output=True, timeout=300,
    )
    if not target.is_file() or target.stat().st_size < 1024:
        raise RuntimeError("镜头切片没有生成有效视频")


def concat_takes(takes: list[Path], output: Path) -> None:
    if not takes:
        raise ValueError("至少需要一个 Take")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = output.with_suffix(".concat.txt")
    manifest.write_text(
        "".join(f"file {json.dumps(str(path.resolve()))}\n" for path in takes),
        encoding="utf-8",
    )
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat",
             "-safe", "0", "-i", str(manifest), "-c", "copy", str(output)],
            check=True, capture_output=True, timeout=600,
        )
    finally:
        manifest.unlink(missing_ok=True)
    if not output.is_file() or output.stat().st_size < 1024:
        raise RuntimeError("Timeline 合成没有生成有效视频")


def assemble_takes(
    takes: list[tuple[Path, float]], output: Path, ratio: str,
) -> None:
    """Trim Takes to authored Timeline spans, normalize, then concatenate."""
    if not takes or any(seconds <= 0 for _, seconds in takes):
        raise ValueError("Timeline Take 列表无效")
    width, height = (1280, 736) if ratio == "16:9" else (736, 1280)
    filters = []
    inputs = []
    for index, (path, seconds) in enumerate(takes):
        inputs += ["-i", str(path)]
        filters.append(
            f"[{index}:v:0]trim=duration={seconds:.3f},setpts=PTS-STARTPTS,"
            f"fps=24,scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{index}]"
        )
    labels = "".join(f"[v{i}]" for i in range(len(takes)))
    filters.append(f"{labels}concat=n={len(takes)}:v=1:a=0[outv]")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs,
         "-filter_complex", ";".join(filters), "-map", "[outv]", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-movflags", "+faststart", str(output)],
        check=True, capture_output=True, timeout=900,
    )
    if not output.is_file() or output.stat().st_size < 1024:
        raise RuntimeError("Timeline 渲染没有生成有效视频")


def shot_prompt(contract: str, brief: str, duration: float, ratio: str) -> str:
    return (
        "[Hypit Shot Take] Generate only this independent shot Take. "
        f"Duration: {duration:.3f}s. Aspect ratio: {ratio}. "
        "Preserve the shot's camera grammar, action order and subject relationships. "
        "Do not invent an opening or closing shot, subtitles, logos or extra subjects. "
        f"Shot contract: {contract or 'match the source shot naturally'}. "
        + (f"User direction: {brief.strip()}" if brief.strip() else "")
    )


def plan_take_groups(
    analysis: dict, duration: float, *, target_group_seconds: float = 9.0,
    dependency_context: dict | None = None,
) -> list[dict]:
    """Group adjacent analyzed shots into bounded 5-15s model Takes.

    H3's public task contract accepts integer 5-15s requests. Groups retain
    actual source ranges; generated excess is trimmed during final assembly.
    """
    if not math.isfinite(duration) or duration < 4.9:
        raise ValueError("视频编辑项目原视频不能少于 5 秒")
    raw_shots = plan_shots(analysis, duration)
    # Make the authored takes cover the whole source timeline, including small
    # gaps at the head/tail or between analyzer cut estimates.
    shots = []
    for index, shot in enumerate(raw_shots):
        start = 0.0 if index == 0 else shot.start
        end = duration if index == len(raw_shots) - 1 else raw_shots[index + 1].start
        end = max(start, min(duration, end))
        if end > start:
            shots.append(replace(shot, start=start, end=end, duration=end - start))
    # A long uninterrupted source shot still needs multiple bounded model
    # Takes; split its source interval without introducing timeline gaps.
    bounded = []
    for shot in shots:
        cursor = shot.start
        part = 1
        while shot.end - cursor > target_group_seconds:
            end = cursor + target_group_seconds
            bounded.append(replace(
                shot, key=f"{shot.key}_part_{part:02d}", start=cursor, end=end,
                duration=end - cursor,
                dependency_fingerprint=fingerprint({
                    "shot": shot.dependency_fingerprint,
                    "range": [round(cursor, 3), round(end, 3)],
                }),
            ))
            cursor, part = end, part + 1
        if shot.end > cursor:
            bounded.append(replace(
                shot, key=f"{shot.key}_part_{part:02d}" if part > 1 else shot.key,
                start=cursor, end=shot.end, duration=shot.end - cursor,
                dependency_fingerprint=fingerprint({
                    "shot": shot.dependency_fingerprint,
                    "range": [round(cursor, 3), round(shot.end, 3)],
                }) if part > 1 else shot.dependency_fingerprint,
            ))
    shots = bounded
    groups: list[list] = []
    current: list = []
    for shot in shots:
        if current and shot.end - current[0].start > target_group_seconds:
            groups.append(current)
            current = []
        current.append(shot)
    if current:
        groups.append(current)
    # Avoid an unusably tiny tail by balancing two neighboring ranges.
    if len(groups) > 1 and groups[-1][-1].end - groups[-1][0].start < 2.5:
        previous = groups[-2]
        all_shots = previous + groups[-1]
        midpoint = (all_shots[0].start + all_shots[-1].end) / 2
        split = min(range(1, len(all_shots)), key=lambda i: abs(all_shots[i].start - midpoint))
        groups[-2:] = [all_shots[:split], all_shots[split:]]
    result = []
    for index, group in enumerate(groups, 1):
        start, end = group[0].start, group[-1].end
        seconds = end - start
        result.append({
            "index": index,
            "node_key": f"take_{index:03d}",
            "shot_keys": [shot.key for shot in group],
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(seconds, 3),
            "request_duration": max(5, min(15, math.ceil(seconds - 0.001))),
            "contract": "\n".join(shot.contract for shot in group if shot.contract),
            "dependency_fingerprint": fingerprint({
                "shots": [shot.dependency_fingerprint for shot in group],
                "context": dependency_context or {},
            }),
        })
    return result


def take_generation_prompt(
    base_prompt: str, contract: str, brief: str, start: float, end: float,
    request_duration: int,
) -> str:
    return (
        f"{base_prompt}\n\n[Independent Take] Generate the edited source passage from "
        f"{start:.3f}s to {end:.3f}s as a standalone {request_duration}-second Take. "
        "Use the supplied trimmed source video as the sole authority for this passage's "
        "camera, action, framing and temporal order. Apply the replacement reference "
        "images consistently. Do not invent adjacent scenes or add captions.\n"
        f"[Passage contract] {contract or 'Preserve the visible action and framing.'}\n"
        f"[User direction] {brief.strip()}"
    )


async def dispatch_project_take(row: dict) -> None:
    """Dispatch one sequential Take, or compose when all Takes are ready."""
    import gongji
    import local_storage
    import object_storage
    import transfer_storage
    import video_projects
    from database import connect, public_id, transaction
    from scheduler import _input_url, _terminal_failure
    from commerce import copy_asset, store_output

    payload = json.loads(row["payload"] or "{}")
    project_meta = payload.get("_project") or {}
    options = payload.get("_remix") or {}
    source_seconds = float(options.get("source_seconds") or row["duration"])
    plan = project_meta.get("take_plan")
    if not isinstance(plan, list):
        plan = plan_take_groups(
            options.get("analysis") or {}, source_seconds,
            dependency_context={
                "prompt": payload.get("_original_prompt") or payload.get("prompt") or "",
                "image_sha256": options.get("image_sha256") or [],
                "roles": options.get("roles") or [],
                "ratio": row["ratio"],
            },
        )
        project_meta["take_plan"] = plan
        project_meta["take_cursor"] = 0
        project_meta["stage"] = "taking"
        payload["_project"] = project_meta
        now = time.time()
        with transaction() as db:
            for item in plan:
                db.execute(
                    """INSERT OR IGNORE INTO video_project_nodes
                    (id,project_id,node_type,node_key,source_start,source_end,
                     dependency_fingerprint,status,output_json,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (public_id("node"), project_meta["project_id"], "take", item["node_key"],
                     item["start"], item["end"], item["dependency_fingerprint"],
                     "planned", json.dumps(item, ensure_ascii=False), now, now),
                )
            db.execute(
                "UPDATE merchant_tasks SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), row["id"]),
            )
    cursor = int(project_meta.get("take_cursor") or 0)
    if cursor >= len(plan):
        try:
            await asyncio.to_thread(_compose_project, row, payload, plan)
        except Exception as exc:  # noqa: BLE001
            log.exception("Hypit project composition failed for %s", row["id"])
            _terminal_failure(row["id"], f"Timeline 合成失败：{str(exc)[:500]}", True,
                              float(row.get("provider_billing") or 0))
        return

    take = plan[cursor]
    source_item = next((item for item in json.loads(row["input_objects"] or "[]")
                        if item.get("field") == "videos"), None)
    if source_item is None:
        _terminal_failure(row["id"], "视频编辑项目缺少原视频素材", False)
        return
    child_id = f"{row['id']}-take-{cursor + 1:03d}"
    try:
        with tempfile.TemporaryDirectory(prefix="h3-project-take-") as directory:
            root = Path(directory)
            source_path, cut_path = root / "source.mp4", root / "take-source.mp4"
            await asyncio.to_thread(copy_asset, source_item, source_path)
            await asyncio.to_thread(extract_shot, source_path, cut_path, take["start"], take["end"])
            clip_name = f"{take['node_key']}-source.mp4"
            clip_size = cut_path.stat().st_size
            local_clip = None
            if payload.get("_transfer_backend") == "local":
                from transfer_storage import signed_input_url, store_input
                local_asset_id = public_id("ast")
                with cut_path.open("rb") as handle:
                    local_key, clip_size = await asyncio.to_thread(
                        store_input, handle, row["merchant_id"], row["id"],
                        100 + cursor, clip_name,
                    )
                local_clip = {
                    "id": local_asset_id, "storage": "local", "bucket": "local-input",
                    "key": local_key, "filename": clip_name,
                    "content_type": "video/mp4", "size_bytes": clip_size,
                }
                clip_url = signed_input_url(row["id"], local_asset_id, expires=24 * 3600)
            else:
                clip_key = (
                    f"{os.getenv('COS_INPUT_PREFIX', 'inputs').strip('/')}/"
                    f"{row['merchant_id']}/{row['id']}/project/{clip_name}"
                )
                await asyncio.to_thread(object_storage.upload_path, cut_path, clip_key, "video/mp4")
                clip_url = object_storage.presigned_url(clip_key, expires=24 * 3600)

        media = [{"field": "videos", "filename": clip_name, "url": clip_url}]
        inputs = json.loads(row["input_objects"] or "[]")
        # The deployed 720p remix worker uses the ``ref1`` graph: a Take may
        # contain the source clip plus uploaded role references, but only one
        # generated target-frame anchor.  Sending two or more generated
        # anchors in one Take makes the worker enter an unsupported reference
        # combination and eventually terminate at its 300s watchdog.  Keep
        # the anchor nearest the Take midpoint; the authored contract still
        # carries the complete shot action for the rest of the interval.
        generated_candidates = []
        for item in inputs:
            if item.get("field") != "images":
                continue
            if item.get("generated_kind"):
                timestamp = float(item.get("timestamp") or -1)
                if not float(take["start"]) <= timestamp <= float(take["end"]):
                    continue
                generated_candidates.append((
                    abs(timestamp - (float(take["start"]) + float(take["end"])) / 2),
                    item,
                ))
                continue
            media.append({"field": "images", "filename": item["filename"], "url": _input_url(row, item)})
        if generated_candidates:
            _, anchor = min(generated_candidates, key=lambda value: value[0])
            media.append({"field": "images", "filename": anchor["filename"], "url": _input_url(row, anchor)})
        take_prompt = take_generation_prompt(
            str(payload.get("prompt") or ""), str(take.get("contract") or ""),
            str(payload.get("_original_prompt") or ""), float(take["start"]),
            float(take["end"]), int(take["request_duration"]),
        )
        worker_input = {
            key: value for key, value in payload.items()
            if not key.startswith("_") and key not in {"media", "video_audio_flags"}
        }
        worker_input.update({
            "mode": "reference", "tier": row["quality"],
            "duration": int(take["request_duration"]), "ratio": row["ratio"],
            "prompt": take_prompt, "seed": int(payload.get("seed") or 0) + cursor,
            "video_audio_flags": "0", "media": media,
        })
        remote_id = await gongji.create_job(
            child_id, row["merchant_id"], worker_input, row["quality"],
            payload.get("_transfer_backend") or "cos", workload="remix",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Hypit project Take dispatch failed for %s/%s", row["id"], take["node_key"])
        _terminal_failure(row["id"], f"镜头素材投递失败：{str(exc)[:500]}", False)
        return

    now = time.time()
    with transaction() as db:
        db.execute(
            """INSERT OR IGNORE INTO assets
            (id,merchant_id,task_id,role,bucket,object_key,filename,content_type,size_bytes,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (local_clip["id"] if local_clip else public_id("ast"), row["merchant_id"], row["id"], "input",
             "local-input" if local_clip else object_storage.bucket_name(),
             local_clip["key"] if local_clip else clip_key, clip_name, "video/mp4",
             clip_size, now),
        )
        db.execute(
            """UPDATE video_project_nodes SET status='running',run_id=(
              SELECT id FROM video_project_runs WHERE task_id=? ORDER BY created_at DESC LIMIT 1
            ),output_json=?,updated_at=? WHERE project_id=? AND node_key=?""",
            (row["id"], json.dumps({**take, "job_id": child_id, "remote_id": str(remote_id)}, ensure_ascii=False),
             now, project_meta["project_id"], take["node_key"]),
        )
        db.execute(
            """UPDATE merchant_tasks SET status='running',remote_id=?,started_at=COALESCE(started_at,?),
            progress=?,message=?,payload=? WHERE id=? AND status='dispatching'""",
            (str(remote_id), now, min(88, 55 + int(30 * cursor / max(1, len(plan)))),
             f"正在生成镜头 Take {cursor + 1}/{len(plan)}",
             json.dumps(payload, ensure_ascii=False, separators=(",", ":")), row["id"]),
        )
        video_projects.mark_run(db, row["id"], status="running")


def _compose_project(row: dict, payload: dict, plan: list[dict]) -> None:
    """Compose cached Takes and finalize the parent task with original audio."""
    import local_storage
    from commerce import copy_asset, finalize_remix, store_output
    from database import connect, public_id, settle_task, transaction
    from object_storage import list_objects

    project_meta = payload.get("_project") or {}
    with connect() as db:
        nodes = db.execute(
            "SELECT node_key,status,output_json FROM video_project_nodes WHERE project_id=? AND node_type='take' ORDER BY node_key",
            (project_meta["project_id"],),
        ).fetchall()
    by_key = {node["node_key"]: json.loads(node["output_json"] or "{}") for node in nodes
              if node["status"] == "ready"}
    if any(take["node_key"] not in by_key for take in plan):
        raise RuntimeError("Timeline 仍有镜头 Take 未完成")
    with tempfile.TemporaryDirectory(prefix="h3-project-render-") as directory:
        root = Path(directory)
        media = []
        for index, take in enumerate(plan, 1):
            artifact = by_key[take["node_key"]]["artifact"]
            path = root / f"take-{index:03d}.mp4"
            copy_asset(artifact, path, output=True)
            media.append((path, float(take["duration"])))
        rendered = root / "timeline.mp4"
        assemble_takes(media, rendered, row["ratio"])
        composed_artifact = store_output(row, rendered, "video")
        final_artifacts = finalize_remix(row, [composed_artifact])
    now = time.time()
    input_expiry = now + int(os.getenv("H3_INPUT_RETENTION_SECONDS", "86400"))
    output_expiry = now + int(os.getenv("H3_OUTPUT_RETENTION_SECONDS", "172800"))
    project_meta["stage"] = "completed"
    payload["_project"] = project_meta
    with transaction() as db:
        db.execute(
            """UPDATE merchant_tasks SET status='completed',progress=100,message='视频编辑项目完成',
            artifacts=?,finished_at=?,input_expires_at=?,output_expires_at=?,payload=? WHERE id=? AND status='dispatching'""",
            (json.dumps(final_artifacts, ensure_ascii=False), now,
             input_expiry,
             output_expiry,
             json.dumps(payload, ensure_ascii=False, separators=(",", ":")), row["id"]),
        )
        db.execute(
            "UPDATE assets SET expires_at=? WHERE task_id=? AND role='input' AND expires_at IS NULL",
            (input_expiry, row["id"]),
        )
        # Take artifacts are intermediate project outputs, but live in the
        # same storage namespace and must follow the parent task's retention
        # window instead of becoming permanent orphaned objects.
        db.execute(
            "UPDATE assets SET expires_at=? WHERE task_id=? AND role='output' AND expires_at IS NULL",
            (output_expiry, row["id"]),
        )
        for artifact in final_artifacts:
            db.execute(
                """INSERT OR IGNORE INTO assets
                (id,merchant_id,task_id,role,bucket,object_key,filename,content_type,size_bytes,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (public_id("ast"), row["merchant_id"], row["id"], "output",
                 "local" if artifact.get("storage") == "local" else artifact.get("bucket", "cos"),
                 artifact.get("path") or artifact.get("key"), artifact["name"], "video/mp4",
                 artifact["size_bytes"], now, now + int(os.getenv("H3_OUTPUT_RETENTION_SECONDS", "172800"))),
            )
        settle_task(db, row["id"], "success", note="Hypit 风格视频编辑项目完成")
        video_projects.mark_run(db, row["id"], status="completed",
                                result={"artifacts": final_artifacts, "takes": plan})


async def poll_project_take(row: dict) -> None:
    """Poll the active child job, persist its Take, and queue the next stage."""
    import gongji
    import local_storage
    import video_projects
    from database import connect, transaction
    from local_storage import list_task_videos
    from object_storage import bucket_name, list_objects
    from scheduler import MIN_OUTPUT_BYTES, _terminal_failure

    payload = json.loads(row["payload"] or "{}")
    project = payload.get("_project") or {}
    cursor = int(project.get("take_cursor") or 0)
    plan = project.get("take_plan") or []
    if cursor >= len(plan):
        return
    take = plan[cursor]
    child_id = f"{row['id']}-take-{cursor + 1:03d}"
    try:
        info = await gongji.job_status(row["remote_id"])
    except Exception:  # noqa: BLE001
        log.warning("Project Take status unavailable for %s", child_id, exc_info=True)
        return
    if info["status"] in {"queued", "running"}:
        progress = min(88, 58 + int(25 * cursor / max(1, len(plan))))
        with connect() as db:
            db.execute(
                "UPDATE merchant_tasks SET progress=?,message=? WHERE id=? AND status='running'",
                (progress, f"正在生成镜头 Take {cursor + 1}/{len(plan)}", row["id"]),
            )
        return
    if info["status"] == "failed":
        _terminal_failure(row["id"], f"镜头 Take {cursor + 1} 生成失败：{info.get('reason') or 'GPU 错误'}",
                          True, float(row.get("provider_billing") or 0) + float(info.get("billing") or 0))
        return

    artifacts = await asyncio.to_thread(
        list_task_videos, row["merchant_id"], child_id, MIN_OUTPUT_BYTES
    )
    if not artifacts and payload.get("_transfer_backend") == "local":
        # A local worker writes its result to the Guangzhou output volume. Do
        # not look in COS when the local callback is delayed; mixing backends
        # here made editor projects appear to lose outputs during migration.
        if time.time() - float(row.get("started_at") or row["created_at"]) < MAX_TASK_SECONDS + 120:
            return
        _terminal_failure(row["id"], f"镜头 Take {cursor + 1} 已结束但找不到本地输出", True,
                          float(row.get("provider_billing") or 0) + float(info.get("billing") or 0))
        return
    if not artifacts:
        prefix = f"{OUTPUT_PREFIX}/{row['merchant_id']}/{child_id}/"
        objects = await asyncio.to_thread(list_objects, prefix)
        objects = [item for item in objects if str(item.get("key", "")).endswith(".mp4")
                   and int(item.get("size") or 0) >= MIN_OUTPUT_BYTES]
        if not objects:
            if time.time() - float(row.get("started_at") or row["created_at"]) < MAX_TASK_SECONDS + 120:
                return
            _terminal_failure(row["id"], f"镜头 Take {cursor + 1} 已结束但找不到输出", True,
                              float(row.get("provider_billing") or 0) + float(info.get("billing") or 0))
            return
        item = max(objects, key=lambda value: int(value.get("size") or 0))
        artifact = {
            "name": item["key"].rsplit("/", 1)[-1], "bucket": bucket_name(),
            "key": item["key"], "media": "video", "size_bytes": int(item["size"]),
        }
        if local_storage.enabled():
            artifact = await asyncio.to_thread(
                store_cos_video, item["key"], row["merchant_id"], child_id,
                artifact["name"], artifact["size_bytes"],
            )
    else:
        artifact = artifacts[0]

    now = time.time()
    take["artifact"] = artifact
    take["provider_billing"] = float(info.get("billing") or 0)
    with transaction() as db:
        db.execute(
            """UPDATE video_project_nodes SET status='ready',output_json=?,updated_at=?
            WHERE project_id=? AND node_key=?""",
            (json.dumps(take, ensure_ascii=False), now, project["project_id"], take["node_key"]),
        )
        db.execute(
            """INSERT OR IGNORE INTO assets
            (id,merchant_id,task_id,role,bucket,object_key,filename,content_type,size_bytes,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (video_projects.fingerprint({"project": project["project_id"], "node": take["node_key"]})[:32],
             row["merchant_id"], row["id"], "output",
             "local" if artifact.get("storage") == "local" else artifact.get("bucket", "cos"),
             artifact.get("path") or artifact.get("key"), artifact.get("name", "take.mp4"),
             "video/mp4", int(artifact.get("size_bytes") or 0), now),
        )
        project["take_cursor"] = cursor + 1
        project["stage"] = "taking" if cursor + 1 < len(plan) else "composing"
        payload["_project"] = project
        db.execute(
            """UPDATE merchant_tasks SET status='queued',remote_id=NULL,provider_started=0,
            progress=?,message=?,payload=?,provider_billing=provider_billing+?,queued_at=?
            WHERE id=? AND status='running'""",
            (min(90, 58 + int(30 * (cursor + 1) / max(1, len(plan)))),
             "镜头 Take 已完成，准备下一段" if cursor + 1 < len(plan) else "镜头素材齐全，正在合成 Timeline",
             json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
             float(info.get("billing") or 0), now, row["id"]),
        )
