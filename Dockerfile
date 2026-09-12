FROM python:3.12-slim

# Debian security patches Docker Hub hasn't rebuilt this tag with yet.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (uid 1000), matching the compose `user:` and the
# bind-mounted state dir ownership on the host.
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -m app

WORKDIR /app

# Build stamps (parity with the bookhunt lab pattern).
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ENV GIT_SHA=${GIT_SHA} BUILD_TIME=${BUILD_TIME} PYTHONUNBUFFERED=1

# Install deps first for layer caching. NO secrets are ever copied in.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code only — no .env, no state/, no credential files (see .dockerignore).
COPY src/ ./src/
COPY scripts/ ./scripts/
# Supervisor that runs the bot loop AND the read-only web dashboard together.
COPY --chmod=0755 entrypoint.sh ./

# state/ is a bind mount at runtime; create the dir so first run has it.
RUN mkdir -p /app/state && chown -R 1000:1000 /app

USER 1000:1000

ENTRYPOINT ["/app/entrypoint.sh"]
