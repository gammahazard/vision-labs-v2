# services/base/Dockerfile
#
# PURPOSE:
#   Shared base image for every CUDA-using service in the stack
#   (pose-detector, vehicle-detector, face-recognizer). Bakes the
#   pieces that take 3-5 GB and never change between services:
#     - CUDA 12.8 runtime (Blackwell-compatible: sm_120 on the 5070 Ti)
#     - Python 3.11 + pip
#     - OpenCV's system libs (libgl1, libglib2.0-0, etc.)
#     - The Python deps that every detector imports (numpy, opencv,
#       redis client, async-timeout)
#
# WHY THIS EXISTS (Phase C — packaging):
#   Before: each detector image was ~18 GB because each rebuilt CUDA +
#   Python + system deps + ~20 Python packages from scratch. Three
#   detectors × three slot copies = 47+ GB of source layers on disk.
#
#   After: this base contains the common 3-5 GB. Detectors inherit it
#   via `FROM vision-labs-base:cuda12.8` and only add their framework
#   layer (torch+ultralytics, or onnxruntime+insightface). Docker's
#   layer dedup makes slot copies (pose-detector-cam2 etc.) cost only
#   their service-code layer — basically free.
#
# HOW TO BUILD:
#   docker build -t vision-labs-base:cuda12.8 services/base
#   (or use the build helper script — `scripts/build.sh`.)
#
#   `docker compose build` will not build this automatically — the
#   detector Dockerfiles reference it via `FROM`. Build the base FIRST.
#
# WHY NOT BAKE A FRAMEWORK (torch / onnxruntime) HERE?
#   Pose + vehicle want torch + ultralytics (~13 GB).
#   Face wants onnxruntime-gpu + insightface (~5 GB) — and DOES NOT
#   want torch (would add 5 GB of waste).
#   Splitting frameworks one layer higher lets face-recognizer skip the
#   torch tax.

# cuDNN variant — face-recognizer's onnxruntime-gpu links against system
# cuDNN. PyTorch wheels (used by pose/vehicle) bundle their own cuDNN so
# they don't strictly need this, but the ~800 MB cost gets shared across
# every detector via this single base layer.
FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System packages every detector needs.
# - python3.11 + venv + dev + pip — runtime; python3.11-dev is needed by
#   face-recognizer's insightface install (Cython compiles native ext).
# - build-essential — same reason; insightface ships C extensions.
# - libgl1 + glib + sm/xext/xrender — OpenCV's GUI shims (need them even
#   though we use opencv-python-headless; the JPEG decoder still links them)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3.11 \
      python3.11-venv \
      python3.11-dev \
      python3-pip \
      build-essential \
      libgl1 \
      libglib2.0-0 \
      libsm6 \
      libxext6 \
      libxrender1 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python

# Common Python deps every detector imports — pinned identically to
# what each service's requirements.txt was specifying, but installed
# once here so the layer is reused.
RUN python -m pip install --no-cache-dir \
      "redis==5.2.1" \
      "async-timeout>=4.0.0" \
      "numpy>=1.24.0,<2" \
      "opencv-python-headless==4.10.0.84"

# No CMD — this image is never run directly; it's purely a FROM target.
