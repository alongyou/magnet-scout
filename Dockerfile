# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="Magnet Scout" \
      org.opencontainers.image.description="Tracker and DHT resource visibility probe" \
      org.opencontainers.image.source="https://github.com/alongyou/magnet-scout" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MAGNET_SCOUT_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 scout \
    && useradd --uid 10001 --gid scout --no-create-home scout \
    && mkdir /data \
    && chown scout:scout /data

COPY magnet_api.py tracker_probe.py tracker_select.py dht_probe.py paths.py ./
COPY magnets.txt dht_bootstrap.txt LICENSE ./
COPY scripts/container_entrypoint.py ./scripts/

USER scout
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import json, urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3))['ok']"

ENTRYPOINT ["python", "scripts/container_entrypoint.py"]
CMD ["python", "-m", "uvicorn", "magnet_api:app", "--host", "0.0.0.0", "--port", "8765"]
