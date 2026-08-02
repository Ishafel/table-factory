# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace


FROM python-base AS development

ARG APP_UID=1000
ARG APP_GID=1000

RUN set -eux; \
    case "${APP_UID}" in \
        ""|*[!0-9]*) echo "APP_UID must be a positive integer" >&2; exit 64 ;; \
    esac; \
    case "${APP_GID}" in \
        ""|*[!0-9]*) echo "APP_GID must be a positive integer" >&2; exit 64 ;; \
    esac; \
    if [ "${APP_UID}" -eq 0 ] || [ "${APP_GID}" -eq 0 ]; then \
        echo "APP_UID and APP_GID must be non-zero; refusing a root runtime" >&2; \
        exit 64; \
    fi; \
    if ! getent group "${APP_GID}" >/dev/null; then \
        groupadd --gid "${APP_GID}" developer; \
    fi; \
    if ! getent passwd "${APP_UID}" >/dev/null; then \
        useradd \
            --uid "${APP_UID}" \
            --gid "${APP_GID}" \
            --create-home \
            --shell /bin/sh \
            developer; \
    fi

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

RUN mkdir -p /opt/build-wheels \
    && python -m pip download \
        --no-cache-dir \
        --dest /opt/build-wheels \
        . \
        "setuptools>=77" \
        wheel \
    && python -m pip install --no-cache-dir -e ".[dev]"

ENV HOME=/tmp \
    PIP_FIND_LINKS=/opt/build-wheels \
    PIP_NO_INDEX=1

USER ${APP_UID}:${APP_GID}

ENTRYPOINT ["table-factory"]
