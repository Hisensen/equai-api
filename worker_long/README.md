# Long-video worker overlay

This directory contains the API server, one-shot job handler, and workflow
builder used by the API-only long-video worker. The deployment overlay also
vendors `ComfyUI-H3-Motion-Context` version 0.6.1 under
`runtime/custom_nodes/`; its release archive is stored separately in COS.

The overlay is extracted over the validated 720P worker image at job startup.
It does not modify or retag the production 5–15 second worker image.

Production duration is capped at 45 seconds. All two or three latent-linked
segments stay in one ComfyUI graph, decoded video and audio are concatenated in
memory, and the complete timeline is encoded once. No production request
saves, unloads, or reloads AV latent between memory windows.

Every continuation prompt receives an automatic two-second continuity hold.
The model must preserve the preceding framing, scale, camera, and motion before
advancing the caller's requested action; this prevents a new segment prompt
from immediately resetting the composition.

The older chunk endpoints remain in the overlay for rollback diagnostics, but
the production `long_text` handler does not use them.
