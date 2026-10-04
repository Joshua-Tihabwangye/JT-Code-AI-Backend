# syntax=docker/dockerfile:1.7
# JT-Code API, workers, beat and consumers: one image, role chosen at runtime
# (docker/entrypoint.sh). Targets:
#   runtime - API and every worker except document conversion (default)
#   office  - runtime + headless LibreOffice for the jobs.visualization worker
# The base image is pinned by digest; Dependabot (docker ecosystem) bumps it.
ARG PYTHON_IMAGE=python:3.14.4-slim-trixie@sha256:2ca02f32b4d9d893863367ce07ec1972819f476dd38d8612f2a9cb6a41cbb727

FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt pyproject.toml ./
RUN pip wheel --wheel-dir /wheels --requirement requirements.txt

FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=dev
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="jt-code-api" \
      org.opencontainers.image.source="https://github.com/Joshua-Tihabwangye/jt-code-backend" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    SENTRY_RELEASE=jt-code-api@${VERSION} \
    APP_HOME=/app
# Runtime libraries only: WeasyPrint (pango/harfbuzz/fonts), tini as PID 1.
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
      tini libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 libfontconfig1 fonts-dejavu-core \
 && apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --no-create-home --home-dir /app --shell /usr/sbin/nologin app
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/*.whl && rm -rf /wheels
WORKDIR /app
COPY --chown=root:root . /app
# Static files are collected at build time with a settings profile that needs no secrets.
RUN DJANGO_SETTINGS_MODULE=config.settings.build python manage.py collectstatic --noinput \
 && mkdir -p converted_files generated_images rendered_documents \
 && chown app:app converted_files generated_images rendered_documents \
 && chmod 0555 docker/entrypoint.sh
# Code is owned by root and read-only for the app user; only these paths are writable
# (Kubernetes mounts emptyDir volumes there with readOnlyRootFilesystem).
USER 10001:10001
EXPOSE 8000 9808
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["api"]

FROM runtime AS office
USER root
RUN apt-get update \
 && apt-get install --no-install-recommends -y libreoffice-writer-nogui \
 && rm -rf /var/lib/apt/lists/*
USER 10001:10001
