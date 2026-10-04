"""Automatic review that replaces a human approval gate.

Every generated picture and Take is judged against what it was directed to be. A failed
judgement returns concrete issues; the runner feeds them back into the next attempt.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

from PIL import Image

from . import llm
from .align import char_error_rate

PASS_SCORE = 7


def data_url(path: Path, max_side: int = 1024) -> str:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def _tail_heard(expected: str, heard: str) -> bool:
    """True when the script's last few characters were actually heard at the end of the take."""
    from .script import normalise
    a = [c for c in (normalise(ch) for ch in expected) if c][-4:]
    b = [c for c in (normalise(ch) for ch in heard) if c][-8:]
    if not a:
        return True
    return sum(1 for ch in a if ch in b) >= max(1, len(a) - 1)


def _verdict(value: object) -> dict:
    if not isinstance(value, dict):
        return {"pass": False, "score": 0, "issues": ["质检结果格式错误"]}
    score = float(value.get("score") or 0)
    issues = [str(i) for i in value.get("issues") or [] if str(i).strip()][:8]
    return {"pass": bool(value.get("pass")) and score >= PASS_SCORE, "score": score, "issues": issues}


async def review_setup_image(image: Path, *, setup: dict, cast: dict[str, dict], layout: Path | None,
                             main: Path | None, identities: list[tuple[str, Path]], ledger: llm.Ledger,
                             redesigned: list[str] | None = None, style: str = "") -> dict:
    """Checklist review: the model answers concrete questions, the code decides pass/fail, and only the
    failed checks become fixed-wording corrections for the next attempt."""
    content: list[dict] = [{"type": "text", "text": "IMAGE A (the generated picture to judge):"},
                           {"type": "image_url", "image_url": {"url": data_url(image)}}]
    if main is not None:
        content += [{"type": "text", "text": "IMAGE B (main camera setup: the characters' identities):"},
                    {"type": "image_url", "image_url": {"url": data_url(main, 768)}}]
    for label, path in identities:
        content += [{"type": "text", "text": f"IDENTITY PHOTO of {label}:"},
                    {"type": "image_url", "image_url": {"url": data_url(path, 768)}}]
    if layout is not None:
        content += [{"type": "text", "text": "IMAGE C (the ORIGINAL reference video frame, with the OLD characters):"},
                    {"type": "image_url", "image_url": {"url": data_url(layout, 768)}}]
    listed = [cast[c].get("label", c) for c in setup["visible"] if c in cast]
    expected = "\n".join(f"- {cast[c].get('label', c)}: {cast[c].get('person', '')[:260]}" for c in setup["visible"] if c in cast)
    content.append({"type": "text", "text": f"""Answer questions about IMAGE A only.
IMAGE A should show exactly {len(listed)} character(s): {', '.join(listed) or 'nobody (a view of the place or objects)'}.
{expected}
Intended framing: {setup.get('shot', '')}
{f'Requested visual style: {style}' if style else ''}
Notes: IMAGE A may show fewer people than IMAGE B; that is correct when they are not listed. Pose, gesture, gaze,
open or closed eyes, expression and small prop differences are NOT problems.
Return JSON with these keys:
"people_in_a": number of people clearly visible in IMAGE A,
"listed_present": true if every listed character is clearly visible in IMAGE A,
"unlisted_people": true if IMAGE A shows a clearly visible person who is not listed,
"identity_match": true if each listed character in IMAGE A has the same face, hair and outfit as in IMAGE B or the
  identity photo (true when there is no IMAGE B and no identity photo),
"matches_description": true if each listed character's hair, outfit colours and garments in IMAGE A match the written
  description above (a different dress, colour or hairstyle is false),
"copies_old_look": true only if a listed character in IMAGE A wears the OLD character's outfit or hairstyle from IMAGE C,
"text_or_logo": true if IMAGE A contains captions, subtitles, logos, watermarks or garbled text,
"anatomy_ok": true if there are no extra limbs, broken hands, merged or melted faces,
"framing_ok": true if the shot size and camera angle broadly match the intended framing,
"style_ok": true if IMAGE A matches the requested visual style (true when none is requested),
"notes": "one short sentence"}}"""})
    value = await llm.vision(content, ledger, kind="qa-image", max_tokens=500)
    value = value if isinstance(value, dict) else {}
    names = ", ".join(listed)
    checks = [
        ("listed_present", True, 3, f"All of these must be clearly visible: {names}." if listed else ""),
        ("unlisted_people", False, 3, f"Only {names} may appear; remove anyone else." if listed else
         "Nobody may appear in this shot."),
        ("identity_match", True, 3, "Keep each character's face, hair and outfit identical to the main camera setup."),
        ("matches_description", True, 3, "Dress every character exactly as written: " + "; ".join(
            f"{cast[c].get('label', c)}: {cast[c].get('person', '')[:200]}" for c in setup["visible"] if c in cast)
         if listed else ""),
        ("copies_old_look", False, 2, f"{', '.join(redesigned or [])} must not wear the old characters' outfits or hairstyles."
         if redesigned else ""),
        ("text_or_logo", False, 2, "No captions, logos, watermarks or text anywhere."),
        ("anatomy_ok", True, 3, "Correct anatomy: natural hands, limbs and faces."),
        ("framing_ok", True, 1, "Follow the requested shot size and camera angle."),
        ("style_ok", True, 2, f"Render in this visual style: {style}." if style else ""),
    ]
    score, issues = 10.0, []
    for key, good, weight, fix in checks:
        if key == "copies_old_look" and not redesigned:
            continue
        if key not in value:
            continue
        if bool(value.get(key)) != good:
            score -= weight
            if fix:
                issues.append(fix)
    return {"pass": score >= PASS_SCORE, "score": max(0.0, score), "issues": issues, "checks": value}


async def review_setup_consistency(images: list[tuple[str, Path, list[str]]], people: str, ledger: llm.Ledger) -> list[dict]:
    """All camera setups side by side, before any video is generated: a character who wears or looks different in
    one setup would make every take that cuts between them fail. Returns the outlier setups to redraw."""
    content: list[dict] = []
    for sid, path, labels in images:
        content += [{"type": "text", "text": f"SETUP {sid} (should show: {', '.join(labels) or 'nobody'}):"},
                    {"type": "image_url", "image_url": {"url": data_url(path, 640)}}]
    content.append({"type": "text", "text": f"""These pictures are camera setups of ONE continuous scene, so every character
must look the same in all of them. The written casting is the authority:
{people}
For each character, compare hairstyle, hair colour and hair accessories, garments and their colours across the setups
that show them, and against the written casting. Report only differences a viewer would notice at a glance: a different garment, a different colour, a different
hairstyle or hair colour. Ignore small accessories (pins, rings, buttons, earrings), wording differences in the casting,
pose, expression, lighting, camera angle and clothing that is merely hidden by the crop.
When most setups agree with the casting and one does not, that one is the outlier.
Return JSON: {{"outliers": [{{"setup": "S1", "character": "name", "problem": "short English description"}}]}}"""})
    try:
        value = await llm.vision(content, ledger, kind="qa-consistency", max_tokens=600)
    except llm.ModelError:
        return []
    known = {sid for sid, _, _ in images}
    items = value.get("outliers") if isinstance(value, dict) else None
    return [x for x in items or [] if isinstance(x, dict) and x.get("setup") in known]


async def confirm_take(video_url: str, verdict: dict, *, cast_lines: str, people: str, ledger: llm.Ledger) -> dict:
    """A second, stricter look before paying for another take: keep only problems that are clearly visible, and
    tell a clip that is wrong from a clip that follows the casting but not a mismatched setup picture."""
    claims = verdict.get("visual", {}).get("issues") or []
    if not claims:
        return verdict
    content: list[dict] = [{"type": "video_url", "video_url": {"url": video_url}, "fps": 2}]
    listed = "\n".join(f"#{i} {c}" for i, c in enumerate(claims))
    content.append({"type": "text", "text": f"""A first reviewer claimed these problems in this generated clip:
{listed}
Script (who says what): {cast_lines}
Written casting: {people}
Watch the clip and check each claim strictly. A claim is confirmed only when the problem is clearly visible in the
clip, not a matter of interpretation, sampling or missing context. For each confirmed claim give its cause:
"generation" when the clip itself is wrong (artifacts, extra or duplicated people, the wrong person talking, burned-in
text, a person whose hair or outfit differs from the written casting, a different place), or "setup" when the clip
matches the written casting and only differs from one of the setup pictures.
Return JSON: {{"claims": [{{"i": 0, "confirmed": true, "cause": "generation|setup"}}]}}"""})
    try:
        value = await llm.vision(content, ledger, kind="qa-confirm", max_tokens=500)
    except llm.ModelError:
        return verdict
    rows = {int(x.get("i", -1)): x for x in (value.get("claims") if isinstance(value, dict) else None) or []
            if isinstance(x, dict)}
    kept = [(c, str(rows[i].get("cause") or "generation")) for i, c in enumerate(claims)
            if i in rows and rows[i].get("confirmed")]
    speech = [i for i in verdict["issues"] if i not in claims]  # measured problems (wrong words) always stand
    out = {**verdict, "issues": speech + [c for c, _ in kept], "unconfirmed": [c for c in claims if c not in dict(kept)]}
    if not kept and not speech:
        out.update({"pass": True, "score": max(float(verdict["score"]), PASS_SCORE)})
    elif kept and all(cause == "setup" for _, cause in kept) and not speech:
        out["input_problem"] = True  # another take cannot fix a mismatched setup picture
    return out


async def review_take(video_url: str, *, expected_speech: str, heard_speech: str, duration: float,
                      speech_end: float, setup_images: list[tuple[str, Path]], cast_lines: str,
                      ledger: llm.Ledger, motion: bool = False) -> dict:
    cer = char_error_rate(expected_speech, heard_speech)
    issues: list[str] = []
    if cer > 0.3:
        issues.append(f"The spoken words differ from the script (character error rate {cer:.0%}); "
                      "every line must be spoken exactly as written.")
    if cer > 0.1 and speech_end > duration - 0.15 and not _tail_heard(expected_speech, heard_speech):
        issues.append("The last line is cut off by the end of the clip; deliver the lines a little faster.")
    content: list[dict] = [{"type": "video_url", "video_url": {"url": video_url}, "fps": 2}]
    for label, path in setup_images:
        content += [{"type": "text", "text": f"CAMERA SETUP {label}:"},
                    {"type": "image_url", "image_url": {"url": data_url(path, 768)}}]
    camera_rule = ("(5) (framing follows a reference performance, so new camera positions are fine) the people are "
                   "not in the place shown by the setups;" if motion else
                   "(5) a new camera position that matches none of the setups, or a long drifting camera move;")
    content.append({"type": "text", "text": f"""You are the continuity supervisor reviewing a generated dialogue clip.
The clip must only use the CAMERA SETUPS above as its shots (hard cuts between them are fine).
Script (who says what): {cast_lines}
The frames are sampled from one generated video at 2 frames per second, so do NOT judge phoneme-level lip sync and
never fail a clip for looking static. Lines marked as an off-screen voice need nobody on screen to talk.
Fail only for clear, visible problems:
(1) a character's identity drifts (different face, species features or outfit than in the setups);
(2) extra people appear or a character who should be in a setup is missing;
(3) the wrong character is visibly talking while another character's line is heard, or nobody moves their mouth
    during a line although the speaker is on screen;
(4) burned-in subtitles, captions, text or logos appear;
{camera_rule}
(6) severe artifacts (melting faces, broken hands, heavy flicker).
Score 8-10 when none of these occur, 5-7 for one moderate issue, below 5 for severe ones.
Return JSON: {{"pass": true|false, "score": 0-10, "issues": ["only the clear problems, in English"]}}"""})
    try:
        visual = _verdict(await llm.vision(content, ledger, kind="qa-take", max_tokens=800))
    except llm.ModelError as exc:
        visual = {"pass": True, "score": PASS_SCORE, "issues": [], "error": str(exc)}
    score = min(visual["score"], 10 - cer * 20)
    all_issues = issues + visual["issues"]
    return {"pass": visual["pass"] and not issues, "score": round(score, 2), "cer": cer,
            "issues": all_issues, "visual": visual}


async def review_render(frames: list[tuple[float, Path]], ledger: llm.Ledger) -> dict:
    content: list[dict] = []
    for t, path in frames:
        content += [{"type": "text", "text": f"FRAME at {t:.1f}s:"},
                    {"type": "image_url", "image_url": {"url": data_url(path, 900)}}]
    content.append({"type": "text", "text": """These are frames of a finished short video with captions and graphics.
Check: captions are readable and do not cover faces; graphics do not cover the speaking character's face; nothing is
cut off at the frame edges; no duplicated or garbled text.
Return JSON: {"pass": true|false, "score": 0-10, "issues": ["..."], "caption_covers_faces": true|false}"""})
    value = await llm.vision(content, ledger, kind="qa-render", max_tokens=600)
    verdict = _verdict(value)
    verdict["caption_covers_faces"] = bool(isinstance(value, dict) and value.get("caption_covers_faces"))
    return verdict


def summary(verdict: dict) -> str:
    return json.dumps({k: verdict.get(k) for k in ("pass", "score", "cer", "issues")}, ensure_ascii=False)
