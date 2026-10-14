FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl libpq5 && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir pip==26.1.2
COPY pyproject.toml setup.py MANIFEST.in alembic.ini constraints.txt /app/
COPY alembic /app/alembic
COPY branding /app/branding
COPY app /app/app
COPY scripts /app/scripts
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install -c constraints.txt .
COPY . /app
RUN chmod 755 /app/scripts/*.sh
RUN adduser --disabled-password --gecos "" appuser \
    && mkdir -p /app/uploads \
    && chown -R appuser:appuser /app
USER appuser
