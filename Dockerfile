FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
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

RUN pip install --no-cache-dir --force-reinstall . \
    && mkdir -p /app/data/cookies /app/data/metrics /app/logs \
    && python -c "import inspect, xuse.core.llm_service.service as s; src=inspect.getsource(s.LLMService.generate_text); print('xuse loaded from:', s.__file__); assert '/app/src/' in s.__file__, s.__file__; assert 'max_completion_tokens' in src" \
    && x-use --help > /dev/null

EXPOSE 8080

CMD ["python", "railway_worker.py"]
