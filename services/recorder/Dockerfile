# services/recorder/Dockerfile
#
# PURPOSE:
#   Lightweight container that records the RTSP sub-stream to MPEG-TS
#   segments. Uses ffmpeg directly (no GPU, no Python decode).
#
# One-hour .ts segments stored as:
#   /recordings/{camera_id}/YYYY-MM-DD/HH-MM.ts

FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps — just redis, for the registry-fallback path that lets
# slot-based cameras (cam3+) discover their RTSP URL without hardcoding
# it in docker-compose.yml. See _load_rtsp_from_registry() in recorder.py.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY recorder.py .

CMD ["python", "-u", "recorder.py"]
