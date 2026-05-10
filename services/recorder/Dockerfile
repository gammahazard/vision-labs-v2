# services/recorder/Dockerfile
#
# PURPOSE:
#   Lightweight container that records the RTSP sub-stream to MP4 segments
#   on the QNAP NAS. Uses ffmpeg directly (no GPU, no Python decode).
#
# One-hour MP4 segments stored as:
#   /recordings/front_door/YYYY-MM-DD/HH-00.mp4

FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY recorder.py .

CMD ["python", "-u", "recorder.py"]
