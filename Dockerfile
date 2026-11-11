# photos2yt container: Python slim + ffmpeg (for ffprobe), installed with uv from uv.lock.
FROM python:3.12-slim

# ffprobe comes from ffmpeg; tzdata so zoneinfo has America/Los_Angeles
# (quota day) and the config timezone even without the tzdata Python package.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=astral/uv:0.11.32 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# Dependencies first (cached layer), then the package itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY photos2yt ./photos2yt
RUN uv sync --frozen --no-dev --no-editable

COPY scripts/nightly.sh /usr/local/bin/photos2yt-nightly
RUN chmod 755 /usr/local/bin/photos2yt-nightly \
    && mkdir -p /config /data /staging

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PHOTOS2YT_CONFIG=/config/config.yaml

# Runs as an unprivileged user; docker-compose.yml overrides it with PUID:PGID
# so files in the mounted folders belong to you.
USER 1000:1000
# ingest_report.csv is written to the working directory.
WORKDIR /data

ENTRYPOINT ["photos2yt"]
CMD ["--help"]
