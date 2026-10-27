"""Small, auditable adapter for the supplied IndexTTS2 ComfyUI workflow.

The uploaded UI graph contains a fixed example recording and a long example
script.  Production calls replace those values with per-request inputs.  The
IndexTTS2 inference API runs the same synthesis node without starting the H3
video ComfyUI image or loading its unrelated models.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VoiceWorkflow:
    # IndexTTS2Run (#12) widget values in the supplied graph.
    top_k: int = 30
    top_p: float = 0.8
    temperature: float = 0.8
    num_beams: int = 3
    max_mel_tokens: int = 1500
    max_text_tokens_per_segment: int = 120
    emotion_weight: float = 1.0
    reference_start_seconds: float = 0.0
    reference_end_seconds: float = 12.0


DEFAULT_WORKFLOW = VoiceWorkflow()


def validate_request(text: str, emotion_weight: float = 1.0) -> str:
    clean = text.strip()
    if not clean or len(clean) > 20_000:
        raise ValueError("朗读文本长度须为 1–20000 个字符")
    if not 0 <= emotion_weight <= 2:
        raise ValueError("情绪强度须在 0–2 之间")
    return clean


def inference_kwargs(*, emotion_audio: str | None, emotion_weight: float) -> dict:
    """Map graph node #12's public controls to the official IndexTTS2 API."""
    p = DEFAULT_WORKFLOW
    return {
        "emo_audio_prompt": emotion_audio,
        "emo_alpha": emotion_weight if emotion_audio else 1.0,
        "top_k": p.top_k,
        "top_p": p.top_p,
        "temperature": p.temperature,
        "num_beams": p.num_beams,
        "max_mel_tokens": p.max_mel_tokens,
        "max_text_tokens_per_segment": p.max_text_tokens_per_segment,
    }
