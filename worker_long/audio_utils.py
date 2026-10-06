"""Lossless source-audio mux helpers for generated videos."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

def _preserve_source_audio(video: Path, source_audio: Path, duration: float) -> Path:
    """Replace generated audio with the original track, losslessly where needed."""
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1",
        str(source_audio),
    ], check=True, capture_output=True, text=True, timeout=30)
    codec = probe.stdout.strip().lower()
    duration_probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(source_audio),
    ], check=True, capture_output=True, text=True, timeout=30)
    trim_audio = float(duration_probe.stdout.strip()) > duration + 0.001
    target = video.with_name("long-video-source-audio.mp4")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
        "-i", str(source_audio), "-map", "0:v:0", "-map", "1:a:0",
        "-frames:v", str(math.ceil(duration * 24)), "-c:v", "copy",
    ]
    if codec == "aac" and not trim_audio:
        # AAC is already the widely supported MP4 audio format, so keep its
        # original compressed packets without touching the uploaded sound.
        command.extend(["-c:a", "copy"])
    else:
        # MP3-in-MP4 and ALAC-in-MP4 do not play in every browser/player.
        # Put compatible AAC first, while retaining the exact uploaded audio
        # as a second track (packet-copy, or lossless ALAC after a trim).
        command.extend(["-map", "1:a:0", "-c:a:0", "aac", "-b:a:0", "256k"])
        if trim_audio:
            filters = f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS"
            command.extend(["-filter:a:0", filters, "-filter:a:1", filters])
        command.extend([
            "-c:a:1", "alac" if trim_audio or codec not in {"mp3", "alac", "ac3", "eac3"} else "copy",
            "-disposition:a:0", "default", "-disposition:a:1", "0",
        ])
    command.extend(["-movflags", "+faststart", str(target)])
    subprocess.run(command, check=True, timeout=600)
    target.replace(video)
    return video
