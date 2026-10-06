from __future__ import annotations

import importlib.util
import asyncio
import json
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from starlette.datastructures import UploadFile
from unittest.mock import patch

import gongji
import music_api
from main import app

_worker_spec = importlib.util.spec_from_file_location(
    "music_worker", Path(__file__).parents[1] / "music-worker" / "worker.py"
)
music_worker = importlib.util.module_from_spec(_worker_spec)
_worker_spec.loader.exec_module(music_worker)


def test_music_output_targets_accept_two_named_audio_files() -> None:
    with patch.object(gongji, "signed_output_url", side_effect=lambda task, slot, **_: f"https://transfer/{task}/{slot}"):
        targets = gongji._output_upload_targets(
            "mus_test", "mer_test", 7200, "local", ["song.mp3", "song.flac"]
        )

    assert len(targets) == 4
    assert [item["name"] for item in targets] == [
        "song.mp3", "song.flac", "output-3.mp4", "output-4.mp4"
    ]
    assert targets[0]["key"].endswith("/00-song.mp3")
    assert targets[1]["key"].endswith("/01-song.flac")


def test_music_create_reads_idempotency_key_from_request_header() -> None:
    schema = app.openapi()
    operation = schema["paths"]["/api/account/music"]["post"]
    assert any(
        parameter["name"] == "Idempotency-Key" and parameter["in"] == "header"
        for parameter in operation.get("parameters", [])
    )
    assert "/api/account/music/tasks/{task_id}/generate" in schema["paths"]


def test_music_worker_only_allows_missing_lyrics_for_instrumentals() -> None:
    assert music_worker.resolve_lyrics("  words  ", "摇滚", "女声") == "words"
    assert music_worker.resolve_lyrics("", "纯器乐", "不指定") == "[Instrumental]"
    try:
        music_worker.resolve_lyrics("", "摇滚", "不指定")
    except RuntimeError as exc:
        assert "缺少歌词" in str(exc)
    else:
        raise AssertionError("vocal music must require confirmed lyrics")


def test_music_worker_error_report_redacts_urls_and_secret_values() -> None:
    message = music_worker._safe_error(RuntimeError(
        "fetch https://example.test/input?signature=abc token=private-value"
    ))
    assert "https://" not in message
    assert "private-value" not in message
    assert "[redacted]" in message


def test_music_worker_uses_the_mounted_h3_models_prefix(monkeypatch) -> None:
    monkeypatch.delenv("MUSIC_MODEL_ROOT", raising=False)
    monkeypatch.setenv("MUSIC_MODEL_RELEASE", "test-release")
    assert music_worker.model_root() == Path("/models/music/test-release")


def test_music_worker_copies_remote_code_from_the_read_only_mount(tmp_path) -> None:
    source = tmp_path / "mounted" / "SheetSage2"
    source.mkdir(parents=True)
    (source / "model.py").write_text("class Model: pass\n")
    target = music_worker.copy_sheetsage_code(source, tmp_path / "job")
    assert (target / "model.py").read_text() == "class Model: pass\n"


def test_music_worker_uses_a_writable_remote_code_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("HF_MODULES_CACHE", raising=False)
    cache = music_worker.configure_hf_modules_cache(tmp_path / "job")
    assert cache.is_dir()
    assert Path(__import__("os").environ["HF_MODULES_CACHE"]) == cache


def test_sheetsage_repairs_a_missing_transformers_remote_module(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MUSIC_MODEL_ROOT", str(tmp_path))
    monkeypatch.setenv("MUSIC_MODEL_RELEASE", "test-release")
    source = tmp_path / "sheetsage2" / "SheetSage2"
    source.mkdir(parents=True)
    base_model = tmp_path / "test-release" / "mert" / "MERT-v2-FullSong"
    base_model.mkdir(parents=True)
    (source / "chord_spelling_sheetsage2.py").write_text("# cached remote module\n")
    cached = tmp_path / "hf" / "transformers_modules" / "sheetsage2" / "hash" / "chord_spelling_sheetsage2.py"
    calls = []

    class Model:
        def eval(self): return self
        def to(self, _device): return self

    class AutoModel:
        @staticmethod
        def from_pretrained(*_args, **_kwargs):
            assert _kwargs["base_model_path"] == str(base_model)
            calls.append(1)
            if len(calls) == 1:
                raise FileNotFoundError(2, "missing remote module", str(cached))
            return Model()

    monkeypatch.setitem(__import__("sys").modules, "transformers", type("Transformers", (), {"AutoModel": AutoModel})())
    assert isinstance(music_worker.load_sheetsage_model(source), Model)
    assert cached.read_text() == "# cached remote module\n"
    assert len(calls) == 2


def test_music_is_one_point_and_available_to_every_merchant(monkeypatch) -> None:
    assert music_api.MUSIC_PRICE_MILLI == 500
    monkeypatch.setattr(music_api, "MUSIC_ENABLED", True)
    monkeypatch.setenv("GONGJI_MUSIC_IMAGE", "music-worker:test")
    assert music_api._music_available("merchant-internal")
    assert music_api._music_available("merchant-public")
    music_api._require_music("merchant-public")


def test_music_creation_accepts_the_production_merchant_record_shape(monkeypatch) -> None:
    monkeypatch.setenv("H3_MUSIC_ENABLED", "1")
    monkeypatch.setenv("GONGJI_MUSIC_IMAGE", "music-worker:test")
    monkeypatch.setattr(music_api, "MUSIC_ENABLED", True)
    statements = []

    class FakeDB:
        def execute(self, sql, params=()):
            statements.append((sql, params))
            return type("Result", (), {"rowcount": 1})()

    async def fake_upload(*_args, **_kwargs):
        return {"id": "asset-test", "bucket": "local-input", "key": "test.wav",
                "filename": "test.wav", "content_type": "audio/wav", "size_bytes": 16}

    monkeypatch.setattr(music_api, "_upload", fake_upload)
    monkeypatch.setattr(music_api, "transaction", lambda: nullcontext(FakeDB()))
    monkeypatch.setattr(music_api, "reserve_task", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(music_api, "_get_task", lambda *_args: {"id": "mus_test"})
    monkeypatch.setattr(music_api, "_music_task_view", lambda row, **_kwargs: row)

    # Production merchant rows have id/status, but no api_key_status column.
    result = asyncio.run(music_api.create_music(
        merchant={"id": "merchant-internal", "status": "active"},
        source=UploadFile(BytesIO(b"RIFF" + b"0" * 12), filename="test.wav"),
        style="纯器乐", voice="无人声", prompt="", lyrics="", title="test",
    ))

    assert result["id"] == "mus_test"
    task_insert = next(params for sql, params in statements if "INSERT INTO merchant_tasks" in sql)
    assert task_insert[7] == "music_queued"
    assert task_insert[14] == 0  # Failed attempts release the one-point reservation.
    assert json.loads(task_insert[10])["_transfer_backend"] == "local"
    assert json.loads(task_insert[10])["_output_names"] == ["song.mp3", "song.flac"]


def test_music_task_view_exposes_authenticated_mp3_preview_only(monkeypatch) -> None:
    monkeypatch.setattr(music_api, "_task_view", lambda *_args, **_kwargs: {
        "artifacts": [{"name": "song.mp3"}, {"name": "song.flac"}]
    })
    view = music_api._music_task_view({
        "id": "mus_test", "payload": "{}", "status": "completed"
    })
    assert [artifact["name"] for artifact in view["artifacts"]] == ["song.mp3"]
    assert view["artifacts"][0]["preview_url"] == "/api/account/music/tasks/mus_test/preview"
    assert view["artifacts"][0]["download_url"] == "/api/account/music/tasks/mus_test/download"


def test_music_preview_streams_inline_mp3_for_the_owning_merchant(tmp_path, monkeypatch) -> None:
    path = tmp_path / "song.mp3"
    path.write_bytes(b"fake-mp3")
    row = {
        "id": "mus_test", "kind": "music", "status": "completed",
        "output_expires_at": None,
        "artifacts": json.dumps([{"name": "song.mp3", "storage": "local", "path": "merchant/mus_test/00-song.mp3"}]),
    }
    calls = []
    monkeypatch.setattr(music_api, "_require_music", lambda merchant_id: None)
    monkeypatch.setattr(music_api, "_get_task", lambda merchant_id, task_id: calls.append((merchant_id, task_id)) or row)
    monkeypatch.setattr(music_api.local_storage, "resolve", lambda _relative: path)

    response = music_api.music_preview("mus_test", {"id": "merchant_test"})

    assert calls == [("merchant_test", "mus_test")]
    assert response.media_type == "audio/mpeg"
    assert "inline" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, max-age=300"


def test_music_download_streams_attachment_as_mpeg(tmp_path, monkeypatch) -> None:
    path = tmp_path / "song.mp3"
    path.write_bytes(b"fake-mp3")
    row = {
        "id": "mus_test", "kind": "music", "status": "completed",
        "output_expires_at": None,
        "artifacts": json.dumps([{"name": "song.mp3", "storage": "local", "path": "merchant/mus_test/00-song.mp3"}]),
    }
    monkeypatch.setattr(music_api, "_require_music", lambda _merchant_id: None)
    monkeypatch.setattr(music_api, "_get_task", lambda _merchant_id, _task_id: row)
    monkeypatch.setattr(music_api.local_storage, "resolve", lambda _relative: path)

    response = music_api.music_download("mus_test", {"id": "merchant_test"})

    assert response.media_type == "audio/mpeg"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"
