from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import time
import uuid
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx
import runpod
from network_bootstrap import install_transfer_host_mapping
from object_storage import upload_outputs
from audio_utils import _preserve_source_audio

install_transfer_host_mapping()

INTERNAL_API = os.getenv("H3_INTERNAL_API", "http://127.0.0.1:8000")
RUNTIME = Path(os.getenv("H3_ROOT", "/app")) / "runtime"
INPUT = RUNTIME / "input"
OUTPUT = RUNTIME / "output"
JOB_TIMEOUT = int(os.getenv("H3_JOB_TIMEOUT_SECONDS", "3600"))
MAX_DOWNLOAD_BYTES = int(os.getenv("H3_MAX_INPUT_BYTES", str(512 * 1024 * 1024)))
MAX_INTERNAL_POLL_FAILURES = int(os.getenv("H3_MAX_INTERNAL_POLL_FAILURES", "12"))
MAX_MEDIA_DOWNLOAD_ATTEMPTS = int(os.getenv("H3_MEDIA_DOWNLOAD_ATTEMPTS", "8"))
MEDIA_DOWNLOAD_PARALLELISM = max(
    1, int(os.getenv("H3_MEDIA_DOWNLOAD_PARALLELISM", "4"))
)

ROUTES = {
    "text": "/api/generate/text",
    "text_latent_hd": "/api/generate/text-latent-hd",
    "image": "/api/generate/image",
    "image_latent_hd": "/api/generate/image-latent-hd",
    "reference": "/api/generate/reference",
    "reference_latent_hd": "/api/generate/reference-latent-hd",
    "enhance": "/api/generate/enhance",
    "rtx_only": "/api/generate/rtx-only",
    "latent_refine": "/api/generate/latent-refine",
    "long_text": "/api/generate/long",
    "long_image": "/api/generate/long",
    "digital_human_workflow": "/api/generate/digital-human-workflow",
}


def _use_staged_hd() -> bool:
    """Keep production on the proven staged path; allow isolated one-graph A/Bs."""
    mode = os.getenv("H3_HD_EXECUTION_MODE", "staged").strip().lower()
    if mode not in {"staged", "single_graph"}:
        raise ValueError("H3_HD_EXECUTION_MODE 必须是 staged 或 single_graph")
    return mode == "staged"


def _safe_name(value: str, fallback: str) -> str:
    name = Path(value or fallback).name
    return name[:180] or fallback


def _download(url: str, target: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("素材地址必须使用 HTTP 或 HTTPS")
    for attempt in range(MAX_MEDIA_DOWNLOAD_ATTEMPTS):
        written = 0
        try:
            with httpx.stream("GET", url, timeout=300, follow_redirects=True) as response:
                response.raise_for_status()
                with target.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            raise ValueError("单个参考素材超过大小限制")
                        handle.write(chunk)
            return
        except httpx.TransportError:
            target.unlink(missing_ok=True)
            if attempt == MAX_MEDIA_DOWNLOAD_ATTEMPTS - 1:
                raise
            time.sleep(min(2**attempt, 15))
        except httpx.HTTPStatusError as exc:
            target.unlink(missing_ok=True)
            retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
            if not retryable or attempt == MAX_MEDIA_DOWNLOAD_ATTEMPTS - 1:
                raise
            time.sleep(min(2**attempt, 15))


def _prepare_media(job_id: str, items: list[dict]) -> tuple[Path, list[dict]]:
    folder = Path("/tmp/h3-jobs") / job_id
    folder.mkdir(parents=True, exist_ok=False)
    try:
        prepared = []
        allowed_fields = {
            "first_frame", "last_frame", "images", "videos", "audios",
            "source_video",
        }
        downloads = []
        for index, item in enumerate(items):
            field = str(item.get("field") or "")
            if field not in allowed_fields:
                raise ValueError(f"不支持的素材字段: {field}")
            name = _safe_name(str(item.get("filename") or ""), f"media-{index}")
            target = folder / f"{index:02d}-{name}"
            downloads.append((str(item.get("url") or ""), target))
            prepared.append({"field": field, "filename": name, "path": target})
        with ThreadPoolExecutor(
            max_workers=min(MEDIA_DOWNLOAD_PARALLELISM, max(1, len(downloads)))
        ) as pool:
            futures = [pool.submit(_download, url, target) for url, target in downloads]
            for future in futures:
                future.result()
        return folder, prepared
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def _submit(mode: str, payload: dict, media: list[dict]) -> str:
    files = []
    opened = []
    try:
        for item in media:
            handle = item["path"].open("rb")
            opened.append(handle)
            content_type = mimetypes.guess_type(item["filename"])[0] or "application/octet-stream"
            files.append((item["field"], (item["filename"], handle, content_type)))
        response = httpx.post(
            INTERNAL_API + ROUTES[mode], data=payload, files=files, timeout=300
        )
        if response.is_error:
            body = response.text.replace("\n", " ")[:900]
            raise RuntimeError(f"H3 endpoint HTTP {response.status_code}: {body}")
        prompt_id = str(response.json().get("prompt_id") or "")
        if not prompt_id:
            raise RuntimeError("H3 后端没有返回任务编号")
        return prompt_id
    finally:
        for handle in opened:
            handle.close()


def _wait_result(
    prompt_id: str, *, require_video: bool = True
) -> tuple[list[Path], dict[str, Path]]:
    deadline = time.monotonic() + JOB_TIMEOUT
    consecutive_request_failures = 0
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{INTERNAL_API}/api/tasks/{prompt_id}", timeout=30)
            response.raise_for_status()
            consecutive_request_failures = 0
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            consecutive_request_failures += 1
            if consecutive_request_failures >= MAX_INTERNAL_POLL_FAILURES:
                raise RuntimeError(
                    f"H3 状态接口连续 {consecutive_request_failures} 次返回服务错误"
                ) from exc
            time.sleep(min(2**min(consecutive_request_failures - 1, 4), 15))
            continue
        except httpx.TransportError as exc:
            consecutive_request_failures += 1
            if consecutive_request_failures >= MAX_INTERNAL_POLL_FAILURES:
                raise RuntimeError(
                    f"H3 状态接口连续 {consecutive_request_failures} 次连接失败"
                ) from exc
            time.sleep(min(2**min(consecutive_request_failures - 1, 4), 15))
            continue
        result = response.json()
        if result.get("status") == "error":
            errors = []
            for event in result.get("messages") or []:
                if not isinstance(event, list) or len(event) != 2 or event[0] != "execution_error":
                    continue
                info = event[1] if isinstance(event[1], dict) else {}
                errors.append(
                    f"{info.get('exception_type') or 'ExecutionError'}: "
                    f"{info.get('exception_message') or '未知错误'} "
                    f"(node={info.get('node_id')}, class={info.get('node_type')})"
                )
            raise RuntimeError("; ".join(errors) or "H3 生成失败")
        if result.get("status") == "completed":
            paths = []
            for item in result.get("files") or []:
                filename = _safe_name(str(item.get("filename") or ""), "output.mp4")
                subfolder = Path(str(item.get("subfolder") or ""))
                target = (OUTPUT / subfolder / filename).resolve()
                if OUTPUT.resolve() not in target.parents or not target.is_file():
                    raise RuntimeError("H3 返回了无效的输出路径")
                paths.append(target)
            latents: dict[str, Path] = {}
            for item in result.get("latents") or []:
                role = str(item.get("role") or "")
                relative = str(item.get("path") or "")
                if not relative:
                    relative = (
                        (str(item.get("subfolder") or "") + "/" + str(item.get("filename") or ""))
                        .lstrip("/")
                    )
                target = (OUTPUT / relative).resolve()
                if role in {"video", "audio"} and OUTPUT.resolve() in target.parents and target.is_file():
                    latents[role] = target
            if require_video and not paths:
                raise RuntimeError("H3 任务完成但没有视频文件")
            if not require_video and not latents:
                raise RuntimeError("H3 中间阶段完成但没有 Latent")
            return paths, latents
        time.sleep(2)
    raise TimeoutError("H3 视频生成超时")


def _wait(prompt_id: str) -> list[Path]:
    """Compatibility wrapper retained for the standard polling contract."""
    return _wait_result(prompt_id)[0]


def _submit_latent_refine(payload: dict, latents: dict[str, Path]) -> str:
    missing = {"video", "audio"} - set(latents)
    if missing:
        raise RuntimeError(f"首轮没有返回可用的 AV Latent: {sorted(missing)}")
    with latents["video"].open("rb") as video, latents["audio"].open("rb") as audio:
        response = httpx.post(
            INTERNAL_API + ROUTES["latent_refine"],
            data=payload,
            files={
                "video_latent": (latents["video"].name, video, "application/octet-stream"),
                "audio_latent": (latents["audio"].name, audio, "application/octet-stream"),
            },
            timeout=300,
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"H3 高清阶段提交失败 ({response.status_code}): {response.text[:2000]}"
        )
    prompt_id = str(response.json().get("prompt_id") or "")
    if not prompt_id:
        raise RuntimeError("H3 高清阶段没有返回任务编号")
    return prompt_id


def _free_comfy_memory() -> None:
    """Drop completed graph tensors while keeping the loaded model reusable."""
    try:
        response = httpx.post(
            INTERNAL_API.rsplit(":", 1)[0] + ":8188/free",
            json={"unload_models": False, "free_memory": True}, timeout=30,
        )
        response.raise_for_status()
    except Exception:
        # The next prompt can still rely on ComfyUI's normal cache eviction.
        pass


def _submit_long_chunk(
    prompts: list[str], lengths: list[int], ratio: str, seed: int,
    segment_offset: int, latents: dict[str, Path], media: list[dict],
) -> str:
    files = []
    opened = []
    try:
        for item in media:
            handle = item["path"].open("rb")
            opened.append(handle)
            value = (
                item["filename"], handle,
                mimetypes.guess_type(item["filename"])[0]
                or "application/octet-stream",
            )
            files.append((item["field"], value))
        for role in ("video", "audio"):
            path = latents.get(role)
            if path is not None:
                handle = path.open("rb")
                opened.append(handle)
                value = (path.name, handle, "application/octet-stream")
                files.append((f"{role}_latent", value))
        response = httpx.post(
            INTERNAL_API + "/api/generate/long-chunk",
            data={
                "prompts_json": json.dumps(prompts, ensure_ascii=False),
                "lengths_json": json.dumps(lengths),
                "ratio": ratio,
                "seed": seed,
                "segment_offset": segment_offset,
            },
            files=files,
            timeout=300,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"H3 长视频分块提交失败 ({response.status_code}): {response.text[:2000]}"
            )
        prompt_id = str(response.json().get("prompt_id") or "")
        if not prompt_id:
            raise RuntimeError("H3 长视频分块没有返回任务编号")
        return prompt_id
    finally:
        for handle in opened:
            handle.close()


def _run_hd_staged(mode: str, payload: dict, media: list[dict], tier: str) -> tuple[str, list[Path]]:
    """Finish each HD rung before submitting the next independent graph."""
    base_mode = mode.removesuffix("_latent_hd")
    base_payload = {
        key: payload[key]
        for key in ("prompt", "duration", "ratio", "quality", "seed", "profile", "video_audio_flags")
        if key in payload
    }
    base_payload["output_mode"] = "latent"
    first_prompt_id = _submit(base_mode, base_payload, media)
    _, latents = _wait_result(first_prompt_id, require_video=False)
    refine_payload = {
        "prompt": payload["prompt"],
        "duration": payload["duration"],
        "ratio": payload["ratio"],
        "quality": payload.get("output_quality", 2.0),
        "seed": payload["seed"],
        "profile": payload["profile"],
        "refine_denoise": payload.get("refine_denoise", 0.35),
        "refine_sigma": 0.7,
    }
    rungs = ["1080p"] if tier == "1080p" else ["1080p", "2k"]
    outputs: list[Path] = []
    final_prompt_id = first_prompt_id
    for index, rung in enumerate(rungs):
        final_rung = index == len(rungs) - 1
        refine_payload["tier"] = rung
        refine_payload["output_mode"] = "video" if final_rung else "latent"
        if rung == "2k":
            raw_final_denoise = os.getenv("H3_2K_FINAL_DENOISE", "").strip()
            if raw_final_denoise:
                final_denoise = float(raw_final_denoise)
                if not 0.05 <= final_denoise <= 0.5:
                    raise ValueError("H3_2K_FINAL_DENOISE 必须在 0.05–0.5 之间")
                refine_payload["refine_denoise"] = final_denoise
        final_prompt_id = _submit_latent_refine(dict(refine_payload), latents)
        outputs, latents = _wait_result(
            final_prompt_id, require_video=final_rung
        )
    return final_prompt_id, outputs


def _direct_local_upload(paths: list[Path], targets: list[dict]) -> list[dict]:
    """Upload large local-transfer outputs around the public edge proxy.

    The signed path/query is still issued by the API.  Only the connection
    endpoint changes to the Tencent host IP, with the original Host header so
    nginx selects the signed API vhost.  This avoids Cloudflare's 524 timeout
    on long MP4 PUTs while keeping the same authorization grant.
    """
    transfer_ip = os.getenv("H3_TRANSFER_HOST_IP", "").strip()
    if not transfer_ip or not targets or any(item.get("bucket") != "local" for item in targets):
        return []
    uploaded = []
    for index, path in enumerate(paths):
        if index >= len(targets):
            raise RuntimeError("视频上传授权数量不足")
        target = targets[index]
        parsed = urlsplit(str(target.get("url") or ""))
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError("视频上传授权地址无效")
        direct_url = urlunsplit(("https", transfer_ip, parsed.path, parsed.query, ""))
        headers = {
            "Host": parsed.netloc,
            "Content-Type": "video/mp4",
            "Content-Length": str(path.stat().st_size),
        }
        with path.open("rb") as handle:
            # Bypass any proxy inherited by the ephemeral GPU container.  A
            # proxy can silently send this request back through the public
            # edge, recreating the 502/524 failure we are avoiding.
            response = httpx.put(
                direct_url, headers=headers, content=handle,
                timeout=1800, verify=False, trust_env=False,
            )
        response.raise_for_status()
        uploaded.append({
            "bucket": target.get("bucket", "local"),
            "key": target.get("key", ""),
            "name": target.get("name", path.name),
        })
    return uploaded


def _upload(job_id: str, paths: list[Path]) -> list[dict]:
    prefix = os.getenv("COS_OUTPUT_PREFIX", "outputs").strip("/")
    try:
        targets = json.loads(os.getenv("H3_OUTPUT_UPLOAD_TARGETS", "[]"))
    except json.JSONDecodeError:
        targets = []
    direct = _direct_local_upload(paths, targets)
    if direct:
        return direct
    return upload_outputs(job_id, paths, prefix)


def _report_failure(job_id: str, error: Exception) -> None:
    """Persist a short internal diagnostic without changing the task state."""
    try:
        target = json.loads(os.getenv("H3_JOB_RESULT_UPLOAD_TARGET", "{}"))
        url = str(target.get("url") or "")
        if not url:
            return
        message = str(error).replace("\n", " ")[:500]
        response = httpx.put(
            url,
            json={"job_id": job_id, "ok": False, "diagnostic": {
                "type": type(error).__name__, "message": message,
            }},
            timeout=15,
        )
        response.raise_for_status()
    except Exception:
        # Diagnostics must never mask the original generation failure.
        return


TIER_DELIVERY = {"720p": None, "1080p": (1920, 1080), "2k": (2560, 1440)}


def _normalize_exact(paths: list[Path], ratio: str, tier: str) -> list[Path]:
    """Crop the model's 32-aligned raster to the tier's exact delivery frame."""
    delivery = TIER_DELIVERY.get(tier)
    if not delivery:
        return paths
    width, height = delivery
    if ratio == "9:16":
        width, height = height, width
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={width}:{height},setsar=1"
    )
    for path in paths:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if probe.stdout.strip() == f"{width}x{height}":
            continue
        temporary = path.with_name(f"{path.stem}.exact-1080p.tmp.mp4")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(path), "-vf", video_filter,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-c:a", "copy", "-movflags", "+faststart", str(temporary),
                ],
                check=True,
                timeout=600,
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return paths


def _finalize_long_output(paths: list[Path], duration: float, folder: Path) -> Path:
    """Create one exact-duration MP4 from memory-bounded Comfy chunks."""
    if not paths:
        raise RuntimeError("长视频没有可合并的分块")
    target = folder / "long-video-final.mp4"
    if len(paths) > 1:
        overlap = 4 / 24
        durations = []
        for path in paths:
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(path),
                ],
                check=True, capture_output=True, text=True, timeout=30,
            )
            durations.append(float(probe.stdout.strip()))
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for path in paths:
            command.extend(["-i", str(path)])
        filters = []
        for index in range(len(paths)):
            # The GPU graph already removes the 18-frame motion-context head
            # from every continuation.  Only the continuation clip keeps the
            # deliberate four-frame seam overlap.  Trimming the first clip as
            # well shifts every later clip 4/24 s ahead of the original audio.
            kept_duration = (
                max(0.1, durations[index] - overlap)
                if index > 0 else durations[index]
            )
            filters.extend([
                f"[{index}:v]trim=duration={kept_duration:.6f},"
                f"setpts=PTS-STARTPTS[v{index}]",
                f"[{index}:a]atrim=duration={kept_duration:.6f},"
                f"asetpts=PTS-STARTPTS[a{index}]",
            ])
        concat_inputs = "".join(
            f"[v{index}][a{index}]" for index in range(len(paths))
        )
        filters.append(
            f"{concat_inputs}concat=n={len(paths)}:v=1:a=1[vcat][acat]"
        )
        timeline = sum(durations) - overlap * (len(paths) - 1)
        pad = max(0.0, duration - timeline + 0.1)
        filters.extend([
            f"[vcat]tpad=stop_mode=clone:stop_duration={pad:.6f}[vout]",
            f"[acat]apad=pad_dur={pad:.6f}[aout]",
        ])
        command.extend([
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", "[aout]", "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(target),
        ])
        subprocess.run(command, check=True, timeout=2400)
        return target

    source = paths[0]
    copy_command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-t", f"{duration:.3f}", "-c", "copy", "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart", str(target),
    ]
    subprocess.run(copy_command, check=True, timeout=900)
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(target),
        ],
        check=True, capture_output=True, text=True, timeout=30,
    )
    actual_duration = float(probe.stdout.strip())
    if actual_duration < duration - 0.02:
        temporary = folder / "long-video-reencoded.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-vf",
                f"tpad=stop_mode=clone:stop_duration={max(0.0, duration - actual_duration):.6f}",
                "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "18", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(temporary),
            ],
            check=True, timeout=1800,
        )
        temporary.replace(target)
    source.unlink(missing_ok=True)
    return target


def _run_long_chunked(
    payload: dict, folder: Path, media: list[dict]
) -> tuple[str, list[Path]]:
    """Render linked text/image segments in memory-bounded Comfy graphs."""
    from long_video import segment_lengths

    prompts = json.loads(payload["prompts_json"])
    lengths, _ = segment_lengths(float(payload["duration"]))
    if len(prompts) != len(lengths):
        raise ValueError(
            f"长视频提示词数量 {len(prompts)} 与帧计划 {len(lengths)} 不一致"
        )
    chunks: list[Path] = []
    latent_files: set[Path] = set()
    previous_latents: dict[str, Path] = {}
    prompt_id = ""
    chunk_size = 3
    try:
        for offset in range(0, len(prompts), chunk_size):
            if offset:
                _free_comfy_memory()
            chunk_media = media if offset == 0 else []
            prompt_id = _submit_long_chunk(
                prompts[offset:offset + chunk_size], lengths[offset:offset + chunk_size],
                str(payload["ratio"]), int(payload["seed"]), offset,
                previous_latents, chunk_media,
            )
            outputs, previous_latents = _wait_result(prompt_id)
            if len(outputs) != 1 or set(previous_latents) != {"video", "audio"}:
                raise RuntimeError(
                    "长视频分块必须返回一个视频及完整的 AV Latent"
                )
            chunks.append(outputs[0])
            latent_files.update(previous_latents.values())
        final = _finalize_long_output(chunks, float(payload["duration"]), folder)
        return prompt_id, [final]
    finally:
        for path in latent_files:
            path.unlink(missing_ok=True)
        for path in chunks:
            if path != folder / "long-video-final.mp4":
                path.unlink(missing_ok=True)


def _run_long_single_window(
    mode: str, payload: dict, folder: Path, media: list[dict]
) -> tuple[str, list[Path]]:
    """Render the complete 16–45 second timeline in one ComfyUI graph."""
    prompt_id = _submit(mode, payload, media)
    outputs = _wait(prompt_id)
    if len(outputs) != 1:
        raise RuntimeError("单窗口长视频必须返回一个视频")
    final = _finalize_long_output(
        outputs, float(payload["duration"]), folder
    )
    return prompt_id, [final]


def _payload(value: dict) -> tuple[str, dict]:
    mode = str(value.get("mode") or "")
    if mode not in ROUTES:
        raise ValueError(
            "mode 必须是 text、image、reference、text_latent_hd、"
            "image_latent_hd、reference_latent_hd、enhance、rtx_only、"
            "long_text、long_image 或 digital_human_workflow"
        )
    if mode == "digital_human_workflow":
        workflow_id = str(value.get("workflow_id") or "")
        duration = float(value.get("duration") or 0)
        ratio = str(value.get("ratio") or "16:9")
        megapixels = float(value.get("megapixels") or 1.0)
        if workflow_id != "digital_human_image_audio_v1":
            raise ValueError("数字人工作流标识无效")
        if not 1 <= duration <= float(os.getenv("H3_LONG_MAX_DURATION_SECONDS", "60")):
            raise ValueError("数字人音频时长超出安全范围")
        if ratio not in {"16:9", "9:16"} or megapixels != 1.0:
            raise ValueError("数字人画幅仅支持 16:9 或 9:16，清晰度固定为 1M")
        prompt = value.get("prompt") or ""
        if not isinstance(prompt, str) or len(prompt) > 40000:
            raise ValueError("数字人 H3 提示词格式无效或过长")
        return mode, {
            "mode": mode,
            "workflow_id": workflow_id,
            "duration": duration,
            "ratio": ratio,
            "megapixels": megapixels,
            "prompt": prompt,
        }
    if mode in {"long_text", "long_image"}:
        prompts = value.get("segment_prompts")
        if not isinstance(prompts, list) or not all(isinstance(item, str) for item in prompts):
            raise ValueError("segment_prompts 必须是字符串列表")
        if len(prompts) < 2 or any(not item.strip() for item in prompts):
            raise ValueError("长视频分段提示词数量或内容无效")
        duration = float(value.get("duration") or 0)
        minimum = 16
        if not minimum <= duration <= int(os.getenv("H3_LONG_MAX_DURATION_SECONDS", "60")):
            raise ValueError("长视频时长超出安全范围")
        ratio = str(value.get("ratio") or "16:9")
        if ratio not in {"16:9", "9:16"}:
            raise ValueError("ratio 只能是 16:9 或 9:16")
        return mode, {
            "mode": mode,
            "prompts_json": json.dumps(prompts, ensure_ascii=False),
            "duration": duration,
            "ratio": ratio,
            "seed": int(value.get("seed") or 0),
        }
    prompt = str(value.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    payload = {
        "prompt": prompt,
        "duration": value.get("duration", 5),
        "ratio": value.get("ratio", "16:9"),
        "quality": value.get("quality", 0.4),
        "seed": value.get("seed", 0),
        "profile": "standard",
    }
    if mode in {"reference", "reference_latent_hd"}:
        payload["video_audio_flags"] = str(value.get("video_audio_flags") or "")
    if mode.endswith("_latent_hd"):
        payload["output_quality"] = value.get("output_quality", 2.0)
        payload["refine_denoise"] = value.get("refine_denoise", 0.35)
        # 档位决定逐级二采阶梯；老调用方不传时保持 1080p 单级行为。
        payload["tier"] = str(value.get("tier") or "1080p")
        payload["refine_steps"] = value.get("refine_steps", 2)
        payload["refine_extra_steps"] = value.get("refine_extra_steps", 0)
    return mode, payload


def handler(job: dict) -> dict:
    value = job.get("input") or {}
    job_id = str(job.get("id") or uuid.uuid4().hex)
    folder = None
    outputs: list[Path] = []
    before_inputs = set(INPUT.glob("*"))
    try:
        try:
            mode, payload = _payload(value)
            media = value.get("media") or []
            if not isinstance(media, list):
                raise TypeError("media 必须是列表")
            folder, prepared = _prepare_media(job_id, media)
            fields = [item["field"] for item in prepared]
            if mode == "long_text" and fields:
                raise ValueError("纯文生长视频不能包含素材")
            if mode == "long_image" and fields != ["first_frame"]:
                raise ValueError("图生长视频必须且只能提供一个 first_frame")
            source_audio = None
            if mode == "digital_human_workflow":
                image_items = [item for item in prepared if item["field"] == "images"]
                audio_items = [item for item in prepared if item["field"] == "audios"]
                if fields != ["images", "audios"] or len(image_items) != 1 or len(audio_items) != 1:
                    raise ValueError("数字人工作流需要一张人物图和一段原始音频")
                source_audio = audio_items[0]["path"]
        except Exception as exc:
            raise RuntimeError(f"stage=input_prepare: {type(exc).__name__}: {exc}") from exc
        try:
            tier = str(value.get("tier") or "1080p")
            if mode == "digital_human_workflow":
                prompt_id = _submit(mode, payload, prepared)
                outputs = _wait(prompt_id)
                if len(outputs) != 1:
                    raise RuntimeError("数字人工作流必须返回一个视频")
            elif mode in {"long_text", "long_image"}:
                if float(payload["duration"]) <= 45:
                    prompt_id, outputs = _run_long_single_window(
                        mode, payload, folder, prepared
                    )
                else:
                    prompt_id, outputs = _run_long_chunked(
                        payload, folder, prepared
                    )
            elif (
                mode.endswith("_latent_hd")
                and tier in {"1080p", "2k"}
                and _use_staged_hd()
            ):
                prompt_id, outputs = _run_hd_staged(mode, payload, prepared, tier)
            else:
                if mode in {"text", "image", "reference"}:
                    payload["output_mode"] = "video"
                prompt_id = _submit(mode, payload, prepared)
        except Exception as exc:
            raise RuntimeError(f"stage=submit: {type(exc).__name__}: {exc}") from exc
        if not outputs:
            try:
                outputs = _wait(prompt_id)
            except Exception as exc:
                raise RuntimeError(f"stage=generation: {type(exc).__name__}: {exc}") from exc
        if mode == "digital_human_workflow" and source_audio is not None:
            try:
                outputs = [_preserve_source_audio(outputs[0], source_audio, payload["duration"])]
            except Exception as exc:
                raise RuntimeError(f"stage=preserve_original_audio: {type(exc).__name__}: {exc}") from exc
        if mode.endswith("_latent_hd") or mode == "rtx_only":
            tier = str(value.get("tier") or "1080p")
            try:
                outputs = _normalize_exact(
                    outputs, str(value.get("ratio") or "16:9"), tier
                )
            except Exception as exc:
                raise RuntimeError(f"stage=normalize_{tier}: {type(exc).__name__}: {exc}") from exc
        try:
            artifacts = _upload(job_id, outputs)
        except Exception as exc:
            raise RuntimeError(f"stage=output_upload: {type(exc).__name__}: {exc}") from exc
        return {"ok": True, "prompt_id": prompt_id, "artifacts": artifacts}
    except Exception as exc:
        _report_failure(job_id, exc)
        raise
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        for path in set(INPUT.glob("*")) - before_inputs:
            if path.is_file():
                path.unlink(missing_ok=True)
        for path in outputs:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
