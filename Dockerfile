FROM python:3.11-slim@sha256:a630a63cdb314e2d138a2fca3e375e319e8568346ffafac5b980f888630ac4f1 AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp
WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --disable-pip-version-check --no-cache-dir \
      --require-hashes --only-binary=:all: -r requirements.lock \
    && python -m pip check

FROM dependencies AS verified
COPY requirements-dev.lock ./
RUN python -m pip install --disable-pip-version-check --no-cache-dir \
      --require-hashes --only-binary=:all: -r requirements-dev.lock \
    && python -m pip check
COPY src/finance_app_database_service ./finance_app_database_service
COPY tests ./tests
COPY migrations ./migrations
COPY alembic.ini ./
RUN python -m compileall -q finance_app_database_service tests migrations
USER 65532:65532
RUN python -m unittest discover -s tests -v

FROM dependencies AS runtime
COPY --from=verified /app/finance_app_database_service ./finance_app_database_service
COPY --from=verified /app/migrations ./migrations
COPY --from=verified /app/alembic.ini ./
ARG SOURCE_COMMIT
RUN case "$SOURCE_COMMIT" in *[!0-9a-f]*|'') exit 1 ;; esac \
    && test "${#SOURCE_COMMIT}" -eq 40
LABEL org.opencontainers.image.source="https://github.com/lward27/finance_app_database_service" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}"

USER 65532:65532
EXPOSE 8000

ENTRYPOINT ["opentelemetry-instrument", "python", "-m", "finance_app_database_service"]
