"""Composition: Takes on one Timeline, word-bound Caption and MG components, sound, encode.

Every graphic is placed from Script Moments and Selections resolved through the aligned
words of the actual performance, so a different delivery moves the graphics with it.
Components are drawn per frame with Pillow and overlaid by ffmpeg; the picture itself
can take a zoom punch on a Moment.
"""
from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from .media import probe, run
from .script import Line, cue_ranges, cue_text

FPS = 30
FONT_CANDIDATES = [
    os.getenv("H3_REENACT_FONT", ""),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def canvas_size(ratio: str) -> tuple[int, int]:
    return (1280, 720) if ratio == "16:9" else (720, 1280)


@lru_cache(maxsize=64)
def font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if path and Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def hex_rgb(value: str | None, default: tuple[int, int, int]) -> tuple[int, int, int]:
    text = str(value or "").lstrip("#")
    if len(text) >= 6:
        try:
            return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            pass
    return default


def ease_out_back(x: float) -> float:
    x = max(0.0, min(1.0, x))
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def ease_out(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


# ── Timeline ────────────────────────────────────────────────────────────────────────

@dataclass
class TakeClip:
    path: Path
    trim_start: float
    trim_end: float
    offset: float = 0.0

    @property
    def length(self) -> float:
        return self.trim_end - self.trim_start


@dataclass
class Cue:
    start: float
    end: float
    text: str
    role: str


@dataclass
class Timeline:
    duration: float
    cues: list[Cue] = field(default_factory=list)
    moments: dict[str, float] = field(default_factory=dict)
    selections: dict[str, tuple[float, float]] = field(default_factory=dict)
    segment_spans: list[tuple[float, float]] = field(default_factory=list)
    source_ranges: list[tuple[float, float]] = field(default_factory=list)

    def from_source(self, t: float) -> float:
        """Map a reference-time position onto the new Timeline through the passage it belongs to."""
        for (a, b), (x, y) in zip(self.source_ranges, self.segment_spans):
            if a <= t <= b:
                return round(x + (t - a) * ((y - x) / max(b - a, 1e-6)), 3)
        if self.source_ranges and t > self.source_ranges[-1][1]:
            return self.duration
        return max(0.0, min(t, self.duration))


def plan_trims(duration: float, first_word: float, last_word: float,
               cuts: list[float] | None = None) -> tuple[float, float]:
    """Keep the performance with a little air, but never a sliver of another shot: a cut the model made
    just before the first word or just after the last one becomes the Take's edge."""
    start = max(0.0, first_word - 0.35) if first_word > 0.9 else 0.0
    end = min(duration, last_word + 0.6) if last_word > 0 else duration
    for cut in cuts or []:
        if start < cut <= first_word and first_word - cut < 1.2:
            start = cut
        if last_word <= cut < end:
            end = cut
    return round(start, 3), round(max(end, start + 1.0), 3)


def build_timeline(takes: list[TakeClip], lines_per_take: list[list[Line]],
                   times_per_take: list[list[list[tuple[float, float]]]]) -> Timeline:
    offset = 0.0
    tl = Timeline(duration=0.0)
    for clip, lines, times in zip(takes, lines_per_take, times_per_take):
        clip.offset = offset

        def g(t: float) -> float:
            return round(offset + min(max(t - clip.trim_start, 0.0), clip.length), 3)

        tl.segment_spans.append((offset, offset + clip.length))
        for line, atom_times in zip(lines, times):
            for start, end in cue_ranges(line):
                text = cue_text(line, start, end)
                if text:
                    tl.cues.append(Cue(g(atom_times[start][0]), g(atom_times[end - 1][1]), text, line.role))
            for name, index in line.moments.items():
                index = min(index, len(atom_times) - 1)
                tl.moments[name] = g(atom_times[index][0])
            for name, (a, b) in line.selections.items():
                a, b = min(a, len(atom_times) - 1), max(min(b, len(atom_times)) - 1, 0)
                tl.selections[name] = (g(atom_times[a][0]), g(atom_times[b][1]))
        offset += clip.length
    tl.duration = round(offset, 3)
    # Hold each Cue until the next begins (short gaps), with a readable minimum.
    tl.cues.sort(key=lambda c: c.start)
    for i, cue in enumerate(tl.cues):
        nxt = tl.cues[i + 1].start if i + 1 < len(tl.cues) else tl.duration
        if nxt - cue.end < 0.45:
            cue.end = nxt
        cue.end = max(cue.end + 0.12, min(cue.start + 0.6, nxt)) if cue.end - cue.start < 0.6 else min(cue.end + 0.12, nxt)
    return tl


def assemble(takes: list[TakeClip], target: Path, ratio: str,
             audio_source: tuple[Path, float, float] | None = None, layout: dict | None = None) -> Path:
    """Concatenate trimmed Takes; with audio_source, the reference soundtrack (path, start, end) replaces theirs.
    A panel layout places the camera picture in its band of the frame over the panel colour."""
    width, height = canvas_size(ratio)
    inputs, filters = [], []
    if layout and layout.get("type") == "panel":
        top = int(round(height * layout["top"] / 2) * 2)
        rh = int(round(height * (layout["bottom"] - layout["top"]) / 2) * 2)
        place = (f"scale={width}:{rh}:force_original_aspect_ratio=increase,crop={width}:{rh},"
                 f"pad={width}:{height}:0:{top}:color={layout.get('color', '#606060')},setsar=1")
    else:
        place = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
    for i, clip in enumerate(takes):
        inputs += ["-i", str(clip.path)]
        filters.append(
            f"[{i}:v:0]trim=start={clip.trim_start:.3f}:end={clip.trim_end:.3f},setpts=PTS-STARTPTS,fps={FPS},"
            f"{place}[v{i}]")
        filters.append(
            f"[{i}:a:0]atrim=start={clip.trim_start:.3f}:end={clip.trim_end:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo[a{i}]")
    if audio_source is None:
        labels = "".join(f"[v{i}][a{i}]" for i in range(len(takes)))
        filters.append(f"{labels}concat=n={len(takes)}:v=1:a=1[v][a]")
    else:
        filters = [f for f in filters if not f.startswith(tuple(f"[{i}:a:0]" for i in range(len(takes))))]
        path, start, end = audio_source
        inputs += ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(path)]
        filters.append("".join(f"[v{i}]" for i in range(len(takes))) + f"concat=n={len(takes)}:v=1:a=0[v]")
        filters.append(f"[{len(takes)}:a:0]aresample=48000,aformat=channel_layouts=stereo[a]")
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(filters),
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
         "-c:a", "pcm_s16le", "-shortest", str(target.with_suffix(".mkv"))], 1200)
    return target.with_suffix(".mkv")


# ── sound ───────────────────────────────────────────────────────────────────────────

SFX = {
    "pop": ("0.9*sin(2*PI*(700-2600*t)*t)*exp(-28*t)", 0.18),
    "ding": ("0.5*(sin(2*PI*1318*t)+0.6*sin(2*PI*1976*t))*exp(-6*t)", 0.9),
    "cash": ("0.45*(sin(2*PI*1568*t)+sin(2*PI*2093*t)*gte(t,0.09))*exp(-7*t)", 0.8),
    "whoosh": ("0.5*(random(0)*2-1)*sin(PI*t/0.45)", 0.45),
    "boom": ("0.95*sin(2*PI*(90-60*t)*t)*exp(-5*t)+0.2*(random(0)*2-1)*exp(-18*t)", 0.9),
    "riser": ("0.35*sin(2*PI*(300+900*t)*t)*(t/1.0)", 1.0),
}


def sfx_file(kind: str, work: Path) -> Path:
    expr, seconds = SFX.get(kind, SFX["pop"])
    target = work / f"sfx-{kind}.wav"
    if not target.is_file():
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
             f"aevalsrc='{expr}':s=48000:d={seconds}", "-af", "afade=t=out:st=%.2f:d=0.05" % max(0, seconds - 0.05),
             "-ac", "2", str(target)], 60)
    return target


def mix_audio(base: Path, target: Path, hits: list[tuple[float, str]], music: Path | None,
              duration: float, work: Path, music_gain: float = 0.13) -> Path:
    inputs = ["-i", str(base)]
    filters = ["[0:a]aformat=channel_layouts=stereo,aresample=48000[voice]"]
    mix = ["[voice]"]
    n = 1
    for t, kind in hits:
        inputs += ["-i", str(sfx_file(kind, work))]
        delay = int(max(0.0, t) * 1000)
        filters.append(f"[{n}:a]adelay={delay}|{delay},volume=0.55[s{n}]")
        mix.append(f"[s{n}]")
        n += 1
    if music is not None:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        filters.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{duration:.3f},"
                       f"volume={music_gain},afade=t=in:d=0.4,afade=t=out:st={max(0, duration - 0.8):.2f}:d=0.8[bgm]")
        filters.append("[bgm][voice]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=300[duck]")
        mix.append("[duck]")
    filters.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first,alimiter=limit=0.95[out]")
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(filters),
         "-map", "[out]", "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", str(target)], 600)
    return target


# ── components ──────────────────────────────────────────────────────────────────────

class Layer:
    z = 50

    def active(self, t: float) -> bool:
        raise NotImplementedError

    def draw(self, frame: Image.Image, t: float) -> None:
        raise NotImplementedError


def text_block(text: str, size: int, fill: tuple, stroke: tuple, max_width: int) -> Image.Image:
    f = font(size)
    words = list(text) if any("㐀" <= ch <= "鿿" for ch in text) else text.split(" ")
    joiner = "" if words and len(words) == len(text) else " "
    lines, current = [], ""
    for w in words:
        trial = (current + joiner + w) if current else w
        if f.getlength(trial) > max_width and current:
            lines.append(current)
            current = w
        else:
            current = trial
    if current:
        lines.append(current)
    lines = lines[:3]
    stroke_w = max(2, size // 11)
    line_h = int(size * 1.18)
    width = int(max(f.getlength(l) for l in lines)) + stroke_w * 2 + 8
    height = line_h * len(lines) + stroke_w * 2 + 8
    pad = size // 2
    image = Image.new("RGBA", (width + pad * 2, height + pad * 2), (0, 0, 0, 0))
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    d = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        x = pad + (width - f.getlength(line)) / 2
        y = pad + i * line_h
        sd.text((x + size * 0.06, y + size * 0.09), line, font=f, fill=(0, 0, 0, 200),
                stroke_width=stroke_w, stroke_fill=(0, 0, 0, 200))
        d.text((x, y), line, font=f, fill=fill + (255,), stroke_width=stroke_w, stroke_fill=stroke + (255,))
    shadow = shadow.filter(ImageFilter.GaussianBlur(size / 14))
    return Image.alpha_composite(shadow, image)


def paste_scaled(frame: Image.Image, image: Image.Image, cx: float, cy: float, scale: float,
                 alpha: float = 1.0, angle: float = 0.0) -> None:
    if scale <= 0.01 or alpha <= 0.01:
        return
    w, h = max(1, int(image.width * scale)), max(1, int(image.height * scale))
    img = image.resize((w, h), Image.BICUBIC) if (w, h) != image.size else image
    if angle:
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True)
    if alpha < 1:
        img = img.copy()
        img.putalpha(img.getchannel("A").point(lambda v: int(v * alpha)))
    frame.alpha_composite(img, (int(cx - img.width / 2), int(cy - img.height / 2)))


class CaptionLayer(Layer):
    z = 70

    def __init__(self, cues: list[Cue], styles: dict, canvas: tuple[int, int], position: str, size: str,
                 animation: str, uppercase: bool):
        self.cues = cues
        self.canvas = canvas
        short = min(canvas)
        self.size = int(short * {"s": 0.056, "m": 0.068, "l": 0.082}.get(size, 0.062))
        self.y = canvas[1] * (0.5 if position == "middle" else (0.86 if canvas[0] > canvas[1] else 0.72))
        self.animation = animation
        self.uppercase = uppercase
        self.styles = styles
        self.cache: dict[int, Image.Image] = {}

    def image(self, i: int) -> Image.Image:
        if i not in self.cache:
            cue = self.cues[i]
            style = self.styles.get(cue.role) or {}
            text = cue.text.upper() if self.uppercase else cue.text
            self.cache[i] = text_block(text, self.size, hex_rgb(style.get("fill"), (255, 255, 255)),
                                       hex_rgb(style.get("stroke"), (20, 16, 12)), int(self.canvas[0] * 0.86))
        return self.cache[i]

    def active(self, t: float) -> bool:
        return any(c.start <= t < c.end for c in self.cues)

    def draw(self, frame: Image.Image, t: float) -> None:
        for i, cue in enumerate(self.cues):
            if cue.start <= t < cue.end:
                age = t - cue.start
                scale = 1.0
                alpha = 1.0
                if self.animation == "pop":
                    scale = 0.78 + 0.22 * ease_out_back(age / 0.16)
                elif self.animation == "fade":
                    alpha = min(1.0, age / 0.12)
                paste_scaled(frame, self.image(i), self.canvas[0] / 2, self.y, scale, alpha)


class FlashLayer(Layer):
    z = 80

    def __init__(self, at: float, color: tuple, canvas: tuple[int, int], intensity: float = 0.3):
        self.at, self.color, self.canvas, self.intensity = at, color, canvas, intensity

    def active(self, t: float) -> bool:
        return self.at <= t < self.at + 8 / FPS

    def draw(self, frame: Image.Image, t: float) -> None:
        k = (t - self.at) * FPS
        level = k / 2 if k < 2 else (1.0 if k < 3 else max(0.0, 1 - (k - 3) / 5))
        overlay = Image.new("RGBA", self.canvas, self.color + (int(255 * self.intensity * level),))
        frame.alpha_composite(overlay)


def rounded(image: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, image.width - 1, image.height - 1), radius, fill=255)
    out = image.convert("RGBA")
    out.putalpha(ImageChops.multiply(out.getchannel("A"), mask))
    return out


def card(image: Image.Image, box: tuple[int, int], border: int = 8, radius: int = 18) -> Image.Image:
    fitted = ImageOps.fit(image.convert("RGB"), (box[0] - border * 2, box[1] - border * 2), Image.LANCZOS)
    inner = rounded(fitted, max(4, radius - border))
    base = Image.new("RGBA", box, (0, 0, 0, 0))
    ImageDraw.Draw(base).rounded_rectangle((0, 0, box[0] - 1, box[1] - 1), radius, fill=(255, 255, 255, 255))
    base.alpha_composite(inner, (border, border))
    pad = 24
    shadowed = Image.new("RGBA", (box[0] + pad * 2, box[1] + pad * 2), (0, 0, 0, 0))
    shadow = Image.new("RGBA", shadowed.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((pad, pad + 8, pad + box[0], pad + box[1] + 8), radius, fill=(0, 0, 0, 150))
    shadowed.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)))
    shadowed.alpha_composite(base, (pad, pad))
    return shadowed


class BRollLayer(Layer):
    z = 40

    def __init__(self, image: Image.Image, start: float, end: float, canvas: tuple[int, int], layout: str, index: int,
                 position: str = ""):
        self.start, self.end, self.canvas, self.layout = start, max(end, start + 0.8), canvas, layout
        w, h = canvas
        landscape = w > h
        if layout == "half":
            layout = "card"
            self.layout = "card"
        if layout == "full":
            self.full = ImageOps.fit(image.convert("RGB"), (int(w * 1.08), int(h * 1.08)), Image.LANCZOS).convert("RGBA")
            self.card = None
        else:
            box = (int(w * (0.34 if landscape else 0.62)), int(h * (0.5 if landscape else 0.3)))
            if layout == "pip":
                box = (int(box[0] * 0.7), int(box[1] * 0.7))
            self.card = card(image, box)
            self.full = None
        side = 1 if index % 2 == 0 else -1
        if landscape:
            self.cx = w * (0.76 if side > 0 else 0.24)
            self.cy = h * 0.38
        else:
            self.cx = w * 0.5 + side * w * 0.04
            self.cy = h * 0.3
        self.angle = 3.0 * side
        if position in {"top", "middle", "bottom"} and self.card is not None:
            self.cx = w / 2
            self.cy = {"top": h * 0.3, "middle": h * 0.5, "bottom": h * 0.74}[position]
            self.angle = 0.0

    def active(self, t: float) -> bool:
        return self.start <= t < self.end + 0.15

    def draw(self, frame: Image.Image, t: float) -> None:
        age, left = t - self.start, self.end + 0.15 - t
        alpha = min(1.0, left / 0.15) if left < 0.15 else 1.0
        if self.full is not None:
            k = (t - self.start) / max(self.end - self.start, 0.1)
            zoom = 1.0 + 0.06 * k
            img = self.full
            w, h = self.canvas
            crop_w, crop_h = int(img.width / zoom / 1.08 * 1.0), int(img.height / zoom / 1.08)
            x0, y0 = (img.width - crop_w) // 2, (img.height - crop_h) // 2
            view = img.crop((x0, y0, x0 + crop_w, y0 + crop_h)).resize((w, h), Image.BILINEAR)
            paste_scaled(frame, view, w / 2, h / 2, 1.0, alpha * min(1.0, age / 0.12))
            return
        scale = 0.82 + 0.18 * ease_out_back(age / 0.22)
        paste_scaled(frame, self.card, self.cx, self.cy, scale, alpha, self.angle)


class TitleLayer(Layer):
    z = 60

    def __init__(self, text: str, start: float, end: float, canvas: tuple[int, int], accent: tuple):
        self.start, self.end, self.canvas = start, max(end, start + 1.2), canvas
        size = int(min(canvas) * 0.058)
        f = font(size)
        width = min(int(f.getlength(text)) + size * 2, int(canvas[0] * 0.92))
        height = int(size * 1.9)
        plate = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        d = ImageDraw.Draw(plate)
        d.rounded_rectangle((0, 0, width - 1, height - 1), height // 3, fill=accent + (235,))
        d.text((width / 2, height / 2), text, font=f, anchor="mm", fill=(255, 255, 255, 255),
               stroke_width=max(1, size // 16), stroke_fill=(0, 0, 0, 160))
        self.plate = plate
        self.y = canvas[1] * (0.075 if canvas[0] > canvas[1] else 0.09) + height / 2

    def active(self, t: float) -> bool:
        return self.start <= t < self.end + 0.2

    def draw(self, frame: Image.Image, t: float) -> None:
        age, left = t - self.start, self.end + 0.2 - t
        dy = (1 - ease_out(age / 0.25)) * -self.plate.height * 1.5
        alpha = min(1.0, left / 0.2)
        paste_scaled(frame, self.plate, self.canvas[0] / 2, self.y + dy, 1.0, alpha)


class HeadlineLayer(Layer):
    """Designed multi-line on-screen text (titles, changing exercise headlines), each line its own colour."""
    z = 60

    def __init__(self, lines: list[dict], start: float, end: float, canvas: tuple[int, int], position: str, plate: str,
                 layout: dict | None = None):
        self.start, self.end, self.canvas = start, max(end, start + 0.4), canvas
        short = min(canvas)
        blocks = []
        for line in lines[:5]:
            size = int(short * {"l": 0.085, "m": 0.062, "s": 0.045}.get(str(line.get("size") or "m"), 0.062))
            blocks.append(text_block(str(line.get("text") or "")[:90], size, hex_rgb(line.get("color"), (255, 255, 255)),
                                     (20, 20, 20), int(canvas[0] * 0.94)))
        width = max(b.width for b in blocks)
        height = sum(int(b.height * 0.7) for b in blocks) + int(blocks[-1].height * 0.3)
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        if plate in {"dark", "light"}:
            ImageDraw.Draw(image).rounded_rectangle((width * 0.04, blocks[0].height * 0.18, width * 0.96, height - 4), 18,
                                                    fill=(0, 0, 0, 150) if plate == "dark" else (255, 255, 255, 190))
        y = 0
        for b in blocks:
            image.alpha_composite(b, ((width - b.width) // 2, y))
            y += int(b.height * 0.7)
        self.image = image
        self.position, self.placed = position, False
        H = canvas[1]
        if layout and layout.get("type") == "panel" and layout["top"] > 0.12:
            # The reference keeps its words on the panel above the picture.
            self.y = {"top": H * 0.03 + height / 2, "middle": H * 0.03 + height / 2,
                      "bottom": (H * layout["bottom"] + H) / 2}.get(position, H * 0.03 + height / 2)
        else:
            self.y = {"top": H * 0.04 + height / 2, "middle": H / 2,
                      "bottom": H * 0.8}.get(position, H * 0.04 + height / 2)

    def active(self, t: float) -> bool:
        return self.start <= t < self.end

    def draw(self, frame: Image.Image, t: float) -> None:
        age = t - self.start
        scale = 0.9 + 0.1 * ease_out(age / 0.15) if age < 0.15 else 1.0
        paste_scaled(frame, self.image, self.canvas[0] / 2, self.y, scale)


class StickerLayer(Layer):
    z = 65

    def __init__(self, text: str, at: float, canvas: tuple[int, int], color: tuple, index: int):
        self.at, self.canvas = at, canvas
        size = int(min(canvas) * 0.05)
        f = font(size)
        width, height = int(f.getlength(text)) + size * 2, int(size * 1.9)
        plate = Image.new("RGBA", (width + 12, height + 12), (0, 0, 0, 0))
        d = ImageDraw.Draw(plate)
        d.rounded_rectangle((6, 6, width + 5, height + 5), height // 2, fill=color + (255,), outline=(255, 255, 255, 255), width=4)
        d.text((width / 2 + 6, height / 2 + 6), text, font=f, anchor="mm", fill=(255, 255, 255, 255),
               stroke_width=max(1, size // 14), stroke_fill=(0, 0, 0, 180))
        self.plate = plate
        side = 1 if index % 2 == 0 else -1
        self.cx = canvas[0] * (0.5 + side * 0.28)
        self.cy = canvas[1] * (0.42 if canvas[0] > canvas[1] else 0.36)
        self.angle = -6 * side

    def active(self, t: float) -> bool:
        return self.at <= t < self.at + 1.6

    def draw(self, frame: Image.Image, t: float) -> None:
        age = t - self.at
        scale = ease_out_back(age / 0.2)
        alpha = min(1.0, (1.6 - age) / 0.2)
        wobble = math.sin(age * 9) * 3 * max(0.0, 1 - age)
        paste_scaled(frame, self.plate, self.cx, self.cy, scale, alpha, self.angle + wobble)


class RevealStripLayer(Layer):
    """Answer slots: question marks until each Moment reveals its icon; revealed items persist."""
    z = 55

    def __init__(self, items: list[tuple[float, Image.Image | None, str]], canvas: tuple[int, int], start: float,
                 end: float, accent: tuple, top: float | None = None):
        self.items = sorted(items, key=lambda i: i[0])
        self.canvas, self.start, self.end = canvas, start, end
        n = len(self.items)
        self.d = int(min(canvas[0] * 0.8 / max(n, 1), min(canvas) * 0.15))
        gap = self.d * 0.35
        total = n * self.d + (n - 1) * gap
        self.xs = [canvas[0] / 2 - total / 2 + self.d / 2 + i * (self.d + gap) for i in range(n)]
        self.y = (top if top is not None else canvas[1] * (0.06 if canvas[0] > canvas[1] else 0.08)) + self.d / 2
        self.placeholder = self._disc(None, "?", accent)
        self.revealed = [self._disc(icon, "", accent) for _, icon, _ in self.items]
        label_size = max(14, int(self.d * 0.2))
        self.labels = [text_block(label, label_size, (255, 255, 255), (0, 0, 0), int(self.d * 1.6)) if label else None
                       for _, _, label in self.items]

    def _disc(self, icon: Image.Image | None, mark: str, accent: tuple) -> Image.Image:
        d = self.d
        disc = Image.new("RGBA", (d, d), (0, 0, 0, 0))
        draw = ImageDraw.Draw(disc)
        if icon is None:
            draw.ellipse((0, 0, d - 1, d - 1), fill=(20, 20, 28, 220), outline=accent + (255,), width=max(3, d // 18))
            draw.text((d / 2, d / 2), mark, font=font(int(d * 0.55)), anchor="mm", fill=(255, 255, 255, 255))
        else:
            inner = ImageOps.fit(icon.convert("RGB"), (d, d), Image.LANCZOS).convert("RGBA")
            mask = Image.new("L", (d, d), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, d - 1, d - 1), fill=255)
            inner.putalpha(mask)
            disc.alpha_composite(inner)
            draw.ellipse((0, 0, d - 1, d - 1), outline=(255, 255, 255, 255), width=max(3, d // 16))
        return disc

    def active(self, t: float) -> bool:
        return self.start <= t < self.end

    def draw(self, frame: Image.Image, t: float) -> None:
        for i, (at, _, _) in enumerate(self.items):
            x = self.xs[i]
            if t < at:
                paste_scaled(frame, self.placeholder, x, self.y, 1.0)
                continue
            age = t - at
            scale = ease_out_back(age / 0.25) if age < 0.25 else 1.0
            paste_scaled(frame, self.revealed[i], x, self.y, max(scale, 0.05))
            if self.labels[i] is not None and age > 0.1:
                paste_scaled(frame, self.labels[i], x, self.y + self.d * 0.72, 1.0, min(1.0, (age - 0.1) / 0.15))


class LogoCardLayer(Layer):
    z = 75

    def __init__(self, text: str, sub: str, start: float, end: float, canvas: tuple[int, int], accent: tuple):
        self.start, self.end, self.canvas = start, end, canvas
        w, h = int(canvas[0] * (0.46 if canvas[0] > canvas[1] else 0.78)), int(min(canvas) * 0.3)
        plate = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(plate)
        d.rounded_rectangle((0, 0, w - 1, h - 1), h // 6, fill=(12, 14, 24, 230), outline=accent + (255,), width=5)
        big = int(h * 0.36)
        d.text((w / 2, h * 0.42), text, font=font(big), anchor="mm", fill=accent + (255,),
               stroke_width=max(1, big // 20), stroke_fill=(255, 255, 255, 255))
        if sub:
            d.text((w / 2, h * 0.78), sub, font=font(int(h * 0.14)), anchor="mm", fill=(255, 255, 255, 255))
        self.plate = plate

    def active(self, t: float) -> bool:
        return self.start <= t < self.end

    def draw(self, frame: Image.Image, t: float) -> None:
        age = t - self.start
        scale = 0.7 + 0.3 * ease_out_back(age / 0.3)
        paste_scaled(frame, self.plate, self.canvas[0] / 2, self.canvas[1] * 0.38, scale, min(1.0, age / 0.15))


# ── build and encode ────────────────────────────────────────────────────────────────

@dataclass
class Packaging:
    layers: list[Layer]
    hits: list[tuple[float, str]]
    punches: list[float]


def build_packaging(tl: Timeline, packaging: dict, images: dict[str, Path], canvas: tuple[int, int],
                    layout: dict | None = None) -> Packaging:
    caps = packaging.get("captions") or {}
    styles = caps.get("styles") if isinstance(caps.get("styles"), dict) else {}
    layers: list[Layer] = []
    if caps.get("enabled", True) is not False:
        layers.append(CaptionLayer(tl.cues, styles, canvas, str(caps.get("position") or "bottom"),
                                   str(caps.get("size") or "m"), str(caps.get("animation") or "pop"),
                                   bool(caps.get("uppercase"))))
    hits: list[tuple[float, str]] = []
    punches: list[float] = []
    accent = (255, 77, 109)
    broll_index = sticker_index = 0
    for n, comp in enumerate(packaging.get("components") or []):
        kind = comp.get("type")
        moment = tl.moments.get(str(comp.get("moment") or ""))
        selection = tl.selections.get(str(comp.get("selection") or ""))
        source = comp.get("source")
        if isinstance(source, list) and len(source) == 2:
            a, b = tl.from_source(float(source[0])), tl.from_source(float(source[1]))
            selection = (a, max(b, a + 0.3))
            moment = a
        if kind == "headline" and selection and comp.get("lines"):
            layers.append(HeadlineLayer(comp["lines"], selection[0], selection[1], canvas,
                                        str(comp.get("position") or "top"), str(comp.get("plate") or "none"), layout))
            continue
        if kind == "flash" and moment is not None:
            layers.append(FlashLayer(moment, hex_rgb(comp.get("color"), (255, 255, 255)), canvas))
        elif kind == "sfx" and moment is not None:
            hits.append((moment, str(comp.get("kind") or "pop")))
        elif kind == "zoom_punch" and moment is not None:
            punches.append(moment)
        elif kind == "sticker" and moment is not None and comp.get("text"):
            layers.append(StickerLayer(str(comp["text"])[:16], moment, canvas, hex_rgb(comp.get("color"), accent), sticker_index))
            sticker_index += 1
        elif kind == "title" and (selection or moment is not None) and comp.get("text"):
            start, end = selection or (moment, moment + 2.0)
            layers.append(TitleLayer(str(comp["text"])[:24], start, end, canvas, accent))
        elif kind == "broll" and selection and images.get(f"broll-{n}"):
            with Image.open(images[f"broll-{n}"]) as img:
                layers.append(BRollLayer(img.copy(), selection[0], selection[1], canvas,
                                         str(comp.get("layout") or "card"), broll_index, str(comp.get("position") or "")))
            broll_index += 1
        elif kind == "logo_card" and (selection or moment is not None) and comp.get("text"):
            start, end = selection or (moment, tl.duration)
            if tl.duration - end < 1.5:
                end = tl.duration
            layers.append(LogoCardLayer(str(comp["text"])[:16], str(comp.get("sub") or "")[:28], start, end, canvas, accent))
        elif kind == "reveal_strip":
            items = []
            for k, item in enumerate(comp.get("items") or []):
                at = tl.moments.get(str(item.get("moment") or ""))
                if at is None:
                    continue
                path = images.get(f"icon-{n}-{k}")
                icon = Image.open(path).copy() if path else None
                items.append((at, icon, str(item.get("label") or "")[:8]))
            if items:
                first = min(i[0] for i in items)
                start = next((a for a, b in tl.segment_spans if a <= first < b), 0.0)
                titles = [layer for layer in layers if isinstance(layer, TitleLayer)]
                top = max((layer.y + layer.plate.height / 2 for layer in titles), default=None)
                layers.append(RevealStripLayer(items, canvas, start, tl.duration, accent,
                                               None if top is None else top + min(canvas) * 0.03))
    # Stack concurrent headlines from the top down so blocks never overlap.
    heads = [layer for layer in layers if isinstance(layer, HeadlineLayer) and layer.position in {"top", "middle"}]
    for layer in sorted(heads, key=lambda h: (h.position != "top", h.start)):
        above = [h for h in heads if h is not layer and h.placed and h.start < layer.end and layer.start < h.end]
        if above:
            floor = max(h.y + h.image.height / 2 for h in above)
            layer.y = max(layer.y, floor + canvas[1] * 0.008 + layer.image.height / 2)
        layer.placed = True
    layers.sort(key=lambda layer: layer.z)
    return Packaging(layers, hits, punches)


def zoom_filter(punches: list[float], canvas: tuple[int, int]) -> str:
    if not punches:
        return "null"
    terms = []
    for at in punches:
        terms.append(f"0.12*(between(t,{at:.3f},{at + 0.13:.3f})*(t-{at:.3f})/0.13"
                     f"+between(t,{at + 0.13:.3f},{at + 0.73:.3f})"
                     f"+between(t,{at + 0.73:.3f},{at + 0.93:.3f})*(1-(t-{at + 0.73:.3f})/0.2))")
    z = "(1+" + "+".join(terms) + ")"
    w, h = canvas
    return (f"scale=w='trunc({w}*{z}/2)*2':h='trunc({h}*{z}/2)*2':eval=frame,"
            f"crop={w}:{h}:(in_w-{w})/2:(in_h-{h})/2")


def encode(base_video: Path, audio: Path, packaging: Packaging, duration: float, canvas: tuple[int, int],
           target: Path) -> Path:
    w, h = canvas
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(base_video),
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
           "-i", str(audio), "-filter_complex",
           f"[0:v]{zoom_filter(packaging.punches, canvas)},setsar=1[b];[b][1:v]overlay=0:0:format=auto,format=yuv420p[v]",
           "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.3f}", "-movflags", "+faststart", str(target)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    empty = Image.new("RGBA", canvas, (0, 0, 0, 0)).tobytes()
    frames = int(math.ceil(duration * FPS))
    try:
        for i in range(frames):
            t = i / FPS
            active = [layer for layer in packaging.layers if layer.active(t)]
            if not active:
                proc.stdin.write(empty)
                continue
            frame = Image.new("RGBA", canvas, (0, 0, 0, 0))
            for layer in active:
                layer.draw(frame, t)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait(timeout=1200) != 0:
        raise RuntimeError(f"成片编码失败: {err[-800:]}")
    return target


def render(takes: list[TakeClip], lines_per_take: list[list[Line]], times_per_take, packaging: dict,
           images: dict[str, Path], ratio: str, work: Path, target: Path, music: Path | None = None,
           caption_position: str | None = None, source_ranges: list[tuple[float, float]] | None = None,
           audio_source: tuple[Path, float, float] | None = None, layout: dict | None = None) -> tuple[Path, Timeline]:
    canvas = canvas_size(ratio)
    tl = build_timeline(takes, lines_per_take, times_per_take)
    tl.source_ranges = list(source_ranges or [])
    if caption_position:
        packaging = {**packaging, "captions": {**(packaging.get("captions") or {}), "position": caption_position}}
    base = assemble(takes, work / "timeline", ratio, audio_source, layout)
    pack = build_packaging(tl, packaging, images, canvas, layout)
    audio = mix_audio(base, work / "mix.wav", pack.hits, music, tl.duration, work)
    encode(base, audio, pack, tl.duration, canvas, target)
    if probe(target)["duration"] < tl.duration - 0.5:
        raise RuntimeError("成片时长不足")
    return target, tl
