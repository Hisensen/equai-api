"""Model calls used by the reenactment stages.

vision(): Qwen-VL through DashScope's OpenAI-compatible endpoint (reads video and images).
director(): the configured prompt service (DeepSeek) for text-only creative decisions.
Both return parsed JSON and record token cost on a shared Ledger.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field

import httpx


class ModelError(RuntimeError):
    pass


@dataclass
class Ledger:
    calls: list[dict] = field(default_factory=list)

    def add(self, kind: str, seconds: float, tokens_in: int, tokens_out: int, yuan: float) -> None:
        self.calls.append({"kind": kind, "seconds": round(seconds, 2), "in": tokens_in,
                           "out": tokens_out, "yuan": round(yuan, 6)})

    @property
    def yuan(self) -> float:
        return round(sum(c["yuan"] for c in self.calls), 6)


def parse_json(text: str) -> dict | list:
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0]
    starts = [i for i in (value.find("{"), value.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON")
    first = min(starts)
    last = max(value.rfind("}"), value.rfind("]"))
    value = value[first:last + 1]
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", value))


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


async def _post(url: str, headers: dict, body: dict, timeout: float) -> dict:
    last = ""
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=20)) as client:
                response = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            last = type(exc).__name__
            await asyncio.sleep(2 ** attempt)
            continue
        if response.status_code == 400 and "data_inspection_failed" in response.text:
            raise ModelError("素材未通过模型服务的内容安全审核")
        if response.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {response.status_code}"
            await asyncio.sleep(2 ** attempt + 1)
            continue
        if response.status_code >= 400:
            raise ModelError(f"模型服务返回 {response.status_code}: {response.text[:300]}")
        return response.json()
    raise ModelError(f"模型服务多次重试后仍失败: {last}")


async def vision(content: list[dict], ledger: Ledger, *, kind: str, max_tokens: int = 6000,
                 model: str | None = None) -> dict | list:
    """One Qwen-VL call returning JSON; retries once when the answer is not valid JSON."""
    key = _env("DASHSCOPE_API_KEY", "")
    if not key:
        raise ModelError("视频理解接口尚未配置")
    base = _env("H3_VISION_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    name = model or _env("H3_REENACT_VISION_MODEL", _env("H3_VISION_MODEL", "qwen3-vl-plus"))
    price_in = float(_env("H3_VISION_PRICE_IN_PER_M", "1.0"))
    price_out = float(_env("H3_VISION_PRICE_OUT_PER_M", "10.0"))
    messages = [{"role": "user", "content": content}]
    for attempt in range(2):
        started = time.monotonic()
        data = await _post(f"{base}/chat/completions", {"Authorization": f"Bearer {key}"}, {
            "model": name, "messages": messages, "max_tokens": max_tokens,
            "temperature": 0.1, "enable_thinking": False,
        }, float(_env("H3_VISION_TIMEOUT_SEC", "240")))
        usage = data.get("usage") or {}
        tin, tout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        ledger.add(kind, time.monotonic() - started, tin, tout, (tin * price_in + tout * price_out) / 1e6)
        text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        try:
            return parse_json(text)
        except (ValueError, json.JSONDecodeError):
            if attempt:
                raise ModelError(f"{kind}: 视觉模型没有返回有效 JSON")
            messages = messages + [{"role": "assistant", "content": text[:4000]},
                                   {"role": "user", "content": "Your answer was not valid JSON. Return ONLY the JSON object."}]
    raise ModelError(f"{kind}: 视觉模型调用失败")


async def director(system: str, user: str, ledger: Ledger, *, kind: str, max_tokens: int = 8192,
                   temperature: float = 0.4) -> dict | list:
    """One DeepSeek call returning JSON, through the admin-configured prompt service."""
    import prompt_service

    settings = prompt_service._runtime_settings()
    if not settings.get("enabled"):
        raise ModelError("提示词服务（DeepSeek）未启用，无法编写新台词")
    body = prompt_service._chat_payload(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens, temperature=temperature,
    )
    body["response_format"] = {"type": "json_object"}
    url = prompt_service._endpoint(settings["base_url"], "chat/completions")
    price_in = float(_env("H3_DIRECTOR_PRICE_IN_PER_M", "2.0"))
    price_out = float(_env("H3_DIRECTOR_PRICE_OUT_PER_M", "8.0"))
    for attempt in range(2):
        started = time.monotonic()
        data = await _post(url, {"Authorization": f"Bearer {settings['api_key']}"}, body, 240)
        usage = data.get("usage") or {}
        tin, tout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        ledger.add(kind, time.monotonic() - started, tin, tout, (tin * price_in + tout * price_out) / 1e6)
        choice = (data.get("choices") or [{}])[0]
        text = str((choice.get("message") or {}).get("content") or "")
        try:
            value = parse_json(text)
            if isinstance(value, dict):
                return value
        except (ValueError, json.JSONDecodeError):
            pass
        if attempt:
            reason = choice.get("finish_reason")
            raise ModelError(f"{kind}: 导演模型没有返回有效 JSON（{reason}）")
        body["messages"] = body["messages"] + [
            {"role": "assistant", "content": text[:6000]},
            {"role": "user", "content": "That was not one complete JSON object. Return ONLY the complete JSON object."},
        ]
    raise ModelError(f"{kind}: 导演模型调用失败")
