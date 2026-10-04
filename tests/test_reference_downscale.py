from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import reference_video


def _synth(path: Path, width: int, height: int, seconds: float = 1.0) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=24:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )


def _size(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    width, height = out.split(",")[:2]
    return int(width), int(height)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg 不可用")
class ReferenceDownscaleTest(unittest.TestCase):
    def test_portrait_short_edge_is_capped(self) -> None:
        with TemporaryDirectory() as root:
            source, target = Path(root) / "in.mp4", Path(root) / "out.mp4"
            _synth(source, 736, 1280)
            self.assertTrue(reference_video.downscale_file(source, target))
            width, height = _size(target)
            self.assertEqual(width, reference_video.SHORT_EDGE)
            # Aspect ratio is preserved within one rounding step of yuv420p.
            self.assertAlmostEqual(width / height, 736 / 1280, delta=0.01)

    def test_landscape_short_edge_is_capped(self) -> None:
        with TemporaryDirectory() as root:
            source, target = Path(root) / "in.mp4", Path(root) / "out.mp4"
            _synth(source, 1280, 736)
            self.assertTrue(reference_video.downscale_file(source, target))
            width, height = _size(target)
            self.assertEqual(height, reference_video.SHORT_EDGE)
            self.assertAlmostEqual(width / height, 1280 / 736, delta=0.01)

    def test_small_video_is_left_alone(self) -> None:
        with TemporaryDirectory() as root:
            source, target = Path(root) / "in.mp4", Path(root) / "out.mp4"
            _synth(source, 426, 240)
            self.assertFalse(reference_video.downscale_file(source, target))
            self.assertFalse(target.exists())

    def test_corrupt_input_is_not_fatal(self) -> None:
        with TemporaryDirectory() as root:
            source, target = Path(root) / "in.mp4", Path(root) / "out.mp4"
            source.write_bytes(b"not a video")
            self.assertFalse(reference_video.downscale_file(source, target))

    def test_disabled_by_configuration(self) -> None:
        original = reference_video.SHORT_EDGE
        reference_video.SHORT_EDGE = 0
        try:
            with TemporaryDirectory() as root:
                source, target = Path(root) / "in.mp4", Path(root) / "out.mp4"
                _synth(source, 736, 1280)
                self.assertFalse(reference_video.downscale_file(source, target))
        finally:
            reference_video.SHORT_EDGE = original


if __name__ == "__main__":
    unittest.main()
