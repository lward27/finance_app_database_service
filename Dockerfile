FROM python:3.11
USER root
WORKDIR /app

COPY requirements.txt .
COPY src/setup.py .
COPY src/finance_app_database_service ./finance_app_database_service

ENV PATH="/root/.local/bin:$PATH"
RUN pip install -r requirements.txt --user
RUN pip install . --user
RUN opentelemetry-bootstrap -a install

EXPOSE 8000

ENTRYPOINT [ "opentelemetry-instrument", "python", "-m", "finance_app_database_service" ]
