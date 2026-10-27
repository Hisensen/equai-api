import importlib.util
import asyncio
import json
import time
import shutil
import subprocess
import sys
import types
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import gongji
import database
import scheduler
import voice_clone_api
from voice_clone_worker import worker as voice_worker
from main import app
from security import video_merchant


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / "voice_clone_worker" / "workflow.py"
SPEC = importlib.util.spec_from_file_location("voice_clone_workflow", WORKFLOW_PATH)
assert SPEC and SPEC.loader
workflow = importlib.util.module_from_spec(SPEC)
import sys
sys.modules[SPEC.name] = workflow
SPEC.loader.exec_module(workflow)


def test_supplied_workflow_defaults_and_optional_emotion():
    assert workflow.DEFAULT_WORKFLOW.reference_end_seconds == 12
    assert workflow.inference_kwargs(emotion_audio="emotion.wav", emotion_weight=0.6) == {
        "emo_audio_prompt": "emotion.wav", "emo_alpha": 0.6,
        "top_k": 30, "top_p": 0.8, "temperature": 0.8,
        "num_beams": 3, "max_mel_tokens": 1500,
        "max_text_tokens_per_segment": 120,
    }
    assert workflow.inference_kwargs(emotion_audio=None, emotion_weight=0.6)["emo_alpha"] == 1.0


@pytest.mark.parametrize("value", ["", "  ", "a" * 20001])
def test_rejects_invalid_text(value):
    with pytest.raises(ValueError):
        workflow.validate_request(value)


def test_voice_clone_has_separate_queue_and_api_contract():
    assert scheduler._queue_for('{"_tool":"voice_clone"}') == "voice_clone"
    assert scheduler._queue_for('{"_tool":"music"}') == "music"
    paths = app.openapi()["paths"]
    assert "/v1/voice-clones" in paths
    assert "/v1/voice-clones/{task_id}/audio" in paths
    assert "/api/account/voice-clones" in paths


def test_public_availability_requires_worker_and_positive_price(monkeypatch):
    monkeypatch.setenv("H3_VOICE_CLONE_ENABLED", "1")
    monkeypatch.setenv("GONGJI_VOICE_CLONE_IMAGE", "registry/voice:test")
    monkeypatch.setenv("GONGJI_VOICE_CLONE_STORAGE_ID", "1234")
    monkeypatch.setenv("H3_VOICE_CLONE_PRICE_MILLI", "0")
    assert voice_clone_api._available() is False
    monkeypatch.setenv("H3_VOICE_CLONE_PRICE_MILLI", "200")
    assert voice_clone_api._available() is True


def test_disabled_voice_clone_rejects_create_without_gpu_job():
    app.dependency_overrides[video_merchant] = lambda: {
        "id": "test", "status": "active", "api_key_status": "active",
    }
    try:
        with patch("voice_clone_api._available", return_value=False), patch.object(
            gongji, "create_voice_clone_job", new_callable=AsyncMock,
        ) as submit:
            result = TestClient(app).post(
                "/v1/voice-clones",
                data={"text": "你好"},
                files={"voice": ("voice.wav", b"0" * 4096, "audio/wav")},
            )
        assert result.status_code == 503
        assert "尚未开放" in result.json()["detail"]
        submit.assert_not_awaited()
    finally:
        app.dependency_overrides.pop(video_merchant, None)


def test_creation_is_idempotent_and_isolated_from_video_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database, "DB", tmp_path / "voice-test.sqlite3")
    database.init_db()
    now = time.time()
    with database.transaction() as db:
        db.execute(
            """INSERT INTO merchants(id,username,password_hash,recovery_hash,status,
               balance_milli,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
            ("merchant-voice", "voice-user", "hash", "hash", "active", 5000, now, now),
        )

    async def fake_upload(_merchant, task_id, index, field, upload, **_kwargs):
        return {"id": f"asset-{index}", "storage": "local", "bucket": "local-input",
                "key": f"merchant-voice/{task_id}/{index}-{field}.wav", "field": field,
                "filename": upload.filename, "content_type": "audio/wav", "size_bytes": 4096}

    merchant = {"id": "merchant-voice", "status": "active", "api_key_status": "active"}
    async def make_call(text="测试一句话"):
        return await voice_clone_api.create_voice_clone(
            merchant=merchant,
            voice=__import__("starlette.datastructures", fromlist=["UploadFile"]).UploadFile(
                BytesIO(b"RIFF" + b"0" * 4092), filename="voice.wav"),
            text=text, emotion=None, emotion_weight=1.0, idempotency_key="voice-order-1",
        )

    monkeypatch.setattr(voice_clone_api, "_available", lambda: True)
    monkeypatch.setattr(voice_clone_api, "_upload", fake_upload)
    monkeypatch.setattr(voice_clone_api, "_price_milli", lambda: 200)
    first = asyncio.run(make_call())
    second = asyncio.run(make_call())
    assert first["id"] == second["id"]
    with database.connect() as db:
        row = db.execute("SELECT * FROM merchant_tasks WHERE id=?", (first["id"],)).fetchone()
        balance = db.execute("SELECT balance_milli,reserved_milli FROM merchants WHERE id=?",
                             (merchant["id"],)).fetchone()
    assert row["status"] == "queued" and row["kind"] == "voice_clone"
    assert scheduler._queue_for(row["payload"]) == "voice_clone"
    assert json.loads(row["input_objects"])[0]["field"] == "voice"
    assert tuple(balance) == (4800, 200)
    with patch.object(scheduler, "local_output_capacity_ok", return_value=True), patch.object(
        scheduler, "local_transfer_capacity_ok", return_value=True,
    ), patch.object(scheduler, "_input_url", return_value="https://input.invalid/voice"), patch.object(
        gongji, "create_voice_clone_job", new_callable=AsyncMock, return_value=12345,
    ) as submit:
        assert asyncio.run(scheduler._dispatch_once("voice_clone")) is True
    sent = submit.call_args.args[2]
    assert sent["text"] == "测试一句话"
    assert sent["media"][0]["field"] == "voice"
    with database.connect() as db:
        dispatched = db.execute("SELECT * FROM merchant_tasks WHERE id=?",
                                (first["id"],)).fetchone()
    assert (dispatched["status"], dispatched["remote_id"]) == ("running", "12345")
    short_audio = {"name": "01-output.mp3", "storage": "local",
                   "bucket": "local", "path": "merchant-voice/voc/01-output.mp3",
                   "content_type": "audio/mpeg", "size_bytes": 4096}
    with patch.object(scheduler, "_transfer_backend", return_value="local"), patch.object(
        scheduler, "list_task_media", return_value=[short_audio],
    ) as find_audio:
        assert asyncio.run(scheduler._complete_locked(dispatched, {"billing": 0.01})) is True
    find_audio.assert_called_once_with("merchant-voice", first["id"], 2048)
    with database.connect() as db:
        result = db.execute("SELECT status,artifacts FROM merchant_tasks WHERE id=?",
                            (first["id"],)).fetchone()
        balance = db.execute("SELECT balance_milli,reserved_milli FROM merchants WHERE id=?",
                             (merchant["id"],)).fetchone()
    assert result["status"] == "completed"
    assert json.loads(result["artifacts"])[0]["name"] == "01-output.mp3"
    assert tuple(balance) == (4800, 0)
    monkeypatch.setenv("H3_LOCAL_OUTPUT_DIR", str(tmp_path / "outputs"))
    audio_path = tmp_path / "outputs" / short_audio["path"]
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"ID3" + b"0" * 4093)
    app.dependency_overrides[video_merchant] = lambda: merchant
    try:
        response = TestClient(app).get(f"/v1/voice-clones/{first['id']}/audio")
        assert response.status_code == 200
        assert response.content == audio_path.read_bytes()
        app.dependency_overrides[video_merchant] = lambda: {
            "id": "another-merchant", "status": "active", "api_key_status": "active",
        }
        assert TestClient(app).get(f"/v1/voice-clones/{first['id']}/audio").status_code == 404
    finally:
        app.dependency_overrides.pop(video_merchant, None)


def test_worker_crops_reference_and_uploads_mp3(tmp_path, monkeypatch):
    source = tmp_path / "reference.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=330:duration=13", str(source)], check=True)
    model = tmp_path / "models"
    model.mkdir()
    (model / "config.yaml").touch()
    monkeypatch.setenv("INDEXTTS2_MODEL_DIR", str(model))
    monkeypatch.setenv("H3_OUTPUT_UPLOAD_TARGETS", json.dumps([{"url": "https://out.invalid/audio"}]))
    monkeypatch.setenv("H3_JOB_RESULT_UPLOAD_TARGET", json.dumps({"url": "https://out.invalid/result"}))
    monkeypatch.setattr(voice_worker, "REQUIRED_MODEL_FILES", ())
    monkeypatch.setattr(voice_worker, "REQUIRED_MODEL_DIRS", ())
    monkeypatch.setattr(voice_worker, "download", lambda _url, dest: shutil.copyfile(source, dest))
    outputs = {}
    monkeypatch.setattr(voice_worker, "_put", lambda url, data, kind: outputs.__setitem__(url, (data, kind)))

    class FakeIndexTTS2:
        def __init__(self, **kwargs):
            assert kwargs["use_qwen_emo"] is False
            assert kwargs["aux_paths"]["w2v_bert"].endswith("w2v-bert-2.0")

        def infer(self, *, spk_audio_prompt, output_path, text, **kwargs):
            assert text == "测试语音"
            assert kwargs["top_k"] == 30
            assert 11.9 <= float(subprocess.check_output([
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", spk_audio_prompt,
            ])) <= 12.1
            shutil.copyfile(spk_audio_prompt, output_path)

    package = types.ModuleType("indextts")
    package.__path__ = []
    infer_module = types.ModuleType("indextts.infer_v2")
    infer_module.IndexTTS2 = FakeIndexTTS2
    monkeypatch.setitem(sys.modules, "indextts", package)
    monkeypatch.setitem(sys.modules, "indextts.infer_v2", infer_module)
    voice_worker.run({"text": "测试语音", "media": [
        {"field": "voice", "url": "https://input.invalid/reference"}]}, "voc-test")
    audio, kind = outputs["https://out.invalid/audio"]
    assert kind == "audio/mpeg" and len(audio) > 2048
    assert json.loads(outputs["https://out.invalid/result"][0])["ok"] is True


@pytest.mark.asyncio
async def test_voice_clone_job_uses_its_own_image_and_model_disk(monkeypatch):
    monkeypatch.setenv("GONGJI_VOICE_CLONE_IMAGE", "registry/voice:tested")
    monkeypatch.setenv("GONGJI_VOICE_CLONE_STORAGE_ID", "9876")
    with patch.object(gongji, "_select_available_profile", new_callable=AsyncMock) as select, patch.object(
        gongji, "create_job", new_callable=AsyncMock, return_value=123,
    ) as submit:
        select.side_effect = lambda profile, *_args, **_kw: (profile, {"mark": "test"})
        result = await gongji.create_voice_clone_job("voc_test", "mer_test", {"text": "你好"})
    assert result == 123
    kwargs = submit.call_args.kwargs
    assert kwargs["selected_profile"]["image"] == "registry/voice:tested"
    assert kwargs["selected_profile"]["storage_id"] == 9876
    assert kwargs["start_script"]["args"] == ["/app/worker.py"]
