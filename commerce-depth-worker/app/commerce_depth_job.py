"""Prepare a Tencent-generated 360p commerce clip for the H3 reference graph.

This runs inside the same one-shot Gongji container that later runs H3.  It
keeps the color source out of ComfyUI: only the derived grayscale clip and the
uploaded role images are placed in the final H3_JOB_INPUT.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from fractions import Fraction
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

import httpx

ROOT = Path("/tmp/h3-commerce-depth")
_configured_model_dir = Path(os.getenv(
    "H3_COMMERCE_DEPTH_MODEL_DIR",
    "/tmp/h3-commerce-depth/depth-anything-v2-small",
))
# Some Gongji model disks are read-only. Never try to put the temporary
# Depth Anything download, extraction staging directory, or lock under /models.
MODEL_DIR = (
    Path("/tmp/h3-commerce-depth/depth-anything-v2-small")
    if _configured_model_dir.is_relative_to(Path("/models"))
    else _configured_model_dir
)
MAX_SOURCE_BYTES = 512 * 1024 * 1024


def _run(command: list[str], *, timeout: int = 300) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def _probe(source: Path) -> tuple[int, int, str, float, bool]:
    info = json.loads(_run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source),
    ], timeout=30))
    video = next((item for item in info.get("streams", []) if item.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("360p 工作视频没有视频流")
    width, height = int(video["width"]), int(video["height"])
    if min(width, height) > 360:
        raise RuntimeError("腾讯云传入的视频不是 360p 工作副本")
    fps = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
    try:
        if float(Fraction(fps)) <= 0:
            raise ValueError
    except (ValueError, ZeroDivisionError):
        raise RuntimeError("360p 工作视频帧率无效")
    seconds = float(info.get("format", {}).get("duration") or 0)
    if not 0 < seconds <= 15.5:
        raise RuntimeError("源视频时长无效")
    has_audio = any(item.get("codec_type") == "audio" for item in info.get("streams", []))
    return width, height, fps, seconds, has_audio


def _download(url: str, target: Path) -> None:
    written = 0
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                written += len(chunk)
                if written > MAX_SOURCE_BYTES:
                    raise ValueError("360p 源视频超过大小限制")
                handle.write(chunk)
    if written == 0:
        raise RuntimeError("无法下载 360p 工作视频")


def _ensure_model() -> None:
    if (MODEL_DIR / "config.json").is_file() and (MODEL_DIR / "model.safetensors").is_file():
        return
    MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
    lock_path = MODEL_DIR.parent / ".depth-anything-v2-small.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if (MODEL_DIR / "config.json").is_file() and (MODEL_DIR / "model.safetensors").is_file():
            return
        url = os.getenv("H3_COMMERCE_DEPTH_MODEL_URL", "").strip()
        if not url:
            raise RuntimeError("共绩模型盘没有深度模型缓存，且未配置模型包地址")
        archive = ROOT / "depth-anything-v2-small.tar.gz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        _download(url, archive)
        staging = MODEL_DIR.with_name(MODEL_DIR.name + ".staging")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                destination = (staging / member.name).resolve()
                if staging.resolve() not in destination.parents and destination != staging.resolve():
                    raise RuntimeError("深度模型包包含非法路径")
            bundle.extractall(staging)
        if not (staging / "config.json").is_file() or not (staging / "model.safetensors").is_file():
            raise RuntimeError("深度模型包不完整")
        shutil.rmtree(MODEL_DIR, ignore_errors=True)
        staging.replace(MODEL_DIR)
        archive.unlink(missing_ok=True)


def _read_frame(pipe, size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = pipe.read(size - len(chunks))
        if not chunk:
            if not chunks:
                return None
            raise RuntimeError("视频解码返回了不完整帧")
        chunks.extend(chunk)
    return bytes(chunks)


def _gray_frame(depth):
    import numpy as np

    values = np.asarray(depth, dtype=np.float32)
    low, high = np.percentile(values, (2, 98))
    if not np.isfinite(low) or not np.isfinite(high) or high - low < 1e-6:
        return np.zeros(values.shape, dtype=np.uint8)
    return np.round(np.clip((values - low) / (high - low), 0, 1) * 255).astype(np.uint8)


def _make_depth_video(source: Path, muted: Path, width: int, height: int, fps: str) -> int:
    import numpy as np
    import torch
    from transformers import AutoImageProcessor, AutoModelForDepthEstimation

    _ensure_model()
    torch.set_num_threads(max(1, int(os.getenv("H3_DEPTH_CPU_THREADS", "2"))))
    processor = AutoImageProcessor.from_pretrained(str(MODEL_DIR), local_files_only=True)
    model = AutoModelForDepthEstimation.from_pretrained(
        str(MODEL_DIR), local_files_only=True, torch_dtype=torch.float16,
    ).to("cuda").eval()

    decoder = subprocess.Popen([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-map", "0:v:0",
        "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    muted.parent.mkdir(parents=True, exist_ok=True)
    encoder = subprocess.Popen([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
        "-s:v", f"{width}x{height}", "-r", fps, "-i", "pipe:0", "-an", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(muted),
    ], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    frames_per_batch = max(1, min(8, int(os.getenv("H3_DEPTH_BATCH_SIZE", "8"))))
    frame_bytes = width * height * 3
    processed = 0
    try:
        while True:
            batch = []
            for _ in range(frames_per_batch):
                raw = _read_frame(decoder.stdout, frame_bytes)
                if raw is None:
                    break
                batch.append(np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3))
            if not batch:
                break
            inputs = processor(images=batch, return_tensors="pt")
            pixel_values = inputs["pixel_values"].to(device="cuda", dtype=torch.float16)
            with torch.inference_mode():
                prediction = model(pixel_values=pixel_values).predicted_depth
                resized = torch.nn.functional.interpolate(
                    prediction.unsqueeze(1), size=(height, width), mode="bicubic", align_corners=False,
                ).squeeze(1).float().cpu().numpy()
            for depth in resized:
                encoder.stdin.write(_gray_frame(depth).tobytes())
                processed += 1
            if processed == len(batch) or processed % 60 == 0:
                print(f"COMMERCE_DEPTH frames={processed}", flush=True)
        decoder.stdout.close()
        decode_error = decoder.stderr.read().decode("utf-8", "replace") if decoder.stderr else ""
        if decoder.wait() != 0:
            raise RuntimeError(f"360p 视频解码失败：{decode_error[-800:]}")
        encoder.stdin.close()
        encode_error = encoder.stderr.read().decode("utf-8", "replace") if encoder.stderr else ""
        if encoder.wait() != 0:
            raise RuntimeError(f"深度视频编码失败：{encode_error[-800:]}")
    finally:
        for process in (decoder, encoder):
            if process.poll() is None:
                process.kill()
                process.wait()
    if processed == 0 or not muted.is_file() or muted.stat().st_size == 0:
        raise RuntimeError("深度模型没有生成有效视频帧")
    return processed


def _mux_audio(source: Path, muted: Path, target: Path, seconds: float, has_audio: bool) -> None:
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(muted), "-i", str(source), "-map", "0:v:0"]
    if has_audio:
        command += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    else:
        command += ["-an"]
    command += ["-c:v", "libx264", "-crf", "19", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-t", f"{seconds:.3f}", "-movflags", "+faststart", str(target)]
    _run(command, timeout=180)


def prepare(job_input_path: Path, manifest_path: Path, output_dir: Path) -> None:
    payload = json.loads(job_input_path.read_text(encoding="utf-8"))
    if not payload.get("_commerce_depth_worker"):
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return
    media = payload.get("media") or []
    videos = [item for item in media if item.get("field") == "videos"]
    if len(videos) != 1:
        raise RuntimeError("电商复刻必须且只能有一个 360p 工作视频")
    images = [item for item in media if item.get("field") == "images"]
    if any(item.get("field") not in {"videos", "images"} for item in media):
        raise RuntimeError("电商复刻 worker 收到不支持的素材类型")
    output_dir.mkdir(parents=True, exist_ok=True)
    source = output_dir / "source-360.mp4"
    silent = output_dir / "depth-silent.mp4"
    depth = output_dir / "depth-reference.mp4"
    _download(str(videos[0].get("url") or ""), source)
    width, height, fps, seconds, has_audio = _probe(source)
    _make_depth_video(source, silent, width, height, fps)
    _mux_audio(source, silent, depth, seconds, has_audio)
    source.unlink(missing_ok=True)
    silent.unlink(missing_ok=True)
    payload["media"] = [
        {"field": "videos", "filename": "commerce-depth-360.mp4",
         "url": "http://127.0.0.1:18765/depth-reference.mp4"},
        *images,
    ]
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def run() -> int:
    work = ROOT / os.getenv("H3_JOB_ID", "job")
    work.mkdir(parents=True, exist_ok=True)
    input_path = work / "input.json"
    manifest = work / "prepared.json"
    media_dir = work / "media"
    original_input = os.environ.get("H3_JOB_INPUT") or os.environ.get("H3_COMMERCE_ORIGINAL_INPUT") or "{}"
    input_path.write_text(original_input, encoding="utf-8")
    server = None
    server_thread = None
    base = None
    prepare_process = None
    try:
        env = os.environ.copy()
        env.pop("H3_JOB_INPUT", None)
        env.pop("H3_COMMERCE_ORIGINAL_INPUT", None)
        env["H3_ENABLE_SERVERLESS_HANDLER"] = "0"
        base = subprocess.Popen(["/app/entrypoint.sh"], env=env)
        prepare_process = subprocess.Popen([
            sys.executable, str(Path(__file__).resolve()), "--prepare",
            str(input_path), str(manifest), str(media_dir),
        ], env=env)
        deadline = time.monotonic() + 240
        backend_ready = False
        while time.monotonic() < deadline:
            if base.poll() is not None:
                raise RuntimeError("H3 worker 启动失败")
            if prepare_process.poll() is not None and prepare_process.returncode != 0:
                detail_path = manifest.with_suffix(".error")
                detail = detail_path.read_text(encoding="utf-8") if detail_path.is_file() else ""
                raise RuntimeError(f"共绩深度预处理失败: {detail[:1200] or '子进程无错误详情'}")
            try:
                response = httpx.get("http://127.0.0.1:8000/health", timeout=2)
                backend_ready = response.status_code == 200 and response.json().get("ok") is True
            except Exception:
                backend_ready = False
            if backend_ready:
                break
            time.sleep(1)
        if not backend_ready:
            raise TimeoutError("H3 后端启动超时")
        prepare_process.wait(timeout=max(1, deadline - time.monotonic()))
        if prepare_process.returncode != 0:
            raise RuntimeError("共绩深度预处理失败")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if payload.get("_commerce_depth_worker"):
            handler = lambda *args, **kwargs: _QuietHandler(*args, directory=str(media_dir), **kwargs)
            server = ThreadingHTTPServer(("127.0.0.1", 18765), handler)
            server_thread = Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
        env["H3_JOB_INPUT"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        env.pop("H3_COMMERCE_DEPTH_MODEL_URL", None)
        job = subprocess.run([sys.executable, "/app/job_runner.py"], env=env, check=False)
        return int(job.returncode)
    finally:
        if prepare_process and prepare_process.poll() is None:
            prepare_process.terminate()
            try:
                prepare_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                prepare_process.kill()
                prepare_process.wait()
        if server:
            server.shutdown()
            server.server_close()
        if server_thread:
            server_thread.join(timeout=2)
        if base and base.poll() is None:
            base.terminate()
            try:
                base.wait(timeout=10)
            except subprocess.TimeoutExpired:
                base.kill()
                base.wait()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--prepare":
        try:
            prepare(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
        except Exception as exc:
            detail = re.sub(r"https?://\S+", "[media URL]", str(exc))
            print(f"Depth preparation failed: {type(exc).__name__}: {detail[:1000]}", file=sys.stderr, flush=True)
            Path(sys.argv[3]).with_suffix(".error").write_text(
                f"{type(exc).__name__}: {detail[:1000]}", encoding="utf-8",
            )
            raise SystemExit(1)
    else:
        try:
            raise SystemExit(run())
        except Exception as exc:
            detail = re.sub(r"https?://\S+", "[media URL]", str(exc))
            report = {
                "ok": False,
                "job_id": os.environ.get("H3_JOB_ID", ""),
                "error": f"Depth preprocessing failed: {type(exc).__name__}: {detail[:1000]}",
            }
            print(json.dumps(report, ensure_ascii=False), file=sys.stderr, flush=True)
            try:
                from object_storage import upload_job_result
                upload_job_result(report)
            except Exception as upload_error:
                print(f"Worker failure report upload failed: {type(upload_error).__name__}", file=sys.stderr, flush=True)
            raise SystemExit(1)
