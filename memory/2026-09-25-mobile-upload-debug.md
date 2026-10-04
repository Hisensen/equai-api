# Mobile upload debug (2026-09-25)

- Symptom: mobile video/photo selection succeeds, but pressing submit fails.
- Root causes found in code: browsers can send an empty/nonstandard MIME (the API signed `application/octet-stream` and rejected it); HEIC/HEIF uses an ISO-BMFF `ftyp` header that was classified as a video/container; direct-upload errors were masked by generic recovery text.
- Fix: canonical MIME by field/extension in `video_api.py` and `portal/app.js`; HEIC brands recognized as image; optional `pillow-heif` registered for server normalization; direct uploads retry three times; deterministic validation errors are shown directly; mobile accept lists broadened; static cache version bumped to r99.
- Evidence: `python -m pytest -q` => 160 passed.
- Deployment: completed 2026-09-25 through the working internal SSH jump host. API container is healthy, `https://equai.vip/health` is ready, and the site serves r99 mobile-upload assets. `pillow-heif` 1.8.0 is present in the container.
