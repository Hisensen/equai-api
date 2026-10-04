"""Prompt Kits: fixed, tested wording plus slots, so variants only change what they must.

Image Kits follow Hypit's Capture / Person / Shot / Setting division. The performance Kit
writes MiniMax H3's six-section Ref2VA format with a closed set of camera setups, the
street-interview-v1 "reference contract" idea generalised to any cast.
"""
from __future__ import annotations

import re

from .script import strip_markup

CAPTURE = {
    "phone": ("A photograph captured as a single frame from a video actually shot on an iPhone, with the texture of "
              "real iPhone footage. The image looks real, without an oily, overprocessed finish. The background is "
              "clearly visible, with no depth-of-field blur. Skin texture is natural and fine, and the lighting is "
              "natural. The image is coherent and free of visual artifacts."),
    "cinematic": ("A single frame from a high-end live-action film shot on a cinema camera, with photoreal skin, fur, "
                  "fabric and metal texture, motivated practical lighting, rich but natural colour and crisp focus on "
                  "the characters. The image is coherent and free of visual artifacts, with no text anywhere."),
}

NO_TEXT = ("No captions, subtitles, speech bubbles, labels, watermarks or added logos anywhere in the picture; "
           "signs and screens that belong to the place stay free of readable text.")


ILLUSTRATED = re.compile(r"漫画|漫剧|动漫|动画|二次元|插画|卡通|手绘|国漫|3D|三维|建模|像素|水彩|油画|"
                         r"anime|manga|manhua|comic|cartoon|illustrat|pixar|cel[- ]shad|3d render|painting", re.I)


def capture_text(capture: str, style: str) -> str:
    """The opening paragraph of every picture: the requested style leads; photographic language is used
    only when the look is photographic, so it never fights an illustrated or animated style."""
    if style and ILLUSTRATED.search(style):
        return (f"A single high-quality frame in this visual style: {style}. The same style is applied consistently to "
                "every character, object and the place; it is not photographic. Clean, coherent drawing or rendering, "
                "crisp character design, no visual artifacts, no text anywhere.")
    base = CAPTURE[capture]
    return f"{base} Visual style: {style}." if style else base


def capture_for(analysis: dict) -> str:
    style = " ".join(str(v) for v in (analysis.get("style") or {}).values()).lower() + " " + str(analysis.get("format", "")).lower()
    return "phone" if re.search(r"phone|iphone|selfie|vlog|ugc|handheld|street interview|tiktok", style) else "cinematic"


def setup_image_prompt(*, capture: str, setup: dict, cast: dict[str, dict], setting: str,
                       inputs: list[tuple[str, str]], framing_note: str = "", style: str = "") -> str:
    """inputs: ordered (kind, subject) for the Input images. The role of every Input image comes first, each
    stating what it provides and what it must not provide, then the picture's content."""
    refs = []
    for n, (kind, subject) in enumerate(inputs, 1):
        if kind == "main":
            refs.append(f"Input image {n} (identity and place): the established main camera setup of this production. It "
                        "provides every character's face, species features, hair, outfit and props, the products, the "
                        "place and the light. It does NOT provide the camera position or framing of this picture.")
        elif kind == "identity":
            refs.append(f"Input image {n} (identity of {subject}): provides only {subject}'s face, hair, build, outfit and "
                        "accessories. It does NOT provide the pose, the camera angle or the background.")
        elif kind == "keep":
            refs.append(f"Input image {n} (the frame to edit): keep its people (faces, hair, build, outfits), the place, "
                        "the light and the composition exactly; remove any captions, logos or overlaid text; change only "
                        f"what is asked{': ' + subject if subject else ''}.")
        elif kind == "product":
            refs.append(f"Input image {n} (the product {subject}): provides only the product's exact shape, colours, "
                        "materials and parts; wherever the product appears it is this object, held and used like the old one. "
                        "It does NOT provide people, pose or background.")
        elif kind == "layout":
            refs.append(f"Input image {n} (framing only): a blurred grey sketch of the reference shot. It provides only the "
                        "camera angle, shot size, crop and where each character stands. It does NOT provide faces, hair, "
                        "clothing, colours, props or the place, which all come from the text below.")
    count = len(setup["visible"])
    # The image service accepts at most 5000 characters: more characters get shorter descriptions.
    per_person = max(220, 2200 // max(count, 1))
    people = "\n".join(f"{cast[cid].get('label', cid)}: {clip(cast[cid].get('person', ''), per_person)}."
                       for cid in setup["visible"] if cid in cast)
    return "\n\n".join(filter(None, [
        capture_text(capture, style),
        "REFERENCE IMAGES:\n" + "\n".join(refs) if refs else "",
        "PEOPLE:\n" + people if people else "",
        "SHOT: " + clip(setup.get("shot", ""), 600) + ".",
        framing_note,
        "PLACE: " + clip(setting, 500) + ".",
        (f"Exactly {count} character{'s' if count != 1 else ''} from this cast appear{'s' if count == 1 else ''}, each "
         "once; no extra people. Every character is in a natural, ready-to-speak idle state. " if count else
         "A view of the place or of objects: no people and no hands unless the shot text asks for a hand. ") + NO_TEXT,
    ]))


def icon_prompt(subject: str) -> str:
    return (f"A single bold flat sticker-style icon of {subject}, centered on a plain pure white background, thick clean "
            "outline, vivid saturated colours, simple readable shape, no text, no letters, no border frame.")


def broll_prompt(prompt: str, capture: str, style: str = "") -> str:
    if style and ILLUSTRATED.search(style):
        return f"{prompt.strip()}\n\nVisual style: {style}. {NO_TEXT}"
    return f"{prompt.strip()}\n\n{'Photoreal, natural light.' if capture == 'phone' else 'Cinematic photoreal still.'} {NO_TEXT}"


def clip(text: str, limit: int) -> str:
    """Shorten to whole sentences so a model never receives half an instruction."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text.rstrip(".")
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "), cut.rfind("。"))
    return (cut[:end] if end > limit * 0.5 else cut[:cut.rfind(" ")] if " " in cut else cut).rstrip(" ,;.")


def _stamp(seconds: float) -> str:
    """H3's shot clock: MM:SS.mmm."""
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60):02d}:{seconds % 60:06.3f}"


def language_of(text: str) -> str:
    """The spoken language tag H3 expects inside <d>[Language] ...</d>."""
    if re.search(r"[\u3040-\u30ff]", text):
        return "Japanese"
    if re.search(r"[\uac00-\ud7af]", text):
        return "Korean"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "Chinese"
    if re.search(r"[ñ¿¡]|\b(que|el|la|los|es|mío|tuyo|qué|por|para|mira|pero|yo)\b", text, re.I):
        return "Spanish"
    if re.search(r"[àâçéèêëîïôûùüÿœ]|\b(le|la|les|est|et|je|vous|mais)\b", text, re.I):
        return "French"
    return "English"


def said(text: str) -> str:
    """Dialogue in H3's format: exact words, original language, clean end punctuation."""
    words = re.sub(r"\s+", " ", strip_markup(text, spoken=True)).strip()
    words = re.sub(r"[~～]+", "", words)
    if words and words[-1] not in ".?!。？！":
        words += "。" if re.search(r"[\u4e00-\u9fff]$", words) else "."
    return f"<d>[{language_of(words)}] {words}</d>"


def _local_speakers(segment: dict) -> dict[str, int]:
    """(Sx) follows the order of vocal events inside this generated clip."""
    ids: dict[str, int] = {}
    for line in segment.get("lines") or []:
        ids.setdefault(line["role"], len(ids) + 1)
    return ids


def _offscreen(line: dict) -> bool:
    return bool(line.get("offscreen")) or line.get("role") == "VO"


def _subjects(pictures: list[str], setups: dict[str, dict]) -> list[str]:
    ids: list[str] = []
    for sid in pictures:
        for cid in setups[sid]["visible"]:
            if cid not in ids:
                ids.append(cid)
    return ids


def _shot_list(shots: list[int] | None, _unused: dict | None = None) -> str:
    return ", ".join(f"[Shot {n}]" for n in shots) or "[Shot 1]"


def performance_prompt(*, segment: dict, cast: dict[str, dict], setups: dict[str, dict], pictures: list[str],
                       audio_for: dict[str, int], speaker_ids: dict[str, int], setting: str, fmt: str,
                       ratio: str, estimates: list[float], corrections: str = "",
                       products: list[dict] | None = None, style: str = "") -> str:
    """H3 Ref2VA prompt (reference generation) following MiniMax's full-reference format.

    Every shot is anchored to one camera-setup picture ("the shot's keyframe corresponds to <Picture N>"),
    subjects carry their appearance at first appearance, and dialogue is written as <d>[Language] ...</d>.
    pictures: setup ids in <Picture N> order; products follow as extra pictures; audio_for: cast id -> <Audio N>."""
    speaker_ids = _local_speakers(segment)
    in_segment = _subjects(pictures, setups)
    subject = {cid: f"<Subject {i}>" for i, cid in enumerate(in_segment, 1)}
    place = f"<Subject {len(in_segment) + 1}>"
    product_label = {}
    for k, product in enumerate(products or [], 1):
        product_label[k] = f"<Subject {len(in_segment) + 1 + k}>"
    picture = {sid: f"<Picture {i}>" for i, sid in enumerate(pictures, 1)}
    duration = segment["request_duration"]

    # Shots in playback order: consecutive lines in one setup form one shot.
    shots: list[tuple[str, list[int]]] = []
    for i, line in enumerate(segment["lines"]):
        if shots and shots[-1][0] == line["setup"]:
            shots[-1][1].append(i)
        else:
            shots.append((line["setup"], [i]))
    shots_of_setup: dict[str, list[int]] = {}
    shots_of_subject: dict[str, list[int]] = {}
    for n, (sid, _) in enumerate(shots, 1):
        shots_of_setup.setdefault(sid, []).append(n)
        for cid in setups[sid]["visible"]:
            shots_of_subject.setdefault(cid, []).append(n)
    cutaways = [sid for sid in pictures if not setups[sid]["visible"]]

    definitions = []
    for cid in in_segment:
        seen = ", ".join(picture[s] for s in pictures if cid in setups[s]["visible"])
        definitions.append(f"{subject[cid]} is {cast[cid].get('label', cid)} as shown in {seen}: "
                           f"{clip(cast[cid].get('person', ''), 700)}.")
    definitions.append(f"{place} is the environment as shown in {', '.join(picture[s] for s in pictures)}: {clip(setting, 400)}.")
    for k, product in enumerate(products or [], 1):
        definitions.append(f"{product_label[k]} is the product {product.get('label', '')} in <Picture {len(pictures) + k}>: "
                           f"{clip(product.get('description', ''), 300)}.")
    for sid in pictures:
        who = ", ".join(subject[c] for c in setups[sid]["visible"] if c in subject) or "no people"
        used_in = shots_of_setup.get(sid)
        role = f"the keyframe of {_shot_list(used_in, picture)}" if used_in else "the keyframe of any cutaway shot"
        definitions.append(f"{picture[sid]} is {role}, showing {who}: {clip(setups[sid].get('shot', ''), 500)}.")
    for cid, n in audio_for.items():
        if cid in subject:
            definitions.append(f"<Audio {n}> is the voice-timbre reference for {subject[cid]} (S{speaker_ids[cid]}).")

    task = "reference generation + audio reference" if audio_for else "reference generation"
    roster = ", ".join(subject[c] for c in in_segment)
    summary = (f"[{task}] The target video is a {duration}-second {ratio} {fmt or 'dialogue scene'} in {place}. "
               f"{roster or 'The characters'} speak the lines below; each shot is framed exactly as its keyframe picture "
               "and cuts go directly from one keyframe framing to the next. "
               f"Exactly {len(in_segment)} distinct character{'s' if len(in_segment) != 1 else ''} exist"
               f"{'s' if len(in_segment) == 1 else ''}; nobody is duplicated or added, and no other place appears.")

    retention = []
    for cid in in_segment:
        retention.append(f"{subject[cid]} (appears in {_shot_list(shots_of_subject.get(cid, []), picture)}): fully_preserved - "
                         "face, species features, hair, outfit, accessories and held props stay identical in every shot.")
    retention.append(f"{place} (appears in every shot): fully_preserved - the same place, palette and light throughout.")
    for k, product in enumerate(products or [], 1):
        retention.append(f"{product_label[k]}: fully_preserved - the product's exact shape, colours and parts wherever it appears.")
    for sid in pictures:
        used_in = shots_of_setup.get(sid)
        where = f"{_shot_list(used_in, picture)} keyframe" if used_in else "cutaway keyframe"
        retention.append(f"{picture[sid]} ({where}): fully_preserved - composition, camera angle, crop, character "
                         "placement, place and light; the camera stays in this framing for the whole shot.")
    for cid, n in audio_for.items():
        if cid in subject:
            retention.append(f"<Audio {n}>: reference - {subject[cid]} follows the voice timbre of <Audio {n}> "
                             "without copying the original signal.")

    opening = (f"The target video is {clip(style, 200) if style else 'realistic, naturally lit footage'}, "
               f"cut as a {fmt or 'dialogue scene'} with hard cuts between fixed camera framings.")
    body, clock, introduced = [opening], 0.25, set()
    for n, (sid, idxs) in enumerate(shots, 1):
        anchor = (f"[Shot {n}] The shot's keyframe corresponds to {picture[sid]}." if n == 1 else
                  f"[Shot {n}] At {_stamp(clock)}, the shot cuts to the keyframe of {picture[sid]}.")
        parts = [anchor, clip(setups[sid].get("shot", ""), 260) + "."]
        for cid in setups[sid]["visible"]:
            if cid in subject and cid not in introduced:
                parts.append(f"{subject[cid]}, {clip(cast[cid].get('person', ''), 180)}, is in frame.")
                introduced.add(cid)
        for i in idxs:
            line = segment["lines"][i]
            timbre = (f" using the voice timbre referenced from <Audio {audio_for[line['role']]}>"
                      if line["role"] in audio_for else "")
            action = clip(line.get("action", ""), 260)
            if _offscreen(line):
                parts.append(f"{action + '. ' if action else ''}An off-screen voice (S{speaker_ids.get(line['role'], 1)}) "
                             f"says{timbre}, {said(line['text'])} Nobody on screen moves their mouth for it.")
                continue
            who = subject.get(line["role"], cast.get(line["role"], {}).get("label", line["role"]))
            parts.append(f"{action + '. ' if action else ''}{who} (S{speaker_ids.get(line['role'], 1)}) says{timbre}, "
                         f"{said(line['text'])} Only {who} moves their mouth; the others listen.")
        body.append(" ".join(parts))
        clock += sum(estimates[i] for i in idxs)
    if cutaways:
        body.append(f"Brief cutaways between lines show only the keyframe framing of "
                    f"{', '.join(picture[s] for s in cutaways)}; the voice continues over them.")
    body.append(f"The last line ends with a short natural reaction before {_stamp(float(duration))}. "
                "The picture carries no subtitles, captions, text overlays, logos or watermarks.")
    if corrections:
        body.append("Corrections from the previous attempt: " + corrections)

    return "\n\n".join([
        "subject_definitions:\n" + "\n".join(definitions),
        "summary:\n" + summary,
        "retention_analysis:\n" + "\n".join(retention),
        "detailed_description:\n" + "\n".join(body),
        f"overall_soundscape:\nClean, close dialogue with the natural room tone of {place} throughout.",
        "non_diegetic_music:\nN/A",
    ])


def motion_performance_prompt(*, segment: dict, cast: dict[str, dict], setups: dict[str, dict], pictures: list[str],
                              speaker_ids: dict[str, int], setting: str, ratio: str, duration: int,
                              reuse_audio: bool, corrections: str = "", framing_note: str = "",
                              products: list[dict] | None = None, style: str = "",
                              lyrics: list[dict] | None = None) -> str:
    """H3 Ref2VA prompt (reference generation + audio reuse): the new cast and place come from the setup
    pictures, movement, framing and cut timing from the reference passage (<Video 1>), and the passage's
    original soundtrack (<Audio 1>) is reused as the complete audio."""
    speaker_ids = _local_speakers(segment)
    in_segment = _subjects(pictures, setups)
    subject = {cid: f"<Subject {i}>" for i, cid in enumerate(in_segment, 1)}
    place = f"<Subject {len(in_segment) + 1}>"
    product_label = {k: f"<Subject {len(in_segment) + 1 + k}>" for k, _ in enumerate(products or [], 1)}
    picture = {sid: f"<Picture {i}>" for i, sid in enumerate(pictures, 1)}

    definitions = []
    for cid in in_segment:
        seen = ", ".join(picture[s] for s in pictures if cid in setups[s]["visible"])
        definitions.append(f"{subject[cid]} is {cast[cid].get('label', cid)}, whose appearance comes from {seen} and whose "
                           f"body movement and timing come from <Video 1>: {clip(cast[cid].get('person', ''), 650)}.")
    definitions.append(f"{place} is the environment as shown in {', '.join(picture[s] for s in pictures)}: {clip(setting, 400)}.")
    for k, product in enumerate(products or [], 1):
        definitions.append(f"{product_label[k]} is the product {product.get('label', '')} in <Picture {len(pictures) + k}>: "
                           f"{clip(product.get('description', ''), 300)}.")
    for sid in pictures:
        who = ", ".join(subject[c] for c in setups[sid]["visible"] if c in subject) or "no people"
        definitions.append(f"{picture[sid]} is the keyframe for every shot framed as camera setup {sid}, showing {who}: "
                           f"{clip(setups[sid].get('shot', ''), 450)}.")
    definitions.append("<Video 1> is the reference performance for the whole target video: its body movements, action "
                       "form, gestures, rhythm, camera framing and cut timing are followed; its people, faces, hairstyles, "
                       "hair accessories, outfits, text, location, grey colouring and blur are not used: it is only a softened grey "
                       "movement guide, and every frame has the full colour, detail and light of the keyframe pictures. "
                       "Every person on screen is one of the "
                       "subjects above with the hair and outfit of their keyframe picture, and each shot shows exactly "
                       "the people of its keyframe picture, never a second copy of anyone.")
    if reuse_audio:
        definitions.append("<Audio 1> is the synchronized original soundtrack of this passage and is reused as the "
                           "target video's complete audio.")

    task = "reference generation + audio reuse" if reuse_audio else "reference generation"
    roster = ", ".join(subject[c] for c in in_segment)
    if roster:
        summary = (f"[{task}] The target video is a {duration}-second {ratio} clip in which {roster} perform the movements "
                   f"and timing of <Video 1> in {place}, looking exactly as in the keyframe pictures"
                   + (", over <Audio 1>." if reuse_audio else ".")
                   + f" Exactly {len(in_segment)} distinct character{'s' if len(in_segment) != 1 else ''} exist"
                   f"{'s' if len(in_segment) == 1 else ''}; nobody is duplicated or added.")
    else:
        summary = (f"[{task}] The target video is a {duration}-second {ratio} clip of {place} with no people: it follows "
                   "the camera movement, motion of the elements and cut timing of <Video 1>, looking exactly as in the "
                   "keyframe pictures" + (", over <Audio 1>." if reuse_audio else ".") + " No person or character appears.")

    retention = [f"{subject[c]} (appears in every shot where it is visible): fully_preserved - identity, face, hair, "
                 "outfit and props from the keyframe pictures." for c in in_segment]
    retention.append(f"{place} (appears in every shot): fully_preserved - the same place, palette and light throughout.")
    for k, _ in enumerate(products or [], 1):
        retention.append(f"{product_label[k]}: fully_preserved - the product's exact shape, colours and parts.")
    retention += [f"{picture[s]} (keyframe of the shots framed as {s}): fully_preserved - composition, character "
                  "placement, place and light." for s in pictures]
    retention.append("<Video 1> (movement, framing and cut structure): partially_preserved - movement, form, rhythm, "
                     "framing and cut timing are followed; its people, clothing, text, location and grey, blurred look "
                     "are replaced by the full-colour keyframe pictures.")
    if reuse_audio:
        retention.append("<Audio 1>: fully_copy - <Audio 1> is reused 1:1 as the target video's complete audio track.")

    opening = (f"The target video is {clip(style, 200) if style else 'realistic, naturally lit footage'}, following "
               "the cuts and movement of <Video 1>.")
    body = [opening]
    shots = [x for x in segment.get("shot_plan") or [] if isinstance(x, dict)]
    if shots:
        for n, shot in enumerate(shots, 1):
            sid = shot.get("setup") if shot.get("setup") in picture else pictures[0]
            lead = (f"[Shot {n}] The shot's keyframe corresponds to {picture[sid]}." if n == 1 else
                    f"[Shot {n}] At {_stamp(float(shot.get('start') or 0))}, the shot cuts to the keyframe of {picture[sid]}.")
            body.append(f"{lead} {clip(shot.get('action', ''), 400)}.")
    else:
        body.append(f"[Shot 1] The shot's keyframe corresponds to {picture[pictures[0]]}. "
                    f"{clip(segment.get('action') or 'The characters follow the movement of <Video 1>.', 900)}.")
    if framing_note:
        body.append(framing_note)
    for line in segment.get("lines") or []:
        if _offscreen(line):
            body.append(f"An off-screen voice (S{speaker_ids.get(line['role'], 1)}) says {said(line['text'])}"
                        + (" in <Audio 1>" if reuse_audio else "") + "; nobody on screen moves their mouth for it.")
            continue
        who = subject.get(line["role"], cast.get(line["role"], {}).get("label", line["role"]))
        body.append(f"{who} (S{speaker_ids.get(line['role'], 1)}) says {said(line['text'])}"
                    + (" lip-synced to <Audio 1>." if reuse_audio else ""))
    for sung in lyrics or []:
        if not in_segment:
            continue
        if sung.get("kind") == "background":
            body.append(f"The words {said(sung['text'])} in <Audio 1> come from the background (off screen); nobody on "
                        "screen moves their mouth for them.")
        else:
            body.append(f"When <Audio 1> reaches the sung words {said(sung['text'])}, the characters keep performing their "
                        "movements with closed or relaxed mouths and do not sing.")
    body.append("The picture carries no subtitles, captions, text overlays, logos or watermarks.")
    if corrections:
        body.append("Corrections from the previous attempt: " + corrections)
    sound = ("The ambience and physical sounds are copied from <Audio 1>." if reuse_audio else
             f"Clean dialogue and the natural room tone of {place}.")
    return "\n\n".join([
        "subject_definitions:\n" + "\n".join(definitions),
        "summary:\n" + summary,
        "retention_analysis:\n" + "\n".join(retention),
        "detailed_description:\n" + "\n".join(body),
        "overall_soundscape:\n" + sound,
        "non_diegetic_music:\n" + ("Any music is contained in <Audio 1>." if reuse_audio else "N/A"),
    ])


IMAGE_PROMPT_LIMIT = 4800


def fit_image_prompt(prompt: str, fixes: list[str]) -> str:
    """Append review corrections within the image service's 5000-character limit."""
    room = IMAGE_PROMPT_LIMIT - len(prompt) - 12
    kept: list[str] = []
    for fix in fixes:
        if room - len(fix) - 1 < 0:
            break
        kept.append(fix)
        room -= len(fix) + 1
    text = prompt + ("\n\nMUST FIX: " + " ".join(kept) if kept else "")
    return text if len(text) <= IMAGE_PROMPT_LIMIT else clip(text, IMAGE_PROMPT_LIMIT)
