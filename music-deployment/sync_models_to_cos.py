"""Download pinned music model snapshots from ModelScope and upload them to COS.

Credentials stay in the server env file. Existing COS objects are never deleted.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from qcloud_cos import CosConfig, CosS3Client


RELEASE = "2026-09-29-yue2-sheetsage2"
PREFIX = f"models/h3-standard/music/{RELEASE}"
MODELS = (
    ("m-a-p/YuE2-3B", "yue2/YuE2-3B"),
    ("m-a-p/YuE2-Vae", "yue2-vae/YuE2-Vae"),
    ("m-a-p/SheetSage2", "sheetsage2/SheetSage2"),
    ("m-a-p/MERT-v2-FullSong", "mert/MERT-v2-FullSong"),
)


def load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value or value.startswith("#") or "=" not in value:
            continue
        key, item = value.split("=", 1)
        os.environ.setdefault(key.strip(), item.strip().strip("'\""))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def upload_tree(client: CosS3Client, bucket: str, root: Path, target: str) -> list[dict]:
    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        key = f"{PREFIX}/{target}/{relative}"
        size = path.stat().st_size
        checksum = sha256(path)
        client.upload_file(
            Bucket=bucket,
            LocalFilePath=str(path),
            Key=key,
            PartSize=16,
            MAXThread=8,
            EnableMD5=True,
        )
        remote = client.head_object(Bucket=bucket, Key=key)
        remote_size = int(remote.get("Content-Length") or remote.get("ContentLength") or 0)
        if remote_size != size:
            raise RuntimeError(f"COS size mismatch for {key}: {remote_size} != {size}")
        files.append({"key": key, "bytes": size, "sha256": checksum})
        print(f"uploaded {key} ({size} bytes)", flush=True)
    return files


def put_json(client: CosS3Client, bucket: str, key: str, value: dict) -> None:
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )


def main() -> int:
    env_path = Path(os.getenv("EQUAI_ENV_FILE", "/opt/equai-local-api/api.env"))
    staging = Path(os.getenv("EQUAI_MODEL_STAGING", "/opt/equai-model-download/staging"))
    load_env(env_path)
    required = ("COS_REGION", "COS_BUCKET", "COS_SECRET_ID", "COS_SECRET_KEY")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("missing environment fields: " + ", ".join(missing))
    client = CosS3Client(CosConfig(
        Region=os.environ["COS_REGION"],
        SecretId=os.environ["COS_SECRET_ID"],
        SecretKey=os.environ["COS_SECRET_KEY"],
    ))
    bucket = os.environ["COS_BUCKET"]
    manifest = {"release": RELEASE, "cos_prefix": PREFIX, "models": []}
    staging.mkdir(parents=True, exist_ok=True)
    for repo, target in MODELS:
        local = staging / target.replace("/", "-")
        subprocess.run([
            sys.executable, "-m", "modelscope.cli.cli", "download", repo,
            "--local-dir", str(local), "--max-workers", "8",
        ], check=True)
        files = upload_tree(client, bucket, local, target)
        manifest["models"].append({"source": repo, "target": target, "files": files})
        # This directory was created solely as a transfer staging area.
        shutil.rmtree(local)
    put_json(client, bucket, f"{PREFIX}/manifest.json", manifest)
    put_json(client, bucket, "models/h3-standard/music/current.json", {
        "release": RELEASE,
        "manifest": f"{PREFIX}/manifest.json",
    })
    print(json.dumps({"ok": True, "bucket": bucket, "prefix": PREFIX}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
