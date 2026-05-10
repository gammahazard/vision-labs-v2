# services/face-recognizer/Dockerfile
#
# PURPOSE:
#   Face recognition service — uses InsightFace to generate face embeddings
#   and matches against a SQLite database of known faces.
#
# RELATIONSHIPS:
#   - Built and run by docker-compose.yml
#   - Reads from: frames + detections streams (Redis)
#   - Writes to: identities stream + identity_state hash (Redis)
#   - Stores data in: /data/faces.db (SQLite, volume-mounted)
#   - GPU required: ONNX Runtime with CUDA for InsightFace
#
# PORTS:
#   8081 — Enrollment REST API (used by dashboard)

FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04

# Prevent interactive prompts during apt install
ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3.11 and system deps for OpenCV + InsightFace
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    libgl1 \
    libglib2.0-0 \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python

WORKDIR /app

# Install Python dependencies (cached layer)
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY face_db.py .
COPY recognizer.py .

# Create data directory for SQLite
RUN mkdir -p /data

EXPOSE 8081

CMD ["python", "-u", "recognizer.py"]
