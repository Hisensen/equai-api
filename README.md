# EQUAI API

EQUAI merchant console and video-generation API. GPU jobs run on Gongji. The
local production profile stores task inputs, worker outputs, generated videos,
and result files on the mounted `/data` disk. The older COS profile remains
available only for migration and legacy task recovery.

For the verified production topology, server roles, ports, GPU pool, and
For the application flow and service boundaries, see [the architecture notes](docs/ARCHITECTURE.md).

## Deploy

1. Copy `api.env.example` to `/opt/equai/api.env` and set production secrets.
2. Mount a persistent data disk at `/data`.
3. Run `docker compose up -d --build`, or use
   `docker compose -f compose.production.local.yaml up -d --build` on the
   Tencent Cloud media server.
4. Put Nginx or another TLS reverse proxy in front of `127.0.0.1:8000`.

The local profile requires `H3_PUBLIC_BASE_URL`, `H3_TRANSFER_BASE_URL`,
`H3_TRANSFER_HOSTNAME`, `H3_TRANSFER_HOST_IP`, `H3_DOWNLOAD_SECRET`, and
`H3_TRANSFER_SECRET` in the deployment environment. Keep `/data` on a
persistent disk with enough free space for active uploads and generated video.

For the HK gateway layout, keep `H3_PUBLIC_BASE_URL` on the user-facing
`equai.vip` origin and point `H3_TRANSFER_BASE_URL` at a TLS hostname/IP that
routes to this Guangzhou API. The gateway must proxy `/internal/uploads`,
`/internal/worker/`, `/internal/runtime/`, `/v1/videos/`, and
`/api/account/` without request buffering; otherwise browser direct uploads or
worker callbacks will fail or be routed back to the HK process.

Copy the two runtime overlays named by `H3_LOCAL_RUNTIME_OVERLAY_PATH` and
`H3_LOCAL_HYPIT_ANALYZER_OVERLAY_PATH` into `/data/runtime` before enabling
local mode. Existing COS objects are intentionally left readable during the
migration window; after old tasks and assets are migrated, remove the legacy
COS variables and client.

Runtime data, videos, databases, models, credentials, and generated artifacts
must never be committed with the source tree.
