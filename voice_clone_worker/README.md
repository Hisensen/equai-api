# IndexTTS-2 voice clone worker

The user's ComfyUI UI graph (SHA-256
`6792a7ed2b5b1bce0eb4c74fc42d4359b2c6d189e36eb34a0850a7e2981df302`)
contains `LoadAudio` → `AudioCrop` (0–12 s) → `IndexTTS2Run` →
`SaveAudio`/`SaveAudioMP3`, with a second cropped audio for emotion. The graph
embeds a private example script and hard-coded upload filename; those are not
shipped in this worker. `workflow.py` preserves the generation defaults and
`worker.py` calls the model author's inference API directly. This avoids
adding a Python-3.10-only TTS extension to the Python-3.13 H3 video image.

Worker input is JSON in `H3_JOB_INPUT` with `text`, optional `emotion_weight`,
and `media` entries whose `field` values are `voice` and optional `emotion`.
The one-shot job writes `speech.mp3` to the first signed upload target and
reports completion to `H3_JOB_RESULT_UPLOAD_TARGET`. Model files must be
prewarmed separately at `/models/TTS/IndexTTS-2`; the Docker image contains
code and dependencies, not model weights.

The Tencent API exposes `POST /v1/voice-clones` for API keys and
`POST /api/account/voice-clones` for portal sessions. Both accept multipart
`voice` (required audio), `text` (required), `emotion` (optional audio),
`emotion_weight` (optional float 0–2), and `Idempotency-Key` (recommended).
Use `GET /v1/voice-clones/status` to check availability, then
`GET /v1/voice-clones/{id}` and `GET /v1/voice-clones/{id}/audio` for the
result. A disabled/unconfigured worker returns HTTP 503 at creation time.

To activate, build and publish this directory's Dockerfile, preload the
official IndexTTS-2 checkpoint and its `hf_cache` auxiliaries on a Gongji
storage volume, configure `GONGJI_VOICE_CLONE_IMAGE` and
`GONGJI_VOICE_CLONE_STORAGE_ID` on Tencent, set a positive
`H3_VOICE_CLONE_PRICE_MILLI`, run a real GPU job, and only then set
`H3_VOICE_CLONE_ENABLED=1`. Do not point the dedicated task at the existing
H3-only model volume unless the IndexTTS files are actually present there.

The [official model license](https://github.com/index-tts/index-tts/blob/main/LICENSE)
allows use under its stated terms. Section 2.2 requires a separate written
license for organizations above the listed monthly-user or annual-revenue
thresholds. Confirm the applicable license terms and complete a real GPU smoke
test before enabling the public route.
