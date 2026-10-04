"""MiniMax H3 worker service with the API-only long-video route."""

import math
import os
import uuid
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from h3_profiles import get_profile, profile_status


ROOT = Path(os.getenv("H3_ROOT", "/root/h3-server"))
RUNTIME = ROOT / "runtime"
INPUT = RUNTIME / "input"
OUTPUT = RUNTIME / "output"
COMFY = os.getenv("COMFY_URL", "http://127.0.0.1:8188")
INPUT.mkdir(parents=True, exist_ok=True)
OUTPUT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="MiniMax H3 Server", version="0.1.0")

MEGAPIXEL_CHOICES = {0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.98, 1.0, 1.2, 1.5, 1.8, 2.0}
LATENT_UPSCALER_MODEL = "minimax_h3_latent_upscaler_3d_fp16.safetensors"

# 逐级二采阶梯：横屏目标尺寸，竖屏自动交换。720p 不做二采。
TIER_LADDER = {
    "720p": [],
    "1080p": [(1920, 1088)],
    # 2K 直接从首轮 latent 放大一次。连续两次放大在长视频上会把
    # latent 分布推离模型训练范围，最终解码成灰噪；单次路径已验证稳定。
    "2k": [(2560, 1440)],
}
# 交付帧：模型输出按 32 对齐，需裁到精确交付尺寸。
TIER_DELIVERY = {"720p": None, "1080p": (1920, 1080), "2k": (2560, 1440)}


def tier_targets(tier: str, ratio: str) -> list[tuple[int, int]]:
    """Return the refine ladder for a tier, swapped for portrait output."""
    if tier not in TIER_LADDER:
        raise ValueError(f"不支持的输出档位: {tier}")
    targets = TIER_LADDER[tier]
    if ratio == "9:16":
        return [(height, width) for width, height in targets]
    return targets
ENHANCE_DENOISE = float(os.getenv("H3_ENHANCE_DENOISE", "0.35"))
ENHANCE_EXTRA_STEPS = int(os.getenv("H3_ENHANCE_EXTRA_STEPS", "1"))
ENHANCE_SIGMA_START = float(os.getenv("H3_ENHANCE_SIGMA_START", "0.7"))
ENHANCE_PREUPSCALED = os.getenv("H3_ENHANCE_PREUPSCALED", "0").strip() == "1"
ENHANCE_BYPASS_TILING = os.getenv("H3_ENHANCE_BYPASS_TILING", "0").strip() == "1"
ENHANCE_TILES = max(1, int(os.getenv("H3_ENHANCE_TILES", "2")))

# Keep the Sol-Attn graph contract identical to the standalone GPU server.
# This node must attach to the raw H3 model, not to the SageAttention patch.
H3_SOLATTN = os.getenv("H3_SOLATTN", "tau:1.3:0.0:1.0").strip()
H3_SOLATTN_IMPL = os.getenv("H3_SOLATTN_IMPL", "cuda").strip().lower()
H3_SOLATTN_STRICT = os.getenv("H3_SOLATTN_STRICT", "0").strip() == "1"
H3_2K_TILES = max(2, int(os.getenv("H3_2K_TILES", "4")))
REFERENCE_STEPS = int(os.getenv("H3_REFERENCE_STEPS", "6"))


def _solattn_enabled() -> bool:
    return bool(H3_SOLATTN) and H3_SOLATTN.lower() not in {"off", "0", "false", "no"}


def _solattn_node(src_node: str, sage_node: str | None = None) -> dict:
    try:
        mode, value, start, end = H3_SOLATTN.split(":")
        if H3_SOLATTN_IMPL == "blackwell_cute":
            if mode != "tau":
                raise ValueError("Blackwell CuTe Sol-Attn 仅支持 tau 模式")
            return {"class_type": "SolAttnH3", "inputs": {
                # KJNodes SageAttention object-patches every H3 block's
                # Attention.forward. Feeding that patched model here bypasses
                # ComfyUI's optimized_attention_override entirely, so the CuTe
                # kernel can never run. CuTe owns this path and falls back to
                # the configured dense backend when a call is not eligible.
                "model": [src_node, 0], "enabled": True,
                "tau": float(value), "thresh_type": "diag",
                "first_dense_steps": 0.0, "first_dense_layers": 0,
                "sink_mode": "prefix", "correctness_gate": True,
                "strict": H3_SOLATTN_STRICT,
            }}
        if H3_SOLATTN_IMPL == "blackwell_flex":
            if mode != "tau":
                raise ValueError("Blackwell Flex Sol-Attn 仅支持 tau 模式")
            return {"class_type": "SolAttnMiniMaxH3Patcher", "inputs": {
                "model": [sage_node or src_node, 0], "enabled": True,
                "tau": float(value), "thresh_type": "diag",
                "exact_mode": "exact_kv_and_rows", "dense_steps": 0,
                "step_off": 0.0, "sink_tokens": 0,
            }}
        if H3_SOLATTN_IMPL == "blackwell":
            if mode != "tau":
                raise ValueError("Blackwell Sol-Attn 仅支持 tau 模式")
            return {"class_type": "MiniMaxH3ScheduledSolAttentionPatch", "inputs": {
                "model": [sage_node or src_node, 0], "enabled": True,
                "tau_start": float(value), "tau_end": float(value), "curve": "linear",
                "min_tokens": 256, "strict": H3_SOLATTN_STRICT,
                "dense_percent": 0.0, "thresh_type": "diag",
                "int8_qk": False, "int8_pv": False,
                "sink_conditioning": "exact_kv_and_rows", "dense_blocks": "",
            }}
        if H3_SOLATTN_IMPL != "cuda":
            raise ValueError(f"未知 Sol-Attn 实现: {H3_SOLATTN_IMPL}")
        selection = {"tau": "adaptive tau", "topk": "top-k (SLA)", "vsa": "VSA (FastVideo)"}[mode]
        parameter = {"tau": "tau", "topk": "keep_percent", "vsa": "vsa_keep_percent"}[mode]
        return {"class_type": "SolAttnMiniMax", "inputs": {
            "model": [src_node, 0], "selection": selection,
            f"selection.{parameter}": float(value),
            "start_percent": float(start), "end_percent": float(end),
            "min_tokens": 0, "sink_conditioning": "exact_kv_and_rows", "verbose": False,
        }}
    except (ValueError, KeyError) as exc:
        raise ValueError("H3_SOLATTN 必须是 tau:1.3:0.0:1.0、topk:10:0.0:1.0、vsa:10:0.0:1.0 或 off") from exc

try:
    from task_center import router as task_center_router

    app.include_router(task_center_router)
except Exception as exc:
    print(f"[warn] 统一任务中心未加载: {exc}")

try:  # Qwen 图像能力为独立模块；加载失败也绝不能影响 H3 视频服务
    from qwen import router as qwen_router

    app.include_router(qwen_router)
except Exception as exc:
    print(f"[warn] Qwen 图像模块未加载: {exc}")

try:  # 提示词助手只调用 DeepSeek，不占用 GPU，也不进入视频任务队列
    from prompt_assistant import router as prompt_assistant_router

    app.include_router(prompt_assistant_router)
except Exception as exc:
    print(f"[warn] H3 提示词助手未加载: {exc}")

try:  # Long video is isolated to the dedicated Gongji worker image.
    from long_video import router as long_video_router

    app.include_router(long_video_router)
except Exception as exc:
    print(f"[warn] H3 长视频模块未加载: {exc}")

def frames_for_seconds(seconds: float) -> int:
    target = max(5.0, seconds * 24)
    block_count = max(0, round((target - 5) / 17))
    return block_count * 17 + 5


def dimensions(ratio: str, megapixels: float) -> tuple[int, int]:
    workflow_16_9 = {
        0.2: (608, 352), 0.3: (736, 416), 0.4: (864, 480), 0.5: (960, 544),
        0.6: (1056, 608), 0.7: (1152, 640), 0.8: (1216, 672), 0.9: (1280, 736),
        0.98: (1344, 768), 1.0: (1376, 768), 1.2: (1504, 832), 1.5: (1664, 928),
        1.8: (1824, 1024), 2.0: (1920, 1088),
    }
    if ratio == "16:9": return workflow_16_9[megapixels]
    if ratio == "9:16":
        width, height = workflow_16_9[megapixels]
        return height, width
    rw, rh = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:3": (4, 3), "3:4": (3, 4)}[ratio]
    pixels = megapixels * 1_000_000
    width = math.sqrt(pixels * rw / rh)
    height = width * rh / rw
    return max(32, math.ceil(width / 32) * 32), max(32, math.ceil(height / 32) * 32)


async def save_upload(upload: UploadFile | None, category: str) -> str | None:
    if upload is None or not upload.filename:
        return None
    suffix = Path(upload.filename).suffix.lower()
    allowed = {
        "image": {".jpg", ".jpeg", ".png", ".webp"},
        "video": {".mp4", ".mov", ".mkv", ".webm"},
        "audio": {".wav", ".mp3", ".flac", ".m4a", ".aac"},
        "latent": {".latent"},
    }[category]
    if suffix not in allowed:
        raise HTTPException(400, f"不支持的{category}格式: {suffix}")
    name = f"h3_{uuid.uuid4().hex}{suffix}"
    target = INPUT / name
    limit = {"image": 64, "video": 512, "audio": 64, "latent": 256}[category] * 1024 * 1024
    written = 0
    with target.open("wb") as dst:
        while chunk := upload.file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                dst.close()
                target.unlink(missing_ok=True)
                raise HTTPException(413, f"{category}文件过大")
            dst.write(chunk)
    return name


def validate_request(prompt: str, duration: float, ratio: str, quality: float):
    if not prompt.strip() or len(prompt) > 20_000:
        raise HTTPException(400, "提示词不能为空且不能超过 20000 字符")
    if not 4 <= duration <= 15:
        raise HTTPException(400, "时长必须为 4–15 秒")
    if ratio not in {"16:9", "9:16", "1:1", "4:3", "3:4"}:
        raise HTTPException(400, "不支持的画幅")
    if quality not in MEGAPIXEL_CHOICES:
        raise HTTPException(400, "不支持的分辨率档位")


def require_profile(profile_key: str):
    try:
        profile = get_profile(profile_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    hardware = profile_status(ROOT)
    status = hardware["profiles"][profile_key]
    if not status["files_ready"]:
        raise HTTPException(503, f"{profile.label}缺少模型文件：{', '.join(status['missing'])}")
    if not status["enough_ram"]:
        ram = hardware["host_ram_gib"]
        raise HTTPException(409, f"{profile.label}需要至少 {profile.recommended_ram_gib:.0f}GB 主机内存；当前约 {ram:.0f}GB，请选择标准模式")
    if not status["enough_vram"]:
        vram = hardware["gpu_vram_gib"]
        raise HTTPException(409, f"{profile.label}需要至少 {profile.recommended_vram_gib:.0f}GB 显存；当前约 {vram:.0f}GB，请选择标准模式")
    return profile


def common_nodes(profile_key: str) -> dict:
    """The loader/adapter chain used by the supplied H3 Easy workflow."""
    profile = get_profile(profile_key)
    return {
        "6": {"class_type": "MiniMaxH3EasyLoader", "inputs": {
            "fl2va_model": profile.fl_model, "ref2va_model": profile.ref_model,
            "text_encoder": profile.text_encoder, "video_vae": profile.video_vae,
            "audio_vae": profile.audio_vae}},
    }


def sampling_nodes(condition_node: str, seed: int, prefix: str, profile_key: str,
                   reference: bool = False, model_node: str = "9",
                   sampler: str = "euler", output_mode: str = "both") -> dict:
    profile = get_profile(profile_key)
    lora = profile.ref_lora if reference else profile.fl_lora
    # Production Ref2VA intentionally takes one additional denoising step over
    # the Turbo LoRA's four-step baseline. Keep FL2VA on its profile setting.
    steps = REFERENCE_STEPS if reference else profile.fl_steps
    lora_strength = 1.0
    scheduler = "simple" if reference else "beta"
    guider_model = [model_node, 0]
    nodes = {
        "8": {"class_type": "MiniMaxH3EasyOutput", "inputs": {"h3_context": [condition_node, 1]}},
        "102": {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": [condition_node, 0]}},
        "9": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["102", 0], "lora_name": lora, "strength_model": lora_strength}},
        "50": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "51": {"class_type": "BasicGuider", "inputs": {"model": guider_model, "conditioning": ["8", 0]}},
        "52": {"class_type": "BasicScheduler", "inputs": {"model": [model_node, 0], "scheduler": scheduler, "steps": steps, "denoise": 1.0}},
        "53": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler}},
        "54": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["50", 0], "guider": ["51", 0], "sampler": ["53", 0], "sigmas": ["52", 0], "latent_image": ["8", 1]}},
    }
    if output_mode in {"both", "video"}:
        nodes.update({
            "55": {"class_type": "VAEDecode", "inputs": {"samples": ["54", 0], "vae": ["8", 2]}},
            "56": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["54", 0], "vae": ["8", 3]}},
            "57": {"class_type": "CreateVideo", "inputs": {"images": ["55", 0], "audio": ["56", 0], "fps": 24.0, "bit_depth": 8}},
            "58": {"class_type": "SaveVideo", "inputs": {"video": ["57", 0], "filename_prefix": prefix, "format": "mp4", "codec": "h264"}},
        })
    if output_mode in {"both", "latent"}:
        # Persist the denoised first-pass AV latent as two standard safetensors.
        # A complete H3 AV NestedTensor cannot be serialized by core SaveLatent.
        nodes.update({
            "59": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["54", 1]}},
            "60": {"class_type": "SaveLatent", "inputs": {
                "samples": ["59", 0], "filename_prefix": f"{prefix}/latent_video"}},
            "61": {"class_type": "SaveLatent", "inputs": {
                "samples": ["59", 1], "filename_prefix": f"{prefix}/latent_audio"}},
        })
    if _solattn_enabled():
        nodes["104"] = _solattn_node(condition_node, "102")
        nodes["9"]["inputs"]["model"] = ["104", 0]
    if reference:
        nodes["103"] = {"class_type": "MiniMaxH3SigmaShift", "inputs": {
            "model": [model_node, 0], "shift_video": 12.0, "shift_audio": 3.0}}
        nodes["51"]["inputs"]["model"] = ["103", 0]
    return nodes


def easy_condition(prompt: str, seconds: float, ratio: str, width: int, height: int,
                   mode: str = "image") -> dict:
    return {
        "h3_bundle": ["6", 0], "mode": mode, "prompt": prompt,
        "resolution": "custom", "aspect_ratio": ratio,
        "width": width, "height": height, "seconds": seconds,
        "advanced": True, "fps": 24.0, "keyframe_role": "first",
        "ref_image_size": "1.5k", "reference_mention_mode": "index",
        "force_offload": True,
        "prompt_optimizer_settings": False,
        "prompt_optimizer_scene_guide": "none",
    }


def image_workflow(prompt: str, first: str, last: str | None, seconds: float, ratio: str, megapixels: float, seed: int, profile_key: str = "standard", output_mode: str = "both") -> dict:
    width, height = dimensions(ratio, megapixels)
    graph = common_nodes(profile_key)
    graph["10"] = {"class_type": "LoadImage", "inputs": {"image": first}}
    condition = easy_condition(prompt, seconds, ratio, width, height)
    condition.update({"media_1": ["10", 0], "media_type_1": "image"})
    if last:
        graph["11"] = {"class_type": "LoadImage", "inputs": {"image": last}}
        condition.update({"media_2": ["11", 0], "media_type_2": "image"})
    graph["20"] = {"class_type": "MiniMaxH3Easy", "inputs": condition}
    graph.update(sampling_nodes("20", seed, "h3/image", profile_key, output_mode=output_mode))
    return graph


def text_workflow(prompt: str, seconds: float, ratio: str, megapixels: float, seed: int, profile_key: str = "standard", output_mode: str = "both") -> dict:
    """纯文生视频。MiniMaxH3ImageToVideo 的 first_frame 是可选输入，不传即为文生。"""
    width, height = dimensions(ratio, megapixels)
    graph = common_nodes(profile_key)
    condition = easy_condition(prompt, seconds, ratio, width, height)
    graph["20"] = {"class_type": "MiniMaxH3Easy", "inputs": condition}
    graph.update(sampling_nodes("20", seed, "h3/text", profile_key, output_mode=output_mode))
    return graph


def reference_workflow(prompt: str, images: list[str], videos: list[str], audios: list[str],
                       seconds: float, ratio: str, megapixels: float, seed: int,
                       video_audio_flags: list[bool] | None = None,
                       profile_key: str = "standard", output_mode: str = "both") -> dict:
    width, height = dimensions(ratio, megapixels)
    graph = common_nodes(profile_key)
    condition = easy_condition(prompt, seconds, ratio, width, height, "reference")
    condition["ref_image_size"] = "match"
    node_id = 10
    media_index = 1
    for i, name in enumerate(images):
        key = str(node_id)
        graph[key] = {"class_type": "LoadImage", "inputs": {"image": name}}
        condition[f"media_{media_index}"] = [key, 0]
        condition[f"media_type_{media_index}"] = "image"
        node_id += 1; media_index += 1
    video_audio_flags = video_audio_flags or [True] * len(videos)
    for i, name in enumerate(videos):
        load_key = str(node_id)
        graph[load_key] = {"class_type": "LoadVideo", "inputs": {"file": name, "keep_audio": bool(video_audio_flags[i])}}
        condition[f"media_{media_index}"] = [load_key, 0]
        condition[f"media_type_{media_index}"] = "video"
        node_id += 1; media_index += 1
    for i, name in enumerate(audios):
        key = str(node_id); graph[key] = {"class_type": "LoadAudio", "inputs": {"audio": name}}
        condition[f"media_{media_index}"] = [key, 0]
        condition[f"media_type_{media_index}"] = "audio"
        node_id += 1; media_index += 1
    graph["40"] = {"class_type": "MiniMaxH3Easy", "inputs": condition}
    graph.update(sampling_nodes(
        "40", seed, "h3/reference", profile_key, True, "9", "euler", output_mode
    ))
    return graph


def _add_latent_hd_ladder(
    graph: dict, prompt: str, seconds: float, ratio: str,
    targets: list[tuple[int, int]], seed: int, profile_key: str,
    denoise: float, output_prefix: str,
    refine_steps: int = 2, refine_extra_steps: int = 0,
    sigma_start: float = 0.7,
) -> dict:
    """Chain N latent-upscale + low-noise refine stages onto a first pass.

    Every stage runs inside a single ComfyUI graph: the denoised AV latent of
    one stage feeds the next, so nothing is serialised to disk between passes.
    ``targets`` is the already ratio-corrected ladder; an empty list leaves the
    first pass untouched.
    """
    if not targets:
        return graph

    # Drop the first-pass decode/save chain, keep node 59 (the AV latent split).
    for key in ("55", "56", "57", "58", "60", "61"):
        graph.pop(key, None)

    source_video, source_audio = ["59", 0], ["59", 1]
    last_output = None
    for index, (width, height) in enumerate(targets, start=1):
        base = 200 + (index - 1) * 100
        n = lambda offset: str(base + offset)  # noqa: E731

        # The reference media are already distilled into the first-pass latent,
        # so each refine stage only re-states the target geometry.
        condition = easy_condition(prompt, seconds, ratio, width, height)
        condition["force_offload"] = False

        upscale = latent_upscale_node("__src__", width, height, seconds > 5)
        upscale["inputs"]["latent"] = source_video
        graph.update({
            n(0): upscale,
            n(1): {"class_type": "LTXVConcatAVLatent", "inputs": {
                "video_latent": [n(0), 0], "audio_latent": source_audio}},
            n(2): {"class_type": "MiniMaxH3Easy", "inputs": condition},
            n(3): {"class_type": "MiniMaxH3EasyOutput", "inputs": {
                "h3_context": [n(2), 1]}},
            n(4): {"class_type": "MiniMaxH3EasySecondPassConditioning", "inputs": {
                "h3_context": [n(2), 1], "second_pass_video_latent": [n(0), 0]}},
            n(5): {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch",
                   "inputs": {"model": [n(2), 0]}},
            n(6): {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
            n(7): {"class_type": "BasicGuider", "inputs": {
                "model": [n(5), 0], "conditioning": [n(4), 0]}},
            n(8): {"class_type": "BasicScheduler", "inputs": {
                "model": [n(5), 0], "scheduler": "beta",
                "steps": refine_steps, "denoise": denoise}},
            n(9): {"class_type": "H3SigmaRefiner", "inputs": {
                "sigmas": [n(8), 0], "extra_steps": refine_extra_steps,
                "start_at_sigma": sigma_start, "end_at_sigma": 0.0,
                "spacing": "cosine"}},
            n(10): {"class_type": "KSamplerSelect", "inputs": {
                "sampler_name": "euler"}},
            n(11): {"class_type": "H3TiledSampler", "inputs": {
                "noise": [n(6), 0], "guider": [n(7), 0],
                "sampler": [n(10), 0], "sigmas": [n(9), 0],
                "latent_image": [n(1), 0], "bypass_tiling": False,
                "tile_axis": "auto", "n_tiles": 2, "tile_overlap": 8,
                "max_size_for_no_tile": 24,
                "target_frames": frames_for_seconds(seconds),
                "frame_padding_mode": "replicate_last", "debug": False}},
        })
        if _solattn_enabled():
            graph[n(104)] = _solattn_node(n(2), n(5))
            graph[n(7)]["inputs"]["model"] = [n(104), 0]
            graph[n(8)]["inputs"]["model"] = [n(104), 0]
        if index < len(targets):
            # denoised_output (index 1) carries the AV latent for the next rung.
            graph[n(12)] = {"class_type": "LTXVSeparateAVLatent", "inputs": {
                "av_latent": [n(11), 1]}}
            source_video, source_audio = [n(12), 0], [n(12), 1]
        last_output = (n(11), n(3))

    sampler_node, vae_node = last_output
    graph.update({
        "900": {"class_type": "VAEDecode", "inputs": {
            "samples": [sampler_node, 0], "vae": [vae_node, 2]}},
        "901": {"class_type": "VAEDecodeAudio", "inputs": {
            "samples": [sampler_node, 0], "vae": [vae_node, 3]}},
        "902": {"class_type": "CreateVideo", "inputs": {
            "images": ["900", 0], "audio": ["901", 0],
            "fps": 24.0, "bit_depth": 8}},
        "903": {"class_type": "SaveVideo", "inputs": {
            "video": ["902", 0], "filename_prefix": output_prefix,
            "format": "mp4", "codec": "h264"}},
    })
    return graph


def text_latent_hd_workflow(
    prompt: str, seconds: float, ratio: str, first_megapixels: float,
    output_megapixels: float, seed: int, profile_key: str = "standard",
    denoise: float = 0.35, tier: str = "1080p",
    refine_steps: int = 2, refine_extra_steps: int = 0,
) -> dict:
    graph = text_workflow(
        prompt, seconds, ratio, first_megapixels, seed, profile_key,
    )
    return _add_latent_hd_ladder(
        graph, prompt, seconds, ratio, tier_targets(tier, ratio), seed,
        profile_key, denoise, "h3/text_latent_hd",
        refine_steps, refine_extra_steps,
    )


def image_latent_hd_workflow(
    prompt: str, first: str, last: str | None, seconds: float, ratio: str,
    first_megapixels: float, output_megapixels: float, seed: int,
    profile_key: str = "standard", denoise: float = 0.35, tier: str = "1080p",
    refine_steps: int = 2, refine_extra_steps: int = 0,
) -> dict:
    graph = image_workflow(
        prompt, first, last, seconds, ratio, first_megapixels, seed, profile_key,
    )
    return _add_latent_hd_ladder(
        graph, prompt, seconds, ratio, tier_targets(tier, ratio), seed,
        profile_key, denoise, "h3/image_latent_hd",
        refine_steps, refine_extra_steps,
    )


def reference_latent_hd_workflow(
    prompt: str, images: list[str], videos: list[str], audios: list[str],
    seconds: float, ratio: str, first_megapixels: float, output_megapixels: float,
    seed: int, video_audio_flags: list[bool] | None = None,
    profile_key: str = "standard", denoise: float = 0.35, tier: str = "1080p",
    refine_steps: int = 2, refine_extra_steps: int = 0,
) -> dict:
    graph = reference_workflow(
        prompt, images, videos, audios, seconds, ratio, first_megapixels, seed,
        video_audio_flags, profile_key,
    )
    return _add_latent_hd_ladder(
        graph, prompt, seconds, ratio, tier_targets(tier, ratio), seed,
        profile_key, denoise, "h3/reference_latent_hd",
        refine_steps, refine_extra_steps,
    )


def enhance_workflow(prompt: str, source_video: str, seconds: float, ratio: str,
                     megapixels: float, seed: int, profile_key: str = "standard") -> dict:
    """YCNodes 二采：VSR 升清、VAE 回灌与低噪 Sigma 精修。"""
    width, height = dimensions(ratio, megapixels)
    condition = easy_condition(prompt, seconds, ratio, width, height)
    # Keep the model resident while YCNodes refines the re-encoded source latent.
    condition["force_offload"] = False
    graph = common_nodes(profile_key)
    graph.update({
        "10": {"class_type": "LoadVideo", "inputs": {"file": source_video}},
        "11": {"class_type": "GetVideoComponents", "inputs": {"video": ["10", 0]}},
        "13": {"class_type": "VAEEncode", "inputs": {
            "pixels": ["11", 0] if ENHANCE_PREUPSCALED else ["12", 0],
            "vae": ["8", 2]}},
        "14": {"class_type": "VAEEncodeAudio", "inputs": {"audio": ["11", 1], "vae": ["8", 3]}},
        "15": {"class_type": "LTXVConcatAVLatent", "inputs": {
            "video_latent": ["13", 0], "audio_latent": ["14", 0]}},
        "20": {"class_type": "MiniMaxH3Easy", "inputs": condition},
        "8": {"class_type": "MiniMaxH3EasyOutput", "inputs": {"h3_context": ["20", 1]}},
        "21": {"class_type": "MiniMaxH3EasySecondPassConditioning", "inputs": {
            "h3_context": ["20", 1], "second_pass_video_latent": ["13", 0]}},
        "102": {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": ["20", 0]}},
        "50": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "51": {"class_type": "BasicGuider", "inputs": {"model": ["102", 0], "conditioning": ["21", 0]}},
        "52": {"class_type": "BasicScheduler", "inputs": {
            "model": ["102", 0], "scheduler": "beta", "steps": 4,
            "denoise": ENHANCE_DENOISE}},
        "54": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        # YCNodes H3 分块二采：每个 video tile 都携带完整 audio latent。
        "55": {"class_type": "H3TiledSampler", "inputs": {
            "noise": ["50", 0], "guider": ["51", 0], "sampler": ["54", 0],
            "sigmas": ["53", 0] if ENHANCE_EXTRA_STEPS else ["52", 0],
            "latent_image": ["15", 0],
            "bypass_tiling": ENHANCE_BYPASS_TILING, "tile_axis": "auto", "n_tiles": ENHANCE_TILES,
            "tile_overlap": 8, "max_size_for_no_tile": 24,
            "target_frames": frames_for_seconds(seconds),
            "frame_padding_mode": "replicate_last", "debug": False}},
        "56": {"class_type": "VAEDecode", "inputs": {"samples": ["55", 0], "vae": ["8", 2]}},
        "57": {"class_type": "CreateVideo", "inputs": {
            "images": ["56", 0], "audio": ["11", 1], "fps": 24.0, "bit_depth": 8}},
        "58": {"class_type": "SaveVideo", "inputs": {
            "video": ["57", 0], "filename_prefix": "h3/enhanced", "format": "mp4", "codec": "h264"}},
    })
    if not ENHANCE_PREUPSCALED:
        graph["12"] = {"class_type": "ImageResizeKJv2", "inputs": {
            "image": ["11", 0], "width": width, "height": height,
            "upscale_method": "nvidia_rtx_vsr", "keep_proportion": "crop",
            "pad_color": "0, 0, 0", "crop_position": "center",
            "divisible_by": 0, "device": "cpu"}}
    if ENHANCE_EXTRA_STEPS:
        graph["53"] = {"class_type": "H3SigmaRefiner", "inputs": {
            "sigmas": ["52", 0], "extra_steps": ENHANCE_EXTRA_STEPS,
            "start_at_sigma": ENHANCE_SIGMA_START,
            "end_at_sigma": 0.0, "spacing": "cosine"}}
    if _solattn_enabled():
        graph["104"] = _solattn_node("20", "102")
        graph["51"]["inputs"]["model"] = ["104", 0]
        graph["52"]["inputs"]["model"] = ["104", 0]
    return graph


def rtx_only_workflow(source_video: str, ratio: str, megapixels: float) -> dict:
    """RTX VSR-only control path: resize pixels and preserve the original audio."""
    width, height = dimensions(ratio, megapixels)
    return {
        "10": {"class_type": "LoadVideo", "inputs": {"file": source_video}},
        "11": {"class_type": "GetVideoComponents", "inputs": {"video": ["10", 0]}},
        "12": {"class_type": "ImageResizeKJv2", "inputs": {
            "image": ["11", 0], "width": width, "height": height,
            "upscale_method": "nvidia_rtx_vsr", "keep_proportion": "crop",
            "pad_color": "0, 0, 0", "crop_position": "center",
            "divisible_by": 0, "device": "cpu"}},
        "57": {"class_type": "CreateVideo", "inputs": {
            "images": ["12", 0], "audio": ["11", 1], "fps": 24.0, "bit_depth": 8}},
        "58": {"class_type": "SaveVideo", "inputs": {
            "video": ["57", 0], "filename_prefix": "h3/rtx-only",
            "format": "mp4", "codec": "h264"}},
    }


def latent_upscale_node(video_node: str, width: int, height: int,
                        enable_chunking: bool) -> dict:
    return {"class_type": "MinimaxH3LatentUpscaler3D", "inputs": {
        "latent": [video_node, 0],
        "model_name": LATENT_UPSCALER_MODEL,
        # ComfyUI V3 dynamic inputs are flattened in API prompts. The runtime
        # reconstructs this as {mode, width, height} for node.execute().
        "mode": "target dimensions",
        "mode.width": width,
        "mode.height": height,
        "align": 32,
        "enable_chunking": enable_chunking,
        "device": "cuda",
        "precision": "fp16",
    }}


def latent_refine_workflow(prompt: str, video_latent: str, audio_latent: str,
                           seconds: float, ratio: str, megapixels: float, seed: int,
                           profile_key: str = "standard", denoise: float = 0.35,
                           sigma_start: float = 0.7,
                           tier: str = "1080p", output_mode: str = "both") -> dict:
    """Refined HD: latent upscale plus the existing 4+1 low-noise Sigma pass."""
    targets = tier_targets(tier, ratio)
    width, height = targets[-1] if targets else dimensions(ratio, megapixels)
    condition = easy_condition(prompt, seconds, ratio, width, height)
    condition["force_offload"] = tier == "2k"
    graph = common_nodes(profile_key)
    graph.update({
        "10": {"class_type": "LoadLatent", "inputs": {"latent": video_latent}},
        "11": {"class_type": "LoadLatent", "inputs": {"latent": audio_latent}},
        "12": latent_upscale_node("10", width, height, seconds > 5),
        "13": {"class_type": "LTXVConcatAVLatent", "inputs": {
            "video_latent": ["12", 0], "audio_latent": ["11", 0]}},
        "20": {"class_type": "MiniMaxH3Easy", "inputs": condition},
        "8": {"class_type": "MiniMaxH3EasyOutput", "inputs": {"h3_context": ["20", 1]}},
        "21": {"class_type": "MiniMaxH3EasySecondPassConditioning", "inputs": {
            "h3_context": ["20", 1], "second_pass_video_latent": ["12", 0]}},
        "102": {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {
            "model": ["20", 0]}},
        "50": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "51": {"class_type": "BasicGuider", "inputs": {
            "model": ["102", 0], "conditioning": ["21", 0]}},
        "52": {"class_type": "BasicScheduler", "inputs": {
            "model": ["102", 0], "scheduler": "beta", "steps": 2, "denoise": denoise}},
        "53": {"class_type": "H3SigmaRefiner", "inputs": {
            "sigmas": ["52", 0], "extra_steps": 0, "start_at_sigma": sigma_start,
            "end_at_sigma": 0.0, "spacing": "cosine"}},
        "54": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "55": {"class_type": "H3TiledSampler", "inputs": {
            "noise": ["50", 0], "guider": ["51", 0], "sampler": ["54", 0],
            "sigmas": ["53", 0], "latent_image": ["13", 0],
            "bypass_tiling": False, "tile_axis": "auto",
            "n_tiles": H3_2K_TILES if tier == "2k" else 2,
            "tile_overlap": 8, "max_size_for_no_tile": 24,
            "target_frames": frames_for_seconds(seconds),
            "frame_padding_mode": "replicate_last", "debug": False}},
    })
    if output_mode in {"both", "video"}:
        graph.update({
            "56": {"class_type": "VAEDecode", "inputs": {"samples": ["55", 0], "vae": ["8", 2]}},
            "57": {"class_type": "VAEDecodeAudio", "inputs": {
                "samples": ["55", 0], "vae": ["8", 3]}},
            "58": {"class_type": "CreateVideo", "inputs": {
                "images": ["56", 0], "audio": ["57", 0], "fps": 24.0, "bit_depth": 8}},
            "59": {"class_type": "SaveVideo", "inputs": {
                "video": ["58", 0], "filename_prefix": "h3/latent_refined",
                "format": "mp4", "codec": "h264"}},
        })
    if output_mode in {"both", "latent"}:
        graph.update({
            "60": {"class_type": "LTXVSeparateAVLatent", "inputs": {
                "av_latent": ["55", 1]}},
            "61": {"class_type": "SaveLatent", "inputs": {
                "samples": ["60", 0], "filename_prefix": "h3/latent_video"}},
            "62": {"class_type": "SaveLatent", "inputs": {
                "samples": ["60", 1], "filename_prefix": "h3/latent_audio"}},
        })
    if _solattn_enabled():
        graph["104"] = _solattn_node("20", "102")
        graph["51"]["inputs"]["model"] = ["104", 0]
        graph["52"]["inputs"]["model"] = ["104", 0]
    return graph


async def submit(graph: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{COMFY}/prompt", json={"prompt": graph})
    if response.status_code >= 400:
        raise HTTPException(500, f"推理后端拒绝任务: {response.text[:1000]}")
    return response.json()


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse((ROOT / "web" / "index.html").read_text("utf-8"))


@app.get("/health")
async def health():
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            backend = (await client.get(f"{COMFY}/system_stats")).status_code == 200
        except Exception:
            backend = False
    return {"ok": backend, "backend": backend}


@app.get("/api/profiles")
async def profiles():
    return profile_status(ROOT)


def validate_output_mode(value: str) -> str:
    if value not in {"both", "video", "latent"}:
        raise HTTPException(400, "输出模式无效")
    return value


@app.post("/api/generate/text")
async def generate_text(
    prompt: Annotated[str, Form()],
    duration: Annotated[float, Form()] = 5, ratio: Annotated[str, Form()] = "16:9",
    quality: Annotated[float, Form()] = 0.4, seed: Annotated[int, Form()] = 0,
    profile: Annotated[str, Form()] = "standard",
    output_mode: Annotated[str, Form()] = "both",
):
    validate_request(prompt, duration, ratio, quality)
    require_profile(profile)
    graph = text_workflow(
        prompt, duration, ratio, quality, seed or uuid.uuid4().int % (2**63),
        profile, validate_output_mode(output_mode),
    )
    return await submit(graph)


@app.post("/api/generate/text-latent-hd")
async def generate_text_latent_hd(
    prompt: Annotated[str, Form()],
    duration: Annotated[float, Form()] = 5, ratio: Annotated[str, Form()] = "16:9",
    quality: Annotated[float, Form()] = 0.9,
    output_quality: Annotated[float, Form()] = 2.0,
    seed: Annotated[int, Form()] = 0,
    profile: Annotated[str, Form()] = "standard",
    refine_denoise: Annotated[float, Form()] = 0.35,
    tier: Annotated[str, Form()] = "1080p",
    refine_steps: Annotated[int, Form()] = 2,
    refine_extra_steps: Annotated[int, Form()] = 0,
):
    validate_request(prompt, duration, ratio, quality)
    validate_request(prompt, duration, ratio, output_quality)
    require_profile(profile)
    if not 0.05 <= refine_denoise <= 0.5:
        raise HTTPException(400, "精修 denoise 必须在 0.05–0.5 之间")
    if tier not in TIER_LADDER:
        raise HTTPException(400, f"不支持的输出档位: {tier}")
    if not 1 <= refine_steps <= 8:
        raise HTTPException(400, "精修步数必须在 1–8 之间")
    graph = text_latent_hd_workflow(
        prompt, duration, ratio, quality, output_quality,
        seed or uuid.uuid4().int % (2**63), profile, refine_denoise,
        tier, refine_steps, refine_extra_steps,
    )
    return await submit(graph)


@app.post("/api/generate/image")
async def generate_image(
    prompt: Annotated[str, Form()], first_frame: Annotated[UploadFile, File()],
    last_frame: Annotated[UploadFile | None, File()] = None,
    duration: Annotated[float, Form()] = 5, ratio: Annotated[str, Form()] = "16:9",
    quality: Annotated[float, Form()] = 0.4, seed: Annotated[int, Form()] = 0,
    profile: Annotated[str, Form()] = "standard",
    output_mode: Annotated[str, Form()] = "both",
):
    validate_request(prompt, duration, ratio, quality)
    require_profile(profile)
    first = await save_upload(first_frame, "image")
    last = await save_upload(last_frame, "image")
    graph = image_workflow(
        prompt, first, last, duration, ratio, quality,
        seed or uuid.uuid4().int % (2**63), profile,
        validate_output_mode(output_mode),
    )
    return await submit(graph)


@app.post("/api/generate/image-latent-hd")
async def generate_image_latent_hd(
    prompt: Annotated[str, Form()], first_frame: Annotated[UploadFile, File()],
    last_frame: Annotated[UploadFile | None, File()] = None,
    duration: Annotated[float, Form()] = 5, ratio: Annotated[str, Form()] = "16:9",
    quality: Annotated[float, Form()] = 0.9,
    output_quality: Annotated[float, Form()] = 2.0,
    seed: Annotated[int, Form()] = 0,
    profile: Annotated[str, Form()] = "standard",
    refine_denoise: Annotated[float, Form()] = 0.35,
    tier: Annotated[str, Form()] = "1080p",
    refine_steps: Annotated[int, Form()] = 2,
    refine_extra_steps: Annotated[int, Form()] = 0,
):
    validate_request(prompt, duration, ratio, quality)
    validate_request(prompt, duration, ratio, output_quality)
    require_profile(profile)
    if not 0.05 <= refine_denoise <= 0.5:
        raise HTTPException(400, "精修 denoise 必须在 0.05–0.5 之间")
    if tier not in TIER_LADDER:
        raise HTTPException(400, f"不支持的输出档位: {tier}")
    if not 1 <= refine_steps <= 8:
        raise HTTPException(400, "精修步数必须在 1–8 之间")
    first = await save_upload(first_frame, "image")
    last = await save_upload(last_frame, "image")
    graph = image_latent_hd_workflow(
        prompt, first, last, duration, ratio, quality, output_quality,
        seed or uuid.uuid4().int % (2**63), profile, refine_denoise,
        tier, refine_steps, refine_extra_steps,
    )
    return await submit(graph)


@app.post("/api/generate/reference")
async def generate_reference(
    prompt: Annotated[str, Form()], images: Annotated[list[UploadFile], File()] = [],
    videos: Annotated[list[UploadFile], File()] = [], audios: Annotated[list[UploadFile], File()] = [],
    duration: Annotated[float, Form()] = 5, ratio: Annotated[str, Form()] = "16:9",
    quality: Annotated[float, Form()] = 0.4, seed: Annotated[int, Form()] = 0,
    video_audio_flags: Annotated[str, Form()] = "",
    profile: Annotated[str, Form()] = "standard",
    output_mode: Annotated[str, Form()] = "both",
):
    validate_request(prompt, duration, ratio, quality)
    require_profile(profile)
    if not images and not videos and not audios: raise HTTPException(400, "至少上传一个参考素材")
    if len(images) > 9 or len(videos) > 3 or len(audios) > 3: raise HTTPException(400, "参考素材数量超过 H3 限制")
    if len(images) + len(videos) + len(audios) > 12: raise HTTPException(400, "所有参考素材合计不能超过 12 个")
    image_names = [await save_upload(x, "image") for x in images if x.filename]
    video_names = [await save_upload(x, "video") for x in videos if x.filename]
    audio_names = [await save_upload(x, "audio") for x in audios if x.filename]
    flags = [x == "1" for x in video_audio_flags.split(",")] if video_audio_flags else [True] * len(video_names)
    graph = reference_workflow(prompt, image_names, video_names, audio_names, duration, ratio,
                               quality, seed or uuid.uuid4().int % (2**63), flags, profile,
                               validate_output_mode(output_mode))
    return await submit(graph)


@app.post("/api/generate/reference-latent-hd")
async def generate_reference_latent_hd(
    prompt: Annotated[str, Form()], images: Annotated[list[UploadFile], File()] = [],
    videos: Annotated[list[UploadFile], File()] = [], audios: Annotated[list[UploadFile], File()] = [],
    duration: Annotated[float, Form()] = 5, ratio: Annotated[str, Form()] = "16:9",
    quality: Annotated[float, Form()] = 0.9,
    output_quality: Annotated[float, Form()] = 2.0,
    seed: Annotated[int, Form()] = 0,
    video_audio_flags: Annotated[str, Form()] = "",
    profile: Annotated[str, Form()] = "standard",
    refine_denoise: Annotated[float, Form()] = 0.35,
    tier: Annotated[str, Form()] = "1080p",
    refine_steps: Annotated[int, Form()] = 2,
    refine_extra_steps: Annotated[int, Form()] = 0,
):
    validate_request(prompt, duration, ratio, quality)
    validate_request(prompt, duration, ratio, output_quality)
    require_profile(profile)
    if not images and not videos and not audios:
        raise HTTPException(400, "至少上传一个参考素材")
    if len(images) > 9 or len(videos) > 3 or len(audios) > 3:
        raise HTTPException(400, "参考素材数量超过 H3 限制")
    if len(images) + len(videos) + len(audios) > 12:
        raise HTTPException(400, "所有参考素材合计不能超过 12 个")
    if not 0.05 <= refine_denoise <= 0.5:
        raise HTTPException(400, "精修 denoise 必须在 0.05–0.5 之间")
    if tier not in TIER_LADDER:
        raise HTTPException(400, f"不支持的输出档位: {tier}")
    if not 1 <= refine_steps <= 8:
        raise HTTPException(400, "精修步数必须在 1–8 之间")
    image_names = [await save_upload(x, "image") for x in images if x.filename]
    video_names = [await save_upload(x, "video") for x in videos if x.filename]
    audio_names = [await save_upload(x, "audio") for x in audios if x.filename]
    flags = [x == "1" for x in video_audio_flags.split(",")] if video_audio_flags else [True] * len(video_names)
    graph = reference_latent_hd_workflow(
        prompt, image_names, video_names, audio_names, duration, ratio, quality,
        output_quality, seed or uuid.uuid4().int % (2**63), flags, profile,
        refine_denoise, tier, refine_steps, refine_extra_steps,
    )
    return await submit(graph)


@app.post("/api/generate/enhance")
async def generate_enhance(
    prompt: Annotated[str, Form()], source_video: Annotated[UploadFile, File()],
    duration: Annotated[float, Form()], ratio: Annotated[str, Form()] = "16:9",
    quality: Annotated[float, Form()] = 0.9, seed: Annotated[int, Form()] = 0,
    profile: Annotated[str, Form()] = "standard",
):
    validate_request(prompt, duration, ratio, quality)
    require_profile(profile)
    source = await save_upload(source_video, "video")
    graph = enhance_workflow(prompt, source, duration, ratio, quality,
                             seed or uuid.uuid4().int % (2**63), profile)
    return await submit(graph)


@app.post("/api/generate/rtx-only")
async def generate_rtx_only(
    source_video: Annotated[UploadFile, File()],
    duration: Annotated[float, Form()], ratio: Annotated[str, Form()] = "16:9",
    quality: Annotated[float, Form()] = 2.0,
):
    # Keep the same public validation contract even though RTX VSR needs no prompt.
    validate_request("RTX VSR", duration, ratio, quality)
    source = await save_upload(source_video, "video")
    return await submit(rtx_only_workflow(source, ratio, quality))


@app.post("/api/generate/latent-refine")
async def generate_latent_refine(
    prompt: Annotated[str, Form()],
    video_latent: Annotated[UploadFile, File()],
    audio_latent: Annotated[UploadFile, File()],
    duration: Annotated[float, Form()], ratio: Annotated[str, Form()] = "16:9",
    quality: Annotated[float, Form()] = 0.9, seed: Annotated[int, Form()] = 0,
    profile: Annotated[str, Form()] = "standard",
    refine_denoise: Annotated[float, Form()] = 0.35,
    refine_sigma: Annotated[float, Form()] = 0.7,
    tier: Annotated[str, Form()] = "1080p",
    output_mode: Annotated[str, Form()] = "both",
):
    validate_request(prompt, duration, ratio, quality)
    require_profile(profile)
    if not 0.05 <= refine_denoise <= 0.5:
        raise HTTPException(400, "精修 denoise 必须在 0.05–0.5 之间")
    if not 0.0 <= refine_sigma <= 1.0:
        raise HTTPException(400, "精修 Sigma 必须在 0–1 之间")
    if tier not in TIER_LADDER or tier == "720p":
        raise HTTPException(400, f"不支持的精修档位: {tier}")
    video_name = await save_upload(video_latent, "latent")
    audio_name = await save_upload(audio_latent, "latent")
    graph = latent_refine_workflow(
        prompt, video_name, audio_name, duration, ratio, quality,
        seed or uuid.uuid4().int % (2**63), profile, refine_denoise, refine_sigma,
        tier, validate_output_mode(output_mode),
    )
    return await submit(graph)


@app.get("/api/tasks/{prompt_id}")
async def task(prompt_id: str):
    async with httpx.AsyncClient(timeout=15) as client:
        queue = (await client.get(f"{COMFY}/queue")).json()
        history = (await client.get(f"{COMFY}/history/{prompt_id}")).json()
    if prompt_id in history:
        item = history[prompt_id]
        status = item.get("status", {})
        files = []
        latents = []
        for output in item.get("outputs", {}).values():
            for values in output.values():
                if isinstance(values, list):
                    for value in values:
                        if isinstance(value, dict) and value.get("filename"):
                            if value["filename"].endswith(".latent"):
                                latent = dict(value)
                                latent["role"] = "audio" if "latent_audio" in value["filename"] else "video"
                                relative = ((value.get("subfolder") or "") + "/" + value["filename"]).lstrip("/")
                                target = (OUTPUT / relative).resolve()
                                if OUTPUT.resolve() in target.parents and target.is_file():
                                    latent["size"] = target.stat().st_size
                                    latent["path"] = relative
                                latents.append(latent)
                            else:
                                files.append(value)
        if not status.get("completed", True):
            return {"status": "error", "files": files, "latents": latents,
                    "messages": status.get("messages", [])}
        return {"status": "completed", "files": files, "latents": latents,
                "messages": status.get("messages", [])}
    running = any(x[1] == prompt_id for x in queue.get("queue_running", []))
    pending = any(x[1] == prompt_id for x in queue.get("queue_pending", []))
    return {"status": "running" if running else "queued" if pending else "unknown"}


@app.get("/api/files/{filename:path}")
async def result_file(filename: str, download: bool = False):
    target = (OUTPUT / filename).resolve()
    if OUTPUT.resolve() not in target.parents or not target.is_file(): raise HTTPException(404)
    return FileResponse(target, filename=target.name if download else None, media_type="video/mp4")
