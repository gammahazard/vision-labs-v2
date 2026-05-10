FROM nvidia/cuda:12.8.0-runtime-ubuntu22.04

# Prevent interactive prompts during apt install
ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3.11, system deps for ComfyUI + video generation
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    git wget ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python

WORKDIR /app

# Install PyTorch with CUDA 12.8 (required for Blackwell sm_120 / RTX 5070 Ti)
RUN python -m pip install --no-cache-dir \
    torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cu128

# Clone ComfyUI — v0.15.0 includes EmptyWanLatentVideo for proper t2v generation
RUN git clone https://github.com/comfyanonymous/ComfyUI.git /app && \
    cd /app && git checkout v0.15.0 && \
    python -m pip install --no-cache-dir -r requirements.txt

# Install AnimateDiff-Evolved custom node — pinned version
RUN cd /app/custom_nodes && \
    git clone https://github.com/Kosinkadink/ComfyUI-AnimateDiff-Evolved.git && \
    cd ComfyUI-AnimateDiff-Evolved && git checkout v1.20.0 && \
    python -m pip install --no-cache-dir -r requirements.txt || true

# Install VideoHelperSuite (for video output nodes — GIF/MP4 encoding) — pinned version
RUN cd /app/custom_nodes && \
    git clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git && \
    cd ComfyUI-VideoHelperSuite && git checkout v1.5.0 && \
    python -m pip install --no-cache-dir -r requirements.txt || true

# Install IPAdapter Plus custom node — pinned version
RUN cd /app/custom_nodes && \
    git clone https://github.com/cubiq/ComfyUI_IPAdapter_plus.git && \
    cd ComfyUI_IPAdapter_plus && git checkout V3.0 && \
    python -m pip install --no-cache-dir -r requirements.txt || true

# Install GGUF support (enables FP8 quantized WAN models for lower VRAM usage)
RUN cd /app/custom_nodes && \
    git clone https://github.com/city96/ComfyUI-GGUF.git && \
    cd ComfyUI-GGUF && \
    python -m pip install --no-cache-dir -r requirements.txt || true

# Ensure critical deps are installed LAST (after all custom nodes, so nothing overwrites them)
# - pyyaml: ComfyUI's utils/extra_config.py requires 'import yaml'
# - requests: ComfyUI's frontend_management.py requires 'import requests'
# - opencv-python-headless: VideoHelperSuite requires 'import cv2'
RUN python -m pip install --no-cache-dir pyyaml requests opencv-python-headless imageio-ffmpeg && \
    python -c "import yaml; import requests; import cv2; import imageio_ffmpeg; print('deps OK')"

# Model downloads moved to entrypoint.sh — they must live on the host
# volume (./models/comfyui:/app/models) to persist across rebuilds.
# The Dockerfile previously downloaded them here but the volume mount
# would override /app/models with an empty directory at runtime.

# Copy entrypoint script (handles first-run model downloads)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Default port
EXPOSE 8188

# Entrypoint downloads missing models, then starts ComfyUI
ENTRYPOINT ["/entrypoint.sh"]
