"""Keep the pinned Digital Human H3 template on the Tencent API side."""

from __future__ import annotations

import json
from pathlib import Path


_ROOT = Path(__file__).resolve().parent
_PACKAGED_WORKFLOW = _ROOT / "digital_human_image_audio_v1.json"
WORKFLOW_PATH = (
    _PACKAGED_WORKFLOW
    if _PACKAGED_WORKFLOW.is_file()
    else _ROOT / "worker_long" / "workflows" / "digital_human_image_audio_v1.json"
)


def compose_digital_human_workflow_prompt(direction: str = "") -> str:
    """Return the exact workflow prompt, optionally followed by raw user direction.

    No LLM or prompt rewriting is used. The user's literal text is appended
    inside the H3 ``detailed_description`` section, preserving the template's
    existing Ref2VA section structure.
    """
    with WORKFLOW_PATH.open(encoding="utf-8") as source:
        graph = json.load(source)
    prompts = [graph[node]["inputs"]["prompt"] for node in ("136", "276")]
    if prompts[0] != prompts[1]:
        raise ValueError("数字人工作流的基础提示词不一致")
    base_prompt = prompts[0]
    extra = direction.strip()
    if not extra:
        return base_prompt
    marker = "\n\noverall_soundscape:"
    if base_prompt.count(marker) != 1:
        raise ValueError("数字人工作流提示词缺少 H3 音频段落")
    return base_prompt.replace(
        marker,
        f"\n\nAdditional user direction (verbatim):\n{extra}\n\noverall_soundscape:",
        1,
    )
