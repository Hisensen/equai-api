"""共绩算力 Job 批处理客户端。

替代原先的 RunPod 调用：每个生成请求创建一个独立的 Job，任务跑完容器
自行退出，不存在常驻节点的空闲成本。任务参数通过容器环境变量传入，
成品由容器通过一次性 HTTPS 授权直接上传到配置的传输后端。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from local_storage import output_relative
from object_storage import bucket_name, presigned_origin_url, presigned_put_url
from transfer_storage import (
    backend as transfer_backend,
    callback_configured,
    signed_output_url,
    signed_result_url,
    signed_runtime_url,
)

BASE = os.getenv("GONGJI_BASE", "https://openapi.suanli.cn").rstrip("/")
TOKEN = os.getenv("GONGJI_TOKEN", "").strip()
MODELS_DIR = os.getenv("GONGJI_MODELS_DIR", "/models")
REPO_USER = os.getenv("GONGJI_REPO_USERNAME", "").strip()
REPO_PASSWORD = os.getenv("GONGJI_REPO_PASSWORD", "").strip()
SHM_MIB = int(os.getenv("GONGJI_SHM_MIB", "30720"))
TIMEOUT_SEC = int(os.getenv("GONGJI_TIMEOUT_SEC", "2400"))
USE_SPOT = os.getenv("GONGJI_USE_SPOT", "0").strip() == "1"
ESTIMATED_SEC = int(os.getenv("GONGJI_ESTIMATED_SEC", "600"))
COMFY_ARGS = os.getenv(
    "GONGJI_COMFY_ARGS",
    "--use-ck-attention --reserve-vram 1 --fast-disk",
)

# 资源 mark 是一段加密串，与规格绑定且会变化，因此运行时查询并缓存，
# 不写死在代码里。
_RESOURCE_CACHE: dict[str, dict[str, Any]] = {}
_RESOURCE_TTL = 1800.0


class GongjiError(RuntimeError):
    pass


class NoGpuAvailableError(GongjiError):
    """No configured region currently reports an available GPU."""


def _direct_local_media_urls(worker_input: dict[str, Any], backend: str) -> dict[str, Any]:
    """Use the TLS-valid transfer IP for signed assets sent to GPU workers.

    The public hostname is pinned to this IP inside the GPU container, but
    the origin certificate covers only the IP. The grant signs the path, so
    changing just the URL host keeps the existing authorization valid.
    """
    if backend != "local":
        return worker_input
    transfer_ip = os.getenv("H3_TRANSFER_HOST_IP", "").strip()
    public_host = urlsplit(os.getenv("H3_TRANSFER_BASE_URL", "")).hostname
    if not transfer_ip or not public_host:
        return worker_input
    media = worker_input.get("media")
    if not isinstance(media, list):
        return worker_input
    direct = []
    for item in media:
        if not isinstance(item, dict):
            direct.append(item)
            continue
        url = str(item.get("url") or "")
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.hostname == public_host
                and parsed.path.startswith("/internal/worker/tasks/")):
            item = {**item, "url": urlunsplit(("https", transfer_ip, parsed.path,
                                               parsed.query, parsed.fragment))}
        direct.append(item)
    return {**worker_input, "media": direct}


def _runtime_bundle_url(
    job_id: str, key: str, expires: int,
    env_name: str = "H3_LOCAL_RUNTIME_OVERLAY_PATH",
    backend_name: str | None = None,
) -> str:
    """Resolve a worker bundle from local media storage or legacy COS."""
    local = os.getenv(env_name, "").strip()
    if local and os.path.isfile(local):
        root = os.getenv("H3_LOCAL_RUNTIME_DIR", "/data/runtime").rstrip("/") + "/"
        if local.startswith(root):
            return signed_runtime_url(job_id, local[len(root):], expires=expires)
    if (backend_name or transfer_backend()) == "local":
        raise GongjiError(f"本地运行包不存在，无法提交共绩任务: {local or key}")
    return presigned_origin_url(key, expires=expires)


def _profile(quality: str, workload: str = "video") -> dict[str, Any]:
    if quality not in {"720p", "1080p", "2k"}:
        raise GongjiError(f"不支持的清晰度档位: {quality}")
    standard_prefix = {"720p": "GONGJI_720", "1080p": "GONGJI_1080", "2k": "GONGJI_2K"}[quality]
    if workload not in {"video", "remix"}:
        raise GongjiError(f"不支持的任务资源池: {workload}")
    prefix = (
        standard_prefix if workload == "video"
        else {"720p": "GONGJI_REMIX_720", "1080p": "GONGJI_REMIX_1080", "2k": "GONGJI_REMIX_2K"}[quality]
    )
    # Until a dedicated 2K image is published, the latent-capable 1080 image
    # remains a safe rollback/default.  The worker receives the explicit tier
    # in H3_JOB_INPUT and can be switched to a dedicated image with env only.
    fallback_prefix = "GONGJI_1080" if quality == "2k" else "GONGJI_720"
    defaults = {
        "image": os.getenv("GONGJI_IMAGE", ""),
        "storage_id": os.getenv("GONGJI_STORAGE_ID", "0"),
        "region": os.getenv("GONGJI_REGION_NAME", "福建四区"),
        "gpu": "5090" if quality == "1080p" else os.getenv("GONGJI_GPU_NAME", "4090"),
        "gpu_count": os.getenv("GONGJI_GPU_COUNT", "1"),
        "comfy_args": COMFY_ARGS,
        "solattn": os.getenv("H3_SOLATTN", "tau:1.3:0.0:1.0"),
        "estimated_sec": os.getenv("GONGJI_ESTIMATED_SEC", str(ESTIMATED_SEC)),
        "timeout_sec": os.getenv("GONGJI_TIMEOUT_SEC", str(TIMEOUT_SEC)),
    }

    def configured_value(suffix: str, default: Any) -> Any:
        # A dedicated workload may override only the region/storage while
        # inheriting the validated worker image and sampler settings.
        standard = os.getenv(
            standard_prefix + suffix,
            os.getenv(fallback_prefix + suffix, default),
        )
        return os.getenv(prefix + suffix, standard)

    return {
        "image": str(configured_value("_IMAGE", defaults["image"])).strip(),
        "storage_id": int(configured_value("_STORAGE_ID", defaults["storage_id"]) or 0),
        "region": configured_value("_REGION_NAME", defaults["region"]),
        "gpu": configured_value("_GPU_NAME", defaults["gpu"]),
        "gpu_count": int(configured_value("_GPU_COUNT", defaults["gpu_count"])),
        "comfy_args": configured_value("_COMFY_ARGS", defaults["comfy_args"]),
        "solattn": configured_value("_SOLATTN", defaults["solattn"]),
        "estimated_sec": int(configured_value("_ESTIMATED_SEC", defaults["estimated_sec"])),
        "timeout_sec": int(configured_value("_TIMEOUT_SEC", defaults["timeout_sec"])),
    }


def _image_has_baked_standard_runtime(image: str | None) -> bool:
    """Return whether a standard video image contains its Worker runtime.

    Images tagged with ``-baked-runtime-`` carry the API/transfer code and
    custom nodes directly in the image, so they must not download the legacy
    Tencent-hosted runtime overlay at pod startup.
    """
    reference = str(image or "").rsplit("/", 1)[-1].lower()
    return "-baked-runtime-" in reference


def configured(quality: str | None = None) -> bool:
    profiles = [_profile(quality)] if quality else [_profile("720p"), _profile("1080p"), _profile("2k")]
    return bool(TOKEN and all(item["image"] and item["storage_id"] for item in profiles))


def _headers() -> dict[str, str]:
    if not TOKEN:
        raise GongjiError("GONGJI_TOKEN 尚未配置")
    return {
        "token": TOKEN,
        "version": "1.0.0",
        "timestamp": str(int(time.time() * 1000)),
        "Content-Type": "application/json",
    }


def _unwrap(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        raise GongjiError(f"共绩接口 HTTP {response.status_code}: {response.text[:300]}")
    body = response.json()
    if str(body.get("code")) != "0000":
        raise GongjiError(f"共绩接口返回 {body.get('code')}: {body.get('message')}")
    return body.get("data")


async def _resource(profile: dict[str, Any]) -> dict[str, Any]:
    now = time.monotonic()
    cache_key = f'{profile["region"]}:{profile["gpu"]}:{profile["gpu_count"]}'
    cached = _RESOURCE_CACHE.get(cache_key)
    if cached and now - cached["fetched_at"] < _RESOURCE_TTL:
        return cached["mark"]

    async with httpx.AsyncClient(timeout=60) as client:
        data = _unwrap(await client.get(f"{BASE}/api/deployment/resource/search", headers=_headers()))

    for item in data.get("results") or []:
        for region in (item.get("region_map") or {}).values():
            mark = region.get("mark") or {}
            spec = mark.get("resource") or {}
            if (
                region.get("region_name") == profile["region"]
                and spec.get("gpu_name") == profile["gpu"]
                and spec.get("gpu_count") == profile["gpu_count"]
            ):
                _RESOURCE_CACHE[cache_key] = {"mark": mark, "fetched_at": now}
                return mark
    raise GongjiError(
        f'未找到匹配资源: {profile["region"]} {profile["gpu"]} ×{profile["gpu_count"]}'
    )


def _backup_profile(
    profile: dict[str, Any], quality: str, workload: str,
    backup_prefix: str | None = None,
) -> dict[str, Any] | None:
    """Build an optional, per-tier backup profile without changing other workers.

    Example for ordinary 720p jobs:
      GONGJI_720_BACKUP_REGION_NAME=天津一区
      GONGJI_720_BACKUP_GPU_NAME=5090
      GONGJI_720_BACKUP_GPU_COUNT=1
      GONGJI_720_BACKUP_CPU_CORES=16
      GONGJI_720_BACKUP_STORAGE_ID=<Tianjin storage id>
      GONGJI_720_BACKUP_IMAGE=<Tianjin-compatible image>

    Remix profiles use GONGJI_REMIX_720_BACKUP_* instead.
    """
    prefix = backup_prefix or (
        {"720p": "GONGJI_720", "1080p": "GONGJI_1080", "2k": "GONGJI_2K"}[quality]
        if workload == "video"
        else {"720p": "GONGJI_REMIX_720", "1080p": "GONGJI_REMIX_1080", "2k": "GONGJI_REMIX_2K"}[quality]
    ) + "_BACKUP"
    region = os.getenv(prefix + "_REGION_NAME", "").strip()
    gpu = os.getenv(prefix + "_GPU_NAME", "").strip()
    if not region and not gpu:
        return None
    if not region or not gpu:
        raise GongjiError(f"{prefix} 备用区域和 GPU 型号必须同时配置")
    count = int(os.getenv(prefix + "_GPU_COUNT", "1"))
    cpu_raw = os.getenv(prefix + "_CPU_CORES", "").strip()
    storage_raw = os.getenv(prefix + "_STORAGE_ID", "").strip()
    solattn = os.getenv(prefix + "_SOLATTN", "").strip()
    if not solattn:
        # The 5090 sibling image ships the ABI-matched Comfy Kitchen CUDA
        # extension with Sol-Attn kernels and native sm_120 support, so inherit
        # the same configured Sol-Attn mode as the primary unless explicitly
        # overridden for this backup profile.
        solattn = profile["solattn"]
    return {
        **profile,
        "region": region,
        "gpu": gpu,
        "gpu_count": count,
        "cpu_cores": int(cpu_raw) if cpu_raw else None,
        "storage_id": int(storage_raw) if storage_raw else 0,
        "image": os.getenv(prefix + "_IMAGE", "").strip(),
        "solattn": solattn,
        "_backup": True,
    }


async def _select_available_profile(
    profile: dict[str, Any], quality: str, workload: str,
    *, backup_prefix: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pick the first configured profile with live inventory, primary first.

    Inventory is intentionally fetched for every submission; resource marks can
    be cached, but a 30-minute inventory cache would send users into known-empty
    regions. The selected Job still contains exactly one region.
    """
    candidates = [profile]
    backup = _backup_profile(profile, quality, workload, backup_prefix=backup_prefix)
    if backup:
        candidates.append(backup)

    async with httpx.AsyncClient(timeout=60) as client:
        data = _unwrap(await client.get(f"{BASE}/api/deployment/resource/search", headers=_headers()))

    results = (data or {}).get("results") or []
    for candidate in candidates:
        for item in results:
            for region in (item.get("region_map") or {}).values():
                mark = region.get("mark") or {}
                resource = mark.get("resource") or {}
                if (
                    region.get("region_name") != candidate["region"]
                    or resource.get("gpu_name") != candidate["gpu"]
                    or int(resource.get("gpu_count") or 0) != candidate["gpu_count"]
                ):
                    continue
                cpu_cores = candidate.get("cpu_cores")
                if cpu_cores is not None and int(resource.get("cpu_cores") or 0) != cpu_cores:
                    continue
                try:
                    inventory = int(region.get("inventory"))
                except (TypeError, ValueError):
                    # Unknown inventory is not proof that a card is available.
                    continue
                if inventory < 1:
                    continue
                if candidate.get("_backup") and (
                    not candidate.get("image") or not candidate.get("storage_id")
                ):
                    raise GongjiError(
                        f"{candidate['region']} 有可用 GPU，但备用镜像或同区域模型存储尚未配置"
                    )
                return candidate, mark

    raise NoGpuAvailableError("暂时无GPU，稍后重试")


def _output_upload_targets(
    job_id: str,
    merchant_id: str,
    expires: int,
    transfer_backend: str,
    output_names: list[str] | None = None,
    *, base_url: str | None = None,
) -> list[dict[str, str]]:
    """Create exact-object upload grants so GPU jobs never receive storage credentials."""
    names = list(output_names or [])
    names = [
        names[index] if index < len(names) else f"output-{index + 1}.mp4"
        for index in range(4)
    ]
    if transfer_backend == "local":
        url_kwargs = {"base_url": base_url} if base_url else {}
        return [
            {
                "bucket": "local",
                "key": output_relative(merchant_id, job_id, index, names[index]),
                "name": names[index],
                "url": signed_output_url(job_id, index, expires=expires, **url_kwargs),
            }
            for index in range(4)
        ]
    prefix = os.getenv("COS_OUTPUT_PREFIX", "outputs").strip("/")
    bucket = bucket_name()
    targets = []
    for index in range(4):
        key = f"{prefix}/{merchant_id}/{job_id}/{index:02d}-{names[index]}"
        targets.append({
            "bucket": bucket,
            "key": key,
            "name": names[index],
            "url": presigned_put_url(key, expires=expires),
        })
    return targets


def _result_upload_target(
    job_id: str,
    merchant_id: str,
    expires: int,
    transfer_backend: str,
    *, base_url: str | None = None,
) -> dict[str, str]:
    if transfer_backend == "local" or callback_configured():
        url_kwargs = {"base_url": base_url} if base_url else {}
        return {"key": "_job_result.json", "url": signed_result_url(job_id, expires=expires, **url_kwargs)}
    prefix = os.getenv("COS_OUTPUT_PREFIX", "outputs").strip("/")
    key = f"{prefix}/{merchant_id}/{job_id}/_job_result.json"
    return {"key": key, "url": presigned_put_url(key, expires=expires)}


async def create_job(
    job_id: str,
    merchant_id: str,
    worker_input: dict[str, Any],
    quality: str,
    transfer_backend: str = "cos",
    *,
    service_image: str | None = None,
    task_prefix: str = "h3",
    timeout_sec: int | None = None,
    estimated_sec: int | None = None,
    start_script: dict[str, Any] | None = None,
    extra_env: list[dict[str, str]] | None = None,
    workload: str = "video",
    selected_profile: dict[str, Any] | None = None,
    selected_mark: dict[str, Any] | None = None,
) -> int:
    """创建一个生成任务，返回共绩的 task_id。"""
    profile = selected_profile or _profile(quality, workload)
    if not TOKEN or not profile["image"] or not profile["storage_id"]:
        raise GongjiError(f"共绩 {quality} Job 配置不完整")

    if selected_mark is not None:
        mark = selected_mark
    elif service_image is None and task_prefix in {"h3", "h3-commerce"}:
        # Standard video and commerce remix jobs use a live inventory check
        # and an optional per-tier backup. Dedicated worker images (music,
        # digital human, Qwen analysis, long video, etc.) keep their own path.
        profile, mark = await _select_available_profile(profile, quality, workload)
    else:
        mark = await _resource(profile)
    env = [
        {"name": "H3_MODELS_DIR", "value": MODELS_DIR},
        {"name": "H3_ENABLE_SERVERLESS_HANDLER", "value": "0"},
        {"name": "H3_ROOT", "value": "/app"},
        {"name": "H3_COMFY_ARGS", "value": profile["comfy_args"]},
        {"name": "H3_JOB_ID", "value": job_id},
        {"name": "H3_JOB_INPUT", "value": json.dumps(
            _direct_local_media_urls(worker_input, transfer_backend), ensure_ascii=False,
        )},
        # Keep the existing optimized 4090 path. Blackwell currently falls
        # back to ComfyUI attention to avoid the 4090 SageAttention PTX kernel.
        {"name": "H3_USE_SAGE_ATTENTION", "value": (
            os.getenv("H3_5090_USE_SAGE_ATTENTION", "0").strip()
            if str(profile["gpu"]).startswith("5090")
            else os.getenv("H3_USE_SAGE_ATTENTION", "1").strip()
        )},
        # Matches the standalone GPU server's default Sol-Attn graph patch.
        # Set to "off" for an A/B run without rebuilding the worker image.
        {"name": "H3_SOLATTN", "value": profile["solattn"]},
        {"name": "H3_FFN_CHUNKS", "value": "1"},
        {"name": "H3_ATTN_HEAD_CHUNKS", "value": "1"},
    ]
    env.extend(extra_env or [])
    if os.getenv("H3_RUNTIME_FINGERPRINT", "0").strip() == "1":
        env.append({"name": "H3_RUNTIME_FINGERPRINT", "value": "1"})
    if os.getenv("H3_DIAGNOSTIC_ONLY", "0").strip() == "1":
        env.append({"name": "H3_DIAGNOSTIC_ONLY", "value": "1"})
    if os.getenv("H3_CUTE_SELFTEST", "0").strip() == "1":
        env.extend([
            {"name": "H3_CUTE_SELFTEST", "value": "1"},
            {
                "name": "H3_CUTE_SELFTEST_TOKENS",
                "value": os.getenv("H3_CUTE_SELFTEST_TOKENS", "8192 16384 30976"),
            },
            {
                "name": "H3_CUTE_SELFTEST_TAU",
                "value": os.getenv("H3_CUTE_SELFTEST_TAU", "1.3"),
            },
        ])
    solattn_impl = os.getenv("H3_SOLATTN_IMPL", "").strip()
    if solattn_impl:
        env.append({"name": "H3_SOLATTN_IMPL", "value": solattn_impl})
    if os.getenv("H3_SOLATTN_STRICT", "0").strip() == "1":
        env.append({"name": "H3_SOLATTN_STRICT", "value": "1"})
    triton_cache_dir = os.getenv("TRITON_CACHE_DIR", "").strip()
    if triton_cache_dir:
        env.append({"name": "TRITON_CACHE_DIR", "value": triton_cache_dir})
    if transfer_backend == "local" or callback_configured():
        transfer_hostname = os.getenv("H3_TRANSFER_HOSTNAME", "").strip()
        transfer_host_ip = os.getenv("H3_TRANSFER_HOST_IP", "").strip()
        if transfer_hostname or transfer_host_ip:
            if not transfer_hostname or not transfer_host_ip:
                raise GongjiError("H3_TRANSFER_HOSTNAME 与 H3_TRANSFER_HOST_IP 必须同时配置")
            env.extend([
                {"name": "H3_TRANSFER_HOSTNAME", "value": transfer_hostname},
                {"name": "H3_TRANSFER_HOST_IP", "value": transfer_host_ip},
            ])
    job_timeout = int(timeout_sec or profile["timeout_sec"])
    job_estimated = int(estimated_sec or profile["estimated_sec"])
    upload_expires = max(7200, job_timeout + 1800)
    commerce_base_url = (
        os.getenv("H3_COMMERCE_TRANSFER_BASE_URL", "").strip()
        if worker_input.get("_commerce_depth_worker") else ""
    )
    transfer_ip = os.getenv("H3_TRANSFER_HOST_IP", "").strip()
    # GPU-side HTTPS must use the IP present on the origin certificate. This
    # includes result uploads, otherwise a failed job loses its diagnostics.
    worker_base_url = commerce_base_url or (
        f"https://{transfer_ip}" if transfer_backend == "local" and transfer_ip else ""
    )
    worker_url_kwargs = {"base_url": worker_base_url} if worker_base_url else {}
    if timeout_sec is not None:
        env.append({"name": "H3_JOB_TIMEOUT_SECONDS", "value": str(job_timeout)})
    env.append({
        "name": "H3_OUTPUT_UPLOAD_TARGETS",
        "value": json.dumps(
            _output_upload_targets(
                job_id, merchant_id, upload_expires, transfer_backend,
                list(worker_input.get("_output_names") or []),
                **worker_url_kwargs,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    })
    env.append({
        "name": "H3_JOB_RESULT_UPLOAD_TARGET",
        "value": json.dumps(
            _result_upload_target(
            job_id, merchant_id, upload_expires, transfer_backend,
            **worker_url_kwargs,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    })

    # Standard 720p/1080p/2K images may need the Tencent direct-upload handler
    # overlay. Digital-human has its own fully baked image and must never fetch
    # a runtime bundle; ordinary long-video jobs provide their own start script.
    if (
        start_script is None
        and transfer_backend == "local"
        and worker_input.get("mode") != "digital_human_workflow"
        and not _image_has_baked_standard_runtime(service_image or profile["image"])
    ):
        overlay_key = os.getenv("GONGJI_STANDARD_OVERLAY_KEY", "h3-long-worker.tar.gz").strip()
        if overlay_key and os.path.isfile(os.getenv("H3_LOCAL_RUNTIME_OVERLAY_PATH", "")):
            env.append({
                "name": "H3_STANDARD_OVERLAY_URL",
                "value": _runtime_bundle_url(
                    job_id, overlay_key, upload_expires,
                    backend_name=transfer_backend,
                ),
            })
            start_script = {
                "command": ["/bin/bash"],
                "args": [
                    "-c",
                    (
                        "set -euo pipefail; "
                        "curl --fail --location --retry 4 --retry-all-errors "
                        "--connect-timeout 20 --max-time 180 "
                        '"$H3_STANDARD_OVERLAY_URL" -o /tmp/h3-standard-worker.tar.gz; '
                        "tar -xzf /tmp/h3-standard-worker.tar.gz -C /; "
                        "rm -f /tmp/h3-standard-worker.tar.gz; "
                        "exec /app/entrypoint.sh"
                    ),
                ],
            }

    service: dict[str, Any] = {
        "service_name": "container-01",
        "service_image": service_image or profile["image"],
        "storage_config": [{"storage_id": profile["storage_id"], "target_dir": MODELS_DIR}],
        "share_mem_config": [{"target_dir": "/dev/shm", "volume_name": "mem-default"}],
        "env": env,
        "resource_weight": {"cpu_weight": 1, "mem_weight": 1, "gpu_weight": 1},
    }
    if start_script:
        service["start_script"] = start_script
    if REPO_USER:
        service["repository_account"] = {
            "repository_username": REPO_USER,
            "repository_password": REPO_PASSWORD,
        }

    payload = {
        "task_name": f"{task_prefix}-{job_id}"[:60],
        "sub_type": "Spot" if USE_SPOT else None,
        "points": 1,
        "resources": [{
            "resource": mark["resource"],
            "region_name": profile["region"],
            "mark": mark["mark"],
            "weight": None,
        }],
        "share_mem_volumes": [{"name": "mem-default", "size_limit": SHM_MIB}],
        "services": [service],
        "job_support": {
            # Spot 任务据此寻找稳定的资源窗口，按需任务传 null。
            "estimated_exec_sec": job_estimated if USE_SPOT else None,
            "timeout_sec": job_timeout,
            "parallelism": 1,
            # 失败不重启，避免同一个任务反复占用 GPU 重复计费。
            "mod_param": {"default": {"backoff_limit": 0, "restart_policy": "Never"}, "index": None},
        },
    }

    async with httpx.AsyncClient(timeout=90) as client:
        data = _unwrap(await client.post(f"{BASE}/api/task/job/create", headers=_headers(), json=payload))
    task_id = (data or {}).get("task_id")
    if not task_id:
        raise GongjiError(f"共绩未返回 task_id: {data}")
    return int(task_id)


async def create_commerce_remix_job(
    job_id: str,
    merchant_id: str,
    worker_input: dict[str, Any],
    quality: str,
    transfer_backend: str = "local",
) -> int:
    """Run Tencent-prepared commerce input through one isolated H3 worker job.

    The small runtime overlay performs Depth Anything on the worker GPU before
    the normal H3 handler starts.  No second Gongji task is created.
    """
    from pathlib import Path

    if transfer_backend != "local":
        raise GongjiError("电商复刻深度处理当前要求腾讯云本地传输后端")
    runtime_root = Path(os.getenv("H3_LOCAL_RUNTIME_DIR", "/data/runtime")).resolve()
    overlay = Path(os.getenv(
        "H3_LOCAL_COMMERCE_DEPTH_OVERLAY_PATH",
        str(runtime_root / "commerce-depth-worker.tar.gz"),
    )).resolve()
    model_bundle = Path(os.getenv(
        "H3_LOCAL_COMMERCE_DEPTH_MODEL_PATH",
        str(runtime_root / "depth-anything-v2-small.tar.gz"),
    )).resolve()
    urls: list[str] = []
    for path in (overlay, model_bundle):
        try:
            relative = path.relative_to(runtime_root).as_posix()
        except ValueError as exc:
            raise GongjiError("电商复刻运行包必须位于腾讯云运行包目录内") from exc
        if not path.is_file():
            raise GongjiError(f"电商复刻运行文件缺失: {path.name}")
        url_kwargs = {"base_url": os.getenv("H3_COMMERCE_TRANSFER_BASE_URL", "").strip()}
        if not url_kwargs["base_url"]:
            url_kwargs = {}
        urls.append(__import__("transfer_storage").signed_runtime_url(
            # Gongji may leave a task queued before its container starts. Keep
            # the runtime packages fetchable for the same 26-hour window as
            # the existing local-transfer grants.
            job_id, relative, expires=93600,
            **url_kwargs,
        ))

    script = (
        "set -eu; "
        "STAGE=select-python; "
        "if [ -d /opt/h3-venv/bin ]; then PATH=/opt/h3-venv/bin:$PATH; export PATH; "
        "elif [ -d /opt/venv/bin ]; then PATH=/opt/venv/bin:$PATH; export PATH; fi; "
        "report_failure() { H3_BOOT_ERROR=\"bootstrap stage=$1 exit=$2\" python -c "
        "'import json,os,urllib.request; t=json.loads(os.environ[\"H3_JOB_RESULT_UPLOAD_TARGET\"]); "
        "d=json.dumps({\"ok\":False,\"job_id\":os.environ.get(\"H3_JOB_ID\",\"\"),"
        "\"error\":os.environ[\"H3_BOOT_ERROR\"]}).encode(); "
        "r=urllib.request.Request(t[\"url\"],data=d,headers={\"Content-Type\":\"application/json\"},method=\"PUT\"); "
        "urllib.request.urlopen(r,timeout=30).read()' || true; }; "
        "STAGE=download-overlay; "
        "if ! curl --fail --location --retry 3 --retry-delay 2 --connect-timeout 20 --max-time 120 "
        '"$H3_COMMERCE_DEPTH_OVERLAY_URL" -o /tmp/commerce-depth-worker.tar.gz; '
        "then report_failure download-overlay 1; exit 1; fi; "
        "STAGE=extract-overlay; "
        "if ! tar -xzf /tmp/commerce-depth-worker.tar.gz -C /; "
        "then report_failure extract-overlay 1; exit 1; fi; "
        "rm -f /tmp/commerce-depth-worker.tar.gz; "
        "STAGE=start-commerce-worker; "
        "exec python /app/commerce_depth_job.py"
    )
    return await create_job(
        job_id, merchant_id, worker_input, quality, transfer_backend,
        task_prefix="h3-commerce", workload="remix",
        start_script={"command": ["/bin/sh"], "args": ["-c", script]},
        extra_env=[
            {"name": "H3_COMMERCE_DEPTH_WORKER", "value": "1"},
            {"name": "H3_COMMERCE_DEPTH_OVERLAY_URL", "value": urls[0]},
            {"name": "H3_COMMERCE_DEPTH_MODEL_URL", "value": urls[1]},
            # Gongji model disks are mounted read-only in the worker image. The
            # commerce depth model is downloaded per task into writable scratch
            # space and must never be extracted under /models.
            {"name": "H3_COMMERCE_DEPTH_MODEL_DIR", "value": "/tmp/h3-commerce-depth/depth-anything-v2-small"},
        ],
    )


async def create_music_job(
    job_id: str,
    merchant_id: str,
    worker_input: dict[str, Any],
    transfer_backend: str = "local",
) -> int:
    """Submit the single GPU YuE2 worker using the prewarmed model mount."""
    image = os.getenv("GONGJI_MUSIC_IMAGE", "").strip()
    if not image:
        raise GongjiError("音乐 Worker 镜像尚未配置")
    return await create_job(
        job_id, merchant_id, worker_input, "720p", transfer_backend,
        service_image=image, task_prefix="h3-music",
        timeout_sec=int(os.getenv("GONGJI_MUSIC_TIMEOUT_SEC", "3600")),
        estimated_sec=int(os.getenv("GONGJI_MUSIC_ESTIMATED_SEC", "900")),
        # This image has its own YuE2 runner. Letting create_job install the
        # standard video overlay replaces its ENTRYPOINT with /app/entrypoint.sh
        # and sends the music payload to the video API, which rejects its mode.
        start_script={"command": ["python3"], "args": ["/app/worker.py"]},
        workload="video",
    )


def voice_clone_configured() -> bool:
    return bool(
        os.getenv("GONGJI_VOICE_CLONE_IMAGE", "").strip()
        and os.getenv("GONGJI_VOICE_CLONE_STORAGE_ID", "").strip()
    )


async def create_voice_clone_job(
    job_id: str,
    merchant_id: str,
    worker_input: dict[str, Any],
) -> int:
    """Submit the isolated IndexTTS-2 worker without touching H3 video images."""
    if not voice_clone_configured():
        raise GongjiError("声音克隆 Worker 镜像或模型盘尚未配置")
    profile = _profile("720p")
    profile.update({
        "image": os.environ["GONGJI_VOICE_CLONE_IMAGE"].strip(),
        "storage_id": int(os.environ["GONGJI_VOICE_CLONE_STORAGE_ID"]),
        "region": os.getenv("GONGJI_VOICE_CLONE_REGION_NAME", "浙江四区"),
        "gpu": os.getenv("GONGJI_VOICE_CLONE_GPU_NAME", "4090"),
        "gpu_count": 1,
    })
    profile, mark = await _select_available_profile(
        profile, "720p", "video", backup_prefix="GONGJI_VOICE_CLONE_BACKUP",
    )
    return await create_job(
        job_id, merchant_id, worker_input, "720p", "local",
        service_image=profile["image"], task_prefix="voice-clone",
        timeout_sec=int(os.getenv("GONGJI_VOICE_CLONE_TIMEOUT_SEC", "3600")),
        estimated_sec=int(os.getenv("GONGJI_VOICE_CLONE_ESTIMATED_SEC", "300")),
        start_script={"command": ["/opt/index-tts/.venv/bin/python"],
                      "args": ["/app/worker.py"]},
        selected_profile=profile, selected_mark=mark,
    )


def long_configured() -> bool:
    return bool(os.getenv("GONGJI_LONG_IMAGE", "").strip() and configured("720p"))


async def create_long_job(
    job_id: str,
    merchant_id: str,
    worker_input: dict[str, Any],
    transfer_backend: str = "cos",
) -> int:
    """Submit the isolated latent-linked long-video worker."""
    is_digital_human = worker_input.get("mode") == "digital_human_workflow"
    if is_digital_human:
        # Never fall back to the long-video image here. That fallback is what
        # sent digital-human jobs to the r38 worker, where 16:9 asked for a
        # model file the current Comfy build does not list and 9:16 was
        # rejected outright -- both surfacing only as "GPU 任务失败: Error".
        # A missing image must fail loudly at submission instead.
        image = os.getenv("GONGJI_DIGITAL_HUMAN_IMAGE", "").strip()
        if not image:
            raise GongjiError("数字人共绩镜像尚未配置（GONGJI_DIGITAL_HUMAN_IMAGE）")
        profile = _profile("720p")
        profile["image"] = image
        profile, mark = await _select_available_profile(
            profile, "720p", "video",
            backup_prefix="GONGJI_DIGITAL_HUMAN_BACKUP",
        )
        image = profile["image"]
    else:
        image = os.getenv("GONGJI_LONG_IMAGE", "").strip()
        if not image:
            raise GongjiError("长视频共绩镜像尚未配置")
    timeout = int(os.getenv("GONGJI_LONG_TIMEOUT_SEC", "7200"))
    estimated = int(os.getenv("GONGJI_LONG_ESTIMATED_SEC", "1800"))
    # Digital-human code and its pinned workflow are baked into its dedicated
    # worker image. Keep the runtime overlay path only for ordinary long video.
    overlay_key = "" if is_digital_human else os.getenv("GONGJI_LONG_OVERLAY_KEY", "").strip()
    if not is_digital_human and not overlay_key:
        raise GongjiError("长视频共绩运行包尚未配置")
    start_script = None
    extra_env = [{
        "name": "H3_LONG_MAX_DURATION_SECONDS",
        "value": os.getenv("H3_LONG_MAX_DURATION_SECONDS", "45"),
    }]
    if overlay_key:
        start_script = {
            "command": ["/bin/bash"],
            "args": [
                "-c",
                (
                    "set -euo pipefail; "
                    "curl --fail --location --retry 4 --retry-all-errors "
                    "--connect-timeout 20 --max-time 180 "
                    '"$H3_LONG_OVERLAY_URL" -o /tmp/h3-long-worker.tar.gz; '
                    "tar -xzf /tmp/h3-long-worker.tar.gz -C /; "
                    "rm -f /tmp/h3-long-worker.tar.gz; "
                    "exec /app/entrypoint.sh"
                ),
            ],
        }
        extra_env.append({
            "name": "H3_LONG_OVERLAY_URL",
            "value": _runtime_bundle_url(
                job_id, overlay_key, timeout + 1800,
                env_name="H3_LOCAL_RUNTIME_OVERLAY_PATH",
                backend_name=transfer_backend,
            ),
        })
    return await create_job(
        job_id,
        merchant_id,
        worker_input,
        "720p",
        transfer_backend,
        service_image=image,
        task_prefix="h3-long",
        timeout_sec=timeout,
        estimated_sec=estimated,
        start_script=start_script,
        extra_env=extra_env,
        selected_profile=profile if is_digital_human else None,
        selected_mark=mark if is_digital_human else None,
    )


async def create_remix_analysis_job(
    job_id: str,
    merchant_id: str,
    analysis_input: dict[str, Any],
    transfer_backend: str = "cos",
) -> int:
    """Run Qwen-VL as a separate evidence stage before the H3 image-only job."""
    image = os.getenv("GONGJI_QWEN_IMAGE", "").strip()
    if not image:
        raise GongjiError("视频复刻 Qwen-VL 镜像尚未配置")
    import remix_hypit
    if not remix_hypit.enabled():
        return await create_job(
            job_id, merchant_id, analysis_input, "720p", transfer_backend,
            service_image=image, task_prefix="qwen-remix", workload="remix",
        )
    # Same Qwen image; the overlay adds Linux Node + hypit + the hypit analyzer (the pattern the
    # long-video worker already uses), so no image rebuild is needed to change the analyzer.
    overlay_key = os.getenv("GONGJI_HYPIT_ANALYZER_OVERLAY_KEY", "").strip()
    if not overlay_key:
        raise GongjiError("hypit 分析运行包尚未配置")
    timeout = int(os.getenv("GONGJI_HYPIT_ANALYZER_TIMEOUT_SEC", "1800"))
    return await create_job(
        job_id, merchant_id, analysis_input, "720p", transfer_backend,
        service_image=image, task_prefix="hypit-remix", workload="remix",
        timeout_sec=timeout,
        estimated_sec=int(os.getenv("GONGJI_HYPIT_ANALYZER_ESTIMATED_SEC", "300")),
        start_script={
            "command": ["/bin/bash"],
            "args": [
                "-c",
                (
                    "set -euo pipefail; "
                    "curl --fail --location --retry 4 --retry-all-errors "
                    "--connect-timeout 20 --max-time 600 "
                    '"$RECAST_OVERLAY_URL" -o /tmp/recast.tgz; '
                    "tar -xzf /tmp/recast.tgz -C /; rm -f /tmp/recast.tgz; "
                    "cd /app; exec python /opt/recast/analyzer/hypit_remix_analyzer.py"
                ),
            ],
        },
        extra_env=[{
            "name": "RECAST_OVERLAY_URL",
            "value": _runtime_bundle_url(
                job_id, overlay_key, timeout + 1800,
                "H3_LOCAL_HYPIT_ANALYZER_OVERLAY_PATH",
                transfer_backend,
            ),
        }],
    )


async def job_status(task_id: int | str) -> dict[str, Any]:
    """返回 {status, running_seconds, billing, reason}。

    status 取值 queued / running / completed / failed，与任务中心保持一致。
    """
    async with httpx.AsyncClient(timeout=60) as client:
        data = _unwrap(await client.get(
            f"{BASE}/api/deployment/task/points",
            headers=_headers(), params={"task_id": task_id},
        ))

    results = (data or {}).get("results") or []
    if not results:
        return {"status": "queued", "running_seconds": 0, "billing": 0, "reason": "scheduling"}

    node = results[0]
    container = (node.get("containers") or [{}])[0]
    reason = container.get("reason")
    node_status = node.get("status")
    job_state = None
    job_detail = {}
    job_status_detail = {}

    # Some nodes finish with reason=null even when the Kubernetes Job failed.
    # Resolve that ambiguous terminal state from the task-level status so a
    # crashed worker can never be reported to merchants as completed.
    success_reasons = {"Completed", "Succeeded"}
    failed_reasons = {"Error", "OOMKilled", "DeadlineExceeded", "Evicted", "Failed"}
    terminal_nodes = {"End", "Succeeded", "Failed", "Error"}
    contradictory_terminal = node_status in {"Failed", "Error"} and reason in success_reasons
    # End is not definitive by itself: the container may say Completed while
    # the provider's Job has not yet finalized, or vice versa. Always consult
    # the Job summary for an End node so succeeded/failed pod counts decide.
    if node_status in terminal_nodes | {"Running"} and (
        node_status in {"Running", "End"} or reason is None or contradictory_terminal
    ):
        async with httpx.AsyncClient(timeout=60) as client:
            job_detail = _unwrap(await client.get(
                f"{BASE}/api/task/job/detail",
                headers=_headers(), params={"task_id": task_id},
            )) or {}
        job_status_detail = job_detail.get("job_status") or {}
        job_state = job_status_detail.get("status")

    if job_state in {"Complete", "Completed", "Succeeded"}:
        # The provider's top-level "End" plus a null container reason is
        # ambiguous. Trust a successful Job state, but reject the contradictory
        # case where the provider reports failed pods and no succeeded pod.
        failed_pods = int(job_status_detail.get("failed_pods") or 0)
        succeeded_pods = int(job_status_detail.get("succeeded_pods") or 0)
        status = "failed" if failed_pods and not succeeded_pods else "completed"
    elif job_state in {"Failed", "Error", "Manual", "Cancelled", "Terminated"}:
        status = "failed"
    elif node_status == "Succeeded" and reason in success_reasons:
        status = "completed"
    elif node_status in {"End", "Succeeded"}:
        failed_pods = int(job_status_detail.get("failed_pods") or 0)
        succeeded_pods = int(job_status_detail.get("succeeded_pods") or 0)
        # Manual/unknown terminal Job states are not proof of success. In
        # particular, the provider may return status=Manual with failed_pods=1,
        # succeeded_pods=0 after its pod ends without a container reason.
        status = (
            "failed"
            if reason in failed_reasons or failed_pods or not succeeded_pods
            else "completed"
        )
    elif node_status == "Running":
        status = "running"
    elif node_status in {"Failed", "Error"}:
        status = "failed"
    else:
        status = "queued"

    return {
        "status": status,
        "running_seconds": node.get("runing_time") or 0,
        # API 返回的是百万分之一元；例如 206250 = ¥0.20625。
        "billing": float(node.get("billing_value") or 0) / 1_000_000,
        "reason": reason or (job_state if status == "failed" else None),
        "node_name": node.get("name"),
        "point_id": node.get("point_id"),
    }


async def stop_job(task_id: int | str) -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        _unwrap(await client.post(
            f"{BASE}/api/task/job/stop", headers=_headers(), json={"task_id": int(task_id)},
        ))
