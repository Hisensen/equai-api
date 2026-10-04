"""Reenactment templates: a finished task's reference understanding, Script, packaging,
pictures and voices, frozen so new prompts adapt them instead of starting over.

A template lives for TEMPLATE_TTL (3 days by default); every asset it references is kept
for the same period and then cleaned up with it.
"""
from __future__ import annotations

import json
import os
import time

from database import connect, public_id, transaction

TEMPLATE_TTL = int(os.getenv("H3_REENACT_TEMPLATE_TTL_SECONDS", str(3 * 86400)))
KEEP_NODES = ("analysis", "direction", "script")
ASSET_PREFIXES = ("setup-", "broll-", "icon-", "voice-")


class TemplateError(RuntimeError):
    pass


def _nodes(db, project_id: str) -> dict[str, dict]:
    rows = db.execute("SELECT node_key,node_type,status,output_json FROM video_project_nodes WHERE project_id=?",
                      (project_id,)).fetchall()
    return {r["node_key"]: {"type": r["node_type"], "status": r["status"], **json.loads(r["output_json"] or "{}")}
            for r in rows}


def save(merchant_id: str, task_id: str, name: str, note: str = "") -> dict:
    now = time.time()
    with transaction() as db:
        task = db.execute("SELECT * FROM merchant_tasks WHERE id=? AND merchant_id=?", (task_id, merchant_id)).fetchone()
        if not task:
            raise TemplateError("任务不存在")
        payload = json.loads(task["payload"] or "{}")
        if payload.get("_tool") != "reenact" or task["status"] != "completed":
            raise TemplateError("只有已完成的视频重演任务可以保存为模板")
        if task["output_expires_at"] and task["output_expires_at"] <= now:
            raise TemplateError("这条任务的素材已过期，无法保存为模板")
        project_id = (payload.get("_project") or {}).get("project_id")
        nodes = _nodes(db, project_id)
        missing = [k for k in KEEP_NODES if nodes.get(k, {}).get("status") != "ready"]
        if missing:
            raise TemplateError(f"任务缺少可复用的步骤：{', '.join(missing)}")
        source = next((i for i in json.loads(task["input_objects"] or "[]") if i.get("field") == "videos"), None)
        snapshot = {
            "analysis": nodes["analysis"]["analysis"],
            "direction": nodes["direction"]["direction"],
            "script": nodes["script"]["script"],
            "assets": {k: {"type": v["type"], "stored": v.get("stored"), "qa": v.get("qa"), "seconds": v.get("seconds"),
                           "source_speaker": v.get("source_speaker")}
                       for k, v in nodes.items() if k.startswith(ASSET_PREFIXES) and v.get("status") == "ready"
                       and v.get("stored")},
            "source": source,
            "ratio": task["ratio"],
            "source_seconds": float((payload.get("_reenact") or {}).get("source_seconds") or task["duration"]),
            "prompt": str((payload.get("_reenact") or {}).get("prompt") or ""),
            "preview": (json.loads(task["artifacts"] or "[]") or [None])[0],
        }
        template_id = public_id("tpl")
        expires = now + TEMPLATE_TTL
        db.execute("""INSERT INTO video_templates
            (id,merchant_id,name,note,source_task_id,project_id,snapshot_json,created_at,expires_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
                   (template_id, merchant_id, name.strip()[:60] or "未命名模板", note.strip()[:200], task_id, project_id,
                    json.dumps(snapshot, ensure_ascii=False), now, expires))
        # Everything the template points at stays exactly as long as the template does.
        db.execute("UPDATE assets SET expires_at=MAX(COALESCE(expires_at,0),?) WHERE task_id=? AND purged_at IS NULL",
                   (expires, task_id))
        db.execute("UPDATE merchant_tasks SET output_expires_at=MAX(COALESCE(output_expires_at,0),?) WHERE id=?",
                   (expires, task_id))
    return get(merchant_id, template_id)


def _view(row, with_snapshot: bool = False) -> dict:
    snapshot = json.loads(row["snapshot_json"] or "{}")
    analysis, direction, script = snapshot.get("analysis") or {}, snapshot.get("direction") or {}, snapshot.get("script") or {}
    value = {
        "id": row["id"], "name": row["name"], "note": row["note"], "source_task_id": row["source_task_id"],
        "uses": row["uses"], "created_at": row["created_at"], "expires_at": row["expires_at"],
        "ratio": snapshot.get("ratio"), "source_seconds": snapshot.get("source_seconds"),
        "summary": {
            "format": analysis.get("format"),
            "mode": "原声复用 + 动作参考" if direction.get("audio_strategy") == "reuse" else "新台词重新表演",
            "segments": len(script.get("segments") or []),
            "cast": [c.get("label") for c in direction.get("cast") or []],
            "style": direction.get("style"),
            "components": sorted({c.get("type") for c in script.get("components") or []}),
        },
        "prompt": snapshot.get("prompt"),
    }
    preview = snapshot.get("preview")
    if preview and preview.get("key") and row["expires_at"] > time.time():
        try:
            from object_storage import presigned_url
            value["preview_url"] = presigned_url(preview["key"], expires=3600)
        except Exception:  # noqa: BLE001 - a preview link is a convenience
            pass
    if with_snapshot:
        value["snapshot"] = snapshot
    return value


def list_for(merchant_id: str) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM video_templates WHERE merchant_id=? AND expires_at>? ORDER BY created_at DESC",
                          (merchant_id, time.time())).fetchall()
    return [_view(r) for r in rows]


def get(merchant_id: str, template_id: str, with_snapshot: bool = False) -> dict:
    with connect() as db:
        row = db.execute("SELECT * FROM video_templates WHERE id=? AND merchant_id=? AND expires_at>?",
                         (template_id, merchant_id, time.time())).fetchone()
    if not row:
        raise TemplateError("模板不存在或已过期")
    return _view(row, with_snapshot)


def snapshot(template_id: str) -> dict:
    with connect() as db:
        row = db.execute("SELECT snapshot_json FROM video_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        raise TemplateError("模板不存在")
    return json.loads(row["snapshot_json"])


def delete(merchant_id: str, template_id: str) -> None:
    with connect() as db:
        changed = db.execute("DELETE FROM video_templates WHERE id=? AND merchant_id=?", (template_id, merchant_id))
    if not changed.rowcount:
        raise TemplateError("模板不存在")


def purge_expired() -> int:
    with connect() as db:
        return db.execute("DELETE FROM video_templates WHERE expires_at<=?", (time.time(),)).rowcount


def create_task(merchant: dict, template_id: str, prompt: str, images: list[dict], note: str = "",
                task_id: str | None = None) -> dict:
    """One reenactment task that starts from the template instead of a fresh analysis."""
    from commerce import reenact_price_milli
    from database import reserve_task
    from video_api import transfer_backend
    import video_projects

    tpl = get(merchant["id"], template_id, with_snapshot=True)
    snap = tpl["snapshot"]
    source = snap.get("source")
    if not source:
        raise TemplateError("模板缺少原视频")
    seconds = float(snap["source_seconds"])
    price = reenact_price_milli(seconds)
    task_id = task_id or public_id("vid")
    payload = {"mode": "reference", "tier": "720p", "prompt": prompt, "duration": int(round(seconds)),
               "ratio": snap["ratio"], "quality": 0.9, "seed": 0, "video_audio_flags": "", "output_quality": 2.0,
               "refine_denoise": 0.35, "refine_steps": 2, "refine_extra_steps": 0, "refine_ladder": [],
               "_transfer_backend": transfer_backend(), "_tool": "reenact",
               "_reenact": {"source_seconds": seconds, "has_audio": True, "prompt": prompt, "mode": "reenact",
                            "template_id": template_id}}
    now = time.time()
    objects = [source] + images
    with transaction() as db:
        account = db.execute("SELECT balance_milli,status FROM merchants WHERE id=?", (merchant["id"],)).fetchone()
        if not account or account["status"] != "active":
            raise TemplateError("账户未启用或已暂停")
        if account["balance_milli"] < price:
            raise TemplateError(f"积分不足：每条需要 {price / 1000:g} 积分")
        db.execute("""INSERT INTO merchant_tasks (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,
            payload,portal_note,input_objects,price_milli,failure_price_milli,idempotency_key,request_hash,created_at,queued_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (task_id, merchant["id"], "reference", "720p", int(round(seconds)), snap["ratio"], "视频重演",
                    "queued", 5, "任务已进入队列", json.dumps(payload, ensure_ascii=False),
                    (note or f"模板：{tpl['name']}")[:80], json.dumps(objects, ensure_ascii=False), price, 0, None,
                    f"template-{template_id}-{task_id}", now, now))
        for item in images:
            db.execute("""INSERT INTO assets (id,merchant_id,task_id,role,bucket,object_key,filename,content_type,size_bytes,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (item["id"], merchant["id"], task_id, "input", item["bucket"], item["key"],
                                               item["filename"], item["content_type"], item.get("size_bytes"), now))
        project_id, run_id = video_projects.create_for_editor(
            db, merchant_id=merchant["id"], task_id=task_id, title="视频重演", brief=prompt, input_objects=objects,
            prompt=prompt, duration=int(round(seconds)), ratio=snap["ratio"])
        payload["_project"] = {"project_id": project_id, "run_id": run_id, "version": 1, "mode": "reenact"}
        db.execute("UPDATE merchant_tasks SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), task_id))
        db.execute("UPDATE video_templates SET uses=uses+1 WHERE id=?", (template_id,))
        reserve_task(db, merchant["id"], task_id, "720p", amount_milli=price)
    return {"task_id": task_id, "prompt": prompt, "price": price / 1000}
