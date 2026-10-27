# Baked standard Worker runtime

`app/` is the standard H3 Worker runtime payload and custom-node source used to
derive the 720p, 1080p and 2K images. It is intentionally versioned with the image
build inputs so new Worker pods do not download Python code or node packages
from the Tencent API host.

The payload was copied from the previously deployed standard runtime package
and kept in this directory for reproducible image builds. The
`ComfyUI-YCNodes-MiniMax-H3` initializer also imports the image-baked
`H3TiledSampler` module so high-resolution workflows register the node after
the runtime files are copied into `/app`.

Build from this directory as context with `worker_long/Dockerfile.baked-runtime`
and set `BASE_IMAGE` to the corresponding 720p, 1080p, or 2K base image. The
720p image uses its own node set, so the optional tiled sampler import is allowed
to be absent while the sigma refiner remains registered.

The 720p runtime payload matches the prior Tencent-hosted standard overlay
(SHA-256 `083bcfd71f90f89d19158c42727f45ab715552c2e290f4be1ebe5c9867699a1e`).
Its published `standard-v9-r50-4090-720-ref1-6step-baked-runtime-overlay-20261007-v2`
image completed an isolated 15-second GPU job (task `3387733`) and produced a
15.084-second H.264/AAC MP4 at the model-aligned size 1280×736. The 1080p and
2K baked images also completed their isolated 15-second GPU tests.
