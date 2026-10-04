FROM python:3.13.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    H3_ROOT=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

COPY *.py /app/
COPY reenact /app/reenact
COPY worker_long /app/worker_long
COPY portal /app/portal

RUN groupadd --gid 10001 h3 \
    && useradd --uid 10001 --gid 10001 --no-create-home h3 \
    && mkdir -p /data/videos \
    && chown -R h3:h3 /data /app

USER h3
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3))['ok']"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
