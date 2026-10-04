from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import BinaryIO

from qcloud_cos import CosConfig, CosS3Client


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def bucket_name() -> str:
    """腾讯云 COS 桶名，必须包含 APPID，例如 h3-1250000000。"""
    return _required_env("COS_BUCKET")


def configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in ("COS_REGION", "COS_BUCKET", "COS_SECRET_ID", "COS_SECRET_KEY")
    )


def client() -> CosS3Client:
    config = CosConfig(
        Region=_required_env("COS_REGION"),
        SecretId=_required_env("COS_SECRET_ID"),
        SecretKey=_required_env("COS_SECRET_KEY"),
        Token=os.getenv("COS_SESSION_TOKEN") or None,
        Scheme="https",
    )
    return CosS3Client(config)


def _download_client() -> CosS3Client:
    domain = os.getenv("COS_CDN_DOMAIN", "").strip()
    if not domain:
        return client()
    domain = domain.removeprefix("https://").removeprefix("http://").rstrip("/")
    config = CosConfig(
        Region=_required_env("COS_REGION"),
        SecretId=_required_env("COS_SECRET_ID"),
        SecretKey=_required_env("COS_SECRET_KEY"),
        Token=os.getenv("COS_SESSION_TOKEN") or None,
        Scheme="https",
        Domain=domain,
        # A CDN normally rewrites the origin Host header. Excluding Host keeps
        # the COS query signature valid through that private CDN origin.
        SignHost=False,
    )
    return CosS3Client(config)


_relay_state = {"checked": 0.0, "up": False}


def _relay_base() -> str:
    """The download relay (a same-region Lighthouse with unmetered bandwidth that reads COS over Tencent's free
    internal network), or "" when unset or unreachable so links fall back to COS itself."""
    base = os.getenv("COS_DOWNLOAD_RELAY", "").strip().rstrip("/")
    if not base:
        return ""
    _ensure_prober()
    if _relay_state["checked"] == 0.0:
        _refresh_download_relay()  # the very first request of a process waits once; later ones never do
    return base if _relay_state["up"] else ""


_prober_lock = __import__("threading").Lock()
_prober_started = False


def _refresh_download_relay() -> None:
    base = os.getenv("COS_DOWNLOAD_RELAY", "").strip().rstrip("/")
    now = time.time()
    if not base or now < _relay_state["checked"] + 55:  # also honours a pause set by relay_failed()
        return
    up, left = _probe_relay(base, now)
    _relay_state.update(checked=now, up=up, cert_hours_left=left)


def _refresh_upload_relays() -> None:
    now = time.time()
    for base in _upload_bases():
        checked, _ = _upload_state.get(base, (0.0, False))
        if now - checked >= 55:
            _upload_state[base] = (now, _probe_upload_relay(base, now))


def _ensure_prober() -> None:
    """Health checks run in a background thread every 30 s, so requests only read the cached verdicts and a
    repaired relay is picked up again within a minute."""
    global _prober_started
    if _prober_started:
        return
    with _prober_lock:
        if _prober_started:
            return
        _prober_started = True
        import threading

        def loop() -> None:
            while True:
                for refresh in (_refresh_download_relay, _refresh_upload_relays):
                    try:
                        refresh()
                    except Exception:  # noqa: BLE001 - a failed check just keeps the previous verdict
                        pass
                time.sleep(30)
        threading.Thread(target=loop, name="relay-health", daemon=True).start()


RELAY_CERT_MARGIN_HOURS = 24


def _tls_hours_left(base: str, now: float) -> float | None:
    """Hours of certificate left on a verified TLS connection to base, or None when unreachable or untrusted."""
    import socket
    import ssl
    from urllib.parse import urlsplit
    parts = urlsplit(base)
    try:
        context = ssl.create_default_context()
        with socket.create_connection((parts.hostname, parts.port or 443), timeout=3) as raw, \
                context.wrap_socket(raw, server_hostname=parts.hostname) as tls:
            not_after = ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])
    except Exception:  # noqa: BLE001 - unreachable, expired or untrusted
        return None
    return round((not_after - now) / 3600, 1)


def _probe_relay(base: str, now: float) -> tuple[bool, float | None]:
    """Up only when the relay answers over a verified TLS connection whose certificate has more than a day left,
    so no link it signs can expire in the middle of a download (its IP certificate lives six days)."""
    left = _tls_hours_left(base, now)
    if left is None:
        return False, None
    if left <= RELAY_CERT_MARGIN_HOURS:
        return False, left
    # A real signed round trip on an awkwardly named object ("+", spaces, brackets, CJK): a proxy that re-encodes
    # paths breaks COS signatures for such names, and must never be handed out as a download link.
    import urllib.request
    origin = f"https://{bucket_name()}.cos.{os.getenv('COS_REGION', '').strip()}.myqcloud.com/"
    signed = client().get_presigned_url(Method="GET", Bucket=bucket_name(), Key=RELAY_CANARY_KEY, Expired=120)
    try:
        with urllib.request.urlopen(base + "/" + signed[len(origin):], timeout=5) as response:
            ok = response.status == 200 and response.read(64) == b"relay-canary"
    except Exception:  # noqa: BLE001
        ok = False
    return ok, left


RELAY_CANARY_KEY = "relay-canary/a b+c (1) 中文.txt"  # contains "relay-canary"; uploaded once, never expires


_upload_state: dict[str, tuple[float, bool]] = {}


def upload_relay() -> str | None:
    """The first healthy browser-upload relay (H3_UPLOAD_RELAY_BASE_URL, then H3_UPLOAD_RELAY_FALLBACK_URL), or ""
    when relays are configured but none is healthy (uploads then go through the API itself), or None when no relay
    is configured. Checked at most once a minute each, so a repaired relay is used again automatically."""
    bases = _upload_bases()
    if not bases:
        return None
    _ensure_prober()
    if any(base not in _upload_state for base in bases):
        _refresh_upload_relays()  # first use in this process
    for base in bases:
        if _upload_state.get(base, (0.0, False))[1]:
            return base
    return ""


def _upload_bases() -> list[str]:
    return [b.strip().rstrip("/") for b in (os.getenv("H3_UPLOAD_RELAY_BASE_URL", ""),
                                            os.getenv("H3_UPLOAD_RELAY_FALLBACK_URL", "")) if b.strip()]


def _probe_upload_relay(base: str, now: float) -> bool:
    left = _tls_hours_left(base, now)
    if left is None or left <= RELAY_CERT_MARGIN_HOURS:
        return False
    import urllib.request
    try:
        with urllib.request.urlopen(base + "/health", timeout=5) as response:
            return response.status == 200 and bool(json.loads(response.read(512) or b"{}").get("ok"))
    except Exception:  # noqa: BLE001
        return False


RELAY_PAUSE_SECONDS = 3600


def relay_failed(message: str) -> bool:
    """A GPU worker could not fetch an input through the relay: stop handing out relay links for an hour (COS
    links still work), so one unforeseen relay problem can fail at most the jobs already holding its links."""
    from urllib.parse import urlsplit
    base = os.getenv("COS_DOWNLOAD_RELAY", "").strip()
    host = urlsplit(base).hostname if base else None
    if not host or host not in str(message or ""):
        return False
    import logging
    logging.getLogger(__name__).error("Download relay paused for %ss after a worker failure: %s",
                                      RELAY_PAUSE_SECONDS, str(message)[:500])
    _relay_state.update(up=False, checked=time.time() + RELAY_PAUSE_SECONDS - 60, paused_at=time.time())
    return True


def relay_status() -> dict:
    """For the admin board: whether downloads go through the relay and how long its certificate has left."""
    base = os.getenv("COS_DOWNLOAD_RELAY", "").strip()
    upload = upload_relay()
    if not base:
        return {"enabled": False, "upload": upload}
    _relay_base()
    return {"enabled": True, "up": _relay_state["up"], "cert_hours_left": _relay_state.get("cert_hours_left"),
            "paused_at": _relay_state.get("paused_at"), "upload": upload}


def via_relay(url: str) -> str:
    """Send a signed COS GET through the relay; the path and signed query stay byte-for-byte identical."""
    base = _relay_base()
    origin = f"https://{bucket_name()}.cos.{os.getenv('COS_REGION', '').strip()}.myqcloud.com/"
    return base + "/" + url[len(origin):] if base and url.startswith(origin) else url


def presigned_url(key: str, *, download: bool = False, expires: int | None = None) -> str:
    params = {}
    if download:
        params["response-content-disposition"] = (
            f'attachment; filename="{Path(key).name}"'
        )
    signer = _download_client()
    url = signer.get_presigned_url(
        Method="GET",
        Bucket=bucket_name(),
        Key=key,
        Expired=expires or int(os.getenv("COS_PRESIGN_SECONDS", "3600")),
        Params=params,
        SignHost=False if os.getenv("COS_CDN_DOMAIN", "").strip() else None,
    )
    return url if os.getenv("COS_CDN_DOMAIN", "").strip() else via_relay(url)


def presigned_origin_url(key: str, *, expires: int = 3600) -> str:
    """Sign a COS GET URL for GPU-to-COS transfers (through the download relay when one is up)."""
    return via_relay(client().get_presigned_url(
        Method="GET",
        Bucket=bucket_name(),
        Key=key,
        Expired=expires,
    ))


def presigned_put_url(key: str, *, expires: int = 7200) -> str:
    """Return a time-limited upload URL scoped to one exact COS object."""
    return client().get_presigned_url(
        Method="PUT",
        Bucket=bucket_name(),
        Key=key,
        Expired=expires,
    )


def upload_fileobj(fileobj: BinaryIO, key: str, content_type: str) -> None:
    client().put_object(
        Bucket=bucket_name(),
        Key=key,
        Body=fileobj,
        ContentType=content_type,
    )


def object_metadata(key: str) -> dict:
    value = client().head_object(Bucket=bucket_name(), Key=key)
    normalized = {str(name).lower(): item for name, item in value.items()}
    return {
        "size": int(normalized.get("content-length") or normalized.get("contentlength") or 0),
        "content_type": str(normalized.get("content-type") or normalized.get("contenttype") or ""),
        "etag": str(normalized.get("etag") or "").strip('"'),
    }


def read_prefix(key: str, size: int = 16) -> bytes:
    response = client().get_object(
        Bucket=bucket_name(), Key=key, Range=f"bytes=0-{max(0, size - 1)}"
    )
    return response["Body"].get_raw_stream().read(size)


def _upload_grant_secret() -> bytes:
    value = (
        os.getenv("H3_UPLOAD_GRANT_SECRET")
        or os.getenv("H3_TRANSFER_SECRET")
        or os.getenv("H3_DOWNLOAD_SECRET", "")
    ).strip()
    if len(value) < 32:
        raise RuntimeError("素材直传签名密钥必须至少为 32 个字符")
    return value.encode()


def _upload_grant_payload(value: dict) -> bytes:
    fields = {
        key: value[key]
        for key in (
            "merchant_id", "field", "filename", "content_type", "size_bytes",
            "key", "expires_at",
        )
    }
    return json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def create_upload_grant(
    merchant_id: str,
    field: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    *,
    expires: int = 3600,
) -> dict:
    safe_name = Path(filename).name[:180] or "asset"
    prefix = os.getenv("COS_INPUT_PREFIX", "inputs").strip("/")
    key = f"{prefix}/{merchant_id}/staged/{uuid.uuid4().hex}/{safe_name}"
    grant = {
        "merchant_id": merchant_id,
        "field": field,
        "filename": safe_name,
        "content_type": content_type,
        "size_bytes": int(size_bytes),
        "key": key,
        "expires_at": int(time.time()) + expires,
    }
    grant["signature"] = hmac.new(
        _upload_grant_secret(), _upload_grant_payload(grant), hashlib.sha256
    ).hexdigest()
    grant["upload_url"] = presigned_put_url(key, expires=expires)
    return grant


def verify_upload_grant(value: dict, merchant_id: str) -> bool:
    try:
        if value.get("merchant_id") != merchant_id:
            return False
        expires_at = int(value["expires_at"])
        if expires_at < int(time.time()) or expires_at > int(time.time()) + 3700:
            return False
        prefix = os.getenv("COS_INPUT_PREFIX", "inputs").strip("/")
        if not str(value["key"]).startswith(f"{prefix}/{merchant_id}/staged/"):
            return False
        expected = hmac.new(
            _upload_grant_secret(), _upload_grant_payload(value), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, str(value.get("signature") or ""))
    except (KeyError, TypeError, ValueError):
        return False


def upload_path(path: Path, key: str, content_type: str = "application/octet-stream") -> None:
    with path.open("rb") as handle:
        client().put_object(
            Bucket=bucket_name(), Key=key, Body=handle, ContentType=content_type,
            ServerSideEncryption="AES256",
        )


def download_path(key: str, path: Path) -> None:
    """Download one COS object to a local path without buffering it in memory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if _relay_base():
        import urllib.request
        try:
            with urllib.request.urlopen(presigned_origin_url(key, expires=900), timeout=120) as stream, \
                    path.open("wb") as handle:
                shutil.copyfileobj(stream, handle, length=1024 * 1024)
            return
        except Exception:  # noqa: BLE001 - the relay is an optimisation; COS itself still works
            path.unlink(missing_ok=True)
    response = client().get_object(Bucket=bucket_name(), Key=key)
    stream = response["Body"].get_raw_stream()
    with path.open("wb") as handle:
        shutil.copyfileobj(stream, handle, length=1024 * 1024)


def delete_keys(keys: list[str]) -> None:
    if not keys:
        return
    client().delete_objects(
        Bucket=bucket_name(),
        Delete={"Object": [{"Key": key} for key in keys], "Quiet": "true"},
    )


def list_objects(prefix: str) -> list[dict]:
    """列出前缀下的对象；处理 COS 分页，返回最小稳定字段集。"""
    marker = ""
    items: list[dict] = []
    while True:
        response = client().list_objects(
            Bucket=bucket_name(), Prefix=prefix, Marker=marker, MaxKeys=1000
        )
        for item in response.get("Contents") or []:
            items.append(
                {
                    "key": item.get("Key"),
                    "size": int(item.get("Size") or 0),
                    "etag": str(item.get("ETag") or "").strip('"'),
                    "last_modified": item.get("LastModified"),
                }
            )
        if response.get("IsTruncated") != "true":
            break
        marker = response.get("NextMarker") or ""
        if not marker:
            break
    return items


def read_json(key: str, *, max_bytes: int = 64 * 1024) -> dict | None:
    """Read a small internal JSON object without exposing a signed URL."""
    try:
        response = client().get_object(Bucket=bucket_name(), Key=key)
    except Exception as exc:  # qcloud_cos does not expose one stable not-found type
        status = getattr(exc, "get_status_code", lambda: None)()
        if status == 404:
            return None
        raise
    raw = response["Body"].get_raw_stream().read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("JSON object exceeds diagnostic size limit")
    value = __import__("json").loads(raw.decode("utf-8"))
    return value if isinstance(value, dict) else None


def configure_lifecycle() -> None:
    """COS 兜底生命周期。应用层仍按每个任务的精确到期时间清理。"""
    rules = [
        {
            "ID": "h3-temporary-inputs",
            "Status": "Enabled",
            "Filter": {"Prefix": os.getenv("COS_INPUT_PREFIX", "inputs").strip("/") + "/"},
            "Expiration": {"Days": "2"},
        },
        {
            "ID": "h3-temporary-outputs",
            "Status": "Enabled",
            "Filter": {"Prefix": os.getenv("COS_OUTPUT_PREFIX", "outputs").strip("/") + "/"},
            "Expiration": {"Days": "4"},
        },
    ]
    client().put_bucket_lifecycle(Bucket=bucket_name(), LifecycleConfiguration={"Rule": rules})
