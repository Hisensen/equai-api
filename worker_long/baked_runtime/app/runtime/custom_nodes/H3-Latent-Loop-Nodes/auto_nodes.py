"""Audio-window helpers for automatic MiniMax H3 latent continuation."""
from __future__ import annotations

import math
from collections.abc import Mapping

import torch
import torch.nn.functional as F



def frame_count_from_video_latent_t(video_t: int) -> int:
    if video_t < 2 or (video_t - 2) % 5 != 0:
        raise ValueError("video latent time must satisfy MiniMax H3's 5k+2 grid")
    return ((video_t - 2) // 5) * 17 + 5

FPS = 24
INITIAL_FRAMES = 362       # 17*21 + 5 ~= 15.083 s
OVERLAP_FRAMES = 22        # 17 + 5 ~= 0.917 s
MAX_EXTENSION_FRAMES = 340 # 17*20 ~= 14.167 s of new video per pass


def _audio_parts(audio: Mapping) -> tuple[torch.Tensor, int]:
    if not isinstance(audio, Mapping) or "waveform" not in audio or "sample_rate" not in audio:
        raise TypeError("audio must contain waveform and sample_rate")
    waveform = audio["waveform"]
    sample_rate = int(audio["sample_rate"])
    if not isinstance(waveform, torch.Tensor) or waveform.ndim != 3:
        raise ValueError("audio waveform must have shape [batch, channels, samples]")
    if waveform.shape[-1] <= 0 or sample_rate <= 0:
        raise ValueError("audio must have samples and a positive sample rate")
    return waveform, sample_rate


def _aligned_frame_count(frame_count: int) -> int:
    frame_count = max(5, int(frame_count))
    remainder = (frame_count - 5) % 17
    return frame_count if remainder == 0 else frame_count + 17 - remainder


def _audio_frame_count(audio: Mapping) -> int:
    waveform, sample_rate = _audio_parts(audio)
    return int(math.ceil(waveform.shape[-1] / sample_rate * FPS))


class H3ContinuationPassCount:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": ("AUDIO",)}}

    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("continuation_passes",)
    FUNCTION = "count"
    CATEGORY = "MiniMax H3/continuation"

    def count(self, audio):
        target = _aligned_frame_count(_audio_frame_count(audio))
        remaining = max(0, target - INITIAL_FRAMES)
        passes = max(1, math.ceil(remaining / MAX_EXTENSION_FRAMES))
        return (passes,)


class H3ContinuationWindowPlan:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio": ("AUDIO",),
                "previous_av_latent": ("LATENT",),
                "overlap_frames": ("INT", {"default": OVERLAP_FRAMES, "min": 5, "max": 362, "step": 17}),
                "max_extension_frames": ("INT", {"default": MAX_EXTENSION_FRAMES, "min": 17, "max": 340, "step": 17}),
            }
        }

    RETURN_TYPES = ("BOOLEAN", "INT", "INT", "INT")
    RETURN_NAMES = ("continue", "audio_start_frame", "extension_frames", "window_frames")
    FUNCTION = "plan"
    CATEGORY = "MiniMax H3/continuation"

    def plan(self, audio, previous_av_latent, overlap_frames=OVERLAP_FRAMES, max_extension_frames=MAX_EXTENSION_FRAMES):
        samples = previous_av_latent.get("samples") if isinstance(previous_av_latent, Mapping) else None
        streams = getattr(samples, "tensors", None)
        if streams is None or len(streams) != 2:
            raise TypeError("previous_av_latent must be a native MiniMax H3 AV latent")
        current_frames = frame_count_from_video_latent_t(int(streams[0].shape[2]))
        target_frames = _aligned_frame_count(_audio_frame_count(audio))
        should_continue = current_frames < target_frames
        if should_continue:
            remaining = target_frames - current_frames
            extension = min(int(max_extension_frames), math.ceil(remaining / 17) * 17)
            extension = max(17, extension)
        else:
            # This is bypassed by the lazy switch; keep the schema-valid target harmless.
            extension = 17
        window_frames = int(overlap_frames) + extension
        if (window_frames - 5) % 17 != 0:
            raise ValueError("overlap_frames + extension_frames must satisfy the H3 17k+5 grid")
        if window_frames > 362:
            raise ValueError("continuation window exceeds the trained 15-second H3 window")
        return (should_continue, current_frames - int(overlap_frames), extension, window_frames)


class H3AudioWindowCrop:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio": ("AUDIO",),
                "start_frame": ("INT", {"default": 0, "min": 0, "max": 1000000}),
                "frame_count": ("INT", {"default": INITIAL_FRAMES, "min": 5, "max": 362}),
            }
        }

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("audio_window",)
    FUNCTION = "crop"
    CATEGORY = "MiniMax H3/continuation"

    def crop(self, audio, start_frame=0, frame_count=INITIAL_FRAMES):
        waveform, sample_rate = _audio_parts(audio)
        start_sample = round(int(start_frame) * sample_rate / FPS)
        end_sample = round((int(start_frame) + int(frame_count)) * sample_rate / FPS)
        start_sample = min(start_sample, waveform.shape[-1])
        end_sample = min(max(start_sample, end_sample), waveform.shape[-1])
        window = waveform[..., start_sample:end_sample]
        wanted = max(1, round(int(frame_count) * sample_rate / FPS))
        if window.shape[-1] < wanted:
            window = F.pad(window, (0, wanted - window.shape[-1]))
        elif window.shape[-1] > wanted:
            window = window[..., :wanted]
        return ({"waveform": window.contiguous(), "sample_rate": sample_rate},)
