"""Keep the pinned Digital Human H3 template on the Tencent API side.

The template is a complete Ref2VA rewrite (six sections). A user's optional
direction is folded into it by DeepSeek so the result stays one coherent
prompt instead of a paragraph bolted onto the end. The merge may never weaken
the two guarantees the template exists to enforce -- the person must stay the
person in <Picture 1>, and the speech must stay the supplied <Audio 1>
verbatim -- so those rules are restated to the model and re-checked on its
output. Anything unusable falls back to the literal append.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


_ROOT = Path(__file__).resolve().parent
_PACKAGED_WORKFLOW = _ROOT / "digital_human_image_audio_v1.json"
WORKFLOW_PATH = (
    _PACKAGED_WORKFLOW
    if _PACKAGED_WORKFLOW.is_file()
    else _ROOT / "worker_long" / "workflows" / "digital_human_image_audio_v1.json"
)


def base_prompt() -> str:
    """The pinned Ref2VA template, read from the workflow graph itself."""
    with WORKFLOW_PATH.open(encoding="utf-8") as source:
        graph = json.load(source)
    prompts = [graph[node]["inputs"]["prompt"] for node in ("136", "276")]
    if prompts[0] != prompts[1]:
        raise ValueError("数字人工作流的基础提示词不一致")
    return prompts[0]


def compose_digital_human_workflow_prompt(direction: str = "") -> str:
    """Template plus the user's literal direction.

    This is what a task carries at creation time. When a direction is present the
    scheduler replaces it with the DeepSeek merge before dispatch; this stays the
    fallback, so the task is submittable even if the prompt service is down.
    """
    base = base_prompt()
    direction = direction.strip()
    return append_direction(base, direction) if direction else base


SECTIONS = (
    "subject_definitions:", "summary:", "retention_analysis:",
    "detailed_description:", "overall_soundscape:", "non_diegetic_music:",
)
LABELS = ("<Subject 1>", "<Picture 1>", "<Audio 1>")

_MERGE_RULES = """You rewrite MiniMax H3 full-reference (Ref2VA) prompts for a talking-head
digital human. You are given a pinned base prompt and one line of direction from the
end user. Fold the direction into the base prompt and return the merged prompt only.

Output format:
- Exactly these six sections, in this order, each on its own line followed by its body:
  subject_definitions: / summary: / retention_analysis: / detailed_description: /
  overall_soundscape: / non_diegetic_music:
- No preamble, no code fences, no commentary.
- Write in English. Keep any dialogue, lyrics or on-screen text in its original language.

Hard rules you may never weaken, whatever the direction says:
- Keep the labels <Subject 1>, <Picture 1> and <Audio 1> with their existing meaning.
- <Subject 1> keeps the identity, face, hair, clothing, proportions, background, objects,
  colors, lighting and composition of <Picture 1>. Never allow a different person, a
  changed outfit, a replaced background or a new location.
- <Audio 1> is reused exactly: same waveform, timing, language and voice, with accurate
  mouth synchronization. Never add, remove, translate or replace words.
- overall_soundscape uses the supplied <Audio 1> as the only sound and adds no effects or
  ambience. non_diegetic_music stays N/A. The delivered video re-muxes the original audio,
  so any music or sound effect you promise would not exist.
- Keep one continuous shot that continues from the preceding latent tail. No cuts.

What the direction may change: facial expression, gaze, blinking and breathing, restrained
head and shoulder movement, mood and energy, and the degree of camera stillness. Fold these
into detailed_description, and into summary when they change the intent of the shot. If part
of the direction conflicts with a hard rule, apply the rest and silently drop that part."""


def _usable(merged: str, base: str) -> bool:
    """Reject anything that lost the section skeleton or the reference labels."""
    if not merged or len(merged) > 40000:
        return False
    position = -1
    for section in SECTIONS:
        found = merged.find(section)
        if found <= position:          # missing, or out of order
            return False
        position = found
    return all(label in merged for label in LABELS)


def append_direction(base_prompt: str, direction: str) -> str:
    """The literal fallback: the user's words, verbatim, ahead of the audio sections."""
    marker = "\n\noverall_soundscape:"
    if base_prompt.count(marker) != 1:
        raise ValueError("数字人工作流提示词缺少 H3 音频段落")
    return base_prompt.replace(
        marker,
        f"\n\nAdditional user direction (verbatim):\n{direction}\n\noverall_soundscape:",
        1,
    )


async def merge_direction(base_prompt: str, direction: str, *,
                          merchant_id: str | None = None) -> dict:
    """Fold the direction into the template with DeepSeek.

    Never raises for a provider problem: a failed or malformed merge falls back
    to append_direction, so a prompt-service outage costs polish, not the task.
    """
    import prompt_service

    direction = direction.strip()
    if not direction:
        return {"prompt": base_prompt, "provider_used": False, "reason": "no_direction"}
    fallback = {"prompt": append_direction(base_prompt, direction), "provider_used": False}
    try:
        settings = (prompt_service._runtime_settings(merchant_id) if merchant_id
                    else prompt_service._runtime_settings())
    except prompt_service.PromptServiceError as exc:
        return {**fallback, "reason": f"unconfigured: {exc}"}
    try:
        merged = await prompt_service._call_provider(
            settings, "DigitalHumanRef2VA",
            f"Base prompt:\n{base_prompt}\n\nUser direction:\n{direction}",
            system_prompt=_MERGE_RULES, max_tokens=4096,
        )
    except Exception as exc:  # noqa: BLE001 - polish must never fail a paid task
        return {**fallback, "reason": f"provider error: {exc}"[:200]}
    merged = re.sub(r"^```[a-zA-Z]*\n|```$", "", merged.strip()).strip()
    cut = merged.find(SECTIONS[0])
    if cut > 0:
        merged = merged[cut:]
    if not _usable(merged, base_prompt):
        return {**fallback, "reason": "merged prompt failed structure check"}
    return {"prompt": merged, "provider_used": True, "reason": "merged"}
