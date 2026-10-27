"""Reference-video reading through Alibaba Model Studio instead of a Gongji analysis job.

H3_REMIX_ANALYSIS_ENGINE=hypit-api keeps the whole hypit remix path (360p source + role pictures to
H3 with a `[video editing]` prompt) and replaces only the analysis stage: one qwen3-vl-plus call
that reads the source video from its signed URL, plus ffmpeg scene cuts computed here as hints.

Measured 2026-09-22 on a 15.08 s clip with one target picture, called from the HK host:
  Gongji Qwen3-VL-4B job (hypit analyzer): median 148 s, 0.064 yuan, 12+ serial model calls
  qwen3-vl-plus, one call:                 23 s, 0.0137 yuan, shots aligned with the real cuts
The provider fetches the video itself, so the slow HK -> COS link carries only the request JSON.

The output matches the Gongji analyzer's `analysis` contract, so compile_prompt and the rest of
the pipeline are unchanged. If the API refuses or fails and the Gongji analyzer overlay is
configured, the scheduler falls back to it.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time

import httpx

import asr_service

ROLE_WORD = {"person": "presenter (person)", "product": "product", "scene": "scene / environment"}
TARGET_FOCUS = {
    "person": "face, apparent age, hair, build, and every visible clothing item with colours and materials",
    "product": "shape, proportions, material, colours, every part (lid, handle, straw, cap...) and any visible text copied exactly",
    "scene": "layout, surfaces, furniture, decor, light direction, colour palette and mood",
}
ORIGINAL_KEYS = ("person", "person_short", "product", "product_short", "scene", "scene_short")


class VisionError(RuntimeError):
    pass


def _setting(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def configured() -> bool:
    return bool(_setting("DASHSCOPE_API_KEY", ""))


def model() -> str:
    return _setting("H3_VISION_MODEL", "qwen3-vl-plus")


def cut_hints(video_url: str) -> list[float]:
    """Scene-change timestamps from ffmpeg. Hints only: a failure never blocks the analysis."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", video_url,
             "-vf", "select='gt(scene,0.12)',showinfo", "-f", "null", "-"],
            capture_output=True, text=True, timeout=90,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    hints: list[float] = []
    for value in sorted(float(v) for v in re.findall(r"pts_time:([\d.]+)", proc.stderr)):
        if not hints or value - hints[-1] >= 0.3:
            hints.append(round(value, 2))
    return hints[:12]


def _instruction(duration: float, roles: list[str], hints: list[float], transcript: str = "",
                 include_target_images: bool = True) -> str:
    targets = "\n".join(
        f'  "{role}": the TARGET {ROLE_WORD[role]} picture - {TARGET_FOCUS[role]}.' for role in roles
    )
    audio = (
        "The source audio was transcribed by qwen3-asr-flash-filetrans. Preserve its meaning and timing; "
        "put any audible dialogue only inside <d> tags in shot text. Do not invent speech.\n"
        f"VERBATIM AUDIO TRANSCRIPT: {transcript}\n"
        if transcript else
        "No reliable audio transcript is available. Do not invent dialogue; describe audible speech only when clearly heard.\n"
    )
    reference_context = (
        f"The user uploaded reference pictures for these explicitly selected roles: {', '.join(roles)}. "
        "Role labels come from the upload form; do not infer or describe the appearance of those pictures. "
        if not include_target_images else
        f"The source video is followed by {len(roles)} TARGET reference picture(s) in this order: {', '.join(roles)}. "
    )
    targets_output = (
        f'"targets": an object with exactly these keys:\n{targets}\n'
        "  Each value is ONE noun phrase of 30-80 words that starts directly with the subject, e.g. \"a man in his forties "
        "with ...\". Do not start with \"is\", \"The target\" or \"This\". Ignore any plain studio or white background.\n"
        if include_target_images else ""
    )
    return f"""You are reading a {duration:.2f}-second short video. {reference_context}Mechanical cut candidates from ffmpeg (seconds, reliable):
{hints or "none"}.

{audio}

Return ONE JSON object with these keys:
"summary": 2-4 sentences: what the video shows from opening to close and how its pacing works.
"style": 1-2 sentences on camera handling, framing habits, editing rhythm and colour grade ONLY. Do not mention any
  person, product, place, venue, lighting source or time of day in "style".
"person": full visual description of the main on-screen presenter, including every outfit they wear.
"person_short": 2-5 word noun phrase for that presenter, e.g. "the young woman".
"product": full visual description of the main product shown or used, and its packaging.
"product_short": 2-6 word noun phrase naming that product by its look, e.g. "the black skull-graphic tumbler".
"scene": full description of the environment, background and lighting.
"scene_short": 2-5 word noun phrase for that environment, e.g. "the night street market".
"shots": the real shots in order, each {{"start": seconds, "end": seconds, "purpose": short phrase, "text": "..."}}.
  First shot starts at 0, last ends at {duration:.2f}. Start a new shot at every real cut above and at every clear
  change of framing, action or location. Usually 4-9 shots.
  "text": 40-90 English words describing, in order: the framing and where the subject is in frame, what happens
  step by step, how the camera moves, and when the product appears or is handled. Use these exact tokens instead
  of naming things: PERSON = the main on-screen presenter; PRODUCT = the main product object itself and nothing
  else; PLACE = the setting. PRODUCT is never a liquid, food, contents or packaging ("pours tea into PRODUCT").
  Never name the product type in words (no "tumbler", "cup", "bottle"): always write PRODUCT.
  Never describe PERSON's face, hair, body or clothing. Never describe anything on PRODUCT: no colours, graphics,
  characters, prints, lights, glowing parts or branding. Never describe background decor, lighting or weather.
  Say "PERSON reappears in a different outfit" for a wardrobe change. Secondary people may be described plainly.
{targets_output}
Describe only what is visible. JSON only."""


def _timed_transcript(result: dict | None) -> list[dict]:
    """Normalize Qwen ASR sentence timestamps to seconds for the shot analyst."""
    if not isinstance(result, dict):
        return []
    output = result.get("output") or {}
    raw = output.get("transcripts") or output.get("results") or []
    if isinstance(raw, dict):
        raw = [raw]
    sentences = []
    for transcript in raw:
        if not isinstance(transcript, dict):
            continue
        nested = transcript.get("transcript_data")
        if isinstance(nested, dict):
            sentences.extend(_timed_transcript({"output": nested}))
        for sentence in transcript.get("sentences") or transcript.get("sentence_list") or []:
            if not isinstance(sentence, dict):
                continue
            start = float(sentence.get("begin_time") or sentence.get("start_time") or 0) / 1000
            end = float(sentence.get("end_time") or sentence.get("stop_time") or 0) / 1000
            text = str(sentence.get("text") or sentence.get("sentence") or "").strip()
            if text:
                sentences.append({"start": max(0, start), "end": max(start, end), "text": text})
    return sorted(sentences, key=lambda item: item["start"])


def _json_object(text: str) -> dict:
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0]
    first, last = value.find("{"), value.rfind("}")
    if first < 0 or last <= first:
        raise ValueError("no JSON object")
    value = value[first:last + 1]
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        result = json.loads(re.sub(r",\s*([}\]])", r"\1", value))
    if not isinstance(result, dict):
        raise ValueError("not an object")
    return result


GENERIC = [
    (r"\b(the |a )?(primary|main) (person|presenter|subject|woman|man)\b", "{PERSON}"),
    (r"\b(the |a )?main product\b", "{PRODUCT}"),
]


def placeholders(text: str) -> str:
    for token in ("PERSON", "PRODUCT", "PLACE"):
        text = re.sub(rf"\{{?\b{token}\b\}}?", "{" + token + "}", text)
    for pattern, token in GENERIC:
        text = re.sub(pattern, token, text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def target_phrase(text: str) -> str:
    """compile_prompt writes '<Subject N> is {phrase}', so the phrase must not bring its own verb."""
    value = re.sub(r"\s+", " ", str(text)).strip()
    value = re.sub(r"^(?:the |this )?(?:target\b[^.:]*?\b)?(?:picture |image )?(?:shows |is |depicts )",
                   "", value, flags=re.I)
    return value.rstrip(". ")


def normalise_shots(raw: object, duration: float) -> list[dict]:
    shots, last = [], -1.0
    items = raw if isinstance(raw, list) else []
    for item in sorted((i for i in items if isinstance(i, dict)), key=lambda s: float(s.get("start") or 0)):
        text = placeholders(str(item.get("text") or ""))
        if not text:
            continue
        start = max(0.0, min(float(item.get("start") or 0), duration - 0.5))
        if start - last < 0.5:
            continue
        shots.append({"start": round(start, 2), "purpose": str(item.get("purpose") or "")[:80], "text": text})
        last = start
    if not shots:
        raise ValueError("no usable shots")
    shots[0]["start"] = 0.0
    for i, shot in enumerate(shots):
        shot["end"] = shots[i + 1]["start"] if i + 1 < len(shots) else round(duration, 2)
    return shots[:10]


def _validated(value: dict, roles: list[str], duration: float,
               include_target_images: bool = True) -> dict:
    missing = [k for k in ("summary", "style", *ORIGINAL_KEYS, "shots") if k not in value]
    if include_target_images and "targets" not in value:
        missing.append("targets")
    if missing:
        raise ValueError(f"missing {missing}")
    targets = value.get("targets") if isinstance(value.get("targets"), dict) else {}
    described = {role: target_phrase(targets.get(role) or "") for role in roles} if include_target_images else {}
    empty = [role for role, text in described.items() if len(text) < 10]
    if empty:
        raise ValueError(f"missing target description for {empty}")
    return {
        "style": str(value["style"]),
        "summary": str(value["summary"]),
        "original": {k: str(value[k]).strip() for k in ORIGINAL_KEYS},
        "targets": {r: described.get(r, "") for r in ("person", "product", "scene")},
        "shots": normalise_shots(value["shots"], duration),
    }


SCRIPT_WASH_SYSTEM = """You are a precise video shot-script editor. You receive a source script and a role map built from
the user's explicit upload-purpose selections. Rewrite only the descriptions of roles that have uploaded reference
pictures. Preserve the original shot count, order, timing, actions, gestures, camera moves and pacing.

For an uploaded role, replace source appearance or environment descriptions with that role's exact placeholder:
{PERSON}, {PRODUCT}, or {PLACE}. Never keep source-specific appearance details for an uploaded role. The final
video model receives the actual images separately and binds these placeholders to the listed Picture numbers.
If visual descriptions of target pictures are supplied, use them only to understand which source role they replace;
do not copy them into the shot text. If they are not supplied, rely only on the selected role labels.

When PRODUCT has a reference, preserve the interaction intent but adapt hand actions to the described target
product; do not carry over parts or mechanisms that belong only to the source product. When PLACE has a reference,
replace all source-location and background descriptions with {PLACE}; keep camera movement and subject blocking.
For roles without an uploaded picture, preserve their source appearance and setting information. Never transfer
one role's attributes to another role. Do not use dialogue as visual evidence.

Every role placeholder present in an input shot must remain present in that same output shot. Do not add or remove
shots, invent actions, change shot order, or return explanations. Return one JSON object: {"shots": ["...", ...]}.
"""


class _Client:
    def __init__(self) -> None:
        self.base = _setting("H3_VISION_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        self.price_in = float(_setting("H3_VISION_PRICE_IN_PER_M", "1.0"))
        self.price_out = float(_setting("H3_VISION_PRICE_OUT_PER_M", "10.0"))
        self.timeout = float(_setting("H3_VISION_TIMEOUT_SEC", "180"))
        self.calls: list[dict] = []

    def cost(self) -> float:
        return round(sum(c["in"] * self.price_in + c["out"] * self.price_out for c in self.calls) / 1e6, 6)

    async def ask(self, content: list[dict]) -> str:
        body = {"model": model(), "messages": [{"role": "user", "content": content}],
                "max_tokens": 3000, "temperature": 0, "enable_thinking": False}
        headers = {"Authorization": f"Bearer {_setting('DASHSCOPE_API_KEY', '')}"}
        for attempt in range(3):
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(f"{self.base}/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise VisionError(f"视频理解接口不可用: {type(exc).__name__}") from exc
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code == 400 and "data_inspection_failed" in response.text:
                raise VisionError("参考素材未通过视频理解服务的内容安全审核")
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code != 200:
                raise VisionError(f"视频理解接口返回 {response.status_code}: {response.text[:200]}")
            data = response.json()
            usage = data.get("usage") or {}
            self.calls.append({"seconds": round(time.monotonic() - started, 2),
                               "in": int(usage.get("prompt_tokens") or 0),
                               "out": int(usage.get("completion_tokens") or 0)})
            return str(data["choices"][0]["message"]["content"] or "")
        raise VisionError("视频理解接口多次重试后仍失败")


BACKGROUND = re.compile(r"\b(background|backdrop|behind (?:him|her|them|{PERSON}))\b", re.I)


def drop_background(text: str) -> str:
    """Sentences about the background never carry action; for a scene swap they drag the old set back."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [s for s in sentences if not BACKGROUND.search(s)]
    return " ".join(kept) if kept else text


async def _wash_shots_with_deepseek(shots: list[dict], roles: list[str], analysis: dict,
                                    role_only_references: bool = False) -> tuple[float, dict]:
    """Rewrite source shots against the uploaded role map using the prompt service.

    The text model receives only the user-selected role mapping and source script in the commerce app;
    target pictures are neither sent to nor described for it. Other existing remix modes can continue
    to provide target descriptions from their vision pass.
    A failed wash must stop this task rather than silently send a source-conflicting script to H3.
    """
    from reenact import llm

    picture_no = {role: index for index, role in enumerate(roles, 1)}
    originals = analysis.get("original") or {}
    target_descriptions = analysis.get("targets") or {}
    role_map = []
    for role, token in (("person", "{PERSON}"), ("product", "{PRODUCT}"), ("scene", "{PLACE}")):
        item = {
            "role": role, "script_placeholder": token,
            "uploaded": role in picture_no,
            "picture_number": f"<Picture {picture_no[role]}>" if role in picture_no else None,
            "source_description": originals.get(role, ""),
        }
        if role in picture_no and not role_only_references:
            item["reference_visual_description"] = target_descriptions.get(role, "")
        role_map.append(item)
    user = (
        "Rewrite this source video's shot script using the uploaded-reference role map. "
        + ("The role map comes only from the user's explicit upload-purpose selections; no uploaded-picture appearance analysis is provided. "
           "Do not infer picture appearance. " if role_only_references else
           "Use any supplied reference visual descriptions only to identify the role being replaced. ")
        + "Only roles marked uploaded may be replaced. Use each selected role's placeholder so the final prompt can bind it to the exact Picture number. "
        "The listed Picture numbers are fixed and must never be exchanged.\n\n"
        "ROLE MAP:\n" + json.dumps(role_map, ensure_ascii=False)
        + "\n\nSOURCE SHOTS (one output item per input item, same order):\n"
        + json.dumps([shot["text"] for shot in shots], ensure_ascii=False)
    )
    ledger = llm.Ledger()
    started = time.monotonic()
    try:
        result = await llm.director(
            SCRIPT_WASH_SYSTEM, user, ledger, kind="commerce_script_wash",
            max_tokens=5000, temperature=0.0,
        )
    except Exception as exc:
        raise VisionError(f"分镜提示词整理失败，已停止提交生成：{exc}") from exc
    rewritten = result.get("shots") if isinstance(result, dict) else None
    if not isinstance(rewritten, list) or len(rewritten) != len(shots):
        raise VisionError("分镜提示词整理结果无效，已停止提交生成")
    for shot, text in zip(shots, rewritten):
        if not isinstance(text, str) or not text.strip():
            raise VisionError("分镜提示词整理结果缺少镜头内容，已停止提交生成")
        text = placeholders(text)
        required = set(re.findall(r"\{(?:PERSON|PRODUCT|PLACE)\}", shot["text"]))
        actual = set(re.findall(r"\{(?:PERSON|PRODUCT|PLACE)\}", text))
        if not required <= actual:
            raise VisionError("分镜提示词整理改动了人物、商品或场景占位符，已停止提交生成")
        shot["text"] = text
    return ledger.yuan, {"calls": ledger.calls, "seconds": round(time.monotonic() - started, 2)}


async def analyse(analysis_input: dict) -> tuple[dict, float, dict]:
    """Returns (analysis, cost_yuan, runtime) in the Gongji analyzer's `analysis` shape."""
    if not configured():
        raise VisionError("视频理解接口尚未配置")
    if analysis_input.get("require_asr") and not asr_service.configured():
        raise VisionError("语音转写服务暂时不可用")
    started = time.monotonic()
    duration = float(analysis_input["duration"])
    roles = [r for r in analysis_input.get("roles") or [] if r in ROLE_WORD]
    targets = {t["role"]: t["url"] for t in analysis_input.get("targets") or []}
    include_target_images = not bool(analysis_input.get("role_only_references"))
    # A generic edit may have no uploaded target pictures.  Do not block the
    # source-video analysis; the post-analysis planner decides whether to
    # extract a frame or generate a target with Image2.
    if include_target_images and any(r not in targets for r in roles):
        raise VisionError("复刻参考图数量与用途不一致")
    video_url = analysis_input["source"]["url"]
    asr_video_url = analysis_input.get("asr_source_url") or video_url
    hint_task = asyncio.to_thread(cut_hints, video_url)
    asr_task = asr_service.transcribe(asr_video_url) if asr_service.configured() else None
    if asr_task is None:
        hints, asr_result = await hint_task, None
    else:
        hints, asr_result = await asyncio.gather(hint_task, asr_task, return_exceptions=True)
        if isinstance(hints, Exception):
            hints = []
        if isinstance(asr_result, Exception):
            asr_error = str(asr_result)
            asr_result = None
        else:
            asr_error = ""
    hint_seconds = round(time.monotonic() - started, 2)
    transcript = asr_service.transcript_text(asr_result) if isinstance(asr_result, dict) else ""
    timed_transcript = _timed_transcript(asr_result)
    if timed_transcript:
        transcript += "\nTimed sentences (seconds):\n" + "\n".join(
            f"{item['start']:.2f}-{item['end']:.2f}: {item['text']}" for item in timed_transcript
        )

    content: list[dict] = [{"type": "video_url", "video_url": {"url": video_url}, "fps": 2}]
    if include_target_images:
        for role in roles:
            content += [{"type": "text", "text": f"TARGET {ROLE_WORD[role]} picture:"},
                        {"type": "image_url", "image_url": {"url": targets[role]}}]
    content.append({"type": "text", "text": _instruction(
        duration, roles, hints, transcript, include_target_images=include_target_images,
    )})

    client = _Client()
    result: dict | None = None
    for attempt in range(2):
        text = await client.ask(content)
        try:
            result = _validated(_json_object(text), roles, duration,
                                include_target_images=include_target_images)
            break
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            if attempt:
                raise VisionError(f"视频理解接口返回的镜头脚本不可用: {exc}") from None
            content = content[:-1] + [{"type": "text", "text": _instruction(
                                       duration, roles, hints, transcript,
                                       include_target_images=include_target_images)
                                       + "\n\nYour previous answer was not valid. Return ONLY one JSON object "
                                       "with every required key."}]
    assert result is not None
    wash_cost, wash_runtime = await _wash_shots_with_deepseek(
        result["shots"], roles, result, role_only_references=not include_target_images,
    )
    analysis = {
        "engine": f"hypit+{model()}-api",
        "remix2": bool(analysis_input.get("require_asr")),
        "duration_seconds": round(duration, 3),
        **result,
        "cut_hints": hints,
        "transcript": transcript,
        "transcript_timeline": timed_transcript,
    }
    runtime = {"model": model(), "hint_seconds": hint_seconds, "calls": client.calls,
               "deepseek_script_wash": wash_runtime,
               "total_seconds": round(time.monotonic() - started, 2),
               "asr": ({"model": asr_result.get("model"), "task_id": asr_result.get("task_id"),
                        "seconds": asr_result.get("seconds"), "transcript_chars": len(transcript)}
                       if isinstance(asr_result, dict) else {"configured": asr_service.configured(),
                                                              "error": asr_error if 'asr_error' in locals() else ""})}
    return analysis, round(client.cost() + wash_cost, 6), runtime
