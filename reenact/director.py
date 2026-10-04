"""Director stage: Brief, Treatment, casting, camera setups, the new Script and packaging.

Two DeepSeek calls, following Hypit's division of authority:
  1. treatment(): what the user asked for (Brief), the creative answer (Treatment),
     who plays each role, where each identity comes from, which camera setups exist.
  2. write_script(): the adopted words with Moments/Selections, per-line camera setup and
     action, and the packaging systems (Caption, reveals, B-roll, sound) bound to them.
The Script is validated against H3's 5-15 s request window and re-requested on violation.
"""
from __future__ import annotations

import json
import math
import re

from . import llm
from .analysis import VO
from .script import estimate_seconds, parse_line, spoken_units, strip_markup

MAX_TAKE = 14.5
MOMENT_COMPONENTS = {"flash", "sfx", "zoom_punch", "sticker"}
MIN_TAKE = 4.0

TREATMENT_SYSTEM = """You are the director of a short-video studio that remakes proven viral videos with new casts,
products and words. You receive a precise reading of a reference video and the user's request. You decide what the
new video is. Follow Hypit's principles:
- The Brief records what the user wants. Interpret the request from its words; when it is ambiguous, choose the most
  plausible reading and state it in "interpretation".
- Preserve what makes the reference work (hook, turn, payoff, relationships, rhythm, graphics roles) and transform every
  manifestation of a replaced role (a replaced product changes the lines, props, graphics and the call to action).
- Identity: an uploaded picture that shows a character is that character's identity authority. Otherwise the
  character is generated from a strong casting direction. Generate each identity once and reuse it everywhere.
- Change ONLY what the user's request asks for. A request that swaps a product, a prop or a brand keeps the same
  people (identity "keep"), their voices, the place (keep_place true), the camera setups and, unless the words must
  change, the soundtrack (audio_strategy "reuse"). Never invent brand names, product names or claims that neither the
  user nor the reference supplies.
- When a character IS replaced, that means REDESIGNING their look while keeping who they are. Keep their dramatic role, their
  relationships and their recognisable archetype, including species and iconic traits the story relies on (a monkey
  king keeps a monkey face and fur, a bull demon keeps horns and a bovine face, a pig spirit keeps a pig face, a named
  historical or mythic figure stays recognisable), because the lines and jokes refer to who they are. Everything
  else is new: face design, hair or fur styling, colour palette, wardrobe genre, silhouette, accessories and props'
  design. Never reuse the reference's signature items (its specific outfits, headwear, eyewear, jewellery, colour
  scheme). A viewer who knows the reference must immediately see a new design of the same characters.
  Only when the user explicitly asks for different kinds of people (e.g. "换成真人", "换成猫") do you change the
  archetype itself; only when the user asks to keep a look do you keep it.
- Characters must be instantly distinguishable from each other: different silhouettes, hair, dominant wardrobe colours
  and accessories. Do not dress the cast in one shared outfit or palette; a unified art direction still gives every
  character their own colour and shape.
- Give the whole remake one fresh visual style (the user's if they name one; otherwise your own confident choice,
  such as a different fashion genre, era, palette or material world) while keeping the reference's camera grammar and
  pacing. The setting may change with that style as long as the story still works there.
- Camera setups: the new video uses a small closed set of camera setups. The main setup (usually the widest, with the
  most characters) is generated first; other setups derive from it so faces, outfits, place and light stay identical.
  Keep the reference's variety of shot sizes: its single-person close-ups stay single-person close-ups ("visible" lists
  only that person), its two-shots stay two-shots, its detail shots stay detail shots. Consecutive shots that differ
  only slightly look like glitches, so never merge a close-up and a two-shot into one setup.
- A reference without characters (scenery, space, nature, objects, motion graphics, a song over imagery) is remade
  without characters: "cast" is [] and every setup has "visible": []; the setups recreate the same kinds of views and
  camera movement in the requested style.
Write image directions in English as a painter's brief: decisive casting, appeal, wardrobe, pose and relationships.
For a person: give the kind and strength of appeal ("exceptionally, strikingly beautiful, with the looks of a leading
actress"), and add "very broad shoulders and excellent head-to-shoulder proportions" for humans. Stylised or
non-human characters (animal heads, mythic figures) keep their species features explicitly. JSON only."""

TREATMENT_USER = """USER REQUEST (their words are the authority):
{prompt}

USER PICTURES (in upload order, 1-based):
{pictures}

TARGET ASPECT RATIO: {ratio}

REFERENCE ANALYSIS:
{analysis}

Return ONE JSON object:
{{
 "brief": {{"goal": "Chinese, one or two sentences", "interpretation": "Chinese: how you read the request, including
   whether characters keep their identity/type with new faces or become entirely new people",
   "replace": ["Chinese list of what changes"], "keep": ["Chinese list of what stays"],
   "facts": ["Chinese: product truths, names, claims that must stay accurate"]}},
 "motion_reference": true|false,
 "audio_strategy": "reuse|reperform",
 "style": "Chinese: the remake's new visual style in one sentence",
 "treatment": "Chinese markdown, 150-400 characters: premise, visual style, story movement (hook/turn/payoff/CTA),
   casting and performance attitude, camera setups, graphics and sound systems of the new video",
 "cast": [{{"id": "A", "source": "reference cast id this character replaces, or null if new", "label": "Chinese name",
   "identity": "upload:N if user picture N shows this character; keep if the request does not replace this character
     (the reference person stays); otherwise generate",
   "person": "English 50-110 words: complete visual casting of the NEW, redesigned character (face, age, species
     features if stylised, hair, build, full wardrobe, accessories, props held), in the remake's style. If identity is
     upload:N, describe what must be preserved from that picture.",
   "keeps": ["Chinese: the archetype traits kept so the character stays recognisable"],
   "look_changes": ["Chinese: at least 3 concrete visible differences from the reference character"],
   "speaks": true|false,
   "voice": "source:<reference cast id> to keep that performer's voice timbre, or design",
   "voice_desc": "English: voice qualities and delivery attitude"}}],
 "keep_place": true|false (true when the request does not change the place),
 "products": [{{"source": "reference prop id or null", "label": "Chinese name", "identity": "upload:N | keep |
   generate", "description": "English: what the product in the new video looks like (from user picture N when given)"}}],
 "setting": "English 40-80 words: the place of the new video, a few telling details, palette, light",
 "setups": [{{"id": "S1", "source": "reference setup id it mirrors or null", "visible": ["A", "B"],
   "derive_from": null or "S1", "shot": "English 40-90 words: shot size, camera angle, exact placement of each visible
   character (left/right/foreground), gaze direction, idle poses and props, what occupies the frame edges. A setup is a
   reusable idle state that several lines continue from: no pointing, shouting or mid-action gestures"}}]
}}
motion_reference: true when the reference's appeal depends on exact physical movement (exercise, dance, demonstration,
choreography) or when the user only swaps people/place and keeps everything else; the new takes then follow the
reference footage's movement. audio_strategy: read the request. "reuse" keeps the original soundtrack 1:1 and the new
performers lip-sync to it: choose it when the request says to keep the rest (其余不变 / 保持一样 / 其他一样) or does not
touch what is said, and the old words still make sense after the change. "reperform" speaks new or re-voiced lines:
choose it when the request asks to change words, language, voice or a brand/product named in the speech, or when the
change makes the old words wrong. Swapping people, places or products alone does not require new words.
Setups are real camera views: of the characters, or of the product/objects with nobody in frame (product shots,
cutaways; use "visible": [] for those). Inserted illustrations, animations and graphics are not setups (the packaging
rebuilds them as B-roll), and platform end cards, logos and search pages are excluded entirely. Every reference
camera setup that is real footage gets a counterpart; when keep_place is true, mirror them one to one.
Rules: 1-8 setups (as many as the reference really uses), each a clearly different camera position or shot size (never two near-identical views); the first
setup has derive_from null and is the widest establishing view (with the most characters when there are any); each character appears
in at least one setup; keep background characters if the reference has them; every speaking character needs a setup
where they are clearly visible; cast ids are A, B, C… in importance order."""

SCRIPT_SYSTEM = """You are the head writer and editor of a short-video studio. You write the new Script for a remake,
keeping the reference's proven structure: the same kind of hook in the first seconds, the same turn and payoff, the
same comic or persuasive mechanics, similar total length and rhythm. Replace what the Brief replaces and keep lines
natural in the reference's spoken language. Each Segment becomes ONE generated video request of 5-15 seconds, so group
consecutive lines into Segments of about 6-13 seconds of speech; a Segment may contain several speakers and camera cuts.

Script line markup:
  ||            breaks the on-screen caption into the next cue (use at natural phrase boundaries)
  @{id!}        a Moment placed right before the word it marks (a reveal, a price, a brand name, a punchline)
  @{id}…@{/id}  a Selection spanning the words an element illustrates (B-roll, title)
  <shown|spoken>  shown text differs from pronunciation, e.g. <EQUAI|E Q AI>, <1.5|一点五>, <AI|A I>
Ids are lowercase ascii. Every id used by packaging must exist in the Script. Place each Moment immediately before
the exact word it stands for (a reveal labelled "稳定半年" sits right before "稳定"), so graphics land on that word.

Packaging components you can use (bind each to Script Moments/Selections, never to seconds):
  {"type":"title","selection":"id","text":"short headline"}              banner while the Selection is spoken
  {"type":"reveal_strip","items":[{"moment":"id","label":"2-6 chars","icon":"English prompt for a flat icon"}]}
                                                                          slots that reveal one by one, persist
  {"type":"flash","moment":"id","color":"#RRGGBB"}                        full-frame colour flash
  {"type":"sfx","moment":"id","kind":"pop|ding|whoosh|boom|cash|riser"}   sound effect hit
  {"type":"broll","selection":"id","prompt":"English image prompt","layout":"card|pip|full"}
  {"type":"sticker","moment":"id","text":"short text","color":"#RRGGBB"}  pop-up text sticker ~1.5 s
  {"type":"logo_card","selection":"id","text":"BRAND","sub":"short tagline"}  brand end card / CTA
  {"type":"zoom_punch","moment":"id"}                                    quick push-in on the picture
Never invent brand names, product names or claims: use only names and facts from the user's request, the Brief facts
or the reference. Keep the reference's lines, their order and their length; change only the words the request requires. Never add
lines, jokes, scenes or selling points the reference does not have. When the Brief replaces a product or brand,
rewrite every line that sells it around the new product's real benefits
from the Brief facts and the user's request: do not merely swap the name, and never leave leftovers of the old product
(its version numbers, its specific claims). Keep lines that have nothing to do with the replacement close to the
original, because their rhythm is part of what works. For an advertising skit, end on a clear brand payoff.
Use only the kinds of graphics the reference actually has (listed below), plus any the user explicitly asks for;
never add stickers, icons, reveal strips, flashes, sound effects or zooms the reference does not use.
Action directions are English, directing attitude and a few decisive reactions; never exact finger counts; never
ask the video to show text, subtitles or logos. JSON only."""

SCRIPT_USER = """BRIEF:
{brief}

TREATMENT:
{treatment}

CAST (ids you must use as roles; "VO" is an off-screen voice such as a narrator or system voice that nobody on
screen lip-syncs):
{cast}

CAMERA SETUPS (ids you must use per line):
{setups}

REFERENCE SPEECH WITH TIMES AND SPEAKERS (reference cast ids):
{lines}

REFERENCE STRUCTURE:
{beats}

REFERENCE GRAPHICS AND SOUND:
{graphics}

USER REQUEST: {prompt}
Reference duration: {duration:.1f}s. Target total speech: about {target:.0f}s. The reference is spoken at about
{pace:.1f} syllables per second; size Segments for that pace (at most ~{budget:.0f} syllables per Segment).
Every Moment/Selection id is unique. Moment components (flash, sfx, zoom_punch, sticker, reveal_strip items) need
Moments @{{id!}}; title/broll/logo_card need Selections @{{id}}…@{{/id}}.
{correction}
Return ONE JSON object:
{{
 "segments": [{{"id": "seg1", "purpose": "Chinese: what this passage does",
   "lines": [{{"role": "A", "text": "the line with markup", "setup": "S1",
     "action": "English: this line's attitude, reaction of listeners, gesture"}}]}}],
 "captions": {{"position": "bottom|middle", "size": "s|m|l", "animation": "pop|fade|none", "uppercase": false,
   "styles": {{"<role id>": {{"fill": "#RRGGBB", "stroke": "#RRGGBB"}}}}}},
 "components": [ ...packaging components... ],
 "music": {{"mood": "English short description", "energy": "low|mid|high"}}
}}"""


def _compact_analysis(a: dict) -> str:
    keep = {k: a.get(k) for k in ("format", "summary", "why_it_works", "style", "cast", "props", "scene",
                                   "setups", "beats", "graphics", "sound", "brand_mentions")}
    keep["shots"] = [{**s, "action": s["action"][:160]} for s in a.get("shots", [])]
    keep["dialogue"] = [{"t": f"{l['start']:.1f}-{l['end']:.1f}", "speaker": l["speaker"], "text": l["text"]}
                        for l in a.get("lines", [])]
    return json.dumps(keep, ensure_ascii=False)


WORD_CHANGE = re.compile(r"台词|文案|对白|口播|旁白|配音|声音|音色|嗓音|说成|改说|改成说|念成|翻译|英文|英语|中文|日语|韩语|"
                         r"粤语|方言|改词|改剧本|重写|新剧本|新台词|script|dialogue|voice|translate|language", re.I)


def needs_new_words(prompt: str, analysis: dict) -> bool:
    """Only a request that changes what is said (words, language, voice, or a brand named in the speech)
    re-performs the soundtrack; swapping people, places or products keeps it."""
    if WORD_CHANGE.search(prompt or ""):
        return True
    spoken = "".join(str(l.get("text") or "") for l in analysis.get("lines") or []).lower()
    if not spoken:
        return False
    for match in re.finditer(r"([A-Za-z][A-Za-z0-9 .]{1,30}?|[\u4e00-\u9fff]{1,8})\s*(?:换成|替换成|改成|变成|->|→)", prompt or ""):
        name = match.group(1).strip().lower()
        if len(name) >= 2 and (name in spoken or name.replace(" ", "") in spoken.replace(" ", "")):
            return True
    return False


async def treatment(prompt: str, analysis: dict, ratio: str, ledger: llm.Ledger) -> dict:
    pictures = "\n".join(f"{p.get('index')}: {p.get('description')}" for p in analysis.get("user_pictures") or []) or "none"
    value = await llm.director(TREATMENT_SYSTEM, TREATMENT_USER.format(
        prompt=prompt, pictures=pictures, ratio=ratio, analysis=_compact_analysis(analysis),
    ), ledger, kind="treatment", temperature=0.5)
    return validate_treatment(value, analysis)


def _identity(value: object, uploads: int, has_source: bool) -> str:
    identity = str(value or "generate")
    if identity.startswith("upload:"):
        try:
            n = int(identity.split(":", 1)[1])
        except ValueError:
            n = 0
        return f"upload:{n}" if 1 <= n <= uploads else "generate"
    return "keep" if identity == "keep" and has_source else "generate"


def validate_treatment(value: dict, analysis: dict) -> dict:
    uploads = len(analysis.get("user_pictures") or [])
    cast = [c for c in value.get("cast") or [] if isinstance(c, dict) and c.get("id") and c.get("id") != VO]
    if not cast and analysis["cast"]:
        raise llm.ModelError("导演没有给出角色表")
    source_ids = {c["id"] for c in analysis["cast"]}
    for c in cast:
        c["identity"] = _identity(c.get("identity"), uploads, bool(c.get("source")))
        voice = str(c.get("voice") or "design")
        if c["identity"] == "keep" and c.get("source") in source_ids:
            voice = f"source:{c['source']}"
        c["voice"] = voice if voice.startswith("source:") and voice.split(":", 1)[1] in source_ids else "design"
        c["person"] = str(c.get("person") or "")
    ids = {c["id"] for c in cast}
    setups = [s for s in value.get("setups") or [] if isinstance(s, dict) and s.get("id")][:8]
    if not setups:
        raise llm.ModelError("导演没有给出机位")
    setup_ids = [s["id"] for s in setups]
    for i, s in enumerate(setups):
        s["visible"] = [v for v in s.get("visible") or [] if v in ids]  # [] = product/object shot
        derive = s.get("derive_from")
        s["derive_from"] = derive if i and derive in setup_ids[:i] else (setup_ids[0] if i else None)
        if s.get("source") not in {x["id"] for x in analysis["setups"]}:
            s["source"] = None
    people_first = sorted(setups, key=lambda x: (not x["visible"], setups.index(x)))
    if people_first[0] is not setups[0]:
        setups = people_first
        for i, x in enumerate(setups):
            x["derive_from"] = None if i == 0 else (x.get("derive_from") if x.get("derive_from") in
                                                     [y["id"] for y in setups[:i]] else setups[0]["id"])
    setups[0]["derive_from"] = None
    # A reference setup used only by inserted graphics is not a camera view of the characters.
    insert_only = {x["id"] for x in analysis["setups"]} - {sh["setup"] for sh in analysis.get("shots") or []
                                                            if sh.get("kind") != "insert"}
    kept = [x for x in setups if x.get("source") not in insert_only or x.get("source") is None]
    if kept and len(kept) < len(setups):
        setups = kept
        setups[0]["derive_from"] = None
        valid = {x["id"] for x in setups}
        for x in setups[1:]:
            if x.get("derive_from") not in valid:
                x["derive_from"] = setups[0]["id"]
    return {
        "brief": value.get("brief") if isinstance(value.get("brief"), dict) else {},
        "treatment": str(value.get("treatment") or ""),
        "style": str(value.get("style") or ""),
        "keep_place": bool(value.get("keep_place")),
        "products": [{**p, "identity": _identity(p.get("identity"), uploads, bool(p.get("source")))}
                     for p in value.get("products") or [] if isinstance(p, dict)],
        "motion_reference": bool(value.get("motion_reference", analysis.get("motion_critical"))),
        "audio_strategy": "reuse" if str(value.get("audio_strategy")) == "reuse" else "reperform",
        "cast": cast,
        "setting": str(value.get("setting") or ""),
        "setups": setups,
    }


def replaced_names(analysis: dict, direction: dict, prompt: str) -> set[str]:
    """Latin brand/product tokens from the reference that the Brief or the user asked to replace."""
    import re
    wanted = (" ".join(direction.get("brief", {}).get("replace") or []) + " " + prompt).lower()
    names = set()
    for mention in analysis.get("brand_mentions") or []:
        for token in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", str(mention.get("text") or "")):
            if token.lower() in wanted and token.lower() not in {"ai", "app", "the"}:
                names.add(token.lower())
    for token in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", prompt):
        if re.search(rf"{token}\s*(换成|替换|改成|→|->|to)", prompt, re.I):
            names.add(token.lower())
    return names


def _check_script(value: dict, direction: dict, pace: float = 4.6, old_names: set[str] | None = None,
                  source_units: float = 0.0, limit: float = 1.6) -> list[str]:
    problems = []
    if source_units > 0:
        new_units = sum(spoken_units(strip_markup(str(l.get("text") or ""), spoken=True))
                        for seg in value.get("segments") or [] for l in seg.get("lines") or [])
        if new_units > source_units * limit + 4:
            problems.append(f"the new Script is much longer than the reference ({new_units:.0f} vs {source_units:.0f} "
                            "syllables); keep the same length and number of lines, add no new lines or plot")
    import re
    from difflib import SequenceMatcher
    lines_text = " ".join(str(l.get("text") or "") for seg in value.get("segments") or [] for l in seg.get("lines") or [])
    tokens = {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", lines_text)}
    for name in old_names or set():
        # ASR spellings of the old name (Grok/GROM) count as the old name too.
        hits = [t for t in tokens if t == name or SequenceMatcher(None, t, name).ratio() >= 0.75]
        if hits:
            problems.append(f"the replaced brand/product '{name}' still appears as {sorted(hits)}; rewrite those lines "
                            "around the new product")
    roles = {c["id"] for c in direction["cast"]} | {VO}
    setups = {s["id"]: s for s in direction["setups"]}
    segments = value.get("segments") or []
    if not segments:
        return ["no segments"]
    moments: set[str] = set()
    selections: set[str] = set()
    for seg in segments:
        lines = [l for l in seg.get("lines") or [] if isinstance(l, dict)]
        if not lines:
            problems.append(f"{seg.get('id')}: no lines")
            continue
        est = estimate_seconds([strip_markup(str(l.get("text") or ""), spoken=True) for l in lines], pace)
        if est > MAX_TAKE:
            problems.append(f"{seg.get('id')}: about {est:.1f}s of speech, over the 14.5s limit; split it or shorten")
        for l in lines:
            if l.get("role") not in roles:
                problems.append(f"{seg.get('id')}: unknown role {l.get('role')}")
            if l.get("setup") not in setups:
                problems.append(f"{seg.get('id')}: unknown setup {l.get('setup')}")
            parsed = parse_line("x", str(l.get("text") or ""))
            for name in list(parsed.moments) + list(parsed.selections):
                if name in moments or name in selections:
                    problems.append(f"id '{name}' is used more than once; every Moment/Selection id must be unique")
            moments |= set(parsed.moments)
            selections |= set(parsed.selections)
    for comp in value.get("components") or []:
        kind = comp.get("type")
        if kind == "reveal_strip":
            refs = [item.get("moment") for item in comp.get("items") or []]
            if len(set(refs)) != len(refs):
                problems.append("reveal_strip items must each have their own Moment (they reveal one by one)")
            wanted = moments
        elif kind in MOMENT_COMPONENTS:
            refs, wanted = [comp.get("moment")], moments
        else:
            refs, wanted = [comp.get("selection") or comp.get("moment")], selections | moments
        for r in refs:
            if not r or r not in wanted:
                need = "a Moment @{id!}" if wanted is moments else "a Selection @{id}...@{/id}"
                problems.append(f"component {kind} must refer to {need} that exists in the Script (got {r!r})")
    return problems


async def write_script(prompt: str, analysis: dict, direction: dict, ledger: llm.Ledger) -> dict:
    lines = "\n".join(f"{l['start']:.1f}-{l['end']:.1f}s {l['speaker']}: {l['text']}" for l in analysis["lines"]) or "(no speech)"
    spoken = sum(l["end"] - l["start"] for l in analysis["lines"]) or analysis["duration"] * 0.7
    pace = float(analysis.get("speech_pace") or 4.6)
    correction = ""
    value: dict = {}
    for attempt in range(3):
        value = await llm.director(SCRIPT_SYSTEM, SCRIPT_USER.format(
            brief=json.dumps(direction["brief"], ensure_ascii=False), treatment=direction["treatment"],
            cast=json.dumps([{k: c.get(k) for k in ("id", "label", "source", "speaks", "voice_desc")} for c in direction["cast"]], ensure_ascii=False),
            setups=json.dumps([{k: s.get(k) for k in ("id", "visible", "shot")} for s in direction["setups"]], ensure_ascii=False),
            lines=lines, beats=json.dumps(analysis.get("beats"), ensure_ascii=False),
            graphics=json.dumps({"graphics": analysis.get("graphics"), "sound": analysis.get("sound")}, ensure_ascii=False),
            prompt=prompt, duration=analysis["duration"], target=spoken, correction=correction,
            pace=pace, budget=(MAX_TAKE - 1.5) * pace,
        ), ledger, kind=f"script-{attempt + 1}", temperature=0.6)
        source_units = sum(spoken_units(l["text"]) for l in analysis.get("lines") or [])
        # "Keep the rest" requests keep the length too; others only stop a runaway rewrite.
        keep_rest = bool(re.search(r"其余|其他|保持|不变|一样|照搬|原样", prompt or ""))
        problems = _check_script(value, direction, pace, replaced_names(analysis, direction, prompt), source_units,
                                 1.15 if keep_rest else 1.6)
        if not problems:
            result = normalise_script(value, direction, pace=pace)
            result["components"] = drop_platform_marks(result["components"], analysis)
            return keep_reference_graphics(result, analysis, prompt)
        correction = "YOUR PREVIOUS SCRIPT HAD THESE PROBLEMS, FIX THEM:\n- " + "\n- ".join(problems[:12])
    result = normalise_script(value, direction, force=True, pace=pace)
    result["components"] = drop_platform_marks(result["components"], analysis)
    return keep_reference_graphics(result, analysis, prompt)


def normalise_script(value: dict, direction: dict, *, force: bool = False, pace: float = 4.6) -> dict:
    """Clean the Script; with force, split overlong Segments at line boundaries and drop dangling references."""
    roles = [c["id"] for c in direction["cast"]]
    setups = {s["id"]: s for s in direction["setups"]}
    segments = []
    for seg in value.get("segments") or []:
        lines = []
        for l in seg.get("lines") or []:
            if not isinstance(l, dict) or not str(l.get("text") or "").strip():
                continue
            role = l.get("role") if l.get("role") in roles or l.get("role") == VO else (roles[0] if roles else VO)
            setup = l.get("setup") if l.get("setup") in setups else next(
                (sid for sid, s in setups.items() if role in s["visible"]), next(iter(setups)))
            lines.append({"role": role, "text": str(l["text"]).strip(), "setup": setup,
                          "action": str(l.get("action") or "")})
        if not lines:
            continue
        chunks, current = [], []
        for line in lines:
            trial = current + [line]
            if current and estimate_seconds([strip_markup(x["text"], spoken=True) for x in trial], pace) > MAX_TAKE:
                chunks.append(current)
                current = [line]
            else:
                current = trial
        chunks.append(current)
        for n, chunk in enumerate(chunks, 1):
            seg_id = str(seg.get("id") or f"seg{len(segments) + 1}")
            segments.append({"id": seg_id if len(chunks) == 1 else f"{seg_id}-{n}",
                             "purpose": str(seg.get("purpose") or ""), "lines": chunk})
    if not segments:
        raise llm.ModelError("新台词为空")
    for i, seg in enumerate(segments, 1):
        seg["id"] = f"seg{i:02d}"
        est = estimate_seconds([strip_markup(l["text"], spoken=True) for l in seg["lines"]], pace)
        seg["estimate"] = round(est, 2)
        seg["pace"] = pace
        seg["request_duration"] = int(max(5, min(15, math.ceil(max(est, MIN_TAKE) + 0.8))))
    moments, selections = set(), set()
    for seg in segments:
        for l in seg["lines"]:
            parsed = parse_line(l["role"], l["text"])
            moments |= set(parsed.moments)
            selections |= set(parsed.selections)
    components = []
    for comp in value.get("components") or []:
        if not isinstance(comp, dict):
            continue
        kind = comp.get("type")
        if kind == "reveal_strip":
            seen, items = set(), []
            for item in comp.get("items") or []:
                if isinstance(item, dict) and item.get("moment") in moments and item["moment"] not in seen:
                    seen.add(item["moment"])
                    items.append(item)
            comp["items"] = items
            if items:
                components.append(comp)
        elif kind in MOMENT_COMPONENTS:
            if comp.get("moment") in moments:
                components.append(comp)
        elif (comp.get("selection") in selections) or (comp.get("moment") in moments):
            components.append(comp)
    captions = value.get("captions") if isinstance(value.get("captions"), dict) else {}
    return {"segments": segments, "captions": captions, "components": components,
            "music": value.get("music") if isinstance(value.get("music"), dict) else {}}


def script_markdown(script: dict, direction: dict) -> str:
    labels = {c["id"]: c.get("label", c["id"]) for c in direction["cast"]}
    out = ["# SCRIPT", ""]
    for seg in script["segments"]:
        out.append(f"## {seg['id']} · {seg.get('purpose','')}（约 {seg['estimate']}s → 请求 {seg['request_duration']}s）")
        for l in seg["lines"]:
            out.append(f"- **{labels.get(l['role'], l['role'])}** [{l['setup']}] {l['text']}  \n  _{l['action']}_")
        out.append("")
    out.append("## 包装")
    out.append("```json\n" + json.dumps({"captions": script["captions"], "components": script["components"],
                                          "music": script["music"]}, ensure_ascii=False, indent=1) + "\n```")
    return "\n".join(out)


# ── reuse mode: the reference's own soundtrack and timeline ──────────────────────────

def plan_reuse_segments(analysis: dict, direction: dict, max_len: float = 13.0) -> list[dict]:
    """Cut the reference timeline (0..content_end) into 5-15 s passages at shot cuts or speech gaps.

    Each passage keeps its original lines verbatim; its new take follows the reference footage
    of the same range and lip-syncs to the reused audio."""
    end = float(analysis.get("content_end") or analysis["duration"])
    lines = analysis.get("lines") or []
    cuts = sorted({round(float(x["start"]), 2) for x in analysis.get("shots") or [] if 0 < float(x["start"]) < end})
    speaking = [(float(l["start"]), float(l["end"])) for l in lines]

    def inside_speech(t: float) -> bool:
        return any(a + 0.05 < t < b - 0.05 for a, b in speaking)

    candidates = [t for t in cuts if not inside_speech(t)]
    gaps = [(b1 + a2) / 2 for (_, b1), (a2, _) in zip(speaking, speaking[1:]) if a2 - b1 > 0.25]
    candidates = sorted(set(candidates + [round(g, 2) for g in gaps if 0 < g < end]))
    bounds, cursor = [0.0], 0.0
    while end - cursor > max_len + 2:
        window = [t for t in candidates if cursor + 5 <= t <= cursor + max_len]
        target = cursor + min(max_len, (end - cursor) / max(1, round((end - cursor) / 10)))
        nxt = min(window, key=lambda t: abs(t - target)) if window else cursor + max_len
        bounds.append(round(nxt, 2))
        cursor = nxt
    bounds.append(round(end, 2))
    if len(bounds) > 2 and bounds[-1] - bounds[-2] < 5:
        bounds.pop(-2)
    source_to_cast = {c.get("source"): c["id"] for c in direction["cast"] if c.get("source")}
    setups = direction["setups"]
    segments = []
    for i, (a, b) in enumerate(zip(bounds, bounds[1:]), 1):
        seg_lines = []
        for l in lines:
            mid = (float(l["start"]) + float(l["end"])) / 2
            if a <= mid < b:
                role = VO if l["speaker"] == VO else source_to_cast.get(
                    l["speaker"], direction["cast"][0]["id"] if direction["cast"] else VO)
                setup = next((x["id"] for x in setups if role in x["visible"]), setups[0]["id"])
                seg_lines.append({"role": role, "text": l["text"], "setup": setup, "action": l.get("delivery", ""),
                                  "offscreen": bool(l.get("offscreen")) or role == VO,
                                  "source_start": l["start"], "source_end": l["end"]})
        shots = [x for x in analysis.get("shots") or [] if x["start"] < b and x["end"] > a]
        segments.append({"id": f"seg{i:02d}", "source_start": a, "source_end": b, "lines": seg_lines,
                         "shots": shots, "purpose": "", "estimate": round(b - a, 2),
                         "request_duration": int(max(5, min(15, math.ceil(b - a))))})
    return segments


REUSE_SYSTEM = """You are the editor of a short-video studio remaking a reference video shot-for-shot with a new cast
and place, while its soundtrack, words and timing are kept exactly. You direct each passage's movement for the video
model and rebuild the reference's graphics on the same timeline. Every graphic is placed with "source": [start, end]
in the reference's seconds. JSON only."""

REUSE_USER = """CAST (new characters; ids are roles):
{cast}

CAMERA SETUPS:
{setups}

PASSAGES (reference time ranges; lines are kept verbatim; shot times are RELATIVE to the passage start, which is
0 s of that passage's generated clip; shots describe the reference movement):
{segments}

REFERENCE GRAPHICS (copy their text exactly; exclude platform overlays):
{graphics}

ON-SCREEN TEXT TRACK (read frame by frame: the exact words and reference seconds of every designed text; it is the
authority for the words and times of rebuilt captions, lyrics and titles):
{text_track}

PLATFORM OVERLAYS TO EXCLUDE: {overlays}
SONG LYRICS AND BACKGROUND VOICES (reference seconds; nobody on screen lip-syncs them): {lyrics}

USER REQUEST: {prompt}

Return ONE JSON object:
{{
 "segments": [{{"id": "seg01", "setup_flow": ["S1"],
   "shot_plan": [{{"start": 0.0, "setup": "S1", "action": "English 20-60 words: what the characters do in this shot,
     by role label, with no words about old appearance, clothes, text or the old place"}}],
   "action": "English, 40-120 words: the exact movements, exercise
   form, gestures, rhythm and camera cuts of this passage in order, with times relative to the passage start
   (\"At 0.7 s …\"), naming characters by role label only, with no words about their old appearance, clothes or the
   old place, and no singing or mouthing of song lyrics"}}],
 "captions": {{"enabled": true|false (true only if the reference shows spoken captions), "position": "bottom|middle",
   "size": "s|m|l", "animation": "pop|fade|none", "styles": {{"<role id>": {{"fill": "#RRGGBB", "stroke": "#RRGGBB"}}}}}},
 "components": [
   {{"type": "headline", "source": [s, e], "lines": [{{"text": "exact text", "color": "#RRGGBB", "size": "l|m|s"}}],
     "position": "top|middle|bottom", "plate": "none|dark|light"}},
   {{"type": "broll", "source": [s, e], "prompt": "English image prompt recreating the inset's content", "layout":
     "card|pip|full|half", "position": "top|middle|bottom"}},
   {{"type": "sticker", "source": [s, e], "text": "...", "color": "#RRGGBB"}},
   {{"type": "flash", "source": [s, s], "color": "#RRGGBB"}}, {{"type": "sfx", "source": [s, s], "kind": "pop|ding|whoosh"}}
 ]
}}
Recreate every designed graphic of the reference (titles, changing headlines, labels, inserted illustrations) at the
same times with the same text. Every entry of the ON-SCREEN TEXT TRACK that belongs to a kept graphic becomes one
headline component with the same "source" times, the same lines in the same order (e.g. a lyric and its
translation) and the reference's position. The texts come from OCR and may contain look-alike character mistakes (e.g. 级炼 for
锻炼, 手臀 for 手臂): correct obvious ones. Never recreate platform marks (app logos, account ids, search bars, QR codes,
end cards); do not add graphics the reference does not have. If the user's request changes some
text, change it accordingly.
Actions describe only the performers' movement; never describe inserted graphics, text, measurements or logos in an
action. If the reference already shows the spoken words as on-screen text, rebuild that as headline components and
set captions.enabled to false: never show the same words twice.
Captions are built only from the passages' lines. Song lyrics get on-screen text only where the reference shows them
as text (they then appear in REFERENCE GRAPHICS) or where the user asks for subtitles (字幕): rebuild them as headline
components at their "source" times, keeping any translation line the reference shows. Background voices never get text.
Lines of role "VO" are an off-screen voice: in actions, nobody on screen speaks them."""


async def write_reuse(prompt: str, analysis: dict, direction: dict, ledger: llm.Ledger) -> dict:
    segments = plan_reuse_segments(analysis, direction)
    # Shot times are given relative to each passage: its take starts at 0 s.
    brief_segments = [{"id": x["id"], "source": [x["source_start"], x["source_end"]],
                       "length": round(x["source_end"] - x["source_start"], 1),
                       "lines": [f"{l['role']}: {l['text']}" for l in x["lines"]],
                       "shots": [f"{max(0.0, sh['start'] - x['source_start']):.1f}-"
                                 f"{min(sh['end'], x['source_end']) - x['source_start']:.1f}s " + (
                           "[INSERTED GRAPHIC: rebuilt by packaging as B-roll; the performers simply continue here] "
                           if sh.get("kind") == "insert" else f"[{sh['setup']}] ") + sh["action"] for sh in x["shots"]]}
                      for x in segments]
    value = await llm.director(REUSE_SYSTEM, REUSE_USER.format(
        cast=json.dumps([{k: c.get(k) for k in ("id", "label", "source")} for c in direction["cast"]], ensure_ascii=False),
        setups=json.dumps([{k: x.get(k) for k in ("id", "source", "visible", "shot")} for x in direction["setups"]], ensure_ascii=False),
        segments=json.dumps(brief_segments, ensure_ascii=False),
        graphics=json.dumps(analysis.get("graphics"), ensure_ascii=False),
        text_track=json.dumps(analysis.get("text_track") or [], ensure_ascii=False),
        overlays=json.dumps(analysis.get("platform_overlays"), ensure_ascii=False), prompt=prompt,
        lyrics=json.dumps([{"source": [x["start"], x["end"]], "kind": x.get("kind"), "text": x["text"]}
                           for x in analysis.get("lyrics") or [] if x.get("kind") != "noise"], ensure_ascii=False),
    ), ledger, kind="reuse-plan", temperature=0.3)
    actions = {x.get("id"): x for x in value.get("segments") or [] if isinstance(x, dict)}
    setup_ids = [x["id"] for x in direction["setups"]]
    source_setups = {x.get("source"): x["id"] for x in direction["setups"] if x.get("source")}
    for seg in segments:
        plan = actions.get(seg["id"]) or {}
        seg["action"] = str(plan.get("action") or "")
        flow = [f for f in plan.get("setup_flow") or [] if f in setup_ids]
        if not flow:
            flow = [source_setups.get(sh["setup"]) for sh in seg["shots"]]
            flow = [f for f in flow if f] or [setup_ids[0]]
        seg["setup_flow"] = list(dict.fromkeys(flow))
        plan = []
        for shot in (actions.get(seg["id"]) or {}).get("shot_plan") or []:
            if isinstance(shot, dict) and shot.get("action"):
                try:
                    start = max(0.0, min(float(shot.get("start") or 0), seg["source_end"] - seg["source_start"]))
                except (TypeError, ValueError):
                    start = 0.0
                plan.append({"start": round(start, 2), "setup": shot.get("setup") if shot.get("setup") in setup_ids
                             else seg["setup_flow"][0], "action": str(shot["action"])})
        seg["shot_plan"] = sorted(plan, key=lambda x: x["start"])
        seg["mode"] = "reuse"
    components = []
    for comp in value.get("components") or []:
        if not isinstance(comp, dict):
            continue
        src = comp.get("source")
        if isinstance(src, list) and len(src) == 2:
            try:
                comp["source"] = [float(src[0]), float(src[1])]
                components.append(comp)
            except (TypeError, ValueError):
                continue
    components = drop_platform_marks(rebuild_text_track(components, analysis, prompt), analysis)
    captions = value.get("captions") if isinstance(value.get("captions"), dict) else {"enabled": False}
    if not any(seg["lines"] for seg in segments):
        captions["enabled"] = False  # nothing is spoken: sung lyrics never become Captions
    elif captions.get("enabled", True):
        from .align import char_error_rate
        spoken = "".join(l["text"] for seg in segments for l in seg["lines"])
        shown = "".join(str(line.get("text") or "") for comp in components if comp.get("type") == "headline"
                        for line in comp.get("lines") or [])
        if shown and char_error_rate(spoken, shown) < 0.5:
            captions["enabled"] = False  # the headlines already carry the spoken words
    for seg in segments:
        flow = [f for f in seg["setup_flow"] if f in setup_ids]
        seg["setup_flow"] = flow or [setup_ids[0]]
    return keep_reference_graphics({"mode": "reuse", "segments": segments, "captions": captions,
                                    "components": components, "music": {}}, analysis, prompt)


NO_TEXT_REQUEST = re.compile(r"(不要|去掉|删掉|去除|无|没有|不加)(任何)?(字幕|文字)|no (subtitles|captions|text)", re.I)


def rebuild_text_track(components: list[dict], analysis: dict, prompt: str) -> list[dict]:
    """Every designed text the reference shows (subtitles, lyrics and their translations, titles) comes back
    with the same words, times and position, whether or not the editor remembered it. The editor's own
    component wins where it covers the same moment (e.g. text the user asked to change)."""
    if NO_TEXT_REQUEST.search(prompt or ""):
        return [c for c in components if c.get("type") not in {"headline", "title"}]
    written = [c["source"] for c in components if c.get("type") in {"headline", "title"}
               and isinstance(c.get("source"), list)]

    def covered(a: float, b: float) -> bool:
        return any(min(b, y) - max(a, x) > 0.5 * (b - a) for x, y in written)

    out = list(components)
    for entry in analysis.get("text_track") or []:
        a, b = float(entry["start"]), float(entry["end"])
        lines = [str(x) for x in entry.get("lines") or [] if str(x).strip()][:4]
        if not lines or covered(a, b):
            continue
        out.append({"type": "headline", "source": [a, b], "position": entry.get("position") or "bottom",
                    "plate": "none", "from_reference": True,
                    "lines": [{"text": t, "color": "#FFFFFF", "size": "m" if i == 0 else "s"} for i, t in enumerate(lines)]})
    return out


# ── platform marks never become graphics ─────────────────────────────────────────────

PLATFORM = re.compile(r"抖音|快手|小红书|视频号|哔哩|B站|b站|微信|扫一扫|二维码|搜索|抖音号|账号|关注|点赞|"
                      r"tiktok|douyin|kuaishou|bilibili|xiaohongshu|@[\w.]+", re.I)


def platform_tokens(analysis: dict) -> set[str]:
    """Account ids and handles seen in platform overlays or UI-like graphics (e.g. 'bingbing66668888')."""
    texts = [str(o.get("description") or "") for o in analysis.get("platform_overlays") or []]
    texts += [str(g.get("description") or "") for g in analysis.get("graphics") or []
              if str(g.get("type")) in {"logo", "other", "search_bar", "qr", "watermark"}]
    tokens = set()
    for text in texts:
        tokens |= {t.lower() for t in re.findall(r"[A-Za-z0-9_]{6,}", text) if re.search(r"\d", t)}
        tokens |= set(re.findall(r"\d{2,}的[\u4e00-\u9fff]{2,6}", text))
    return tokens


def drop_platform_marks(components: list[dict], analysis: dict) -> list[dict]:
    tokens = platform_tokens(analysis)

    def is_platform(text: str) -> bool:
        low = text.lower()
        return bool(PLATFORM.search(text)) or any(t.lower() in low for t in tokens)

    kept = []
    for comp in components:
        if comp.get("type") == "headline":
            lines = [l for l in comp.get("lines") or [] if not is_platform(str(l.get("text") or ""))]
            if lines:
                kept.append({**comp, "lines": lines})
            continue
        text = " ".join(str(comp.get(k) or "") for k in ("text", "sub", "label"))
        if text.strip() and is_platform(text):
            continue
        kept.append(comp)
    return kept


# ── templates: adapt a finished direction to a new request ───────────────────────────

ADAPT_SYSTEM = """You are the director of a short-video studio reusing a finished production as a template. You get
the template's direction (brief, cast, setting, style, camera setups) and a NEW user request. Change only what the new
request asks for and keep everything else exactly as it is (copy unchanged text verbatim, keep ids). Replacing a
character means redesigning their look while keeping their role and recognisable archetype, unless the request asks
for a different kind of character. New user pictures that show a character become that character's identity
("upload:N"). JSON only."""

ADAPT_USER = """NEW USER REQUEST (authoritative):
{prompt}

NEW USER PICTURES (1-based, may be empty):
{pictures}

TEMPLATE DIRECTION:
{direction}

TEMPLATE SPOKEN LINES AND GRAPHIC TEXTS (for deciding whether the words change):
{texts}

Return ONE JSON object with the same keys as the template direction ("brief", "treatment", "style", "motion_reference",
"audio_strategy", "cast", "setting", "setups"), updated for the new request, plus:
"changes": {{"cast": ["ids of characters whose look or identity changes"], "setting": true|false (the place changes),
  "style": true|false (the overall visual style changes), "script": "keep|rewrite" (rewrite only if the new request
  changes spoken words, on-screen text, product, brand or language), "summary": "Chinese, one sentence: what changes"}}
Keep audio_strategy "reuse" unless the new request changes what is said. Keep setup ids; when the setting, style or a
visible character changes, update that setup's shot text only if its framing must change."""


async def adapt(prompt: str, analysis: dict, template: dict, ratio: str, ledger: llm.Ledger) -> dict:
    script = template.get("script") or {}
    texts = [l.get("text") for seg in script.get("segments") or [] for l in seg.get("lines") or []]
    texts += [line.get("text") for c in script.get("components") or [] for line in c.get("lines") or []]
    texts += [c.get("text") for c in script.get("components") or [] if c.get("text")]
    pictures = "\n".join(f"{p.get('index')}: {p.get('description')}" for p in analysis.get("user_pictures") or []) or "none"
    value = await llm.director(ADAPT_SYSTEM, ADAPT_USER.format(
        prompt=prompt, pictures=pictures, direction=json.dumps(template["direction"], ensure_ascii=False),
        texts=json.dumps([t for t in texts if t][:80], ensure_ascii=False),
    ), ledger, kind="adapt", temperature=0.3)
    changes = value.get("changes") if isinstance(value.get("changes"), dict) else {}
    direction = validate_treatment(value, analysis)
    old = {c["id"]: c for c in template["direction"].get("cast") or []}
    changed_cast = {c for c in changes.get("cast") or [] if c in {x["id"] for x in direction["cast"]}}
    # Trust the evidence as well as the claim: any rewritten casting counts as a change.
    for c in direction["cast"]:
        before = old.get(c["id"])
        if before is None or before.get("person") != c.get("person") or c["identity"].startswith("upload:"):
            changed_cast.add(c["id"])
    setting_changed, style_changed = bool(changes.get("setting")), bool(changes.get("style"))
    # What the director did not change keeps the template's exact words, so prompts match reused pictures.
    if not setting_changed:
        direction["setting"] = template["direction"].get("setting", direction.get("setting"))
    if not style_changed:
        direction["style"] = template["direction"].get("style", direction.get("style"))
    claimed = {c for c in changes.get("cast") or []}
    for c in direction["cast"]:
        before = old.get(c["id"])
        if before is not None and c["id"] not in claimed and not c["identity"].startswith("upload:"):
            c["person"] = before.get("person", c.get("person"))
            changed_cast.discard(c["id"])
    old_setups = {x["id"]: x for x in template["direction"].get("setups") or []}
    for x in direction["setups"]:
        if x["id"] in old_setups and not setting_changed and not style_changed and not set(x["visible"]) & changed_cast:
            x["shot"] = old_setups[x["id"]].get("shot", x.get("shot"))
    direction["changes"] = {
        "cast": sorted(changed_cast),
        "setting": setting_changed,
        "style": style_changed,
        "script": "rewrite" if str(changes.get("script")) == "rewrite" else "keep",
        "summary": str(changes.get("summary") or ""),
    }
    return direction


def reusable_assets(template: dict, direction: dict) -> dict[str, dict]:
    """Template picture/voice nodes the new direction can keep unchanged."""
    changes = direction.get("changes") or {}
    changed_cast = set(changes.get("cast") or [])
    place_changed = changes.get("setting") or changes.get("style")
    old_setups = {s["id"]: s for s in template["direction"].get("setups") or []}
    keep: dict[str, dict] = {}
    for key, node in (template.get("assets") or {}).items():
        if key.startswith("setup-"):
            sid = key[len("setup-"):]
            new = next((s for s in direction["setups"] if s["id"] == sid), None)
            old = old_setups.get(sid)
            if (new and old and not place_changed and not changed_cast & set(new["visible"])
                    and set(new["visible"]) == set(old.get("visible") or []) and new.get("shot") == old.get("shot")):
                keep[key] = node
        elif key.startswith(("broll-", "icon-")):
            if changes.get("script") == "keep":
                keep[key] = node
        elif key.startswith("voice-"):
            cid = key[len("voice-"):]
            before = next((c for c in template["direction"].get("cast") or [] if c["id"] == cid), {})
            after = next((c for c in direction["cast"] if c["id"] == cid), {})
            if after and before.get("voice") == after.get("voice"):
                keep[key] = node
    return keep



# ── graphics the remake may use ──────────────────────────────────────────────────────

REFERENCE_KINDS = {
    "caption": {"captions", "headline"}, "title": {"title", "headline"}, "sticker": {"sticker"}, "reveal": {"reveal_strip"},
    "broll": {"broll"}, "flash": {"flash"}, "logo": {"logo_card"},
}
REQUEST_KINDS = [
    (r"字幕|caption|subtitle", {"captions", "headline"}), (r"标题|title|headline", {"title", "headline"}),
    (r"贴纸|sticker", {"sticker"}), (r"图标|揭晓|icon|reveal", {"reveal_strip"}),
    (r"闪光|flash|特效", {"flash", "zoom_punch"}), (r"音效|sfx|sound effect", {"sfx"}),
    (r"结尾卡|品牌卡|片尾|logo|落版", {"logo_card"}), (r"配图|插图|b-?roll|示意图", {"broll"}),
]


def allowed_graphics(analysis: dict, prompt: str) -> set[str]:
    """Graphic kinds the remake may show: what the reference really uses, plus what the user asks for."""
    allowed: set[str] = set()
    for g in analysis.get("graphics") or []:
        allowed |= REFERENCE_KINDS.get(str(g.get("type") or ""), set())
    for pattern, kinds in REQUEST_KINDS:
        if re.search(pattern, prompt or "", re.I):
            allowed |= kinds
    editing = " ".join(str(v) for v in (analysis.get("style") or {}).values()).lower()
    if re.search(r"zoom|punch|push-?in", editing):
        allowed.add("zoom_punch")
    if allowed & {"sticker", "reveal_strip", "flash", "title", "headline", "logo_card"} and \
            re.search(r"whoosh|swoosh|ding|pop|boom|音效|sound effect", json.dumps(analysis.get("sound") or {}, ensure_ascii=False), re.I):
        allowed.add("sfx")
    return allowed


def keep_reference_graphics(script: dict, analysis: dict, prompt: str) -> dict:
    allowed = allowed_graphics(analysis, prompt)
    script["components"] = [c for c in script.get("components") or [] if c.get("type") in allowed]
    captions = script.get("captions") if isinstance(script.get("captions"), dict) else {}
    if "captions" not in allowed:
        captions["enabled"] = False
    script["captions"] = captions
    return script
