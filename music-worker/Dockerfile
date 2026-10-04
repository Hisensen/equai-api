FROM harbor.suanleme.cn/h3platform/h3-video-worker:standard-v9-r50-4090-2k-ref1-6step-denoise02-io
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
USER root
WORKDIR /app
COPY requirements.txt .
RUN python3 -m pip install -U pip && python3 -m pip install -r requirements.txt
COPY yue-src /opt/yue
RUN python3 -m pip install --no-build-isolation --no-deps /opt/yue
COPY worker.py .
ENTRYPOINT ["python3", "/app/worker.py"]
