from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict, deque
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Cookie, Header, HTTPException, Request

from database import connect


MERCHANT_COOKIE = "h3_merchant_session"
ADMIN_COOKIE = "h3_admin_session"
SESSION_DAYS = int(os.getenv("H3_SESSION_DAYS", "14"))
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,31}$")
_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str) -> str:
    if len(password) < 10 or len(password) > 128:
        raise ValueError("密码长度需要 10–128 位")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$16384$8$1$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode(),
        base64.urlsafe_b64encode(digest).decode(),
    )


def password_matches(password: str, encoded: str) -> bool:
    try:
        _, n, r, p, salt, expected = encoded.split("$")
        digest = hashlib.scrypt(
            password.encode(), salt=base64.urlsafe_b64decode(salt),
            n=int(n), r=int(r), p=int(p),
        )
        return hmac.compare_digest(base64.urlsafe_b64encode(digest).decode(), expected)
    except (ValueError, TypeError):
        return False


def new_session(principal_type: str, merchant_id: str | None = None) -> str:
    raw = secrets.token_urlsafe(40)
    now = time.time()
    with connect() as db:
        db.execute(
            "INSERT INTO sessions(token_hash,principal_type,merchant_id,created_at,expires_at,last_seen_at) VALUES(?,?,?,?,?,?)",
            (token_hash(raw), principal_type, merchant_id, now, now + SESSION_DAYS * 86400, now),
        )
    return raw


def revoke_session(raw: str | None) -> None:
    if not raw:
        return
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(raw),))


def session_cookie_kwargs(cookie_name: str) -> dict:
    return {
        "key": cookie_name,
        "httponly": True,
        "secure": os.getenv("H3_COOKIE_SECURE", "1") == "1",
        "samesite": "lax",
        "max_age": SESSION_DAYS * 86400,
        "path": "/",
    }


def _session(raw: str | None, principal: str) -> dict:
    if not raw:
        raise HTTPException(401, "请先登录")
    with connect() as db:
        row = db.execute(
            "SELECT * FROM sessions WHERE token_hash=? AND principal_type=? AND expires_at>?",
            (token_hash(raw), principal, time.time()),
        ).fetchone()
        if not row:
            raise HTTPException(401, "登录已失效")
        db.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (time.time(), row["token_hash"]))
        if principal == "admin":
            return {"principal": "admin"}
        merchant = db.execute("SELECT * FROM merchants WHERE id=?", (row["merchant_id"],)).fetchone()
    if not merchant:
        raise HTTPException(401, "账户不存在")
    return dict(merchant)


def portal_merchant(
    session: Annotated[str | None, Cookie(alias=MERCHANT_COOKIE)] = None,
) -> dict:
    return _session(session, "merchant")


def admin_session(
    session: Annotated[str | None, Cookie(alias=ADMIN_COOKIE)] = None,
) -> dict:
    return _session(session, "admin")


def api_merchant(authorization: Annotated[str | None, Header()] = None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer API Key")
    raw = authorization[7:].strip()
    if not raw.startswith("h3_live_"):
        raise HTTPException(401, "API Key 无效")
    with connect() as db:
        key = db.execute("SELECT * FROM api_keys WHERE key_hash=?", (token_hash(raw),)).fetchone()
        if not key or key["status"] == "revoked":
            raise HTTPException(401, "API Key 无效或已撤销")
        account = db.execute("SELECT * FROM merchants WHERE id=?", (key["merchant_id"],)).fetchone()
        if not account:
            raise HTTPException(401, "账户不存在")
        db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (time.time(), key["id"]))
    value = dict(account)
    value["api_key_status"] = key["status"]
    return value


def video_merchant(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    session: Annotated[str | None, Cookie(alias=MERCHANT_COOKIE)] = None,
) -> dict:
    """Authenticate public API calls with a key and portal app calls with its session.

    The browser application deliberately shares the same video creation code and
    billing transaction as the public API.  Only the authentication boundary is
    different, so a portal submission cannot bypass balance or concurrency checks.
    """
    if request.url.path.startswith((
        "/api/account/videos/", "/api/account/remix/", "/api/account/remix2/",
        "/api/account/commerce-remix/", "/api/account/video-edits/",
        "/api/account/image-edits", "/api/account/creative/",
        "/api/account/digital-human/",
        "/api/account/voice-clones",
    )):
        origin = request.headers.get("origin", "").strip()
        if origin:
            origin_host = (urlsplit(origin).netloc or "").lower()
            request_host = request.headers.get(
                "x-forwarded-host", request.headers.get("host", "")
            ).split(",", 1)[0].strip().lower()
            if not origin_host or origin_host != request_host:
                raise HTTPException(403, "不允许跨站提交任务")
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise HTTPException(403, "不允许跨站提交任务")
        value = _session(session, "merchant")
        value["api_key_status"] = "active"
        return value
    return api_merchant(authorization)


def verify_admin_password(value: str) -> bool:
    configured = os.getenv("H3_ADMIN_PASSWORD", "")
    return bool(configured) and hmac.compare_digest(value, configured)


def request_ip(request: Request) -> str:
    real_ip = request.headers.get("x-real-ip", "").strip()
    return real_ip or (request.client.host if request.client else "unknown")


def rate_limit(request: Request, scope: str, *, attempts: int, window: int) -> None:
    now = time.time()
    key = f"{scope}:{request_ip(request)}"
    bucket = _ATTEMPTS[key]
    while bucket and bucket[0] <= now - window:
        bucket.popleft()
    if len(bucket) >= attempts:
        raise HTTPException(429, "请求过于频繁，请稍后再试")
    bucket.append(now)
