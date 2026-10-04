from __future__ import annotations

import secrets
import sqlite3
import time
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from database import DEFAULT_MAX_CONCURRENCY, connect, merchant_view, points, public_id
from security import (
    MERCHANT_COOKIE,
    USERNAME_RE,
    new_session,
    password_hash,
    password_matches,
    portal_merchant,
    rate_limit,
    revoke_session,
    session_cookie_kwargs,
    token_hash,
)


router = APIRouter(prefix="/api/account", tags=["merchant-account"])


class RegisterBody(BaseModel):
    username: str = Field(min_length=4, max_length=32)
    password: str = Field(min_length=10, max_length=128)


class LoginBody(BaseModel):
    username: str
    password: str


class RecoveryBody(BaseModel):
    merchant_id: str
    recovery_code: str
    new_password: str = Field(min_length=10, max_length=128)


class KeyBody(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=40)


def _set_session(response: Response, merchant_id: str) -> None:
    response.set_cookie(value=new_session("merchant", merchant_id), **session_cookie_kwargs(MERCHANT_COOKIE))


@router.post("/register", status_code=201)
def register(body: RegisterBody, response: Response, request: Request) -> dict:
    rate_limit(request, "merchant-register", attempts=5, window=3600)
    username = body.username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(422, "登录名只能使用 4–32 位字母、数字、点、横线或下划线")
    try:
        encoded = password_hash(body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    merchant_id = public_id("mrc")
    recovery_code = "H3-" + "-".join(
        secrets.token_hex(3).upper() for _ in range(4)
    )
    now = time.time()
    try:
        with connect() as db:
            db.execute(
                """INSERT INTO merchants
                (id,username,password_hash,recovery_hash,status,max_concurrency,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    merchant_id, username, encoded, token_hash(recovery_code), "pending",
                    DEFAULT_MAX_CONCURRENCY, now, now,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "这个登录名已被使用") from exc
    _set_session(response, merchant_id)
    return {
        "merchant_id": merchant_id,
        "username": username,
        "status": "pending",
        "balance": 0,
        "recovery_code": recovery_code,
        "recovery_code_notice": "恢复码只显示这一次，请离线保存。充值时把商户 ID 发给管理员。",
    }


@router.post("/login")
def login(body: LoginBody, response: Response, request: Request) -> dict:
    rate_limit(request, "merchant-login", attempts=12, window=300)
    with connect() as db:
        row = db.execute(
            "SELECT * FROM merchants WHERE username=? COLLATE NOCASE", (body.username.strip(),)
        ).fetchone()
    if not row or not password_matches(body.password, row["password_hash"]):
        raise HTTPException(401, "登录名或密码错误")
    _set_session(response, row["id"])
    return merchant_view(row)


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    session: Annotated[str | None, Cookie(alias=MERCHANT_COOKIE)] = None,
) -> Response:
    revoke_session(session)
    response.delete_cookie(MERCHANT_COOKIE, path="/")
    return response


@router.post("/recover")
def recover(body: RecoveryBody, request: Request) -> dict:
    rate_limit(request, "merchant-recover", attempts=5, window=3600)
    try:
        encoded = password_hash(body.new_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with connect() as db:
        row = db.execute("SELECT * FROM merchants WHERE id=?", (body.merchant_id,)).fetchone()
        if not row or not secrets.compare_digest(token_hash(body.recovery_code.strip()), row["recovery_hash"]):
            raise HTTPException(401, "商户 ID 或恢复码错误")
        db.execute("UPDATE merchants SET password_hash=?,updated_at=? WHERE id=?", (encoded, time.time(), row["id"]))
        db.execute("DELETE FROM sessions WHERE merchant_id=?", (row["id"],))
    return {"ok": True, "message": "密码已更新，请重新登录"}


@router.get("/me")
def me(account: Annotated[dict, Depends(portal_merchant)]) -> dict:
    with connect() as db:
        row = db.execute("SELECT * FROM merchants WHERE id=?", (account["id"],)).fetchone()
        stats = db.execute(
            """SELECT COUNT(*) total,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,
            SUM(CASE WHEN status IN ('queued','dispatching','running') THEN 1 ELSE 0 END) active
            FROM merchant_tasks WHERE merchant_id=?""",
            (account["id"],),
        ).fetchone()
    value = merchant_view(row)
    value["usage"] = {key: int(stats[key] or 0) for key in ("total", "completed", "failed", "active")}
    return value


@router.get("/keys")
def list_keys(account: Annotated[dict, Depends(portal_merchant)]) -> dict:
    with connect() as db:
        rows = db.execute(
            "SELECT id,name,prefix,status,created_at,last_used_at FROM api_keys WHERE merchant_id=? ORDER BY created_at DESC",
            (account["id"],),
        ).fetchall()
    return {"keys": [dict(row) for row in rows]}


@router.post("/keys", status_code=201)
def create_key(body: KeyBody, account: Annotated[dict, Depends(portal_merchant)]) -> dict:
    if account["status"] == "pending":
        raise HTTPException(403, "账户尚未开通，请把商户 ID 发给管理员充值")
    if account["status"] not in {"active"}:
        raise HTTPException(403, "账户已暂停，请联系管理员")
    raw = "h3_live_" + secrets.token_urlsafe(32)
    key_id = public_id("key")
    status = "active" if account["balance_milli"] >= 1000 else "suspended_balance"
    prefix = raw[:16]
    with connect() as db:
        db.execute(
            """INSERT INTO api_keys(id,merchant_id,name,prefix,key_hash,status,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (key_id, account["id"], body.name.strip(), prefix, token_hash(raw), status, time.time()),
        )
    return {
        "id": key_id,
        "name": body.name.strip(),
        "prefix": prefix,
        "status": status,
        "api_key": raw,
        "notice": "API Key 只显示这一次，请立即保存到服务端环境变量。",
    }


@router.delete("/keys/{key_id}")
def revoke_key(key_id: str, account: Annotated[dict, Depends(portal_merchant)]) -> dict:
    with connect() as db:
        result = db.execute(
            "UPDATE api_keys SET status='revoked',revoked_at=? WHERE id=? AND merchant_id=? AND status!='revoked'",
            (time.time(), key_id, account["id"]),
        )
    if not result.rowcount:
        raise HTTPException(404, "API Key 不存在")
    return {"ok": True}


@router.get("/ledger")
def ledger(account: Annotated[dict, Depends(portal_merchant)], limit: int = 100) -> dict:
    limit = min(max(limit, 1), 200)
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM point_ledger WHERE merchant_id=? ORDER BY created_at DESC LIMIT ?",
            (account["id"], limit),
        ).fetchall()
    return {
        "items": [
            {
                "id": row["id"], "task_id": row["task_id"], "type": row["event_type"],
                "available_delta": points(row["available_delta_milli"]),
                "reserved_delta": points(row["reserved_delta_milli"]),
                "charged": points(row["charged_milli"]),
                "available_after": points(row["available_after_milli"]),
                "reserved_after": points(row["reserved_after_milli"]),
                "note": row["note"], "created_at": row["created_at"],
            }
            for row in rows
        ]
    }
