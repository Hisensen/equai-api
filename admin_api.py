from __future__ import annotations

import json
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response
from pydantic import BaseModel, Field

from database import (
    audit,
    connect,
    merchant_view,
    MAX_MERCHANT_CONCURRENCY,
    points,
    public_id,
    refresh_key_state,
    settle_task,
    transaction,
)
from security import (
    ADMIN_COOKIE,
    admin_session,
    new_session,
    revoke_session,
    session_cookie_kwargs,
    rate_limit,
    verify_admin_password,
)
from prompt_service import (
    PromptServiceError,
    public_settings as prompt_public_settings,
    save_settings as save_prompt_settings,
    test_connection as test_prompt_connection,
)


router = APIRouter(prefix="/api/admin", tags=["admin"])


class LoginBody(BaseModel):
    password: str


class AdjustBody(BaseModel):
    points: Decimal
    note: str = Field(min_length=2, max_length=300)


class StatusBody(BaseModel):
    status: Literal["active", "disabled"]
    note: str = Field(default="管理员调整", max_length=300)


class ConcurrencyBody(BaseModel):
    max_concurrency: int = Field(ge=1, le=MAX_MERCHANT_CONCURRENCY)
    note: str = Field(default="管理员调整并发", max_length=300)


class PromptServiceBody(BaseModel):
    enabled: bool = False
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str = Field(default="", max_length=512)


@router.post("/login")
def login(body: LoginBody, response: Response, request: Request) -> dict:
    rate_limit(request, "admin-login", attempts=8, window=900)
    if not verify_admin_password(body.password):
        raise HTTPException(401, "管理员密码错误")
    response.set_cookie(value=new_session("admin"), **session_cookie_kwargs(ADMIN_COOKIE))
    return {"ok": True}


@router.post("/login/form", include_in_schema=False)
def login_form(
    password: Annotated[str, Form()],
    request: Request,
) -> Response:
    """Native form fallback for browsers where admin.js did not load."""
    rate_limit(request, "admin-login", attempts=8, window=900)
    if not verify_admin_password(password):
        raise HTTPException(401, "管理员密码错误")
    response = Response(status_code=303, headers={"Location": "/admin.html"})
    response.set_cookie(value=new_session("admin"), **session_cookie_kwargs(ADMIN_COOKIE))
    return response


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    session: Annotated[str | None, Cookie(alias=ADMIN_COOKIE)] = None,
) -> Response:
    revoke_session(session)
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return response


@router.get("/me")
def me(_: Annotated[dict, Depends(admin_session)]) -> dict:
    import object_storage
    return {"role": "admin", "download_relay": object_storage.relay_status()}


@router.get("/prompt-service")
def get_prompt_service(_: Annotated[dict, Depends(admin_session)]) -> dict:
    return prompt_public_settings()


@router.post("/prompt-service")
def update_prompt_service(
    body: PromptServiceBody,
    _: Annotated[dict, Depends(admin_session)],
) -> dict:
    try:
        return save_prompt_settings(
            enabled=body.enabled,
            base_url=body.base_url,
            api_key=body.api_key,
        )
    except (ValueError, PromptServiceError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/prompt-service/test")
async def test_prompt_service(
    body: PromptServiceBody,
    _: Annotated[dict, Depends(admin_session)],
) -> dict:
    try:
        return await test_prompt_connection(base_url=body.base_url, api_key=body.api_key)
    except (ValueError, PromptServiceError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/merchants")
def merchants(
    _: Annotated[dict, Depends(admin_session)],
    query: str = "",
    limit: int = 100,
) -> dict:
    limit = min(max(limit, 1), 200)
    needle = f"%{query.strip()}%"
    with connect() as db:
        rows = db.execute(
            """SELECT * FROM merchants WHERE ?='' OR id LIKE ? OR username LIKE ?
            ORDER BY created_at DESC LIMIT ?""",
            (query.strip(), needle, needle, limit),
        ).fetchall()
    return {
        "merchants": [merchant_view(row) for row in rows],
        "platform_max_concurrency": MAX_MERCHANT_CONCURRENCY,
    }


# 北京时间与 UTC 的时差：按天聚合必须按本地日历分组，否则每天的分界线会差 8 小时
DAY_OFFSET = int(os.getenv("H3_STATS_UTC_OFFSET", "28800"))


@router.get("/tasks")
def tasks(
    _: Annotated[dict, Depends(admin_session)],
    status: str = "",
    kind: str = "",
    merchant: str = "",
    since_days: int = 7,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """按创建时间倒序的任务流水，最新的在最前面。"""
    limit, offset = min(max(limit, 1), 500), max(offset, 0)
    since = time.time() - max(since_days, 1) * 86400
    clauses, params = ["t.created_at >= ?"], [since]
    if status.strip():
        clauses.append("t.status = ?")
        params.append(status.strip())
    if kind.strip():
        clauses.append("t.kind = ?")
        params.append(kind.strip())
    if merchant.strip():
        needle = f"%{merchant.strip()}%"
        clauses.append("(t.merchant_id LIKE ? OR m.username LIKE ?)")
        params += [needle, needle]
    where = " AND ".join(clauses)
    with connect() as db:
        total = db.execute(
            f"SELECT COUNT(*) FROM merchant_tasks t JOIN merchants m ON m.id=t.merchant_id WHERE {where}",
            params,
        ).fetchone()[0]
        rows = db.execute(
            f"""SELECT t.*, m.username,
                COALESCE((SELECT SUM(charged_milli) FROM point_ledger WHERE task_id=t.id),0) charged_milli
                FROM merchant_tasks t JOIN merchants m ON m.id=t.merchant_id
                WHERE {where} ORDER BY t.created_at DESC LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
    return {"total": total, "limit": limit, "offset": offset,
            "tasks": [_task_row(row) for row in rows]}


def _task_row(row) -> dict:
    payload = json.loads(row["payload"] or "{}")
    started, finished = row["started_at"], row["finished_at"]
    return {
        "id": row["id"], "merchant": row["username"], "merchant_id": row["merchant_id"],
        "tool": payload.get("_tool", "video"), "kind": row["kind"], "quality": row["quality"],
        "duration": row["duration"], "status": row["status"], "progress": row["progress"],
        "message": row["message"], "error": row["error"],
        "created_at": row["created_at"], "finished_at": finished,
        "queue_seconds": round((started or row["created_at"]) - row["created_at"], 1),
        "run_seconds": round(finished - started, 1) if started and finished else None,
        "price": points(row["price_milli"]), "charged": points(row["charged_milli"]),
        "cost": round(float(row["provider_billing"] or 0), 4),
    }


@router.get("/stats/daily")
def daily_stats(
    _: Annotated[dict, Depends(admin_session)],
    days: int = 14,
) -> dict:
    """每天跑了多少任务、成功几条、花了多少钱、收了多少积分。"""
    days = min(max(days, 1), 90)
    since = time.time() - days * 86400
    with connect() as db:
        rows = db.execute(
            """SELECT date(created_at + ?, 'unixepoch') day,
               COUNT(*) total,
               SUM(status='completed') completed,
               SUM(status='failed') failed,
                SUM(status IN ('queued','awaiting_lyrics','dispatching','running')) active,
               SUM(COALESCE(provider_billing,0)) cost,
               AVG(CASE WHEN finished_at IS NOT NULL AND started_at IS NOT NULL
                        THEN finished_at - started_at END) avg_run
               FROM merchant_tasks WHERE created_at >= ?
               GROUP BY day ORDER BY day DESC""",
            (DAY_OFFSET, since),
        ).fetchall()
        charged = dict(db.execute(
            """SELECT date(created_at + ?, 'unixepoch') day, SUM(charged_milli)
               FROM point_ledger WHERE created_at >= ? GROUP BY day""",
            (DAY_OFFSET, since),
        ).fetchall())
    return {"days": [{
        "day": row["day"], "total": row["total"], "completed": row["completed"] or 0,
        "failed": row["failed"] or 0, "active": row["active"] or 0,
        "cost": round(float(row["cost"] or 0), 4),
        "revenue": points(charged.get(row["day"], 0) or 0),
        "avg_run_seconds": round(row["avg_run"], 1) if row["avg_run"] else None,
    } for row in rows]}


@router.get("/merchants/{merchant_id}")
def merchant_detail(merchant_id: str, _: Annotated[dict, Depends(admin_session)]) -> dict:
    with connect() as db:
        account = db.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
        if not account:
            raise HTTPException(404, "商户不存在")
        tasks = db.execute(
            "SELECT id,kind,quality,status,provider_billing,created_at,finished_at FROM merchant_tasks WHERE merchant_id=? ORDER BY created_at DESC LIMIT 100",
            (merchant_id,),
        ).fetchall()
        ledger = db.execute(
            "SELECT * FROM point_ledger WHERE merchant_id=? ORDER BY created_at DESC LIMIT 100",
            (merchant_id,),
        ).fetchall()
    return {
        "merchant": merchant_view(account),
        "tasks": [dict(row) for row in tasks],
        "ledger": [
            {
                **dict(row),
                "available_delta": points(row["available_delta_milli"]),
                "reserved_delta": points(row["reserved_delta_milli"]),
                "charged": points(row["charged_milli"]),
            }
            for row in ledger
        ],
    }


@router.post("/merchants/{merchant_id}/adjust")
def adjust(
    merchant_id: str,
    body: AdjustBody,
    _: Annotated[dict, Depends(admin_session)],
) -> dict:
    try:
        milli = int(body.points * Decimal(1000))
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(422, "积分格式错误") from exc
    if milli == 0 or abs(milli) > 100_000_000:
        raise HTTPException(422, "单次调整必须在 ±100000 积分内且不能为 0")
    now = time.time()
    with transaction() as db:
        account = db.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
        if not account:
            raise HTTPException(404, "商户不存在")
        if account["balance_milli"] + milli < 0:
            raise HTTPException(409, "可用积分不足，不能扣成负数")
        new_status = "active" if milli > 0 and account["status"] == "pending" else account["status"]
        db.execute(
            """UPDATE merchants SET balance_milli=balance_milli+?,status=?,updated_at=?,
            activated_at=CASE WHEN activated_at IS NULL AND ?='active' THEN ? ELSE activated_at END
            WHERE id=?""",
            (milli, new_status, now, new_status, now, merchant_id),
        )
        updated = db.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
        db.execute(
            """INSERT INTO point_ledger
            (id,merchant_id,event_type,available_delta_milli,reserved_delta_milli,charged_milli,
             available_after_milli,reserved_after_milli,note,actor,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                public_id("led"), merchant_id, "admin_adjust", milli, 0, 0,
                updated["balance_milli"], updated["reserved_milli"], body.note.strip(), "admin", now,
            ),
        )
        refresh_key_state(db, merchant_id)
        audit(db, "merchant.adjust", merchant_id, {"points_milli": milli, "note": body.note})
    return merchant_view(updated)


@router.post("/merchants/{merchant_id}/status")
def set_status(
    merchant_id: str,
    body: StatusBody,
    _: Annotated[dict, Depends(admin_session)],
) -> dict:
    with transaction() as db:
        account = db.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
        if not account:
            raise HTTPException(404, "商户不存在")
        db.execute(
            "UPDATE merchants SET status=?,consecutive_failures=CASE WHEN ?='active' THEN 0 ELSE consecutive_failures END,updated_at=? WHERE id=?",
            (body.status, body.status, time.time(), merchant_id),
        )
        if body.status == "disabled":
            queued = db.execute(
                "SELECT id FROM merchant_tasks WHERE merchant_id=? AND status IN ('queued','awaiting_lyrics')", (merchant_id,)
            ).fetchall()
            for task in queued:
                expires = time.time() + 86400
                db.execute(
                    "UPDATE merchant_tasks SET status='cancelled',message='账户停用，排队任务已释放',finished_at=?,input_expires_at=? WHERE id=?",
                    (time.time(), expires, task["id"]),
                )
                db.execute("UPDATE assets SET expires_at=? WHERE task_id=? AND role='input'", (expires, task["id"]))
                settle_task(db, task["id"], "cancel_before_start", note="管理员停用账户")
        refresh_key_state(db, merchant_id)
        audit(db, "merchant.status", merchant_id, {"status": body.status, "note": body.note})
        updated = db.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
    return merchant_view(updated)


@router.post("/merchants/{merchant_id}/concurrency")
def set_concurrency(
    merchant_id: str,
    body: ConcurrencyBody,
    _: Annotated[dict, Depends(admin_session)],
) -> dict:
    with transaction() as db:
        account = db.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
        if not account:
            raise HTTPException(404, "商户不存在")
        previous = account["max_concurrency"]
        db.execute(
            "UPDATE merchants SET max_concurrency=?,updated_at=? WHERE id=?",
            (body.max_concurrency, time.time(), merchant_id),
        )
        audit(
            db, "merchant.concurrency", merchant_id,
            {"from": previous, "to": body.max_concurrency, "note": body.note},
        )
        updated = db.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
    return merchant_view(updated)


class ReenactRerun(BaseModel):
    reset: list[str] = Field(min_length=1, max_length=40)


@router.post("/reenact/{task_id}/rerun")
def rerun_reenactment(task_id: str, body: ReenactRerun, _: Annotated[dict, Depends(admin_session)]) -> dict:
    """Operator repair: redo selected stages of a finished reenactment, reusing everything else."""
    from reenact import runner
    try:
        removed = runner.rerun(task_id, body.reset)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"task_id": task_id, "reset": removed}
