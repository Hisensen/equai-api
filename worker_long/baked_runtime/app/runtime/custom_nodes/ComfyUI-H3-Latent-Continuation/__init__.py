"""ComfyUI extension and legacy helper nodes for automatic H3 continuation."""

from comfy_api.latest import ComfyExtension

from .continuation_nodes import (
    MiniMaxH3AppendContinuation,
    MiniMaxH3GuidedContinuationWindow,
    MiniMaxH3LatentTailGuide,
)


class H3LatentContinuationExtension(ComfyExtension):
    async def get_node_list(self):
        return [
            MiniMaxH3GuidedContinuationWindow,
            MiniMaxH3LatentTailGuide,
            MiniMaxH3AppendContinuation,
        ]


async def comfy_entrypoint() -> H3LatentContinuationExtension:
    return H3LatentContinuationExtension()

__all__ = ["comfy_entrypoint"]
