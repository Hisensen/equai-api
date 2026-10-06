from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

import video_api
import reference_image


def _large_jpeg() -> bytes:
    image = Image.new("RGB", (3000, 2000), (40, 90, 150))
    stream = io.BytesIO()
    image.save(stream, "JPEG", quality=98)
    return stream.getvalue()


class ReferenceImageDownscaleTest(unittest.TestCase):
    def test_large_image_is_resized_and_reencoded(self) -> None:
        source_bytes = _large_jpeg()
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.jpeg"
            source.write_bytes(source_bytes)
            output = reference_image.downscale_file(source, Path(folder))
            self.assertIsNotNone(output)
            assert output is not None
            with Image.open(output) as result:
                self.assertEqual("JPEG", result.format)
                self.assertLessEqual(max(result.size), reference_image.MAX_EDGE)
                self.assertAlmostEqual(result.width / result.height, 1.5, delta=0.01)
            self.assertLess(output.stat().st_size, len(source_bytes))

    def test_transparent_png_keeps_its_alpha_channel(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "person.png"
            Image.new("RGBA", (1800, 1200), (10, 20, 30, 90)).save(source)
            output = reference_image.downscale_file(source, Path(folder))
            self.assertIsNotNone(output)
            assert output is not None
            self.assertEqual(".webp", output.suffix)
            with Image.open(output) as result:
                self.assertEqual("RGBA", result.mode)
                self.assertLessEqual(max(result.size), reference_image.MAX_EDGE)

    def test_digital_human_stores_the_processed_image_before_task_creation(self) -> None:
        source_bytes = _large_jpeg()
        image = UploadFile(
            filename="portrait.jpeg", file=io.BytesIO(source_bytes),
            size=len(source_bytes), headers=Headers({"content-type": "image/jpeg"}),
        )
        audio = UploadFile(
            filename="voice.wav", file=io.BytesIO(b"fake audio"), size=10,
            headers=Headers({"content-type": "audio/wav"}),
        )
        request = Request({
            "type": "http", "method": "POST", "path": "/api/account/digital-human/720p",
            "headers": [], "query_string": b"", "server": ("equai.vip", 443),
            "client": ("127.0.0.1", 12345), "scheme": "https",
        })
        merchant = {
            "id": "merchant-test", "status": "active", "api_key_status": "active",
            "balance_milli": 5000,
        }
        captured = {}

        async def store(_merchant_id, _task_id, _index, field, upload, safe,
                        content_type, _asset_id, **_kwargs):
            raw = upload.file.read()
            if field == "images":
                with Image.open(io.BytesIO(raw)) as processed:
                    captured.update({
                        "filename": safe, "content_type": content_type,
                        "size": len(raw), "dimensions": processed.size,
                        "format": processed.format,
                    })
            return {"id": "asset-test", "storage": "local", "bucket": "local-input",
                    "key": safe, "filename": safe, "content_type": content_type,
                    "field": field, "size_bytes": len(raw)}

        class FakeDB:
            def execute(self, *_args, **_kwargs):
                return None

        @contextmanager
        def transaction():
            yield FakeDB()

        with patch.object(video_api, "_store_upload", side_effect=store), \
                patch.object(video_api, "transaction", transaction), \
                patch.object(video_api, "_uploaded_audio_seconds", return_value=16), \
                patch.object(video_api, "reserve_task"), \
                patch.object(video_api, "public_id", side_effect=["task-test", "asset-image", "asset-audio"]), \
                patch.object(video_api, "_get_task", return_value={"id": "task-test"}), \
                patch.object(video_api, "_task_view", return_value={"ok": True}):
            async def submit():
                return await video_api.create_digital_human(
                    request, merchant, mode="audio", image=image,
                    audio=audio,
                    ratio="16:9", title="图片缩放测试", idempotency_key=None,
                )

            response = asyncio.run(submit())

        self.assertEqual({"ok": True}, response)
        self.assertEqual("JPEG", captured["format"])
        self.assertTrue(captured["filename"].endswith(".jpg"))
        self.assertEqual("image/jpeg", captured["content_type"])
        self.assertLessEqual(max(captured["dimensions"]), reference_image.MAX_EDGE)
        self.assertLess(captured["size"], len(source_bytes))


if __name__ == "__main__":
    unittest.main()
