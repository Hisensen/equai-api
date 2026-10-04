"""Latent-linked long-form workflow for the isolated H3 worker image."""

from __future__ import annotations

import json
import math
import os
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
import httpx


router = APIRouter()

COMFY = os.getenv("COMFY_URL", "http://127.0.0.1:8188")
FPS = 24
CONTEXT_FRAMES = 22
CROSS_CHUNK_OVERLAP_FRAMES = 4
CROSS_CHUNK_OVERLAP_FRAMES = 4
CONTINUATION_AIRLOCK = (
    "Continue the exact preceding shot without a cut or reset. For the first "
    "two seconds, hold the exact closing framing, subject scale, camera position, "
    "lighting, and ongoing motion trajectory before progressing the requested "
    "action. Never zoom, reframe, pan, cut, or reveal a different view during "
    "this continuity hold. Preserve exact identity, clothing, props, background, "
    "lens, and sound continuity."
)
FIRST_SEGMENT_FRAMES = 362
NORMAL_CONTINUATION_FRAMES = 379
NORMAL_CONTRIBUTION_FRAMES = NORMAL_CONTINUATION_FRAMES - CONTEXT_FRAMES
MAX_FINAL_CONTINUATION_FRAMES = 396
MAX_DURATION = int(os.getenv("H3_LONG_MAX_DURATION_SECONDS", "60"))
ROOT = Path(os.getenv("H3_ROOT", "/app"))
INPUT = ROOT / "runtime" / "input"


def _valid_h3_length_at_least(frames: int) -> int:
    return max(22, math.ceil((frames - 5) / 17) * 17 + 5)


def _valid_h3_length_nearest(frames: int) -> int:
    """Pick the closest legal H3 window for a final continuation.

    Rounding the final window down by a few frames is preferable to making the
    model invent a long silent tail. The finalizer can clone the last frame for
    the small remainder, while the audio remains the uploaded source track.
    """
    estimate = round((frames - 5) / 17)
    return max(22, estimate * 17 + 5)


def segment_lengths(duration: float) -> tuple[list[int], int]:
    """Split into 15-second contributions and add H3's latent overlap to tails."""
    target = math.ceil(duration * FPS)
    if target < FPS:
        raise ValueError("数字人音频不能短于 1 秒")
    count = max(1, math.ceil(duration / 15))
    lengths: list[int] = []
    produced = 0
    for index in range(count):
        contribution = min(15 * FPS, target - produced)
        raw = _valid_h3_length_at_least(
            contribution if index == 0 else contribution + CONTEXT_FRAMES
        )
        if index and raw > MAX_FINAL_CONTINUATION_FRAMES:
            raise ValueError("H3 续段长度超过安全范围")
        lengths.append(raw)
        produced += raw if index == 0 else raw - CONTEXT_FRAMES
    return lengths, produced


def avatar_segment_lengths(duration: float) -> tuple[list[int], int]:
    """Use one H3 window when a short remainder cannot form a valid continuation."""
    complete_windows = int(duration // 15)
    if complete_windows and 0 < duration - complete_windows * 15 < 1:
        duration = float(complete_windows * 15)
    target = math.ceil(duration * FPS)
    if target <= FPS:
        raise ValueError("数字人音频不能短于 1 秒")
    if target <= FIRST_SEGMENT_FRAMES + 17:
        length = _valid_h3_length_at_least(target)
        return [length], length
    count = max(1, math.ceil(duration / 15))
    lengths: list[int] = []
    produced = 0
    for index in range(count):
        if index == 0:
            raw = FIRST_SEGMENT_FRAMES
        elif index < count - 1:
            # 379 - 22 = 357 fresh frames: the closest valid H3 window
            # to 15 seconds without making each intermediate chunk late.
            raw = NORMAL_CONTINUATION_FRAMES
        else:
            raw = _valid_h3_length_nearest(target - produced + CONTEXT_FRAMES)
        if index and raw <= CONTEXT_FRAMES:
            raise ValueError("H3 续段必须包含新的生成帧")
        if index and raw > MAX_FINAL_CONTINUATION_FRAMES:
            raise ValueError("H3 续段长度超过安全范围")
        lengths.append(raw)
        produced += raw if index == 0 else raw - CONTEXT_FRAMES
    return lengths, produced


def dimensions(ratio: str) -> tuple[int, int]:
    if ratio == "16:9":
        return 1280, 736
    if ratio == "9:16":
        return 736, 1280
    raise ValueError("ratio 只能是 16:9 或 9:16")


def long_workflow(
    prompts: list[str], duration: float, ratio: str, seed: int,
    first_frame: str | None = None,
    avatar_image: str | None = None,
    audio_segments: list[str] | None = None,
    selflift: bool = False,
) -> tuple[dict, list[int], int]:
    lengths, rendered_frames = (
        avatar_segment_lengths(duration) if avatar_image
        else segment_lengths(duration)
    )
    if len(prompts) != len(lengths):
        raise ValueError(
            f"{duration} 秒需要 {len(lengths)} 段提示词，当前收到 {len(prompts)} 段"
        )
    width, height = dimensions(ratio)
    if avatar_image:
        return _avatar_long_workflow(
            prompts, lengths, rendered_frames, ratio, seed, avatar_image,
            audio_segments or [], duration, selflift=selflift,
        )
    model = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
    text_encoder = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
    video_vae = "official/minimax_h3_video_vae_int8_convrot.safetensors"
    audio_vae = "minimax_h3_audio_vae_fp32.safetensors"
    lora = "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"

    graph: dict = {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": model, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": text_encoder, "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": video_vae}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": audio_vae}},
        "5": {"class_type": "SolAttnMiniMax", "inputs": {
            "model": ["1", 0], "selection": "adaptive tau", "selection.tau": 1.3,
            "start_percent": 0.0, "end_percent": 1.0, "min_tokens": 0,
            "sink_conditioning": "exact_kv_and_rows", "verbose": False}},
        "6": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["5", 0], "lora_name": lora, "strength_model": 1.0}},
    }
    if first_frame:
        graph["7"] = {
            "class_type": "LoadImage", "inputs": {"image": first_frame}
        }
    previous_sampler: str | None = None
    combined_images: list | None = None
    combined_audio: list | None = None
    for index, (prompt, length) in enumerate(zip(prompts, lengths), start=1):
        base = 100 + (index - 1) * 20
        condition = str(base)
        effective_prompt = (
            prompt if index == 1 else f"{CONTINUATION_AIRLOCK}\n\n{prompt}"
        )
        condition_inputs = {
            "clip": ["2", 0], "vae": ["3", 0], "prompt": effective_prompt,
            "width": width, "height": height, "length": length,
        }
        if index == 1 and first_frame:
            condition_inputs["first_frame"] = ["7", 0]
        graph[condition] = {
            "class_type": "MiniMaxH3ImageToVideo", "inputs": condition_inputs
        }
        trim_source = None
        if index == 1:
            conditioning = [condition, 0]
            noise, guider, scheduler, sampler_select, sampler = (
                str(base + 1), str(base + 2), str(base + 3),
                str(base + 4), str(base + 5),
            )
        else:
            motion = str(base + 1)
            graph[motion] = {"class_type": "MiniMaxH3MotionContext", "inputs": {
                "conditioning": [condition, 0], "vae": ["3", 0],
                "latent": [condition, 1], "context_length": str(CONTEXT_FRAMES),
                "audio_context_length": 24, "context_latent": [previous_sampler, 0]}}
            conditioning = [motion, 0]
            trim_source = motion
            noise, guider, scheduler, sampler_select, sampler = (
                str(base + 2), str(base + 3), str(base + 4),
                str(base + 5), str(base + 6),
            )
        graph[noise] = {"class_type": "RandomNoise", "inputs": {
            "noise_seed": seed + index - 1}}
        graph[guider] = {"class_type": "BasicGuider", "inputs": {
            "model": ["6", 0], "conditioning": conditioning}}
        graph[scheduler] = {"class_type": "BasicScheduler", "inputs": {
            "model": ["6", 0], "scheduler": "beta", "steps": 4,
            "denoise": 1.0}}
        graph[sampler_select] = {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": "euler"}}
        graph[sampler] = {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": [noise, 0], "guider": [guider, 0],
            "sampler": [sampler_select, 0], "sigmas": [scheduler, 0],
            "latent_image": [condition, 1]}}
        previous_sampler = sampler

        decode_video, decode_audio = str(base + 10), str(base + 11)
        graph[decode_video] = {"class_type": "VAEDecode", "inputs": {
            "samples": [sampler, 0], "vae": ["3", 0]}}
        graph[decode_audio] = {"class_type": "VAEDecodeAudio", "inputs": {
            "samples": [sampler, 0], "vae": ["4", 0]}}
        if trim_source is None:
            images, audio = [decode_video, 0], [decode_audio, 0]
        else:
            trim = str(base + 12)
            graph[trim] = {"class_type": "MiniMaxH3MotionContextTrim", "inputs": {
                "images": [decode_video, 0], "trim_frames": [trim_source, 1],
                "audio": [decode_audio, 0], "fps": float(FPS),
                "match_tail": True}}
            images, audio = [trim, 0], [trim, 1]
        if combined_images is None:
            combined_images, combined_audio = images, audio
        else:
            image_batch, audio_concat = str(base + 15), str(base + 16)
            graph[image_batch] = {"class_type": "ImageBatch", "inputs": {
                "image1": combined_images, "image2": images}}
            graph[audio_concat] = {"class_type": "AudioConcat", "inputs": {
                "audio1": combined_audio, "audio2": audio, "direction": "after"}}
            combined_images, combined_audio = [image_batch, 0], [audio_concat, 0]

    # Match the proven long-video graph: concatenate decoded frames and audio
    # inside ComfyUI, then encode once. Encoding each segment independently
    # creates a visible boundary even when the latent overlap is correct.
    graph["9000"] = {"class_type": "CreateVideo", "inputs": {
        "images": combined_images, "audio": combined_audio,
        "fps": float(FPS), "bit_depth": 8}}
    graph["9001"] = {"class_type": "SaveVideo", "inputs": {
        "video": ["9000", 0], "filename_prefix": "h3/long_video_combined",
        "format": "mp4", "codec": "h264", "codec.encoding": "re-encode",
        "codec.encoding.crf": 18.0}}
    return graph, lengths, rendered_frames


def _avatar_long_workflow(
    prompts: list[str], lengths: list[int], rendered_frames: int, ratio: str,
    seed: int, avatar_image: str, audio_segments: list[str], target_duration: float,
    video_latent: str | None = None, audio_latent: str | None = None,
    segment_offset: int = 0, persist_latent: bool = False, selflift: bool = False,
) -> tuple[dict, list[int], int]:
    """Build H3 Easy digital-human contexts while keeping MotionContext AV links."""
    from h3_profiles import get_profile

    width, height = dimensions(ratio)
    profile = get_profile("standard")
    graph: dict = {
        "1": {"class_type": "MiniMaxH3EasyLoader", "inputs": {
            "fl2va_model": profile.fl_model, "ref2va_model": profile.ref_model,
            "text_encoder": profile.text_encoder, "video_vae": profile.video_vae,
            "audio_vae": profile.audio_vae}},
    }
    previous_sampler = None
    if (video_latent is None) != (audio_latent is None):
        raise ValueError("数字人续段必须同时包含视频和音频 Latent")
    if video_latent and audio_latent:
        graph.update({
            "2": {"class_type": "LoadLatent", "inputs": {"latent": video_latent}},
            "3": {"class_type": "LoadLatent", "inputs": {"latent": audio_latent}},
            "4": {"class_type": "LTXVConcatAVLatent", "inputs": {
                "video_latent": ["2", 0], "audio_latent": ["3", 0]}},
        })
        previous_sampler = "4"
    for index, (prompt, length) in enumerate(zip(prompts, lengths)):
        base = 100 + index * 30
        image_node, condition, output = "100", str(base + 1), str(base + 2)
        if index == 0:
            graph[image_node] = {"class_type": "LoadImage", "inputs": {"image": avatar_image}}
        # Multi-window graphs condition each window independently. The chunk
        # endpoint already supplies lengths[0] / FPS as target_duration.
        if target_duration > 15.0 or segment_offset:
            duration = min(30.0, length / FPS)
        else:
            duration = min(30.0, target_duration)
        condition_inputs = {
            "h3_bundle": ["1", 0], "mode": "reference", "prompt": prompt,
            "resolution": "custom", "aspect_ratio": ratio,
            "width": width, "height": height, "seconds": duration,
            "advanced": True, "fps": float(FPS), "keyframe_role": "first",
            "ref_image_size": "match", "reference_mention_mode": "index",
            "prompt_optimizer_settings": False,
            "prompt_optimizer_scene_guide": "none",
            "media_1": [image_node, 0], "media_type_1": "image",
        }
        if audio_segments:
            audio_node = str(base + 3)
            graph[audio_node] = {"class_type": "LoadAudio", "inputs": {"audio": audio_segments[index]}}
            condition_inputs.update({"media_2": [audio_node, 0], "media_type_2": "audio"})
        graph[condition] = {"class_type": "MiniMaxH3Easy", "inputs": condition_inputs}
        graph[output] = {"class_type": "MiniMaxH3EasyOutput", "inputs": {"h3_context": [condition, 1]}}

        # SelfLift's documented H3 path preserves the uploaded audio inside
        # the sampler: encode the exact segment, attach a zero audio noise
        # mask, then concatenate that audio latent with the generated video
        # latent. Merely muxing the source audio after sampling makes the
        # soundtrack correct while leaving the mouth driven by a different
        # generated audio latent.
        if selflift and audio_segments:
            audio_nodes = 1000 + index * 100
            split = str(audio_nodes)
            audio_vae = str(audio_nodes + 1)
            audio_mask = str(audio_nodes + 2)
            audio_masked = str(audio_nodes + 3)
            av_latent = str(audio_nodes + 4)
            graph[split] = {"class_type": "LTXVSeparateAVLatent", "inputs": {
                "av_latent": [output, 1]}}
            graph[audio_vae] = {"class_type": "VAEEncodeAudio", "inputs": {
                "audio": [audio_node, 0], "vae": [output, 3]}}
            graph[audio_mask] = {"class_type": "SolidMask", "inputs": {
                "value": 0.0, "width": width, "height": height}}
            graph[audio_masked] = {"class_type": "SetLatentNoiseMask", "inputs": {
                "samples": [audio_vae, 0], "mask": [audio_mask, 0]}}
            graph[av_latent] = {"class_type": "LTXVConcatAVLatent", "inputs": {
                "video_latent": [split, 0], "audio_latent": [audio_masked, 0]}}
            latent_source = [av_latent, 0]
        else:
            latent_source = [output, 1]

        trim_source = None
        if previous_sampler is None:
            conditioning, latent = [output, 0], latent_source
            offset = 4
        else:
            motion = str(base + 4)
            graph[motion] = {"class_type": "MiniMaxH3MotionContext", "inputs": {
                "conditioning": [output, 0], "vae": [output, 2],
                "latent": latent_source, "context_length": str(CONTEXT_FRAMES),
                "audio_context_length": 24, "context_latent": [previous_sampler, 0]}}
            conditioning, latent, trim_source = [motion, 0], latent_source, motion
            offset = 5
        patch, model, shifted = (str(base + offset + i) for i in range(3))
        graph[patch] = {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": [condition, 0]}}
        # Match the proven 15-second multi-reference sampler. SolAttn belongs
        # on the raw H3 model; the Sage patch remains a fallback when disabled.
        solattn = str(base + 29)
        solattn_config = os.getenv("H3_SOLATTN", "tau:1.3:0.0:1.0").strip()
        model_source = [patch, 0]
        if solattn_config and solattn_config.lower() not in {"off", "0", "false", "no"}:
            mode, value, start, end = solattn_config.split(":")
            if mode not in {"tau", "topk", "vsa"}:
                raise ValueError("数字人 SolAttn 配置无效")
            selection = {"tau": "adaptive tau", "topk": "top-k (SLA)", "vsa": "VSA (FastVideo)"}[mode]
            parameter = {"tau": "tau", "topk": "keep_percent", "vsa": "vsa_keep_percent"}[mode]
            graph[solattn] = {"class_type": "SolAttnMiniMax", "inputs": {
                "model": [condition, 0], "selection": selection,
                f"selection.{parameter}": float(value),
                "start_percent": float(start), "end_percent": float(end),
                "min_tokens": 0, "sink_conditioning": "exact_kv_and_rows", "verbose": False,
            }}
            model_source = [solattn, 0]
        graph[model] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": model_source, "lora_name": profile.ref_lora,
            "strength_model": 1.0}}
        graph[shifted] = {"class_type": "MiniMaxH3SigmaShift", "inputs": {
            "model": [model, 0], "shift_video": 12.0, "shift_audio": 3.0}}
        noise, guider, scheduler, select, sampler = (str(base + offset + i) for i in range(3, 8))
        graph[noise] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed + segment_offset + index}}
        graph[guider] = {"class_type": "BasicGuider", "inputs": {"model": [shifted, 0], "conditioning": conditioning}}
        graph[scheduler] = {"class_type": "BasicScheduler", "inputs": {
            "model": [shifted, 0], "scheduler": "simple", "steps": 8 if selflift else 6, "denoise": 1.0}}
        graph[select] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
        if selflift:
            negative = str(base + offset + 8)
            graph[negative] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": conditioning}}
            graph[sampler] = {"class_type": "SelfLiftAvatarH3Sampler", "inputs": {
                "model": [shifted, 0], "positive": conditioning,
                "negative": [negative, 0], "vae": [output, 2],
                "latent_image": latent, "sampler": [select, 0],
                "sigmas": [scheduler, 0], "seed": seed + segment_offset + index,
                "cfg": 1.0, "transition_step": 6, "lowres_scale": 0.5,
                "rho": 0.6, "w_min": 1.0, "w_max": 1.0,
                "upscaler_model": "none", "highres_tiling": False,
                "tiling_mode": "auto", "tiling_tiles": 2, "tiling_axis": "auto"}}
        else:
            graph[sampler] = {"class_type": "SamplerCustomAdvanced", "inputs": {
                "noise": [noise, 0], "guider": [guider, 0], "sampler": [select, 0],
                "sigmas": [scheduler, 0], "latent_image": latent}}
        previous_sampler = sampler
        decode_video, decode_audio = str(base + 20), str(base + 21)
        graph[decode_video] = {"class_type": "VAEDecode", "inputs": {"samples": [sampler, 0], "vae": [output, 2]}}
        graph[decode_audio] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": [sampler, 0], "vae": [output, 3]}}
        if trim_source is None:
            images, audio = [decode_video, 0], [decode_audio, 0]
        else:
            trim = str(base + 22)
            graph[trim] = {"class_type": "MiniMaxH3MotionContextTrim", "inputs": {
                "images": [decode_video, 0],
                "trim_frames": (
                    CONTEXT_FRAMES - CROSS_CHUNK_OVERLAP_FRAMES
                    if video_latent and index == 0 else [trim_source, 1]
                ),
                "audio": [decode_audio, 0], "fps": float(FPS), "match_tail": True}}
            images, audio = [trim, 0], [trim, 1]
        if index == 0:
            combined_images, combined_audio = images, audio
        else:
            image_batch, audio_concat = str(base + 25), str(base + 26)
            graph[image_batch] = {"class_type": "ImageBatch", "inputs": {"image1": combined_images, "image2": images}}
            graph[audio_concat] = {"class_type": "AudioConcat", "inputs": {"audio1": combined_audio, "audio2": audio, "direction": "after"}}
            combined_images, combined_audio = [image_batch, 0], [audio_concat, 0]
    graph.update({
        "9000": {"class_type": "CreateVideo", "inputs": {
            "images": combined_images, "audio": combined_audio,
            "fps": float(FPS), "bit_depth": 8}},
        "9001": {"class_type": "SaveVideo", "inputs": {
            "video": ["9000", 0], "filename_prefix": "h3/long_avatar",
            "format": "mp4", "codec": "h264", "codec.encoding": "re-encode",
            "codec.encoding.crf": 12.0}},
    })
    if persist_latent:
        graph.update({
            "9010": {"class_type": "LTXVSeparateAVLatent", "inputs": {
                "av_latent": [previous_sampler, 0]}},
            "9011": {"class_type": "SaveLatent", "inputs": {
                "samples": ["9010", 0],
                "filename_prefix": f"h3/avatar_chunk_{segment_offset:02d}/latent_video"}},
            "9012": {"class_type": "SaveLatent", "inputs": {
                "samples": ["9010", 1],
                "filename_prefix": f"h3/avatar_chunk_{segment_offset:02d}/latent_audio"}},
        })
    return graph, lengths, rendered_frames


def long_chunk_workflow(
    prompts: list[str], lengths: list[int], ratio: str, seed: int,
    segment_offset: int = 0, video_latent: str | None = None,
    audio_latent: str | None = None, first_frame: str | None = None,
) -> dict:
    """Build at most three linked segments and persist the final AV latent.

    Keeping a full minute of decoded RGB frames in one Comfy graph exceeds the
    64 GiB host-memory limit of the Gongji 4090 worker. A chunk stays within the
    validated 45-second memory envelope; its final latent is loaded by the next
    chunk so generation remains motion-context linked.
    """
    if not prompts or len(prompts) != len(lengths) or len(prompts) > 3:
        raise ValueError("每个长视频内存分块必须包含 1–3 个匹配的提示词和帧长")
    if (video_latent is None) != (audio_latent is None):
        raise ValueError("续写分块必须同时提供视频和音频 Latent")
    if first_frame and segment_offset:
        raise ValueError("首帧图片只能用于第一个内存分块")
    width, height = dimensions(ratio)
    graph: dict = {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
            "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {
            "vae_name": "official/minimax_h3_video_vae_int8_convrot.safetensors"}},
        "4": {"class_type": "VAELoader", "inputs": {
            "vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "5": {"class_type": "SolAttnMiniMax", "inputs": {
            "model": ["1", 0], "selection": "adaptive tau", "selection.tau": 1.3,
            "start_percent": 0.0, "end_percent": 1.0, "min_tokens": 0,
            "sink_conditioning": "exact_kv_and_rows", "verbose": False}},
        "6": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["5", 0],
            "lora_name": "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
            "strength_model": 1.0}},
    }
    previous_sampler: list | None = None
    if video_latent and audio_latent:
        graph.update({
            "7": {"class_type": "LoadLatent", "inputs": {"latent": video_latent}},
            "8": {"class_type": "LoadLatent", "inputs": {"latent": audio_latent}},
            "9": {"class_type": "LTXVConcatAVLatent", "inputs": {
                "video_latent": ["7", 0], "audio_latent": ["8", 0]}},
        })
        previous_sampler = ["9", 0]
    if first_frame:
        graph["10"] = {
            "class_type": "LoadImage", "inputs": {"image": first_frame}
        }

    combined_images: list | None = None
    combined_audio: list | None = None
    last_sampler = ""
    for local_index, (prompt, length) in enumerate(zip(prompts, lengths)):
        global_index = segment_offset + local_index
        base = 100 + local_index * 30
        condition = str(base)
        effective_prompt = (
            prompt
            if global_index == 0
            else f"{CONTINUATION_AIRLOCK}\n\n{prompt}"
        )
        condition_inputs = {
            "clip": ["2", 0], "vae": ["3", 0], "prompt": effective_prompt,
            "width": width, "height": height, "length": length,
        }
        if global_index == 0 and first_frame:
            condition_inputs["first_frame"] = ["10", 0]
        graph[condition] = {
            "class_type": "MiniMaxH3ImageToVideo", "inputs": condition_inputs
        }
        trim_source = None
        if global_index == 0:
            conditioning = [condition, 0]
            latent_image = [condition, 1]
            node_shift = 1
        else:
            if previous_sampler is None:
                raise ValueError("续写分块缺少上一段 Latent")
            motion = str(base + 1)
            graph[motion] = {"class_type": "MiniMaxH3MotionContext", "inputs": {
                "conditioning": [condition, 0], "vae": ["3", 0],
                "latent": [condition, 1], "context_length": str(CONTEXT_FRAMES),
                "audio_context_length": 24, "context_latent": previous_sampler}}
            conditioning = [motion, 0]
            latent_image = [condition, 1]
            trim_source = motion
            node_shift = 2
        noise = str(base + node_shift)
        guider = str(base + node_shift + 1)
        scheduler = str(base + node_shift + 2)
        sampler_select = str(base + node_shift + 3)
        sampler = str(base + node_shift + 4)
        graph[noise] = {"class_type": "RandomNoise", "inputs": {
            "noise_seed": seed + global_index}}
        graph[guider] = {"class_type": "BasicGuider", "inputs": {
            "model": ["6", 0], "conditioning": conditioning}}
        graph[scheduler] = {"class_type": "BasicScheduler", "inputs": {
            "model": ["6", 0], "scheduler": "beta", "steps": 4,
            "denoise": 1.0}}
        graph[sampler_select] = {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": "euler"}}
        graph[sampler] = {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": [noise, 0], "guider": [guider, 0],
            "sampler": [sampler_select, 0], "sigmas": [scheduler, 0],
            "latent_image": latent_image}}
        previous_sampler = [sampler, 0]
        last_sampler = sampler

        decode_video, decode_audio = str(base + 10), str(base + 11)
        graph[decode_video] = {"class_type": "VAEDecode", "inputs": {
            "samples": [sampler, 0], "vae": ["3", 0]}}
        graph[decode_audio] = {"class_type": "VAEDecodeAudio", "inputs": {
            "samples": [sampler, 0], "vae": ["4", 0]}}
        if trim_source is None:
            images, audio = [decode_video, 0], [decode_audio, 0]
        else:
            trim = str(base + 12)
            graph[trim] = {"class_type": "MiniMaxH3MotionContextTrim", "inputs": {
                "images": [decode_video, 0],
                "trim_frames": (
                    CONTEXT_FRAMES - CROSS_CHUNK_OVERLAP_FRAMES
                    if video_latent and local_index == 0
                    else [trim_source, 1]
                ),
                "audio": [decode_audio, 0], "fps": float(FPS),
                "match_tail": True}}
            images, audio = [trim, 0], [trim, 1]
        if combined_images is None:
            combined_images, combined_audio = images, audio
        else:
            image_batch, audio_concat = str(base + 15), str(base + 16)
            graph[image_batch] = {"class_type": "ImageBatch", "inputs": {
                "image1": combined_images, "image2": images}}
            graph[audio_concat] = {"class_type": "AudioConcat", "inputs": {
                "audio1": combined_audio, "audio2": audio, "direction": "after"}}
            combined_images, combined_audio = [image_batch, 0], [audio_concat, 0]

    graph.update({
        "9000": {"class_type": "CreateVideo", "inputs": {
            "images": combined_images, "audio": combined_audio,
            "fps": float(FPS), "bit_depth": 8}},
        "9001": {"class_type": "SaveVideo", "inputs": {
            "video": ["9000", 0],
            "filename_prefix": f"h3/long_chunk_{segment_offset:02d}",
            "format": "mp4", "codec": "h264", "codec.encoding": "re-encode",
            "codec.encoding.crf": 12.0}},
        "9010": {"class_type": "LTXVSeparateAVLatent", "inputs": {
            "av_latent": [last_sampler, 0]}},
        "9011": {"class_type": "SaveLatent", "inputs": {
            "samples": ["9010", 0],
            "filename_prefix": f"h3/long_chunk_{segment_offset:02d}/latent_video"}},
        "9012": {"class_type": "SaveLatent", "inputs": {
            "samples": ["9010", 1],
            "filename_prefix": f"h3/long_chunk_{segment_offset:02d}/latent_audio"}},
    })
    return graph


async def _save_latent(upload: UploadFile | None) -> str | None:
    if upload is None or not upload.filename:
        return None
    if Path(upload.filename).suffix.lower() != ".latent":
        raise HTTPException(400, "长视频上下文必须使用 .latent 文件")
    INPUT.mkdir(parents=True, exist_ok=True)
    name = f"h3_long_{uuid.uuid4().hex}.latent"
    target = INPUT / name
    written = 0
    with target.open("wb") as handle:
        while chunk := upload.file.read(1024 * 1024):
            written += len(chunk)
            if written > 512 * 1024 * 1024:
                target.unlink(missing_ok=True)
                raise HTTPException(413, "长视频上下文 Latent 过大")
            handle.write(chunk)
    return name


async def _save_first_frame(upload: UploadFile | None) -> str | None:
    if upload is None or not upload.filename:
        return None
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(400, "长视频首帧必须是 PNG、JPEG 或 WebP 图片")
    INPUT.mkdir(parents=True, exist_ok=True)
    name = f"h3_long_first_{uuid.uuid4().hex}{suffix}"
    target = INPUT / name
    written = 0
    with target.open("wb") as handle:
        while chunk := upload.file.read(1024 * 1024):
            written += len(chunk)
            if written > 64 * 1024 * 1024:
                target.unlink(missing_ok=True)
                raise HTTPException(413, "长视频首帧图片过大")
            handle.write(chunk)
    return name


async def _save_avatar_upload(upload: UploadFile, field: str) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    allowed = {"images": {".png", ".jpg", ".jpeg", ".webp"},
               "audios": {".wav", ".mp3", ".m4a", ".flac"}}
    if suffix not in allowed.get(field, set()):
        raise HTTPException(400, f"数字人 {field} 素材格式不正确")
    INPUT.mkdir(parents=True, exist_ok=True)
    name = f"h3_avatar_{uuid.uuid4().hex}{suffix}"
    target = INPUT / name
    written = 0
    with target.open("wb") as handle:
        while chunk := upload.file.read(1024 * 1024):
            written += len(chunk)
            if written > 128 * 1024 * 1024:
                target.unlink(missing_ok=True)
                raise HTTPException(413, "数字人参考素材过大")
            handle.write(chunk)
    return name


async def _submit(graph: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{COMFY}/prompt", json={"prompt": graph})
    if response.status_code >= 400:
        raise HTTPException(500, f"推理后端拒绝长视频任务: {response.text[:1000]}")
    return response.json()


@router.post("/api/generate/long")
@router.post("/api/generate/long-avatar2")
async def generate_long(
    request: Request,
    prompts_json: Annotated[str, Form()],
    duration: Annotated[float, Form()],
    ratio: Annotated[str, Form()] = "16:9",
    seed: Annotated[int, Form()] = 0,
    first_frame: Annotated[UploadFile | None, File()] = None,
    images: Annotated[list[UploadFile] | None, File()] = None,
    audios: Annotated[list[UploadFile] | None, File()] = None,
    avatar_mode: Annotated[str, Form()] = "",
    selflift: Annotated[str, Form()] = "",
) -> dict:
    # The dedicated Digital-human 2 endpoint cannot be downgraded by a
    # missing or false form flag.  Its graph must always use selflift-Avatar.
    if request.url.path.rstrip("/").endswith("/long-avatar2"):
        selflift = "1"
    min_duration = 1 if avatar_mode == "audio" else 16
    if not min_duration <= duration <= MAX_DURATION:
        raise HTTPException(400, f"视频时长必须为 {min_duration}–{MAX_DURATION} 秒")
    try:
        prompts = json.loads(prompts_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "segment_prompts 不是有效 JSON") from exc
    if not isinstance(prompts, list) or not all(isinstance(item, str) for item in prompts):
        raise HTTPException(400, "segment_prompts 必须是字符串列表")
    if any(not item.strip() or len(item) > 30000 for item in prompts):
        raise HTTPException(400, "每段提示词长度需要 1–30000 字符")
    first_name = await _save_first_frame(first_frame)
    avatar_image = None
    avatar_audio_names: list[str] = []
    if images or audios or avatar_mode:
        if avatar_mode != "audio" or len(images or []) != 1:
            raise HTTPException(400, "数字人任务需要一张人物图和一段驱动音频")
        prompt_count = len(prompts)
        if len(audios or []) != prompt_count:
            raise HTTPException(400, "数字人音频参考数量与分段数量不一致")
        avatar_image = await _save_avatar_upload((images or [])[0], "images")
        for upload in audios or []:
            avatar_audio_names.append(await _save_avatar_upload(upload, "audios"))
        if first_name:
            (INPUT / first_name).unlink(missing_ok=True)
            raise HTTPException(400, "数字人任务不能同时上传首帧")
    try:
        graph, lengths, rendered_frames = long_workflow(
            [item.strip() for item in prompts], duration, ratio,
            seed or uuid.uuid4().int % (2**63),
            first_name, avatar_image, avatar_audio_names,
            selflift=bool(selflift.strip() and selflift.strip() not in {"0", "false", "no"}),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    submitted = await _submit(graph)
    return {
        **submitted,
        "segment_count": len(lengths),
        "raw_segment_frames": lengths,
        "rendered_frames": rendered_frames,
        "target_frames": duration * FPS,
        "first_frame": bool(first_name),
    }


@router.post("/api/generate/long-chunk")
async def generate_long_chunk(
    prompts_json: Annotated[str, Form()],
    lengths_json: Annotated[str, Form()],
    ratio: Annotated[str, Form()] = "16:9",
    seed: Annotated[int, Form()] = 0,
    segment_offset: Annotated[int, Form()] = 0,
    video_latent: Annotated[UploadFile | None, File()] = None,
    audio_latent: Annotated[UploadFile | None, File()] = None,
    first_frame: Annotated[UploadFile | None, File()] = None,
) -> dict:
    try:
        prompts = json.loads(prompts_json)
        lengths = json.loads(lengths_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "长视频分块参数不是有效 JSON") from exc
    if not isinstance(prompts, list) or not all(isinstance(x, str) and x.strip() for x in prompts):
        raise HTTPException(400, "长视频分块提示词无效")
    if not isinstance(lengths, list) or not all(isinstance(x, int) and 22 <= x <= 396 for x in lengths):
        raise HTTPException(400, "长视频分块帧长无效")
    if ratio not in {"16:9", "9:16"} or segment_offset < 0:
        raise HTTPException(400, "长视频分块画幅或序号无效")
    video_name = await _save_latent(video_latent)
    audio_name = await _save_latent(audio_latent)
    first_name = await _save_first_frame(first_frame)
    try:
        graph = long_chunk_workflow(
            [x.strip() for x in prompts], lengths, ratio,
            seed or uuid.uuid4().int % (2**63), segment_offset,
            video_name, audio_name, first_name,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return await _submit(graph)


@router.post("/api/generate/long-avatar-chunk")
@router.post("/api/generate/long-avatar2-chunk")
async def generate_long_avatar_chunk(
    prompts_json: Annotated[str, Form()],
    lengths_json: Annotated[str, Form()],
    ratio: Annotated[str, Form()] = "9:16",
    seed: Annotated[int, Form()] = 0,
    segment_offset: Annotated[int, Form()] = 0,
    video_latent: Annotated[UploadFile | None, File()] = None,
    audio_latent: Annotated[UploadFile | None, File()] = None,
    images: Annotated[list[UploadFile] | None, File()] = None,
    audios: Annotated[list[UploadFile] | None, File()] = None,
    selflift: Annotated[str, Form()] = "",
) -> dict:
    try:
        prompts, lengths = json.loads(prompts_json), json.loads(lengths_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "数字人分块参数不是有效 JSON") from exc
    if not isinstance(prompts, list) or not 1 <= len(prompts) <= 3 or not all(isinstance(x, str) and x.strip() for x in prompts):
        raise HTTPException(400, "数字人分块提示词无效")
    if not isinstance(lengths, list) or len(lengths) != len(prompts) or not all(isinstance(x, int) and 22 <= x <= 396 for x in lengths):
        raise HTTPException(400, "数字人分块帧长无效")
    if ratio not in {"16:9", "9:16"} or segment_offset < 0:
        raise HTTPException(400, "数字人分块画幅或序号无效")
    if len(images or []) != 1 or len(audios or []) != len(prompts):
        raise HTTPException(400, "每个数字人分块需要一张人物图和对应数量的音频段")
    if (video_latent is None) != (audio_latent is None):
        raise HTTPException(400, "数字人续段必须同时提供视频和音频 Latent")
    video_name = await _save_latent(video_latent)
    audio_name = await _save_latent(audio_latent)
    image_name = await _save_avatar_upload(images[0], "images")
    audio_names = [await _save_avatar_upload(upload, "audios") for upload in audios]
    graph, _, _ = _avatar_long_workflow(
        [item.strip() for item in prompts], lengths, sum(lengths), ratio,
        seed, image_name, audio_names, lengths[0] / FPS,
        video_name, audio_name, segment_offset, persist_latent=True,
        selflift=bool(selflift.strip() and selflift.strip() not in {"0", "false", "no"}),
    )
    return await _submit(graph)
