# Multi-arch build: tagged for linux/arm64 (Raspberry Pi) and linux/amd64.
#
# Build (single arch, local):
#   docker build -t slack-benchy .
#
# Build (multi-arch, push to registry):
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/artogahr/slack-benchy:latest --push .

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip build && python -m build --wheel --outdir /wheels

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/var/lib/slack-benchy/slack-benchy.sqlite3

RUN useradd --system --uid 1000 --home /var/lib/slack-benchy prusabot && \
    install -d -o prusabot -g prusabot -m 0755 /var/lib/slack-benchy

COPY --from=builder /wheels /wheels
RUN pip install /wheels/*.whl && rm -rf /wheels

USER prusabot
WORKDIR /var/lib/slack-benchy
VOLUME ["/var/lib/slack-benchy"]

# Smoke check: import succeeds.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import slack_benchy" || exit 1

ENTRYPOINT ["python", "-m", "slack_benchy"]
