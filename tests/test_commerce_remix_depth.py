import asyncio
import importlib.util
import json
from io import BytesIO
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest
from fastapi import HTTPException, UploadFile

import commerce_remix
import remix_vision
import scheduler
from commerce import commerce_remix_price_milli, validate_commerce_remix_uploads


def test_depth_grayscale_maps_nearer_depth_to_white():
    gray = commerce_remix.depth_gray(np.array([[0.0, 1.0, 2.0, 3.0, 4.0]], dtype=np.float32))
    assert gray[0, 0] == 0
    assert gray[0, -1] == 255
    assert gray[0, 2] > gray[0, 1]


def test_nominal_15_second_commerce_upload_uses_h3_duration_limit(monkeypatch):
    import commerce

    monkeypatch.setattr(commerce, "inspect_remix", lambda *args, **kwargs: {
        "duration": 15, "source_seconds": 15.083333, "ratio": "9:16", "has_audio": True,
    })
    video = UploadFile(BytesIO(b"video"), filename="clip.mp4")
    result = asyncio.run(validate_commerce_remix_uploads([video], [], "[]"))
    assert result["duration"] == 15
    assert result["source_seconds"] == 15.0


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_commerce_work_video_is_trimmed_to_15_seconds_with_audio(tmp_path: Path):
    source = tmp_path / "source-360.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=black:s=360x640:r=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
        "-t", "15.083333", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", str(source),
    ], check=True, capture_output=True, timeout=60)
    assert commerce_remix._media_duration(source) > 15.0

    commerce_remix._trim_360(source)

    assert 14.9 <= commerce_remix._media_duration(source) <= 15.05
    streams = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
        "-of", "csv=p=0", str(source),
    ], check=True, capture_output=True, text=True, timeout=30)
    assert "audio" in streams.stdout.splitlines()
    assert not (tmp_path / "source-360-trim.mp4").exists()


def test_commerce_prompt_binds_uploaded_images_by_role_and_describes_missing_roles():
    prompt = commerce_remix._prompt({
        "summary": "A presenter demonstrates a product.",
        "style": "Handheld close shots.",
        "original": {"person": "a presenter", "product": "a bottle", "scene": "a market"},
        "shots": [{"start": 0, "end": 5, "text": "PERSON holds PRODUCT at PLACE."}],
        "transcript_timeline": [{"start": 1, "end": 2, "text": "Hello"}],
    }, ["product"])

    assert "<Video 1> is the 360p grayscale depth-map" in prompt
    assert "<Picture 1> is the user's 商品 reference image (商品参考图 1)" in prompt
    assert "PRODUCT means the product shown in <Picture 1>; use <Picture 1> only for PRODUCT" in prompt
    assert "No 人物 picture was uploaded" in prompt
    assert "No 场景 picture was uploaded" in prompt
    assert "1.00-2.00s: Hello" in prompt
    assert "original color video is not provided" in prompt


@pytest.mark.parametrize(("roles", "expected"), [
    (["person"], [(1, "人物", "PERSON")]),
    (["product"], [(1, "商品", "PRODUCT")]),
    (["scene"], [(1, "场景", "PLACE")]),
    (["person", "product"], [(1, "人物", "PERSON"), (2, "商品", "PRODUCT")]),
    (["person", "scene"], [(1, "人物", "PERSON"), (2, "场景", "PLACE")]),
    (["product", "scene"], [(1, "商品", "PRODUCT"), (2, "场景", "PLACE")]),
    (["person", "product", "scene"], [
        (1, "人物", "PERSON"), (2, "商品", "PRODUCT"), (3, "场景", "PLACE"),
    ]),
])
def test_commerce_prompt_numbers_each_uploaded_picture_by_its_role(roles, expected):
    prompt = commerce_remix._prompt({"shots": []}, roles)

    for number, label, token in expected:
        assert f"<Picture {number}> is the user's {label} reference image ({label}参考图 {number})" in prompt
        expected_subject = {"PERSON": "person", "PRODUCT": "product", "PLACE": "environment"}[token]
        assert f"{token} means the {expected_subject} shown in <Picture {number}>; use <Picture {number}> only for {token}" in prompt

    for role, label, token in (
        ("person", "人物", "PERSON"),
        ("product", "商品", "PRODUCT"),
        ("scene", "场景", "PLACE"),
    ):
        if role not in roles:
            assert f"No {label} picture was uploaded for {token}" in prompt
        else:
            assert f"No {label} picture was uploaded for {token}" not in prompt


def test_scene_reference_overrides_source_location_and_each_role_is_bound_in_shots():
    prompt = commerce_remix._prompt({
        "summary": "PERSON walks toward camera on a city sidewalk, holding PRODUCT.",
        "style": "Tight shots with warm skin tones.",
        "original": {"scene": "an urban street with parked cars", "scene_short": "city sidewalk"},
        "shots": [{
            "start": 0, "end": 5, "purpose": "introduce PERSON and setting",
            "text": "{PERSON} walks on a city sidewalk, holding {PRODUCT}; background remains consistent.",
        }],
    }, ["person", "product", "scene"])

    assert "city sidewalk" not in prompt
    assert "evening city sidewalk" not in prompt
    assert "<Picture 1>" in prompt and "<Picture 2>" in prompt and "<Picture 3>" in prompt
    assert "the person shown in <Picture 1>" in prompt
    assert "the product shown in <Picture 2>" in prompt
    assert "the environment shown in <Picture 3>" in prompt
    assert "the person shown in <Picture 1> walks on the environment shown in <Picture 3>, holding the product shown in <Picture 2>" in prompt
    assert "每个镜头都必须用场景参考图 <Picture 3> 完整替换原视频中的地点、背景、环境物体" in prompt
    assert "不得保留、复现或混合原视频中的背景" in prompt
    assert "warm skin tones" not in prompt
    assert "商品动作适配" in prompt
    assert "商品参考图 <Picture 2> 中实际可见的结构" in prompt
    assert "source descriptions never override the mapped reference pictures" in prompt


def test_roles_without_uploaded_pictures_keep_their_source_identity_and_scene():
    prompt = commerce_remix._prompt({
        "original": {
            "person": "a presenter in a red coat", "product": "a glass bottle",
            "scene": "a small kitchen with tiled walls",
        },
        "shots": [{"start": 0, "end": 5, "text": "PERSON holds PRODUCT in PLACE."}],
    }, ["person"])

    assert "No 商品 picture was uploaded for PRODUCT; preserve the original 商品" in prompt
    assert "No 场景 picture was uploaded for PLACE; preserve the original 场景" in prompt
    assert "Use its background geometry and scene layout to preserve the original environment" in prompt
    assert "No 人物 picture was uploaded for PERSON" not in prompt


def test_commerce_upload_rejects_role_order_that_could_bind_the_wrong_image(monkeypatch):
    import commerce

    monkeypatch.setattr(commerce, "inspect_remix", lambda *args, **kwargs: {
        "duration": 10, "source_seconds": 10, "ratio": "9:16", "has_audio": True,
    })
    video = UploadFile(BytesIO(b"video"), filename="clip.mp4")
    images = [UploadFile(BytesIO(b"image"), filename="scene.jpg"),
              UploadFile(BytesIO(b"image"), filename="person.jpg")]

    with pytest.raises(HTTPException, match="参考图片需按人物、商品、场景顺序填写用途"):
        asyncio.run(validate_commerce_remix_uploads(
            [video], images, '["scene","person"]',
        ))


def test_asr_sentences_include_timestamps_in_seconds():
    result = remix_vision._timed_transcript({
        "output": {"transcripts": [{"sentences": [
            {"begin_time": 1250, "end_time": 2450, "text": "开场"},
        ]}]}
    })
    assert result == [{"start": 1.25, "end": 2.45, "text": "开场"}]


def test_commerce_worker_gets_tencent_360_copy_and_role_images_only(monkeypatch):
    monkeypatch.setattr(scheduler, "_input_url", lambda row, item, **_: f"https://media.test/{item['filename']}")
    row = {
        "payload": '{"_tool":"remix","mode":"video","_remix":{"version":3,"commerce_depth_worker":true},'
                   '"prompt":"script","video_audio_flags":"1"}',
        "input_objects": '[{"field":"videos","filename":"original.mp4"},'
                         '{"field":"images","filename":"person.jpg"},'
                         '{"field":"videos","filename":"commerce-source-360.mp4",'
                         '"commerce_depth_source":true}]',
    }

    worker_input = scheduler._worker_input(row)

    assert [(item["field"], item["filename"]) for item in worker_input["media"]] == [
        ("images", "person.jpg"), ("videos", "commerce-source-360.mp4"),
    ]
    assert worker_input["_commerce_depth_worker"] is True
    assert worker_input["mode"] == "reference"
    assert all("original.mp4" not in item["filename"] for item in worker_input["media"])


def test_depth_worker_keeps_selected_picture_order_after_putting_video_first(tmp_path, monkeypatch):
    module_path = Path(__file__).resolve().parents[1] / "commerce-depth-worker/app/commerce_depth_job.py"
    spec = importlib.util.spec_from_file_location("commerce_depth_job_test", module_path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    def download(_url, target):
        target.write_bytes(b"source")

    def make_depth(_source, silent, *_args):
        silent.write_bytes(b"depth")

    def mux_audio(_source, _silent, target, *_args):
        target.write_bytes(b"depth-with-audio")

    monkeypatch.setattr(worker, "_download", download)
    monkeypatch.setattr(worker, "_probe", lambda _path: (360, 640, 24, 5, True))
    monkeypatch.setattr(worker, "_make_depth_video", make_depth)
    monkeypatch.setattr(worker, "_mux_audio", mux_audio)

    job_input = tmp_path / "input.json"
    manifest = tmp_path / "prepared.json"
    output = tmp_path / "media"
    job_input.write_text(json.dumps({
        "_commerce_depth_worker": True,
        "media": [
            {"field": "videos", "filename": "source-360.mp4", "url": "https://example.test/video"},
            {"field": "images", "filename": "selected-person.jpg", "url": "https://example.test/person"},
            {"field": "images", "filename": "selected-scene.jpg", "url": "https://example.test/scene"},
        ],
    }), encoding="utf-8")

    worker.prepare(job_input, manifest, output)

    media = json.loads(manifest.read_text(encoding="utf-8"))["media"]
    assert [(item["field"], item["filename"]) for item in media] == [
        ("videos", "commerce-depth-360.mp4"),
        ("images", "selected-person.jpg"),
        ("images", "selected-scene.jpg"),
    ]


def test_commerce_remix_is_queued_on_remix_pool_at_fixed_1_point_5():
    assert scheduler._queue_for('{"_tool":"commerce_remix"}') == "remix"
    assert commerce_remix_price_milli() == 1500
