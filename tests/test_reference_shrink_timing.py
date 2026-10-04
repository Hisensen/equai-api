"""The 360 shrink runs at dispatch, not inside the create request.

Creating a task used to download the reference video from COS, transcode it and
upload it back before the browser got its task id — three crossings of a
0.7 MB/s link while the user watched a spinner. These tests pin the new
placement: create returns without touching the bytes, and the scheduler shrinks
once, just before the job is dispatched.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("H3_DATA_DIR", tempfile.mkdtemp(prefix="equai-shrink-test-"))

import reference_video
import scheduler


VIDEO = {"field": "videos", "storage": "cos", "key": "inputs/m/staged/a.mp4",
         "filename": "a.mp4", "content_type": "video/mp4"}
IMAGE = {"field": "images", "storage": "cos", "key": "inputs/m/staged/b.png",
         "filename": "b.png", "content_type": "image/png"}


class ShrinkInputsTest(unittest.TestCase):
    def test_only_cos_videos_are_shrunk(self) -> None:
        seen = []
        with patch.object(reference_video, "short_edge_of", return_value=1080), \
                patch.object(reference_video, "rewrite_object",
                             side_effect=lambda key, ct: seen.append(key) or 123):
            asyncio.run(reference_video.shrink_inputs([VIDEO, IMAGE]))
        self.assertEqual(seen, [VIDEO["key"]])

    def test_already_small_video_is_not_downloaded(self) -> None:
        """A restart after a completed shrink costs one header fetch, not a transcode."""
        with patch.object(reference_video, "short_edge_of", return_value=360), \
                patch.object(reference_video, "rewrite_object",
                             side_effect=AssertionError("must not re-transcode")):
            self.assertEqual(asyncio.run(reference_video.shrink_inputs([VIDEO])), 0)

    def test_locally_stored_video_is_processed_without_remote_probe(self) -> None:
        local = {**VIDEO, "storage": "local"}
        with patch.object(reference_video, "short_edge_of",
                          side_effect=AssertionError("must not probe local inputs")), \
                patch.object(reference_video, "rewrite_local", return_value=123) as rewrite:
            self.assertEqual(asyncio.run(reference_video.shrink_inputs([local])), 123)
        rewrite.assert_called_once_with(local["key"])

    def test_unreadable_probe_still_attempts_the_shrink(self) -> None:
        """A probe failure must not silently leave a 1080p video on the GPU bill."""
        with patch.object(reference_video, "short_edge_of", return_value=0), \
                patch.object(reference_video, "rewrite_object", return_value=99) as rewrite:
            asyncio.run(reference_video.shrink_inputs([VIDEO]))
        rewrite.assert_called_once()

    def test_disabled_by_configuration(self) -> None:
        with patch.object(reference_video, "SHORT_EDGE", 0), \
                patch.object(reference_video, "short_edge_of",
                             side_effect=AssertionError("must not probe when disabled")):
            self.assertEqual(asyncio.run(reference_video.shrink_inputs([VIDEO])), 0)

    def test_rewrite_keeps_the_original_when_transcoding_fails(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with patch.object(reference_video, "download_path",
                              side_effect=lambda key, path: Path(path).write_bytes(b"not a video")), \
                    patch.object(reference_video, "upload_path",
                                 side_effect=AssertionError("must not upload a failed transcode")):
                self.assertEqual(reference_video.rewrite_object("k", "video/mp4"), 0)
            self.assertFalse(list(Path(root).iterdir()))


class WorkerInputTest(unittest.TestCase):
    def test_shrink_flag_never_reaches_the_gpu_worker(self) -> None:
        row = {
            "id": "vid_1", "kind": "reference", "input_objects": json.dumps([IMAGE]),
            "payload": json.dumps({"mode": "reference", "_shrink_reference": True,
                                   "_transfer_backend": "cos"}),
        }
        with patch.object(scheduler, "presigned_url", return_value="https://example/x"):
            worker = scheduler._worker_input(row)
        self.assertNotIn("_shrink_reference", worker)


if __name__ == "__main__":
    unittest.main()
