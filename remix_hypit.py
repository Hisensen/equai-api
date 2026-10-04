"""hypit-based video remix: switch, analysis-input shape and the deterministic H3 edit compiler.

H3_REMIX_ANALYSIS_ENGINE=legacy (default) keeps the Qwen blueprint + ImageTo keyframes +
image-only Ref2VA path unchanged. H3_REMIX_ANALYSIS_ENGINE=hypit switches the portal remix to:

  1. analysis job: same Qwen image, plus an overlay (Linux Node + hypit + analyzer) that reads the
     reference with hypit media (probe / boundaries / frames) and writes a shot table with
     {PERSON}/{PRODUCT}/{PLACE} placeholders, the source appearance of every role and descriptions
     of the uploaded target pictures;
  2. no keyframe stage;
  3. H3 receives the role pictures AND the (360p) source video, with a `[video editing]` prompt.

Why the prompt shape matters (verified 2026-09-22 on all seven person/product/scene combinations):
without `[video editing]` + `<Video 1>: partially_preserved ... removed and replaced`, H3 treats the
role pictures as extra things to add, so the original product or environment stays in frame.
"""
from __future__ import annotations

import os
import re

ENGINE_ENV = "H3_REMIX_ANALYSIS_ENGINE"


def enabled() -> bool:
    return os.getenv(ENGINE_ENV, "legacy").strip().lower() in {"hypit", "hypit-api"}


def analysis_via_api() -> bool:
    """`hypit-api` reads the reference through remix_vision instead of a Gongji analysis job."""
    return os.getenv(ENGINE_ENV, "legacy").strip().lower() == "hypit-api"


def is_hypit_analysis(analysis: object) -> bool:
    return isinstance(analysis, dict) and str(analysis.get("engine") or "").startswith("hypit")


def analysis_targets(input_objects: list[dict], roles: list[str], url_for) -> list[dict]:
    """Pair uploaded role pictures with their roles, in upload order (= <Picture N> order)."""
    images = [item for item in input_objects if item.get("field") == "images"]
    if len(images) != len(roles):
        raise ValueError("复刻参考图数量与用途不一致")
    return [
        {"role": role, "name": item["filename"], "size": int(item.get("size_bytes") or 0),
         "url": url_for(item)}
        for role, item in zip(roles, images)
    ]


# ── compiler (mirrors 视频复刻工程/service/compiler.py) ─────────────────────────────────

LABEL = {"person": "presenter", "product": "product", "scene": "environment"}
ORDER = ("person", "product", "scene")
STOP = {"the", "a", "an", "of", "with", "and", "in", "on", "young", "old", "main", "black", "white"}
ROLE_HINTS = {"person": {"woman", "man", "girl", "boy", "she", "he", "her", "his", "presenter", "host"},
              "product": set(), "scene": {"market", "street", "room", "studio", "outdoor", "indoor", "night"}}


def _join(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _stamp(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{seconds % 60:06.3f}"


def _sentence_case(text: str) -> str:
    text = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text.strip())
    return text if text.endswith((".", "!", "?")) else text + "."


def _clean_style(style: str, orig: dict, roles: list[str]) -> str:
    """Drop style sentences that describe a role being replaced (the vision model sometimes leaks them)."""
    banned: set[str] = set()
    for role in roles:
        banned |= ROLE_HINTS[role]
        banned |= {w for w in re.findall(r"[a-z]+", str(orig.get(f"{role}_short", "")).lower()) if w not in STOP}
    kept = [s for s in re.split(r"(?<=[.!?])\s+", style.strip())
            if s and not banned & set(re.findall(r"[a-z]+", s.lower()))]
    return " ".join(kept)


VENUE_WORDS = [
    (r"\b(bustling|busy|lively|crowded) (?=<Subject)", ""),
    (r"\b(?:a |the )?(<Subject \d+> )?(?:street |food |market )?vendor (?:stall|booth|kiosk|cart)s?\b",
     r"a counter"),
    (r"\b(?:a |the )?(?:street |food |market )?(?:stall|booth|kiosk)s?\b", "a counter"),
    (r"\b(?:a |the )?(?:street |food |market )?vendors?\b", "a second person"),
]


def _neutral_venue(text: str) -> str:
    """When the environment is replaced, venue nouns from the source pull H3 back to it."""
    for pattern, replacement in VENUE_WORDS:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return re.sub(r"\s{2,}", " ", text)


def _definite(phrase: str) -> str:
    phrase = str(phrase).strip()
    return phrase if re.match(r"(?i)(the|a|an|this|that|his|her|their)\b", phrase) else "the " + phrase


def _tidy(text: str) -> str:
    # "{PLACE}" often arrives as "a bustling {PLACE}"; a subject label takes no article, and a kept
    # role's "the ..." phrase must not double an article the model already wrote ("The the tumbler").
    text = re.sub(r"\b(?:a|an|the) (<Subject \d+>)", r"\1", text, flags=re.I)
    text = re.sub(r"\b(a|an|the) (the|a|an)\b", r"\2", text, flags=re.I)
    return re.sub(r"\s{2,}", " ", text)


def _drop_source_scene(text: str, orig: dict) -> str:
    """With a replaced environment, a sentence that still describes the source surroundings would
    contradict the new scene; drop it (same vocabulary as _clean_style)."""
    kept = _clean_style(text.replace("{PLACE}", "PLACEHOLDER"), orig, ["scene"]).replace("PLACEHOLDER", "{PLACE}")
    return kept or text


def compile_prompt(roles: list[str], analysis: dict) -> str:
    """Six-section H3 Ref2VA prompt for editing <Video 1> with the role pictures in upload order."""
    order = [r for r in roles if r in ORDER]
    if not order:
        raise ValueError("至少需要替换人物、商品、场景中的一项")
    subject = {r: f"<Subject {i}>" for i, r in enumerate(order, 1)}
    picture = {r: f"<Picture {i}>" for i, r in enumerate(order, 1)}
    orig, target = analysis["original"], analysis["targets"]
    fill = {
        "PERSON": subject.get("person", _definite(orig["person_short"])),
        "PRODUCT": subject.get("product", _definite(orig["product_short"])),
        "PLACE": subject.get("scene", _definite(orig["scene_short"])),
    }
    shots = analysis["shots"]

    definitions = [f"{subject[r]} is {str(target[r]).strip().rstrip('.')}, as shown in {picture[r]}." for r in order]
    definitions += ["<Video 1> is the source video for the target video edit.",
                    "<Audio 1> is the synchronized audio track of <Video 1> and is reused in the target video."]

    summary = ("[video editing + reference generation + audio reuse] The target video is an edited version "
               "of <Video 1>. The shot order, the camera framing and movement, the action progression, the "
               "timing of each cut and the pacing of <Video 1> are all kept, while the "
               f"{_join(LABEL[r] for r in order)} shown in <Video 1> "
               f"{'is' if len(order) == 1 else 'are'} replaced by {_join(subject[r] for r in order)}. "
               "<Audio 1> is reused unchanged.")

    shot_ids = ", ".join(f"[Shot {i}]" for i in range(1, len(shots) + 1))
    retention = [f"{subject[r]} (appears in {shot_ids}): fully_preserved - every feature defined in "
                 f"{picture[r]} is followed exactly and stays identical in every shot." for r in order]
    kept = [LABEL[r] for r in ORDER if r not in order]
    removals = []
    if "person" in order:
        removals.append(f"{orig['person_short']} is fully replaced by {subject['person']} in every shot, including "
                        "close-ups and shots where the presenter is only partly visible, so the original "
                        "presenter's face, hair, body and every outfit no longer appear anywhere and only one "
                        "presenter is ever visible")
    if "product" in order:
        removals.append(f"{orig['product_short']} and its packaging are fully removed from every frame and "
                        f"replaced by {subject['product']}, so no trace of the original product remains and only "
                        "one such product is ever visible")
    if "scene" in order:
        removals.append(f"{orig['scene_short']}, with all of its background elements and lighting, is fully "
                        f"removed and replaced by {subject['scene']}, so none of the original surroundings "
                        "remain anywhere in the target video")
    retention.append(
        "<Video 1> (source video being edited): partially_preserved - the order of shots, the camera framing "
        "and movement, the action progression, the timing of each cut and the pacing are all kept; "
        + (f"the {_join(kept)} of <Video 1> {'stays exactly as it is' if len(kept) == 1 else 'stay exactly as they are'}; "
           if kept else "")
        + "; ".join(removals) + ".")
    retention.append("<Audio 1>: fully_copy - <Audio 1> is reused 1:1 as the target video's complete final audio track.")

    new_scene = "scene" in order
    opening = _clean_style(str(analysis.get("style") or ""), orig, order)
    if new_scene:
        # A replaced environment needs anchoring in every shot: a single "takes place in" line lets
        # H3 drift back to the source surroundings on close-ups (observed 2026-09-22).
        opening = (opening + " " if opening else "") + (
            f"The whole video takes place in {subject['scene']}: {str(target['scene']).strip().rstrip('.')}. "
            "Every shot, including close-ups, hand-detail shots and walking shots, is set in "
            f"{subject['scene']}; no part of {_definite(orig['scene_short'])} appears anywhere.")
    else:
        opening = (opening + " " if opening else "") + (
            f"The video keeps the environment of <Video 1>: {str(orig['scene']).strip().rstrip('.')}.")
    body = [opening]
    for i, shot in enumerate(shots, 1):
        text = str(shot["text"])
        if new_scene:
            text = _drop_source_scene(text, orig)
        text = re.sub(r"\{(PERSON|PRODUCT|PLACE)\}", lambda m: fill[m.group(1)], text)
        if new_scene:
            text = _neutral_venue(text)
        text = _sentence_case(_tidy(text))
        lead = text if text.startswith("<") else text[0].lower() + text[1:]
        if new_scene:
            lead = f"in {subject['scene']}, {lead}"
        if i == 1:
            body.append(f"[Shot 1] {lead[0].upper() + lead[1:]}")
        else:
            body.append(f"[Shot {i}] At {_stamp(float(shot['start']))}, {lead}")

    return "\n\n".join([
        "subject_definitions:\n" + "\n".join(definitions),
        "summary:\n" + summary,
        "retention_analysis:\n" + "\n".join(retention),
        "detailed_description:\n" + "\n".join(body),
        "overall_soundscape:\nThe original synchronized audio of <Audio 1> is reused unchanged throughout, "
        "including all speech, ambience and sound effects.",
        "non_diegetic_music:\nN/A",
    ])
