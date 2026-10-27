from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from database import connect
from security import portal_merchant
from video_projects import project_view

router = APIRouter(tags=["video-projects"])


@router.get("/api/account/video-projects", include_in_schema=False)
def list_video_projects(
    merchant: Annotated[dict, Depends(portal_merchant)],
    limit: int = 50,
) -> dict:
    limit = max(1, min(limit, 100))
    with connect() as db:
        rows = db.execute(
            """SELECT id,title,status,source_task_id,current_run_id,created_at,updated_at
            FROM video_projects WHERE merchant_id=? ORDER BY updated_at DESC LIMIT ?""",
            (merchant["id"], limit),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@router.get("/api/account/video-projects/{project_id}", include_in_schema=False)
def get_video_project(
    project_id: str,
    merchant: Annotated[dict, Depends(portal_merchant)],
) -> dict:
    with connect() as db:
        value = project_view(db, project_id, merchant["id"])
    if value is None:
        raise HTTPException(404, "视频编辑项目不存在")
    return _with_media_links(value)


def _sign(stored: object, task_id: str | None = None) -> str | None:
    if not isinstance(stored, dict):
        return None
    try:
        # Intermediate project images/audio are local *input* assets.  Their
        # URL is already signed for the worker-input endpoint; treating them
        # as final video outputs produces a 404 in the project gallery.
        existing = stored.get("url")
        if isinstance(existing, str) and "/internal/worker/" in existing:
            # Worker input links are short lived. Project nodes retain the
            # original signed URL, so opening an old project later would show
            # broken thumbnails/videos. Re-sign the asset each time the owner
            # loads the project; the worker-input route still verifies task
            # ownership of the asset through its HMAC grant.
            asset_id = stored.get("id")
            if stored.get("storage") == "local" and task_id and asset_id:
                from transfer_storage import signed_input_url
                return signed_input_url(task_id, str(asset_id), expires=24 * 3600)
            return existing
        if stored.get("storage") == "local":
            relative = stored.get("path")
            if not task_id or not relative:
                return None
            from local_storage import signed_url
            return signed_url(task_id, relative, stored.get("name") or "video.mp4", expires=3600)
        if not stored.get("key"):
            return None
        from object_storage import presigned_url
        return presigned_url(stored["key"], expires=3600)
    except Exception:  # noqa: BLE001 - links are a convenience
        return None


def _with_media_links(project: dict) -> dict:
    """Sign intermediate pictures, voices and Takes so the owner can inspect every step."""
    for node in project.get("nodes") or []:
        output = node.get("output") or {}
        node_task_id = output.get("job_id") or project.get("source_task_id")
        if output.get("stored"):
            output["url"] = _sign(output["stored"], node_task_id)
        if isinstance(output.get("artifact"), dict):
            output["url"] = _sign(output["artifact"], node_task_id)
        for attempt in output.get("attempts") or []:
            if attempt.get("stored"):
                attempt["url"] = _sign(attempt["stored"], node_task_id)
            if isinstance(attempt.get("artifact"), dict):
                attempt["url"] = _sign(attempt["artifact"], node_task_id)
            attempt.pop("transcript", None)
            attempt.pop("prompt", None) if node.get("node_type") == "image" else None
        chosen = output.get("chosen")
        if isinstance(chosen, dict):
            chosen.pop("transcript", None)
    return project




# ── reenactment templates ─────────────────────────────────────────────────────────────

class TemplateCreate(BaseModel):
    task_id: str = Field(min_length=4, max_length=80)
    name: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=200)


@router.post("/api/account/video-templates", status_code=201, include_in_schema=False)
def create_video_template(body: TemplateCreate, merchant: Annotated[dict, Depends(portal_merchant)]) -> dict:
    from reenact import templates
    try:
        return templates.save(merchant["id"], body.task_id, body.name, body.note)
    except templates.TemplateError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/api/account/video-templates", include_in_schema=False)
def list_video_templates(merchant: Annotated[dict, Depends(portal_merchant)]) -> dict:
    from reenact import templates
    return {"items": templates.list_for(merchant["id"])}


@router.delete("/api/account/video-templates/{template_id}", status_code=204, include_in_schema=False)
def delete_video_template(template_id: str, merchant: Annotated[dict, Depends(portal_merchant)]) -> Response:
    from reenact import templates
    try:
        templates.delete(merchant["id"], template_id)
    except templates.TemplateError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(status_code=204)


MAX_TEMPLATE_BATCH = 20


@router.post("/api/account/video-templates/{template_id}/runs", status_code=202, include_in_schema=False)
async def run_video_template(
    template_id: str,
    merchant: Annotated[dict, Depends(portal_merchant)],
    prompts: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
    images: Annotated[list[UploadFile] | None, File()] = None,
    direct_assets: Annotated[str, Form()] = "",
) -> dict:
    """One task per non-empty prompt line; optional pictures apply to every line (图1, 图2 …)."""
    raise HTTPException(503, "视频编辑暂不开放，恢复后再试")

    from commerce import normalize_upload, normalized_upload
    from database import public_id
    from reenact import templates
    from video_api import (
        _remove_uploaded_inputs, _upload, direct_assets_as_uploads, _uploads_named,
    )

    images = images or []
    if direct_assets:
        if images:
            raise HTTPException(422, "模板图片不能同时使用直传和 multipart 上传")
        _staged, staged = await direct_assets_as_uploads(
            merchant["id"], "reference", direct_assets,
        )
        images = _uploads_named(staged, "images")

    lines = [line.strip() for line in prompts.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(422, "请至少写一行提示词")
    if len(lines) > MAX_TEMPLATE_BATCH:
        raise HTTPException(422, f"一次最多批量 {MAX_TEMPLATE_BATCH} 条")
    if len(images or []) > 3:
        raise HTTPException(422, "参考图片最多 3 张")
    try:
        templates.get(merchant["id"], template_id)
        pictures = [(upload.filename or f"picture-{i}.png", normalize_upload(upload)) for i, upload in enumerate(images or [], 1)]
        if pictures and not all(data for _, data in pictures):
            raise ValueError("图片为空")
    except templates.TemplateError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    created = []
    for n, line in enumerate(lines, 1):
        task_id = public_id("vid")
        objects: list[dict] = []
        try:
            for index, (name, data) in enumerate(pictures):
                upload = normalized_upload(data, index)
                obj = await _upload(merchant["id"], task_id, index, "images", upload, force_local=True)
                objects.append({**obj, "field": "images"})
            created.append(templates.create_task(merchant, template_id, line, objects,
                                                 note=(f"{note} #{n}" if note else ""), task_id=task_id))
        except templates.TemplateError as exc:
            await _remove_uploaded_inputs(objects)
            if not created:
                raise HTTPException(402, str(exc)) from exc
            break
    return {"template_id": template_id, "items": created, "requested": len(lines)}
