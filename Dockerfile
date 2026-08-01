# Runtime image for the market-making bot (both currency pairs) and the
# history dashboard -- one image, different commands per docker-compose
# service. Secrets (market_maker/.env) are never baked in; they're passed
# at container start via docker-compose's env_file.
FROM python:3.11-slim

ARG LOB_REVISION=uncommitted
ARG LOB_BUILD_DATE=unknown
LABEL org.opencontainers.image.revision="${LOB_REVISION}" \
      org.opencontainers.image.created="${LOB_BUILD_DATE}"

# Unbuffered stdout/stderr -- without this, Python fully buffers output
# when it isn't attached to a TTY (always true in a container), so
# `docker compose logs` shows nothing for a long time even though the
# bot is running and writing results correctly.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY market_maker/ ./market_maker/

WORKDIR /app/market_maker

# No default CMD -- docker-compose.yml sets the command per service
# (run.py --profile btc/usdt --mode demo, or dashboard.py).
