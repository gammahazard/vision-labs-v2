# services/vehicle-attributes/Dockerfile
#
# PURPOSE:
#   Per-camera vehicle attribute pipeline.
#
#   Phase 1: capture HD crops on vehicle_sample events, write per-track
#   dirs with metadata.json (null attributes block) at vehicle_gone flush.
#
#   Phase 3 (this version): runs a ConvNeXt-Tiny multi-head classifier
#   (color + body + make + model heads) on the buffered crops at flush
#   time, fills the attributes block. Gated by ENABLE_CLASSIFIER env so
#   the image can be deployed before trained weights exist on HF Hub.
#
# INHERITS FROM vision-labs-base:cuda12.8 (services/base/Dockerfile).
# vision-labs-base does NOT ship PyTorch — each service installs the
# framework it needs on top. Mirror of pose-detector's pattern.

FROM vision-labs-base:cuda12.8

WORKDIR /app

# PyTorch matched to the base image's CUDA 12.8 runtime.
# Use `python -m pip` (not bare `pip`) so the install lands in the SAME
# interpreter that `CMD ["python", ...]` runs against. Bare `pip` in the
# base image resolves to Ubuntu's python3.10 while `python` resolves to
# python3.11 — bare `pip install torch` silently installs it where the
# service can't find it. Mirror of pose-detector.
RUN python -m pip install --no-cache-dir \
      torch torchvision \
      --index-url https://download.pytorch.org/whl/cu128

# Service deps
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# Source files + the static class label JSONs
COPY service.py buffer.py storage.py classifier.py ./
COPY classes/ ./classes/

# Snapshot output dir gets bind-mounted at runtime
RUN mkdir -p /data /models

CMD ["python", "-u", "service.py"]
