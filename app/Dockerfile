FROM python:3.11-slim

WORKDIR /opt/tinyscreen

RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

COPY collector/requirements.txt collector/requirements.txt
RUN pip install --no-cache-dir -r collector/requirements.txt flask

COPY collector/ collector/
COPY webflasher/ webflasher/
COPY app/server.py app/server.py
COPY app/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV TINYSCREEN_STATUS_FILE=/tmp/tinyscreen_status.json
ENV TINYSCREEN_SERIAL_PORT=""
ENV TINYSCREEN_CERT_DIR=/opt/tinyscreen/certs

EXPOSE 8989 8990

ENTRYPOINT ["/entrypoint.sh"]
