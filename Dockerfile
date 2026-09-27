FROM python:3.12.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl libmagic1 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY apps ./apps
COPY config ./config
COPY manage.py ./
RUN pip install --upgrade pip && pip install .
COPY . .
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS -H 'Host: localhost' http://127.0.0.1:8000/api/v1/health/live/ || exit 1
CMD ["sh", "-c", "python manage.py collectstatic --noinput && exec gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --workers 2 --access-logfile -"]
