# Multi-arch build: tagged for linux/arm64 (Raspberry Pi) and linux/amd64.
#
# Build (single arch, local):
#   docker build -t prusa-slack-bot .
#
# Build (multi-arch, push to registry):
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/your-org/prusa-slack-bot:latest --push .

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
    DB_PATH=/var/lib/prusa-slack-bot/prusa-slack-bot.sqlite3

RUN useradd --system --uid 1000 --home /var/lib/prusa-slack-bot prusabot && \
    install -d -o prusabot -g prusabot -m 0755 /var/lib/prusa-slack-bot

COPY --from=builder /wheels /wheels
RUN pip install /wheels/*.whl && rm -rf /wheels

USER prusabot
WORKDIR /var/lib/prusa-slack-bot
VOLUME ["/var/lib/prusa-slack-bot"]

# Smoke check: import succeeds.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import prusa_slack_bot" || exit 1

ENTRYPOINT ["python", "-m", "prusa_slack_bot"]
