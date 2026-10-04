"""Word timing: DashScope ASR output -> timed characters -> Script atom times.

Measured delivery establishes time. Each Take's ASR words are matched against the
Segment's spoken Script text; atoms between matched characters are interpolated, so
a number written as digits but spoken as words still lands at a sensible time.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from .script import Line, normalise


def asr_sentences(transcript: dict) -> list[dict]:
    """Normalised sentences [{start, end, text, emotion, words:[{start,end,text}]}] in seconds."""
    items = transcript.get("transcripts") or []
    if isinstance(items, dict):
        items = [items]
    sentences = []
    for item in items:
        for sentence in item.get("sentences") or []:
            words = [
                {"start": float(w.get("begin_time") or 0) / 1000, "end": float(w.get("end_time") or 0) / 1000,
                 "text": str(w.get("text") or "") + str(w.get("punctuation") or "")}
                for w in sentence.get("words") or []
            ]
            sentences.append({
                "start": float(sentence.get("begin_time") or 0) / 1000,
                "end": float(sentence.get("end_time") or 0) / 1000,
                "text": str(sentence.get("text") or ""),
                "emotion": sentence.get("emotion"),
                "words": words,
            })
    return sentences


def timed_chars(sentences: list[dict]) -> list[tuple[str, float, float]]:
    """Every comparable character with its (start, end); Latin words are spread evenly."""
    out: list[tuple[str, float, float]] = []
    for sentence in sentences:
        for word in sentence["words"]:
            chars = [normalise(ch) for ch in word["text"]]
            chars = [c for c in chars if c]
            if not chars:
                continue
            span = max(0.0, word["end"] - word["start"])
            step = span / len(chars)
            for i, ch in enumerate(chars):
                out.append((ch, word["start"] + i * step, word["start"] + (i + 1) * step))
    return out


def transcript_text(sentences: list[dict]) -> str:
    return "".join(s["text"] for s in sentences)


def char_error_rate(expected: str, heard: str) -> float:
    a = [c for c in (normalise(ch) for ch in expected) if c]
    b = [c for c in (normalise(ch) for ch in heard) if c]
    if not a:
        return 0.0 if not b else 1.0
    matched = sum(block.size for block in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    return round(1 - matched / max(len(a), 1), 3)


def align_lines(lines: list[Line], chars: list[tuple[str, float, float]], duration: float) -> list[list[tuple[float, float]]]:
    """Return per line, per atom (start, end) seconds within the Take."""
    flat: list[tuple[int, int, str]] = []  # (line, atom, normalised char)
    for li, line in enumerate(lines):
        for ai, atom in enumerate(line.atoms):
            for ch in atom.speech:
                n = normalise(ch)
                if n:
                    flat.append((li, ai, n))
    script = [c for _, _, c in flat]
    heard = [c for c, _, _ in chars]
    times: list[tuple[float, float] | None] = [None] * len(flat)
    for block in SequenceMatcher(None, script, heard, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            _, s, e = chars[block.b + k]
            times[block.a + k] = (s, e)
    # Interpolate unmatched spans between their matched neighbours.
    speech_start = chars[0][1] if chars else 0.3
    speech_end = chars[-1][2] if chars else max(0.5, duration - 0.3)
    i = 0
    while i < len(times):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(times) and times[j] is None:
            j += 1
        left = times[i - 1][1] if i > 0 else speech_start
        right = times[j][0] if j < len(times) else speech_end
        right = max(right, left)
        step = (right - left) / (j - i)
        for k in range(i, j):
            times[k] = (left + (k - i) * step, left + (k - i + 1) * step)
        i = j
    result: list[list[tuple[float, float] | None]] = [[None] * len(line.atoms) for line in lines]
    for (li, ai, _), t in zip(flat, times):
        current = result[li][ai]
        result[li][ai] = t if current is None else (current[0], t[1])
    # Atoms without speech (punctuation, spaces) inherit the previous atom's end.
    for li, line in enumerate(lines):
        previous = result[li - 1][-1][1] if li and result[li - 1] and result[li - 1][-1] else speech_start
        for ai in range(len(line.atoms)):
            if result[li][ai] is None:
                result[li][ai] = (previous, previous)
            previous = result[li][ai][1]
    return result  # type: ignore[return-value]
