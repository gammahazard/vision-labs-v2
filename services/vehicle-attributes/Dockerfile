# services/vehicle-attributes/Dockerfile
#
# PURPOSE:
#   Per-camera vehicle attribute pipeline (Phase 1: capture + group only,
#   no classifier yet). Consumes events:{cam}, maintains per-track HD-crop
#   buffers, writes /data/snapshots/vehicles/{cam}/{date}/{track_id}/.
#
# NO GPU IN PHASE 1.
#   Cropping + JPEG encode is CPU work. We deliberately use a slim
#   python:3.11 base (not vision-labs-base:cuda12.8) to keep the image
#   under 200 MB. Phase 3 will swap this base when the classifier ships.

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY service.py buffer.py storage.py ./

RUN mkdir -p /data

CMD ["python", "-u", "service.py"]
