"""One-shot IndexTTS2 worker for a Gongji Job; independent of video workers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import httpx

try:
    from .workflow import DEFAULT_WORKFLOW, inference_kwargs, validate_request
except ImportError:  # One-shot container invokes /app/worker.py as a script.
    from workflow import DEFAULT_WORKFLOW, inference_kwargs, validate_request


MAX_REFERENCE_BYTES = 50 * 1024 * 1024
REQUIRED_MODEL_FILES = (
    "config.yaml", "bpe.model", "gpt.pth", "s2mel.pth",
    "wav2vec2bert_stats.pt", "feat1.pt", "feat2.pt",
    "hf_cache/semantic_codec_model.safetensors",
    "hf_cache/campplus_cn_common.bin",
    "hf_cache/bigvgan/config.json", "hf_cache/bigvgan/bigvgan_generator.pt",
)
REQUIRED_MODEL_DIRS = ("hf_cache/w2v-bert-2.0",)


def env_json(name: str, default):
    try:
        return json.loads(os.getenv(name, ""))
    except (TypeError, json.JSONDecodeError):
        return default


def download(url: str, dest: Path) -> None:
    written = 0
    with httpx.stream("GET", url, timeout=180, follow_redirects=True) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                written += len(chunk)
                if written > MAX_REFERENCE_BYTES:
                    raise ValueError("参考音频超过 50 MB")
                handle.write(chunk)
    if not written:
        raise ValueError("参考音频为空")


def crop_reference(source: Path, target: Path) -> None:
    p = DEFAULT_WORKFLOW
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", str(p.reference_start_seconds), "-i", str(source),
         "-t", str(p.reference_end_seconds - p.reference_start_seconds),
         "-ac", "1", "-ar", "24000", str(target)],
        check=True, timeout=90,
    )
    if not target.is_file() or target.stat().st_size < 2048:
        raise ValueError("参考音频不足以提取有效人声")


def _put(url: str, data: bytes, content_type: str) -> None:
    httpx.put(url, content=data, headers={"Content-Type": content_type}, timeout=180).raise_for_status()


def safe_error(exc: BaseException) -> str:
    value = f"{type(exc).__name__}: {exc}"
    value = re.sub(r"https?://\S+", "[url]", value)
    return value[:1500]


def run(request: dict, job_id: str) -> None:
    text = validate_request(str(request.get("text") or ""), float(request.get("emotion_weight", 1)))
    media = request.get("media") or []
    voice_url = next((str(item.get("url")) for item in media if item.get("field") == "voice"), "")
    emotion_url = next((str(item.get("url")) for item in media if item.get("field") == "emotion"), "")
    if not voice_url:
        raise ValueError("缺少音色参考音频")
    targets = env_json("H3_OUTPUT_UPLOAD_TARGETS", [])
    result_target = env_json("H3_JOB_RESULT_UPLOAD_TARGET", {})
    if not targets or not targets[0].get("url") or not result_target.get("url"):
        raise ValueError("缺少输出上传地址")
    model_dir = Path(os.getenv("INDEXTTS2_MODEL_DIR", "/models/TTS/IndexTTS-2"))
    missing = [name for name in REQUIRED_MODEL_FILES if not (model_dir / name).is_file()]
    missing.extend(name for name in REQUIRED_MODEL_DIRS if not (model_dir / name).is_dir())
    if missing:
        raise FileNotFoundError(f"IndexTTS-2 模型盘缺少文件: {', '.join(missing[:5])}")
    with tempfile.TemporaryDirectory(prefix="equai-voice-") as folder:
        work = Path(folder)
        raw_voice, voice = work / "voice_input", work / "voice.wav"
        download(voice_url, raw_voice)
        crop_reference(raw_voice, voice)
        emotion = None
        if emotion_url:
            raw_emotion, emotion = work / "emotion_input", work / "emotion.wav"
            download(emotion_url, raw_emotion)
            crop_reference(raw_emotion, emotion)
        # Import only after validating media and model presence, so rejected
        # requests do not spend minutes initializing GPU weights.
        from indextts.infer_v2 import IndexTTS2

        tts = IndexTTS2(
            cfg_path=str(model_dir / "config.yaml"), model_dir=str(model_dir),
            device="cuda:0", use_fp16=False, use_cuda_kernel=False,
            use_deepspeed=False, use_qwen_emo=False,
            aux_paths={
                "w2v_bert": str(model_dir / "hf_cache/w2v-bert-2.0"),
                "semantic_codec": str(model_dir / "hf_cache/semantic_codec_model.safetensors"),
                "campplus": str(model_dir / "hf_cache/campplus_cn_common.bin"),
                "bigvgan": str(model_dir / "hf_cache/bigvgan"),
            },
        )
        wav, mp3 = work / "speech.wav", work / "speech.mp3"
        tts.infer(
            spk_audio_prompt=str(voice), text=text, output_path=str(wav),
            **inference_kwargs(emotion_audio=str(emotion) if emotion else None,
                               emotion_weight=float(request.get("emotion_weight", 1))),
        )
        if not wav.is_file() or wav.stat().st_size < 2048:
            raise RuntimeError("IndexTTS-2 未生成有效音频")
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(wav), "-codec:a", "libmp3lame", "-q:a", "2", str(mp3)],
            check=True, timeout=120,
        )
        if not mp3.is_file() or mp3.stat().st_size < 2048:
            raise RuntimeError("IndexTTS-2 输出的 MP3 音频为空或过短")
        _put(targets[0]["url"], mp3.read_bytes(), "audio/mpeg")
        _put(result_target["url"], json.dumps({
            "ok": True, "job_id": job_id, "outputs": ["speech.mp3"],
        }).encode(), "application/json")


def main() -> int:
    request = env_json("H3_JOB_INPUT", {})
    job_id = str(os.getenv("H3_JOB_ID") or request.get("job_id") or "voice")
    try:
        run(request, job_id)
        return 0
    except Exception as exc:
        target = env_json("H3_JOB_RESULT_UPLOAD_TARGET", {}).get("url")
        if target:
            try:
                _put(target, json.dumps({
                    "ok": False, "job_id": job_id, "stage": "voice_clone",
                    "error": safe_error(exc),
                    "trace": safe_error(RuntimeError(traceback.format_exc()[-8000:])),
                }, ensure_ascii=False).encode(), "application/json")
            except Exception:
                pass
        print(safe_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
