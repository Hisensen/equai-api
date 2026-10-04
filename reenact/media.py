"""Local media primitives (ffmpeg / Pillow) for the reenactment stages."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat


def run(cmd: list[str], timeout: int = 600) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} 失败: {proc.stderr[-800:]}")


def probe(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    info = json.loads(proc.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {
        "duration": float(info.get("format", {}).get("duration") or video.get("duration") or 0),
        "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
        "has_audio": audio is not None,
    }


def frame(source: Path, target: Path, at: float, width: int | None = None) -> Path:
    vf = ["setsar=1"] + ([f"scale={width}:-2"] if width else [])
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{max(0, at):.3f}", "-i", str(source),
         "-frames:v", "1", "-vf", ",".join(vf), "-q:v", "2", str(target)], 90)
    if not target.is_file():
        raise RuntimeError("无法抽取视频帧")
    return target


def sharpness(path: Path) -> float:
    with Image.open(path) as image:
        gray = image.convert("L").resize((320, int(320 * image.height / max(image.width, 1))))
        return ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0]


def best_frame(source: Path, target: Path, around: float, start: float, end: float, work: Path) -> float:
    """Pick the sharpest of several candidates inside [start, end] near ``around``.

    A midpoint can land on a head turn or motion blur; the clearest frame is the better
    composition authority for the camera-setup picture."""
    lo, hi = max(start + 0.1, around - 0.8), min(end - 0.1, around + 0.8)
    if hi <= lo:
        lo = hi = max(0.0, around)
    candidates = [lo + (hi - lo) * k / 4 for k in range(5)] if hi > lo else [lo]
    best, best_t = -1.0, candidates[0]
    for i, t in enumerate(candidates):
        path = frame(source, work / f"cand-{i}.jpg", t)
        score = sharpness(path)
        if score > best:
            best, best_t = score, t
    frame(source, target, best_t)
    return round(best_t, 3)


def voice_sample(source: Path, target: Path, spans: list[tuple[float, float]], max_seconds: float = 12.0) -> float:
    """Concatenate one speaker's clearest sentences into a short timbre reference (wav)."""
    chosen, total = [], 0.0
    for start, end in sorted(spans, key=lambda s: s[1] - s[0], reverse=True):
        if total >= max_seconds:
            break
        seconds = min(end - start + 0.15, max_seconds - total)
        if seconds < 0.6:
            continue
        chosen.append((max(0.0, start - 0.05), seconds))
        total += seconds
    if not chosen:
        raise RuntimeError("没有足够的原声片段")
    chosen.sort()
    inputs, filters = [], []
    for i, (start, seconds) in enumerate(chosen):
        inputs += ["-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(source)]
        filters.append(f"[{i}:a:0]aresample=24000,aformat=channel_layouts=mono,afade=t=in:d=0.03,"
                       f"areverse,afade=t=in:d=0.05,areverse[a{i}]")
    labels = "".join(f"[a{i}]" for i in range(len(chosen)))
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex",
         ";".join(filters) + f";{labels}concat=n={len(chosen)}:v=0:a=1[out]",
         "-map", "[out]", "-c:a", "pcm_s16le", str(target)], 120)
    return round(total, 2)


def voice_features(source: Path, start: float, end: float, rate: int = 8000) -> dict:
    """{"pitch": median fundamental frequency in Hz or None, "level": loudness in dBFS or None} of [start, end].

    A plain normalised autocorrelation over a few dozen short frames: enough to tell a low (typically male)
    voice from a high (typically female or child) one, which settles most speaker questions. The level tells
    foreground dialogue from quieter background voices (a TV, a crowd, a phone speaker)."""
    import math
    from array import array
    seconds = max(0.2, end - start)
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{seconds:.3f}",
                          "-i", str(source), "-vn", "-ac", "1", "-ar", str(rate), "-af", "highpass=f=60,lowpass=f=1000",
                          "-f", "s16le", "-"], capture_output=True, timeout=60).stdout
    samples = array("h", raw[: len(raw) // 2 * 2])
    size, lo, hi = 256, rate // 400, rate // 70  # 70–400 Hz
    if len(samples) < size + hi:
        return {"pitch": None, "level": None}
    rms = (sum(x * x for x in samples) / len(samples)) ** 0.5
    level = round(20 * math.log10(rms / 32768), 1) if rms else None
    starts = range(0, len(samples) - size - hi, max(1, (len(samples) - size - hi) // 30))
    loud = max((sum(x * x for x in samples[i:i + size]) for i in starts), default=0)
    pitches = []
    for i in starts:
        frame_ = samples[i:i + size + hi]
        energy = sum(x * x for x in frame_[:size])
        if energy < loud * 0.1 or energy == 0:
            continue
        scores = []
        for lag in range(lo, hi + 1):
            tail = frame_[lag:lag + size]
            r = sum(a * b for a, b in zip(frame_[:size], tail))
            norm = (energy * sum(b * b for b in tail)) ** 0.5
            scores.append(r / norm if norm else 0.0)
        best = max(scores)
        if best <= 0.6:
            continue
        # Multiples of the period correlate just as well: the first strong peak is the pitch, not the highest one.
        for k, r in enumerate(scores):
            if r >= best * 0.9 and (k + 1 == len(scores) or r >= scores[k + 1]):
                pitches.append(rate / (lo + k))
                break
    if len(pitches) < 3:
        return {"pitch": None, "level": level}
    pitches.sort()
    return {"pitch": round(pitches[len(pitches) // 2], 1), "level": level}


def frames_every(source: Path, directory: Path, every: float, width: int = 480, end: float | None = None) -> list[tuple[float, Path]]:
    """One decode pass: a frame every `every` seconds, as (time, path)."""
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("f-*.jpg"):
        old.unlink()
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source)]
    if end:
        cmd += ["-t", f"{end:.3f}"]
    run(cmd + ["-vf", f"fps=1/{every},scale={width}:-2", "-q:v", "4", str(directory / "f-%05d.jpg")], 300)
    paths = sorted(directory.glob("f-*.jpg"))
    return [(round(i * every, 2), p) for i, p in enumerate(paths)]


def saturation(path: Path) -> float:
    """Mean chroma (0-255) of a picture: saturation weighted by brightness, so near-black noise counts for nothing."""
    with Image.open(path) as im:
        hsv = im.convert("RGB").resize((160, 90)).convert("HSV")
    pixels = list(hsv.getdata())
    return sum(s * v for _, s, v in pixels) / 255 / len(pixels)


def video_saturation(video: Path, work: Path, samples: int = 6) -> float:
    """Median saturation over frames spread across a clip."""
    seconds = probe(video)["duration"]
    values = []
    for k in range(samples):
        target = work / f"sat-{video.stem}-{k}.jpg"
        frame(video, target, seconds * (k + 0.5) / samples, 320)
        values.append(saturation(target))
    values.sort()
    return values[len(values) // 2]


def cut_hints(source: Path) -> list[float]:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(source), "-vf",
         "select='gt(scene,0.12)',showinfo", "-f", "null", "-"],
        capture_output=True, text=True, timeout=180,
    )
    import re
    hints: list[float] = []
    for value in sorted(float(v) for v in re.findall(r"pts_time:([\d.]+)", proc.stderr)):
        if not hints or value - hints[-1] >= 0.3:
            hints.append(round(value, 2))
    return hints[:40]


def analysis_proxy(source: Path, target: Path) -> Path:
    """A small H.264 proxy (<=640px, 2 Mbps) keeps the vision provider's fetch fast."""
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source), "-vf",
         "scale='if(gt(iw,ih),min(640,iw),-2)':'if(gt(iw,ih),-2,min(640,ih))'", "-c:v", "libx264",
         "-preset", "veryfast", "-b:v", "1500k", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
         str(target)], 600)
    return target


def contact_sheet(video: Path, target: Path, every: float, columns: int = 5, width: int = 256) -> Path:
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video), "-vf",
         f"fps=1/{every},scale={width}:-2,tile={columns}x4", "-frames:v", "1", "-q:v", "3", str(target)], 180)
    return target


def composition_sketch(source: Path, target: Path, layout: dict | None = None) -> Path:
    """Grey, blurred, low-contrast version of a reference frame: placement survives,
    faces, outfits and colours do not, so the image model cannot copy the old look."""
    from PIL import ImageEnhance, ImageOps
    with Image.open(source) as image:
        if layout and layout.get("type") == "panel":
            image = image.crop((0, int(image.height * layout["top"]), image.width, int(image.height * layout["bottom"])))
        grey = ImageOps.grayscale(image)
        small = grey.resize((max(1, grey.width // 12), max(1, grey.height // 12)), Image.BILINEAR)
        sketch = small.resize(grey.size, Image.BILINEAR).filter(ImageFilter.GaussianBlur(6))
        sketch = ImageEnhance.Contrast(sketch).enhance(0.8)
        sketch.convert("RGB").save(target, quality=90)
    return target


def excerpt(source: Path, target: Path, start: float, seconds: float, *, audio: bool) -> Path:
    """A passage of the reference: silent H.264 video for motion, or a wav of its soundtrack."""
    common = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{max(0, start):.3f}", "-i", str(source),
              "-t", f"{seconds:.3f}"]
    if audio:
        run(common + ["-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(target)], 120)
    else:
        run(common + ["-an", "-vf", "scale='min(720,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", "20", "-movflags", "+faststart", str(target)], 300)
    return target


def detect_layout(source: Path, work: Path, end: float) -> dict:
    """Find a designed panel: rows that stay still across the whole piece while the picture moves.

    Returns {"type": "full"} or {"type": "panel", "top": fraction, "bottom": fraction, "color": "#rrggbb"}
    where top/bottom bound the moving camera picture inside the frame."""
    from PIL import ImageStat
    times = [end * k / 11 for k in range(1, 11)]
    frames = []
    for i, t in enumerate(times):
        path = frame(source, work / f"layout-probe-{i}.jpg", t, 180)
        with Image.open(path) as im:
            frames.append(im.convert("L").copy())
    w, h = frames[0].size
    moving = []
    for y in range(h):
        rows = [list(f.crop((0, y, w, y + 1)).getdata()) for f in frames]
        diffs = [sum(abs(a - b) for a, b in zip(r1, r2)) / w for r1, r2 in zip(rows, rows[1:])]
        moving.append(sum(diffs) / len(diffs))
    threshold = max(4.0, sorted(moving)[len(moving) // 2] * 0.25)
    top = 0
    while top < h and moving[top] < threshold:
        top += 1
    bottom = h
    while bottom > top and moving[bottom - 1] < threshold:
        bottom -= 1
    top_frac, bottom_frac = top / h, bottom / h
    if top_frac < 0.08 and bottom_frac > 0.92:
        return {"type": "full"}
    with Image.open(work / "layout-probe-0.jpg") as im:
        rgb = im.convert("RGB")
        band = rgb.crop((0, 0, rgb.width, max(1, int(top_frac * rgb.height)))) if top_frac >= 0.08 else \
            rgb.crop((0, int(bottom_frac * rgb.height), rgb.width, rgb.height))
        colour = tuple(int(v) for v in ImageStat.Stat(band).median)
    region_aspect = (w / h) / max(bottom_frac - top_frac, 0.05)
    return {"type": "panel", "top": round(top_frac, 3), "bottom": round(bottom_frac, 3),
            "color": "#%02x%02x%02x" % colour, "aspect": round(region_aspect, 3),
            "gen_ratio": "16:9" if region_aspect >= 0.8 else "9:16"}


def graphic_masks(analysis: dict, layout: dict | None) -> list[tuple[float, float]]:
    """Bands of the camera picture (fractions) that carry designed text, insets or platform marks."""
    if layout and layout.get("type") == "panel":
        return [(0.9, 1.0)]
    masks = []
    for g in analysis.get("graphics") or []:
        pos = str(g.get("position") or "").lower()
        if g.get("type") == "caption":
            masks.append((0.8, 1.0))
        elif "top" in pos:
            masks.append((0.0, 0.16))
        elif "bottom" in pos or "inset" in pos:
            masks.append((0.72, 1.0))
    if analysis.get("platform_overlays"):
        masks += [(0.0, 0.08), (0.9, 1.0)]
    return sorted(set(masks))


def motion_reference(source: Path, target: Path, start: float, seconds: float, content_end: float,
                     layout: dict | None, masks: list[tuple[float, float]] | None = None,
                     freeze: list[tuple[float, float]] | None = None) -> Path:
    """Movement-only reference: the camera picture region, grey and softened so text, colours and
    outfits cannot be copied; frozen on its last frame if the passage reaches the content end."""
    available = max(0.5, min(seconds, content_end - start))
    crop = ""
    if layout and layout.get("type") == "panel":
        crop = f"crop=iw:ih*{layout['bottom'] - layout['top']:.3f}:0:ih*{layout['top']:.3f},"
    pad = max(0.0, seconds - available)
    boxes = "".join(f"drawbox=x=0:y=ih*{a:.3f}:w=iw:h=ih*{b - a:.3f}:color=gray:t=fill,"
                    for a, b in masks or [])
    # H3's cost grows with the reference video's pixel count; a 360 px short edge (the platform's
    # standard for reference videos) keeps the movement and costs about half the GPU time of 720.
    vf = (f"{crop}scale='if(lt(iw,ih),min(360,iw),-2)':'if(lt(iw,ih),-2,min(360,ih))',{boxes}hue=s=0,gblur=sigma=2,"
          + (f"tpad=stop_mode=clone:stop_duration={pad:.3f}," if pad > 0.05 else "") + "format=yuv420p")
    inputs = ["-ss", f"{max(0, start):.3f}", "-t", f"{available:.3f}", "-i", str(source)]
    graph = f"[0:v]{vf}[base]"
    last = "base"
    # Inserted graphics are not performance: hold the neighbouring camera frame over them.
    for i, (a, b) in enumerate(freeze or []):
        a, b = max(0.0, a), min(available, b)
        if b - a < 0.05:
            continue
        hold_at = a - 0.08 if a > 0.2 else min(available - 0.05, b + 0.08)
        still = target.with_name(f"{target.stem}-hold{i}.png")
        frame(source, still, start + hold_at)
        inputs += ["-loop", "1", "-t", f"{available:.3f}", "-i", str(still)]
        graph += (f";[{i + 1}:v]{vf}[h{i}];[{last}][h{i}]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})'[s{i}]")
        last = f"s{i}"
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", f"[{last}]",
         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-t", f"{seconds:.3f}",
         "-movflags", "+faststart", str(target)], 300)
    return target


def scene_cuts(video: Path, threshold: float = 0.3) -> list[float]:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(video), "-vf",
                           f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
                          capture_output=True, text=True, timeout=300)
    import re
    return sorted(float(v) for v in re.findall(r"pts_time:([\d.]+)", proc.stderr))
