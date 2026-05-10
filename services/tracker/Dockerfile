# services/tracker/Dockerfile
#
# PURPOSE:
#   Container for the person tracker service. Lightweight CPU-only — no GPU needed.
#   Reads detections from Redis, tracks people across frames, publishes events.
#
# RELATIONSHIPS:
#   - Built and run by docker-compose.yml
#   - Reads from: detections:pose:{camera_id} (published by pose-detector)
#   - Writes to: events:{camera_id} and state:{camera_id}
#   - Uses: contracts/actions.py for action classification from keypoints
#   - No GPU required — pure Python + NumPy

FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY tracker.py .

# Note: contracts/actions.py is mounted as a volume by docker-compose.yml
# at /app/contracts/ (read-only). No need to COPY it here.

# Run the tracker
CMD ["python", "-u", "tracker.py"]
