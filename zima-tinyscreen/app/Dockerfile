FROM python:3.11-slim

WORKDIR /opt/tinyscreen

COPY collector/requirements.txt collector/requirements.txt
RUN pip install --no-cache-dir -r collector/requirements.txt flask

COPY collector/ collector/
COPY webflasher/ webflasher/
COPY app/server.py app/server.py
COPY app/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV TINYSCREEN_STATUS_FILE=/tmp/tinyscreen_status.json
ENV TINYSCREEN_SERIAL_PORT=""

EXPOSE 8989

ENTRYPOINT ["/entrypoint.sh"]
