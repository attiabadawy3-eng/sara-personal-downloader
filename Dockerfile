FROM python:3.12-slim-bookworm AS app
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SARA_HTTPS=1 \
    SARA_MAX_MB=150 \
    SARA_DATA_DIR=/tmp/sara-downloads
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates libatomic1 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && node --version && python -m yt_dlp --version
COPY app/ ./app/
RUN useradd -r -u 10001 sara && mkdir -p /tmp/sara-downloads && chown sara:sara /tmp/sara-downloads
USER sara
EXPOSE 10000
CMD ["sh","-c","exec uvicorn app.server:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*' --workers 1"]
