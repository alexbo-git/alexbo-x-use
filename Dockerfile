FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Paris

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       chromium \
       chromium-driver \
       ca-certificates \
       tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config
COPY railway_worker.py ./

RUN pip install --no-cache-dir . \
    && mkdir -p /app/data/cookies /app/data/metrics /app/logs \
    && x-use --help > /dev/null

CMD ["python", "railway_worker.py"]
