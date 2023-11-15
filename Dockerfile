FROM python:3.11
USER root
WORKDIR /app

COPY requirements.txt .
COPY src/setup.py .
COPY src/finance_app_database_service ./finance_app_database_service

RUN pip install -r requirements.txt --user
RUN pip install . --user

LABEL "traefik.http.services.finance_app_database_service.loadbalancer.server.port"=8000

EXPOSE 8000

ENTRYPOINT [ "python", "/app/finance_app_database_service" ]
