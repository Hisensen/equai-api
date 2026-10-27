"""Custom-node entry point for the standard H3 worker overlay.

The high-resolution worker images already bake the ``py`` node modules into
the image. The runtime overlay replaces this package initializer, so it must
register every node that the standard video workflows reference.
"""

from .h3_sigma_refiner import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

try:
    from .py.h3_tiled_sampler import (
        NODE_CLASS_MAPPINGS as _TILED_CLASS_MAPPINGS,
        NODE_DISPLAY_NAME_MAPPINGS as _TILED_DISPLAY_NAME_MAPPINGS,
    )
except Exception as exc:  # Keep the sigma-refiner available on older images.
    print(
        "[ComfyUI-YCNodes-MiniMax-H3] H3TiledSampler could not be loaded: "
        f"{type(exc).__name__}: {exc}"
    )
else:
    NODE_CLASS_MAPPINGS = {**NODE_CLASS_MAPPINGS, **_TILED_CLASS_MAPPINGS}
    NODE_DISPLAY_NAME_MAPPINGS = {
        **NODE_DISPLAY_NAME_MAPPINGS,
        **_TILED_DISPLAY_NAME_MAPPINGS,
    }

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
