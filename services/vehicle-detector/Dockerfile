# services/vehicle-detector/Dockerfile
#
# PURPOSE:
#   GPU-enabled container for YOLO vehicle detection. Uses NVIDIA CUDA base image
#   so PyTorch can access the GPU for inference.
#
# RELATIONSHIPS:
#   - Built and run by docker-compose.yml
#   - Requires NVIDIA Container Toolkit (for --gpus flag)
#   - Reads frames from Redis, publishes vehicle detections to Redis

FROM nvidia/cuda:12.8.0-runtime-ubuntu22.04

# Prevent interactive prompts during apt install
ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3.11 and system deps for OpenCV
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3-pip \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python

WORKDIR /app

# Install PyTorch with CUDA 12.8 BEFORE other deps so ultralytics doesn't pull in
# the default CPU/cu121 wheel from PyPI. Required for Blackwell (5070 Ti, sm_120).
RUN python -m pip install --no-cache-dir \
    torch torchvision \
    --index-url https://download.pytorch.org/whl/cu128

# Install Python dependencies (cached layer)
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY detector.py .

# Run the detector — unbuffered output for real-time Docker logs
CMD ["python", "-u", "detector.py"]
