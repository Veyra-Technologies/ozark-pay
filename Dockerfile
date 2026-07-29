FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl libpq5 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml /app/
COPY app /app/app
COPY scripts /app/scripts
RUN python -m pip install --no-cache-dir .
COPY . /app
RUN adduser --disabled-password --gecos "" appuser \
    && mkdir -p /app/uploads \
    && chown -R appuser:appuser /app
USER appuser
