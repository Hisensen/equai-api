from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import gongji
import scheduler
from accounts import router as accounts_router
from admin_api import router as admin_router
from database import init_db
from local_storage import capacity_ok as local_storage_capacity_ok
from local_storage import configured as local_storage_configured
from object_storage import configured as object_storage_configured
from security import portal_merchant
from transfer_api import router as transfer_router
from transfer_storage import backend as transfer_backend
from transfer_storage import capacity_ok as transfer_storage_capacity_ok
from transfer_storage import configured as transfer_storage_configured
from video_api import router as video_router
from video_project_api import router as video_project_router
from commerce import router as commerce_router
from long_video_api import router as long_video_router
from music_api import router as music_router

log = logging.getLogger("h3.api")

# Keep databases, backups, and temporary upload state private by default.
os.umask(0o077)


def _storage_csp_sources() -> list[str]:
    """Return validated HTTPS origins used for uploads and video playback."""
    sources: list[str] = []
    bucket = os.getenv("COS_BUCKET", "").strip()
    region = os.getenv("COS_REGION", "").strip()
    safe_component = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
    if safe_component.fullmatch(bucket) and safe_component.fullmatch(region):
        sources.append(f"https://{bucket}.cos.{region}.myqcloud.com")

    cdn = os.getenv("COS_CDN_DOMAIN", "").strip()
    if cdn:
        parsed = urlsplit(cdn if "://" in cdn else f"https://{cdn}")
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.port is None
            and safe_component.fullmatch(parsed.hostname)
        ):
            sources.append(f"https://{parsed.hostname}")
    for relay in (os.getenv("H3_UPLOAD_RELAY_BASE_URL", "").strip(), os.getenv("H3_UPLOAD_RELAY_FALLBACK_URL", "").strip(),
                  os.getenv("COS_DOWNLOAD_RELAY", "").strip()):
        if not relay:
            continue
        parsed = urlsplit(relay if "://" in relay else f"https://{relay}")
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.port is None
            and safe_component.fullmatch(parsed.hostname)
        ):
            sources.append(f"https://{parsed.hostname}")
    return list(dict.fromkeys(sources))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    scheduler.recover_interrupted_claims()
    loops = [
        asyncio.create_task(scheduler.dispatch_loop("standard"), name="h3-dispatch"),
        asyncio.create_task(scheduler.dispatch_loop("remix"), name="h3-remix-dispatch"),
        asyncio.create_task(scheduler.dispatch_loop("reenact"), name="h3-reenact-dispatch"),
        asyncio.create_task(scheduler.dispatch_loop("music"), name="h3-music-dispatch"),
        asyncio.create_task(scheduler.poll_loop(), name="h3-poll"),
        asyncio.create_task(scheduler.cleanup_loop(), name="h3-cleanup"),
        asyncio.create_task(scheduler.backup_loop(), name="h3-backup"),
    ]
    try:
        yield
    finally:
        for task in loops:
            task.cancel()
        for task in loops:
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="EQUAI Video API",
    version="2.0.0",
    description="商家账户、积分结算与 EQUAI 视频生成 API。",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "").split(",") if item.strip()]
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "Idempotency-Key"],
    )

app.include_router(accounts_router)
app.include_router(admin_router)
app.include_router(transfer_router)
app.include_router(video_router)
app.include_router(video_project_router)
app.include_router(commerce_router)
app.include_router(long_video_router)
app.include_router(music_router)


@app.get("/admin/login", include_in_schema=False)
def admin_login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin.html", status_code=303)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    storage_sources = "".join(f" {source}" for source in _storage_csp_sources())
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
        "script-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: blob:{storage_sources}; "
        f"media-src 'self' blob:{storage_sources}; "
        f"connect-src 'self'{storage_sources}; form-action 'self'"
    )
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/health")
def health() -> dict:
    services = {
        "database": True,
        "object_storage": object_storage_configured(),
        "local_video_storage": local_storage_configured(),
        "local_video_capacity": local_storage_capacity_ok(),
        "worker_transfer": transfer_storage_configured(),
        "worker_transfer_capacity": transfer_storage_capacity_ok(),
        "gongji_720p": gongji.configured("720p"),
        "gongji_1080p": gongji.configured("1080p"),
        "gongji_2k": gongji.configured("2k"),
        "gongji_long": gongji.long_configured(),
        "admin_auth": bool(os.getenv("H3_ADMIN_PASSWORD")),
    }
    return {
        "ok": all(services.values()),
        "status": "ready" if all(services.values()) else "degraded",
        "worker_transfer_backend": transfer_backend(),
    }


@app.get("/api/public/config")
def public_config() -> dict:
    return {
        "pricing": {
            "720p": {"success": 1, "runtime_failure": 0},
            "1080p": {"success": 2, "runtime_failure": 0},
            "2k": {"success": 3, "runtime_failure": 0},
        },
        "quality_specs": {
            "720p": {"resolution": "1280x736"},
            "1080p": {"resolution": "1920x1080"},
            "2k": {"resolution": "2560x1440"},
        },
        "retention": {"inputs_hours": 24, "outputs_hours": 72, "download_url_hours": 1},
        "modes": ["text", "image", "reference"],
        "durations": list(range(5, 16)),
        "uploads": {
            "direct": os.getenv("H3_DIRECT_UPLOADS", "0").strip() == "1",
            "max_files": 12,
            "max_total_mb": 512,
        },
    }


portal = Path(os.getenv("H3_PORTAL_DIR", "/app/portal"))
if portal.is_dir():
    @app.get("/docs", include_in_schema=False)
    @app.get("/docs.html", include_in_schema=False)
    def merchant_docs(_: Annotated[dict, Depends(portal_merchant)]) -> FileResponse:
        return FileResponse(portal / "docs.html")

    app.mount("/", StaticFiles(directory=portal, html=True), name="portal")
