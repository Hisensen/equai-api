"""Orchestrates one reenactment task from reference to finished video.

Each stage result is a project node (video_project_nodes). A stage whose node is ready
is skipped, and a paid request records its remote id before waiting, so a restarted
service continues the same work instead of paying again. Generated pictures and Takes
pass an automatic review; failures feed concrete corrections into the next attempt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from . import analysis as analysis_stage
from . import director, kits, llm, media, providers, qa
from .align import asr_sentences, timed_chars, transcript_text
from .render import TakeClip, plan_trims, render
from .script import parse_line, spoken_units, strip_markup

log = logging.getLogger("h3.reenact")
RUNNING: dict[str, asyncio.Task] = {}
# Each round submits N candidates (default 1) and keeps the best reviewed one; another round,
# carrying the review's corrections, runs only when no candidate passes.
IMAGE_CANDIDATES = int(os.getenv("H3_REENACT_IMAGE_CANDIDATES", "1"))
TAKE_CANDIDATES = int(os.getenv("H3_REENACT_TAKE_CANDIDATES", "1"))
MAX_ROUNDS = int(os.getenv("H3_REENACT_MAX_ROUNDS", "3"))
# GPU generations one user may run at the same time, shared by all of that user's reenactments
# (never above the account's own max_concurrency setting).
GPU_SLOTS_PER_USER = int(os.getenv("H3_REENACT_GPU_SLOTS_PER_USER", "10"))


class Slots:
    """Counting limit that grants several slots atomically (a Segment's candidates start together)."""

    def __init__(self, size: int):
        self.size, self.used = size, 0
        self.cond = asyncio.Condition()

    async def acquire(self, n: int) -> int:
        n = max(1, min(n, self.size))
        async with self.cond:
            await self.cond.wait_for(lambda: self.used + n <= self.size)
            self.used += n
        return n

    async def release(self, n: int) -> None:
        async with self.cond:
            self.used -= n
            self.cond.notify_all()


USER_SLOTS: dict[str, Slots] = {}


def user_slots(merchant_id: str) -> Slots:
    if merchant_id not in USER_SLOTS:
        from database import connect
        with connect() as db:
            row = db.execute("SELECT max_concurrency FROM merchants WHERE id=?", (merchant_id,)).fetchone()
        limit = min(GPU_SLOTS_PER_USER, int(row["max_concurrency"] or GPU_SLOTS_PER_USER) if row else GPU_SLOTS_PER_USER)
        USER_SLOTS[merchant_id] = Slots(max(1, limit))
    return USER_SLOTS[merchant_id]
IMAGE_CONCURRENCY = int(os.getenv("H3_REENACT_IMAGE_CONCURRENCY", "6"))
_image_slots: asyncio.Semaphore | None = None


def image_slots() -> asyncio.Semaphore:
    """One limit for the whole service: the image API rate-limits bursts, so parallel tasks share it."""
    global _image_slots
    if _image_slots is None:
        _image_slots = asyncio.Semaphore(IMAGE_CONCURRENCY)
    return _image_slots


def _casting(cast: dict) -> str:
    return "; ".join(f"{c.get('label', cid)}: {c.get('person', '')[:250]}" for cid, c in cast.items()) or "(no characters)"


def _speaker_label(cast: dict, line: dict) -> str:
    """Who is heard, for the reviewer: an off-screen voice is never a character's mouth."""
    if line.get("offscreen") or line["role"] not in cast:
        return "off-screen voice (nobody on screen speaks it)"
    return cast[line["role"]].get("label", line["role"])


def _image_cost(meta: dict) -> float:
    cost = meta.get("cost") if isinstance(meta, dict) else None
    try:
        return float(cost.get("amount")) if isinstance(cost, dict) else float(cost or 0.0525)
    except (TypeError, ValueError):
        return 0.0525


def work_root() -> Path:
    return Path(os.getenv("H3_REENACT_WORK_DIR", "/data/reenact"))


# ── lifecycle ───────────────────────────────────────────────────────────────────────

def ensure(task_id: str) -> None:
    """Start (or resume) the runner for a task in this process if it is not running."""
    current = RUNNING.get(task_id)
    if current is not None and not current.done():
        return
    RUNNING[task_id] = asyncio.create_task(_guarded(task_id), name=f"reenact-{task_id}")


async def _guarded(task_id: str) -> None:
    try:
        await Runner(task_id).run()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Reenactment failed for %s", task_id)
        Runner.fail(task_id, str(exc))
    finally:
        RUNNING.pop(task_id, None)


def rerun(task_id: str, reset: list[str]) -> list[str]:
    """Redo only the named stages of a finished reenactment and reuse every other node.

    reset holds node keys, or prefixes ending in '*' (e.g. ["script", "take-seg01", "render"]).
    Billing already settled is not charged again; the new result replaces the task's video."""
    from database import connect
    with connect() as db:
        row = db.execute("SELECT payload FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise RuntimeError("任务不存在")
        payload = json.loads(row["payload"] or "{}")
        project_id = (payload.get("_project") or {}).get("project_id")
        options = payload.setdefault("_reenact", {})
        options["run"] = int(options.get("run") or 0) + 1
        for stale in ("warnings", "notes"):  # a rerun reports only its own findings
            options.pop(stale, None)
        db.execute("UPDATE merchant_tasks SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), task_id))
        removed = []
        for key in reset:
            clause, value = ("node_key LIKE ?", key[:-1] + "%") if key.endswith("*") else ("node_key=?", key)
            removed += [r["node_key"] for r in db.execute(
                f"SELECT node_key FROM video_project_nodes WHERE project_id=? AND {clause}", (project_id, value))]
            db.execute(f"DELETE FROM video_project_nodes WHERE project_id=? AND {clause}", (project_id, value))
        db.execute("""UPDATE merchant_tasks SET status='running',remote_id='reenact',progress=30,
                   message='局部重跑：复用已完成的素材',error='',finished_at=NULL WHERE id=?""", (task_id,))
    ensure(task_id)
    return removed


async def cancel(task_id: str) -> None:
    from database import connect
    with connect() as db:
        rows = db.execute(
            """SELECT n.output_json FROM video_project_nodes n JOIN video_project_runs r ON r.project_id=n.project_id
            WHERE r.task_id=? AND n.node_type='take'""", (task_id,),
        ).fetchall()
    for row in rows:
        for attempt in (json.loads(row["output_json"] or "{}").get("attempts") or []):
            if attempt.get("remote") and not attempt.get("artifact"):
                await providers.h3_stop(str(attempt["remote"]))
    task = RUNNING.pop(task_id, None)
    if task is not None:
        task.cancel()


# ── runner ──────────────────────────────────────────────────────────────────────────

class Runner:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.ledger = llm.Ledger()
        self.work = work_root() / task_id
        self.work.mkdir(parents=True, exist_ok=True)
        self.billing = 0.0
        self._lock = asyncio.Lock()
        self.load()

    # persistence -------------------------------------------------------------------
    @property
    def run_tag(self) -> str:
        """Every rerun gets fresh request ids: the image service replays a known idempotency key and an H3 job
        id names its output path, so reusing them would hand back the previous run's pictures and clips."""
        run = int((getattr(self, "options", None) or {}).get("run") or 0)
        return f"{self.task_id}-r{run}" if run else self.task_id

    def load(self) -> None:
        from database import connect
        with connect() as db:
            row = db.execute("SELECT * FROM merchant_tasks WHERE id=?", (self.task_id,)).fetchone()
        if not row:
            raise RuntimeError("任务不存在")
        self.row = dict(row)
        self.payload = json.loads(self.row["payload"] or "{}")
        self.options = self.payload.get("_reenact") or {}
        self.project_id = (self.payload.get("_project") or {}).get("project_id")
        if not self.project_id:
            raise RuntimeError("重演任务缺少项目")
        self.inputs = json.loads(self.row["input_objects"] or "[]")
        self.ratio = self.row["ratio"]
        self.template_id = self.options.get("template_id")

        self._template: dict | None = None

    def template(self) -> dict | None:
        if self.template_id and self._template is None:
            from . import templates
            self._template = templates.snapshot(self.template_id)
        return self._template

    def status(self) -> str:
        from database import connect
        with connect() as db:
            row = db.execute("SELECT status FROM merchant_tasks WHERE id=?", (self.task_id,)).fetchone()
        return row["status"] if row else "missing"

    def node(self, key: str) -> dict | None:
        from database import connect
        with connect() as db:
            row = db.execute("SELECT status,output_json FROM video_project_nodes WHERE project_id=? AND node_key=?",
                             (self.project_id, key)).fetchone()
        if not row:
            return None
        return {"status": row["status"], **json.loads(row["output_json"] or "{}")}

    def save(self, key: str, node_type: str, status: str, output: dict) -> None:
        from database import connect, public_id
        now = time.time()
        with connect() as db:
            run = db.execute("SELECT id FROM video_project_runs WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                             (self.task_id,)).fetchone()
            db.execute(
                """INSERT INTO video_project_nodes
                (id,project_id,run_id,node_type,node_key,dependency_fingerprint,status,output_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id,node_key) DO UPDATE SET status=excluded.status,
                  output_json=excluded.output_json,node_type=excluded.node_type,updated_at=excluded.updated_at""",
                (public_id("node"), self.project_id, run["id"] if run else None, node_type, key, "", status,
                 json.dumps(output, ensure_ascii=False), now, now),
            )

    async def transcribe(self, url: str, seconds: float) -> dict:
        started = time.monotonic()
        result = await providers.transcribe(url)
        self.ledger.add("asr", time.monotonic() - started, 0, 0,
                        seconds * float(os.getenv("H3_ASR_PRICE_PER_SECOND", "0.00022")))
        return result

    def flush_ledger(self) -> None:
        """Move model/ASR/image calls into the task record so a restart never loses the account."""
        if not self.ledger.calls:
            return
        self.options["model_cost"] = round(float(self.options.get("model_cost") or 0) + self.ledger.yuan, 6)
        self.options["model_calls"] = (self.options.get("model_calls") or []) + self.ledger.calls
        self.ledger.calls = []
        self.persist_options()

    def progress(self, value: int, message: str) -> None:
        from database import connect
        self.flush_ledger()
        cost = round(self.billing + float(self.options.get("model_cost") or 0), 6)
        with connect() as db:
            db.execute("""UPDATE merchant_tasks SET progress=MAX(progress,?),message=?,provider_billing=?
                       WHERE id=? AND status='running'""", (value, message[:120], cost, self.task_id))

    def documents(self, **docs: str) -> None:
        from database import connect
        with connect() as db:
            row = db.execute("SELECT treatment_json FROM video_projects WHERE id=?", (self.project_id,)).fetchone()
            current = json.loads((row["treatment_json"] if row else None) or "{}")
            current.setdefault("documents", {}).update(docs)
            db.execute("UPDATE video_projects SET treatment_json=?,updated_at=? WHERE id=?",
                       (json.dumps(current, ensure_ascii=False), time.time(), self.project_id))

    def warn(self, message: str) -> None:
        warnings = self.options.setdefault("warnings", [])
        if message not in warnings:
            warnings.append(message)
        self.persist_options()

    def warn_free(self, message: str) -> None:
        """An informational note shown with the task (not a quality warning)."""
        notes = self.options.setdefault("notes", [])
        if message not in notes:
            notes.append(message)
        self.persist_options()

    def persist_options(self) -> None:
        from database import connect
        self.payload["_reenact"] = self.options
        with connect() as db:
            db.execute("UPDATE merchant_tasks SET payload=? WHERE id=?",
                       (json.dumps(self.payload, ensure_ascii=False, separators=(",", ":")), self.task_id))

    @staticmethod
    def fail(task_id: str, error: str) -> None:
        from scheduler import _terminal_failure
        from database import connect
        with connect() as db:
            row = db.execute("SELECT provider_billing FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
        _terminal_failure(task_id, f"视频重演失败：{error[:900]}", True, float(row["provider_billing"] or 0) if row else 0)

    # main --------------------------------------------------------------------------
    async def run(self) -> None:
        if self.status() != "running":
            return
        a = await self.stage_analysis()
        self.layout = a.get("layout") or {"type": "full"}
        panel = self.layout.get("type") == "panel"
        self.gen_ratio = self.layout.get("gen_ratio", self.ratio) if panel else self.ratio
        self.framing_note = (
            "Keep every character fully inside the central area that matches the final picture band "
            f"(aspect about {self.layout.get('aspect', 1.0):.2f}:1); the frame edges outside it will be cropped away."
            if panel else "")
        d, s = await self.stage_direction(a)
        self.direction = d
        pictures = await self.stage_assets(a, d, s)
        takes = await self.stage_takes(a, d, s, pictures)
        await self.stage_render(a, d, s, pictures, takes)

    # 1. reference understanding ------------------------------------------------------
    async def source_path(self) -> Path:
        item = next(i for i in self.inputs if i.get("field") == "videos")
        return await providers.fetch_input(self.row, item, self.work / "source.mp4")

    async def stage_analysis(self) -> dict:
        done = self.node("analysis")
        if done and done["status"] == "ready":
            return done["analysis"]
        tpl = self.template()
        if tpl is not None:
            # The reference was understood when the template was made; only new pictures need reading.
            self.progress(10, "使用模板：复用原片分析")
            result = json.loads(json.dumps(tpl["analysis"]))
            pictures = []
            for n, item in enumerate(i for i in self.inputs if i.get("field") == "images"):
                path = await providers.fetch_input(self.row, item, self.work / f"upload-{n + 1}{Path(item['filename']).suffix or '.png'}")
                pictures.append(qa.data_url(path, 1280))
            result["user_pictures"] = await analysis_stage.describe_pictures(pictures, self.ledger)
            self.save("analysis", "analysis", "ready", {"analysis": result, "template_id": self.template_id})
            self.documents(ANALYSIS=analysis_stage.analysis_markdown(result), TIMELINE=analysis_stage.timeline_markdown(result))
            return result
        self.progress(8, "正在理解参考视频：语音转写与镜头拆解")
        source = await self.source_path()
        info = media.probe(source)
        proxy = await asyncio.to_thread(media.analysis_proxy, source, self.work / "proxy.mp4")
        stored = await providers.put(self.row, proxy, "proxy.mp4", "video/mp4")
        url = providers.url_for(stored)
        sentences: list[dict] = []
        if info["has_audio"]:
            try:
                sentences = asr_sentences(await self.transcribe(url, info["duration"]))
            except Exception as exc:  # noqa: BLE001 - a silent video still has a structure
                self.warn(f"参考视频语音转写失败：{exc}")
        hints = await asyncio.to_thread(media.cut_hints, source)
        picture_urls = []
        for n, item in enumerate(i for i in self.inputs if i.get("field") == "images"):
            path = await providers.fetch_input(self.row, item, self.work / f"upload-{n + 1}{Path(item['filename']).suffix or '.png'}")
            picture_urls.append(qa.data_url(path, 1280))
        result = await analysis_stage.understand(url, info["duration"], sentences, hints, picture_urls, self.ledger)
        try:
            result["text_track"] = await analysis_stage.text_track(source, result["content_end"], self.work / "text", self.ledger)
        except Exception as exc:  # noqa: BLE001 - the whole-video reading still describes the graphics
            self.warn(f"画面文字识别失败：{exc}")
        if sentences:
            speakers_dir = self.work / "speakers"
            speakers_dir.mkdir(exist_ok=True)
            result = await analysis_stage.attribute_speakers(source, result, sentences, speakers_dir, self.ledger)
        result["source"] = {**info, "proxy": stored, "cut_hints": hints}
        result["layout"] = await asyncio.to_thread(media.detect_layout, source, self.work, result["content_end"])
        self.save("analysis", "analysis", "ready", {"analysis": result})
        from database import connect
        with connect() as db:
            db.execute("UPDATE video_projects SET analysis_json=?,timeline_json=?,status='planned',updated_at=? WHERE id=?",
                       (json.dumps(result, ensure_ascii=False),
                        json.dumps({"shots": result["shots"], "lines": result["lines"]}, ensure_ascii=False),
                        time.time(), self.project_id))
        self.documents(ANALYSIS=analysis_stage.analysis_markdown(result), TIMELINE=analysis_stage.timeline_markdown(result))
        self.progress(18, "参考视频已拆解完成")
        return result

    # 2. Brief, Treatment, Script -------------------------------------------------------
    async def stage_direction(self, a: dict) -> tuple[dict, dict]:
        prompt = str(self.options.get("prompt") or self.payload.get("prompt") or "")
        done = self.node("direction")
        tpl = self.template()
        if done and done["status"] == "ready":
            d = done["direction"]
        elif tpl is not None:
            self.progress(22, "使用模板：导演按新提示词做增量修改")
            d = await director.adapt(prompt, a, tpl, self.ratio, self.ledger)
            self.save("direction", "direction", "ready", {"direction": d})
            reused = director.reusable_assets(tpl, d)
            for key, node in reused.items():
                if not self.node(key):
                    self.save(key, node.get("type") or "image", "ready",
                              {"stored": node["stored"], "qa": node.get("qa"), "seconds": node.get("seconds"),
                               "source_speaker": node.get("source_speaker"), "from_template": self.template_id})
            changes = d.get("changes") or {}
            self.documents(CHANGES="# 相对模板的改动\n\n" + changes.get("summary", "") +
                           f"\n\n- 重新设计的角色：{', '.join(changes.get('cast') or []) or '无'}"
                           f"\n- 场景变化：{'是' if changes.get('setting') else '否'}　风格变化：{'是' if changes.get('style') else '否'}"
                           f"\n- 台词与文字：{'改写' if changes.get('script') == 'rewrite' else '沿用模板'}"
                           f"\n- 直接复用的素材：{', '.join(sorted(reused)) or '无'}")
            self.warn_free(f"模板复用 {len(reused)} 项素材：{', '.join(sorted(reused)) or '无'}")
        else:
            self.progress(22, "导演正在制定改编方案（Brief / Treatment / 选角 / 机位）")
            d = await director.treatment(prompt, a, self.ratio, self.ledger)
            self.save("direction", "direction", "ready", {"direction": d})
            brief = d["brief"]
            looks = "\n".join(f"- **{c.get('label', c['id'])}**：保留 " + "；".join(c.get("keeps") or []) + " ｜ 改动 "
                              + "；".join(c.get("look_changes") or []) for c in d["cast"])
            self.documents(BRIEF="# BRIEF\n\n" + "\n".join(
                [f"**目标**：{brief.get('goal','')}", f"**理解**：{brief.get('interpretation','')}",
                 "**替换**：" + "；".join(brief.get("replace") or []), "**保留**：" + "；".join(brief.get("keep") or []),
                 "**事实**：" + "；".join(brief.get("facts") or []), "", f"> 用户原话：{prompt}"]),
                TREATMENT="# TREATMENT\n\n" + (f"**新风格**：{d.get('style', '')}\n\n" if d.get("style") else "")
                + d["treatment"] + ("\n\n## 造型改动\n" + looks if looks.strip() else ""))
        done = self.node("script")
        if done and done["status"] == "ready":
            return d, done["script"]
        if tpl is not None and (d.get("changes") or {}).get("script") == "keep":
            s = json.loads(json.dumps(tpl["script"]))
            self.save("script", "script", "ready", {"script": s, "from_template": self.template_id})
            self.documents(SCRIPT=director.script_markdown(s, d) if s.get("mode") != "reuse" else
                           "# SCRIPT\n\n沿用模板：原声、分段与包装不变。")
            self.progress(32, f"沿用模板台词与包装：{len(s['segments'])} 段表演")
            return d, s
        if d.get("audio_strategy") == "reuse":
            self.progress(28, "保留原声与节奏：正在按原片切分段落并重建图形包装")
            s = await director.write_reuse(prompt, a, d, self.ledger)
        else:
            self.progress(28, "正在按原片结构编写新台词与包装方案")
            s = await director.write_script(prompt, a, d, self.ledger)
        self.save("script", "script", "ready", {"script": s})
        self.documents(SCRIPT=director.script_markdown(s, d))
        self.progress(32, f"新台词已完成：{len(s['segments'])} 段表演")
        return d, s

    # 3. pictures and voices ------------------------------------------------------------
    async def stage_assets(self, a: dict, d: dict, s: dict) -> dict[str, Path]:
        self.progress(35, "正在生成机位图、图标、B-roll 与音色参考")
        cast = {c["id"]: c for c in d["cast"]}
        capture = kits.capture_for(a)
        # The requested look (the user's words and the director's style) leads every picture.
        prompt_text = str(self.options.get("prompt") or "")
        style = str(d.get("style") or "")
        if kits.ILLUSTRATED.search(prompt_text) and not kits.ILLUSTRATED.search(style):
            style = (style + "；" if style else "") + "按用户要求的画风：" + prompt_text[:120]
        uploads = [i for i in self.inputs if i.get("field") == "images"]
        upload_paths = {}
        for n, item in enumerate(uploads, 1):
            upload_paths[n] = await providers.fetch_input(self.row, item, self.work / f"upload-{n}{Path(item['filename']).suffix or '.png'}")
        source = await self.source_path()
        source_setups = {x["id"]: x for x in a["setups"]}
        semaphore = image_slots()
        results: dict[str, Path] = {}

        async def layout_frame(setup: dict) -> Path | None:
            src = source_setups.get(setup.get("source") or "")
            if not src:
                return None
            target = self.work / f"layout-{setup['id']}.jpg"
            if not target.is_file():
                shot = next((x for x in a["shots"] if x["setup"] == src["id"] and x["start"] <= src["best_time"] <= x["end"]),
                            next((x for x in a["shots"] if x["setup"] == src["id"]), None))
                lo, hi = (shot["start"], shot["end"]) if shot else (0, a["duration"])
                await asyncio.to_thread(media.best_frame, source, target, src["best_time"], lo, hi, self.work)
            sketch = self.work / f"sketch-{setup['id']}.jpg"
            if not sketch.is_file():
                await asyncio.to_thread(media.composition_sketch, target, sketch, a.get("layout"))
            return target

        async def setup_image(setup: dict, main: Path | None, fix: str = "", tag: str = "") -> Path:
            layout = await layout_frame(setup)
            inputs: list[tuple[str, str]] = []
            refs: list[Path] = []
            if main is not None:
                inputs.append(("main", ""))
                refs.append(main)
            identities = []
            for cid in setup["visible"]:
                identity = cast[cid]["identity"]
                if identity.startswith("upload:"):
                    path = upload_paths.get(int(identity.split(":")[1]))
                    if path:
                        inputs.append(("identity", cast[cid].get("label", cid)))
                        refs.append(path)
                        identities.append((cast[cid].get("label", cid), path))
            for product in d.get("products") or []:
                if str(product.get("identity", "")).startswith("upload:"):
                    path = upload_paths.get(int(product["identity"].split(":")[1]))
                    if path:
                        inputs.append(("product", product.get("label", "")))
                        refs.append(path)
            kept = [cid for cid in setup["visible"] if cast[cid]["identity"] == "keep"]
            if layout is not None and kept and d.get("keep_place"):
                # The request keeps these people and the place: edit the real frame instead of repainting it.
                changes = [f"the product becomes {p.get('label', '')} ({p.get('description', '')})"
                           for p in d.get("products") or [] if p.get("identity") != "keep"]
                changes += [f"{cast[c].get('label', c)} becomes the new character described above"
                            for c in setup["visible"] if cast[c]["identity"] != "keep"]
                inputs.append(("keep", "; ".join(changes)))
                refs.append(layout)
            elif layout is not None:
                inputs.append(("layout", ""))
                refs.append(self.work / f"sketch-{setup['id']}.jpg")
            prompt = kits.setup_image_prompt(capture=capture, setup=setup, cast=cast, setting=d["setting"], inputs=inputs,
                                             framing_note=self.framing_note, style=style)

            redesign = [cid for cid in setup["visible"] if cast[cid]["identity"] == "generate"]

            async def judge(path: Path) -> dict:
                return await qa.review_setup_image(path, setup=setup, cast=cast, layout=layout, main=main,
                                                   identities=identities, ledger=self.ledger,
                                                   redesigned=[cast[c].get("label", c) for c in redesign], style=style)
            if not tag:
                return await self.image_node(f"setup-{setup['id']}", prompt, refs, self.gen_ratio, judge, semaphore)
            # A redraw is its own node (fresh request ids), then becomes the setup every take uses.
            key = f"setup-{setup['id']}-{tag}"
            path = await self.image_node(key, kits.fit_image_prompt(prompt, [fix]), refs, self.gen_ratio, judge, semaphore)
            redrawn = {k: v for k, v in (self.node(key) or {}).items() if k != "status"}
            self.save(f"setup-{setup['id']}", "image", "ready", {**redrawn, "redrawn_for": fix})
            return path

        main_setup = d["setups"][0]
        results[f"setup-{main_setup['id']}"] = main_path = await setup_image(main_setup, None)

        jobs = {f"setup-{x['id']}": setup_image(x, main_path) for x in d["setups"][1:]}
        for n, comp in enumerate(s["components"]):
            if comp.get("type") == "broll" and comp.get("prompt"):
                refs, text = [], kits.broll_prompt(comp["prompt"], capture, style)
                src = comp.get("source")
                if isinstance(src, list) and len(src) == 2:
                    # The reference's own insert shows what the picture must mean; redraw it cleanly.
                    ref = self.work / f"broll-ref-{n}.jpg"
                    if not ref.is_file():
                        await asyncio.to_thread(media.frame, source, ref, (float(src[0]) + float(src[1])) / 2)
                        layout = a.get("layout") or {}
                        if layout.get("type") == "panel":
                            from PIL import Image
                            with Image.open(ref) as im:
                                im.crop((0, int(im.height * layout["top"]), im.width,
                                         int(im.height * layout["bottom"]))).save(ref, quality=92)
                    refs = [ref]
                    text = ("Input image 1 is the inserted illustration from the reference video. Recreate the same "
                            "subject, layout and meaning as a clean new picture in the remake's style; do not copy any "
                            "text, numbers, logos, account names or watermarks from it.\n\n" + text)
                jobs[f"broll-{n}"] = self.image_node(f"broll-{n}", kits.fit_image_prompt(text, []), refs,
                                                     self.ratio if comp.get("layout") == "full" else "4:3", None, semaphore)
            if comp.get("type") == "reveal_strip":
                for k, item in enumerate(comp.get("items") or []):
                    jobs[f"icon-{n}-{k}"] = self.image_node(f"icon-{n}-{k}", kits.icon_prompt(item.get("icon") or item.get("label") or "a star"),
                                                            [], "1:1", None, semaphore)
        voice_jobs = {} if s.get("mode") == "reuse" else {
            c["id"]: self.voice_node(c, a, source) for c in d["cast"] if c.get("speaks") and c["voice"].startswith("source:")}
        values = await asyncio.gather(*jobs.values(), *voice_jobs.values(), return_exceptions=True)
        for key, value in zip(list(jobs) + [f"voice-{k}" for k in voice_jobs], values):
            if isinstance(value, Exception):
                if key.startswith("setup-"):
                    raise value
                self.warn(f"{key} 生成失败，已跳过：{value}")
                continue
            if value is not None:
                results[key] = value
        await self.consistent_setups(d, cast, results, setup_image, main_path)
        self.progress(52, "素材已生成并通过质检")
        return results

    async def consistent_setups(self, d: dict, cast: dict, results: dict[str, Path], setup_image, main_path: Path) -> None:
        """Gate before any take is paid for: every character must look the same across all camera setups.
        Outliers are redrawn (a few cents each) instead of letting every take that uses them fail."""
        done = self.node("setup-consistency")
        if done and done.get("status") == "ready":
            return
        people = [x for x in d["setups"] if x["visible"] and f"setup-{x['id']}" in results]
        found: list[dict] = []
        if len(people) >= 2:
            casting = "\n".join(f"- {c.get('label', c['id'])}: {c.get('person', '')[:300]}" for c in d["cast"])
            for round_no in (1, 2):
                self.progress(50, f"正在检查各机位图的人物造型是否一致（第 {round_no} 轮）")
                outliers = await qa.review_setup_consistency(
                    [(x["id"], results[f"setup-{x['id']}"], [cast[c].get("label", c) for c in x["visible"]]) for x in people],
                    casting, self.ledger)
                ids = list(dict.fromkeys(o["setup"] for o in outliers))
                if not ids or len(ids) >= len(people):
                    break  # consistent, or a verdict that blames every picture is not trustworthy
                found += outliers
                for sid in ids:
                    setup = next(x for x in d["setups"] if x["id"] == sid)
                    fix = ("Match the written casting exactly: " +
                           "; ".join(f"{o.get('character')}: {o.get('problem')}" for o in outliers if o["setup"] == sid))
                    main = None if sid == d["setups"][0]["id"] else main_path
                    try:
                        results[f"setup-{sid}"] = await setup_image(setup, main, fix=fix, tag=f"c{round_no}")
                    except providers.ProviderError as exc:
                        self.warn(f"setup-{sid} 造型不一致，重画失败：{exc}")
                    if sid == d["setups"][0]["id"]:
                        main_path = results[f"setup-{sid}"]
                self.warn_free(f"机位图造型一致性检查：重画 {', '.join(ids)}")
        self.save("setup-consistency", "qa", "ready", {"outliers": found})

    async def image_node(self, key: str, prompt: str, refs: list[Path], ratio: str, judge, semaphore) -> Path:
        """Generate candidates in parallel, review each, keep the best; resumes submitted requests."""
        target = self.work / f"{key}.png"
        state = self.node(key) or {}
        if state.get("status") == "ready" and state.get("stored"):
            return await providers.fetch(state["stored"], target)
        attempts: list[dict] = state.get("attempts") or []
        width = IMAGE_CANDIDATES if judge else 1
        rounds = MAX_ROUNDS if judge else 1

        async def finish(current: dict, n: int) -> None:
            data, meta = await providers.image_wait(current["remote"])
            self.ledger.add("image2", time.time() - float(current.get("submitted_at") or time.time()), 0, 0, _image_cost(meta))
            path = self.work / f"{key}-{n}.png"
            path.write_bytes(data)
            current.update({"file": path.name, "cost": meta.get("cost")})
            current["stored"] = await providers.put(self.row, path, f"{key}-{n}.png", "image/png")
            if judge is None:
                current["qa"] = {"pass": True, "score": 10}
            else:
                try:
                    current["qa"] = await judge(path)
                except llm.ModelError as exc:
                    current["qa"] = {"pass": True, "score": qa.PASS_SCORE, "issues": [], "error": str(exc)}
            current["done"] = True

        async with semaphore:
            for round_no in range(1, rounds + 1):
                if any((x.get("qa") or {}).get("pass") for x in attempts if x.get("done")):
                    break
                batch = [x for x in attempts if x.get("round", 1) == round_no]
                if not batch:
                    issues = list(dict.fromkeys(i for x in attempts for i in (x.get("qa") or {}).get("issues", [])))[-6:]
                    text = kits.fit_image_prompt(prompt, issues)
                    for k in range(width):
                        try:
                            remote = await providers.image_submit(text, ratio, refs, f"{self.run_tag}-{key}-{round_no}-{k}")
                        except providers.ProviderError:
                            if k == 0:
                                raise
                            break  # one candidate is enough to continue
                        batch.append({"remote": remote, "prompt": text, "round": round_no, "submitted_at": time.time()})
                    attempts.extend(batch)
                    self.save(key, "image", "generating", {"attempts": attempts, "prompt": prompt})
                pending = [(x, attempts.index(x) + 1) for x in batch if not x.get("done")]
                results = await asyncio.gather(*(finish(x, n) for x, n in pending), return_exceptions=True)
                for (x, _), result in zip(pending, results):
                    if isinstance(result, Exception):
                        x.update({"done": True, "error": str(result), "qa": {"pass": False, "score": 0, "issues": []}})
                self.save(key, "image", "generating", {"attempts": attempts, "prompt": prompt})
        done = [x for x in attempts if x.get("done") and x.get("stored")]
        if not done:
            raise providers.ProviderError(f"{key} 没有生成结果：{attempts[-1].get('error') if attempts else ''}")
        best = max(done, key=lambda x: (bool((x.get("qa") or {}).get("pass")), float((x.get("qa") or {}).get("score") or 0)))
        if not (best.get("qa") or {}).get("pass"):
            self.warn(f"{key} 质检未完全通过（{(best.get('qa') or {}).get('score')} 分），采用最好的一版")
        self.save(key, "image", "ready", {"attempts": attempts, "prompt": prompt, "stored": best["stored"],
                                           "qa": best.get("qa")})
        return await providers.fetch(best["stored"], target)

    async def product_refs(self, d: dict) -> tuple[list[dict], list[str]]:
        """Uploaded product pictures, as extra H3 picture references after the camera setups."""
        products, urls = [], []
        images = [i for i in self.inputs if i.get("field") == "images"]
        for product in d.get("products") or []:
            identity = str(product.get("identity", ""))
            if not identity.startswith("upload:"):
                continue
            n = int(identity.split(":")[1])
            if not 1 <= n <= len(images):
                continue
            key = f"product-{n}"
            state = self.node(key)
            if not state:
                item = images[n - 1]
                path = await providers.fetch_input(self.row, item, self.work / f"upload-{n}{Path(item['filename']).suffix or '.png'}")
                state = {"stored": await providers.put(self.row, path, f"{key}{path.suffix}", item.get("content_type") or "image/png")}
                self.save(key, "product", "ready", state)
            products.append(product)
            urls.append(providers.url_for(state["stored"]))
        return products, urls

    async def voice_node(self, character: dict, a: dict, source: Path) -> Path | None:
        key = f"voice-{character['id']}"
        target = self.work / f"{key}.wav"
        state = self.node(key)
        if state and state.get("status") == "ready":
            return await providers.fetch(state["stored"], target)
        speaker = character["voice"].split(":", 1)[1]
        spans = [(l["start"], l["end"]) for l in a["lines"] if l["speaker"] == speaker]
        if not spans:
            return None
        seconds = await asyncio.to_thread(media.voice_sample, source, target, spans)
        stored = await providers.put(self.row, target, f"{key}.wav", "audio/wav")
        self.save(key, "voice", "ready", {"stored": stored, "seconds": seconds, "source_speaker": speaker})
        return target

    # 4. performances -----------------------------------------------------------------
    async def stage_takes(self, a: dict, d: dict, s: dict, pictures: dict[str, Path]) -> list[dict]:
        cast = {c["id"]: c for c in d["cast"]}
        setups = {x["id"]: x for x in d["setups"]}
        speaker_ids: dict[str, int] = {}
        for seg in s["segments"]:
            for line in seg["lines"]:
                speaker_ids.setdefault(line["role"], len(speaker_ids) + 1)
        stored = {k: (self.node(k) or {}).get("stored") for k in list(pictures)}
        total = len(s["segments"])
        finished = 0
        slots = user_slots(self.row["merchant_id"])
        fmt = a.get("format") or "dialogue scene"

        async def one(seg: dict) -> dict:
            nonlocal finished
            held = await slots.acquire(TAKE_CANDIDATES)
            try:
                if seg.get("mode") == "reuse":
                    result = await self.motion_take_node(seg, a, cast, setups, speaker_ids, stored, pictures, d)
                else:
                    result = await self.take_node(seg, cast, setups, speaker_ids, stored, pictures, fmt, d["setting"])
            finally:
                await slots.release(held)
            finished += 1
            self.progress(52 + int(36 * finished / total), f"表演片段已完成 {finished}/{total}")
            return result

        self.progress(54, f"正在并发生成 {total} 段表演（本账号最多 {slots.size} 个同时生成）")
        return list(await asyncio.gather(*(one(seg) for seg in s["segments"])))

    async def take_node(self, seg: dict, cast: dict, setups: dict, speaker_ids: dict, stored: dict,
                        pictures: dict[str, Path], fmt: str, setting: str) -> dict:
        key = f"take-{seg['id']}"
        state = self.node(key) or {}
        if state.get("status") == "ready":
            return state["chosen"]
        used = []
        for line in seg["lines"]:
            if line["setup"] not in used and stored.get(f"setup-{line['setup']}"):
                used.append(line["setup"])
        # Product/object shots have no speaker; offer them as the passage's cutaways so the model never
        # invents another place for them.
        for sid, x in setups.items():
            if not x["visible"] and sid not in used and stored.get(f"setup-{sid}"):
                used.append(sid)
        used = used[:7]  # H3 takes at most 9 pictures: leave room for product references
        speaking = []
        for line in seg["lines"]:
            if line["role"] not in speaking:
                speaking.append(line["role"])
        audio_for, audio_urls = {}, []
        for cid in speaking:
            voice = self.node(f"voice-{cid}")
            if voice and voice.get("stored") and len(audio_urls) < 3:
                audio_for[cid] = len(audio_urls) + 1
                audio_urls.append(providers.url_for(voice["stored"]))
        image_urls = [providers.url_for(stored[f"setup-{sid}"]) for sid in used]
        products, product_urls = await self.product_refs(self.direction)
        image_urls += product_urls
        pace = float(seg.get("pace") or 4.6)
        estimates = [spoken_units(strip_markup(l["text"], spoken=True)) / pace + 0.3 for l in seg["lines"]]
        expected = "".join(strip_markup(l["text"], spoken=True) for l in seg["lines"])
        cast_lines = " / ".join(f"{_speaker_label(cast, l)}: {strip_markup(l['text'], spoken=True)}"
                                for l in seg["lines"])
        attempts: list[dict] = state.get("attempts") or []
        seed = int(self.payload.get("seed") or 0) + int(seg["id"][-2:]) * 101

        async def finish(current: dict) -> None:
            artifact, billing = await providers.h3_wait(self.row, current["job_id"], current["remote"])
            async with self._lock:
                self.billing += billing
            current.update({"artifact": artifact, "billing": billing})
            path = await providers.fetch_artifact(artifact, self.work / f"{current['job_id']}.mp4")
            info = media.probe(path)
            artifact_url = providers.artifact_url(artifact, self.row["id"])
            try:
                transcript = await self.transcribe(artifact_url, info["duration"])
            except Exception as exc:  # noqa: BLE001
                transcript = {}
                self.warn(f"{seg['id']} 语音转写失败：{exc}")
            sentences = asr_sentences(transcript)
            chars = timed_chars(sentences)
            verdict = await qa.review_take(
                artifact_url, expected_speech=expected, heard_speech=transcript_text(sentences),
                duration=info["duration"], speech_end=chars[-1][2] if chars else 0.0,
                setup_images=[(sid, pictures[f"setup-{sid}"]) for sid in used if f"setup-{sid}" in pictures],
                cast_lines=cast_lines, ledger=self.ledger)
            if not verdict["pass"]:
                verdict = await qa.confirm_take(artifact_url, verdict, cast_lines=cast_lines,
                                                people=_casting(cast), ledger=self.ledger)
            current.update({"done": True, "transcript": transcript, "qa": verdict, "duration_actual": info["duration"]})

        for round_no in range(1, MAX_ROUNDS + 1):
            if any((x.get("qa") or {}).get("pass") for x in attempts if x.get("done")):
                break
            if any((x.get("qa") or {}).get("input_problem") for x in attempts if x.get("done")):
                self.warn(f"{seg['id']} 的画面按文字设定生成正确，只与某张机位图不一致；重做视频无法修复，已停止重做")
                break
            batch = [x for x in attempts if x.get("round", 1) == round_no]
            if not batch:
                issues = [i for x in attempts for i in (x.get("qa") or {}).get("issues", [])[:4]]
                duration = seg["request_duration"] + (1 if any("cut off" in i for i in issues) else 0)
                duration = min(15, duration)
                prompt = kits.performance_prompt(
                    segment={**seg, "request_duration": duration}, cast=cast, setups=setups, pictures=used,
                    audio_for=audio_for, speaker_ids=speaker_ids, setting=setting, fmt=fmt, ratio=self.ratio,
                    estimates=estimates, corrections="; ".join(issues)[:900], products=products,
                    style=str(self.direction.get("style") or ""))
                for k in range(TAKE_CANDIDATES):
                    n = len(attempts) + 1
                    job_id = f"{self.run_tag}-{seg['id']}-{n}"
                    remote = await providers.h3_submit(self.row, job_id, prompt, duration, self.gen_ratio,
                                                       seed + 7919 * n, image_urls, audio_urls)
                    attempt = {"job_id": job_id, "remote": remote, "prompt": prompt, "duration": duration,
                               "round": round_no, "submitted_at": time.time()}
                    attempts.append(attempt)
                    batch.append(attempt)
                self.save(key, "take", "running", {"attempts": attempts, "setups": used})
            pending = [x for x in batch if not x.get("done")]
            results = await asyncio.gather(*(finish(x) for x in pending), return_exceptions=True)
            for x, result in zip(pending, results):
                if isinstance(result, Exception):
                    x.update({"done": True, "error": str(result), "qa": {"pass": False, "score": 0, "issues": []}})
            self.save(key, "take", "running", {"attempts": attempts, "setups": used})
            scores = " / ".join(str((x.get("qa") or {}).get("score")) for x in batch)
            self.progress(55, f"{seg['id']} 第 {round_no} 次生成：质检 {scores} 分")
        usable = [x for x in attempts if x.get("artifact")]
        if not usable:
            raise providers.ProviderError(f"{seg['id']} 多次生成均失败：{attempts[-1].get('error') if attempts else ''}")
        best = max(usable, key=lambda x: (bool((x.get("qa") or {}).get("pass")), float((x.get("qa") or {}).get("score") or 0)))
        if not (best.get("qa") or {}).get("pass"):
            self.warn(f"{seg['id']} 质检未完全通过（{best['qa'].get('score')} 分）：{'; '.join(best['qa'].get('issues', [])[:2])}")
        chosen = {"segment": seg["id"], "artifact": best["artifact"], "transcript": best.get("transcript") or {},
                  "qa": best.get("qa"), "job_id": best["job_id"]}
        self.save(key, "take", "ready", {"attempts": attempts, "setups": used, "chosen": chosen})
        return chosen

    async def motion_take_node(self, seg: dict, a: dict, cast: dict, setups: dict, speaker_ids: dict, stored: dict,
                               pictures: dict[str, Path], d: dict) -> dict:
        """Reuse mode: the new cast performs the reference passage's movement to its original soundtrack."""
        key = f"take-{seg['id']}"
        state = self.node(key) or {}
        if state.get("status") == "ready":
            return state["chosen"]
        used = [sid for sid in seg.get("setup_flow") or [] if stored.get(f"setup-{sid}")][:7] or \
            [next(k[6:] for k in stored if k.startswith("setup-") and stored[k])]
        source = await self.source_path()
        duration = int(seg["request_duration"])
        start = float(seg["source_start"])
        span = min(float(duration), max(1.0, a["duration"] - start))
        clip_path = await asyncio.to_thread(
            media.motion_reference, source, self.work / f"motion-{seg['id']}.mp4", start, float(duration),
            float(a.get("content_end") or a["duration"]), a.get("layout"), media.graphic_masks(a, a.get("layout")),
            [(sh["start"] - start, sh["end"] - start) for sh in a.get("shots") or []
             if sh.get("kind") == "insert" and sh["end"] > start and sh["start"] < start + duration])
        audio_path = await asyncio.to_thread(media.excerpt, source, self.work / f"audio-{seg['id']}.wav", start, span, audio=True)
        refs = state.get("refs") or {
            "video": await providers.put(self.row, clip_path, f"motion-{seg['id']}.mp4", "video/mp4"),
            "audio": await providers.put(self.row, audio_path, f"audio-{seg['id']}.wav", "audio/wav"),
        }
        image_urls = [providers.url_for(stored[f"setup-{sid}"]) for sid in used]
        products, product_urls = await self.product_refs(d)
        image_urls += product_urls
        expected = "".join(strip_markup(l["text"], spoken=True) for l in seg["lines"])
        cast_lines = " / ".join(f"{_speaker_label(cast, l)}: {l['text']}" for l in seg["lines"]) or "(no speech)"
        attempts: list[dict] = state.get("attempts") or []
        seed = int(self.payload.get("seed") or 0) + int(seg["id"][-2:]) * 101

        async def finish(current: dict) -> None:
            artifact, billing = await providers.h3_wait(self.row, current["job_id"], current["remote"])
            async with self._lock:
                self.billing += billing
            current["artifact"], current["billing"] = artifact, billing
            path = await providers.fetch_artifact(artifact, self.work / f"{current['job_id']}.mp4")
            info = media.probe(path)
            artifact_url = providers.artifact_url(artifact, self.row["id"])
            heard = ""
            if expected:
                try:
                    heard = transcript_text(asr_sentences(await self.transcribe(artifact_url,
                                                                                  info["duration"])))
                except Exception:  # noqa: BLE001
                    heard = expected
            verdict = await qa.review_take(
                artifact_url, expected_speech=expected, heard_speech=heard or expected,
                duration=info["duration"], speech_end=0.0,
                setup_images=[(sid, pictures[f"setup-{sid}"]) for sid in used if f"setup-{sid}" in pictures],
                cast_lines=cast_lines, ledger=self.ledger, motion=True)
            # Measured, not judged: a clip far greyer than its keyframes copied the grey movement guide.
            keyframes = [media.saturation(pictures[f"setup-{sid}"]) for sid in used if f"setup-{sid}" in pictures]
            if keyframes:
                keyframe_chroma = sorted(keyframes)[len(keyframes) // 2]
                clip_chroma = await asyncio.to_thread(media.video_saturation, path, self.work)
                if keyframe_chroma > 20 and clip_chroma < keyframe_chroma * 0.45:
                    verdict = {**verdict, "pass": False, "score": min(float(verdict["score"]), 3.0), "issues": [
                        "The clip lost the colour of the keyframe pictures (it copied the grey movement guide); every "
                        "frame must have the full colour, detail and light of the keyframe pictures."] + verdict["issues"]}
            if not verdict["pass"]:
                verdict = await qa.confirm_take(artifact_url, verdict, cast_lines=cast_lines,
                                                people=_casting(cast), ledger=self.ledger)
            current.update({"done": True, "qa": verdict, "duration_actual": info["duration"]})

        for round_no in range(1, MAX_ROUNDS + 1):
            if any((x.get("qa") or {}).get("pass") for x in attempts if x.get("done")):
                break
            if any((x.get("qa") or {}).get("input_problem") for x in attempts if x.get("done")):
                self.warn(f"{seg['id']} 的画面按文字设定生成正确，只与某张机位图不一致；重做视频无法修复，已停止重做")
                break
            batch = [x for x in attempts if x.get("round", 1) == round_no]
            if not batch:
                issues = [i for x in attempts for i in (x.get("qa") or {}).get("issues", [])[:4]]
                prompt = kits.motion_performance_prompt(
                    segment=seg, cast=cast, setups=setups, pictures=used, speaker_ids=speaker_ids,
                    setting=d["setting"] + (f" Visual style: {d['style']}." if d.get("style") else ""),
                    ratio=self.gen_ratio, duration=duration, reuse_audio=True, corrections="; ".join(issues)[:900],
                    framing_note=self.framing_note, products=products, style=str(d.get("style") or ""),
                    lyrics=[x for x in a.get("lyrics") or [] if x.get("kind") in {"lyrics", "background"}
                            and start <= float(x["start"]) < start + duration])
                for _ in range(TAKE_CANDIDATES):
                    n = len(attempts) + 1
                    job_id = f"{self.run_tag}-{seg['id']}-{n}"
                    remote = await providers.h3_submit(
                        self.row, job_id, prompt, duration, self.gen_ratio, seed + 7919 * n, image_urls,
                        [providers.url_for(refs["audio"])], [providers.url_for(refs["video"])])
                    attempt = {"job_id": job_id, "remote": remote, "prompt": prompt, "duration": duration,
                               "round": round_no, "submitted_at": time.time()}
                    attempts.append(attempt)
                    batch.append(attempt)
                self.save(key, "take", "running", {"attempts": attempts, "setups": used, "refs": refs})
            pending = [x for x in batch if not x.get("done")]
            results = await asyncio.gather(*(finish(x) for x in pending), return_exceptions=True)
            for x, result in zip(pending, results):
                if isinstance(result, Exception):
                    x.update({"done": True, "error": str(result), "qa": {"pass": False, "score": 0, "issues": []}})
            self.save(key, "take", "running", {"attempts": attempts, "setups": used, "refs": refs})
            scores = " / ".join(str((x.get("qa") or {}).get("score")) for x in batch)
            self.progress(55, f"{seg['id']} 第 {round_no} 次生成：质检 {scores} 分")
        usable = [x for x in attempts if x.get("artifact")]
        if not usable:
            raise providers.ProviderError(f"{seg['id']} 多次生成均失败：{attempts[-1].get('error') if attempts else ''}")
        best = max(usable, key=lambda x: (bool((x.get("qa") or {}).get("pass")), float((x.get("qa") or {}).get("score") or 0)))
        if not (best.get("qa") or {}).get("pass"):
            self.warn(f"{seg['id']} 质检未完全通过（{best['qa'].get('score')} 分）：{'; '.join(best['qa'].get('issues', [])[:2])}")
        chosen = {"segment": seg["id"], "artifact": best["artifact"], "transcript": {}, "qa": best.get("qa"),
                  "job_id": best["job_id"]}
        self.save(key, "take", "ready", {"attempts": attempts, "setups": used, "refs": refs, "chosen": chosen})
        return chosen

    # 5. composition and delivery -----------------------------------------------------
    async def stage_render(self, a: dict, d: dict, s: dict, pictures: dict[str, Path], takes: list[dict]) -> None:
        self.progress(90, "正在对齐台词并合成字幕、包装与音效")
        clips, lines_per, times_per = [], [], []
        from .align import align_lines
        reuse = s.get("mode") == "reuse"
        source_chars = timed_chars([{"words": [{"start": w["start"], "end": w["end"], "text": w["text"]}
                                               for l in a["lines"] for w in l.get("words") or []]}]) if reuse else []
        for seg, take in zip(s["segments"], takes):
            if reuse:
                path = await providers.fetch_artifact(take["artifact"], self.work / f"{take['job_id']}.mp4")
                a0, b0 = float(seg["source_start"]), float(seg["source_end"])
                length = min(b0 - a0, media.probe(path)["duration"])
                chars = [(c, t0 - a0, t1 - a0) for c, t0, t1 in source_chars if a0 <= t0 < b0]
                lines = [parse_line(l["role"], l["text"]) for l in seg["lines"]]
                clips.append(TakeClip(path, 0.0, round(length, 3)))
                lines_per.append(lines)
                times_per.append(align_lines(lines, chars, length))
                continue
            path = await providers.fetch_artifact(take["artifact"], self.work / f"{take['job_id']}.mp4")
            duration = media.probe(path)["duration"]
            chars = timed_chars(asr_sentences(take.get("transcript") or {}))
            lines = [parse_line(l["role"], l["text"]) for l in seg["lines"]]
            times = align_lines(lines, chars, duration)
            first = chars[0][1] if chars else 0.0
            last = chars[-1][2] if chars else duration
            start, end = plan_trims(duration, first, last, await asyncio.to_thread(media.scene_cuts, path))
            clips.append(TakeClip(path, start, end))
            lines_per.append(lines)
            times_per.append(times)
        music = pick_music(s.get("music") or {})
        images = {k: v for k, v in pictures.items() if k.startswith(("icon-", "broll-"))}
        render_dir = self.work / "render"
        render_dir.mkdir(exist_ok=True)
        target = self.work / "reenact-final.mp4"
        extra = {}
        if reuse:
            source = await self.source_path()
            extra = {"source_ranges": [(float(x["source_start"]), float(x["source_end"])) for x in s["segments"]],
                     "audio_source": (source, 0.0, float(s["segments"][-1]["source_end"]))}
            music = None
        extra["layout"] = a.get("layout")
        _, tl = await asyncio.to_thread(render, clips, lines_per, times_per, s, images, self.ratio,
                                        render_dir, target, music, None, **extra)
        # Review the finished picture; move Caption once if it covers faces.
        try:
            probes = []
            times = [(c.start + c.end) / 2 for c in tl.cues[:: max(1, len(tl.cues) // 4)][:4]] or \
                [tl.duration * k / 5 for k in range(1, 5)]
            for i, t in enumerate(times):
                probes.append((t, media.frame(target, render_dir / f"qa-{i}.jpg", t)))
            verdict = await qa.review_render(probes, self.ledger)
            if verdict.get("caption_covers_faces"):
                position = "middle" if (s.get("captions") or {}).get("position") != "middle" else "bottom"
                await asyncio.to_thread(render, clips, lines_per, times_per, s, images, self.ratio, render_dir,
                                        target, music, position, **extra)
                verdict["rerendered_caption_position"] = position
        except Exception as exc:  # noqa: BLE001
            verdict = {"error": str(exc)}
        self.save("render", "render", "ready", {"qa": verdict, "duration": tl.duration,
                                                "moments": tl.moments, "selections": tl.selections,
                                                "cues": [[c.start, c.end, c.role, c.text] for c in tl.cues]})
        await self.reconcile_billing()
        await asyncio.to_thread(self.deliver, target)

    async def reconcile_billing(self) -> None:
        """GPU billing is often filled in after a job ends; read every attempt's final cost."""
        import gongji
        from database import connect
        with connect() as db:
            rows = db.execute("SELECT output_json FROM video_project_nodes WHERE project_id=? AND node_type='take'",
                              (self.project_id,)).fetchall()
        total = 0.0
        for row in rows:
            for attempt in json.loads(row["output_json"] or "{}").get("attempts") or []:
                if not attempt.get("remote"):
                    continue
                try:
                    info = await gongji.job_status(attempt["remote"])
                    total += float(info.get("billing") or 0)
                except Exception:  # noqa: BLE001
                    total += float(attempt.get("billing") or 0)
        self.billing = max(self.billing, total)

    def deliver(self, target: Path) -> None:
        from commerce import store_output
        from database import public_id, settle_task, transaction
        import video_projects

        artifact = store_output(self.row, target, "video")
        now = time.time()
        input_expiry = now + int(os.getenv("H3_INPUT_RETENTION_SECONDS", "86400"))
        output_expiry = now + int(os.getenv("H3_OUTPUT_RETENTION_SECONDS", "259200"))
        self.flush_ledger()
        self.payload["_reenact"] = self.options
        cost = round(self.billing + self.options["model_cost"], 6)
        with transaction() as db:
            changed = db.execute(
                """UPDATE merchant_tasks SET status='completed',progress=100,message=?,artifacts=?,finished_at=?,
                input_expires_at=?,output_expires_at=?,payload=?,provider_billing=? WHERE id=? AND status='running'""",
                ("视频重演完成" + ("（有质检提示）" if self.options.get("warnings") else ""),
                 json.dumps([artifact], ensure_ascii=False), now, input_expiry, output_expiry,
                 json.dumps(self.payload, ensure_ascii=False, separators=(",", ":")), cost, self.task_id),
            )
            if not changed.rowcount:
                return
            db.execute("UPDATE assets SET expires_at=? WHERE task_id=? AND role='input' AND expires_at IS NULL",
                       (input_expiry, self.task_id))
            db.execute("UPDATE assets SET expires_at=? WHERE task_id=? AND role='output' AND expires_at IS NULL",
                       (output_expiry, self.task_id))
            db.execute(
                """INSERT OR IGNORE INTO assets
                (id,merchant_id,task_id,role,bucket,object_key,filename,content_type,size_bytes,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (public_id("ast"), self.row["merchant_id"], self.task_id, "output",
                 "local" if artifact.get("storage") == "local" else artifact.get("bucket", "cos"),
                 artifact.get("path") or artifact.get("key"), artifact["name"], "video/mp4",
                 artifact["size_bytes"], now, output_expiry),
            )
            settle_task(db, self.task_id, "success", note="Hypit 重演完成")
            video_projects.mark_run(db, self.task_id, status="completed", result={"artifacts": [artifact]})
        if os.getenv("H3_REENACT_KEEP_WORK", "0") != "1":
            shutil.rmtree(self.work, ignore_errors=True)


def pick_music(music: dict) -> Path | None:
    """Optional music bed from H3_REENACT_MUSIC_DIR: files named by energy (low/mid/high)."""
    folder = Path(os.getenv("H3_REENACT_MUSIC_DIR", "/data/reenact-music"))
    if not folder.is_dir():
        return None
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".aac"})
    if not files:
        return None
    energy = str(music.get("energy") or "mid")
    matching = [p for p in files if p.name.lower().startswith(energy)] or files
    return matching[hash(str(music.get("mood"))) % len(matching)]
