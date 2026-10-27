# Separate runtime: the production H3 video image uses Python 3.13, while the
# upstream IndexTTS-2 project requires Python >=3.10,<3.12.
# The upstream uv.lock installs PyTorch 2.8 cu128 and its NVIDIA runtime
# packages. The host only needs to supply the NVIDIA driver/device.
ARG PYTHON_BASE=python:3.10-slim-bookworm
FROM ${PYTHON_BASE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=hardlink \
    UV_NO_CACHE=1 \
    UV_CONCURRENT_DOWNLOADS=4 \
    UV_CONCURRENT_INSTALLS=2 \
    INDEXTTS2_MODEL_DIR=/models/TTS/IndexTTS-2

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ffmpeg ca-certificates libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.8.13

ARG INDEXTTS_COMMIT=d9e41aac89fd00b3d71497fddb287b7f24613712
WORKDIR /opt/index-tts
RUN git clone https://github.com/index-tts/index-tts.git . \
    && git checkout --detach "$INDEXTTS_COMMIT" \
    && uv sync --frozen --no-dev --python /usr/local/bin/python3.10

WORKDIR /app
COPY workflow.py worker.py /app/
RUN uv pip install --python /opt/index-tts/.venv/bin/python httpx==0.28.1

ENTRYPOINT ["/opt/index-tts/.venv/bin/python", "/app/worker.py"]
