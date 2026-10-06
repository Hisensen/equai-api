"""DashScope asynchronous file transcription for Hypit video analysis."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


class AsrError(RuntimeError):
    pass


def _setting(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def configured() -> bool:
    # Opt-in so existing Qwen-VL-only deployments and tests do not unexpectedly
    # incur an extra provider task. Production Hypit sets H3_ASR_ENABLED=1.
    return _setting("H3_ASR_ENABLED", "0").lower() in {"1", "true", "yes", "on"} and bool(
        _setting("DASHSCOPE_API_KEY", "")
    )


def model() -> str:
    return _setting("H3_ASR_MODEL", "qwen3-asr-flash-filetrans")


def base_url() -> str:
    return _setting("H3_ASR_BASE_URL", "https://dashscope.aliyuncs.com").rstrip("/")


def _asr_file_url(file_url: str, public_base: str | None = None) -> str:
    """Optionally bypass an edge proxy when the ASR vendor fetches local media."""
    # A caller can supply a scoped origin. Music uses this to bypass the edge
    # proxy without changing the URLs used by video transcription.
    if public_base is None:
        public_base = _setting("H3_ASR_FILE_URL_BASE", "")
    public_base = public_base.strip().rstrip("/")
    if not public_base:
        return file_url
    source = urlsplit(file_url)
    target = urlsplit(public_base)
    if (source.scheme != "https" or target.scheme != "https" or not target.netloc
            or target.path not in {"", "/"} or target.query or target.fragment
            or target.username or target.password):
        raise AsrError("语音转写文件地址配置无效")
    return urlunsplit((target.scheme, target.netloc, source.path, source.query, ""))


async def transcribe(
    file_url: str, *, file_url_base: str | None = None
) -> dict[str, Any]:
    """Submit and poll one public/signed media URL, returning normalized ASR data."""
    if not configured():
        raise AsrError("语音转写接口尚未配置")
    headers = {
        "Authorization": f"Bearer {_setting('DASHSCOPE_API_KEY', '')}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    payload = {
        "model": model(),
        # qwen3-asr-flash-filetrans takes one `file_url`; the plural `file_urls`
        # form belongs to Paraformer and is rejected ("A valid file URL is required").
        "input": {"file_url": _asr_file_url(file_url, file_url_base)},
        "parameters": {"channel_id": [0], "enable_words": True},
    }
    timeout = float(_setting("H3_ASR_TIMEOUT_SEC", "30"))
    poll_timeout = float(_setting("H3_ASR_POLL_TIMEOUT_SEC", "180"))
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(
                f"{base_url()}/api/v1/services/audio/asr/transcription",
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise AsrError(f"语音转写提交失败: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise AsrError(f"语音转写提交失败: HTTP {response.status_code}")
        body = response.json()
        task_id = str((body.get("output") or {}).get("task_id") or body.get("task_id") or "")
        if not task_id:
            raise AsrError("语音转写提交未返回 task_id")

        interval = float(_setting("H3_ASR_POLL_INTERVAL_SEC", "2"))
        while time.monotonic() - started < poll_timeout:
            await asyncio.sleep(interval)
            try:
                result = await client.get(
                    f"{base_url()}/api/v1/tasks/{task_id}",
                    headers={"Authorization": headers["Authorization"]},
                )
            except httpx.HTTPError as exc:
                raise AsrError(f"语音转写查询失败: {type(exc).__name__}") from exc
            if result.status_code >= 400:
                raise AsrError(f"语音转写查询失败: HTTP {result.status_code}")
            value = result.json()
            output = value.get("output") or {}
            status = str(output.get("task_status") or value.get("task_status") or "").upper()
            if status in {"PENDING", "RUNNING", "QUEUED", ""}:
                continue
            # SUCCESS_WITH_NO_VALID_FRAGMENT means the transcription ran and found
            # no speech -- a silent clip, music only, or ambience. That is an empty
            # transcript, not a failure: a product video with background music and
            # no voiceover is ordinary input, and treating it as an error used to
            # kill the whole task.
            if not status.startswith(("SUCCEED", "SUCCESS", "COMPLETED")):
                message = output.get("message") or value.get("message") or status
                raise AsrError(f"语音转写任务失败: {message}")
            # DashScope returns a short-lived transcription_url rather than
            # embedding the JSON transcript in the task response.
            results = output.get("results") or []
            if isinstance(output.get("result"), dict):
                results = [output["result"]]
                output["results"] = results
            for item in results if isinstance(results, list) else []:
                if not isinstance(item, dict) or not item.get("transcription_url"):
                    continue
                try:
                    transcript_response = await client.get(
                        str(item["transcription_url"]),
                        timeout=timeout,
                    )
                    if transcript_response.status_code < 400:
                        item["transcript_data"] = transcript_response.json()
                        output.setdefault("transcript_data", item["transcript_data"])
                except (httpx.HTTPError, ValueError):
                    # The task itself succeeded; callers can still use the
                    # provider's inline fields or continue without dialogue.
                    pass
            return {
                "task_id": task_id,
                "model": model(),
                "status": status,
                "output": output,
                "seconds": round(time.monotonic() - started, 2),
            }
    raise AsrError("语音转写任务轮询超时")


def transcript_text(result: dict[str, Any]) -> str:
    """Extract sentence-level text from the several DashScope result shapes."""
    output = result.get("output") or {}
    values: list[str] = []
    transcripts = output.get("transcripts") or output.get("results") or []
    if isinstance(transcripts, dict):
        transcripts = [transcripts]
    for item in transcripts:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("transcript_data"), dict):
            nested = transcript_text({"output": item["transcript_data"]})
            if nested:
                values.append(nested)
                continue
        sentences = item.get("sentences") or item.get("sentence_list") or []
        if isinstance(sentences, dict):
            sentences = [sentences]
        for sentence in sentences:
            if isinstance(sentence, dict):
                text = sentence.get("text") or sentence.get("sentence") or sentence.get("transcript")
            else:
                text = sentence
            if text and str(text).strip():
                values.append(str(text).strip())
        if not sentences and item.get("text"):
            values.append(str(item["text"]).strip())
    if not values and output.get("text"):
        values.append(str(output["text"]).strip())
    return " ".join(values)
