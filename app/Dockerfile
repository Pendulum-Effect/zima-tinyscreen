FROM python:3.11-slim

WORKDIR /opt/tinyscreen

RUN apt-get update && apt-get install -y --no-install-recommends openssl bash smartmontools \
    && rm -rf /var/lib/apt/lists/*

COPY collector/requirements.txt collector/requirements.txt
COPY app/requirements.txt app/requirements.txt
# Everything pinned exactly (see the comments in the two requirements
# files): the self-updater always re-pulls, so unpinned deps meant two
# builds of the same code could differ.
RUN pip install --no-cache-dir -r collector/requirements.txt -r app/requirements.txt

COPY collector/ collector/
COPY webflasher/ webflasher/
COPY app/server.py app/server.py
# Stage 2 self-updater: minimal Docker Engine API client, the swap
# helper that runs in the short-lived updater container, and the
# canonical deployment contract the helper validates against.
COPY app/dockerapi.py app/dockerapi.py
COPY app/self_update_helper.py app/self_update_helper.py
COPY app/canonical_config.json app/canonical_config.json
# About-page content: the repo copies are the source of truth (the server
# fetches them live from GitHub so edits apply without a release), but
# baked copies keep the About tab working when GitHub is unreachable.
COPY about.json webflasher/about.json
COPY CHANGELOG.json webflasher/CHANGELOG.json
COPY app/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV TINYSCREEN_STATUS_FILE=/tmp/tinyscreen_status.json
ENV TINYSCREEN_SERIAL_PORT=""
ENV TINYSCREEN_CERT_DIR=/opt/tinyscreen/certs

EXPOSE 8989 8990

ENTRYPOINT ["/entrypoint.sh"]
