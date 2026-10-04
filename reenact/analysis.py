"""Reference understanding: the whole piece and its time-locatable realisation.

One Qwen-VL reading of the source video, grounded by ASR word times and ffmpeg cut
hints, produces the cast, camera setups, shots, speaker attribution of every spoken
sentence, the persistent visual/sound systems and the story beats. The result is kept
as ANALYSIS (why it works) and TIMELINE (what happens when) for the director.
"""
from __future__ import annotations

import json

from . import llm

VO = "VO"  # the speaker id of a voice that belongs to nobody on screen (narrator, system voice, singer)


def _sentence_table(sentences: list[dict]) -> str:
    if not sentences:
        return "(no speech detected)"
    return "\n".join(
        f"#{i} {s['start']:.2f}-{s['end']:.2f}s [{s.get('emotion') or ''}] {s['text']}"
        for i, s in enumerate(sentences)
    )


def instruction(duration: float, sentences: list[dict], hints: list[float], pictures: int) -> str:
    extra = (f"After the video come {pictures} USER PICTURE(s), in order. They are material the user supplied "
             "for the new version; describe each one.\n" if pictures else "")
    return f"""You are a film editor reading a {duration:.2f}-second short video (the attachment) so a director can remake it.
{extra}
Verbatim speech with measured times (ASR, reliable wording and timing):
{_sentence_table(sentences)}

Mechanical cut candidates (seconds): {hints or "none"}

Return ONE JSON object with these keys:
"summary": 3-5 sentences: what happens from opening to close and what the viewer should feel or decide.
"why_it_works": 2-4 sentences: the hook, the turn and the payoff, and how pacing, performance and graphics sell them.
"format": one short label, e.g. "comic dialogue skit", "street interview", "ranking", "talking-head ad", "podcast clip".
"style": {{"camera": "...", "editing": "...", "grade": "...", "performance": "..."}} describing craft only.
"cast": every recurring on-screen character, most important first:
   [{{"id": "A", "label": "their name exactly as the video says or writes it, in the video's own language and script
   (e.g. 余念, not Yu Nian), else an obvious short nickname", "gender": "female|male|unknown", "appearance": "40-90 English words:
   species/face/age, hair, build, every clothing item, accessories and props they hold", "role": "their dramatic function",
   "speaks": true|false, "voice": "English description of their voice and delivery if they speak, else empty",
   "screen_side": "left|center|right|varies"}}]  Use ids A, B, C... Include silent background characters too.
   Use [] when no person or character appears at all (scenery, space, nature, objects, products, abstract motion
   graphics): a voice or song over such footage belongs to nobody on screen.
"props": important objects or products: [{{"id": "P1", "label": "...", "appearance": "...", "owner": "A|null"}}]
"scene": {{"label": "2-5 words", "description": "40-80 English words: place, layout, light, palette, time of day"}}
"setups": the distinct camera setups (a setup is a camera position+framing that the edit returns to; several shots may
   share one): [{{"id": "S1", "framing": "English: shot size, angle, who is where in frame, what is foreground/background",
   "visible": ["A", "B"], "movement": "static|handheld|push-in|pan|orbit|fly-through", "best_time": seconds of the clearest frame of it}}]
   A setup is one shot size + one set of people in frame + one camera side. A single close-up of A, a single close-up of
   B, an over-the-shoulder shot onto A and a two-shot are FOUR different setups even when filmed from the same side;
   never file a single-person shot under a two-shot setup. A close shot of hands or an object and a shot of the place
   with nobody in it are their own setups with "visible": [] (or the owner of the hands). "visible" lists only the
   characters whose face or body is clearly in frame. Usually 2-8 setups; footage without characters uses setups too
   (one per distinct view: wide vista, close detail, fly-through...).
"shots": consecutive shots covering 0 to {duration:.2f}: [{{"start": s, "end": s, "setup": "S1", "kind": "camera|insert",
   "action": "what happens, gestures, reactions (English, 15-40 words)"}}]. Start a new shot at every real cut.
   kind "insert" = an inserted illustration, animation, diagram, measurement graphic, product shot or end card that is
   not camera footage of the performers; "camera" = footage of the performers. Inserts get no camera setup.
"lines": one entry per ASR sentence above, same order: [{{"i": 0, "speaker": "A", "addressee": "B", "delivery": "English:
   attitude and vocal delivery"}}]. Decide the speaker from the voice (a man's or a woman's), the picture (mouth
   movement, framing) and the dialogue logic (whoever is addressed by name or title is NOT the speaker). Use
   "speaker": "VO" for a voice that belongs to nobody on screen (narrator, system voice, announcer, singer).
"graphics": every designed on-screen layer ADDED IN EDITING (captions, titles, stickers, icons, emoji, reveal boards,
   flashes, logos, progress bars). Text or artwork printed on products, packaging, clothes, signs or screens inside the
   filmed scene is NOT a graphic; leave it out. Use [] when the edit adds nothing: [{{"type": "caption|title|sticker|reveal|broll|flash|logo|other", "description": "content, position,
   font/colour, entry and exit behaviour", "times": "when it appears", "tied_to": "the words or events it responds to"}}]
"sound": {{"music": "describe the music bed or 'none'", "sfx": "describe sound effects and where they hit", "ambience": "..."}}
"beats": the story structure: [{{"start": s, "end": s, "function": "hook|setup|conflict|turn|reveal|payoff|cta",
   "note": "what this passage does for the viewer"}}]
"brand_mentions": every product, brand, price or claim named in speech or text: [{{"text": "...", "time": s, "context": "..."}}]
"performance_type": "dialogue|narration|action|mixed" (what carries the video: conversation, a voice explaining,
   physical action/demonstration, or a mix)
"motion_critical": true when the exact body movement matters to the viewer (exercise form, dance, demonstration,
   choreography, stunts), else false
"platform_overlays": things added by the posting platform or app, not part of the design (app logos, account ids,
   watermarks, search bars, end cards): [{{"description": "...", "start": s, "end": s}}]
"content_end": seconds where the designed content ends (before any platform end card); {duration:.2f} if none
For "graphics", copy on-screen text EXACTLY (every line, in order) and give "start"/"end" seconds and "position"
   (top|middle|bottom|left|right|inset) for each graphic.
"user_pictures": [{{"index": 1, "description": "40-80 English words of what the picture shows"}}]  (empty list if none)
Describe only what is visible or audible. JSON only."""


async def understand(source_url: str, duration: float, sentences: list[dict], hints: list[float],
                     picture_urls: list[str], ledger: llm.Ledger) -> dict:
    content: list[dict] = [{"type": "video_url", "video_url": {"url": source_url}, "fps": 2}]
    for index, url in enumerate(picture_urls, 1):
        content += [{"type": "text", "text": f"USER PICTURE {index}:"},
                    {"type": "image_url", "image_url": {"url": url}}]
    content.append({"type": "text", "text": instruction(duration, sentences, hints, len(picture_urls))})
    value = await llm.vision(content, ledger, kind="analysis", max_tokens=8000)
    if not isinstance(value, dict):
        raise llm.ModelError("视频分析结果格式错误")
    return validate(value, duration, sentences)


def validate(value: dict, duration: float, sentences: list[dict]) -> dict:
    # An empty cast is valid: scenery, product and motion-graphics videos are remade without characters.
    cast = [c for c in value.get("cast") or [] if isinstance(c, dict) and c.get("id") and c.get("id") != VO]
    for c in cast:
        c["gender"] = c.get("gender") if c.get("gender") in {"female", "male"} else "unknown"
    ids = {c["id"] for c in cast}
    setups = [s for s in value.get("setups") or [] if isinstance(s, dict) and s.get("id")]
    for setup in setups:
        setup["visible"] = [v for v in setup.get("visible") or [] if v in ids]
        setup["best_time"] = max(0.0, min(float(setup.get("best_time") or 0), duration - 0.05))
    if not setups:
        setups = [{"id": "S1", "framing": "the main view", "visible": list(ids), "movement": "static",
                   "best_time": duration / 2}]
    setup_ids = {s["id"] for s in setups}
    shots = []
    for shot in sorted((s for s in value.get("shots") or [] if isinstance(s, dict)),
                       key=lambda s: float(s.get("start") or 0)):
        start = max(0.0, min(float(shot.get("start") or 0), duration))
        end = max(start, min(float(shot.get("end") or duration), duration))
        if end - start < 0.2:
            continue
        shots.append({"start": round(start, 2), "end": round(end, 2),
                      "setup": shot.get("setup") if shot.get("setup") in setup_ids else setups[0]["id"],
                      "kind": "insert" if str(shot.get("kind")) == "insert" else "camera",
                      "action": str(shot.get("action") or "")})
    lines = {int(l.get("i", -1)): l for l in value.get("lines") or [] if isinstance(l, dict)}
    speakers = [c["id"] for c in cast if c.get("speaks")] or [c["id"] for c in cast[:1]] or [VO]
    attributed = []
    for i, sentence in enumerate(sentences):
        line = lines.get(i) or {}
        speaker = line.get("speaker") if line.get("speaker") in ids | {VO} else speakers[i % len(speakers)]
        attributed.append({**sentence, "i": i, "speaker": speaker,
                           "addressee": line.get("addressee"), "delivery": line.get("delivery") or ""})
    for member in cast:
        member["speaks"] = any(l["speaker"] == member["id"] for l in attributed) or bool(member.get("speaks") and not sentences)
    return {
        "duration": round(duration, 3),
        "summary": str(value.get("summary") or ""),
        "why_it_works": str(value.get("why_it_works") or ""),
        "format": str(value.get("format") or ""),
        "style": value.get("style") if isinstance(value.get("style"), dict) else {},
        "cast": cast,
        "props": [p for p in value.get("props") or [] if isinstance(p, dict)],
        "scene": value.get("scene") if isinstance(value.get("scene"), dict) else {},
        "setups": setups,
        "shots": shots,
        "lines": attributed,
        "graphics": [g for g in value.get("graphics") or [] if isinstance(g, dict)],
        "sound": value.get("sound") if isinstance(value.get("sound"), dict) else {},
        "beats": [b for b in value.get("beats") or [] if isinstance(b, dict)],
        "brand_mentions": [b for b in value.get("brand_mentions") or [] if isinstance(b, dict)],
        "user_pictures": [p for p in value.get("user_pictures") or [] if isinstance(p, dict)],
        "performance_type": str(value.get("performance_type") or "dialogue"),
        "motion_critical": bool(value.get("motion_critical")),
        "platform_overlays": [o for o in value.get("platform_overlays") or [] if isinstance(o, dict)],
        "content_end": round(max(1.0, min(float(value.get("content_end") or duration), duration)), 2),
    }


def analysis_markdown(a: dict) -> str:
    cast = "\n".join(f"- **{c['id']} · {c.get('label','')}**（{'说话' if c.get('speaks') else '不说话'}）："
                     f"{c.get('appearance','')} — {c.get('role','')}" for c in a["cast"])
    graphics = "\n".join(f"- {g.get('type')}: {g.get('description','')}（{g.get('times','')}；响应：{g.get('tied_to','')}）"
                         for g in a["graphics"]) or "- 无"
    beats = "\n".join(f"- {b.get('start')}–{b.get('end')}s **{b.get('function')}**：{b.get('note','')}" for b in a["beats"])
    return (f"# ANALYSIS\n\n**形式**：{a['format']}\n\n## 整体\n{a['summary']}\n\n## 为什么成立\n{a['why_it_works']}\n\n"
            f"## 结构\n{beats}\n\n## 角色\n{cast}\n\n## 场景\n{a['scene'].get('description','')}\n\n"
            f"## 视觉包装系统\n{graphics}\n\n## 声音\n{json.dumps(a['sound'], ensure_ascii=False)}\n\n"
            f"## 风格\n{json.dumps(a['style'], ensure_ascii=False)}\n")


def timeline_markdown(a: dict) -> str:
    setups = {s["id"]: s for s in a["setups"]}
    out = ["# TIMELINE", ""]
    for shot in a["shots"]:
        said = [l for l in a["lines"] if l["start"] < shot["end"] and l["end"] > shot["start"]]
        speech = " / ".join(f"{l['speaker']}：{l['text']}" for l in said)
        out.append(f"## {shot['start']:.2f}–{shot['end']:.2f} · {shot['setup']}（{setups.get(shot['setup'], {}).get('framing', '')}）")
        out.append(shot["action"])
        if speech:
            out.append(f"台词：{speech}")
        out.append("")
    return "\n".join(out)


# ── on-screen text track ────────────────────────────────────────────────────────────

def _norm_text(lines: list[str]) -> str:
    import re
    return re.sub(r"[\s，。！？、,.!?:：;；\"'“”‘’]+", "", "".join(lines)).lower()


def merge_text_frames(frames: list[dict], step: float) -> list[dict]:
    """Consecutive frames showing the same text become one timed entry; one-frame flickers of a
    neighbour's text (OCR slips) do not split an entry."""
    track: list[dict] = []
    for f in sorted(frames, key=lambda x: x["t"]):
        lines = [str(x).strip() for x in f.get("lines") or [] if str(x).strip()]
        key = _norm_text(lines)
        if track and key and (track[-1]["key"] == key or _similar(track[-1]["key"], key)) \
                and f["t"] - track[-1]["end"] <= step * 1.6:
            track[-1]["end"] = round(f["t"] + step, 2)
            if len("".join(lines)) > len("".join(track[-1]["lines"])):
                track[-1]["lines"], track[-1]["key"] = lines, key  # keep the most complete reading
            continue
        if key:
            track.append({"start": f["t"], "end": round(f["t"] + step, 2), "lines": lines, "key": key,
                          "position": str(f.get("position") or "bottom")})
    for item in track:
        item.pop("key")
    return [x for x in track if x["end"] - x["start"] >= step * 0.99]


def _similar(a: str, b: str) -> bool:
    from difflib import SequenceMatcher
    if not a or not b:
        return False
    # A partly read or still-appearing line is the same text as its complete reading.
    return (min(len(a), len(b)) >= 4 and (a in b or b in a)) or SequenceMatcher(None, a, b).ratio() >= 0.8


async def text_track(source, duration: float, work, ledger: llm.Ledger) -> list[dict]:
    """Exact designed on-screen text (subtitles, lyrics, titles) with times, read frame by frame.

    The whole-video reading summarises graphics and often omits their words; this pass copies them."""
    import asyncio
    from . import media, qa

    step = 0.5 if duration <= 90 else 1.0
    frames = await asyncio.to_thread(media.frames_every, source, work, step, 480, duration)
    semaphore = asyncio.Semaphore(6)

    async def read(batch: list[tuple[float, object]]) -> list[dict]:
        content: list[dict] = []
        for t, path in batch:
            content += [{"type": "text", "text": f"FRAME t={t:.2f}:"},
                        {"type": "image_url", "image_url": {"url": qa.data_url(path, 480)}}]
        content.append({"type": "text", "text": """For every frame, copy the text that was ADDED IN EDITING (subtitles,
song lyrics, their translations, titles, labels), exactly as written, one entry per visual line, top to bottom.
Ignore text that is part of the filmed scene (products, packaging, signs, screens, clothing) and platform marks
(app logos, account ids, watermarks). Use an empty list when a frame has no such text.
Return JSON: {"frames": [{"t": 0.5, "lines": ["第一行", "second line"], "position": "top|middle|bottom"}]}"""})
        async with semaphore:
            try:
                value = await llm.vision(content, ledger, kind="text-track", max_tokens=1500)
            except llm.ModelError:
                return []
        out = []
        items = value.get("frames") if isinstance(value, dict) else None
        for item in items if isinstance(items, list) else []:
            try:
                out.append({"t": float(item["t"]), "lines": item.get("lines") or [], "position": item.get("position")})
            except (KeyError, TypeError, ValueError):
                continue
        return out

    batches = [frames[i:i + 8] for i in range(0, len(frames), 8)]
    results = await asyncio.gather(*(read(b) for b in batches))
    return merge_text_frames([f for r in results for f in r], step)


# ── speaker attribution ─────────────────────────────────────────────────────────────

BREAK = set("，。！？；,.!?;")


def utterance_chunks(sentences: list[dict]) -> list[dict]:
    """Split ASR sentences at clause punctuation: one ASR sentence can hold two speakers."""
    chunks = []
    for sentence in sentences:
        current: list[dict] = []
        for word in sentence["words"]:
            current.append(word)
            if word["text"][-1:] in BREAK:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)
    out = []
    for words in chunks:
        text = "".join(w["text"] for w in words).strip()
        if text:
            out.append({"start": words[0]["start"], "end": words[-1]["end"], "text": text, "words": words})
    return out


MALE_BELOW, FEMALE_ABOVE = 150.0, 190.0  # Hz: clear bands only; the overlap is left to the judges


def voice_matches(speaker: str, pitch: float | None, cast: list[dict]) -> str:
    """A measured voice overrules a speaker of the other gender: the only cast member of the voice's gender
    speaks it, and with none it is an off-screen voice. Ambiguous pitches and unknown genders are left alone."""
    if not pitch or not (pitch < MALE_BELOW or pitch > FEMALE_ABOVE):
        return speaker
    heard = "male" if pitch < MALE_BELOW else "female"
    member = next((c for c in cast if c["id"] == speaker), None)
    if member is None or member.get("gender") not in {"male", "female"} or member["gender"] == heard:
        return speaker
    same = [c["id"] for c in cast if c.get("gender") == heard]
    return same[0] if len(same) == 1 else speaker if same else VO


async def attribute_speakers(source, a: dict, sentences: list[dict], work, ledger: llm.Ledger) -> dict:
    """Decide who speaks each clause: a focused visual judgement per clause (mouth movement),
    then one dialogue-logic pass that reconciles the whole conversation."""
    import asyncio
    from PIL import Image
    from . import media, qa

    chunks = utterance_chunks(sentences)
    if not chunks:
        return a
    cast = "\n".join(f"{c['id']}: {c.get('label')} ({c.get('gender', 'unknown')}; voice: {c.get('voice') or 'silent'}) — "
                     f"{c.get('appearance', '')[:220]}" for c in a["cast"]) or "(nobody appears on screen)"
    semaphore = asyncio.Semaphore(6)
    # The voice itself is the strongest evidence: a man's and a woman's voice are rarely confused, while mouths
    # are often hidden or merely parted in reaction shots.
    features = await asyncio.to_thread(lambda: [media.voice_features(source, c["start"], c["end"]) for c in chunks])
    pitches = [f["pitch"] for f in features]
    levels = sorted(f["level"] for f in features if f["level"] is not None)
    dialogue_level = levels[len(levels) * 3 // 4] if levels else None  # the loud (foreground) voices

    async def look(i: int, chunk: dict) -> dict:
        span = chunk["end"] - chunk["start"]
        paths = [await asyncio.to_thread(media.frame, source, work / f"spk-{i}-{k}.jpg",
                                         chunk["start"] + span * f, 480)
                 for k, f in enumerate((0.15, 0.4, 0.65, 0.9))]
        images = [Image.open(p).convert("RGB") for p in paths]
        w, h = images[0].size
        grid = Image.new("RGB", (w * 2, h * 2))
        for k, im in enumerate(images):
            grid.paste(im.resize((w, h)), ((k % 2) * w, (k // 2) * h))
        grid_path = work / f"spk-{i}.jpg"
        grid.save(grid_path, quality=88)
        content = [
            {"type": "image_url", "image_url": {"url": qa.data_url(grid_path, 1280)}},
            {"type": "text", "text": f"""CAST:
{cast}

The four frames (left-to-right, top-to-bottom) are sampled while clause #{i} is spoken: "{chunk['text']}".
Whose mouth is visibly moving as if speaking in these frames? A speaker can be off-screen in a reaction shot.
Return JSON: {{"speaking_on_screen": ["A"], "visible": ["A","B"], "guess": "A", "confidence": 0.0-1.0,
"note": "what you see"}}"""}]
        async with semaphore:
            try:
                value = await llm.vision(content, ledger, kind="speaker-look", max_tokens=300)
            except llm.ModelError:
                value = {}
        return value if isinstance(value, dict) else {}

    if a.get("performance_type") == "action" or not a["cast"]:
        # Nobody is talking on camera: voice and text logic alone separate lyrics from narration.
        looks = [{} for _ in chunks]
    else:
        looks = await asyncio.gather(*(look(i, c) for i, c in enumerate(chunks)))

    def voice(i: int) -> str:
        hz, level = pitches[i], features[i]["level"]
        band = ("low, typical adult male" if hz < 160 else "high, typical female or child" if hz > 180 else "ambiguous"
                ) if hz else ""
        out = f"voice pitch {hz:.0f} Hz ({band})" if hz else "voice pitch unmeasured"
        if level is not None and dialogue_level is not None:
            out += f", {level - dialogue_level:+.0f} dB vs the main dialogue"
        return out

    evidence = "\n".join(
        f"#{i} {c['text']} | {voice(i)} | on-screen speaking: {looks[i].get('speaking_on_screen')} | visible: "
        f"{looks[i].get('visible')} | visual guess {looks[i].get('guess')} ({looks[i].get('confidence')}) | "
        f"{str(looks[i].get('note') or '')[:120]}"
        for i, c in enumerate(chunks))
    verdicts: dict[int, dict] = {}
    try:
        value = await llm.director(
            "You are a script supervisor reconstructing who says each line of a scene. JSON only.",
            f"""CAST:
{cast}

DIALOGUE CLAUSES WITH VOICE AND VISUAL EVIDENCE:
{evidence}

REFERENCE SOUND: {json.dumps(a.get("sound") or {}, ensure_ascii=False)}

First decide what each clause IS: "speech" (a character talks on screen), "voiceover" (a voice off screen: narrator,
system or AI voice, announcer, inner voice), "background" (voices that are part of the environment rather than
the scene's dialogue: a TV, radio or phone speaker, a PA announcement, passers-by, crowd chatter, a distant shout;
typically much quieter than the main dialogue, e.g. -10 dB or less, and unrelated to or only reacting around the
conversation), "lyrics" (words sung in a song or music bed; typical when the words are poetic, rhymed, or nobody's
mouth matches), or "noise" (misheard sound, platform jingles such as "抖音", counting in background music).
Then, for speech and voiceover, decide the speaker, weighing the evidence in this order:
1. The voice: a measured pitch in the male band cannot belong to a female character and vice versa. Match it to the
   cast's gender and voice description. This outranks the visual guess.
2. Dialogue logic: whoever is addressed by name or title ("嫂子", "泼猴", "sir") is NOT the one speaking. ASR often
   mishears names as similar-sounding characters (许年 for 余念), so compare names by sound. A question is usually
   answered by the other person; an accusation and its defence alternate; a speaker's sentence usually continues across
   consecutive clauses unless the logic turns.
3. The visual guess is weak: in reaction shots the listener's lips are often parted while the speaker is off screen.
Use "speaker": "VO" for a voice that belongs to no cast member (a system voice, narrator or singer), and always when
the cast is empty.
Return JSON: {{"speakers": [{{"i": 0, "kind": "speech|voiceover|background|lyrics|noise", "speaker": "A", "reason": "short"}}]}}""", ledger, kind="speaker-logic",
            max_tokens=2500, temperature=0.1)
        for item in value.get("speakers") or []:
            if isinstance(item, dict) and "i" in item:
                verdicts[int(item["i"])] = {"speaker": item.get("speaker"), "evidence": item.get("reason", ""),
                                            "kind": str(item.get("kind") or "speech")}
    except llm.ModelError:
        for i, look in enumerate(looks):
            verdicts[i] = {"speaker": look.get("guess"), "evidence": look.get("note", "")}
    ids = {c["id"] for c in a["cast"]} | {VO}
    lines: list[dict] = []
    a["lyrics"] = []
    for i, chunk in enumerate(chunks):
        kind = (verdicts.get(i) or {}).get("kind", "speech")
        if kind in {"lyrics", "noise", "background"}:
            # Sung words and environmental voices stay in the soundtrack; nobody on screen lip-syncs them.
            a["lyrics"].append({"start": chunk["start"], "end": chunk["end"], "text": chunk["text"], "kind": kind})
            continue
        speaker = (verdicts.get(i) or {}).get("speaker")
        if speaker not in ids:
            speaker = (lines[-1]["speaker"] if lines else a["lines"][0]["speaker"] if a["lines"]
                       else a["cast"][0]["id"] if a["cast"] else VO)
        speaker = voice_matches(speaker, pitches[i], a["cast"])
        offscreen = kind == "voiceover" or speaker == VO
        if (lines and lines[-1]["speaker"] == speaker and lines[-1].get("offscreen") == offscreen
                and chunk["start"] - lines[-1]["end"] < 0.7):
            lines[-1]["end"] = chunk["end"]
            lines[-1]["text"] += chunk["text"]
            lines[-1]["words"] += chunk["words"]
            continue
        delivery = next((l.get("delivery", "") for l in a["lines"] if l["start"] <= chunk["start"] < l["end"] + 0.05), "")
        lines.append({"i": len(lines), "start": chunk["start"], "end": chunk["end"], "text": chunk["text"],
                      "words": list(chunk["words"]), "speaker": speaker, "offscreen": offscreen, "delivery": delivery,
                      "pitch": pitches[i], "level": features[i]["level"], "evidence": (verdicts.get(i) or {}).get("evidence", "")})
    a["lines"] = lines
    for member in a["cast"]:
        member["speaks"] = any(l["speaker"] == member["id"] for l in lines)
    spoken = sum(len([ch for ch in l["text"] if not ch.isspace()]) for l in lines)
    seconds = sum(l["end"] - l["start"] for l in lines)
    from .script import spoken_units
    units = sum(spoken_units(l["text"]) for l in lines)
    a["speech_pace"] = round(max(3.5, min(7.5, units / seconds)), 2) if seconds > 1 else 4.6
    a["speech_seconds"] = round(seconds, 2)
    a["speech_chars"] = spoken
    return a


async def describe_pictures(picture_urls: list[str], ledger: llm.Ledger) -> list[dict]:
    """Short descriptions of user pictures supplied with a template run (the reference is already understood)."""
    if not picture_urls:
        return []
    content: list[dict] = []
    for index, url in enumerate(picture_urls, 1):
        content += [{"type": "text", "text": f"USER PICTURE {index}:"}, {"type": "image_url", "image_url": {"url": url}}]
    content.append({"type": "text", "text": 'Describe each picture in 40-80 English words (who or what it shows, face, '
                                             'hair, clothing, place). Return JSON: {"user_pictures": [{"index": 1, '
                                             '"description": "..."}]}'})
    value = await llm.vision(content, ledger, kind="pictures", max_tokens=1200)
    items = value.get("user_pictures") if isinstance(value, dict) else []
    return [p for p in items or [] if isinstance(p, dict)]
