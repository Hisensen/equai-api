"""Patch the standard worker overlay so it does not hide baked H3 nodes.

Usage: python worker_long/patch_standard_overlay.py INPUT.tar.gz OUTPUT.tar.gz

The tiled sampler implementation is baked into the r50 worker images. The
overlay only needs to register it; copying a second implementation into the
archive would create version drift.
"""

from __future__ import annotations

import argparse
import copy
import io
import tarfile
from pathlib import Path


TARGET = "app/runtime/custom_nodes/ComfyUI-YCNodes-MiniMax-H3/__init__.py"
SOURCE = Path(__file__).with_name("standard_overlay_nodes") / "__init__.py"


def patch_archive(source: Path, destination: Path) -> None:
    replacement = SOURCE.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(source, "r:gz") as archive:
        members = archive.getmembers()
        if not any(member.name == TARGET for member in members):
            raise ValueError(f"Overlay is missing {TARGET}")
        temp = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with tarfile.open(temp, "w:gz") as output:
                for member in members:
                    if member.name == TARGET:
                        updated = copy.copy(member)
                        updated.size = len(replacement)
                        output.addfile(updated, io.BytesIO(replacement))
                        continue
                    content = archive.extractfile(member) if member.isfile() else None
                    output.addfile(member, content)
            with tarfile.open(temp, "r:gz") as check:
                patched = check.extractfile(TARGET)
                if patched is None or patched.read() != replacement:
                    raise RuntimeError("Patched overlay failed its content check")
            temp.replace(destination)
        finally:
            temp.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    patch_archive(args.source, args.destination)
    print(f"Patched standard worker overlay: {args.destination}")


if __name__ == "__main__":
    main()
