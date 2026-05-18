# services/pose-detector/Dockerfile
#
# PURPOSE:
#   GPU-enabled container for YOLO pose detection.
#
# INHERITS FROM `vision-labs-base:cuda12.8` (services/base/Dockerfile).
#   The base already has: CUDA 12.8 runtime, Python 3.11, system libs for
#   OpenCV, and the common pip deps (numpy, opencv, redis, async-timeout).
#   This file only adds the pose-specific framework layer.
#
# IF YOU SEE "FROM vision-labs-base: pull access denied":
#   The base image isn't built locally yet. Run:
#     docker build -t vision-labs-base:cuda12.8 services/base
#   (Or `bash scripts/build.sh` to build base + all services in order.)

FROM vision-labs-base:cuda12.8

WORKDIR /app

# Install PyTorch with CUDA 12.8 BEFORE ultralytics so we don't end up
# with the default cu121 wheel from PyPI. Required for Blackwell (sm_120).
RUN python -m pip install --no-cache-dir \
      torch torchvision \
      --index-url https://download.pytorch.org/whl/cu128

# Service-specific deps (ultralytics pulls torch as a dep, but we already
# installed our CUDA-pinned version above so pip just verifies the constraint)
RUN python -m pip install --no-cache-dir "ultralytics>=8.3.0"

COPY detector.py .

CMD ["python", "-u", "detector.py"]
