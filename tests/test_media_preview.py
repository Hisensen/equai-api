from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from contextlib import nullcontext

import pytest
from PIL import Image

import media_preview
import music_api
import video_api


def test_image_preview_is_smaller_and_download_source_stays_original(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("H3_LOCAL_OUTPUT_DIR", str(tmp_path))
    source = tmp_path / "merchant" / "task" / "edited.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (2400, 1600), (42, 86, 130)).save(source)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    artifact = {"media": "image", "storage": "local", "path": "merchant/task/edited.png"}

    preview, content_type = media_preview.create_preview("merchant", "task", artifact, "media")

    assert content_type == "image/webp"
    with Image.open(preview) as image:
        assert image.size == (1280, 853)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert media_preview.create_preview("merchant", "task", artifact, "poster")[0].is_file()
    media_preview.remove_task_previews("merchant", "task")
    assert not preview.exists()
    assert source.exists()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg unavailable")
def test_video_preview_preserves_ratio_and_original(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("H3_LOCAL_OUTPUT_DIR", str(tmp_path))
    source = tmp_path / "merchant" / "task" / "output.mp4"
    source.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=1280x720:rate=10", "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
                    "-y", str(source)], check=True)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    artifact = {"media": "video", "storage": "local", "path": "merchant/task/output.mp4"}

    poster, poster_type = media_preview.create_preview("merchant", "task", artifact, "poster")
    preview, video_type = media_preview.create_preview("merchant", "task", artifact, "media")
    dimensions = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                                      "-show_entries", "stream=width,height", "-of", "json", str(preview)]))["streams"][0]

    assert poster_type == "image/jpeg" and poster.is_file()
    assert video_type == "video/mp4"
    assert dimensions["width"] == 960 and dimensions["height"] == 540
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash


def test_music_history_omits_missing_completed_mp3_and_keeps_failures(monkeypatch) -> None:
    rows = [
        {"id": "existing", "status": "completed", "artifacts": json.dumps([{"name": "song.mp3", "storage": "local", "path": "existing.mp3"}]), "output_expires_at": None},
        {"id": "missing", "status": "completed", "artifacts": json.dumps([{"name": "song.mp3", "storage": "local", "path": "missing.mp3"}]), "output_expires_at": None},
        {"id": "failed", "status": "failed", "artifacts": "[]", "output_expires_at": None},
    ]
    class Result:
        def fetchall(self): return rows
    class DB:
        def execute(self, sql, params):
            assert "portal_hidden_at IS NULL" in sql
            return Result()
    monkeypatch.setattr(music_api, "_require_music", lambda _id: None)
    monkeypatch.setattr(music_api, "connect", lambda: nullcontext(DB()))
    monkeypatch.setattr(music_api, "_music_task_view", lambda row, **_: row)
    def resolve(path):
        if path == "missing.mp3": raise FileNotFoundError(path)
        return path
    monkeypatch.setattr(music_api.local_storage, "resolve", resolve)

    assert [row["id"] for row in music_api.list_music({"id": "merchant"})["items"]] == ["existing", "failed"]


def test_running_music_cannot_be_hidden(monkeypatch) -> None:
    class Result:
        def fetchone(self): return {"status": "music_running", "portal_hidden_at": None}
    class DB:
        def execute(self, *_args): return Result()
    monkeypatch.setattr(video_api, "transaction", lambda: nullcontext(DB()))
    with pytest.raises(video_api.HTTPException) as error:
        video_api.hide_portal_task("mus_running", {"id": "merchant"})
    assert error.value.status_code == 409
