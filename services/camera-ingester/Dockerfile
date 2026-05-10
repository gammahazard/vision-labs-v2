# services/camera-ingester/Dockerfile
#
# PURPOSE:
#   Container for the camera ingester service. Reads RTSP stream from the
#   Reolink camera and publishes JPEG frames to Redis Streams.
#
# RELATIONSHIPS:
#   - Built and run by docker-compose.yml
#   - Needs network access to the camera (192.168.2.10) and Redis
#   - Config comes from environment variables (set in docker-compose.yml / .env)
#
# BUILD:
#   docker build -t camera-ingester .
#   (or just use docker-compose up --build)

FROM python:3.11-slim

# Install system dependencies for OpenCV
# libgl1 + libglib2.0 are needed even for headless OpenCV (video capture)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer if requirements don't change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY ingester.py .

# Run the ingester
CMD ["python", "-u", "ingester.py"]
