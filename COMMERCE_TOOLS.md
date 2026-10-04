# Commerce tools — r85

## Scope

- Sidebar: 视频生成 / remix（支持视频编辑） / 图片编辑. Existing generation parameters and worker images are unchanged.
- Remix: one 5–15 s MP4/MOV/WebM, landscape 16:9 or portrait 9:16, with one user authority image for each selected person/product/environment role. The UI exposes one template selector, shows only its required uploads, and has no free-combination or extra-instruction surface.
- The three dedicated public remix APIs are pure H3 mode-specific flows. The existing prompt middleware normalizes the submitted prompt, then H3 receives the uploaded media directly and performs one video generation. They do not call Qwen-VL or the Image API.
- The older account remix workflow remains available for compatibility with the existing portal; the three public `/v1/remix/*/720p` endpoints are the direct H3 paths described below.
- Hypit also runs an optional asynchronous `qwen3-asr-flash-filetrans` pass. It submits the source video's signed URL to DashScope, polls the task, and feeds sentence text into the shot script so audible speech is retained in `<d>` tags. If ASR is unavailable, visual analysis continues without inventing dialogue.
- This is generative recreation, not pixel-exact editing or guaranteed lip synchronization. The first version always preserves the original source audio and does not run a separate lip-sync pass.
- Original audio is remuxed after generation without re-encoding video. Final delivery is trimmed to the measured source duration. Natural holds are retained in the prompt instead of forcing motion at the end.
- Image editing uses the existing ImageTo async upload/create/poll protocol, with separate task routing, merchant authorization, ledger, private results, notes and deletion. It never routes image jobs to Gongji.

## Configuration

Image credentials are read from `H3_IMAGE_API_KEY`, `H3_IMAGE_BASE_URL`, `H3_IMAGE_MODEL`, or an owner-readable `/data/image-service.json` containing `api_key`, `base_url`, `model`. Do not put credentials in source or the frontend.

`H3_IMAGE_EDIT_PRICE_MILLI` is required to open image submissions. It is an integer number of thousandths of a point, greater than zero. Until a business price is confirmed, the image page remains visible but submission returns 503. Successful image jobs settle once; failed/unknown jobs release the reservation. An ambiguous provider submission is not automatically replayed. Its remote acceptance may require administrative reconciliation.

The API container now requires ffmpeg and Pillow. Existing environment/volumes/worker images stay unchanged.

Remix uses the dedicated `GONGJI_REMIX_720_*` resource profile. Any setting not
supplied by the remix profile inherits the validated standard 720p profile, so
image, GPU, sampler, timeout, and storage stay identical unless explicitly
overridden. Production points this profile at `浙江四区` using the available
`4090-48G ×1` resource; ordinary video
generation remains on its existing region. Every public remix mode uses the
remix profile, so no remix stage starts in the ordinary Fujian pool.
Ordinary `reference` tasks that include one or more `videos` assets are also
dispatched to the Zhejiang 48G profile at 720p, 1080p and 2K; image-only
`reference` tasks remain on the standard Fujian profile.

Standard and remix tasks also have separate dispatcher lanes and concurrency
budgets. A backlog or cold start in remix cannot consume every ordinary video
dispatch slot.

Production image-edit price confirmed on 2026-09-10: `H3_IMAGE_EDIT_PRICE_MILLI=200` (0.2 pt per successful image). Failed tasks release the reservation without charging. This price does not change video pricing.

## Account endpoints

- `GET /api/account/creative/config`
- `POST /api/account/remix/720p`: legacy portal-compatible scene-aware workflow.
- `POST /api/account/image-edits`: multipart `images` (1–4, first is original), `prompt`, optional `note`. `Idempotency-Key` is bound to image contents and request text.
- Existing account task list/detail/note/delete routes work for both, including `GET /api/account/tasks?tool=remix|image_edit|video`.

## Public API endpoint

- Dedicated Bearer-authenticated 720P endpoints:
  - `POST /v1/remix/text/720p` — `kind=text`, 文生视频字段。
  - `POST /v1/remix/image/720p` — `kind=image`, 首帧/首尾帧字段。
  - `POST /v1/remix/reference/720p` — `kind=reference`, 多参考素材字段。
  All three pass through the normal prompt middleware and then directly to H3;
  all three use the dedicated Zhejiang resource profile.

## Reference video downscaling

H3 bills GPU time in proportion to the pixel count of a reference video, not its
duration. Uploaded reference videos are therefore downscaled so their short edge
is at most `H3_REFERENCE_VIDEO_SHORT_EDGE` (default 360) before dispatch. Aspect
ratio, frame rate and audio are preserved; the delivered resolution is still
decided by the tier.

Measured on one 15.08 s clip, identical prompt/image/seed:

| Reference video | GPU seconds | Gongji billing | Output sharpness |
| --- | --- | --- | --- |
| 736x1280 (original) | 1141.9 | 0.7551 | 37.1 |
| 480x834 | 541.9 | 0.4522 | 36.1 |
| 360x626 | 374.2 | 0.2517 | 39.6 |

Sharpness (Laplacian variance) does not track the input resolution, so the
spread is generation noise rather than lost detail. All three delivered
736x1280.

Transcoding runs on the API host at `veryfast`, costing 5.15 s of CPU for a
15 s clip and shrinking it from 3.54 MB to 1.44 MB. `H3_REFERENCE_VIDEO_TRANSCODE_SLOTS`
(default 2) caps concurrency so a burst cannot starve the request loop on a
two-core box. Host transcoding throughput sits about 9x above what the GPU pool
can consume, so it is not the bottleneck; to move this work off the host later,
replace the body of `_downscale_reference_video`.

Both multipart uploads and client-side direct uploads are covered. The legacy
portal remix flow is deliberately excluded: its reference video feeds Qwen-VL
shot analysis, where downscaling has not been measured.

## Portal remix analysis via Model Studio (R89)

`H3_REMIX_ANALYSIS_ENGINE` selects how the portal remix reads its reference video:

| Value | Analysis stage | After analysis |
| --- | --- | --- |
| `legacy` (default) | Gongji Qwen job, blueprint | ImageTo keyframes, image-only Ref2VA |
| `hypit` | Gongji Qwen job + hypit analyzer overlay | no keyframes; H3 `[video editing]` with 360p source + role pictures |
| `hypit-api` | `remix_vision.analyse`: ffmpeg scene cuts on the API host + one `qwen3-vl-plus` call (the provider fetches the signed video URL) + one text-only scrub call | same as `hypit` |

Measured on a 15.08 s clip with one role picture, from the HK host: Gongji analysis median 148 s / 0.064 yuan;
`hypit-api` 29 s / 0.015 yuan with shots aligned to the real cuts. The scrub pass plus a deterministic
background-sentence filter keep product surface, appearance and set detail out of the shot table, since those
words pull the original back into a product or scene swap.

`DASHSCOPE_API_KEY` lives only in `/opt/equai/api.env`. Optional: `H3_VISION_MODEL`, `H3_VISION_BASE_URL`,
`H3_VISION_TIMEOUT_SEC`, `H3_VISION_PRICE_IN_PER_M` / `_OUT_PER_M` (cost accounting, yuan per million tokens).
On an API failure or moderation refusal the task falls back to the Gongji analyzer when
`GONGJI_HYPIT_ANALYZER_OVERLAY_KEY` is set; otherwise it fails before any GPU work and the reservation is released.

## Verification and limitations

- Automated tests cover auth, CSRF origin checks, input validation, price gating, role order, prompt compilation, provider response variants, uncertain submission recovery, single settlement, private image downloads and real ffmpeg audio remux.
- Browser checks use stubbed provider/task results: desktop/mobile layout, navigation, upload/submit, active polling and inline result retention. They do not establish generation quality or actual provider latency.
- Real GPU quality still requires a paid end-to-end acceptance run; API tests alone do not establish visual fidelity. Direct H3 tasks record video-stage timing and Gongji billing.
- Public remix prompt compilation uses the same prompt middleware as the ordinary API; no separate analysis or intermediate image stage is involved.
- Long text/first-frame jobs currently support up to 45 seconds, but remix remains 5–15 seconds because the long worker does not yet implement multi-reference anchors per segment. Do not remove this limit without a separate long-remix implementation and acceptance run.
- Keep the previous API image and a source snapshot for rollback. Before rolling back to r63, drain new remix/image tasks; the old scheduler does not understand the new image task kind or remix finalization.
