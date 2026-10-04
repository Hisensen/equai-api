# Long video API

The long-video service is API-only and runs on the isolated 720P Motion
Context worker. Existing 5–15 second video endpoints continue to use their
original workers.

## Discover capabilities

```http
GET /v1/long-videos/capabilities
Authorization: Bearer <api-key>
```

The response declares the current duration range, aspect ratios, segment
length, context length, and price per segment.

## Create a video

```http
POST /v1/long-videos/720p
Authorization: Bearer <api-key>
Idempotency-Key: <unique-request-id>
Content-Type: application/json
```

```json
{
  "duration": 30,
  "ratio": "16:9",
  "title": "Rainy street",
  "seed": 2026090601,
  "segment_prompts": [
    "Describe the first continuous segment...",
    "Continue the exact same shot from the preceding segment..."
  ]
}
```

The number of prompts must match the duration plan returned by validation.
The production range is 16–45 seconds: 20 or 30 seconds require 2 prompts,
and 45 seconds requires 3. A continuation prompt should explicitly preserve
identity, motion direction, camera trajectory, lighting, environment, and
sound continuity.

The worker automatically prefixes every continuation segment with a two-second
continuity hold. This keeps the preceding framing and motion trajectory stable
before the requested next action begins, preventing prompt changes from causing
an immediate reframe at the latent boundary.

The endpoint returns an asynchronous `vid_...` task. Poll the existing task
endpoint for progress and the signed output URL:

```http
GET /v1/videos/{task_id}
Authorization: Bearer <api-key>
```

Long-video output is 720P at 24 fps. All clips for a 16–45 second request stay
inside one ComfyUI graph. Continuation clips inject the previous 22 latent
frames, discard the overlap, concatenate decoded video and audio in memory,
and encode the combined timeline once. Production requests do not cross a
save/reload boundary.
