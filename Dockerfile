FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_PATH=/config/config.yaml \
    STATE_PATH=/data/state.json

WORKDIR /opt/pvoutput-exporter

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd --system --uid 1000 exporter \
    && mkdir -p /data /config \
    && chown exporter /data
USER exporter
VOLUME /data

ENTRYPOINT ["python", "-m", "app.main"]
