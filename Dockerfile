# Build: docker build -t reddit-stash .
# Python 3.12 is the tested default; override PYTHON_VERSION only after CI testing.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION=local

LABEL org.opencontainers.image.title="Reddit Stash" \
      org.opencontainers.image.description="Archive Reddit posts and comments to local and cloud storage" \
      org.opencontainers.image.url="https://github.com/WickedWhisperer/reddit-stash" \
      org.opencontainers.image.source="https://github.com/WickedWhisperer/reddit-stash" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="WickedWhisperer"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SCHEDULE_MODE=once \
    SCHEDULE_INTERVAL=7200 \
    REDDIT_STASH_HEARTBEAT_FILE=/app/runtime/last_successful_run \
    HEALTHCHECK_MAX_AGE_SECONDS=86400

WORKDIR /app

# rclone is required by the MEGA provider. boto3 enables the optional S3 provider.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libc6-dev ffmpeg rclone ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-s3.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt -r requirements-s3.txt

COPY . .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && useradd --uid 10001 --create-home --shell /bin/bash appuser \
    && mkdir -p /app/reddit_dropbox /app/reddit_mega /app/runtime \
    && chown -R appuser:appuser /app

USER appuser

# The default archive roots in settings.ini and mega_settings.ini are both persisted.
VOLUME ["/app/reddit_dropbox", "/app/reddit_mega"]

# The application updates this file only after a fully successful scraper run.
# The long start period accommodates first-time API setup and large first archives.
HEALTHCHECK --interval=5m --timeout=10s --start-period=6h --retries=2 \
  CMD python -c "import os,sys,time; p=os.environ.get('REDDIT_STASH_HEARTBEAT_FILE','/app/runtime/last_successful_run'); age=int(os.environ.get('HEALTHCHECK_MAX_AGE_SECONDS','93600')); sys.exit(0 if os.path.isfile(p) and time.time()-os.path.getmtime(p) <= age else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["reddit_stash.py"]
