from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import traceback
from pathlib import Path

import httpx

_WORKER_STAGE = "startup"


def set_worker_stage(stage: str) -> None:
    global _WORKER_STAGE
    _WORKER_STAGE = stage
    print(f"music_worker_stage={stage}", flush=True)


def report_worker_stage(target: dict, job_id: str) -> None:
    url = target.get("url")
    if not url:
        return
    try:
        httpx.put(
            url,
            content=json.dumps(
                {"ok": False, "job_id": job_id, "stage": _WORKER_STAGE},
                ensure_ascii=False,
            ).encode(),
            headers={"Content-Type": "application/json"},
            timeout=15,
        ).raise_for_status()
    except Exception as exc:
        print(f"music_worker_stage_report_failed={type(exc).__name__}", flush=True)


def env_json(name: str, default):
    try:
        return json.loads(os.getenv(name, ""))
    except (TypeError, json.JSONDecodeError):
        return default


def model_root() -> Path:
    # The configured h3-models storage already maps COS's
    # `models/h3-standard/` prefix into the container at `/models`.
    root = Path(os.getenv("MUSIC_MODEL_ROOT", "/models/music"))
    release = os.getenv("MUSIC_MODEL_RELEASE", "2026-09-29-yue2-sheetsage2")
    return root / release


def download(url: str, target: Path) -> None:
    with httpx.stream("GET", url, timeout=180) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for block in response.iter_bytes(1024 * 1024):
                handle.write(block)


def resolve_lyrics(lyrics: str, style: str, voice: str) -> str:
    lyrics = lyrics.strip()
    if lyrics:
        return lyrics
    if voice != "无人声" and style != "纯器乐":
        raise RuntimeError("音乐任务缺少歌词")
    return "[Instrumental]"


def configure_hf_modules_cache(work: Path) -> Path:
    cache = work / "hf-modules"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["HF_MODULES_CACHE"] = str(cache)
    return cache


def copy_sheetsage_code(source: Path, work: Path) -> Path:
    target = work / "sheetsage2-code"
    shutil.copytree(source, target, dirs_exist_ok=True)
    return target


def load_sheetsage_model(model_path: Path):
    from transformers import AutoModel

    base_model_path = model_root() / "mert" / "MERT-v2-FullSong"
    if not base_model_path.is_dir():
        raise FileNotFoundError(f"本地 MERT 父模型不存在: {base_model_path}")
    for _ in range(16):
        try:
            return AutoModel.from_pretrained(
                str(model_path),
                trust_remote_code=True,
                local_files_only=True,
                base_model_path=str(base_model_path),
            ).eval().to("cuda")
        except FileNotFoundError as exc:
            missing_name = Path(getattr(exc, "filename", "") or "").name
            if not missing_name.endswith(".py"):
                raise
            source = next((item for item in model_path.rglob(missing_name) if item.is_file()), None)
            missing_path = Path(exc.filename)
            if source is None or missing_path.exists():
                raise
            missing_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, missing_path)
    raise RuntimeError("SheetSage2 remote code cache is incomplete")


def transcribe_score(source: Path, output: Path, model_path: Path) -> None:
    """Run SheetSage2 in the worker's existing Python and CUDA environment."""
    configure_hf_modules_cache(output.parent)
    model = load_sheetsage_model(model_path)
    result = model.transcribe(str(source), output_dir=str(output), melody_only=True)
    score = output / "score.abc"
    if not score.is_file() or not score.stat().st_size or not (result or {}).get("abc"):
        raise RuntimeError("SheetSage2 未生成有效旋律谱")
    del model
    import torch

    torch.cuda.empty_cache()


def _run_job(request: dict, job_id: str) -> int:
    set_worker_stage("parse_request")
    media = request.get("media") or []
    source_url = next((item.get("url") for item in media if item.get("field") == "audios"), None)
    if not source_url:
        raise RuntimeError("音乐任务缺少源音频")
    work = Path(os.getenv("MUSIC_JOB_DIR", "/tmp/music-jobs")) / job_id
    work.mkdir(parents=True, exist_ok=True)
    source = work / str(request.get("source_filename") or "source.mp3")
    set_worker_stage("download_input")
    download(source_url, source)
    style = str(request.get("style") or "摇滚")
    voice = str(request.get("voice") or "不指定")
    lyrics = resolve_lyrics(str(request.get("lyrics") or ""), style, voice)
    score_dir = work / "score"
    score_dir.mkdir(exist_ok=True)
    set_worker_stage("prepare_sheetsage_code")
    sheetsage_path = copy_sheetsage_code(model_root() / "sheetsage2" / "SheetSage2", work)
    set_worker_stage("transcribe_score")
    transcribe_score(source, score_dir, sheetsage_path)
    score = (score_dir / "score.abc").read_text(encoding="utf-8")
    style_parts = [style, voice]
    if request.get("prompt"):
        style_parts.append(str(request["prompt"]).strip())
    request_json = {"style": ", ".join(part for part in style_parts if part), "lyrics": lyrics, "abc": score, "cot": "melody", "seed": 42}
    (work / "lyrics.txt").write_text(lyrics, encoding="utf-8")
    (work / "score.abc").write_text(score, encoding="utf-8")
    (work / "request.json").write_text(json.dumps(request_json, ensure_ascii=False, indent=2), encoding="utf-8")
    from yue2 import YuE2Pipeline
    out = work / "generated"
    out.mkdir(exist_ok=True)
    set_worker_stage("load_yue2")
    with YuE2Pipeline.from_pretrained(str(model_root() / "yue2" / "YuE2-3B"), vae=str(model_root() / "yue2-vae" / "YuE2-Vae"), device="cuda", local_files_only=True) as pipe:
        set_worker_stage("generate_audio")
        song = pipe(**request_json)
        song.save_artifacts(out)
    flac = out / "audio.flac"
    mp3 = work / "song.mp3"
    set_worker_stage("encode_mp3")
    subprocess.run(["ffmpeg", "-y", "-i", str(flac), "-codec:a", "libmp3lame", "-q:a", "2", str(mp3)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    targets = env_json("H3_OUTPUT_UPLOAD_TARGETS", [])
    result_target = env_json("H3_JOB_RESULT_UPLOAD_TARGET", {})
    set_worker_stage("upload_outputs")
    with mp3.open("rb") as handle:
        httpx.put(targets[0]["url"], content=handle, headers={"Content-Type": "audio/mpeg"}, timeout=600).raise_for_status()
    result = {"ok": True, "job_id": job_id, "outputs": ["song.mp3"], "score": str(work / "score.abc"), "lyrics": lyrics}
    httpx.put(result_target["url"], content=json.dumps(result, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"}, timeout=120).raise_for_status()
    return 0


def _safe_error(exc: Exception) -> str:
    return _safe_error_text(f"{type(exc).__name__}: {exc}")


def _safe_error_text(value: str, limit: int = 1500) -> str:
    message = value
    message = re.sub(r"https?://\S+", "[url]", message)
    message = re.sub(r"(?i)(token|password|secret|authorization)\s*[=:]\s*\S+", r"\1=[redacted]", message)
    return message[:limit]


def main() -> int:
    request = env_json("H3_JOB_INPUT", {})
    job_id = str(os.getenv("H3_JOB_ID") or request.get("job_id") or "music")
    result_target = env_json("H3_JOB_RESULT_UPLOAD_TARGET", {})
    try:
        set_worker_stage("worker_started")
        report_worker_stage(result_target, job_id)
        return _run_job(request, job_id)
    except Exception as exc:
        target_url = result_target.get("url")
        if target_url:
            report = {
                "ok": False,
                "job_id": job_id,
                "stage": _WORKER_STAGE,
                "error": _safe_error(exc),
                "trace": _safe_error_text(traceback.format_exc(), limit=8000),
            }
            try:
                httpx.put(
                    target_url,
                    content=json.dumps(report, ensure_ascii=False).encode(),
                    headers={"Content-Type": "application/json"},
                    timeout=30,
                ).raise_for_status()
            except Exception:
                pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
