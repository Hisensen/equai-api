from __future__ import annotations

import io
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from worker_long.patch_standard_overlay import SOURCE, TARGET, patch_archive


class StandardOverlayPatchTests(unittest.TestCase):
    def test_patch_registers_image_baked_tiled_sampler(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "overlay.tar.gz"
            destination = root / "patched.tar.gz"
            with tarfile.open(source, "w:gz") as archive:
                init = tarfile.TarInfo(TARGET)
                init_data = b"from .h3_sigma_refiner import NODE_CLASS_MAPPINGS\n"
                init.size = len(init_data)
                archive.addfile(init, io.BytesIO(init_data))
                module = tarfile.TarInfo(
                    "app/runtime/custom_nodes/ComfyUI-YCNodes-MiniMax-H3/h3_sigma_refiner.py"
                )
                module_data = b"preserved"
                module.size = len(module_data)
                archive.addfile(module, io.BytesIO(module_data))

            patch_archive(source, destination)

            with tarfile.open(destination, "r:gz") as archive:
                patched = archive.extractfile(TARGET)
                self.assertIsNotNone(patched)
                self.assertEqual(patched.read(), SOURCE.read_bytes())
                self.assertIsNotNone(
                    archive.extractfile(
                        "app/runtime/custom_nodes/ComfyUI-YCNodes-MiniMax-H3/h3_sigma_refiner.py"
                    )
                )
            source_text = SOURCE.read_text()
            self.assertIn(".py.h3_tiled_sampler", source_text)
            self.assertIn("H3TiledSampler", source_text)

    def test_overlay_initializer_merges_baked_tiled_sampler_mapping(self) -> None:
        package = types.ModuleType("h3_overlay_test")
        package.__path__ = []
        py_package = types.ModuleType("h3_overlay_test.py")
        py_package.__path__ = []
        sigma = types.ModuleType("h3_overlay_test.h3_sigma_refiner")
        sigma.NODE_CLASS_MAPPINGS = {"SigmaRefiner": object()}
        sigma.NODE_DISPLAY_NAME_MAPPINGS = {"SigmaRefiner": "Sigma Refiner"}
        tiled = types.ModuleType("h3_overlay_test.py.h3_tiled_sampler")
        tiled.NODE_CLASS_MAPPINGS = {"H3TiledSampler": object()}
        tiled.NODE_DISPLAY_NAME_MAPPINGS = {"H3TiledSampler": "H3 Tiled Sampler"}
        modules = {
            "h3_overlay_test": package,
            "h3_overlay_test.py": py_package,
            "h3_overlay_test.h3_sigma_refiner": sigma,
            "h3_overlay_test.py.h3_tiled_sampler": tiled,
        }
        namespace = {"__package__": "h3_overlay_test"}

        with patch.dict("sys.modules", modules):
            exec(compile(SOURCE.read_text(), str(SOURCE), "exec"), namespace)

        self.assertEqual(
            set(namespace["NODE_CLASS_MAPPINGS"]),
            {"SigmaRefiner", "H3TiledSampler"},
        )
        self.assertEqual(
            namespace["NODE_DISPLAY_NAME_MAPPINGS"]["H3TiledSampler"],
            "H3 Tiled Sampler",
        )


if __name__ == "__main__":
    unittest.main()
