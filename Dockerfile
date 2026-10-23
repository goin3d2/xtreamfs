FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends fuse3 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY xtreamfs.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

ENV MOUNT_POINT=/mnt/xtream \
    CACHE_DIR=/config \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/entrypoint.sh"]
