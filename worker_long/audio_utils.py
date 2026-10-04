"""Audio splitting and lossless source-audio mux for long digital-human jobs."""

from __future__ import annotations

import subprocess
import math
from pathlib import Path


def _split_avatar_audio(
    source: Path, lengths: list[int], folder: Path,
    target_duration: float | None = None,
) -> list[Path]:
    """Make one H3 reference clip per linked segment, including its overlap head."""
    parts = []
    delivered_frames = 0
    for index, frames in enumerate(lengths):
        # H3 frame counts land on a 17-frame grid, so a generated 15-second
        # section usually ends a few frames away from the nominal boundary.
        # Align the reference audio to the actual delivered video frame where
        # this section begins, including its 22-frame latent context.
        start_frame = 0 if index == 0 else max(0, delivered_frames - 22)
        start, duration = start_frame / 24, frames / 24
        source_window = (
            duration if target_duration is None
            else max(0.0, min(duration, target_duration - start))
        )
        if source_window <= 0:
            raise ValueError("数字人音频分段超出目标时长")
        target = folder / f"avatar-audio-{index:02d}.wav"
        # Trim on the decoded stream.  Packet seeking is inaccurate for MP3
        # (the common upload format) and can move a continuation window by a
        # packet or more, which is enough to make the second segment's mouth
        # drift against the original speech.  The audio reference must start
        # at the exact latent overlap boundary.
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-t", f"{duration:.6f}",
            "-vn", "-af", (
                f"atrim=start={start:.6f}:duration={source_window:.6f},"
                "asetpts=PTS-STARTPTS,"
                f"apad=whole_dur={duration:.6f}"
            ), "-ar", "48000",
            "-ac", "2", "-c:a", "pcm_s16le", str(target),
        ], check=True, timeout=300)
        parts.append(target)
        delivered_frames += frames if index == 0 else frames - 22
    return parts


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
