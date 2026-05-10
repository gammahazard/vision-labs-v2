# services/dashboard/Dockerfile
#
# PURPOSE:
#   Serves the Vision Labs web dashboard — live camera view with detection
#   overlays, settings panel, and event feed.
#
# RELATIONSHIPS:
#   - Built and run by docker-compose.yml
#   - Reads from Redis (frames, detections, events, config)
#   - Serves frontend at http://localhost:8080
#   - No GPU needed — just a web server

FROM python:3.11-slim

# System deps for OpenCV (frame overlay drawing) + ffmpeg (browser-compatible video encoding)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY server.py .
COPY ai_db.py .
COPY routes/ routes/

# Copy frontend files
COPY static/ static/

EXPOSE 8080

CMD ["python", "-u", "server.py"]
