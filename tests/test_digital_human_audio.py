from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.datastructures import UploadFile
from starlette.requests import Request

from security import video_merchant


from worker_long.audio_utils import _preserve_source_audio


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
class DigitalHumanAudioTest(unittest.TestCase):

    def test_legacy_text_mode_is_rejected(self) -> None:
        from video_api import create_digital_human

        request = Request({
            "type": "http", "method": "POST",
            "path": "/api/account/digital-human/720p", "headers": [],
            "query_string": b"", "server": ("equai.vip", 443),
            "client": ("127.0.0.1", 12345), "scheme": "https",
        })
        image = UploadFile(filename="portrait.png", file=io.BytesIO(b""), size=0)
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(create_digital_human(
                request, {"id": "merchant-test"}, mode="text", image=image,
            ))
        self.assertEqual(422, caught.exception.status_code)
        self.assertIn("仅支持上传音频", caught.exception.detail)

    def test_digital_human_portal_route_authenticates_with_merchant_session(self) -> None:
        request = Request({
            "type": "http", "method": "POST",
            "path": "/api/account/digital-human/720p", "headers": [],
            "query_string": b"", "server": ("equai.vip", 443),
            "client": ("127.0.0.1", 12345), "scheme": "https",
        })
        merchant = {"id": "merchant-test", "api_key_status": "active"}
        with patch("security._session", return_value=merchant) as session, \
             patch("security.api_merchant") as api_key_auth:
            result = video_merchant(request, session="portal-session")
        self.assertEqual(merchant, result)
        session.assert_called_once_with("portal-session", "merchant")
        api_key_auth.assert_not_called()



    def test_final_video_keeps_the_uploaded_pcm_audio_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source_audio = folder / "uploaded.wav"
            video = folder / "generated.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(source_audio),
            ], check=True)
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:r=24:d=2",
                "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
            ], check=True)

            result = _preserve_source_audio(video, source_audio, 2)
            source_pcm = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(source_audio),
                "-f", "s16le", "-acodec", "pcm_s16le", "-",
            ], check=True, capture_output=True).stdout
            final_pcm = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(result),
                "-map", "0:a:1", "-f", "s16le", "-acodec", "pcm_s16le", "-",
            ], check=True, capture_output=True).stdout
            self.assertEqual(source_pcm, final_pcm)
            codec = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1",
            str(result),
            ], check=True, capture_output=True, text=True).stdout.strip().splitlines()
            self.assertEqual(["aac", "alac"], codec)

    def test_mp3_upload_gets_playable_default_track_and_original_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source_audio = folder / "uploaded.mp3"
            video = folder / "generated.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:a", "libmp3lame", "-q:a", "2", str(source_audio),
            ], check=True)
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:r=24:d=2",
                "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
            ], check=True)
            result = _preserve_source_audio(video, source_audio, 2)
            streams = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=codec_name:stream_disposition=default",
                "-of", "json", str(result),
            ], check=True, capture_output=True, text=True)
            import json
            audio = json.loads(streams.stdout)["streams"]
            self.assertEqual(["aac", "mp3"], [stream["codec_name"] for stream in audio])
            self.assertEqual([1, 0], [stream["disposition"]["default"] for stream in audio])



if __name__ == "__main__":
    unittest.main()
